#!/usr/bin/env python3
"""Step 10 two-host SGLang acceptance. Requires explicit authorization of the lab.

--prepare uploads an explicit source list and builds the dev image/binaries.
The default runs both qualified model profiles and collects assertions/cleanup.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
REMOTE = '/home/ruslixag/cocoon-step10'


class Lab:
    def __init__(self, output, backend="sglang"):
        self.backend = backend
        self.remote_dir = REMOTE if backend == "sglang" else "/home/ruslixag/cocoon-step11"
        self.image = "cocoon-step10-dev" if backend == "sglang" else "cocoon-step11-dev"
        self.lab = json.loads((ROOT/'experiments/gpu-pipeline/lab.json').read_text())
        self.key = str(Path(self.lab['ssh_key']).expanduser())
        self.output = output
        self.output.mkdir(parents=True, exist_ok=True)

    def ssh(self, rank, argv, data=None, timeout=60):
        try:
            return subprocess.check_output(['ssh','-i',self.key,'-o','BatchMode=yes','-o','ConnectTimeout=10',
                self.lab['hosts'][rank]['ssh'],shlex.join(argv)], input=data, text=True, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.CalledProcessError as exc:
            path = self.output / ('ssh-failure-' + str(time.time_ns()) + '.log')
            path.write_text((exc.stdout or '') + '\n' + (exc.stderr or ''))
            raise RuntimeError(f'host {rank} command failed; diagnostics: {path}') from exc

    def both(self, fn):
        with ThreadPoolExecutor(2) as pool: return list(pool.map(fn, range(2)))

    def remote(self, host_rank, run, action, timeout=60, **fields):
        return json.loads(self.ssh(host_rank, ['sudo','-n','python3',self.remote_dir+'/src/test/sglang-gpu/host.py'],
            json.dumps({'run':run,'action':action,'backend':self.backend,**fields}),timeout))

    def upload(self):
        files = subprocess.check_output(['git','ls-files','--recurse-submodules','-z'],cwd=ROOT).decode().split('\0')
        files = {p for p in files if p and not any(x.startswith('.') for x in Path(p).parts) and (ROOT/p).is_file()}
        for pattern in ('pipeline/Sglang.*','pipeline/sglang-*','pipeline/profiles/sglang-*.json','pipeline/profiles/vllm-*.json','test/test-pipeline-sglang*','test/sglang-gpu/*','pipeline/Vllm.*','pipeline/vllm*','pipeline/engine-helper.py','test/test-pipeline-vllm*'):
            files.update(str(p.relative_to(ROOT)) for p in ROOT.glob(pattern) if p.is_file())
        manifest = self.output/'transfer-files.txt'; manifest.write_text('\n'.join(sorted(files))+'\n')
        def one(rank):
            actual = self.ssh(rank,['hostname']).strip()
            if actual != self.lab['hosts'][rank]['hostname']: raise ValueError('host identity mismatch')
            self.ssh(rank,['mkdir','-p',self.remote_dir+'/src',self.remote_dir+'/build'])
            subprocess.run(['rsync','-az','--files-from='+str(manifest),'-e','ssh -i '+shlex.quote(self.key)+' -o BatchMode=yes',
                str(ROOT)+'/',self.lab['hosts'][rank]['ssh']+':'+self.remote_dir+'/src/'],check=True)
        self.both(one)

    def prepare(self):
        print('Uploading explicit source list',flush=True)
        self.upload()
        def image(rank):
            output = self.ssh(rank,['sudo','-n','docker','build','-t',self.image,'--build-arg','BASE='+json.loads((ROOT/'experiments/gpu-pipeline/profiles.json').read_text())['backends'][self.backend]['image'],self.remote_dir+'/src/test/sglang-gpu'],timeout=1800)
            (self.output/f'image-{rank}.log').write_text(output)
        self.both(image)
        print('Building Cocoon in pinned '+self.backend+' runtime',flush=True)
        script = ('cmake -S /work/cocoon -B /work/build -G Ninja -USECP256K1_LIBRARY -DCMAKE_BUILD_TYPE=Release '
                  '-DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ '
                  '-DTON_ONLY_TONLIB=ON -DTON_USE_ROCKSDB=ON -DTDDB_USE_ROCKSDB=ON '
                  '-DBUILD_TESTING=OFF -DCOCOON_ARCH=x86-64 -DTON_ARCH=x86-64 '
                  '&& cmake --build /work/build --target pipeline-agent-dev pipeline-backend-sandbox '
                  'client-runner proxy-runner worker-runner key-manager-runner '
                  'cocoon-subst router encrypt-message test-pipeline-sglang test-pipeline-vllm test-pipeline-profile -j 6')
        built = self.ssh(0,['sudo','-n','docker','run','--rm','--network','host',
            '-v',self.remote_dir+'/src:/work/cocoon','-v',self.remote_dir+'/build:/work/build',
            self.image,'sh','-c',script],timeout=3600)
        (self.output/'build.log').write_text(built)
        print('Cocoon build complete',flush=True)
        # The member only needs the same agent/sandbox binaries; it never runs a worker.
        local = self.output/'binaries'; local.mkdir(exist_ok=True)
        for name in ('pipeline-agent-dev','pipeline-backend-sandbox'):
            data = subprocess.check_output(['ssh','-i',self.key,self.lab['hosts'][0]['ssh'],
                shlex.join(['cat',self.remote_dir+'/build/pipeline/'+name])])
            (local/name).write_bytes(data)
        self.ssh(1,['mkdir','-p',self.remote_dir+'/build/pipeline'])
        subprocess.run(['scp','-i',self.key,*map(str,local.iterdir()),self.lab['hosts'][1]['ssh']+':'+self.remote_dir+'/build/pipeline/'],check=True)
        self.ssh(1,['chmod','755',self.remote_dir+'/build/pipeline/pipeline-agent-dev',self.remote_dir+'/build/pipeline/pipeline-backend-sandbox'])

    def trial(self, model):
        run = secrets.token_hex(6)
        out = self.output/(model+'-'+run); out.mkdir()
        result = {'run':run,'model':model,'passed':False}
        created = []
        try:
            for rank in (1,0):
                created.append(rank)
                info = self.remote(rank,run,'create',rank=rank,model=model)
                (out/f'created-{rank}.json').write_text(json.dumps(info,indent=2))
            self.both(lambda rank:self.remote(rank,run,'start'))
            self.remote(0,run,'driver',model=model)
            faulted = False
            resource_id = None
            deadline = time.monotonic()+2400
            previous = ''
            while time.monotonic() < deadline:
                progress = self.remote(0,run,'progress')
                if progress.get('tail','') != previous:
                    previous = progress.get('tail','')
                    print(model,previous[-1000:],flush=True)
                if progress.get('fault') and not faulted:
                    info = self.remote(1,run,'kill-member',epoch=progress['fault']['epoch'])
                    (out/'fault.json').write_text(json.dumps(info,indent=2)); faulted = True
                if progress.get('resource') and progress['resource']['id'] != resource_id:
                    spec = progress['resource']
                    info = self.remote(1,run,'resources',phase=spec['phase'])
                    self.remote(0,run,'resource-response',response={'id':spec['id'], **info})
                    resource_id = spec['id']
                if progress.get('result'):
                    result['acceptance'] = progress['result']
                    if not result['acceptance']['passed']: raise RuntimeError(result['acceptance'].get('error','GPU acceptance failed'))
                    if not faulted: raise AssertionError('member fault was not injected')
                    break
                if not progress.get('driver_alive',True): raise RuntimeError('acceptance driver exited without report')
                time.sleep(2)
            else: raise TimeoutError('GPU acceptance deadline')
            result['final_agents'] = self.both(lambda rank:self.remote(rank,run,'status'))
            if not all(s['group_ready'] for s in result['final_agents']): raise AssertionError('group not recovered')
            evidence = self.both(lambda rank:self.remote(rank,run,'evidence'))
            for rank, value in enumerate(evidence):
                for path, digest in value['source_sha256'].items():
                    if hashlib.sha256((ROOT/path).read_bytes()).hexdigest() != digest:
                        raise AssertionError('tested source differs: ' + path)
                (out/f'evidence-{rank}.json').write_text(json.dumps(value,indent=2))
            if evidence[0]['binary_sha256'] != evidence[1]['binary_sha256']:
                raise AssertionError('agent binaries differ between ranks')
            if evidence[0]['versions'] != evidence[1]['versions']:
                raise AssertionError('backend versions differ between ranks')
        except BaseException as exc:
            result['error'] = str(exc)
            raise
        finally:
            cleanup = {}
            for rank in created:
                try: cleanup[str(rank)] = self.remote(rank,run,'stop',timeout=90)
                except Exception as exc: cleanup[str(rank)] = {'passed':False,'error':str(exc)}
            result['cleanup'] = cleanup
            result['passed'] = bool(result.get('acceptance',{}).get('passed') and len(cleanup)==2 and all(x['passed'] for x in cleanup.values()) and 'error' not in result)
            (out/'result.json').write_text(json.dumps(result,indent=2))
            for rank in created:
                # Logs, status and fake-TON fixtures only; no host credentials.
                copied = subprocess.run(['rsync','-az','--no-specials','--no-devices','-e','ssh -i '+shlex.quote(self.key),'--rsync-path=sudo -n rsync',
                    self.lab['hosts'][rank]['ssh']+':'+self.remote_dir+'/runs/'+run+'/',str(out/f'rank-{rank}')+'/'])
                if copied.returncode:
                    result['passed'] = False
                    result.setdefault('collection_errors',[]).append(rank)
            (out/'result.json').write_text(json.dumps(result,indent=2))
        if not result['passed']: raise AssertionError('GPU cleanup did not pass')
        print('PASS GPU',self.backend,model,'artifacts:',out,flush=True)
        return result


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--backend', choices=['sglang','vllm'], default='sglang')
    p.add_argument('--prepare', action='store_true')
    p.add_argument('--upload-only', action='store_true')
    p.add_argument('--model', choices=['small','large','both'], default='both')
    p.add_argument('--output-dir', type=Path, default=None)
    args = p.parse_args()
    output = args.output_dir or ROOT/('build/step10/gpu' if args.backend=='sglang' else 'build/step11/gpu')
    lab = Lab(output.resolve(), args.backend)
    if args.upload_only: lab.upload()
    else:
        if args.prepare: lab.prepare()
        for model in (('small','large') if args.model=='both' else (args.model,)): lab.trial(model)
