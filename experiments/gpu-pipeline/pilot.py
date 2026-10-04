#!/usr/bin/env python3
"""Reproducible dev GPU pilot. Only stdlib is required on the control machine."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import hashlib
import json
import secrets
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

from evidence import capture_summary, network_isolation

HERE = Path(__file__).resolve().parent


def read(path):
    return json.loads(Path(path).read_text())


def write(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + '\n')


def environment(host):
    return {'NCCL_NET': 'Socket', 'NCCL_SOCKET_IFNAME': '=wg0',
            'GLOO_SOCKET_IFNAME': 'wg0', 'NCCL_IB_DISABLE': '1',
            'NCCL_P2P_DISABLE': '1', 'NCCL_SHM_DISABLE': '1',
            'NCCL_DEBUG': 'INFO', 'NCCL_DEBUG_SUBSYS': 'INIT,NET,ENV',
            'NCCL_SOCKET_FAMILY': 'AF_INET', 'VLLM_HOST_IP': host['overlay'],
            'SGLANG_HOST_IP': host['overlay'],
            'TORCH_NCCL_ASYNC_ERROR_HANDLING': '1',
            'TORCH_NCCL_WAIT_TIMEOUT_DUMP_MILSEC': '1000',
            # vLLM's default RPC wait is 300 s; bound loss detection in this lab.
            'VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS': '30',
            'HF_HUB_OFFLINE': '1', 'HF_DATASETS_OFFLINE': '1',
            'OMP_NUM_THREADS': '4', 'PYTHONUNBUFFERED': '1'}


def backend_argv(profiles, lab, backend, model, pp, rank):
    assert pp in (1, 2) and 0 <= rank < pp
    spec = profiles['models'][model]
    path = '/models/' + spec['id'].split('/')[-1] + '/' + spec['revision']
    common = ['--served-model-name', 'pilot', '--dtype', profiles['dtype']]
    if backend == 'sglang':
        args = ['python3', '-m', 'sglang.launch_server', '--model-path', path, *common,
                '--tp-size', '1', '--pp-size', str(pp), '--nnodes', str(pp), '--node-rank', str(rank),
                '--host', '127.0.0.1', '--port', '30000', '--disable-radix-cache', '--enable-cache-report',
                '--disable-cuda-graph', '--disable-piecewise-cuda-graph', '--disable-overlap-schedule',
                '--chunked-prefill-size', str(profiles['backends']['sglang']['chunked_prefill_size']),
                '--attention-backend', 'triton', '--sampling-backend', 'pytorch',
                '--context-length', str(profiles['max_model_len']), '--max-running-requests', '1',
                '--max-total-tokens', str(profiles['max_model_len']),
                '--mem-fraction-static', str(profiles['gpu_memory_fraction'])]
        if pp > 1:
            args += ['--dist-init-addr', lab['hosts'][0]['overlay'] + ':29500']
        return args
    if backend != 'vllm':
        raise ValueError(backend)
    args = ['vllm', 'serve', path, *common, '--distributed-executor-backend', 'mp',
            '--tensor-parallel-size', '1', '--pipeline-parallel-size', str(pp),
            '--nnodes', str(pp), '--node-rank', str(rank), '--master-addr', lab['hosts'][0]['overlay'],
            '--master-port', '29501', '--max-model-len', str(profiles['max_model_len']),
            '--max-num-seqs', '1', '--max-num-batched-tokens', str(profiles['max_model_len']),
            '--gpu-memory-utilization', str(profiles['gpu_memory_fraction']),
            '--cpu-offload-gb', '0', '--enforce-eager', '--no-enable-prefix-caching']
    return args + (['--headless'] if rank else ['--host', '127.0.0.1', '--port', '30000'])


class Pilot:
    def __init__(self, lab, profiles):
        if lab['mode'] != 'dev-only' or profiles['mode'] != 'dev-only':
            raise ValueError('this pilot only accepts dev-only profiles')
        self.lab, self.profiles = lab, profiles
        self.key = str(Path(lab['ssh_key']).expanduser())

    def ssh(self, rank, argv, *, stdin=None, timeout=120):
        return subprocess.check_output(['ssh', '-i', self.key, '-o', 'BatchMode=yes',
                    '-o', 'ConnectTimeout=10', self.lab['hosts'][rank]['ssh'], shlex.join(argv)],
                    input=stdin, text=True, timeout=timeout, stderr=subprocess.PIPE)

    def remote(self, rank, run, action, **data):
        return json.loads(self.ssh(rank, ['sudo', '-n', 'python3', self.lab['remote_dir'] + '/host.py'],
                     stdin=json.dumps(dict(run=run, action=action, **data)), timeout=data.get('timeout', 120)+15))

    def both(self, fn):
        with ThreadPoolExecutor(2) as pool:
            return list(pool.map(fn, range(2)))

    def upload(self):
        files = [str(p) for p in HERE.glob('*.py')] + [str(HERE / 'profiles.json')]
        for rank in range(2):
            actual = self.ssh(rank, ['hostname']).strip()
            if actual != self.lab['hosts'][rank]['hostname']:
                raise ValueError(f'host mismatch: {actual}')
            self.ssh(rank, ['mkdir', '-p', self.lab['remote_dir']])
            subprocess.run(['scp', '-i', self.key, *files,
                            self.lab['hosts'][rank]['ssh'] + ':' + self.lab['remote_dir'] + '/'], check=True)

    def prepare(self, stop_existing):
        self.upload()
        for rank, host in enumerate(self.lab['hosts']):
            if stop_existing:
                self.ssh(rank, ['sudo', 'docker', 'stop', '--timeout', '20', host['existing_gpu_container']])
            self.ssh(rank, ['sudo', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y',
                            'wireguard-tools', 'iperf3', 'tcpdump'], timeout=600)
            for spec in self.profiles['backends'].values():
                self.ssh(rank, ['sudo', 'docker', 'pull', spec['image']], timeout=2400)
            self.ssh(rank, ['sudo', 'docker', 'run', '--rm', '--name', 'cocoon-pilot-fetch',
                       '--entrypoint', 'python3', '-v', self.lab['remote_dir'] + ':/pilot:ro',
                       '-v', self.lab['remote_dir'] + '/models:/models',
                       self.profiles['backends']['vllm']['image'], '/pilot/fetch_models.py'], timeout=3600)

    def verify_models(self, model):
        spec = self.profiles['models'][model]
        path = self.lab['remote_dir'] + '/models/' + spec['id'].split('/')[-1] + '/' + spec['revision'] + '/manifest.json'
        manifests = self.both(lambda rank: json.loads(self.ssh(rank, ['cat', path])))
        if manifests[0] != manifests[1] or manifests[0]['model'] != spec:
            raise ValueError('model manifests differ between ranks or profile')
        return manifests[0]

    def network(self, run, out):
        result = {}
        for overlay, addr in [(False, self.lab['hosts'][1]['lan']), (True, self.lab['hosts'][1]['overlay'])]:
            name = 'wireguard' if overlay else 'lan'
            result[name] = {}
            result[name]['ping'] = self.remote(0, run, 'net', overlay=overlay,
                                      argv=['ping', '-c', '8', '-i', '0.2', '-M', 'do', '-s', '1292', addr])
            self.remote(1, run, 'net', overlay=overlay, background=True, timeout=15, log=name+'-iperf.log',
                        argv=['iperf3', '-s', '-1', '-B', addr, '-p', '5209'])
            result[name]['cpu_before'] = self.ssh(0, ['head', '-n', '1', '/proc/stat']).strip()
            result[name]['iperf'] = json.loads(self.remote(0, run, 'net', overlay=overlay,
                         argv=['iperf3', '-c', addr, '-p', '5209', '-t', '5', '-J'])['output'])
            result[name]['cpu_after'] = self.ssh(0, ['head', '-n', '1', '/proc/stat']).strip()
        write(out / 'network.json', result)

    def wait_file(self, rank, run, file, timeout):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            path = self.lab['remote_dir'] + '/runs/' + run + '/' + file
            found = self.ssh(rank, ['python3', '-c',
                       'from pathlib import Path; p=Path(' + repr(path) + '); print(p.read_text() if p.exists() else "PENDING")'])
            if found.strip() != 'PENDING':
                return found
            time.sleep(2)
        raise TimeoutError(file)

    def trial(self, backend, model, pp, output, network=False):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S')
        run = f'{stamp}-{backend}-{model}-pp{pp}-{secrets.token_hex(3)}'
        out = output / run
        out.mkdir(parents=True, exist_ok=False)
        source_dir = out / 'source'
        source_dir.mkdir()
        for path in HERE.glob('*.py'):
            (source_dir/path.name).write_bytes(path.read_bytes())
        write(out/'source-sha256.json', {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_dir.iterdir()})
        result = {'run': run, 'backend': backend, 'model': model, 'pp': pp, 'passed': False}
        write(out / 'lab.json', self.lab)
        write(out / 'profiles.json', self.profiles)
        print('START ' + run, flush=True)
        try:
            write(out / 'model-manifest.json', self.verify_models(model))
            created = self.both(lambda rank: self.remote(rank, run, 'create',
                     image=self.profiles['backends'][backend]['image'], env=environment(self.lab['hosts'][rank]),
                     overlay=self.lab['hosts'][rank]['overlay'], mtu=self.lab['wireguard_mtu'], port=self.lab['wireguard_port']))
            for rank in range(2):
                self.remote(rank, run, 'peer', public_key=created[1-rank]['public_key'],
                           overlay=self.lab['hosts'][1-rank]['overlay'], lan=self.lab['hosts'][1-rank]['lan'], port=self.lab['wireguard_port'])
            before = self.both(lambda rank: self.remote(rank, run, 'status'))
            write(out / 'before.json', before)
            write(out / 'isolation.json', [network_isolation(x) for x in before])
            if network:
                self.network(run, out)
            spec = self.profiles['models'][model]
            model_path = '/models/' + spec['id'].split('/')[-1] + '/' + spec['revision']
            for rank in range(pp):
                meta = self.remote(rank, run, 'exec', argv=['python3', '/pilot/metadata.py', model_path])
                write(out / f'versions-{rank}.json', json.loads(meta['output']))
                self.remote(rank, run, 'net', overlay=False, background=True, timeout=900,
                            log='gpu-samples.csv', argv=['nvidia-smi',
                            '--query-gpu=timestamp,memory.used,utilization.gpu', '--format=csv', '-lms', '200'])
            if pp == 2:
                for rank in range(2):
                    self.remote(rank, run, 'net', overlay=False, background=True, timeout=900,
                        log='capture.log', argv=['tcpdump', '-i', self.lab['underlay_interface'], '-nn',
                            '-s', '80', '-U', '-c', '50000', '-Z', 'root', '-w',
                            self.lab['remote_dir'] + '/runs/' + run + '/underlay.pcap',
                            'host', self.lab['hosts'][1-rank]['lan']])
                for rank in range(2):
                    self.remote(rank, run, 'launch', argv=['python3', '/pilot/nccl_probe.py', str(rank)], log='nccl.log')
                for rank in range(2):
                    write(out / f'nccl-{rank}.json', json.loads(self.wait_file(rank, run, f'nccl-{rank}.json', 180)))
            for rank in range(pp):
                self.remote(rank, run, 'launch', argv=backend_argv(self.profiles, self.lab, backend, model, pp, rank), log='server.log')
            deadline, last_report = time.monotonic()+900, 0
            while time.monotonic() < deadline:
                try:
                    self.remote(0, run, 'exec', argv=['python3', '/pilot/probe.py', 'ready'], timeout=8)
                    break
                except subprocess.CalledProcessError:
                    for rank in range(pp):
                        health = self.remote(rank, run, 'health')
                        if health['exit_code'] is not None:
                            raise RuntimeError(f'backend rank {rank} exited: {health}')
                    if time.monotonic()-last_report > 30:
                        print('Waiting for model readiness: ' + run, flush=True)
                        last_report = time.monotonic()
                    time.sleep(5)
            else:
                raise TimeoutError('model readiness')
            write(out / 'ready.json', self.both(lambda rank: self.remote(rank, run, 'status')))
            api = json.loads(self.remote(0, run, 'exec', argv=['python3', '/pilot/probe.py', 'suite'], timeout=300)['output'])
            write(out / 'api.json', api)
            write(out / 'after.json', self.both(lambda rank: self.remote(rank, run, 'status')))
            if pp == 2:
                self.remote(0, run, 'launch', argv=['python3', '/pilot/probe.py', 'fault'], log='fault.log')
                self.wait_file(0, run, 'fault-started', 60)
                self.remote(1, run, 'kill-stage')
                fault = json.loads(self.wait_file(0, run, 'fault.json', 90))
                write(out / 'fault.json', fault)
                if not fault['passed']:
                    raise AssertionError('stage-loss did not produce a bounded backend/transport error: ' + fault['failure_mode'])
            result['passed'] = True
        except Exception as exc:
            result['error'] = repr(exc)
            if isinstance(exc, subprocess.CalledProcessError):
                result['stderr'] = exc.stderr
            print('FAIL ' + run + ': ' + repr(exc), flush=True)
        finally:
            cleanup = []
            for rank in range(2):
                try:
                    cleanup.append(self.remote(rank, run, 'stop'))
                except Exception as exc:
                    cleanup.append({'error': repr(exc)})
                    result['passed'] = False
                try:
                    subprocess.run(['scp', '-C', '-r', '-i', self.key,
                           self.lab['hosts'][rank]['ssh'] + ':' + self.lab['remote_dir'] + '/runs/' + run,
                           str(out / f'rank{rank}')], check=True, timeout=120)
                except Exception as exc:
                    result['collection_error'] = repr(exc)
                    result['passed'] = False
            write(out / 'cleanup.json', cleanup)
            if pp == 2:
                try:
                    captures = [capture_summary(out/f'rank{rank}'/'underlay.pcap', self.lab['wireguard_port']) for rank in range(2)]
                    write(out/'captures.json', captures)
                    if not all(x['passed'] for x in captures):
                        result['passed'] = False
                        result['capture_error'] = 'capture contains unexpected underlay traffic'
                except Exception as exc:
                    result['passed'] = False
                    result['capture_error'] = repr(exc)
            write(out / 'result.json', result)
        print(json.dumps(result), flush=True)
        return result


def compare(pp1, pp2, tolerance=0.15):
    first, second = read(Path(pp1)/'result.json'), read(Path(pp2)/'result.json')
    if (first['backend'], first['model'], first['pp']) != (second['backend'], second['model'], 1) or second['pp'] != 2:
        raise ValueError('comparison requires PP=1/PP=2 of the same backend and model')
    if read(Path(pp1)/'profiles.json') != read(Path(pp2)/'profiles.json'):
        raise ValueError('comparison requires identical pinned profiles')
    a, b = read(Path(pp1)/'api.json'), read(Path(pp2)/'api.json')
    diffs = []
    for x, y in zip(a['fixtures'], b['fixtures'], strict=True):
        cx, cy = x['response']['choices'][0], y['response']['choices'][0]
        if x['prompt'] != y['prompt'] or cx['message']['content'] != cy['message']['content']:
            raise AssertionError('greedy fixture differs between PP=1 and PP=2')
        if cx['finish_reason'] != cy['finish_reason'] or x['response']['usage'] != y['response']['usage']:
            raise AssertionError('finish/usage differs between PP=1 and PP=2')
        for lx, ly in zip(cx['logprobs']['content'], cy['logprobs']['content'], strict=True):
            assert lx['token'] == ly['token']
            diffs.append(abs(lx['logprob']-ly['logprob']))
    assert diffs and max(diffs) <= tolerance, diffs
    return {'passed': True, 'max_logprob_absolute_difference': max(diffs), 'tolerance': tolerance}


def main():
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'upload', 'prepare', 'run', 'compare', 'matrix'])
    parser.add_argument('--lab', type=Path, default=HERE/'lab.json')
    parser.add_argument('--backend', choices=['sglang', 'vllm'], default='vllm')
    parser.add_argument('--model', choices=['small', 'large'], default='small')
    parser.add_argument('--pp', choices=[1, 2], type=int, default=2)
    parser.add_argument('--output', type=Path, default=HERE/'results')
    parser.add_argument('--network', action='store_true')
    parser.add_argument('--stop-existing', action='store_true', help='explicitly stop the two named existing GPU containers')
    parser.add_argument('--pp1-dir', type=Path)
    parser.add_argument('--pp2-dir', type=Path)
    args = parser.parse_args()
    profiles, lab = read(HERE/'profiles.json'), read(args.lab)
    pilot = Pilot(lab, profiles)
    if args.action == 'plan':
        print(json.dumps({'image': profiles['backends'][args.backend]['image'],
              'ranks': [{'env': environment(lab['hosts'][r]), 'argv': backend_argv(profiles, lab, args.backend, args.model, args.pp, r)}
                        for r in range(args.pp)]}, indent=2))
    elif args.action == 'upload':
        pilot.upload()
    elif args.action == 'prepare':
        pilot.prepare(args.stop_existing)
    elif args.action == 'compare':
        print(json.dumps(compare(args.pp1_dir, args.pp2_dir), indent=2))
    elif args.action == 'matrix':
        pilot.upload()
        results = {}
        for backend in profiles['backends']:
            one = pilot.trial(backend, 'small', 1, args.output)
            two = pilot.trial(backend, 'small', 2, args.output, network=True)
            try:
                comparison = compare(args.output/one['run'], args.output/two['run'])
            except Exception as exc:
                comparison = {'passed': False, 'error': repr(exc)}
            large = pilot.trial(backend, 'large', 2, args.output)
            results[backend] = {'small_pp1': one, 'small_pp2': two, 'comparison': comparison, 'large_pp2': large}
            write(args.output/'matrix.json', results)
        sys.exit(0 if all(case['passed'] for backend in results.values() for case in backend.values()) else 1)
    else:
        result = pilot.trial(args.backend, args.model, args.pp, args.output, args.network)
        sys.exit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()
