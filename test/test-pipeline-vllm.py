#!/usr/bin/env python3
"""Step 11: shared HTTP contract plus vLLM-specific cancellation/launch checks."""
import argparse
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sg = load('shared_contract', ROOT/'test/test-pipeline-sglang.py')
vh = load('vllm_helper', ROOT/'pipeline/vllm-helper.py')
control = load('vllm_control', ROOT/'pipeline/vllm_control.py')
sg.helper = vh.engine
sg.helper.Helper = vh.Helper


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = SimpleNamespace(abort=AsyncMock())
        self.app_state = SimpleNamespace(state=SimpleNamespace(engine_client=self.engine))
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.submissions = []
        async def application(scope, receive, send):
            self.submissions.append(scope)
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped.set()
        self.middleware = control.CancellationMiddleware(application)

    def scope(self, rid, path='/v1/chat/completions'):
        return {'type':'http', 'method':'POST', 'path':path,
                'headers':[(b'x-request-id',rid.encode())], 'app':self.app_state}

    async def abort(self, rid):
        receive = AsyncMock(return_value={'type':'http.request','body':json.dumps({'rid':rid}).encode()})
        send = AsyncMock()
        await self.middleware(self.scope(rid, '/pipeline/abort'), receive, send)
        return send.call_args_list[0].args[0]['status']

    async def test_cancel_registered_task_and_exact_engine_ids(self):
        rid = 'a'*32
        task = asyncio.create_task(self.middleware(self.scope(rid), AsyncMock(), AsyncMock()))
        await self.started.wait()
        self.assertEqual(await self.abort(rid), 200)
        self.assertTrue(self.stopped.is_set())
        self.assertFalse(self.middleware.active)
        self.engine.abort.assert_awaited_once_with(['chatcmpl-'+rid, 'cmpl-'+rid+'-0'])
        await asyncio.gather(task, return_exceptions=True)

    async def test_cancel_before_registration_prevents_late_inference(self):
        rid = 'b'*32
        self.assertEqual(await self.abort(rid), 200)
        send = AsyncMock()
        await self.middleware(self.scope(rid), AsyncMock(), send)
        self.assertEqual(send.call_args_list[0].args[0]['status'],409)
        self.assertFalse(self.submissions)

    async def test_failed_engine_abort_is_not_acknowledged(self):
        self.engine.abort.side_effect = RuntimeError('engine dead')
        self.assertEqual(await self.abort('c'*32),503)

    async def test_tombstones_are_bounded_and_invalid_ids_rejected(self):
        self.assertEqual(await self.abort('arbitrary'),400)
        self.middleware.cancelled = {str(i):float('inf') for i in range(1024)}
        self.assertEqual(await self.abort('d'*32),503)
        self.engine.abort.assert_not_called()

    async def test_member_health_needs_no_http_server(self):
        helper = vh.Helper({'rank':1}, lambda:True)
        helper.control = AsyncMock(side_effect=AssertionError('headless member has no HTTP'))
        self.assertTrue(await helper.backend_health())

    async def test_client_identity_and_engine_extensions(self):
        helper = vh.Helper({'rank':0}, lambda:True)
        obj={'request_id':'client','rid':'client'}
        self.assertEqual(helper.prepare_request(obj,'e'*32),'X-Request-Id: '+'e'*32+'\r\n')
        self.assertEqual(obj,{'request_id':'e'*32})
        for key in ('use_beam_search','kv_transfer_params','chat_template','prompt_embeds'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                helper.prepare_request({key:True},'e'*32)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--build-dir',type=Path,default=ROOT/'build/local')
    parser.add_argument('--no-build',action='store_true')
    parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    out=args.output_dir or Path(tempfile.mkdtemp(prefix='vllm-contract-',dir='/tmp'))
    out.mkdir(parents=True,exist_ok=True)
    if not args.no_build:
        with (out/'build.log').open('w') as log:
            subprocess.run(['cmake','--build',str(args.build_dir),'--target','test-pipeline-vllm','test-pipeline-profile','pipeline-agent-dev','-j','6'],check=True,stdout=log,stderr=subprocess.STDOUT)
    plans=subprocess.check_output([str(args.build_dir/'pipeline/test-pipeline-vllm')],text=True)
    (out/'plans.json').write_text(plans)
    sg.CONFIG=json.loads(plans)[0]['helper']
    subprocess.run([str(args.build_dir/'pipeline/test-pipeline-profile')],check=True)
    suite=unittest.defaultTestLoader.loadTestsFromModule(sg)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(CancellationTests))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    paths=[p for pattern in ('pipeline/Vllm.*','pipeline/vllm*','pipeline/engine-helper.py','test/test-pipeline-vllm*','test/test-pipeline-sglang.py') for p in ROOT.glob(pattern) if p.is_file()]
    (out/'result.json').write_text(json.dumps({'passed':result.wasSuccessful(),'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'sources':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}},indent=2))
    print('Artifacts:',out)
    raise SystemExit(not result.wasSuccessful())
