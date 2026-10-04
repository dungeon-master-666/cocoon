#!/usr/bin/env python3
"""Unprivileged Cocoon dev services; fixed public fake-TON fixtures only."""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import urllib.request

ROOT = Path('/opt/cocoon')
READINESS_TTL = 3.0
KEYS = {
    'proxy': ('ProxyProxyProxyProxyProxyProxyProxyProxyPro=', 'ml+7xELuOQtfp9q9mSWtHiSWx5aSBV6p3xCfwPmHOwY='),
    'worker': ('WorkerWorkerWorkerWorkerWorkerWorkerWorkerU=', 'jYYD/HjU0K2D/xpistoFM4BruJhY3koI1xuO00xd7PI='),
    'client': ('ClientClientClientClientClientClientClientC=', 'gaEatwGNgLGOeFI/TRGfPdJG/K4G8picYHQkDHpLQ9w='),
    'key-manager': ('KeyManagerKeyManagerKeyManagerKeyManagerKe0=', '4DV6iMUS16P6Y/6MlF6qfnxii7q2UWXUwSi4bgvcaf8='),
}


def render(spec, state, external_only=False):
    """Use the existing dev templates with one authoritative model/capacity."""
    roles = list(KEYS) if spec['mode'] == 'local-fake-ton' else ['worker']
    if external_only:roles.remove('worker')
    vars = dict(line.split('=',1) for line in (ROOT/'spec/fake-ton/runtime.vars').read_text().splitlines() if '=' in line)
    vars.update(IS_DEBUG='1', MODEL_NAME=spec['model'], MODEL_COMMIT='dev', MODEL_VERITY_HASH='0'*64,
                TON_CONFIG_FILE=str(state/'global.config.json'), DISK_PATH=str(state),
                OWNER_ADDRESS='UQAuz15H1ZHrZ_psVrAra7HealMIVeFq0wguqlmFno1f3B-m',
                ROOT_CONTRACT_ADDRESS='EQBT4hy4vMEZ9uxSCuhw_gBKh9_AwmHXLe7Wo0O4Vh-4kRjJ',
                EXTERNAL_IP=spec.get('advertise_ip','127.0.0.1'), EXTERNAL_WORKER_PORT='11001', EXTERNAL_CLIENT_PORT='11002',
                EXTERNAL_KEY_MANAGER_PORT='13001', CLIENT_HTTP_PORT='10000', CLIENT_RPC_PORT='10001',
                WORKER_COEFFICIENT=str(spec['coefficient']))

    def subst(value):
        if isinstance(value, str):
            return re.sub(r'\$([A-Z_]+)', lambda m: vars[m[1]], value)
        if isinstance(value, list): return [subst(v) for v in value]
        if isinstance(value, dict): return {k:subst(v) for k,v in value.items()}
        return value

    def write(name, value):
        path = state/name
        path.write_text(json.dumps(value,indent=2)+'\n');path.chmod(0o600)

    fake = subst(json.loads((ROOT/'spec/fake-ton/fake-ton-config.json').read_text()))
    if spec['mode'] == 'external-fake-ton':
        fake['registered_proxies'][0]['address'] = f"{spec['proxy_ip']}:{spec['proxy_worker_port']} {spec['proxy_ip']}:{spec['proxy_client_port']}"
        fake['key_manager_address'] = f"{spec['key_manager_ip']}:{spec['key_manager_port']}"
    write('fake-ton-config.json',fake)
    for role in roles:
        vars['TEE_IMAGE_HASH'], vars['APP_KEY'] = KEYS[role]
        vars['NODE_WALLET_KEY'] = vars['APP_KEY']
        conf = subst(json.loads((ROOT/f'spec/spec-{role}/{role}-config.json').read_text()))
        conf['is_test'] = True
        if role == 'worker':
            conf.update(model_name=spec['model_identifier'], max_active_requests=spec['max_active_requests'],
                        forward_requests_to=spec['forward_requests_to'],coefficient=spec['coefficient'])
        if role == 'client': conf.update(max_coefficient=1000000000,http_port=10000,rpc_port=10001)
        if role == 'key-manager': conf['add_default_key'] = True
        write(role+'-config.json',conf)
    return roles


def read_json(port, path):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=1) as response:
        return json.load(response)


def available(spec, external_only=False):
    try:
        if not external_only:
            worker = read_json(12000, '/jsonstats')
            status = worker['status']
            connections = status.get('ready_proxy_connections')
            if (status.get('enabled') is not True or status.get('uplink_ok') is not True or
                    type(connections) is not int or connections <= 0 or
                    worker['localconfig']['model'] != spec['model_identifier']):
                return False
        if spec['mode'] == 'local-fake-ton':
            value = read_json(10000, '/v1/models')
            return any(m['id'].split('@')[0] == spec['model'] and
                       isinstance(m.get('workers'), list) and bool(m['workers']) for m in value['data'])
        return spec['mode'] == 'external-fake-ton'
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError):
        return False


def refresh_ready(spec, state, external_only=False):
    # Timestamp the start of the probe, so a result started before a new epoch
    # cannot certify that epoch merely because its HTTP response arrived later.
    checked_at = time.monotonic()
    ready = state/'ready.json'
    if available(spec, external_only):
        tmp = ready.with_suffix('.tmp')
        tmp.write_text(json.dumps({'model': spec['model'], 'capacity': spec['max_active_requests'],
                                   'checked_at': checked_at}))
        tmp.replace(ready)
    else:
        ready.unlink(missing_ok=True)


def is_ready(state, checked_after=0.0):
    try:
        checked_at = json.loads((state/'ready.json').read_text())['checked_at']
        return (type(checked_at) in (int, float) and checked_at >= checked_after and
                0 <= time.monotonic() - checked_at <= READINESS_TTL)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def run(spec, state, external_only=False):
    if os.geteuid() != 10001:
        raise RuntimeError('Cocoon services must run as UID 10001')
    state.mkdir(mode=0o700,exist_ok=True)
    os.umask(0o077)
    roles = render(spec,state,external_only)
    ready = state/'ready.json'
    ready.unlink(missing_ok=True)
    stop = False
    def signal_stop(*unused):
        nonlocal stop
        stop = True
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,signal_stop)
    procs=[]
    def start(name,args):
        with (state/(name+'.log')).open('ab') as log:
            p=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,cwd=state,start_new_session=True)
        procs.append((name,p))
    try:
        router=[str(ROOT/'bin/router'),'-S','8116@any']
        if spec['mode']=='local-fake-ton':
            router += ['-R','11001:127.0.0.1:11101@any','-R','11002:127.0.0.1:11102@any',
                       '-R','13001:127.0.0.1:13101@any']
        start('router',router+['--serialize-info'])
        for role in roles:
            argv=[str(ROOT/'bin'/f'{role}-runner'),'--config',str(state/f'{role}-config.json'),'-v3',
                  '--disable-ton',str(state/'fake-ton-config.json')]
            if role=='key-manager':argv+=['--connect-to-proxy-via','127.0.0.1:8116']
            start(role,argv)
        (state/'processes.json').write_text(json.dumps({n:p.pid for n,p in procs}))
        while not stop:
            for name,p in procs:
                if p.poll() is not None:raise RuntimeError(name+' exited; see service log')
            refresh_ready(spec, state, external_only)
            time.sleep(.1)
    finally:
        ready.unlink(missing_ok=True)
        for _,p in reversed(procs):
            try:os.killpg(p.pid,signal.SIGTERM)
            except ProcessLookupError:pass
        end=time.monotonic()+5
        while any(p.poll() is None for _,p in procs) and time.monotonic()<end:time.sleep(.1)
        for _,p in procs:
            try:os.killpg(p.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            p.wait(timeout=3)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--external-only',action='store_true',help='run dev proxy/client/KM fixture without worker')
    args=parser.parse_args()
    run(json.loads(args.config.read_text()),args.state,args.external_only)
