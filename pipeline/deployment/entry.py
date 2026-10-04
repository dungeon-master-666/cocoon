#!/usr/bin/env python3
"""Container supervisor. Runtime/cgroup teardown outlives an individual agent."""
import argparse
import contextlib
import ctypes
import json
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path('/opt/cocoon')
STATE=Path('/state')


def command(args, **kw):
    if args[:2]==['ip','-n']:
        args=['nsenter','--net=/run/netns/'+args[2],'ip',*args[3:]]
    elif args[:3]==['ip','netns','exec']:
        args=['nsenter','--net=/run/netns/'+args[3],*args[4:]]
    try:
        return subprocess.check_output(args,text=True,stderr=subprocess.STDOUT,timeout=kw.pop('timeout',30),**kw)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(' '.join(args[:4])+': '+exc.output.strip()) from None


def save(path,value):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,indent=2)+'\n');tmp.replace(path)


def load_services():
    module=importlib.util.spec_from_file_location('services',Path(__file__).with_name('services.py'))
    services=importlib.util.module_from_spec(module);module.loader.exec_module(services)
    return services


def unprivileged():
    # Service processes have no network-management privileges and cannot read
    # agent status/control or backend UDS owned by root/65534.
    os.setgroups([]);os.setgid(10001);os.setuid(10001)
    if ctypes.CDLL(None).prctl(38,1,0,0,0):raise RuntimeError('no_new_privs failed')


def firewall(agent):
    # Survives the per-epoch guardian: service egress must stay restricted while
    # a crashed guardian is being replaced, and before the first roster exists.
    network=agent['network'];own=network['underlay_ip'];peer=network['peer_ip']
    table='deployment_guard'
    rules=f'add table inet {table}\n'
    for chain in ('input','output','forward'):
        rules+=f'add chain inet {table} {chain} {{ type filter hook {chain} priority -20; policy drop; }}\n'
    rules+=f'add rule inet {table} input iifname "lo" accept\n'
    rules+=f'add rule inet {table} output oifname "lo" accept\n'
    for chain,src,dst in (('input',peer,own),('output',own,peer)):
        rules+=f'add rule inet {table} {chain} ip saddr {src} ip daddr {dst} udp sport 51820 udp dport 51820 accept\n'
        rules+=f'add rule inet {table} {chain} ip saddr {src} ip daddr {dst} tcp dport 12310 accept\n'
        rules+=f'add rule inet {table} {chain} ip saddr {src} ip daddr {dst} tcp sport 12310 ct state established accept\n'
    for endpoint in network.get('service_egress',[]):
        ip,port=endpoint['ip'],endpoint['port']
        rules+=f'add rule inet {table} output meta skuid 10001 ip daddr {ip} tcp dport {port} accept\n'
        rules+=f'add rule inet {table} input ip saddr {ip} tcp sport {port} ct state established accept\n'
    command(['ip','netns','exec','underlay','nft','-f','-'],input=rules)


def run(config, agent_config, services):
    node=config['node']
    end=time.monotonic()+config['startup_timeout']
    while not (STATE/'network-ready').exists():
        if time.monotonic()>end:raise RuntimeError('host network setup deadline')
        time.sleep(.05)
    # Keep PID 1 in an otherwise empty management namespace. The agent uses a
    # nested underlay; network-helper can still reject accidental init-netns use.
    command(['ip','netns','add','underlay'])
    command(['ip','link','set','eth0','netns','underlay'])
    command(['ip','-n','underlay','link','set','lo','up'])
    command(['ip','-n','underlay','addr','add',node['underlay'],'dev','eth0'])
    command(['ip','-n','underlay','link','set','eth0','up'])
    if node.get('gateway'):
        command(['ip','-n','underlay','route','add','default','via',node['gateway'],'dev','eth0'])
    firewall(json.loads(agent_config.read_text()))
    service_dir=STATE/'services'
    if services:
        service_dir.mkdir(mode=0o700,exist_ok=True);os.chown(service_dir,10001,10001)
        (service_dir/'ready.json').unlink(missing_ok=True)
    # Runtime is ephemeral tmpfs; only diagnostics and service databases persist.
    runtime=Path('/run/pipeline');runtime.mkdir(mode=0o711)
    stop=False
    def stopping(*unused):
        nonlocal stop
        stop=True
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,stopping)
    children=[]
    with (STATE/'agent.log').open('ab') as log:
        agent=subprocess.Popen(['nsenter','--net=/run/netns/underlay',str(ROOT/'bin/pipeline-agent-dev'),
            '--config',str(agent_config),'--run-dir',str(runtime/'agent')],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
    children.append(agent)
    service_runtime=load_services()
    ever_ready=False;service=None;group_state=None;checked_after=time.monotonic()
    save(STATE/'supervisor.json',{'state':'STARTING','agent_pid':agent.pid,'pid':os.getpid()})
    try:
        while not stop:
            if agent.poll() is not None:raise RuntimeError('pipeline agent exited')
            status_path=runtime/'agent/status.json'
            status=json.loads(status_path.read_text()) if status_path.exists() else {}
            save(STATE/'agent-status.json',status)
            if status.get('state')=='FAILED':raise RuntimeError('pipeline agent failed: '+str(status.get('failure')))
            current_group=(bool(status.get('group_ready')),status.get('epoch'))
            if current_group!=group_state:
                group_state=current_group;checked_after=time.monotonic()
            if status.get('group_ready'):
                if services and service is None:
                    # setns happens with root capabilities; the service launcher
                    # itself drops UID/GID/capabilities before executing services.
                    with (STATE/'services.log').open('ab') as log:
                        service=subprocess.Popen(['nsenter','--net=/run/netns/underlay',sys.executable,'-I',__file__,
                            '--services',str(services)],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
                    children.append(service)
            if service is not None and service.poll() is not None:raise RuntimeError('Cocoon services exited')
            ready=status.get('group_ready') and (not services or service_runtime.is_ready(service_dir,checked_after))
            if ready:ever_ready=True
            if not ever_ready and time.monotonic()>end:
                raise RuntimeError('group/services startup deadline (peer unavailable, warmup or registration failed)')
            save(STATE/'supervisor.json',{'state':'READY' if ready else 'STARTING',
                'agent_pid':agent.pid,'service_pid':service.pid if service else None,'pid':os.getpid(),
                'epoch':status.get('epoch'),'mode':'dev'})
            time.sleep(.2)
    finally:
        for child in reversed(children):
            with contextlib.suppress(ProcessLookupError):child.terminate()
        deadline=time.monotonic()+20
        while any(p.poll() is None for p in children) and time.monotonic()<deadline:time.sleep(.1)
        for child in children:
            with contextlib.suppress(ProcessLookupError):child.kill()
            child.wait(timeout=3)
        # The enclosing Docker PID namespace/cgroup is the final kill boundary,
        # including grandchildren that left their process group or lost parents.
        for name in ('status.json','effective-config.json'):
            path=runtime/'agent'/name
            if path.exists():(STATE/('final-'+name)).write_bytes(path.read_bytes())
        for path in (runtime/'agent').glob('e*/backend.log'):
            (STATE/('backend-'+path.parent.name+'.log')).write_bytes(path.read_bytes())


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--services',type=Path);args=p.parse_args()
    if args.services:
        spec=json.loads(args.services.read_text())
        unprivileged()
        os.environ['HOME']=str(STATE/'services')
        services=load_services()
        services.run(spec,STATE/'services')
    else:
        try:
            run(json.loads((STATE/'launch.json').read_text()),STATE/'agent.json',
                STATE/'services.json' if (STATE/'services.json').exists() else None)
        except BaseException as exc:
            save(STATE/'supervisor.json',{'state':'FAILED','error':str(exc),'mode':'dev'})
            print('Pipeline deployment stopped: '+str(exc),file=sys.stderr)
            sys.exit(1)
