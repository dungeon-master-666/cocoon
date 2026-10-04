#!/usr/bin/env python3
"""External dev services acceptance, called by the Linux deployment suite."""
import copy
import ipaddress
import json
from pathlib import Path
import os
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT/'pipeline/deployment'))
import bundle
import host


def run(args,check=True,timeout=160):
    value=subprocess.run([str(a) for a in args],capture_output=True,text=True,timeout=timeout)
    if check and value.returncode:raise AssertionError(value.stdout+value.stderr)
    return value


def check(config,artifact,service_ip,output):
    config=copy.deepcopy(config);bundle.ipv4(service_ip)
    network=ipaddress.IPv4Interface(config['ranks'][0]['underlay']).network
    assert ipaddress.IPv4Address(service_ip) in network
    assert service_ip not in [str(ipaddress.IPv4Interface(n['underlay']).ip) for n in config['ranks']]
    interface=config['hosts'][0]['interface']
    run(['arping','-D','-q','-I',interface,'-c','2','-w','3',service_ip])
    config['services']={'mode':'external-fake-ton','coefficient':1000,'proxy_ip':service_ip,
        'proxy_worker_port':11001,'proxy_client_port':11002,'key_manager_ip':service_ip,'key_manager_port':13001}
    output.mkdir(parents=True)
    generated=bundle.generate(config,artifact,output/'bundles');folder=Path(generated['bundles'][0])
    b=host.verify(folder);cli=folder/'pipeline-deploy'
    name='cocoon-egress-'+b['deployment_id'][:20];link='ce'+b['deployment_id'][:10]
    services=output/'services';services.mkdir(mode=0o700);os.chown(services,10001,10001)
    spec=bundle.runtime(b['config'],0,b['models'])[1]
    spec.update(mode='local-fake-ton',advertise_ip=service_ip)
    (services/'fixture.json').write_text(json.dumps(spec));(services/'fixture.json').chmod(0o644)
    result={'image_id':artifact['image_id'],'deployment_id':b['deployment_id'],'service_ip':service_ip}
    fixture_started=False
    def execute(code,container=name,underlay=False,user=None):
        argv=['docker','exec']
        if user:argv+=['--user',str(user)]
        argv+=[container]
        if underlay:argv+=['nsenter','--net=/run/netns/underlay']
        return run(argv+['/usr/bin/python3','-c',code])
    try:
        run(['docker','run','-d','--name',name,'--label','org.cocoon.pipeline.test='+b['deployment_id'],
             '--network','none','--read-only','--init','--cap-drop','ALL','--user','10001:10001',
             '--security-opt','no-new-privileges','--memory','1g','--pids-limit','256',
             '--tmpfs','/tmp:rw,nosuid,nodev,mode=1777','--mount','type=bind,src='+str(services.resolve())+',dst=/services',
             '--entrypoint','/bin/sleep',artifact['image_id'],'infinity'])
        fixture_started=True
        pid=json.loads(run(['docker','inspect',name]).stdout)[0]['State']['Pid']
        run(['ip','link','add','link',interface,'name',link,'alias',b['deployment_id'],'type','ipvlan','mode','l2'])
        run(['ip','link','set',link,'netns',str(pid)])
        for args in (['link','set','lo','up'],['addr','add',service_ip+'/'+str(network.prefixlen),'dev',link],['link','set',link,'up']):
            run(['nsenter','-t',str(pid),'-n','ip',*args])
        # This fixture has no worker; the one from the bundle must register.
        run(['docker','exec','-d',name,'/usr/bin/python3','-I','/opt/cocoon/pipeline/deployment/services.py',
             '--external-only','--config','/services/fixture.json','--state','/services'])
        run(['docker','exec','-d',name,'/usr/bin/python3','-c',
             'import socketserver;socketserver.TCPServer(("0.0.0.0",15000),socketserver.BaseRequestHandler).serve_forever()'])
        run([cli,'start']);run([cli,'wait'])
        # The denied port is genuinely open in the destination, so a failed
        # connection below proves policy instead of just an absent listener.
        execute('import socket;s=socket.create_connection(("127.0.0.1",15000),2);s.close()')
        for uid,port,allowed in ((10001,11001,True),(10001,13001,True),(10001,11002,False),(10001,15000,False),(0,11001,False)):
            code=('import os,socket;os.setgroups([]);os.setgid('+str(uid)+');os.setuid('+str(uid)+');ok=False\n'
                  'try:\n s=socket.create_connection(('+repr(service_ip)+','+str(port)+'),1);s.close();ok=True\n'
                  'except OSError: pass\nassert ok=='+str(allowed))
            execute(code,host.name(b,0),underlay=True)
        for rank in (0,1):
            status=json.loads((host.state(b,rank)/'agent-status.json').read_text())
            ns=status['group']['network']['namespace']
            run(['docker','exec',host.name(b,rank),'nsenter','--net=/run/netns/'+ns,'/usr/bin/python3','-c',
                 'import os,socket;os.setgroups([]);os.setgid(65534);os.setuid(65534)\n'
                 'try: socket.create_connection(('+repr(service_ip)+',11001),1)\n'
                 'except OSError: pass\nelse: raise AssertionError("engine reached underlay service")'])
        body=json.dumps({'model':spec['model'],'messages':[{'role':'user','content':'Say hello.'}],'max_tokens':4})
        answer=execute('import urllib.request,json;print(urllib.request.urlopen(urllib.request.Request('
            '"http://127.0.0.1:10000/v1/chat/completions",data='+repr(body.encode())+',headers={"Content-Type":"application/json"}),timeout=60).read().decode())')
        value=json.loads(answer.stdout);assert value.get('choices') and value.get('usage')
        encrypted=execute('import base64,json,os,pathlib,subprocess,time,urllib.request\n'
            'public=base64.b64decode("+2fQ/NM48g4NSVfZ6CrcEB0uNROkSKOrRgUu4biMWBg=").hex()\n'
            'end=time.monotonic()+45\n'
            'while True:\n'
            ' page=urllib.request.urlopen("http://127.0.0.1:11000/stats",timeout=2).read().decode()\n'
            ' if "<h1>KNOWN PRIVATE KEYS</h1>" in page and public.upper() in page.split("<h1>KNOWN PRIVATE KEYS</h1>",1)[1].split("</table>",1)[0]:break\n'
            ' assert time.monotonic()<end,"external key-manager readiness deadline"\n'
            ' time.sleep(.2)\n'
            'key=pathlib.Path("/services/request-key.bin");key.write_bytes(os.urandom(32));key.chmod(0o600)\n'
            'args=["/opt/cocoon/bin/encrypt-message","-k",str(key),"-p",public]\n'
            'payload=subprocess.run(args,input='+repr(body)+',text=True,capture_output=True,check=True).stdout\n'
            'response=urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:10000/v1/chat/completions",data=payload.encode(),headers={"Content-Type":"application/json"}),timeout=60).read().decode()\n'
            'assert json.loads(response)["is_encrypted"]=="v1"\n'
            'decoded=subprocess.run(args+["-d"],input=response,text=True,capture_output=True,check=True)\n'
            'assert "failed to decrypt" not in decoded.stderr\n'
            'value=json.loads(decoded.stdout);assert value["choices"] and value["usage"];print(json.dumps(value["usage"]))\n')
        result.update(ok=True,usage=value['usage'],encrypted_usage=json.loads(encrypted.stdout))
    except BaseException as exc:
        result.update(ok=False,error=str(exc));raise
    finally:
        stopped=run([cli,'stop'],check=False)
        inspected=run(['docker','inspect',name],check=False)
        if fixture_started:assert inspected.returncode==0 and json.loads(inspected.stdout)[0]['State']['Running'],'group cleanup touched external fixture'
        if inspected.returncode==0:
            obj=json.loads(inspected.stdout)[0]
            assert obj['Config']['Labels'].get('org.cocoon.pipeline.test')==b['deployment_id']
            run(['docker','rm','-f',obj['Id']])
        leftover=run(['ip','-j','link','show','dev',link],check=False)
        if leftover.returncode==0:
            assert json.loads(leftover.stdout)[0].get('ifalias')==b['deployment_id']
            run(['ip','link','del',link])
        result['cleanup']=stopped.returncode==0
        (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        assert result['cleanup'],stopped.stderr
