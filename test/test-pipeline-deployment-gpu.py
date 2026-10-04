#!/usr/bin/env python3
"""Step 12: deploy the same immutable image on the two configured GPU hosts."""
import argparse
import concurrent.futures
import ipaddress
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT/'pipeline/deployment'))
import bundle


def require(value,message):
    if not value:raise AssertionError(message)


class Lab:
    def __init__(self,config,remote):
        self.hosts=config['hosts'];self.key=str(Path(config['ssh_key']).expanduser())
        self.remote=remote;self.config=config

    def run(self,args,check=True,timeout=180):
        r=subprocess.run([str(a) for a in args],text=True,capture_output=True,timeout=timeout)
        if check and r.returncode:raise RuntimeError(' '.join(map(str,args[:5]))+': '+r.stdout+r.stderr)
        return r

    def ssh(self,rank,args,check=True,timeout=180):
        return self.run(['ssh','-i',self.key,'-o','BatchMode=yes','-o','ConnectTimeout=10',
            self.hosts[rank]['ssh'],shlex.join(map(str,args))],check,timeout)

    def both(self,action):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(action,r) for r in (0,1)]
            return [future.result() for future in futures]

    def upload(self,rank,source,destination):
        return self.run(['scp','-i',self.key,'-o','BatchMode=yes','-r',source,
            self.hosts[rank]['ssh']+':'+destination],timeout=1800)

    def download(self,rank,source,destination):
        return self.run(['scp','-i',self.key,'-o','BatchMode=yes',
            self.hosts[rank]['ssh']+':'+source,destination],timeout=1800)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend',required=True,choices=('sglang','vllm'))
    p.add_argument('--model',choices=('small','large'),default='large')
    p.add_argument('--lab',type=Path,default=ROOT/'experiments/gpu-pipeline/lab.json')
    p.add_argument('--remote-dir',default='/home/ruslixag/cocoon-step12')
    p.add_argument('--artifact-dir',required=True,help='directory on head containing artifact.json')
    p.add_argument('--image-archive',help='existing completed docker save archive on head, if member needs image')
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    lab=Lab(json.loads(args.lab.read_text()),args.remote_dir)
    artifact=json.loads(lab.ssh(0,['cat',args.artifact_dir+'/artifact.json']).stdout)
    require(artifact['backend']==args.backend,'artifact backend differs')
    (args.output/'artifact.json').write_text(json.dumps(artifact,indent=2)+'\n')
    print('Verifying image on both hosts',flush=True)
    lab.both(lambda r:lab.ssh(r,['sudo','-n','docker','image','inspect',artifact['base_image']]))
    if lab.ssh(1,['sudo','-n','docker','image','inspect',artifact['image_id']],False).returncode:
        require(args.image_archive,'member needs exact image; pass --image-archive with a completed archive on head')
        print('Transferring image archive',flush=True)
        local=args.output/'image.tar';lab.download(0,args.image_archive,local)
        lab.ssh(1,['mkdir','-p',args.remote_dir]);lab.upload(1,local,args.remote_dir+'/image-'+args.backend+'.tar')
        lab.ssh(1,['sudo','-n','docker','load','-i',args.remote_dir+'/image-'+args.backend+'.tar'],timeout=1800)
    for rank in (0,1):
        info=json.loads(lab.ssh(rank,['sudo','-n','docker','image','inspect',artifact['image_id']]).stdout)[0]
        require(info['Id']==artifact['image_id'],'ranks do not have identical artifacts')
    config={'schema':1,'mode':'dev','backend':args.backend,'model':args.model,'hosts':[], 'ranks':[],
            'services':{'mode':'local-fake-ton','coefficient':1000},'startup_timeout':900}
    before=[]
    for rank,h in enumerate(lab.hosts):
        rows=lab.ssh(rank,['nvidia-smi','--query-gpu=pci.bus_id,uuid,memory.used','--format=csv,noheader,nounits']).stdout.strip().splitlines()
        require(len(rows)==1,'lab acceptance requires exactly one GPU per host')
        bdf,uuid,memory=map(str.strip,rows[0].split(','));before.append({'uuid':uuid,'memory_mib':int(memory)})
        require(int(memory)<64,'GPU is in use; refusing acceptance')
        name='head' if rank==0 else 'member'
        config['hosts'].append({'id':name,'hostname':h['hostname'],'interface':lab.config['underlay_interface']})
        config['ranks'].append({'host':name,'underlay':str(ipaddress.IPv4Address(h['lan'])+10)+'/24',
            'cpus':'0-7','memory_mib':24576,'gpu':bdf.lower()[-12:], 'model_root':lab.config['remote_dir']+'/models'})
    (args.output/'deployment.json').write_text(json.dumps(config,indent=2)+'\n')
    generated=bundle.generate(config,artifact,args.output/'bundles')
    deployment=generated['deployment_id'];names=['cocoon-pipeline-'+deployment[:20]+'-'+str(r) for r in (0,1)]
    remote_bundle=args.remote_dir+'/bundles/'+deployment
    for rank in (0,1):
        lab.ssh(rank,['mkdir','-p',remote_bundle])
        lab.upload(rank,generated['bundles'][rank]+'/.',remote_bundle)
        lab.upload(rank,ROOT/'test/deployment-sandbox.py',args.remote_dir+'/audit-sandbox.py')
    def deploy(rank,command,timeout=180):
        return lab.ssh(rank,['sudo','-n',remote_bundle+'/pipeline-deploy',command],timeout=timeout)
    def execute(rank,code):
        return lab.ssh(rank,['sudo','-n','docker','exec',names[rank],'/usr/bin/python3','-c',code])
    def ready():
        values=lab.both(lambda r:deploy(r,'wait',950))
        epochs={json.loads(v.stdout)[0]['supervisor']['epoch'] for v in values}
        require(len(epochs)==1,'wait returned different deployment epochs')
        return values
    def memory():
        return [int(lab.ssh(r,['nvidia-smi','--query-gpu=memory.used','--format=csv,noheader,nounits']).stdout.strip()) for r in (0,1)]
    def stopped():
        statuses=lab.both(lambda r:deploy(r,'stop',120))
        for r in (0,1):
            require(lab.ssh(r,['sudo','-n','docker','inspect',names[r]],False).returncode!=0,'container left after stop')
            code=('import pathlib,json,subprocess;s=pathlib.Path('+repr('/var/lib/cocoon-pipeline/'+deployment+'/rank-'+str(r))+');'
                  'assert not (s/"lease.json").exists();c=json.loads((s/"container.json").read_text());'
                  'assert not any(p.read_text().strip() for p in pathlib.Path(c["cgroup"]).rglob("cgroup.procs"));'
                  'assert subprocess.run(["ip","link","show","dev",c["link"]],capture_output=True).returncode!=0')
            lab.ssh(r,['sudo','-n','python3','-c',code])
        end=time.monotonic()+30
        while True:
            current=memory()
            if all(v<=before[r]['memory_mib']+8 for r,v in enumerate(current)):break
            require(time.monotonic()<end,'GPU memory not released');time.sleep(1)
        return {'gpu_memory_mib':current,'commands':[json.loads(s.stdout) for s in statuses]}
    result={'artifact':artifact,'deployment_id':deployment,'config':config,'before':before,'passed':[]}
    def record(name):result['passed'].append(name);print('PASS '+name,flush=True)
    try:
        lab.both(lambda r:deploy(r,'check'))
        print('Starting member and head',flush=True)
        deploy(1,'start');deploy(0,'start');ready()
        statuses=lab.both(lambda r:deploy(r,'status'))
        (args.output/'ready.json').write_text(json.dumps([json.loads(s.stdout) for s in statuses],indent=2)+'\n')
        value=json.loads(deploy(0,'request').stdout)
        require(value.get('choices') and value.get('usage',{}).get('total_tokens',0)>0,'Cocoon response/usage missing')
        result['response']=value;record('identical pinned image; real PP=2 Cocoon response with usage')
        identifiers=[]
        for rank in (0,1):
            obj=json.loads(lab.ssh(rank,['sudo','-n','docker','inspect',names[rank]]).stdout)[0];identifiers.append(obj['Id'])
            require(obj['HostConfig']['PidMode']!='host' and obj['HostConfig']['ReadonlyRootfs'],'runtime isolation missing')
            require(obj['HostConfig']['DeviceRequests'][0]['DeviceIDs']==[before[rank]['uuid']],'wrong GPU visibility')
            require(not any(m['Destination'] in ('/work/cocoon','/var/run/docker.sock') for m in obj['Mounts']),'source/socket mount leaked')
        record('private PID, read-only runtime/model, one assigned GPU per rank')
        for rank in (0,1):
            audit=json.loads(lab.ssh(rank,['sudo','-n','python3',args.remote_dir+'/audit-sandbox.py',
                '--state','/var/lib/cocoon-pipeline/'+deployment+'/rank-'+str(rank)]).stdout)
            require(audit['ok'] and len(audit['backend'])>=2,'real backend/helper sandbox audit failed')
            if rank==0:require(audit['services'],'head service users missing')
            (args.output/('sandbox-'+str(rank)+'.json')).write_text(json.dumps(audit,indent=2)+'\n')
        record('real backend/helper credentials, network namespace, cgroup and no inherited underlay FD')
        lab.both(lambda r:deploy(r,'start'))
        for r in (0,1):
            require(json.loads(lab.ssh(r,['sudo','-n','docker','inspect',names[r]]).stdout)[0]['Id']==identifiers[r],'duplicate start replaced container')
        record('repeat start is idempotent')
        print('Killing head agent; checking external cleanup',flush=True)
        execute(0,'import json,os,signal;os.kill(json.load(open("/state/supervisor.json"))["agent_pid"],signal.SIGKILL)')
        end=time.monotonic()+60
        while True:
            status=json.loads(deploy(0,'status').stdout)[0]
            if (status.get('cleanup') or {}).get('clean') and status['unit']=='failed':break
            require(time.monotonic()<end,'agent SIGKILL cleanup deadline');time.sleep(1)
        result['fault_cleanup']=stopped();record('agent SIGKILL cleanup and GPU release')
        deploy(1,'start');deploy(0,'start');ready()
        value=json.loads(deploy(0,'request').stdout)
        require(value.get('choices') and value.get('usage'),'request after clean restart failed')
        record('repeat deployment and complete request after failure')
        result['ok']=True
    except BaseException as exc:
        result.update(ok=False,error=str(exc));raise
    finally:
        try:result['cleanup']=stopped()
        finally:
            for r in (0,1):
                code=('import pathlib,json;root=pathlib.Path('+repr('/var/lib/cocoon-pipeline/'+deployment+'/rank-'+str(r))+');'
                      'print(json.dumps({str(p.relative_to(root)):p.read_bytes()[-65536:].decode(errors="replace") '
                      'for p in root.rglob("*") if p.is_file() and p.suffix in (".json",".log")}))')
                data=lab.ssh(r,['sudo','-n','python3','-c',code],False)
                (args.output/('diagnostics-'+str(r)+'.json')).write_text(data.stdout or json.dumps({'error':data.stderr}))
            (args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__':main()
