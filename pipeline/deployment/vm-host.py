#!/usr/bin/env python3
"""Explicit dev QEMU/KVM + VFIO lifecycle on one Linux host (no confidential computing)."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

BASE=Path('/var/lib/cocoon-pipeline-vm')
OWNER='https://cocoon.org/pipeline/dev-vm/v1'


def run(args,check=True,timeout=120):
    result=subprocess.run([str(a) for a in args],text=True,stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(' '.join(map(str,args[:4]))+': '+result.stderr.strip())
    return result


def virsh(*args,**kwargs):return run(['virsh','-c','qemu:///system',*args],**kwargs)


def digest(config):return hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def save(path,value):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,indent=2)+'\n');tmp.replace(path)


def validate(c):
    required={'schema','mode','name','hostname','gpu','interface','guest_lan','mtu','vcpus','memory_mib',
              'disk_gib','base_image','base_sha256','model_root','artifact_root','ssh_public_key'}
    if set(c)!=required or type(c['schema']) is not int or c['schema']!=1 or c['mode']!='dev':raise ValueError('expected explicit dev VM schema 1')
    if not re.fullmatch(r'cocoon-pipeline-vm-[a-z0-9-]{1,30}',c['name']):raise ValueError('invalid owned VM name')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',c['hostname']):raise ValueError('invalid hostname')
    if not re.fullmatch(r'0000:[0-9a-f]{2}:[0-9a-f]{2}\.0',c['gpu']):raise ValueError('expected GPU function 0 BDF')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}',c['interface']):raise ValueError('invalid LAN interface')
    address=ipaddress.IPv4Interface(c['guest_lan'])
    if not address.ip.is_private or address.ip.is_loopback or address.ip in (address.network.network_address,address.network.broadcast_address):
        raise ValueError('expected private guest LAN address')
    for name,lo,hi in [('vcpus',4,64),('memory_mib',8192,262144),('disk_gib',40,1000),('mtu',1280,9000)]:
        if type(c[name]) is not int or not lo<=c[name]<=hi:raise ValueError('invalid '+name)
    for name in ('base_image','model_root','artifact_root'):
        p=Path(c[name])
        if not p.is_absolute() or '..' in p.parts or str(p)=='/' or '\n' in str(p):raise ValueError('invalid '+name)
    if not re.fullmatch(r'[0-9a-f]{64}',c['base_sha256']):raise ValueError('expected pinned cloud image SHA-256')
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]+)?',c['ssh_public_key']):raise ValueError('expected public SSH key only')
    return c


def mac(c,index):return '52:54:00:'+':'.join(digest(c)[i:i+2] for i in (index*6,index*6+2,index*6+4))


def pci_state(c):
    device=Path('/sys/bus/pci/devices')/c['gpu']
    if not (device/'iommu_group').exists():raise RuntimeError('GPU has no IOMMU group; unsafe passthrough is forbidden')
    if (device/'vendor').read_text().strip()!='0x10de':raise RuntimeError('expected NVIDIA GPU')
    members=sorted(p.name for p in (device/'iommu_group/devices').iterdir())
    expected=[c['gpu'],c['gpu'][:-1]+'1']
    if members!=expected:raise RuntimeError('IOMMU group is not exactly GPU + audio: '+str(members))
    return {bdf:(Path('/sys/bus/pci/devices')/bdf/'driver').resolve().name
            if (Path('/sys/bus/pci/devices')/bdf/'driver').exists() else None for bdf in members}


def domain(c,missing=False):
    # Distinguish absence from libvirt failure; never infer cleanup from a failed query.
    names=virsh('list','--all','--name').stdout.splitlines()
    if c['name'] not in names:
        if missing:return None
        raise RuntimeError('VM is not defined; run prepare')
    xml=ET.fromstring(virsh('dumpxml',c['name']).stdout)
    owner=xml.find('metadata/{'+OWNER+'}owner')
    if owner is None or owner.get('config')!=digest(c):raise RuntimeError('domain belongs to another configuration')
    if xml.findtext('uuid')!=str(uuid.uuid5(uuid.NAMESPACE_URL,OWNER+'/'+digest(c))):raise RuntimeError('domain UUID changed')
    return xml


def stopped(c):return virsh('domstate',c['name']).stdout.strip()=='shut off'


def domain_xml(c,d):
    def sub(parent,tag,text=None,**attrs):
        e=ET.SubElement(parent,tag,attrs)
        if text is not None:e.text=str(text)
        return e
    root=ET.Element('domain',{'type':'kvm'})
    sub(root,'name',c['name']);sub(root,'uuid',str(uuid.uuid5(uuid.NAMESPACE_URL,OWNER+'/'+digest(c))))
    sub(sub(root,'metadata'),'{'+OWNER+'}owner',config=digest(c))
    sub(root,'memory',c['memory_mib'],unit='MiB');sub(root,'vcpu',c['vcpus'],placement='static')
    backing=sub(root,'memoryBacking');sub(backing,'source',type='memfd');sub(backing,'access',mode='shared')
    osnode=sub(root,'os');sub(osnode,'type','hvm',arch='x86_64',machine='q35')
    # Select ordinary OVMF explicitly: firmware autodetection can select an
    # AMD SEV image even for this Intel, non-confidential development guest.
    sub(osnode,'loader','/usr/share/OVMF/OVMF_CODE_4M.fd',readonly='yes',type='pflash')
    sub(osnode,'nvram',str(d/'nvram.fd'),template='/usr/share/OVMF/OVMF_VARS_4M.fd')
    sub(osnode,'boot',dev='hd')
    features=sub(root,'features');sub(features,'acpi');sub(features,'apic')
    sub(root,'cpu',mode='host-passthrough',check='none',migratable='off')
    sub(root,'clock',offset='utc');sub(root,'on_poweroff','destroy');sub(root,'on_reboot','restart');sub(root,'on_crash','destroy')
    devices=sub(root,'devices');sub(devices,'emulator','/usr/bin/qemu-system-x86_64')
    for image,target,fmt,readonly in [('root.qcow2','vda','qcow2',False),('seed.img','vdb','raw',True)]:
        disk=sub(devices,'disk',type='file',device='disk');sub(disk,'driver',name='qemu',type=fmt)
        sub(disk,'source',file=str(d/image));sub(disk,'target',dev=target,bus='virtio')
        if readonly:sub(disk,'readonly')
    net=sub(devices,'interface',type='network');sub(net,'mac',address=mac(c,0))
    sub(net,'source',network='default');sub(net,'model',type='virtio')
    net=sub(devices,'interface',type='direct');sub(net,'mac',address=mac(c,1))
    # macvtap inherits the host interface MTU; libvirt cannot set an MTU on
    # a direct interface. cloud-init sets the matching value in the guest.
    sub(net,'source',dev=c['interface'],mode='bridge');sub(net,'model',type='virtio')
    for bdf in (c['gpu'],c['gpu'][:-1]+'1'):
        hostdev=sub(devices,'hostdev',mode='subsystem',type='pci',managed='yes')
        sub(hostdev,'driver',name='vfio');source=sub(hostdev,'source')
        bus,slot,function=bdf[5:7],bdf[8:10],bdf[-1]
        sub(source,'address',domain='0x0000',bus='0x'+bus,slot='0x'+slot,function='0x'+function)
    for name,key in [('models','model_root'),('artifacts','artifact_root')]:
        fs=sub(devices,'filesystem',type='mount',accessmode='passthrough')
        sub(fs,'driver',type='virtiofs',queue='1024');sub(fs,'source',dir=c[key]);sub(fs,'target',dir=name);sub(fs,'readonly')
    serial=sub(devices,'serial',type='file');sub(serial,'source',path=str(d/'console.log'))
    sub(serial,'target',type='isa-serial',port='0')
    channel=sub(devices,'channel',type='unix');sub(channel,'target',type='virtio',name='org.qemu.guest_agent.0')
    sub(sub(devices,'video'),'model',type='none');sub(devices,'memballoon',model='none')
    return ET.tostring(root,encoding='unicode')


def cloud_config(c):
    user={'hostname':c['name'],'manage_etc_hosts':True,'disable_root':True,'ssh_pwauth':False,
          'users':[{'name':'cocoon','shell':'/bin/bash','sudo':['ALL=(ALL) NOPASSWD:ALL'],
                    'lock_passwd':True,'ssh_authorized_keys':[c['ssh_public_key']]}],
          'mounts':[['models','/srv/cocoon-models','virtiofs','ro,nofail','0','0'],
                    ['artifacts','/srv/cocoon-artifacts','virtiofs','ro,nofail','0','0']]}
    network={'version':2,'ethernets':{
        'mgmt0':{'match':{'macaddress':mac(c,0)},'set-name':'mgmt0','dhcp4':True,'dhcp6':False},
        'lan0':{'match':{'macaddress':mac(c,1)},'set-name':'lan0','addresses':[c['guest_lan']],
                'dhcp4':False,'dhcp6':False,'mtu':c['mtu']}}}
    return user,network


def prepare(c,d):
    if domain(c,missing=True) is not None:
        if not (d/'state.json').exists():raise RuntimeError('missing ownership state')
        if not stopped(c):raise RuntimeError('stop owned VM before preparing its definition')
    if d.exists():
        if not (d/'state.json').exists() or json.loads((d/'state.json').read_text())['config']!=c:
            raise RuntimeError('VM directory already exists without matching owner')
    else:
        d.mkdir(mode=0o755);save(d/'state.json',{'config':c})
    h=hashlib.sha256()
    with Path(c['base_image']).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    if h.hexdigest()!=c['base_sha256']:raise RuntimeError('cloud image checksum mismatch')
    for key in ('model_root','artifact_root'):
        if not Path(c[key]).is_dir():raise RuntimeError(key+' directory missing')
    if not (d/'root.qcow2').exists():
        run(['qemu-img','create','-f','qcow2','-F','qcow2','-b',c['base_image'],d/'root.qcow2',str(c['disk_gib'])+'G'])
    user,network=cloud_config(c)
    (d/'user-data').write_text('#cloud-config\n'+json.dumps(user))
    (d/'network-config').write_text(json.dumps(network))
    (d/'meta-data').write_text(json.dumps({'instance-id':digest(c),'local-hostname':c['name']}))
    if not (d/'seed.img').exists():
        run(['cloud-localds','--network-config='+str(d/'network-config'),d/'seed.img',d/'user-data',d/'meta-data'])
    (d/'domain.xml').write_text(domain_xml(c,d))
    virsh('define',d/'domain.xml')
    domain(c)


def restore_services(c,d):
    state=json.loads((d/'state.json').read_text())
    if 'drivers' not in state:return
    actual=pci_state(c)
    if actual!=state['drivers']:raise RuntimeError('GPU drivers were not restored: '+str(actual))
    if state.get('persistenced'):run(['systemctl','start','nvidia-persistenced.service'])
    state['released']=True;save(d/'state.json',state)


def gpu_users():
    # Monitoring tools also hold NVIDIA references and can make PCI unbind
    # block indefinitely even when nvidia-smi reports no compute processes.
    devices=sorted(p for p in Path('/dev').glob('nvidia*') if p.is_char_device())
    if not devices:raise RuntimeError('NVIDIA device nodes unavailable')
    result=run(['fuser',*devices],check=False,timeout=10)
    if result.returncode not in (0,1):raise RuntimeError('cannot check NVIDIA device users: '+result.stderr.strip())
    if result.returncode==1 and result.stderr.strip():raise RuntimeError('cannot check NVIDIA device users: '+result.stderr.strip())
    pids=result.stdout.split()
    if any(not pid.isdigit() for pid in pids):raise RuntimeError('unexpected fuser output')
    if result.returncode==0 and not pids:raise RuntimeError('cannot identify NVIDIA device users')
    return sorted(set(map(int,pids)))


def start(c,d):
    domain(c)
    if not stopped(c):
        if virsh('domstate',c['name']).stdout.strip()!='running' or set(pci_state(c).values())!={'vfio-pci'}:
            raise RuntimeError('existing VM is not running with its complete VFIO group; inspect status')
        return
    if not Path('/dev/kvm').exists():raise RuntimeError('KVM unavailable')
    if c['vcpus']>os.cpu_count():raise RuntimeError('not enough CPUs')
    available=int(next(l.split()[1] for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:')))//1024
    if available<c['memory_mib']+2048:raise RuntimeError('insufficient available host RAM')
    drivers=pci_state(c)
    if drivers[c['gpu']]!='nvidia':raise RuntimeError('GPU must be on its host NVIDIA driver before start')
    busy=run(['nvidia-smi','-i',c['gpu'],'--query-compute-apps=pid','--format=csv,noheader,nounits']).stdout.strip()
    if busy:raise RuntimeError('GPU has active compute processes; stop the owning workload first')
    links=json.loads(run(['ip','-j','addr','show','dev',c['interface']]).stdout)
    addr=ipaddress.IPv4Interface(c['guest_lan'])
    if 'UP' not in links[0]['flags'] or not any(a['family']=='inet' and ipaddress.IPv4Address(a['local']) in addr.network for a in links[0]['addr_info']):
        raise RuntimeError('guest LAN does not match active host interface')
    if c['mtu']>links[0]['mtu']:raise RuntimeError('guest MTU exceeds host LAN MTU')
    if any(a.get('local')==str(addr.ip) for a in links[0]['addr_info']):raise RuntimeError('guest cannot use host IP')
    run(['arping','-D','-I',c['interface'],'-c','2','-w','3',str(addr.ip)],timeout=10)
    state=json.loads((d/'state.json').read_text())
    if not state.get('released',True):raise RuntimeError('previous driver/service restoration unconfirmed; run stop')
    state.update(drivers=drivers,persistenced=run(['systemctl','is-active','--quiet','nvidia-persistenced.service'],check=False).returncode==0,released=False)
    save(d/'state.json',state)
    try:
        if state['persistenced']:run(['systemctl','stop','nvidia-persistenced.service'])
        users=gpu_users()
        if users:raise RuntimeError('NVIDIA devices remain open; stop their owners first: '+str(users))
        virsh('start',c['name'])
        if set(pci_state(c).values())!={'vfio-pci'}:raise RuntimeError('libvirt did not bind entire GPU group to VFIO')
    except subprocess.TimeoutExpired as e:
        # A timed-out virsh client does not cancel libvirt's device operation.
        # Keep the ownership record until an explicit stop confirms recovery.
        raise RuntimeError('libvirt operation timed out; VM/GPU state is uncertain, inspect before retrying') from e
    except BaseException:
        if stopped(c):restore_services(c,d)
        raise


def stop(c,d):
    if domain(c,missing=True) is None:
        if (d/'state.json').exists():restore_services(c,d)
        return
    if not stopped(c):
        virsh('shutdown',c['name'])
        deadline=time.monotonic()+90
        while not stopped(c) and time.monotonic()<deadline:time.sleep(.5)
        if not stopped(c):virsh('destroy',c['name'])
    if not stopped(c):raise RuntimeError('VM remains active')
    restore_services(c,d)


def status(c,d):
    exists=domain(c,missing=True) is not None
    state=virsh('domstate',c['name']).stdout.strip() if exists else 'undefined'
    active=exists and state!='shut off'
    value={'name':c['name'],'mode':'dev','hardware_attested':False,'defined':exists,'active':active,
           'state':state,'drivers':pci_state(c),'diagnostics':str(d)}
    if (d/'state.json').exists():value['ownership']=json.loads((d/'state.json').read_text())
    if active:
        value['addresses']=virsh('domifaddr',c['name'],'--source','lease').stdout
        value['domain']=virsh('dumpxml',c['name']).stdout
    return value


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['prepare','start','status','stop'])
    p.add_argument('--config',required=True,type=Path);args=p.parse_args()
    c=validate(json.loads(args.config.read_text()))
    if os.geteuid()!=0 or socket.gethostname()!=c['hostname']:raise RuntimeError('run as root on the configured host')
    BASE.mkdir(parents=True,exist_ok=True)
    with (BASE/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        d=BASE/c['name']
        if args.command!='status':globals()[args.command](c,d)
        print(json.dumps(status(c,d),indent=2))


if __name__=='__main__':main()
