#!/usr/bin/env python3
"""Step 12 lifecycle acceptance on one disposable Linux VM, with real WireGuard."""
import argparse
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT/'pipeline/deployment'))
import bundle
import host


def require(value,message):
    if not value:raise AssertionError(message)


def wait(action,timeout=30):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=action()
        if value:return value
        time.sleep(.2)
    raise AssertionError('condition timed out')


def run(args,check=True,timeout=160):
    value=subprocess.run([str(a) for a in args],text=True,capture_output=True,timeout=timeout)
    if check and value.returncode:raise AssertionError(' '.join(map(str,args))+': '+value.stdout+value.stderr)
    return value


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True);p.add_argument('--artifact',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--service-ip',required=True,help='third unused IPv4 on the same LAN, for external dev services')
    args=p.parse_args()
    require(os.geteuid()==0 and sys.platform=='linux','run as root in disposable Linux VM')
    config=json.loads(args.config.read_text());artifact=json.loads(args.artifact.read_text())
    require(len(config['hosts'])==1 and config['backend']=='simulator','requires single-host simulator placement')
    args.output.mkdir(parents=True,exist_ok=False)
    output=bundle.generate(config,artifact,args.output/'bundles')
    folder=Path(output['bundles'][0]);b=host.verify(folder);cli=folder/'pipeline-deploy'
    result={'deployment_id':b['deployment_id'],'artifact':artifact,'config':b['config'],'passed':[]}
    firewall=run(['nft','--stateless','-j','list','ruleset']).stdout
    (args.output/'firewall-before.json').write_text(run(['nft','-j','list','ruleset']).stdout)
    (args.output/'firewall-rules-before.json').write_text(firewall)
    def record(name):result['passed'].append(name);print('PASS '+name,flush=True)
    def clean(rank):
        s=host.state(b,rank)
        if (s/'lease.json').exists() or host.inspect_container(b,rank) is not None:return False
        record=json.loads((s/'container.json').read_text()) if (s/'container.json').exists() else {}
        if record.get('cgroup') and any(p.read_text().strip() for p in Path(record['cgroup']).rglob('cgroup.procs')):
            return False
        if record.get('link') and run(['ip','link','show','dev',record['link']],check=False).returncode==0:
            return False
        return True
    def execute(rank,code,detach=False,user=None):
        argv=['docker','exec']
        if detach:argv+=['-d']
        if user:argv+=['--user',str(user)]
        return run(argv+[host.name(b,rank),'/usr/bin/python3','-c',code])
    try:
        bad_artifact={**artifact,'image_id':'sha256:'+'0'*64}
        bad=bundle.generate(config,bad_artifact,args.output/'bad-artifact')
        failure=run([Path(bad['bundles'][0])/'pipeline-deploy','check'],check=False)
        require(failure.returncode!=0,'wrong artifact passed preflight')
        bad_config=copy.deepcopy(config);bad_config['ranks'][0]['underlay']='198.18.0.211/24'
        bad=bundle.generate(bad_config,artifact,args.output/'bad-endpoint')
        failure=run([Path(bad['bundles'][0])/'pipeline-deploy','check'],check=False)
        require(failure.returncode!=0 and 'LAN' in failure.stderr,'wrong endpoint passed preflight')
        record('wrong artifact and endpoint rejected before resources/readiness')
        run([cli,'check'])
        # A stopped deployment may retain an old READY diagnostic. A new wait
        # must observe the new invocation, including its full service startup.
        for rank in (0,1):
            s=host.state(b,rank);s.mkdir(mode=0o711,parents=True,exist_ok=True)
            host.save(s/'supervisor.json',{'state':'READY','epoch':'stale'})
        run([cli,'start'])
        ready=json.loads(run([cli,'wait']).stdout)
        require(all(v['supervisor']['state']=='READY' and v['supervisor'].get('epoch')!='stale' for v in ready),
                'wait accepted a previous invocation READY marker')
        record('fresh start ignores stale READY diagnostics')
        answer=json.loads(run([cli,'request']).stdout)
        require(answer.get('choices') and answer.get('usage'),'Cocoon response/usage missing')
        record('clean deployment and complete Cocoon request')
        ids=[host.inspect_container(b,r)['Id'] for r in (0,1)]
        run([cli,'start'])
        require(ids==[host.inspect_container(b,r)['Id'] for r in (0,1)],'repeat start replaced running container')
        record('idempotent start')
        for rank in (0,1):
            obj=host.inspect_container(b,rank)
            require(obj['HostConfig']['ReadonlyRootfs'] and obj['HostConfig']['PidMode']!='host','mount/PID isolation absent')
            require(not any(m['Destination'] in ('/var/run/docker.sock','/work/cocoon') for m in obj['Mounts']),'runtime/socket mount leaked')
            execute(rank,'from pathlib import Path; assert len(list(Path("/proc").glob("[0-9]*"))) < 100')
            execute(rank,'import os; assert not os.access("/state/agent.json",os.R_OK); assert not os.access("/run/pipeline/agent/control.sock",os.W_OK)',user=10001)
            agent_status=json.loads((host.state(b,rank)/'agent-status.json').read_text())
            namespace=agent_status['group']['network']['namespace']
            code=('import os,pathlib; ns=os.stat('+repr('/run/netns/'+namespace)+').st_ino; found=[]\n'
                  'for p in pathlib.Path("/proc").glob("[0-9]*"):\n'
                  ' try:\n'
                  '  if int(p.name)==os.getpid(): continue\n'
                  '  s=dict(line.split(":",1) for line in (p/"status").read_text().splitlines() if ":" in line)\n'
                  '  if s["Uid"].split()[0]!="65534": continue\n'
                  '  assert os.stat(p/"ns/net").st_ino==ns\n'
                  '  assert int(s["CapEff"].strip(),16)==0 and int(s["CapBnd"].strip(),16)==0\n'
                  '  assert s["NoNewPrivs"].strip()=="1"\n'
                  '  assert (p/"cgroup").read_text()==pathlib.Path("/proc/self/cgroup").read_text()\n'
                  '  found.append(p.name)\n'
                  ' except FileNotFoundError: pass\n'
                  'assert found, "no sandboxed backend processes"')
            execute(rank,code,user=65534)
            backend=agent_status['backend_socket']
            execute(rank,'import socket; s=socket.socket(socket.AF_UNIX);\n'
                    'try: s.connect('+repr(backend)+'); raise AssertionError("service can bypass gate")\n'
                    'except PermissionError: pass',user=10001)
        record('private PID namespace, immutable runtime, protected agent configuration/control')
        record('backend helper UID/capabilities/network namespace/cgroup and private API socket')
        for rank in (0,1):
            audited=run(['python3',Path(__file__).with_name('deployment-sandbox.py'),
                         '--state',host.state(b,rank)])
            (args.output/('sandbox-'+str(rank)+'.json')).write_text(audited.stdout)
            require(json.loads(audited.stdout)['ok'],'host-side backend FD audit failed')
        record('host-side audit confirms backend has no inherited underlay FD')
        # A process outside the agent's process group must still be killed by
        # the enclosing rank cgroup when the agent disappears.
        for rank,both in ((0,False),(1,False),(0,True)):
            execute(rank,'import os,time,pathlib\nif os.fork():os._exit(0)\n'
                    'os.setsid();pathlib.Path("/tmp/escaped.pid").write_text(str(os.getpid()));time.sleep(1000)',detach=True,user=65534)
            execute(rank,'import os,time,pathlib;p=pathlib.Path("/tmp/escaped.pid");end=time.monotonic()+3\n'
                    'while not p.exists():\n assert time.monotonic()<end;time.sleep(.05)\n'
                    'os.kill(int(p.read_text()),0)')
            s=host.state(b,rank)
            lease=json.loads((s/'lease.json').read_text())
            agent=json.loads((s/'supervisor.json').read_text())['agent_pid']
            guardian=json.loads((s/'agent-status.json').read_text())['group']['network']['guardian']['pid']
            kill_guardian=f'os.kill({guardian},signal.SIGKILL);' if both else ''
            execute(rank,f'import os,signal;{kill_guardian}os.kill({agent},signal.SIGKILL)')
            wait(lambda: clean(rank),45)
            record(('agent+guardian' if both else 'agent')+' SIGKILL rank '+str(rank)+' cleans entire cgroup')
            run([cli,'stop']);require(all(clean(r) for r in (0,1)),'stop left resources')
            if rank==0 and not both:
                # Fault-inject an unconfirmed ownership record. The admission
                # guard must refuse a restart even though no container exists.
                (s/'lease.json').write_text(json.dumps(lease))
                refused=run([cli,'start'],check=False)
                require(refused.returncode!=0 and 'cleanup unconfirmed' in refused.stderr,'retained lease did not block restart')
                require(host.inspect_container(b,rank) is None,'blocked start created a container')
                run([cli,'stop']);require(clean(rank),'explicit cleanup did not release retained lease')
                record('unconfirmed ownership blocks restart until explicit cleanup')
            run([cli,'start']);run([cli,'wait']);run([cli,'request'])
        record('restart after confirmed cleanup')
        # Kill the outer owner: ExecStopPost must operate independently.
        run(['systemctl','kill','--kill-whom=main','--signal=SIGKILL',host.name(b,0)])
        wait(lambda: clean(0),45);run([cli,'stop'])
        record('host supervisor SIGKILL cleanup')
        # Start just one member with the public per-host unit: partial startup
        # has a bounded failure even though no head ever connects.
        run(['systemctl','reset-failed',host.name(b,1)],check=False)
        run(['systemctl','start',host.name(b,1)])
        wait(lambda: (host.state(b,1)/'lease.json').exists(),15)
        wait(lambda: clean(1),b['config']['startup_timeout']+50)
        record('partial group startup deadline and cleanup')
        run([cli,'stop']);run([cli,'stop'])
        module=importlib.util.spec_from_file_location('egress',Path(__file__).with_name('test-pipeline-deployment-egress.py'))
        egress=importlib.util.module_from_spec(module);module.loader.exec_module(egress)
        egress.check(config,artifact,args.service_ip,args.output/'external')
        record('external Cocoon services under default drop and negative egress checks')
        after=run(['nft','--stateless','-j','list','ruleset']).stdout
        (args.output/'firewall-after.json').write_text(run(['nft','-j','list','ruleset']).stdout)
        (args.output/'firewall-rules-after.json').write_text(after)
        require(json.loads(after)==json.loads(firewall),'host firewall rules changed')
        record('scoped idempotent cleanup; host firewall preserved')
        result['ok']=True
    except BaseException as exc:
        result['ok']=False;result['error']=str(exc);raise
    finally:
        stop=run([cli,'stop'],check=False)
        result['final_cleanup']=stop.returncode==0 and all(clean(r) for r in (0,1))
        (args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        require(result['final_cleanup'],'final cleanup failed: '+stop.stderr)


if __name__=='__main__':main()
