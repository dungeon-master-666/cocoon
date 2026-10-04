#!/usr/bin/env python3
"""Native readiness regression with real Cocoon runners, fake TON and a tiny HTTP backend."""
import argparse
import http.server
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT/'pipeline/deployment'))
import services


def load(name,path):
    loader=importlib.machinery.SourceFileLoader(name,str(path))
    spec=importlib.util.spec_from_loader(name,loader)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module
    loader.exec_module(module)
    return module


smoke=load('deployment_smoke',ROOT/'benchmark/smoke-local.py')
launcher=load('deployment_launcher',ROOT/'scripts/cocoon-launch')


class StackStarted(Exception):pass


class StackProcesses(smoke.Processes):
    """Take ownership after the local launcher has started its five components."""
    assembling=True

    def check(self):
        super().check()
        if self.assembling and len(self.children)==5:
            self.assembling=False
            raise StackStarted


class Backend(http.server.BaseHTTPRequestHandler):
    healthy=True

    def do_GET(self):
        body=json.dumps({'data':[{'id':smoke.MODEL}]}).encode()
        self.send_response(200 if self.healthy else 503)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers();self.wfile.write(body)

    def log_message(self,*args):pass


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir',type=Path,default=ROOT/'build/local')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output=args.output.resolve()
    args.output.mkdir(parents=True,exist_ok=False)
    state=args.output/'stack';state.mkdir(mode=0o700)
    marker=args.output/'services';marker.mkdir()
    _,ports,backend_port=smoke.choose_ports()
    server=http.server.ThreadingHTTPServer(('127.0.0.1',backend_port),Backend)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    report={'ports':ports,'cases':[]};pids=[]
    fetch=services.read_json
    def read(port,path):
        return fetch({12000:ports['worker_http'],10000:ports['client_http']}[port],path)
    try:
        with StackProcesses(args.output/'logs') as processes,mock.patch.object(services,'read_json',side_effect=read):
            cfg=SimpleNamespace(local_backend=f'127.0.0.1:{backend_port}',
                                build_dir=str(args.build_dir.resolve()),model=smoke.MODEL)
            try:launcher.start_local_all(cfg,state,ports,processes)
            except StackStarted:pass
            pids.extend(p.pid for _,p in processes.children)
            smoke.wait_ready(processes,lambda:smoke.models_ready(ports['client_http']),60)
            worker=read(12000,'/jsonstats')
            spec={'mode':'external-fake-ton','model':smoke.MODEL,
                  'model_identifier':worker['localconfig']['model'],'max_active_requests':1}
            local={**spec,'mode':'local-fake-ton'}
            def wait_both(expected):
                smoke.wait_ready(processes,lambda:services.available(spec)==expected and
                                 services.available(local)==expected,60)
            def record(name,expected):
                wait_both(expected)
                services.refresh_ready(spec,marker)
                smoke.require(services.is_ready(marker)==expected,name+': stale/incorrect marker')
                current=read(12000,'/jsonstats')
                report['cases'].append({'name':name,'ready':expected,'status':current['status'],
                                        'sc_inited':[p['sc_inited'] for p in current['proxies']]})
                print(name+': PASS',flush=True)
                return current

            record('initial live handshake',True)
            smoke.require(smoke.request(ports['worker_http'],'/request/disable')['status']==200,'disable failed')
            record('disabled worker revokes readiness',False)
            smoke.require(smoke.request(ports['worker_http'],'/request/enable')['status']==200,'enable failed')
            record('enabled worker restores readiness',True)
            Backend.healthy=False
            smoke.wait_ready(processes,lambda:not read(12000,'/jsonstats')['status']['uplink_ok'],10)
            record('backend failure revokes readiness',False)
            Backend.healthy=True
            record('backend recovery restores readiness',True)

            proxy=next(p for name,p in processes.children if name=='proxy')
            proxy.kill();proxy.wait(timeout=5)
            smoke.require(not smoke.group_alive(proxy.pid),'proxy left descendants')
            processes.children.remove(('proxy',proxy))
            smoke.wait_ready(processes,lambda:read(12000,'/jsonstats')['status']['ready_proxy_connections']==0,10)
            lost=record('proxy disconnect revokes readiness',False)
            smoke.require(any(p['sc_inited'] for p in lost['proxies']),
                          'fixture did not reproduce persisted contract after disconnect')
            proxy=processes.start('proxy',proxy.args,cwd=state);pids.append(proxy.pid)
            record('proxy reconnect restores readiness',True)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
    smoke.require(not any(smoke.group_alive(pid) for pid in pids),'process group leaked')
    smoke.check_ports([backend_port,*ports.values()])
    report['cleanup']=True
    (args.output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    print('Readiness regression: PASS; cleanup confirmed',flush=True)


if __name__=='__main__':main()
