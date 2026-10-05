#!/usr/bin/env python3
"""Run the deployment GPU acceptance inside two already prepared QEMU/VFIO VMs.

The shared suite stops its containers; it deliberately leaves VM lifecycle to
vm.py, so guest diagnostics remain accessible. --remote-dir must be guest-writable.
"""
import argparse
import importlib.util
import ipaddress
import json
from pathlib import Path
import shlex
import sys

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'pipeline/deployment'))
import vm

spec=importlib.util.spec_from_file_location('acceptance',ROOT/'test/test-pipeline-deployment-gpu.py')
acceptance=importlib.util.module_from_spec(spec);spec.loader.exec_module(acceptance)


class VMLab(acceptance.Lab):
    def __init__(self,config,remote,state):
        super().__init__(config,remote)
        self.vms=vm.Lab(config,state)
        # VM placement is fixed for this container lifecycle suite. Resolve
        # management leases once; container restarts do not reboot the guests.
        self.guest_commands=[self.vms.guest_command(r,[]) for r in (0,1)]
        self.config={**config,'underlay_interface':'lan0','model_root':'/srv/cocoon-models'}
        self.hosts=[]
        evidence=[]
        for rank in (0,1):
            c=self.vms.config(rank);status=self.vms.action(rank,'status')
            acceptance.require(status['active'] and set(status['drivers'].values())=={'vfio-pci'},'physical GPU is not owned by VFIO')
            acceptance.require(self.ssh(rank,['systemd-detect-virt']).stdout.strip()=='kvm','runtime target is not KVM')
            acceptance.require(self.ssh(rank,['hostname']).stdout.strip()==c['name'],'wrong guest identity')
            self.hosts.append({'hostname':c['name'],'lan':str(ipaddress.IPv4Interface(c['guest_lan']).ip)})
            evidence.append({'physical_host':c['hostname'],'guest':c['name'],'host_status':status,
                'gpu':self.ssh(rank,['nvidia-smi','--query-gpu=name,uuid,pci.bus_id,driver_version','--format=csv,noheader']).stdout,
                'mounts':self.ssh(rank,['findmnt','-t','virtiofs','-J']).stdout})
        vm.vm_host.save(self.vms.state/'gpu-vm-evidence.json',evidence)

    def ssh(self,rank,args,check=True,timeout=180):
        return self.run([*self.guest_commands[rank][:-1],shlex.join(map(str,args))],check,timeout)

    def upload(self,rank,source,destination):
        return self.run(self.vms.copy_guest_command(rank,source,destination),timeout=1800)

    def download(self,rank,source,destination):
        return self.run(self.vms.copy_guest_command(rank,source,destination,download=True),timeout=1800)


if __name__=='__main__':
    p=argparse.ArgumentParser(add_help=False)
    p.add_argument('--vm-state',type=Path,default=ROOT/'build/vfio-vm')
    options,remaining=p.parse_known_args()
    sys.argv=[sys.argv[0],*remaining]
    acceptance.main(lambda config,remote:VMLab(config,remote,options.vm_state))
