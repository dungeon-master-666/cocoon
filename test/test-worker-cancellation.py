#!/usr/bin/env python3
"""Worker cancellation acceptance: real local runners, synthetic backend, fake TON."""

import argparse
import concurrent.futures
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('smoke', ROOT / 'benchmark/smoke-local.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
require = smoke.require


class Backend(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port):
        super().__init__(('127.0.0.1', port), Handler)
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.active = 0
        self.started = 0
        self.closed = 0
        self.expired = 0
        self.models_calls = 0
        self.thread = threading.Thread(target=self.serve_forever)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return {key: getattr(self, key) for key in ('active', 'started', 'closed', 'expired', 'models_calls')}

    def stop(self):
        self.stopping.set()
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def json(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self):
        # First health probe fails at the transport level: readiness must recover
        # through WorkerUplinkMonitor's error callback and scheduled retry.
        with self.server.lock:
            self.server.models_calls += 1
            first = self.server.models_calls == 1
        if first:
            self.close_connection = True
            return
        self.json({'object': 'list', 'data': [{'id': smoke.MODEL, 'object': 'model'}]})

    def chunk(self, event):
        data = ('data: ' + event + '\n\n').encode()
        self.wfile.write(f'{len(data):x}\r\n'.encode() + data + b'\r\n')
        self.wfile.flush()

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        mode = value['messages'][0]['content']
        if mode == 'normal':
            self.json({'id': 'cancel-test', 'object': 'chat.completion', 'model': smoke.MODEL,
                       'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'local smoke response'},
                                    'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 34, 'completion_tokens': 100, 'total_tokens': 134,
                                 'prompt_tokens_details': {'cached_tokens': 11},
                                 'completion_tokens_details': {'reasoning_tokens': 10}}})
            return
        backend = self.server
        with backend.lock:
            backend.active += 1
            backend.started += 1
        peer_closed = False
        try:
            if mode != 'hang-headers':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                self.wfile.flush()
                if mode == 'malformed':
                    self.chunk('{invalid json}')
                elif mode == 'error':
                    self.chunk(json.dumps({'error': {'message': 'injected backend error', 'type': 'server_error'}}))
                elif mode in ('hang-token', 'trickle'):
                    self.chunk(json.dumps({'choices': [{'index': 0, 'delta': {'content': 'x'}, 'finish_reason': None}]}))
            limit = time.monotonic() + 40
            while not backend.stopping.is_set() and time.monotonic() < limit:
                readable, _, _ = select.select([self.connection], [], [], 0.025)
                if readable and not self.connection.recv(4096):
                    peer_closed = True
                    break
                if mode == 'trickle':
                    self.chunk(json.dumps({'choices': [{'index': 0, 'delta': {'content': 'x'}, 'finish_reason': None}]}))
        except (BrokenPipeError, ConnectionResetError):
            peer_closed = True
        finally:
            with backend.lock:
                backend.active -= 1
                backend.closed += int(peer_closed)
                backend.expired += int(not peer_closed)
            self.close_connection = True


def payload(mode, timeout):
    return {'model': smoke.MODEL, 'stream': mode != 'normal', 'max_tokens': 128, 'timeout': timeout,
            'messages': [{'role': 'user', 'content': mode}]}


def worker_stats(ports):
    return json.loads(smoke.request(ports['worker_http'], '/jsonstats', timeout=1)['body'])['stats']


def check_failed_accounting(processes, ports, before, count):
    old = smoke.accounting_values(before)

    def complete():
        after = smoke.statistics(ports)
        counters = {role: {key: after[role]['stats'][key][0] - before[role]['stats'][key][0]
                           for key in ('queries', 'success', 'failed')} for role in before}
        return all(c == {'queries': count, 'success': 0, 'failed': count} for c in counters.values())

    smoke.wait_ready(processes, complete, 5)
    after = smoke.statistics(ports)
    require(smoke.accounting_values(after) == old, 'failed batch changed tokens/payments/balances')
    require(all(c['running_queries'] == 0 and c['reserved_tokens'] == 0 for c in after['proxy']['clients']),
            'proxy reservations retained')
    time.sleep(0.15)
    stable = smoke.statistics(ports)
    require(smoke.accounting_values(stable) == old and complete(), 'late/double completion')


def run(args, output):
    offset, ports, backend_port = smoke.choose_ports()
    backend = Backend(backend_port)
    state = output / 'stack'
    report = {'cases': [], 'ports': ports, 'backend_port': backend_port, 'passed': False}
    owned_pids = []
    try:
        with smoke.Processes(output, grace=10) as processes:
            launcher = processes.start('launcher', [sys.executable, '-u', str(ROOT / 'scripts/cocoon-launch'),
                                       '--local-all', '--skip-build', '--build-dir', str(args.build_dir),
                                       '--local-run-dir', str(state), '--local-port-offset', str(offset),
                                       '--local-backend', f'127.0.0.1:{backend_port}', '--model', smoke.MODEL])
            owned_pids.append(launcher.pid)
            smoke.wait_ready(processes, lambda: smoke.models_ready(ports['client_http']), 60)
            require(backend.snapshot()['models_calls'] >= 2, 'monitor did not retry transport failure')
            changed = smoke.request(ports['worker_http'], '/request/change_coefficient?coefficient=1')
            require(changed['status'] == 200, 'test tariff not applied')
            smoke.wait_ready(processes, lambda: [w['coefficient'] for w in smoke.statistics(ports)['proxy']['worker_connections']] == [1000], 5)

            def clean_after(previous, count):
                snap = backend.snapshot()
                return (snap['active'] == 0 and snap['closed'] == previous['closed'] + count
                        and snap['expired'] == 0 and worker_stats(ports)['active_requests'] == 0)

            def normal(label):
                before = smoke.statistics(ports)
                result = smoke.request(ports['client_http'], '/v1/chat/completions', payload('normal', 5))
                smoke.assert_success(result, False, '/v1/chat/completions')
                accounting = smoke.check_accounting(ports, before, True)
                require(worker_stats(ports)['active_requests'] == 0, 'successful actor retained')
                report['cases'].append({'name': label, 'seconds': result['seconds'], 'accounting': accounting})
                print('PASS ' + label, flush=True)

            normal('initial success')
            cases = [(mode, 0.8, 1) for mode in ('hang-headers', 'hang-body', 'hang-token', 'trickle')]
            cases += [(mode, 30, 1) for mode in ('malformed', 'error')]
            cases += [(mode, timeout, 8) for _ in range(3) for mode, timeout in [('hang-token', 0.8), ('malformed', 30)]]
            for mode, timeout, count in cases:
                previous = backend.snapshot()
                before = smoke.statistics(ports)
                started = time.monotonic()
                with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
                    results = list(pool.map(lambda _: smoke.request(ports['client_http'], '/v1/chat/completions', payload(mode, timeout)), range(count)))
                for result in results:
                    require(result['seconds'] < 2, f'{mode}: request did not finish promptly: {result}')
                    require(result['status'] in (500, 502, 504) or result['transport_error'], f'{mode}: false success')
                    require('[DONE]' not in result['body'], f'{mode}: premature terminal')
                    if timeout < 1:
                        require(result['seconds'] >= timeout * 0.7, f'{mode}: fractional deadline rounded away')
                smoke.wait_ready(processes, lambda: clean_after(previous, count), 2)
                release_seconds = time.monotonic() - started
                require(release_seconds < (timeout if timeout < 1 else 0) + 2, 'cleanup exceeded budget')
                check_failed_accounting(processes, ports, before, count)
                report['cases'].append({'name': mode, 'count': count, 'timeout': timeout,
                                        'release_seconds': release_seconds, 'backend': backend.snapshot()})
                print(f'PASS {mode} x{count}: zero active actors/sockets, zero billing', flush=True)
            normal('success after repeated cancellation')

            # Pause only the Python supervisor. Otherwise killing its proxy child
            # would stop the whole stack and could disguise a leaked worker HTTP
            # socket. The worker/router/client keep running throughout the fault.
            manifest = json.loads((state / 'processes.json').read_text())['processes']
            worker_pid = manifest['worker']
            proxy_pid = manifest['proxy']
            os.kill(launcher.pid, signal.SIGSTOP)
            try:
                for iteration in range(2):
                    # This test qualifies cancellation/reconnect with an already
                    # persisted balance. Crash-before-flush reconciliation is a
                    # separate existing protocol gap (P4-02), not a cancel signal.
                    def balance_persisted():
                        snapshot = smoke.statistics(ports)
                        return all(p['earned_tokens_committed_to_proxy_db'] == p['earned_tokens_max_known']
                                   for p in snapshot['worker']['proxies'])
                    # WorkerRunner compares payment state every 10..20 seconds;
                    # allow one full poll plus the proxy's 1..2 second DB flush.
                    smoke.wait_ready(processes, balance_persisted, 25)
                    previous = backend.snapshot()
                    before = worker_stats(ports)
                    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                        futures = [pool.submit(smoke.request, ports['client_http'], '/v1/chat/completions', payload('hang-token', 30), 5) for _ in range(8)]
                        smoke.wait_ready(processes, lambda: backend.snapshot()['active'] == 8 and worker_stats(ports)['active_requests'] == 8, 3)
                        started = time.monotonic()
                        os.kill(proxy_pid, signal.SIGKILL)
                        if iteration:
                            restarted.wait(timeout=3)
                            processes.children.remove((restart_name, restarted))
                        smoke.wait_ready(processes, lambda: clean_after(previous, 8), 2)
                        seconds = time.monotonic() - started
                        os.kill(worker_pid, 0)
                        after = worker_stats(ports)
                        require(after['failed'][0] - before['failed'][0] == 8 and after['success'][0] == before['success'][0], 'disconnect completed more than once or succeeded')
                        require(after['total_adjusted_tokens_used'][0] == before['total_adjusted_tokens_used'][0], 'disconnect charged partial usage')
                        results = [future.result(timeout=6) for future in futures]
                        require(all('[DONE]' not in r['body'] and (r['transport_error'] or r['status'] in (500, 502, 504)) for r in results), 'disconnect returned success')
                    report['cases'].append({'name': 'proxy disconnect', 'iteration': iteration, 'count': 8,
                                            'worker_pid': worker_pid, 'release_seconds': seconds, 'backend': backend.snapshot()})
                    print(f'PASS proxy disconnect {iteration + 1}: same live worker, 8 sockets closed in {seconds:.3f}s', flush=True)
                    restart_name = f'proxy-restart-{iteration}'
                    restarted = processes.start(restart_name, [str(args.build_dir / 'proxy-runner'), '--config',
                                                str(state / 'proxy-config.json'), '-v3', '--disable-ton',
                                                str(state / 'fake-ton-config.json')], cwd=state)
                    proxy_pid = restarted.pid
                    owned_pids.append(proxy_pid)
                    smoke.wait_ready(processes, lambda: smoke.models_ready(ports['client_http']), 60)
                    normal(f'success after proxy restart {iteration + 1}')
            finally:
                os.kill(launcher.pid, signal.SIGCONT)
                # Let the supervisor poll/reap its killed proxy before its own
                # cleanup. Signalling it immediately here can interrupt waitpid
                # and leave a zombie process group that killpg rejects on macOS.
                launcher.wait(timeout=15)
                processes.children.remove(('launcher', launcher))
            report['monitor_retried'] = backend.snapshot()['models_calls'] >= 2
            report['passed'] = True
    except BaseException as exc:
        report['error'] = str(exc)
        raise
    finally:
        backend.stop()
        if (state / 'processes.json').exists():
            owned_pids.extend(json.loads((state / 'processes.json').read_text())['processes'].values())
        lingering = [pid for pid in owned_pids if smoke.group_alive(pid)]
        report['cleanup'] = {'remaining_process_groups': lingering, 'backend': backend.snapshot()}
        try:
            require(not lingering, f'process groups remain: {lingering}')
            smoke.check_ports([backend_port, *ports.values()])
            report['cleanup']['ports_released'] = True
        except BaseException as exc:
            report['passed'] = False
            report['cleanup']['error'] = str(exc)
            raise
        finally:
            (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=ROOT / 'build/local')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--skip-build', action='store_true')
    args = parser.parse_args()
    args.build_dir = args.build_dir.resolve()
    if args.output_dir:
        output = args.output_dir.resolve()
        output.mkdir(parents=True, exist_ok=False)
    else:
        output = Path(tempfile.mkdtemp(prefix='cocoon-cancel-'))
    output.chmod(0o700)
    print(f'Artifacts: {output}', flush=True)
    sources = ['CMakeLists.txt', 'boost-http/http-client.cpp', 'boost-http/http-client.h',
               'runners/worker/WorkerRunner.cpp', 'runners/worker/WorkerRunner.h',
               'runners/worker/WorkerProxyConnection.cpp', 'runners/worker/WorkerProxyConnection.h',
               'runners/worker/WorkerRunningRequest.cpp', 'runners/worker/WorkerRunningRequest.hpp',
               'runners/worker/WorkerUplinkMonitor.cpp', 'runners/worker/WorkerUplinkMonitor.h',
               'test/test-http-cancel.cpp', 'test/test-worker-cancellation.py']
    (output / 'source-sha256.json').write_text(json.dumps(
        {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}, indent=2) + '\n')
    if not args.skip_build:
        smoke.build(output, 'build-cancellation', ['cmake', '--build', str(args.build_dir), '--target',
                    'runners', 'test-http-cancel', 'test-http-client', '-j4'], 1800)
    for binary in ('test-http-cancel', 'test-http-client'):
        smoke.build(output, binary, [str(args.build_dir / binary)], 30)
    run(args, output)
    print(f'PASS cancellation acceptance; artifacts: {output}', flush=True)


if __name__ == '__main__':
    main()
