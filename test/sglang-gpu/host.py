#!/usr/bin/env python3
"""Scoped root operations for step-10 dev acceptance. JSON stdin, no shell."""
import json
import hashlib
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

BASE = Path('/home/ruslixag/cocoon-step10')
MODEL_ROOT = Path('/home/ruslixag/cocoon-pipeline-dev/models')
IMAGE = 'cocoon-step10-dev'


def command(args, **kw):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=kw.pop('timeout', 30), **kw)


def firewall():
    value = json.loads(command(['nft', '-j', 'list', 'ruleset']))
    def stable(item):
        if isinstance(item, list): return [stable(x) for x in item]
        if isinstance(item, dict):
            return {k: stable(v) for k,v in item.items() if k not in ('packets', 'bytes')}
        return item
    return stable(value)


def main(req):
    run = req['run']
    if not re.fullmatch('[0-9a-f]{12}', run): raise ValueError('invalid run')
    root = BASE / 'runs' / run
    name = 'cp10-' + run
    action = req['action']
    owner = root / 'owner.json'
    if action == 'create':
        if root.exists(): raise ValueError('run already exists')
        rank = req['rank']
        if rank not in (0, 1): raise ValueError('rank')
        if command(['nvidia-smi', '--query-compute-apps=pid', '--format=csv,noheader,nounits']).strip():
            raise RuntimeError('GPU has an existing compute workload')
        addr, peer = ('192.168.100.12', '192.168.100.13')[rank], ('192.168.100.13', '192.168.100.12')[rank]
        root.mkdir(parents=True, mode=0o755)
        baseline = command(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits']).strip()
        fw = firewall()
        state = {'name': name, 'rank': rank, 'baseline_gpu_mib': baseline, 'root_firewall': fw}
        owner.write_text(json.dumps(state)); owner.chmod(0o600)
        # Duplicate-address detection precedes claiming a temporary LAN address.
        command(['docker', 'run', '--rm', '--network', 'host', '--entrypoint', 'arping', IMAGE,
                 '-D', '-I', 'enp4s0.4000', '-c', '3', addr])
        command(['docker', 'run', '-d', '--name', name, '--label', 'cocoon.step10=' + run,
                 '--gpus', 'device=0', '--network', 'none', '--pid', 'host',
                 '--cap-drop', 'ALL', '--cap-add', 'SYS_ADMIN', '--cap-add', 'SYS_PTRACE',
                 '--cap-add', 'NET_ADMIN', '--cap-add', 'NET_RAW',
                 '--cap-add', 'SETPCAP', '--cap-add', 'SETUID', '--cap-add', 'SETGID', '--cap-add', 'CHOWN',
                 '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'KILL', '--security-opt', 'apparmor=unconfined',
                 '--security-opt', 'seccomp=unconfined', '--shm-size', '2g', '--pids-limit', '1024',
                 '--ulimit', 'memlock=-1', '--mount', 'type=bind,src=' + str(BASE/'src') + ',dst=/work/cocoon,readonly',
                 '--mount', 'type=bind,src=' + str(BASE/'build') + ',dst=/work/build,readonly',
                 '--mount', 'type=bind,src=' + str(MODEL_ROOT) + ',dst=/models,readonly',
                 '--mount', 'type=bind,src=' + str(root) + ',dst=/trial', IMAGE])
        info = json.loads(command(['docker', 'inspect', name]))[0]
        pid = info['State']['Pid']
        link = 'p10' + run[:10]
        state.update(container_pid=pid, image=info['Image'], link=link, underlay=addr)
        owner.write_text(json.dumps(state))
        command(['ip', 'link', 'add', 'link', 'enp4s0.4000', 'name', link, 'type', 'ipvlan', 'mode', 'l2'])
        command(['ip', 'link', 'set', link, 'netns', str(pid)])
        command(['nsenter', '-t', str(pid), '-n', 'ip', 'link', 'set', link, 'name', 'eth0'])
        command(['nsenter', '-t', str(pid), '-n', 'ip', 'addr', 'add', addr + '/24', 'dev', 'eth0'])
        command(['nsenter', '-t', str(pid), '-n', 'ip', 'link', 'set', 'eth0', 'up'])
        command(['nsenter', '-t', str(pid), '-n', 'ip', 'link', 'set', 'lo', 'up'])
        kind = req['model']
        if kind not in ('small', 'large'): raise ValueError('model')
        model = '0.6b' if kind == 'small' else '14b'
        cfg = {'profile': f'sglang-qwen3-{model}-dev-pp2-wg-v1', 'rank': rank, 'role': 'head' if rank == 0 else 'member',
               'group': {'peer_port' if rank == 0 else 'listen_port': 12310}, 'network': {'underlay_ip': addr, 'peer_ip': peer}}
        if rank == 0: cfg['gate'] = {'listen_port': 18080}
        (root/'config.json').write_text(json.dumps(cfg))
        return {k:v for k,v in state.items() if k != 'root_firewall'}
    state = json.loads(owner.read_text())
    if state['name'] != name: raise ValueError('owner mismatch')
    if action == 'start':
        code = 'import subprocess; f=open("/trial/agent.log","w"); p=subprocess.Popen(["/work/build/pipeline/pipeline-agent-dev","--config","/trial/config.json","--run-dir","/trial/agent"],stdout=f,stderr=subprocess.STDOUT,start_new_session=True); print(p.pid)'
        pid = int(command(['docker','exec',name,'python3','-c',code]).strip())
        state['agent_pid'] = pid; owner.write_text(json.dumps(state))
        return {'agent_pid': pid}
    if action == 'status':
        p = root/'agent/status.json'
        return json.loads(p.read_text()) if p.exists() else {}
    if action == 'evidence':
        current = json.loads((root/'agent/status.json').read_text())
        ns = current['group']['network']['namespace']
        wrapper = current['process']['pid']
        code = '''import importlib.metadata,json,torch
print(json.dumps({'sglang':importlib.metadata.version('sglang'),'torch':torch.__version__,
                 'cuda':torch.version.cuda,'nccl':torch.cuda.nccl.version()}))'''
        versions = json.loads(command(['docker','exec',name,'python3','-c',code]))
        links = json.loads(command(['docker','exec',name,'ip','-j','-n',ns,'link']))
        if {link['ifname'] for link in links} != {'lo','wg0'}: raise AssertionError('unexpected engine interface')
        process = Path(f'/proc/{wrapper}/status').read_text()
        fields = dict(line.split(':',1) for line in process.splitlines() if ':' in line)
        if set(fields['Uid'].split()) != {'65534'} or int(fields['CapEff'].strip(),16):
            raise AssertionError('backend privilege drop failed')
        fault = root/'fault.json'
        recovered = None
        if fault.exists():
            old = json.loads(fault.read_text())
            try: os.killpg(old['status']['process']['pgid'],0)
            except ProcessLookupError: pass
            else: raise AssertionError('old member process group survived')
            old_socket = root / Path(old['status']['health_socket']).relative_to('/trial')
            if old_socket.exists(): raise AssertionError('old member socket survived')
            if current['epoch'] == old['status']['epoch']: raise AssertionError('member epoch unchanged')
            recovered = {'old_epoch':old['status']['epoch'],'new_epoch':current['epoch'],'old_resources_removed':True}
        sources = [p for p in (BASE/'src/pipeline').iterdir() if p.is_file() and p.suffix in ('.py','.cpp','.h','.json','.in','.txt')]
        sources += [BASE/'src/test/sglang-gpu/inside.py',BASE/'src/test/sglang-gpu/host.py',BASE/'src/test/sglang-gpu/Dockerfile']
        return {'status':current,'versions':versions,'engine_links':links,'backend_status':process,
                'wireguard':command(['docker','exec',name,'ip','netns','exec',ns,'wg','show']),
                'gpu':command(['nvidia-smi','--query-gpu=name,driver_version,memory.used,memory.total','--format=csv']),
                'source_sha256':{str(p.relative_to(BASE/'src')):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                'binary_sha256':{p:hashlib.sha256((BASE/'build/pipeline'/p).read_bytes()).hexdigest()
                                 for p in ('pipeline-agent-dev','pipeline-backend-sandbox')},
                'member_recovery':recovered}
    if action == 'driver':
        model = 'Qwen/Qwen3-0.6B' if req['model'] == 'small' else 'Qwen/Qwen3-14B'
        code = 'import subprocess; f=open("/trial/driver.log","w"); p=subprocess.Popen(["python3","/work/cocoon/test/sglang-gpu/inside.py","--head-run","/trial/agent","--output-dir","/trial/acceptance","--model",' + repr(model) + '],stdout=f,stderr=subprocess.STDOUT,start_new_session=True); print(p.pid)'
        pid = int(command(['docker','exec',name,'python3','-c',code]).strip())
        state['driver_pid'] = pid; owner.write_text(json.dumps(state))
        return {'driver_pid': pid}
    if action == 'progress':
        result = {}
        for key, rel in [('fault','acceptance/fault-request.json'),('result','acceptance/result.json'),
                         ('resource','acceptance/member-resource-request.json')]:
            p = root/rel
            if p.exists(): result[key] = json.loads(p.read_text())
        log = root/'driver.log'
        result['tail'] = log.read_text()[-5000:] if log.exists() else ''
        pid = state.get('driver_pid', 0)
        try: result['driver_alive'] = b'test/sglang-gpu/inside.py' in Path(f'/proc/{pid}/cmdline').read_bytes()
        except FileNotFoundError: result['driver_alive'] = False
        return result
    if action == 'resources':
        phase = req['phase']
        if phase not in ('active', 'idle'): raise ValueError('resource phase')
        current = json.loads((root/'agent/status.json').read_text())
        ns = current['group']['network']['namespace']
        code = '''import json,urllib.request
m={}
for line in urllib.request.urlopen("http://127.0.0.1:30000/metrics",timeout=3).read().decode().splitlines():
 if line.startswith(("sglang:num_running_reqs{","sglang:num_queue_reqs{","sglang:num_used_tokens{","sglang:token_usage{")):
  m.setdefault(line.split("{")[0],[]).append(float(line.rsplit(" ",1)[1]))
print(json.dumps(m))'''
        deadline = time.monotonic()+(40 if phase == 'idle' else 10)
        names = ('sglang:num_running_reqs','sglang:num_queue_reqs','sglang:num_used_tokens','sglang:token_usage')
        values = {}
        while time.monotonic() < deadline:
            values = json.loads(command(['docker','exec',name,'ip','netns','exec',ns,'python3','-c',code]))
            if all(k in values for k in names):
                if phase == 'idle' and all(v == 0 for k in names for v in values[k]): break
                if phase == 'active' and any(v > 0 for v in values['sglang:num_used_tokens']): break
            time.sleep(.2)
        else: return {'passed':False,'metrics':values,'phase':phase}
        return {'passed':True,'metrics':values,'phase':phase}
    if action == 'resource-response':
        tmp = root/'acceptance/member-resource-response.tmp'
        tmp.write_text(json.dumps(req['response']))
        tmp.replace(root/'acceptance/member-resource-response.json')
        return {'written':True}
    if action == 'kill-member':
        if state['rank'] != 1: raise ValueError('member only')
        current = json.loads((root/'agent/status.json').read_text())
        if not current['group_ready'] or current['epoch'] != req['epoch']: raise ValueError('stale fault request')
        wrapper = current['process']['pid']
        # Kill SGLang's actual launcher, preserving our wrapper so it must notice
        # the exit and reap grandchildren. PID ownership is checked in /proc.
        children = Path(f'/proc/{wrapper}/task/{wrapper}/children').read_text().split()
        targets = [int(p) for p in children if b'sglang.launch_server' in Path(f'/proc/{p}/cmdline').read_bytes()]
        if len(targets) != 1: raise ValueError('cannot identify owned SGLang process')
        old = {'status': current, 'wrapper_pid': wrapper, 'sglang_pid': targets[0]}
        (root/'fault.json').write_text(json.dumps(old))
        fd = os.pidfd_open(targets[0])
        try:
            parent = int(Path(f'/proc/{targets[0]}/stat').read_text().rsplit(')', 1)[1].split()[1])
            if parent != wrapper: raise ValueError('SGLang process ownership changed')
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        finally:
            os.close(fd)
        return old
    if action == 'stop':
        result = {'passed': False}
        try:
            info = json.loads(command(['docker','inspect',name]))[0]
            if info['Config']['Labels'].get('cocoon.step10') != run: raise ValueError('container label mismatch')
            code = 'import socket; s=socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect("/trial/agent/control.sock"); s.sendall(b"{\\\"op\\\":\\\"stop\\\"}\\n"); print(s.recv(32768).decode())'
            try: command(['docker','exec',name,'python3','-c',code])
            except subprocess.CalledProcessError: pass
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                p = root/'agent/status.json'
                current = json.loads(p.read_text()) if p.exists() else {}
                if current.get('state') in ('STOPPED', 'FAILED'): break
                time.sleep(.2)
            result['agent'] = current
            result['network_namespaces'] = command(['docker','exec',name,'ip','netns','list'])
            result['network_owners'] = command(['docker','exec',name,'sh','-c','if [ -d /run/cocoon-pipeline-net ]; then ls -A /run/cocoon-pipeline-net; fi'])
            result['agent_cleanup'] = current.get('process',{}).get('cleanup_complete',False) and not result['network_namespaces'].strip() and not result['network_owners'].strip()
            command(['docker','rm','-f',name])
        except subprocess.CalledProcessError:
            result['agent_cleanup'] = False
        # A failed create may have left only the un-moved test link.
        link = state.get('link', 'p10' + run[:10])
        probe = subprocess.run(['ip','link','show',link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode == 0: command(['ip','link','del',link])
        for attempt in range(20):
            result['gpu_mib'] = command(['nvidia-smi','--query-gpu=memory.used','--format=csv,noheader,nounits']).strip()
            result['remaining_gpu_processes'] = command(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader,nounits']).strip()
            result['gpu_memory_released'] = int(result['gpu_mib']) <= int(state['baseline_gpu_mib']) + 8
            if not result['remaining_gpu_processes'] and result['gpu_memory_released']: break
            time.sleep(.5)
        result['root_firewall_unchanged'] = firewall() == state['root_firewall']
        result['passed'] = bool(result.get('agent_cleanup') and not result['remaining_gpu_processes'] and
                                result['gpu_memory_released'] and result['root_firewall_unchanged'])
        (root/'cleanup.json').write_text(json.dumps(result,indent=2))
        return result
    raise ValueError('unknown action')


if __name__ == '__main__':
    if os.geteuid() != 0: raise SystemExit('root required')
    print(json.dumps(main(json.load(sys.stdin))))
