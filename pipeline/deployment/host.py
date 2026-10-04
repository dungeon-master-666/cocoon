#!/usr/bin/env python3
"""Linux host CLI for a generated, explicitly non-confidential dev bundle."""
import argparse
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time

sys.dont_write_bytecode = True
from bundle import cpus, digest, runtime, validate

BASE = Path('/var/lib/cocoon-pipeline')
UNITS = Path('/etc/systemd/system')
LABEL = 'org.cocoon.pipeline'


def command(args, check=True, timeout=60):
    result = subprocess.run([str(a) for a in args], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(' '.join(map(str,args[:4])) + ': ' + result.stderr.strip() + result.stdout.strip())
    return result.stdout.strip() if check else result


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path=Path(path);tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,indent=2)+'\n');tmp.chmod(0o600);tmp.replace(path)


@contextlib.contextmanager
def locked():
    BASE.mkdir(mode=0o700,exist_ok=True)
    with (BASE/'host.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield


def verify(directory):
    directory=Path(directory).resolve()
    hashes=read(directory/'files.sha256.json')
    actual={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()
            if p.name!='files.sha256.json' and p.is_file()}
    if actual != hashes or any(p.is_symlink() or not p.is_file() for p in directory.iterdir()):
        raise ValueError('bundle files changed; regenerate the bundle')
    b=read(directory/'bundle.json')
    config=validate(b['config'],b['artifact'],b['models'])
    tooling={name:actual[name] for name in ('bundle.py','host.py')}
    if (b['tooling']!=tooling or config!=b['config'] or
            b['deployment_id']!=digest({'config':config,'artifact':b['artifact'],'tooling':tooling})):
        raise ValueError('bundle identity/configuration mismatch')
    local=[i for i,n in enumerate(config['ranks']) if n['host']==b['host']]
    if not local or b['ranks']!=local:
        raise ValueError('bundle has incorrect placement')
    for rank in local:
        agent,service=runtime(config,rank,b['models'],b['deployment_id'])
        if read(directory/f'agent-{rank}.json')!=agent:
            raise ValueError('agent configuration differs from authoritative profile')
        if service and read(directory/'services.json')!=service:
            raise ValueError('worker model/capacity/forward endpoint differs from authoritative profile')
    if 0 not in local and (directory/'services.json').exists():
        raise ValueError('member must not run worker services')
    return b


def name(b,rank):
    return 'cocoon-pipeline-'+b['deployment_id'][:20]+'-'+str(rank)


def directory(b):
    return BASE/b['deployment_id']


def state(b,rank):
    return directory(b)/('rank-'+str(rank))


def inspect_container(b,rank):
    result=command(['docker','inspect',name(b,rank)],check=False)
    if result.returncode:
        if 'no such object' in result.stderr.lower() or 'no such container' in result.stderr.lower():
            return None
        raise RuntimeError('cannot confirm container state: '+result.stderr)
    obj=json.loads(result.stdout)[0]
    labels=obj['Config'].get('Labels') or {}
    if labels.get(LABEL+'.deployment')!=b['deployment_id'] or labels.get(LABEL+'.rank')!=str(rank):
        raise RuntimeError('container name belongs to another owner; refusing to touch it')
    return obj


def preflight(b):
    if os.geteuid()!=0 or sys.platform!='linux':raise ValueError('run with sudo on the target Linux host')
    host=next(h for h in b['config']['hosts'] if h['id']==b['host'])
    if socket.gethostname()!=host['hostname']:
        raise ValueError('wrong host: expected '+host['hostname']+', got '+socket.gethostname())
    for executable in ('docker','systemctl','ip','nsenter','arping'):
        if not shutil.which(executable):raise ValueError('missing host dependency: '+executable)
    if not Path('/run/systemd/system').is_dir() or not Path('/sys/fs/cgroup/cgroup.controllers').exists():
        raise ValueError('systemd and cgroup v2 are required')
    command(['docker','info'])
    if {'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine())!=b['artifact']['architecture']:
        raise ValueError('image architecture does not match this host')
    image=json.loads(command(['docker','image','inspect',b['artifact']['image_id']]))[0]
    labels=image['Config'].get('Labels') or {}
    if (image['Id']!=b['artifact']['image_id'] or image['Architecture']!=b['artifact']['architecture'] or
            labels.get(LABEL+'.backend')!=b['artifact']['backend'] or
            labels.get(LABEL+'.catalog')!=b['artifact']['model_catalog_sha256']):
        raise ValueError('loaded image does not match artifact/profile; load the exact image archive')
    links=json.loads(command(['ip','-j','link','show','dev',host['interface']]))
    if 'UP' not in links[0]['flags']:raise ValueError('underlay interface is down')
    online=cpus(Path('/sys/devices/system/cpu/online').read_text().strip())
    for rank in b['ranks']:
        node=b['config']['ranks'][rank]
        if not cpus(node['cpus'])<=online:raise ValueError('assigned CPU is offline or absent')
        address=ipaddress.IPv4Interface(node['underlay'])
        addrs=json.loads(command(['ip','-j','addr','show','dev',host['interface']]))[0]['addr_info']
        if not any(a['family']=='inet' and ipaddress.IPv4Address(a['local']) in address.network for a in addrs):
            raise ValueError('underlay address is not on the selected interface LAN')
        if any(a['family']=='inet' and a['local']==str(address.ip) for a in addrs):
            raise ValueError('rank must have its own unused LAN IP, not the host IP')
    return host


def gpu_uuid(node):
    rows=command(['nvidia-smi','--query-gpu=pci.bus_id,uuid','--format=csv,noheader,nounits'])
    selected=None
    for row in rows.splitlines():
        bus,uuid=map(str.strip,row.split(','))
        if bus.lower()[-12:]==node['gpu']:selected=uuid
    if selected is None:raise ValueError('assigned GPU BDF not found')
    active=command(['nvidia-smi','-i',selected,'--query-compute-apps=pid','--format=csv,noheader,nounits'])
    if active:raise ValueError('assigned GPU already has compute processes')
    if not Path(node['model_root']).is_dir():raise ValueError('model directory missing; prepare artifacts before serving')
    return selected


def reserve(b,rank):
    node=b['config']['ranks'][rank]
    lease={'deployment':b['deployment_id'],'rank':rank,'cpus':node['cpus'],
           'gpu':node.get('gpu'),'ip':str(ipaddress.IPv4Interface(node['underlay']).ip),
           'memory_mib':node['memory_mib']}
    pending_memory=0
    for path in BASE.glob('*/rank-*/lease.json'):
        other=read(path)
        if (lease['ip']==other['ip'] or cpus(lease['cpus'])&cpus(other['cpus']) or
                lease['gpu'] is not None and lease['gpu']==other['gpu']):
            raise ValueError('resource reserved by '+str(path)+'; stop that deployment and confirm cleanup first')
        record=read(path.parent/'container.json') if (path.parent/'container.json').exists() else {}
        usage=Path(record['cgroup'])/'memory.current' if record.get('cgroup') else None
        used=int(usage.read_text()) if usage and usage.exists() else 0
        pending_memory+=max(0,other['memory_mib']*1024*1024-used)
    memory={line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    if node['memory_mib']*1024*1024>memory['MemAvailable']*1024-pending_memory-256*1024*1024:
        raise ValueError('insufficient currently available host memory')
    uuid=gpu_uuid(node) if node.get('gpu') else None
    host=next(h for h in b['config']['hosts'] if h['id']==b['host'])
    probe=command(['arping','-D','-q','-I',host['interface'],'-c','2','-w','3',lease['ip']],check=False)
    if probe.returncode:raise ValueError('underlay IP is already in use or duplicate-address probe failed')
    if uuid:
        lease['gpu_uuid']=uuid
        lease['numa_node']=(Path('/sys/bus/pci/devices')/node['gpu']/'numa_node').read_text().strip()
    save(state(b,rank)/'lease.json',lease)
    return lease


def cgroup_empty(path):
    root=Path(path)
    return not root.exists() or all(not p.read_text().strip() for p in root.rglob('cgroup.procs'))


def cleanup(b,rank):
    """Runs from systemd ExecStopPost, even if the foreground supervisor was killed."""
    with locked():
        s=state(b,rank);obj=inspect_container(b,rank)
        if obj:
            command(['docker','rm','--force',obj['Id']],timeout=60)
        record=read(s/'container.json') if (s/'container.json').exists() else {}
        end=time.monotonic()+15
        while record.get('cgroup') and not cgroup_empty(record['cgroup']):
            if time.monotonic()>end:raise RuntimeError('cgroup still populated; resource lease retained')
            time.sleep(.1)
        link=record.get('link')
        if link:
            result=command(['ip','-j','link','show','dev',link],check=False)
            if result.returncode==0:
                info=json.loads(result.stdout)[0]
                if info.get('ifalias')!=b['deployment_id']+':'+str(rank):
                    raise RuntimeError('network link owner differs; lease retained')
                command(['ip','link','del',link])
        if inspect_container(b,rank):raise RuntimeError('container cleanup unconfirmed; lease retained')
        (s/'lease.json').unlink(missing_ok=True)
        (s/'network-ready').unlink(missing_ok=True)
        save(s/'cleanup.json',{'clean':True,'at':time.time(),'container_id':record.get('id')})


def supervise(b,rank):
    s=state(b,rank);node=b['config']['ranks'][rank]
    stopping=False
    def stop(*unused):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    with locked():
        host=preflight(b)
        if inspect_container(b,rank):raise RuntimeError('previous container exists; cleanup required')
        lease=reserve(b,rank)
        for filename in ('supervisor.json','agent-status.json','cleanup.json','network-ready'):
            (s/filename).unlink(missing_ok=True)
        agent,services=runtime(b['config'],rank,b['models'],b['deployment_id'])
        save(s/'agent.json',agent)
        if services:save(s/'services.json',services)
        save(s/'launch.json',{'node':node,'startup_timeout':b['config']['startup_timeout']})
        argv=['docker','create','--name',name(b,rank),'--label',LABEL+'.deployment='+b['deployment_id'],
              '--label',LABEL+'.rank='+str(rank),'--network','none','--init','--read-only',
              '--cap-drop','ALL','--cap-add','NET_ADMIN','--cap-add','SYS_ADMIN','--cap-add','SETUID',
              '--cap-add','SETGID','--cap-add','SETPCAP','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE',
              '--cap-add','KILL','--security-opt','no-new-privileges',
              '--security-opt','apparmor=unconfined','--security-opt','seccomp=unconfined',
              '--pids-limit','2048','--cpuset-cpus',node['cpus'],'--memory',str(node['memory_mib'])+'m',
              '--memory-swap',str(node['memory_mib'])+'m','--shm-size','2g' if node.get('gpu') else '1g',
              # Triton/torch load JIT shared libraries from the private epoch
              # under /run. Root-owned helpers stay on the read-only image.
              '--tmpfs','/run:rw,exec,nosuid,nodev,mode=755','--tmpfs','/tmp:rw,nosuid,nodev,mode=1777',
              '--mount','type=bind,src='+str(s)+',dst=/state','--env','PYTHONDONTWRITEBYTECODE=1']
        if node.get('gpu'):
            argv+=['--ulimit','memlock=-1','--gpus','device='+lease['gpu_uuid'],
                   '--mount','type=bind,src='+node['model_root']+',dst=/models,readonly']
        argv+=[b['artifact']['image_id']]
        ident=command(argv)
        link='cp'+b['deployment_id'][:10]+str(rank)
        record={'id':ident,'link':link}
        save(s/'container.json',record)
        command(['docker','start',ident])
        obj=inspect_container(b,rank);pid=obj['State']['Pid']
        cg=Path(f'/proc/{pid}/cgroup').read_text().strip().split('0::',1)[1]
        if ident not in cg:raise RuntimeError('cannot identify dedicated container cgroup')
        cgroup=Path('/sys/fs/cgroup'+cg)
        if not (cgroup/'cgroup.procs').is_file() or str(pid) not in (cgroup/'cgroup.procs').read_text().split():
            raise RuntimeError('container cgroup is not observable from this host namespace')
        record['cgroup']=str(cgroup);record['cgroup_validated']=True;save(s/'container.json',record)
        command(['ip','link','add','link',host['interface'],'name',link,'alias',
                 b['deployment_id']+':'+str(rank),'type','ipvlan','mode','l2'])
        command(['ip','link','set',link,'netns',str(pid)])
        command(['nsenter','-t',str(pid),'-n','ip','link','set',link,'name','eth0'])
        (s/'network-ready').touch()
    while not stopping:
        obj=inspect_container(b,rank)
        if not obj or not obj['State']['Running']:
            if obj:
                logs=command(['docker','logs',obj['Id']],check=False)
                (s/'container.log').write_text(logs.stdout+logs.stderr)
            raise RuntimeError('rank container exited; see '+str(s))
        time.sleep(.5)
    command(['docker','stop','--time','25',name(b,rank)],timeout=40)


def install(b,source):
    target=directory(b)/'bundle'
    if target.exists():
        if read(target/'files.sha256.json')!=read(source/'files.sha256.json'):
            raise ValueError('installed bundle differs; stop it and generate a new artifact/bundle')
        verify(target)
    else:
        target.parent.mkdir(mode=0o700,exist_ok=True)
        shutil.copytree(source,target)
        for p in target.iterdir():p.chmod(0o700 if p.name=='pipeline-deploy' else 0o600)
    for rank in b['ranks']:
        s=state(b,rank);s.mkdir(mode=0o711,exist_ok=True)
        unit=UNITS/(name(b,rank)+'.service')
        content='\n'.join(['[Unit]','Description=Cocoon pipeline dev rank '+str(rank),
            'After=docker.service network-online.target','Requires=docker.service','', '[Service]',
            'Type=simple','UMask=0077','Restart=no','KillMode=control-group','TimeoutStopSec=80',
            'ExecStart=/usr/bin/python3 -B '+str(target/'host.py')+' _supervise --rank '+str(rank),
            'ExecStopPost=/usr/bin/python3 -B '+str(target/'host.py')+' _cleanup --rank '+str(rank),''])
        if unit.exists() and unit.read_text()!=content:raise ValueError('systemd unit already has different contents')
        unit.write_text(content)
    command(['systemctl','daemon-reload'])


def status(b):
    result=[]
    for rank in b['ranks']:
        s=state(b,rank)
        unit=command(['systemctl','show',name(b,rank),'-p','ActiveState','--value'],check=False).stdout.strip()
        value=read(s/'supervisor.json') if (s/'supervisor.json').exists() else {'state':'STOPPED'}
        if unit not in ('active','activating'):value={**value,'state':'FAILED' if unit=='failed' else 'STOPPED'}
        result.append({'rank':rank,'unit':unit,'supervisor':value,'diagnostics':str(s),
                       'cleanup':read(s/'cleanup.json') if (s/'cleanup.json').exists() else None})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['check','start','status','wait','stop','request','_supervise','_cleanup'])
    parser.add_argument('--rank',type=int,choices=(0,1))
    parser.add_argument('--timeout',type=int)
    parser.add_argument('--prompt',default='Say hello.')
    args=parser.parse_args()
    if os.geteuid()!=0:parser.error('run with sudo on the target host')
    source=Path(__file__).resolve().parent;b=verify(source)
    if args.rank is not None and args.rank not in b['ranks']:parser.error('rank is not assigned to this host')
    if args.command in ('_supervise','_cleanup'):
        if args.rank is None:parser.error('internal command requires rank')
        return (supervise if args.command=='_supervise' else cleanup)(b,args.rank)
    if args.command=='check':
        preflight(b);print(json.dumps({'valid':True,'mode':'dev','deployment_id':b['deployment_id']}));return
    if args.command=='start':
        with locked():
            preflight(b);install(b,source)
            for rank in b['ranks']:
                if command(['systemctl','is-active',name(b,rank)],check=False).returncode==0:continue
                if (state(b,rank)/'lease.json').exists() or inspect_container(b,rank):
                    raise RuntimeError('previous cleanup unconfirmed; run stop first')
                # systemd starts asynchronously; preflight/reservation in the
                # new supervisor can take seconds. Never expose a READY marker
                # from the previous invocation during that window.
                save(state(b,rank)/'supervisor.json',{'state':'STARTING','mode':'dev'})
                (state(b,rank)/'cleanup.json').unlink(missing_ok=True)
                command(['systemctl','reset-failed',name(b,rank)],check=False)
                command(['systemctl','start','--no-block',name(b,rank)])
        print(json.dumps({'started':b['ranks'],'deployment_id':b['deployment_id'],'next':'pipeline-deploy wait'}));return
    if args.command=='stop':
        for rank in b['ranks']:
            result=command(['systemctl','stop',name(b,rank)],check=False,timeout=100)
            if result.returncode and 'not loaded' not in result.stderr:raise RuntimeError(result.stderr)
            if state(b,rank).exists():cleanup(b,rank)
        print(json.dumps({'stopped':b['ranks'],'cleanup_confirmed':True}));return
    if args.command=='status':print(json.dumps(status(b),indent=2));return
    if args.command=='wait':
        end=time.monotonic()+(args.timeout or b['config']['startup_timeout']+10)
        while time.monotonic()<end:
            values=status(b)
            if all(v['supervisor']['state']=='READY' for v in values):
                print(json.dumps(values,indent=2));return
            if any(v['unit'] in ('failed','inactive') for v in values):raise RuntimeError('rank stopped: '+json.dumps(values))
            time.sleep(.5)
        raise RuntimeError('readiness deadline; use status and inspect diagnostics')
    if args.command=='request':
        if 0 not in b['ranks'] or b['config']['services']['mode']!='local-fake-ton':
            raise ValueError('request is available on head with local-fake-ton services')
        model=runtime(b['config'],0,b['models'])[1]['model']
        body=json.dumps({'model':model,'messages':[{'role':'user','content':args.prompt}],
                         'max_tokens':32,'stream':False})
        print(command(['docker','exec',name(b,0),'nsenter','--net=/run/netns/underlay','/usr/bin/python3','-c',
            'import sys,urllib.request;print(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:10000/v1/chat/completions",data=sys.argv[1].encode(),headers={"Content-Type":"application/json"}),timeout=120).read().decode())',body],timeout=130))


if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,OSError,RuntimeError,subprocess.SubprocessError) as exc:
        print('Deployment error: '+str(exc),file=sys.stderr);sys.exit(1)
