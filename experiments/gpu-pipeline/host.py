#!/usr/bin/env python3
"""Root helper for the disposable, dev-only two-host GPU experiment.

Called over SSH with one JSON request on stdin. Private WireGuard keys stay here.
This is a lab helper, not an agent, attestation mechanism or production launcher.
"""
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys


def command(args, **kwargs):
    return subprocess.check_output([str(x) for x in args], text=True, **kwargs).strip()


def main(req):
    run = req['run']
    if not re.fullmatch(r'[a-z0-9-]{1,45}', run):
        raise ValueError('invalid run name')
    name = 'cocoon-pilot-' + run
    root = Path(__file__).resolve().parent
    artifacts = root / 'runs' / run
    # Ubuntu's enforced wg AppArmor profile permits keys below /etc/wireguard.
    # Keep dev keys in their own root-only subtree; cleanup removes each run key.
    secrets = Path('/etc/wireguard/cocoon-gpu-pilot') / run
    iface = 'cp' + __import__('hashlib').sha256(run.encode()).hexdigest()[:10]
    action = req['action']

    def inspect():
        info = json.loads(command(['docker', 'inspect', name]))[0]
        if info['Config']['Labels'].get('cocoon.gpu-pilot') != run:
            raise ValueError('refusing to operate on an unowned container')
        return info

    def ns(args):
        pid = inspect()['State']['Pid']
        if not pid:
            raise RuntimeError('container has stopped')
        return ['nsenter', '-t', str(pid), '-n', *args]

    if action == 'create':
        if not re.fullmatch(r'.+@sha256:[0-9a-f]{64}', req['image']):
            raise ValueError('image must be pinned by digest')
        artifacts.mkdir(parents=True, exist_ok=False)
        secrets.mkdir(parents=True, mode=0o700, exist_ok=False)
        key = secrets / 'private.key'
        key.write_text(command(['wg', 'genkey']) + '\n')
        key.chmod(0o600)
        public = command(['wg', 'pubkey'], input=key.read_text())
        argv = ['docker', 'run', '-d', '--name', name, '--label', 'cocoon.gpu-pilot=' + run,
                '--network', 'none', '--gpus', 'all', '--shm-size', '4g',
                '--mount', f'type=bind,src={root},dst=/pilot,readonly',
                '--mount', f'type=bind,src={root / "models"},dst=/models,readonly',
                '--mount', f'type=bind,src={artifacts},dst=/artifacts',
                '--entrypoint', 'sleep']
        for k, v in req['env'].items():
            argv += ['-e', f'{k}={v}']
        argv += [req['image'], 'infinity']
        command(argv)
        pid = inspect()['State']['Pid']
        command(['ip', 'link', 'add', iface, 'type', 'wireguard'])
        command(['ip', 'link', 'set', iface, 'netns', pid])
        command(ns(['ip', 'link', 'set', iface, 'name', 'wg0']))
        command(ns(['wg', 'set', 'wg0', 'private-key', str(key), 'listen-port', req['port']]))
        command(ns(['ip', 'addr', 'add', req['overlay'] + '/24', 'dev', 'wg0']))
        command(ns(['ip', 'link', 'set', 'wg0', 'mtu', req['mtu'], 'up']))
        return {'public_key': public, 'container': name, 'pid': pid}
    if action == 'peer':
        command(ns(['wg', 'set', 'wg0', 'peer', req['public_key'], 'allowed-ips',
                    req['overlay'] + '/32', 'endpoint', f"{req['lan']}:{req['port']}",
                    'persistent-keepalive', '5']))
        return {'configured': True}
    if action == 'exec':
        inspect()
        return {'output': command(['docker', 'exec', name, *req['argv']], timeout=req.get('timeout', 120))}
    if action == 'launch':
        inspect()
        log = req['log']
        if not re.fullmatch(r'[a-z0-9.-]+', log):
            raise ValueError('invalid log name')
        (artifacts / (log + '.argv.json')).write_text(json.dumps(req['argv'], indent=2) + '\n')
        shell = (shlex.join(req['argv']) + ' > /artifacts/' + log + ' 2>&1; '
                 'rc=$?; printf "%s\\n" "$rc" > /artifacts/' + log + '.exit; exit "$rc"')
        command(['docker', 'exec', '-d', name, 'sh', '-c', shell])
        return {'launched': req['argv']}
    if action == 'net':
        args = ns(req['argv']) if req.get('overlay', True) else req['argv']
        if req.get('background'):
            # timeout bounds the host-side process even if SSH/orchestrator dies.
            with (artifacts / req['log']).open('w') as log:
                process = subprocess.Popen(['timeout', str(req.get('timeout', 20)), *args],
                                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                # Record birth time as well as PID, so cleanup cannot kill a reused PID.
                start = Path(f'/proc/{process.pid}/stat').read_text().split()[21]
                with (artifacts / 'host-processes.jsonl').open('a') as record:
                    record.write(json.dumps({'pid': process.pid, 'start': start}) + '\n')
            return {'started': True}
        return {'output': command(args, timeout=req.get('timeout', 30))}
    if action == 'health':
        exit_file = artifacts / 'server.log.exit'
        return {'exit_code': int(exit_file.read_text()) if exit_file.exists() else None}
    if action == 'kill-stage':
        inspect()
        # Keep PID 1 (sleep) and the namespace alive until cleanup, but kill all
        # backend processes. /proc here is the pilot container's PID namespace.
        script = ('import os, signal\n'
                  'for name in os.listdir("/proc"):\n'
                  ' if name.isdigit() and int(name) not in (1, os.getpid()):\n'
                  '  try: os.kill(int(name), signal.SIGKILL)\n'
                  '  except ProcessLookupError: pass\n')
        command(['docker', 'exec', name, 'python3', '-c', script])
        return {'stage_killed': True}
    if action == 'status':
        return {'container': inspect(),
                'kernel': command(['uname', '-a']),
                'cpu': command(['lscpu', '-J']),
                'interfaces': command(ns(['ip', '-j', 'addr'])),
                'routes': command(ns(['ip', '-j', 'route'])),
                'wireguard': command(ns(['wg', 'show', 'wg0'])),
                'gpu': command(['nvidia-smi', '--query-gpu=name,uuid,driver_version,memory.total,memory.used,utilization.gpu', '--format=csv']),
                'processes': command(['nvidia-smi', '--query-compute-apps=pid,process_name,used_memory', '--format=csv'])}
    if action == 'stop':
        # Only the exact labelled pilot container can be removed; models/logs survive.
        records = artifacts / 'host-processes.jsonl'
        if records.exists():
            for line in records.read_text().splitlines():
                saved = json.loads(line)
                stat = Path(f'/proc/{saved["pid"]}/stat')
                try:
                    if stat.read_text().split()[21] == saved['start']:
                        os.killpg(saved['pid'], signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except FileNotFoundError:
                    pass
        found = command(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$'])
        if found:
            info = inspect()
            if info['State']['Pid']:
                # Release the host UDP socket before deleting Docker's namespace;
                # otherwise kernel namespace teardown can retain it temporarily.
                subprocess.run(ns(['ip', 'link', 'delete', 'wg0']), capture_output=True, check=False)
            command(['docker', 'rm', '-f', name])
        if subprocess.run(['ip', 'link', 'show', iface], capture_output=True).returncode == 0:
            command(['ip', 'link', 'delete', iface])
        if secrets.exists():
            (secrets / 'private.key').unlink(missing_ok=True)
            secrets.rmdir()
        return {'stopped': name,
                'gpu': command(['nvidia-smi', '--query-compute-apps=pid,process_name,used_memory', '--format=csv'])}
    raise ValueError('unknown action ' + action)


if __name__ == '__main__':
    print(json.dumps(main(json.load(sys.stdin))))
