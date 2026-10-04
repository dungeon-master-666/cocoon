#!/usr/bin/env python3
"""Build and exercise the local pipeline supervisor on macOS/Linux, without a GPU."""

import argparse
import contextlib
import http.client
import hashlib
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'cocoon-simulator@v1:dev-fixture'
BIN = None
RESULTS = None


def wait_for(action, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = action()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError('condition did not become true before deadline')


def alive(pid, group=False):
    try:
        (os.killpg if group else os.kill)(pid, 0)
        return True
    except ProcessLookupError:
        return False


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=3):
        super().__init__('localhost', timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class AgentRun:
    def __init__(self, scenario='normal', rank=0, **sim):
        self.root = Path(tempfile.mkdtemp(prefix='case-', dir=RESULTS))
        self.run = self.root / 'run'
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'profile': 'simulator-dev-pp2-v1', 'rank': rank,
                                          'role': 'head' if rank == 0 else 'member',
                                          'simulator': {'scenario': scenario, **sim}}))
        self.log = (self.root / 'agent.log').open('w')
        self.proc = subprocess.Popen([str(BIN / 'pipeline-agent-dev'), '--config', str(self.config),
                                      '--run-dir', str(self.run)], stdout=self.log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        self.pgid = None

    def status(self):
        try:
            status = json.loads((self.run / 'status.json').read_text())
        except FileNotFoundError:
            return {}
        if status['process']['pid'] > 0:
            self.pgid = status['process']['pgid']
        return status

    def wait_state(self, state, timeout=5):
        def check():
            status = self.status()
            if self.proc.poll() is not None:
                raise AssertionError(f'agent exited before {state}: {status}; logs: {self.root}')
            return status if status.get('state') == state else None
        return wait_for(check, timeout)

    def control(self, op):
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(1)
            conn.connect(str(self.run / 'control.sock'))
            conn.sendall(json.dumps({'op': op}).encode() + b'\n')
            with conn.makefile('rb') as reader:
                return json.loads(reader.readline(8192))

    def request(self, path='/health', body=None, socket_name='backend.sock'):
        conn = UnixHTTP(self.run / socket_name)
        try:
            conn.request('POST' if body else 'GET', path, json.dumps(body) if body else None,
                         {'Content-Type': 'application/json'})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def assert_finished(self, expected, timeout=6):
        code = self.proc.wait(timeout=timeout)
        status = self.status()
        assert code == expected, (code, status, self.root)
        assert status['state'] == ('STOPPED' if expected == 0 else 'FAILED'), status
        assert status['process']['cleanup_complete'], status
        assert not status['local_ready'] and not status['group_ready'], status
        assert not (self.run / 'backend.sock').exists(), status
        assert not (self.run / 'health.sock').exists(), status
        assert not (self.run / 'control.sock').exists(), status
        if self.pgid:
            assert not alive(self.pgid, group=True), status
        return status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        self.status()
        if self.pgid and alive(self.pgid, group=True):
            os.killpg(self.pgid, signal.SIGKILL)
        self.log.close()


def payload(**extra):
    return {'model': MODEL, 'messages': [{'role': 'user', 'content': 'hello pipeline'}],
            'max_tokens': 2, **extra}


class PipelineTests(unittest.TestCase):
    def test_head_and_member_are_local_only(self):
        with AgentRun(rank=0) as head, AgentRun(rank=1) as member:
            a, b = head.wait_state('LOCAL_READY'), member.wait_state('LOCAL_READY')
            self.assertEqual(a['config_digest'], b['config_digest'])
            self.assertNotEqual(a['process']['pid'], b['process']['pid'])
            for node, rank in ((head, 0), (member, 1)):
                info = node.control('status')
                self.assertEqual(info['rank'], rank)
                self.assertTrue(info['local_ready'])
                self.assertFalse(info['group_ready'])
                self.assertFalse(info['hardware_attested'])
                self.assertIsNone(info['epoch'])
                self.assertEqual((node.run.stat().st_mode & 0o777), 0o700)
                self.assertEqual(((node.run / 'backend.sock').stat().st_mode & 0o777), 0o600)
                self.assertEqual(((node.run / 'health.sock').stat().st_mode & 0o777), 0o600)
                self.assertEqual(node.control('stop')['state'], 'STOPPING')
                node.assert_finished(0)

    def test_readiness_requires_warmup(self):
        with AgentRun(warmup_delay_ms=700) as node:
            node.wait_state('WARMING')
            self.assertFalse(node.control('status')['local_ready'])
            self.assertEqual(node.request()[0], 200)
            node.wait_state('LOCAL_READY')
            node.control('stop')
            node.assert_finished(0)

    def test_json_sse_usage_and_cancel(self):
        with AgentRun() as node:
            node.wait_state('LOCAL_READY')
            code, body = node.request('/v1/chat/completions', payload())
            answer = json.loads(body)
            self.assertEqual(code, 200)
            self.assertEqual(answer['choices'][0]['message']['content'], 'simulated reply')
            self.assertEqual(answer['usage'], {'prompt_tokens': 2, 'completion_tokens': 2, 'total_tokens': 4})
            code, body = node.request('/v1/chat/completions', payload(stream=True))
            events = [line[6:] for line in body.decode().splitlines() if line.startswith('data: ')]
            self.assertEqual(events[-1], '[DONE]')
            self.assertEqual(events.count('[DONE]'), 1)
            parsed = [json.loads(event) for event in events[:-1]]
            self.assertEqual(''.join(event['choices'][0]['delta'].get('content', '') for event in parsed),
                             answer['choices'][0]['message']['content'])
            self.assertEqual(parsed[-1]['usage'], answer['usage'])
            conn = UnixHTTP(node.run / 'backend.sock')
            conn.request('POST', '/v1/chat/completions', json.dumps(payload(
                max_tokens=64, stream=True, simulator={'token_delay_ms': 100})))
            response = conn.getresponse()
            self.assertTrue(response.fp.readline().startswith(b'data: '))
            response.close()
            conn.close()
            wait_for(lambda: json.loads(node.request()[1])['active_requests'] == 0)
            self.assertGreaterEqual(json.loads(node.request()[1])['cancelled_requests'], 1)
            self.assertEqual(node.request('/v1/chat/completions', payload())[0], 200)
            node.control('stop')
            node.assert_finished(0)

    def test_request_limits_and_faults(self):
        with AgentRun() as node:
            node.wait_state('LOCAL_READY')
            self.assertEqual(node.request('/v1/chat/completions', payload(max_tokens=65))[0], 400)
            oversized = payload(messages=[{'role': 'user', 'content': 'word ' * 511}])
            self.assertEqual(node.request('/v1/chat/completions', oversized)[0], 400)
            self.assertEqual(node.request('/v1/chat/completions', payload(simulator={'fault': 'http-error'}))[0], 503)
            code, body = node.request('/v1/chat/completions', payload(stream=True, simulator={'fault': 'truncate'}))
            self.assertEqual(code, 200)
            self.assertNotIn(b'[DONE]', body)
            self.assertNotIn(b'"finish_reason":"stop"', body)
            code, body = node.request('/v1/chat/completions', payload(stream=True, simulator={'fault': 'error-event'}))
            self.assertIn(b'"error"', body)
            node.control('stop')
            node.assert_finished(0)

    def test_capacity_is_released_on_disconnect(self):
        with AgentRun() as node:
            node.wait_state('LOCAL_READY')
            connections = []
            responses = []
            try:
                first = UnixHTTP(node.run / 'backend.sock')
                connections.append(first)
                large = payload(messages=[{'role': 'user', 'content': 'word ' * 300}], max_tokens=64,
                                stream=True, simulator={'token_delay_ms': 1000})
                first.request('POST', '/v1/chat/completions', json.dumps(large))
                responses.append(first.getresponse())
                code, body = node.request('/v1/chat/completions', large)
                self.assertEqual(code, 429)
                self.assertIn(b'token budget', body)
                second = UnixHTTP(node.run / 'backend.sock')
                connections.append(second)
                second.request('POST', '/v1/chat/completions', json.dumps(payload(
                    max_tokens=64, stream=True, simulator={'token_delay_ms': 1000})))
                responses.append(second.getresponse())
                self.assertEqual(node.request('/v1/chat/completions', payload())[0], 429)
            finally:
                for response in responses:
                    response.close()
                for conn in connections:
                    conn.close()
            wait_for(lambda: json.loads(node.request()[1])['active_requests'] == 0)
            self.assertEqual(json.loads(node.request()[1])['prompt_tokens_in_flight'], 0)
            self.assertEqual(node.request('/v1/chat/completions', payload())[0], 200)
            node.control('stop')
            node.assert_finished(0)

    def test_startup_and_warmup_failures_are_bounded(self):
        for scenario, reason in [('startup-exit', 'exited'), ('startup-hang', 'startup deadline'),
                                 ('warmup-error', 'warmup failed'), ('warmup-hang', 'warmup deadline')]:
            with self.subTest(scenario=scenario), AgentRun(scenario) as node:
                started = time.monotonic()
                status = node.assert_finished(1)
                self.assertLess(time.monotonic() - started, 5)
                self.assertIn(reason, status['failure'])
                self.assertNotIn('LOCAL_READY', (node.root / 'agent.log').read_text())

    def test_health_watchdog_and_crash(self):
        for scenario, reason in [('health-hang', 'watchdog'), ('crash-after-ready', 'exited')]:
            with self.subTest(scenario=scenario), AgentRun(scenario) as node:
                node.wait_state('LOCAL_READY')
                self.assertTrue(node.control('status')['local_ready'])
                self.assertIn(reason, node.assert_finished(1)['failure'])

    def test_stop_during_startup_and_warmup(self):
        for state, options in [('STARTING', {'startup_delay_ms': 2000}),
                               ('WARMING', {'warmup_delay_ms': 1500})]:
            with self.subTest(state=state), AgentRun(**options) as node:
                node.wait_state(state)
                node.control('stop')
                node.assert_finished(0)
                self.assertNotIn('LOCAL_READY', (node.root / 'agent.log').read_text())

    def test_sigint_sigterm_and_repeat_runs(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig), AgentRun() as node:
                node.wait_state('LOCAL_READY')
                node.proc.send_signal(sig)
                node.assert_finished(0)

    def test_kills_unresponsive_backend_and_descendants(self):
        for crash_parent in (False, True):
            with self.subTest(crash_parent=crash_parent), AgentRun('stubborn-child') as node:
                info = node.wait_state('LOCAL_READY')
                child = wait_for(lambda: int((node.run / 'child.pid').read_text())
                                 if (node.run / 'child.pid').exists() else None)
                self.assertTrue(alive(child))
                if crash_parent:
                    os.kill(info['process']['pid'], signal.SIGKILL)
                else:
                    os.kill(info['process']['pid'], signal.SIGSTOP)
                    node.control('stop')
                    self.assertEqual(node.control('stop')['state'], 'STOPPING')
                status = node.assert_finished(1 if crash_parent else 0)
                self.assertTrue(status['process']['kill_sent'])
                self.assertFalse(alive(child))

    def test_stalled_control_client_cannot_block_stop(self):
        with AgentRun() as node, contextlib.ExitStack() as stack:
            node.wait_state('LOCAL_READY')
            for _ in range(8):
                conn = stack.enter_context(socket.socket(socket.AF_UNIX))
                conn.connect(str(node.run / 'control.sock'))
                conn.sendall(b'{')
            started = time.monotonic()
            self.assertTrue(node.control('status')['local_ready'])
            node.control('stop')
            node.assert_finished(0)
            self.assertLess(time.monotonic() - started, 2)

    def test_stalled_api_clients_cannot_block_health(self):
        with AgentRun() as node:
            node.wait_state('LOCAL_READY')
            self.assertEqual(node.request('/v1/chat/completions', payload(), socket_name='health.sock')[0], 404)
            self.assertEqual(node.request('/v1/models', socket_name='health.sock')[0], 404)
            with contextlib.ExitStack() as stack:
                connections = []
                for _ in range(16):
                    conn = stack.enter_context(socket.socket(socket.AF_UNIX))
                    conn.settimeout(1)
                    conn.connect(str(node.run / 'backend.sock'))
                    conn.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
                                 b'Content-Length: 8192\r\nExpect: 100-continue\r\n\r\n')
                    reader = stack.enter_context(conn.makefile('rb'))
                    # The interim response confirms that this connection owns a
                    # handler slot and is now waiting for its request body.
                    self.assertEqual(reader.readline(), b'HTTP/1.1 100 Continue\r\n')
                    self.assertEqual(reader.readline(), b'\r\n')
                    connections.append(conn)
                deadline = time.monotonic() + 2.5
                while time.monotonic() < deadline:
                    # Keep all slots occupied beyond the 1.5-second watchdog.
                    for conn in connections:
                        conn.sendall(b' ')
                    self.assertTrue(node.control('status')['local_ready'])
                    code, body = node.request(socket_name='health.sock')
                    self.assertEqual(code, 200)
                    self.assertTrue(json.loads(body)['warmup_complete'])
                    time.sleep(0.1)

            def api_available():
                try:
                    return node.request()[0] == 200
                except (OSError, http.client.HTTPException):
                    return False

            wait_for(api_available)
            self.assertEqual(node.request('/v1/chat/completions', payload())[0], 200)
            node.control('stop')
            node.assert_finished(0)

    def test_delayed_supervisor_still_kills_backend(self):
        with AgentRun() as node:
            info = node.wait_state('LOCAL_READY')
            os.kill(info['process']['pid'], signal.SIGSTOP)
            node.control('stop')
            node.proc.send_signal(signal.SIGSTOP)
            try:
                # Resume beyond both original stop budgets. The agent must still
                # send SIGKILL and reap, rather than exit and orphan the backend.
                time.sleep(3)
            finally:
                node.proc.send_signal(signal.SIGCONT)
            node.assert_finished(0)

    def test_invalid_configs_never_spawn(self):
        base = {'profile': 'simulator-dev-pp2-v1', 'rank': 0, 'role': 'head'}
        cases = [('{broken', 'pipeline-agent-dev'),
                 (' ' * 65537, 'pipeline-agent-dev'),
                 ('[' * 20 + '0' + ']' * 20, 'pipeline-agent-dev'),
                 ('{"profile":"x","profile":"simulator-dev-pp2-v1","rank":0,"role":"head"}', 'pipeline-agent-dev'),
                 (json.dumps(base), 'pipeline-agent'),
                 (json.dumps({**base, 'security_mode': 'dev'}), 'pipeline-agent'),
                 (json.dumps({**base, 'role': 'member'}), 'pipeline-agent-dev'),
                 (json.dumps({**base, 'rank': True}), 'pipeline-agent-dev'),
                 (json.dumps({**base, 'profile': 'vllm-dev-pp2-v1'}), 'pipeline-agent-dev'),
                 (json.dumps({**base, 'simulator': {'command': '/bin/sh'}}), 'pipeline-agent-dev')]
        for text, binary in cases:
            with self.subTest(config=text, binary=binary):
                case = Path(tempfile.mkdtemp(prefix='reject-', dir=RESULTS))
                config = case / 'config.json'
                config.write_text(text)
                result = subprocess.run([str(BIN / binary), '--config', str(config), '--run-dir', str(case / 'run')],
                                        capture_output=True, timeout=3)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((case / 'run').exists())
        result = subprocess.run([str(BIN / 'pipeline-agent'), '--no-tee'], capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)

    def test_existing_directory_is_preserved(self):
        with AgentRun() as node:
            node.wait_state('LOCAL_READY')
            marker = node.run / 'unrelated-data'
            marker.write_text('keep')
            result = subprocess.run([str(BIN / 'pipeline-agent-dev'), '--config', str(node.config),
                                     '--run-dir', str(node.run)], capture_output=True, timeout=3)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(node.control('status')['local_ready'])
            node.control('stop')
            node.assert_finished(0)
            self.assertEqual(marker.read_text(), 'keep')

    def test_control_command_line(self):
        with AgentRun() as node:
            node.wait_state('LOCAL_READY')
            for operation, state in [('status', 'LOCAL_READY'), ('stop', 'STOPPING')]:
                result = subprocess.run([sys.executable, str(ROOT / 'pipeline/control.py'), str(node.run), operation],
                                        capture_output=True, timeout=3, check=True)
                self.assertEqual(json.loads(result.stdout)['state'], state)
            node.assert_finished(0)


def main():
    global BIN, RESULTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=ROOT / 'build/local')
    parser.add_argument('--no-build', action='store_true')
    args = parser.parse_args()
    build = args.build_dir.resolve()
    BIN = build / 'pipeline'
    RESULTS = Path(tempfile.mkdtemp(prefix='cocoon-agent-', dir='/tmp'))
    print(f'Logs: {RESULTS}', flush=True)
    if not args.no_build:
        subprocess.run(['cmake', '--build', str(build), '--target', 'pipeline-agent', 'pipeline-agent-dev',
                        'test-pipeline-profile', '-j', '4'], check=True)
    subprocess.run([str(BIN / 'test-pipeline-profile')], check=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PipelineTests)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'passed': result.wasSuccessful(), 'tests': result.testsRun, 'seconds': time.monotonic() - started,
              'platform': platform.platform(),
              'source_sha256': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in sorted((ROOT / 'pipeline').rglob('*'))
                                if path.is_file() and '__pycache__' not in path.parts},
              'failures': [(str(case), detail) for case, detail in result.failures + result.errors]}
    (RESULTS / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'Report: {RESULTS / "report.json"}', flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
