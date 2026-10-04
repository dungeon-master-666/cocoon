#!/usr/bin/env python3
"""Step 10 native adapter/helper contract tests, no GPU or SGLang dependency."""
import argparse
import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sglang_helper', ROOT / 'pipeline/sglang-helper.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
CONFIG = None


async def wait(fn, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := fn():
            return value
        await asyncio.sleep(.01)
    raise AssertionError('condition did not become true')


class Fixture:
    def __init__(self):
        self.tasks, self.active, self.aborted = set(), set(), []
        self.requests, self.sent = [], 0
        self.abort_status = 200
        self.connected = asyncio.Event()
        self.wire = []
        self.hold_wire_open = False
        self.release_wire = asyncio.Event()

    def accept(self, reader, writer):
        task = asyncio.create_task(self.handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def handle(self, reader, writer):
        rid = None
        try:
            writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
            first, headers, _ = await helper.header(reader)
            method, path, _ = first.decode().split()
            data = await reader.readexactly(int(headers.get(b'content-length', b'0')))
            obj = json.loads(data) if data else {}
            if path == '/health':
                await helper.reply(writer, 200, {'fixture': True})
                return
            if path == '/abort_request':
                self.aborted.append(obj['rid'])
                await helper.reply(writer, self.abort_status, {})
                return
            self.requests.append((path, obj))
            rid = obj['rid']
            self.active.add(rid)
            self.connected.set()
            mode = obj.get('user', 'json')
            if mode == 'wire':
                for part in self.wire:
                    writer.write(part)
                    await writer.drain()
                    await asyncio.sleep(0)
                if self.hold_wire_open:
                    await self.release_wire.wait()
            elif mode == 'hang':
                await reader.read()
            elif mode == 'error':
                await helper.reply(writer, 503, {'error': 'injected failure'})
            elif mode == 'truncate':
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort')
                await writer.drain()
            elif mode == 'stream':
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n')
                for event in (b'data: {"delta":"hello"}\n\n', b'data: {"delta":" world"}\n\n', b'data: [DONE]\n\n'):
                    writer.write(f'{len(event):x}\r\n'.encode() + event + b'\r\n')
                    await writer.drain()
                    await asyncio.sleep(.08)
                writer.write(b'0\r\n\r\n')
                await writer.drain()
            elif mode in ('bulk', 'oversized'):
                count = 80 if mode == 'bulk' else 256
                writer.write(f'HTTP/1.1 200 OK\r\nContent-Length: {count*16384}\r\nConnection: close\r\n\r\n'.encode())
                for _ in range(count):
                    writer.write(b'x' * 16384)
                    await writer.drain()
                    self.sent += 16384
            else:
                await helper.reply(writer, 200, {'choices': [{'text': 'fixture'}], 'usage': {'prompt_tokens': 2, 'completion_tokens': 1}})
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            if rid:
                self.active.discard(rid)
            await helper.close(writer)


class HelperTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='sg10-', dir='/tmp')
        self.config = dict(CONFIG)
        self.config.update(api_socket=self.directory.name + '/api.sock', health_socket=self.directory.name + '/health.sock')
        self.fixture = Fixture()
        self.server = await asyncio.start_server(self.fixture.accept, '127.0.0.1', 0, limit=16384)
        self.alive = True
        self.bridge = helper.Helper(self.config, lambda: self.alive, endpoint=self.server.sockets[0].getsockname())
        await self.bridge.start()
        self.clients = []

    async def asyncTearDown(self):
        self.fixture.release_wire.set()
        for w in self.clients:
            await helper.close(w)
        await self.bridge.stop()
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.fixture.tasks):
            task.cancel()
        await asyncio.gather(*self.fixture.tasks, return_exceptions=True)
        self.assertEqual(self.bridge.connections, 0)
        self.assertFalse(self.bridge.active)
        self.assertFalse(Path(self.config['api_socket']).exists())
        self.directory.cleanup()

    def payload(self, mode='json', **fields):
        return {'model': self.config['model_manifest']['model']['id'], 'prompt': 'hello', 'max_tokens': 3, 'user': mode, **fields}

    async def request(self, payload=None, path='/v1/completions', health=False, raw=None):
        r, w = await asyncio.open_unix_connection(self.config['health_socket' if health else 'api_socket'], limit=16384)
        self.clients.append(w)
        if raw is None:
            body = json.dumps(payload).encode() if payload is not None else b''
            raw = f'{"POST" if payload is not None else "GET"} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body
        w.write(raw)
        await w.drain()
        return r, w

    async def response(self, **kwargs):
        r, _ = await self.request(**kwargs)
        return await asyncio.wait_for(r.read(), 4)

    async def test_json_and_http_error(self):
        for mode, code in [('json', 200), ('error', 503)]:
            raw = await self.response(payload=self.payload(mode))
            self.assertIn(f' {code} '.encode(), raw.split(b'\r\n')[0])
            self.assertIn(b'fixture' if mode == 'json' else b'injected failure', raw)
        self.assertEqual(self.bridge.completed, 2)
        self.assertFalse(self.fixture.aborted)

    async def test_incremental_stream_and_terminal(self):
        r, _ = await self.request(self.payload('stream', stream=True))
        await helper.header(r)
        times, data = [], b''
        while part := await asyncio.wait_for(r.read(256), 3):
            times.append(time.monotonic()); data += part
        self.assertGreater(times[-1] - times[0], .12)
        self.assertEqual(data.count(b'[DONE]'), 1)
        self.assertTrue(data.endswith(b'0\r\n\r\n'))

    async def test_complete_response_disconnect_does_not_abort_or_hold_admission(self):
        responses = (
            b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}',
            b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n',
            b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: Chunked\r\n\r\n'
            b'e;fixture="yes"\r\ndata: [DONE]\n\n\r\n0\r\nX-Fixture: done\r\n\r\n',
        )
        self.fixture.hold_wire_open = True
        for raw in responses:
            with self.subTest(response=raw):
                self.fixture.wire = [raw]
                r, w = await self.request(self.payload('wire'))
                self.assertEqual(await asyncio.wait_for(r.readexactly(len(raw)), 2), raw)
                await helper.close(w)  # gate closes as soon as HTTP framing is complete
                self.assertIn(b' 200 ', await self.response(payload=self.payload()))
                self.assertFalse(self.bridge.active)
                self.assertEqual(self.bridge.cancelled, 0)
                self.assertFalse(self.fixture.aborted)
        self.assertEqual(self.bridge.completed, 6)

    async def test_fragmented_framing_completes_without_upstream_eof(self):
        for raw in (b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello',
                    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
                    b'2\r\nhe\r\n3\r\nllo\r\n0\r\n\r\n'):
            with self.subTest(response=raw):
                self.fixture.wire = [raw[i:i+1] for i in range(len(raw))]
                self.fixture.hold_wire_open = True
                self.assertEqual(await self.response(payload=self.payload('wire')), raw)
        self.assertEqual(self.bridge.completed, 2)
        self.assertFalse(self.fixture.aborted)

    async def test_disconnect_during_final_drain_is_already_complete(self):
        final_drain = asyncio.Event()
        relay_response = helper.relay_response

        class SlowWriter:
            def __init__(self, writer):
                self.writer, self.final = writer, False

            def write(self, data):
                self.writer.write(data)
                self.final = data == b'ok'

            async def drain(self):
                await self.writer.drain()
                if self.final:
                    final_drain.set()
                    await asyncio.Event().wait()

        async def slow_relay(source, writer, complete):
            await relay_response(source, SlowWriter(writer), complete)

        self.fixture.wire = [b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok']
        self.fixture.hold_wire_open = True
        with mock.patch.object(helper, 'relay_response', slow_relay):
            r, w = await self.request(self.payload('wire'))
            await helper.header(r)
            self.assertEqual(await asyncio.wait_for(r.readexactly(2), 2), b'ok')
            await asyncio.wait_for(final_drain.wait(), 2)
            await helper.close(w)
            self.assertIn(b' 200 ', await self.response(payload=self.payload()))
        await wait(lambda: not self.bridge.tasks)
        self.assertEqual(self.bridge.completed, 2)
        self.assertEqual(self.bridge.cancelled, 0)
        self.assertFalse(self.fixture.aborted)

    async def test_disconnect_before_framed_end_still_aborts(self):
        responses = (
            b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort',
            b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n64\r\nshort',
            b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\ne\r\ndata: [DONE]\n\n\r\n',
            b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nX-Fixture: waiting\r\n',
        )
        self.fixture.hold_wire_open = True
        for i, raw in enumerate(responses, 1):
            with self.subTest(response=raw):
                self.fixture.wire = [raw]
                r, w = await self.request(self.payload('wire'))
                self.assertEqual(await asyncio.wait_for(r.readexactly(len(raw)), 2), raw)
                rid = self.fixture.requests[-1][1]['rid']
                await helper.close(w)
                await wait(lambda: not self.bridge.active)
                self.assertEqual(self.fixture.aborted[-2:], [rid, rid])
                self.assertEqual(self.bridge.cancelled, i)
                self.assertEqual(self.bridge.completed, 0)
        self.assertFalse(self.bridge.fatal.is_set())

    async def test_invalid_or_truncated_response_framing_aborts(self):
        responses = (
            b'Content-Length: bad\r\n\r\n',
            b'Transfer-Encoding: gzip\r\n\r\n',
            b'Transfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n0\r\n\r\n',
            b'Transfer-Encoding: chunked\r\n\r\n+2\r\nok\r\n0\r\n\r\n',
            b'Transfer-Encoding: chunked\r\n\r\n2\r\nokXX0\r\n\r\n',
            b'Transfer-Encoding: chunked\r\n\r\n2\r\no',
            b'Transfer-Encoding: chunked\r\n\r\ne\r\ndata: [DONE]\n\n\r\n',
            b'Transfer-Encoding: chunked\r\n\r\n0\r\n',
            b'Transfer-Encoding: chunked\r\n\r\n0\r\nBad-Trailer\r\n\r\n',
            b'Transfer-Encoding: chunked\r\n\r\n0\r\nContent-Length: 0\r\n\r\n',
            b'Transfer-Encoding: chunked\r\n\r\n1;' + b'x' * helper.HEADER_LIMIT + b'\r\nx\r\n0\r\n\r\n',
            b'Transfer-Encoding: chunked\r\n\r\n0\r\nX: ' + b'x' * (helper.HEADER_LIMIT - 8) + b'\r\nY: y\r\n\r\n',
            b'Transfer-Encoding: chunked\r\n\r\n' + format(helper.WIRE_LIMIT, 'x').encode() + b'\r\n',
        )
        for i, raw in enumerate(responses, 1):
            with self.subTest(response=raw[:200]):
                self.fixture.wire = [b'HTTP/1.1 200 OK\r\n' + raw]
                await self.response(payload=self.payload('wire'))
                self.assertEqual(self.bridge.cancelled, i)
                self.assertEqual(self.bridge.completed, 0)
                self.assertFalse(self.bridge.active)

    async def test_close_delimited_response_still_waits_for_eof(self):
        raw = b'HTTP/1.0 200 OK\r\n\r\nclose-delimited body'
        self.fixture.wire = [raw]
        self.fixture.hold_wire_open = True
        r, _ = await self.request(self.payload('wire'))
        self.assertEqual(await asyncio.wait_for(r.readexactly(len(raw)), 2), raw)
        self.assertEqual(self.bridge.completed, 0)
        self.assertTrue(self.bridge.active)
        self.fixture.release_wire.set()
        self.assertEqual(await asyncio.wait_for(r.read(), 2), b'')
        self.assertEqual(self.bridge.completed, 1)
        self.assertFalse(self.fixture.aborted)

    async def test_disconnect_before_headers_aborts_exact_identity(self):
        _, w = await self.request(self.payload('hang', rid='client-controlled'))
        await self.fixture.connected.wait()
        rid = next(iter(self.fixture.active))
        self.assertNotEqual(rid, 'client-controlled')
        await helper.close(w)
        await wait(lambda: not self.bridge.active)
        self.assertEqual(self.fixture.aborted, [rid, rid])
        self.assertFalse(self.fixture.active)
        self.assertFalse(self.bridge.fatal.is_set())
        self.assertIn(b' 200 ', await self.response(payload=self.payload()))

    async def test_cancelled_abort_failure_closes_readiness(self):
        self.fixture.abort_status = 500
        _, w = await self.request(self.payload('hang'))
        await self.fixture.connected.wait()
        await helper.close(w)
        await wait(self.bridge.fatal.is_set)
        raw = await self.response(path='/health', health=True)
        self.assertIn(b' 503 ', raw)
        self.assertEqual(self.bridge.abort_failures, 1)
        self.assertIn(b' 503 ', await self.response(payload=self.payload()))

    async def test_capacity_and_health_are_independent(self):
        _, w = await self.request(self.payload('hang'))
        await self.fixture.connected.wait()
        self.assertIn(b' 429 ', await self.response(payload=self.payload()))
        self.assertIn(b'"active_requests":1', await self.response(path='/health', health=True))
        await helper.close(w)

    async def test_simultaneous_admission_is_bounded(self):
        clients = await asyncio.gather(*(self.request(self.payload('hang')) for _ in range(5)))
        await self.fixture.connected.wait()
        await asyncio.sleep(.1)
        self.assertEqual(len(self.fixture.requests), 1)
        self.assertEqual(len(self.bridge.active), 1)
        for _, w in clients:
            await helper.close(w)

    async def test_backpressure_and_disconnect_while_write_blocked(self):
        r, w = await self.request(self.payload('bulk'))
        await helper.header(r)
        await asyncio.sleep(.4)
        self.assertLess(self.fixture.sent, 80 * 16384, 'helper buffered whole response despite stalled UDS reader')
        await helper.close(w)
        await wait(lambda: not self.bridge.active)
        self.assertEqual(self.bridge.cancelled, 1)
        self.assertEqual(len(set(self.fixture.aborted)), 1)

    async def test_byte_exact_transfer_after_slow_consumer_resumes(self):
        r, _ = await self.request(self.payload('bulk'))
        _, _, _ = await helper.header(r)
        await asyncio.sleep(.1)
        body = await asyncio.wait_for(r.read(), 4)
        self.assertEqual(body, b'x' * (80 * 16384))

    async def test_output_wire_limit(self):
        r, _ = await self.request(self.payload('oversized'))
        raw = await asyncio.wait_for(r.read(), 4)
        self.assertLessEqual(len(raw), helper.WIRE_LIMIT)
        await wait(lambda: not self.bridge.active)
        self.assertEqual(self.bridge.cancelled, 1)

    async def test_no_successful_terminal_added_on_truncation(self):
        raw = await self.response(payload=self.payload('truncate'))
        self.assertTrue(raw.endswith(b'\r\n\r\nshort'))
        self.assertIn(b'Content-Length: 100', raw)
        self.assertNotIn(b'[DONE]', raw)
        self.assertEqual(self.bridge.completed, 0)
        self.assertEqual(self.bridge.cancelled, 1)

    async def test_private_api_and_engine_extensions(self):
        for path in ('/health', '/metrics', '/abort_request', '/v1/models', '/v1/completions?x=1'):
            self.assertIn(b' 404 ', await self.response(payload=self.payload(), path=path))
        for fields in ({'model': 'other'}, {'n': 2}, {'prompt': ['one', 'two']}, {'lora_path': '/tmp/evil'},
                       {'custom_logit_processor': 'evil'}, {'stream': 1}):
            self.assertIn(b' 400 ', await self.response(payload=self.payload(**fields)))
        self.assertFalse(self.fixture.requests)

    async def test_invalid_framing_and_body_limit(self):
        for fields in (b'Content-Length: 9000', b'Content-Length: 0\r\nContent-Length: 0',
                       b'Transfer-Encoding: chunked', b'Content-Length: -1'):
            raw = await self.response(raw=b'POST /v1/completions HTTP/1.1\r\n' + fields + b'\r\n\r\n')
            self.assertIn(b' 400 ', raw)
        self.assertFalse(self.fixture.requests)

    async def test_health_is_not_generation_and_backend_exit_is_not_healthy(self):
        raw = await self.response(path='/health', health=True)
        self.assertIn(b' 200 ', raw)
        self.assertNotIn(b'choices', raw)
        self.alive = False
        self.assertIn(b' 503 ', await self.response(path='/health', health=True))

    async def test_member_only_has_private_health_socket(self):
        await self.bridge.stop()
        self.config['rank'] = 1
        self.bridge = helper.Helper(self.config, lambda: True, endpoint=self.server.sockets[0].getsockname())
        await self.bridge.start()
        self.assertFalse(Path(self.config['api_socket']).exists())
        self.assertIn(b'"rank":1', await self.response(path='/health', health=True))


class ArtifactTests(unittest.TestCase):
    def test_manifest_change_rebuilds_embedded_data(self):
        # Configure the real pipeline CMakeLists in a small project, then build
        # only a probe using its generated header. No changes to the worktree.
        with tempfile.TemporaryDirectory(prefix='sg10-cmake-', dir='/tmp') as d:
            root = Path(d)
            shutil.copytree(ROOT / 'pipeline', root / 'pipeline', ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'test').mkdir()
            for source in (ROOT / 'test').glob('test-pipeline-*.cpp'):
                shutil.copy2(source, root / 'test' / source.name)
            (root / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.16)
project(manifest_probe LANGUAGES CXX)
add_library(Boost::headers INTERFACE IMPORTED)
add_library(nlohmann_json::nlohmann_json INTERFACE IMPORTED)
add_subdirectory(pipeline)
add_executable(probe probe.cpp)
target_compile_features(probe PRIVATE cxx_std_17)
target_include_directories(probe PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/pipeline")
''')
            (root / 'probe.cpp').write_text('#include <iostream>\n#include "sglang-models.h"\n'
                                           'int main() { std::cout << sglang_models_json; }\n')

            def run(*argv):
                result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                self.assertEqual(result.returncode, 0, result.stdout)
                return result.stdout

            build = root / 'build'
            run('cmake', '-S', str(root), '-B', str(build))
            run('cmake', '--build', str(build), '--target', 'probe')
            manifest = root / 'pipeline/sglang-models.json'
            original = json.loads(manifest.read_text())
            self.assertEqual(json.loads(run(str(build / 'probe'))), original)
            changed = {**original, 'incremental_build_probe': 'updated manifest'}
            # Ensure distinct timestamps even on build tools with second precision.
            time.sleep(1.1)
            manifest.write_text(json.dumps(changed))
            run('cmake', '--build', str(build), '--target', 'probe')
            self.assertEqual(json.loads(run(str(build / 'probe'))), changed)

    def test_manifest_detects_modified_missing_and_extra_files(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'weights'; p.write_bytes(b'fixed weights')
            cfg = {'model_path': d, 'model_manifest': {'files': {'weights': {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}}}}
            helper.verify_model(cfg)
            p.write_bytes(b'evil! weights')
            with self.assertRaises(ValueError): helper.verify_model(cfg)
            p.unlink()
            with self.assertRaises(ValueError): helper.verify_model(cfg)
            p.write_bytes(b'fixed weights'); (Path(d) / 'config.json').write_text('{}')
            with self.assertRaises(ValueError): helper.verify_model(cfg)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--build-dir', type=Path, default=ROOT / 'build/local')
    parser.add_argument('--no-build', action='store_true')
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    out = args.output_dir or Path(tempfile.mkdtemp(prefix='sg10-contract-', dir='/tmp'))
    out.mkdir(parents=True, exist_ok=True)
    if not args.no_build:
        with (out / 'build.log').open('w') as log:
            subprocess.run(['cmake', '--build', str(args.build_dir), '--target', 'test-pipeline-sglang',
                            'test-pipeline-profile', 'pipeline-agent-dev', '-j', '6'], check=True, stdout=log, stderr=subprocess.STDOUT)
    plans = subprocess.check_output([str(args.build_dir / 'pipeline/test-pipeline-sglang')], text=True)
    (out / 'plans.json').write_text(plans)
    CONFIG = json.loads(plans)[0]['helper']
    subprocess.run([str(args.build_dir / 'pipeline/test-pipeline-profile')], check=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(__import__('__main__')))
    (out / 'result.json').write_text(json.dumps({'passed': result.wasSuccessful(), 'tests': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors),
        'sources': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for pattern in ('pipeline/CMakeLists.txt', 'pipeline/Sglang.*', 'pipeline/sglang-*',
                                    'test/test-pipeline-sglang.*') for p in ROOT.glob(pattern)}}, indent=2))
    print('Artifacts:', out)
    raise SystemExit(not result.wasSuccessful())
