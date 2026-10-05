#!/usr/bin/env python3
"""Control the two dev GPU VMs through SSH; all hypervisor changes use vm-host.py."""
import argparse
import importlib.util
import ipaddress
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
spec=importlib.util.spec_from_file_location('vm_host',HERE/'vm-host.py')
vm_host=importlib.util.module_from_spec(spec);spec.loader.exec_module(vm_host)
REMOTE='/home/ruslixag/cocoon-vm-artifacts'
BASE_SHA256='6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2'


class Lab:
    def __init__(self,lab,state):
        self.lab=lab if isinstance(lab,dict) else json.loads(Path(lab).read_text())
        if self.lab.get('mode')!='dev-only' or len(self.lab['hosts'])!=2:raise ValueError('expected two-host dev lab')
        self.state=Path(state).resolve();self.state.mkdir(parents=True,exist_ok=True)
        self.host_key=str(Path(self.lab['ssh_key']).expanduser())
        self.guest_key=self.state/'id_ed25519'

    def host_command(self,rank,args):
        return ['ssh','-i',self.host_key,'-o','BatchMode=yes','-o','ConnectTimeout=10',
                self.lab['hosts'][rank]['ssh'],shlex.join([str(a) for a in args])]

    def host(self,rank,args,**kwargs):
        return subprocess.run(self.host_command(rank,args),check=True,text=True,capture_output=True,**kwargs).stdout

    def config(self,rank):
        return vm_host.validate(json.loads((self.state/f'vm-{rank}.json').read_text()))

    def init(self):
        if not self.guest_key.exists():
            subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-C','cocoon-vfio-vm-dev','-f',str(self.guest_key)],check=True)
        for rank,h in enumerate(self.lab['hosts']):
            c={'schema':1,'mode':'dev','name':'cocoon-pipeline-vm-'+('head' if rank==0 else 'member'),
                'hostname':h['hostname'],'gpu':'0000:01:00.0','interface':self.lab['underlay_interface'],
                'guest_lan':str(ipaddress.IPv4Address(h['lan'])+20)+'/24','mtu':1400,
                'vcpus':8,'memory_mib':32768,'disk_gib':200,
                'base_image':'/var/lib/cocoon-pipeline-vm/images/noble-server-cloudimg-amd64.img',
                'base_sha256':BASE_SHA256,'model_root':self.lab['remote_dir']+'/models',
                'artifact_root':REMOTE,'ssh_public_key':Path(str(self.guest_key)+'.pub').read_text().strip()}
            path=self.state/f'vm-{rank}.json'
            if path.exists():
                if json.loads(path.read_text())!=c:raise RuntimeError('existing config differs; inspect it before proceeding')
            else:vm_host.save(path,c)

    def upload(self,rank):
        c=self.config(rank)
        actual=self.host(rank,['hostname']).strip()
        if actual!=c['hostname']:raise RuntimeError('SSH host identity mismatch')
        self.host(rank,['mkdir','-p',REMOTE])
        subprocess.run(['scp','-i',self.host_key,str(HERE/'vm-host.py'),str(self.state/f'vm-{rank}.json'),
                        self.lab['hosts'][rank]['ssh']+':'+REMOTE+'/'],check=True)

    def action(self,rank,action):
        return json.loads(self.host(rank,['sudo','-n','python3',REMOTE+'/vm-host.py',action,
                             '--config',REMOTE+f'/vm-{rank}.json'],timeout=240))

    def guest_command(self,rank,args):
        c=self.config(rank)
        status=self.action(rank,'status')
        if not status['active']:raise RuntimeError('VM is stopped')
        rows=[r.split() for r in status['addresses'].splitlines()]
        ips=[str(ipaddress.IPv4Interface(r[3]).ip) for r in rows
             if len(r)==4 and r[1]==vm_host.mac(c,0) and r[2]=='ipv4']
        if len(ips)!=1:raise RuntimeError('guest management DHCP lease not ready')
        proxy=shlex.join(['ssh','-i',self.host_key,'-o','BatchMode=yes','-o','ConnectTimeout=10',
                         '-W','%h:%p',self.lab['hosts'][rank]['ssh']])
        return ['ssh','-i',str(self.guest_key),'-o','BatchMode=yes','-o','ConnectTimeout=10',
                '-o','StrictHostKeyChecking=accept-new','-o','UserKnownHostsFile='+str(self.state/'known_hosts'),
                '-o','HostKeyAlias='+c['name'],'-o','ProxyCommand='+proxy,'cocoon@'+ips[0],
                shlex.join([str(a) for a in args])]

    def guest(self,rank,args,**kwargs):
        return subprocess.run(self.guest_command(rank,args),check=True,text=True,capture_output=True,**kwargs).stdout

    def copy_guest_command(self,rank,source,destination,download=False):
        ssh=self.guest_command(rank,[])[:-1]
        paths=[ssh[-1]+':'+str(source),str(destination)] if download else [str(source),ssh[-1]+':'+str(destination)]
        return ['scp',*ssh[1:-1],'-r',*paths]

    def wait(self,rank,timeout=300):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:
                value=self.guest(rank,['systemd-detect-virt'],timeout=20).strip()
                if value!='kvm':raise RuntimeError('guest is not KVM')
                return self.action(rank,'status')
            except (RuntimeError,subprocess.SubprocessError):time.sleep(2)
        raise RuntimeError('guest SSH deadline; inspect vm-host status and console.log')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--lab',type=Path,default=ROOT/'experiments/gpu-pipeline/lab.json')
    p.add_argument('--state',type=Path,default=ROOT/'build/vfio-vm')
    p.add_argument('--rank',type=int,choices=(0,1))
    p.add_argument('command',choices=['init','prepare-host','prepare','start','wait','status','stop','guest','prepare-guest','copy-to-guest'])
    p.add_argument('guest_command',nargs=argparse.REMAINDER)
    args=p.parse_args();lab=Lab(args.lab,args.state)
    ranks=[args.rank] if args.rank is not None else [0,1]
    if args.command=='init':lab.init();return
    if args.command=='copy-to-guest':
        if args.rank is None or len(args.guest_command)!=2:p.error('copy-to-guest requires --rank before the command, then source and destination')
        sys.exit(subprocess.run(lab.copy_guest_command(args.rank,*args.guest_command)).returncode)
    if args.command=='guest':
        if args.rank is None:p.error('guest requires --rank before the command')
        command=args.guest_command
        if command and command[0]=='--':command=command[1:]
        if not command:p.error('missing guest command')
        sys.exit(subprocess.run(lab.guest_command(args.rank,command)).returncode)
    for rank in ranks:
        if args.command=='prepare':lab.upload(rank)
        if args.command in ('prepare-guest','prepare-host'):
            guest=args.command=='prepare-guest'
            if not guest and lab.host(rank,['hostname']).strip()!=lab.config(rank)['hostname']:
                raise RuntimeError('SSH host identity mismatch')
            log=lab.state/f'{args.command}-{rank}.log'
            print(args.command,rank,'; log:',log,flush=True)
            with log.open('w') as output:
                command=lab.guest_command if guest else lab.host_command
                subprocess.run(command(rank,['sudo','-n','bash','-s']),
                    input=(HERE/('vm-'+args.command+'.sh')).read_text(),text=True,
                    stdout=output,stderr=subprocess.STDOUT,check=True,timeout=1800)
            print('Prepared',rank,'— reboot guest before inference' if guest else '',flush=True)
        else:
            value=lab.wait(rank) if args.command=='wait' else lab.action(rank,args.command)
            vm_host.save(lab.state/f'{args.command}-{rank}.json',value)
            print(json.dumps({k:value[k] for k in ('name','active','drivers','diagnostics') if k in value}),flush=True)


if __name__=='__main__':main()
