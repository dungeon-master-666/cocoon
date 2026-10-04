#!/usr/bin/env python3
"""Bundle validation: run without Docker/GPU or root."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT/'pipeline/deployment'))
import bundle
import entry
import host
import services
spec=importlib.util.spec_from_file_location('builder',ROOT/'pipeline/deployment/build-image.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


class Bundles(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output=Path(self.temp.name)/'bundles'
        self.models=json.loads((ROOT/'pipeline/sglang-models.json').read_text())
        self.artifact={'schema':1,'mode':'dev','backend':'simulator','architecture':'arm64',
                       'image_id':'sha256:'+'1'*64,'model_catalog_sha256':bundle.digest(self.models)}
        self.config={'schema':1,'mode':'dev','backend':'simulator','model':'simulator',
            'hosts':[{'id':'local','hostname':'linux','interface':'eth0'}],
            'ranks':[{'host':'local','underlay':'192.168.1.11/24','cpus':'0-1','memory_mib':512},
                     {'host':'local','underlay':'192.168.1.12/24','cpus':'2-3','memory_mib':512}]}

    def generate(self):
        return bundle.generate(self.config,self.artifact,self.output,self.models)

    def test_roundtrip_and_single_worker(self):
        self.generate();value=host.verify(self.output/'local')
        self.assertEqual(value['ranks'],[0,1])
        worker=json.loads((self.output/'local/services.json').read_text())
        self.assertEqual(worker['max_active_requests'],1)
        self.assertEqual(worker['coefficient'],1000)
        self.assertEqual(worker['forward_requests_to'],'127.0.0.1:18080')

    def test_source_snapshot_invalidates_older_changed_bytes(self):
        root=Path(self.temp.name)/'source';dest=Path(self.temp.name)/'snapshot'
        cmake=root/'vendor/lz4/build/cmake/CMakeLists.txt';cmake.parent.mkdir(parents=True)
        cmake.write_text('version one')
        (root/'build').mkdir();(root/'build/unwanted.o').write_text('object')
        builder.copy_source(root,dest)
        copied=dest/'vendor/lz4/build/cmake/CMakeLists.txt';stamp=copied.stat().st_mtime_ns
        self.assertEqual(copied.read_text(),'version one');self.assertFalse((dest/'build').exists())
        builder.copy_source(root,dest);self.assertEqual(copied.stat().st_mtime_ns,stamp)
        cmake.write_text('version two');os.utime(cmake,(1,1))
        builder.copy_source(root,dest)
        self.assertEqual(copied.read_text(),'version two');self.assertGreater(copied.stat().st_mtime_ns,stamp)
        cmake.unlink();builder.copy_source(root,dest);self.assertFalse(copied.exists())

    def test_image_catalogue_describes_built_snapshot_when_checkout_changes(self):
        root=Path(self.temp.name)/'checkout';catalogue=root/'pipeline/sglang-models.json'
        catalogue.parent.mkdir(parents=True);catalogue.write_text(json.dumps(self.models))
        output=Path(self.temp.name)/'image';cache=Path(self.temp.name)/'cache'
        changed={**self.models,'concurrent_edit':True}
        def docker(args,capture=False):
            if args[:2]==['docker','build']:
                Path(args[args.index('--iidfile')+1]).write_text('sha256:'+'1'*64)
            elif args[:2]==['docker','run']:
                runtime=output/'runtime/pipeline';runtime.mkdir(parents=True)
                (runtime/'sglang-models.json').write_bytes((output/'source/pipeline/sglang-models.json').read_bytes())
                catalogue.write_text(json.dumps(changed))
            elif args[:3]==['docker','image','inspect']:
                return json.dumps([{'Architecture':'amd64'}])
            else:self.fail('unexpected Docker command: '+str(args))
        argv=['build-image.py','--backend','simulator','--base','ubuntu@sha256:'+'2'*64,
              '--output',str(output),'--build-dir',str(cache)]
        with mock.patch.object(builder,'ROOT',root),mock.patch.object(builder,'run',side_effect=docker), \
                mock.patch.object(sys,'argv',argv),mock.patch('sys.stdout',new_callable=io.StringIO):
            builder.main()
        actual=json.loads((output/'artifact.json').read_text())
        self.assertEqual(actual['model_catalog_sha256'],bundle.digest(self.models))
        self.assertNotEqual(actual['model_catalog_sha256'],bundle.digest(changed))
        self.assertIn('org.cocoon.pipeline.catalog="'+bundle.digest(self.models)+'"',
                      (output/'Dockerfile.runtime').read_text())
        payload=json.loads((output/'runtime-files.sha256.json').read_text())
        self.assertEqual(payload['pipeline/sglang-models.json'],hashlib.sha256(
            (output/'runtime/pipeline/sglang-models.json').read_bytes()).hexdigest())

    def test_rejects_resource_collisions(self):
        for field,value in [('cpus','1-3'),('underlay','192.168.1.11/24')]:
            config=copy.deepcopy(self.config);config['ranks'][1][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):bundle.validate(config,self.artifact,self.models)

    def test_profiles_and_artifacts(self):
        for field,value in [('mode','production'),('backend','vllm'),('model','large'),('schema',True)]:
            config=copy.deepcopy(self.config);config[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):bundle.validate(config,self.artifact,self.models)
        artifact={**self.artifact,'model_catalog_sha256':'0'*64}
        with self.assertRaises(ValueError):bundle.validate(self.config,artifact,self.models)

    def test_invalid_endpoints(self):
        for ip in ('127.0.0.1/8','224.0.0.1/24','10.231.0.4/24','192.168.1.0/24','192.168.1.255/24'):
            config=copy.deepcopy(self.config);config['ranks'][0]['underlay']=ip
            with self.subTest(ip=ip),self.assertRaises(ValueError):bundle.validate(config,self.artifact,self.models)

    def test_external_policy_is_head_only(self):
        self.config['services']={'mode':'external-fake-ton','coefficient':777,'proxy_ip':'192.168.1.20',
            'proxy_worker_port':11001,'proxy_client_port':11002,'key_manager_ip':'192.168.1.21','key_manager_port':13001}
        config=bundle.validate(self.config,self.artifact,self.models)
        head,worker=bundle.runtime(config,0,self.models);member,empty=bundle.runtime(config,1,self.models)
        self.assertEqual(head['network']['service_egress'],[{'ip':'192.168.1.20','port':11001},{'ip':'192.168.1.21','port':13001}])
        self.assertNotIn('service_egress',member['network']);self.assertIsNone(empty)
        self.assertEqual(worker['coefficient'],777)

    def test_rehashed_bad_worker_is_rejected(self):
        self.generate();d=self.output/'local';original=json.loads((d/'services.json').read_text())
        for field,value in [('model','other'),('max_active_requests',99),('forward_requests_to','127.0.0.1:8000'),('coefficient',2000)]:
            (d/'services.json').write_text(json.dumps({**original,field:value}))
            hashes=json.loads((d/'files.sha256.json').read_text())
            hashes['services.json']=hashlib.sha256((d/'services.json').read_bytes()).hexdigest()
            (d/'files.sha256.json').write_text(json.dumps(hashes))
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'worker'):host.verify(d)

    def test_bundle_tampering(self):
        self.generate();d=self.output/'local';(d/'agent-1.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'files changed'):host.verify(d)

    def test_gpu_assignment(self):
        self.config.update(backend='vllm',model='large');self.artifact['backend']='vllm'
        for n in self.config['ranks']:n.update(gpu='0000:01:00.0',model_root='/models')
        with self.assertRaisesRegex(ValueError,'GPU assigned'):bundle.validate(self.config,self.artifact,self.models)
        self.config['ranks'][1]['gpu']='0000:02:00.0'
        config=bundle.validate(self.config,self.artifact,self.models)
        self.assertEqual(bundle.runtime(config,0,self.models)[1]['model_identifier'],
            'Qwen/Qwen3-14B@'+self.models['large']['model']['revision'])

    def test_rendered_worker_uses_authoritative_capacity_and_price(self):
        config=bundle.validate(self.config,self.artifact,self.models)
        _,worker=bundle.runtime(config,0,self.models)
        old=services.ROOT;services.ROOT=ROOT
        try:services.render(worker,Path(self.temp.name))
        finally:services.ROOT=old
        actual=json.loads((Path(self.temp.name)/'worker-config.json').read_text())
        self.assertEqual(actual['model_name'],worker['model_identifier'])
        self.assertEqual(actual['max_active_requests'],1)
        self.assertEqual(actual['coefficient'],1000)


class Readiness(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)
        self.spec={'mode':'external-fake-ton','model':'test','model_identifier':'test@revision',
                   'max_active_requests':1}
        self.worker={'status':{'enabled':True,'uplink_ok':True,'ready_proxy_connections':1},
                     'localconfig':{'model':'test@revision'},'proxies':[{'sc_inited':True}]}
        self.models={'data':[{'id':'test@revision','workers':[{'coefficient':1000}]}]}

    def test_external_requires_live_handshake_enabled_uplink_and_matching_model(self):
        with mock.patch.object(services,'read_json',return_value=self.worker):
            self.assertTrue(services.available(self.spec))
            for field,value in [('enabled',False),('uplink_ok',False),('ready_proxy_connections',0),
                                ('ready_proxy_connections',True),('ready_proxy_connections','1')]:
                with self.subTest(field=field,value=value),mock.patch.dict(self.worker['status'],{field:value}):
                    self.assertFalse(services.available(self.spec))
            with mock.patch.dict(self.worker['status'],{'enabled':True,'uplink_ok':True},clear=True):
                self.assertFalse(services.available(self.spec))
            self.worker['localconfig']['model']='other@revision'
            self.assertFalse(services.available(self.spec))

    def test_local_requires_worker_and_model_advertisement(self):
        self.spec['mode']='local-fake-ton'
        def read(port,path):
            return self.worker if port==12000 else self.models
        with mock.patch.object(services,'read_json',side_effect=read) as fetch:
            self.assertTrue(services.available(self.spec))
            fetch.assert_has_calls([mock.call(12000,'/jsonstats'),mock.call(10000,'/v1/models')])
            self.worker['status']['ready_proxy_connections']=0
            self.assertFalse(services.available(self.spec))
            self.worker['status']['ready_proxy_connections']=1
            for model in ({'id':'test','workers':[]},{'id':'other','workers':[{}]},
                          {'id':'test','workers':True},{'id':'test','workers':1}):
                with self.subTest(model=model):
                    self.models['data']=[model];self.assertFalse(services.available(self.spec))

    def test_external_fixture_checks_advertisement_without_local_worker(self):
        self.spec['mode']='local-fake-ton'
        with mock.patch.object(services,'read_json',return_value=self.models) as fetch:
            self.assertTrue(services.available(self.spec,external_only=True))
            fetch.assert_called_once_with(10000,'/v1/models')

    def test_marker_expires_and_requires_probe_started_in_current_epoch(self):
        with mock.patch.object(services,'read_json',return_value=self.worker), \
                mock.patch.object(services.time,'monotonic',return_value=100) as clock:
            services.refresh_ready(self.spec,self.state)
            self.assertTrue(services.is_ready(self.state,checked_after=100))
            self.assertFalse(services.is_ready(self.state,checked_after=100.1))
            clock.return_value=100+services.READINESS_TTL+0.1
            self.assertFalse(services.is_ready(self.state))
            # Completion of a probe begun before the epoch transition is insufficient.
            def slow_read(*args):
                clock.return_value=201
                return self.worker
            clock.return_value=199
            with mock.patch.object(services,'read_json',side_effect=slow_read):
                services.refresh_ready(self.spec,self.state)
            self.assertFalse(services.is_ready(self.state,checked_after=200))
            services.refresh_ready(self.spec,self.state)
            self.assertTrue(services.is_ready(self.state,checked_after=200))

    def test_bad_marker_and_failed_probe_fail_closed(self):
        for value in ('{}','[]','broken','{"checked_at":true}','{"checked_at":1e300}'):
            with self.subTest(value=value):
                (self.state/'ready.json').write_text(value)
                self.assertFalse(services.is_ready(self.state))
        for value in (OSError('connection lost'),ValueError('bad JSON'),{},[]):
            with self.subTest(value=value):
                with mock.patch.object(services,'read_json',return_value=self.worker):
                    services.refresh_ready(self.spec,self.state)
                self.assertTrue(services.is_ready(self.state))
                kwargs={'side_effect':value} if isinstance(value,Exception) else {'return_value':value}
                with mock.patch.object(services,'read_json',**kwargs):
                    services.refresh_ready(self.spec,self.state)
                self.assertFalse((self.state/'ready.json').exists())

    def test_service_loop_revokes_and_restores_ready_after_startup(self):
        stopped=False;handlers={};observed=[]
        process=mock.Mock(pid=987654)
        process.poll.side_effect=lambda:0 if stopped else None
        def tick(*args):
            nonlocal stopped
            observed.append(services.is_ready(self.state))
            self.worker['status']['ready_proxy_connections']=0 if len(observed)==1 else 1
            if len(observed)==3:
                stopped=True;handlers[signal.SIGTERM]()
        with mock.patch.object(services.os,'geteuid',return_value=10001), \
                mock.patch.object(services.os,'umask'),mock.patch.object(services.os,'killpg'), \
                mock.patch.object(services.signal,'signal',side_effect=lambda sig,fn:handlers.update({sig:fn})), \
                mock.patch.object(services,'render',return_value=['worker']), \
                mock.patch.object(services.subprocess,'Popen',return_value=process), \
                mock.patch.object(services,'read_json',return_value=self.worker) as fetch, \
                mock.patch.object(services.time,'sleep',side_effect=tick):
            services.run(self.spec,self.state)
        self.assertEqual(observed,[True,False,True])
        self.assertEqual(fetch.call_count,3)
        self.assertFalse((self.state/'ready.json').exists())


class SupervisorReadiness(unittest.TestCase):
    def exercise(self,tick,startup_timeout=1):
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp);runtime=state/'runtime';marker=state/'services'
            (state/'network-ready').touch();(state/'agent.json').write_text('{}')
            now=[100.0];stopped=False;handlers={};observed=[]
            agent_state={'group_ready':True,'epoch':1}
            status_path=runtime/'agent/status.json'
            def terminate():
                nonlocal stopped
                stopped=True
            def start(*args,**kwargs):
                status_path.parent.mkdir(parents=True,exist_ok=True)
                status_path.write_text(json.dumps(agent_state))
                proc=mock.Mock(pid=987654)
                proc.poll.side_effect=lambda:0 if stopped else None
                proc.terminate.side_effect=terminate
                return proc
            def save(path,value):
                entry_save(path,value)
                if path.name=='supervisor.json' and 'mode' in value:observed.append(value['state'])
            def sleep(*args):
                nonlocal stopped
                if tick(len(observed),now,agent_state,marker):
                    stopped=True;handlers[signal.SIGTERM]()
                status_path.write_text(json.dumps(agent_state))
            entry_save=entry.save
            with mock.patch.object(entry,'STATE',state),mock.patch.object(entry,'command'), \
                    mock.patch.object(entry,'firewall'),mock.patch.object(entry.os,'chown'), \
                    mock.patch.object(entry,'Path',side_effect=lambda p:runtime if p=='/run/pipeline' else Path(p)), \
                    mock.patch.object(entry,'load_services',return_value=services), \
                    mock.patch.object(entry.signal,'signal',side_effect=lambda sig,fn:handlers.update({sig:fn})), \
                    mock.patch.object(entry.subprocess,'Popen',side_effect=start), \
                    mock.patch.object(entry.time,'monotonic',side_effect=lambda:now[0]), \
                    mock.patch.object(entry.time,'sleep',side_effect=sleep),mock.patch.object(entry,'save',side_effect=save):
                entry.run({'node':{'underlay':'192.168.1.11/24'},'startup_timeout':startup_timeout},
                          state/'agent.json',state/'services.json')
            return observed

    def test_expiry_epoch_and_recovery_after_original_startup_deadline(self):
        def tick(step,now,agent_state,marker):
            now[0]+=0.2
            if step in (1,3,6,8):
                (marker/'ready.json').write_text(json.dumps({'checked_at':now[0]}))
            elif step==2:now[0]+=services.READINESS_TTL+0.1
            elif step==4:agent_state['group_ready']=False
            elif step==5:agent_state.update(group_ready=True,epoch=2)
            elif step==7:(marker/'ready.json').unlink()
            return step==9
        self.assertEqual(self.exercise(tick),['STARTING','READY','STARTING','READY','STARTING',
                                             'STARTING','READY','STARTING','READY'])

    def test_services_never_ready_still_hit_startup_deadline(self):
        def tick(step,now,agent_state,marker):
            now[0]+=2
            return False
        with self.assertRaisesRegex(RuntimeError,'startup deadline'):
            self.exercise(tick)


if __name__=='__main__':unittest.main()
