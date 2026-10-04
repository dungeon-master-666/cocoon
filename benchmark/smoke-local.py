#!/usr/bin/env python3
"""One-command native Cocoon smoke/fault test, with synthetic input and fake TON."""

import argparse
import base64
import hashlib
import subprocess
import errno
import http.client
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from local_processes import Processes, check_ports, group_alive, local_ports

SCENARIOS = ('normal', 'delay-headers', 'delay-body', 'http-error', 'hang',
             'disconnect-before-headers', 'disconnect-after-headers',
             'disconnect-mid-stream', 'incomplete-json', 'incomplete-sse',
             'invalid-json-tail', 'empty-json', 'json-error', 'sse-error', 'malformed-sse',
             'incomplete-event', 'disconnect-after-usage', 'disconnect-after-done', 'duplicate-done',
             'http-client-error', 'http-text-error', 'empty-http-error', 'no-content')
MODEL = 'Qwen/Qwen3-8B'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def request(port, path, payload=None, timeout=8):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    started = time.monotonic()
    result = {'status': None, 'body': '', 'transport_error': None}
    try:
        conn.request('POST' if payload is not None else 'GET', path,
                     json.dumps(payload) if payload is not None else None,
                     {'Content-Type': 'application/json'})
        response = conn.getresponse()
        result.update(status=response.status, content_type=response.getheader('Content-Type'))
        result['headers_seconds'] = time.monotonic() - started
        parts = []
        result['read_times'] = []
        try:
            while True:
                part = response.read1(4096)
                if not part:
                    break
                parts.append(part)
                result['read_times'].append(time.monotonic() - started)
        except http.client.IncompleteRead as exc:
            parts.append(exc.partial)
            result['transport_error'] = type(exc).__name__
        finally:
            result['body'] = b''.join(parts).decode('utf-8')
    except (OSError, http.client.HTTPException) as exc:
        result['transport_error'] = f'{type(exc).__name__}: {exc}'
    finally:
        conn.close()
    result['seconds'] = time.monotonic() - started
    return result


def wait_ready(processes, probe, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        processes.check()
        if probe():
            return
        time.sleep(0.1)
    raise RuntimeError(f'readiness timed out after {timeout}s')


def models_ready(port):
    result = request(port, '/v1/models', timeout=1)
    if result['status'] != 200 or result['transport_error']:
        return False
    data = json.loads(result['body'])
    return any(model['id'].split('@')[0] == MODEL and model.get('workers') for model in data['data'])


def assert_usage(usage):
    for name, expected in [('prompt_tokens', 34), ('completion_tokens', 100), ('total_tokens', 134)]:
        require(usage[name] == expected, f'incorrect {name}: {usage}')
    require(usage['prompt_tokens_details']['cached_tokens'] == 11, 'cached usage lost')
    require(usage['completion_tokens_details']['reasoning_tokens'] == 10, 'reasoning usage lost')
    require(usage['total_cost'] == 268, 'nonzero test tariff or usage cost lost')


def assert_success(result, stream, path):
    require(result['status'] == 200 and not result['transport_error'], f'HTTP failed: {result}')
    if stream:
        require(result['content_type'].startswith('text/event-stream'), 'SSE content type lost')
        events = [line[6:] for line in result['body'].splitlines() if line.startswith('data: ')]
        require(events.count('[DONE]') == 1 and events[-1] == '[DONE]', 'missing/duplicate SSE terminal')
        times = result['read_times']
        require(len(times) > 1 and times[-1] - times[0] >= 0.15,
                'SSE was buffered until completion instead of streaming')
        values = [json.loads(event) for event in events[:-1]]
        choices = [choice for value in values for choice in value['choices']]
        content = ''.join(choice.get('delta', {}).get('content', choice.get('text', '')) for choice in choices)
        require(content == 'x' * 24, f'incorrect streamed content: {content!r}')
        require(sum(choice['finish_reason'] == 'stop' for choice in choices) == 1, 'missing/duplicate finish')
        usages = [value['usage'] for value in values if value.get('usage')]
        require(len(usages) == 1, 'missing/duplicate usage')
        assert_usage(usages[0])
    else:
        value = json.loads(result['body'])
        choice = value['choices'][0]
        content = choice['text'] if path == '/v1/completions' else choice['message']['content']
        require(content == 'local smoke response' and choice['finish_reason'] == 'stop', 'incorrect JSON answer')
        assert_usage(value['usage'])


def statistics(ports):
    return {role: json.loads(request(ports[role + '_http'], '/jsonstats')['body'])
            for role in ('worker', 'proxy', 'client')}


def encryption_ready(port):
    page = request(port, '/stats')['body']
    marker = '<h1>KNOWN PRIVATE KEYS</h1>'
    return marker in page and FAKE_PUBLIC_KEY.upper() in page.split(marker, 1)[1].split('</table>', 1)[0]


def accounting_values(snapshot):
    return {
        'worker_tokens': snapshot['worker']['stats']['total_adjusted_tokens_used'][0],
        'proxy_tokens': snapshot['proxy']['stats']['total_adjusted_tokens_used'][0],
        'worker_payment': sum(p['earned_tokens_max_known'] for p in snapshot['worker']['proxies']),
        'client_payment': sum(p['tokens_used_proxy_max'] for p in snapshot['client']['proxies']),
        'proxy_worker_balance': sum(p['earned_tokens'] for p in snapshot['proxy']['workers']),
        'proxy_client_balance': sum(p['used_tokens'] for p in snapshot['proxy']['clients']),
    }


def check_accounting(ports, before, success):
    expected = 134 if success else 0
    old = accounting_values(before)
    deadline = time.monotonic() + 5
    while True:
        after = statistics(ports)
        deltas = {key: value - old[key] for key, value in accounting_values(after).items()}
        counters = {role: {key: after[role]['stats'][key][0] - before[role]['stats'][key][0]
                           for key in ('queries', 'success', 'failed')} for role in before}
        target = {'queries': 1, 'success': int(success), 'failed': int(not success)}
        if all(value == expected for value in deltas.values()) and all(value == target for value in counters.values()):
            # Include a later snapshot: duplicate/late completion must not change
            # accounting after the first terminal result.
            time.sleep(0.15)
            stable = statistics(ports)
            require(accounting_values(stable) == accounting_values(after), 'late billing update')
            for role in before:
                require(all(stable[role]['stats'][key][0] == after[role]['stats'][key][0] for key in target), 'duplicate completion')
            require(all(c['running_queries'] == 0 and c['reserved_tokens'] == 0 for c in stable['proxy']['clients']), 'request reservation leaked')
            return {'token_deltas': deltas, 'terminal_counters': counters}
        if time.monotonic() >= deadline:
            raise AssertionError(f'accounting/terminal mismatch: expected {expected}, got {deltas}; counters={counters}')
        time.sleep(0.1)


# Public fixture from KeyManagerRunner::add_static_private_key, only --fake-ton.
FAKE_PUBLIC_KEY = base64.b64decode('+2fQ/NM48g4NSVfZ6CrcEB0uNROkSKOrRgUu4biMWBg=').hex()


def crypt(args, key, value, decrypt=False):
    result = subprocess.run([str(args.build_dir / 'encrypt-message'), '-k', str(key), '-p', FAKE_PUBLIC_KEY,
                             *(['-d'] if decrypt else [])], input=json.dumps(value), text=True,
                            capture_output=True, timeout=5, check=True)
    require('failed to decrypt' not in result.stderr, 'encrypted response failed authentication')
    return json.loads(result.stdout)


def decrypt_response(args, key, result, stream):
    result = dict(result)
    if stream:
        lines = []
        for line in result['body'].splitlines():
            if line.startswith('data: ') and line != 'data: [DONE]':
                value = json.loads(line[6:])
                require(value.get('is_encrypted') == 'v1', 'unencrypted backend event')
                line = 'data: ' + json.dumps(crypt(args, key, value, True))
            lines.append(line)
        result['body'] = '\n'.join(lines)
    elif result['body']:
        value = json.loads(result['body'])
        require(value.get('is_encrypted') == 'v1', 'unencrypted backend body')
        result['body'] = json.dumps(crypt(args, key, value, True))
    return result


def choose_ports():
    for _ in range(100):
        offset = random.randrange(2000, 45000)
        ports = local_ports(offset)
        backend = 8000 + offset
        try:
            check_ports([backend, *ports.values()])
            return offset, ports, backend
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    raise RuntimeError('cannot find unused local ports')


def run_case(args, scenario, output, backend_binary):
    case_dir = output / scenario
    case_dir.mkdir()
    offset, ports, backend_port = choose_ports()
    report = {'scenario': scenario, 'ports': ports, 'backend_port': backend_port, 'requests': []}
    state = case_dir / 'stack'
    owned_pids = []
    try:
        with Processes(case_dir, grace=10) as processes:
            backend = processes.start('backend', [str(backend_binary), '--listen', f'127.0.0.1:{backend_port}',
                                      '--scenario', scenario, '--delay', '750ms', '--chunks', '3', '--bytes', '8',
                                      '--chunk-delay', '100ms'])
            owned_pids.append(backend.pid)
            wait_ready(processes, lambda: request(backend_port, '/health', timeout=1)['status'] == 200, 10)
            launcher = processes.start('launcher', [sys.executable, '-u', str(ROOT / 'scripts/cocoon-launch'),
                                       '--local-all', '--skip-build', '--build-dir', str(args.build_dir),
                                       '--local-run-dir', str(state), '--local-port-offset', str(offset),
                                       '--local-backend', f'127.0.0.1:{backend_port}', '--model', MODEL])
            owned_pids.append(launcher.pid)
            wait_ready(processes, lambda: models_ready(ports['client_http']), args.startup_timeout)
            # --local-all starts with a free worker. Use its existing admin API
            # so successful billing is nonzero and failure checks are meaningful.
            changed = request(ports['worker_http'], '/request/change_coefficient?coefficient=1')
            require(changed['status'] == 200 and 'coefficient set to 1' in changed['body'], 'test tariff not applied')
            wait_ready(processes, lambda: [w['coefficient'] for w in statistics(ports)['proxy']['worker_connections']] == [1000], 5)
            report['worker_coefficient'] = 1000
            success = scenario in ('normal', 'delay-headers', 'delay-body')
            cases = [(False, '/v1/chat/completions'), (True, '/v1/chat/completions')]
            if scenario == 'normal':
                cases += [(False, '/v1/completions'), (True, '/v1/completions')]
            if not success:
                cases = [(scenario not in ('incomplete-json', 'invalid-json-tail', 'empty-json', 'json-error'), '/v1/chat/completions')]
            cases = [(stream, path, False) for stream, path in cases]
            if scenario in ('normal', 'http-error', 'http-text-error', 'json-error', 'sse-error', 'disconnect-after-done'):
                # Model readiness can precede the periodic key-manager fetch.
                # /stats lists only public fingerprints and expiry timestamps.
                wait_ready(processes, lambda: encryption_ready(ports['proxy_http']), args.startup_timeout)
                cases += [(stream, path, True) for stream, path, _ in cases]
            key = case_dir / 'test-client-key.bin'
            key.write_bytes(os.urandom(32))
            key.chmod(0o600)
            for stream, path, encrypted in cases:
                before = statistics(ports)
                payload = {'model': MODEL, 'stream': stream, 'max_tokens': 128, 'timeout': 4}
                if path == '/v1/completions':
                    payload['prompt'] = 'hello'
                else:
                    payload['messages'] = [{'role': 'user', 'content': 'hello'}]
                if encrypted:
                    payload = crypt(args, key, payload)
                result = request(ports['client_http'], path, payload)
                report['requests'].append({'stream': stream, 'path': path, 'encrypted': encrypted, **result})
                processes.check()
                log = (case_dir / 'backend.log').read_text()
                require(f'request scenario={scenario} stream={str(stream).lower()}' in log,
                        'request did not reach the owned backend')
                if encrypted:
                    require('local smoke response' not in result['body'] and 'injected backend error' not in result['body'], 'plaintext response leaked')
                    result = decrypt_response(args, key, result, stream and scenario not in ('http-error', 'http-text-error'))
                if success:
                    assert_success(result, stream, path)
                    if scenario.startswith('delay-'):
                        require(result['seconds'] >= 0.7, 'backend delay was not observed')
                else:
                    require(f'fault={scenario}' in log, 'fault was not injected')
                    require(result['transport_error'] is None or
                            result['transport_error'].startswith(('IncompleteRead', 'RemoteDisconnected', 'ConnectionResetError')),
                            f'unexpected transport error / test timeout: {result}')
                    if scenario in ('http-error', 'http-client-error', 'http-text-error', 'empty-http-error'):
                        require(result['status'] == (400 if scenario == 'http-client-error' else 503), f'HTTP error status lost: {result}')
                        require(not result['transport_error'], f'complete backend HTTP error body truncated: {result}')
                        if scenario != 'empty-http-error':
                            require('injected backend error' in result['body'], f'backend error reason lost: {result}')
                    elif scenario in ('hang', 'disconnect-before-headers', 'no-content'):
                        require(result['status'] in (502, 504), f'expected upstream error: {result}')
                    else:
                        require(result['status'] in (500, 502, 504) or result['transport_error'],
                                f'incomplete/error response completed successfully: {result}')
                        require('[DONE]' not in result['body'], f'failed response published success marker: {result}')
                        if scenario in ('json-error', 'sse-error'):
                            require('injected backend error' in result['body'], f'backend error details lost: {result}')
                report['requests'][-1]['accounting'] = check_accounting(ports, before, success)
            report['passed'] = True
    except BaseException as exc:
        report['passed'] = False
        report['error'] = str(exc)
        raise
    finally:
        manifest = state / 'processes.json'
        if manifest.exists():
            owned_pids.extend(json.loads(manifest.read_text())['processes'].values())
        # The check is part of success criteria, not merely best-effort cleanup.
        lingering = [pid for pid in owned_pids if group_alive(pid)]
        report['owned_pids'] = owned_pids
        report['cleanup'] = {'remaining_process_groups': lingering}
        try:
            require(not lingering, f'process groups remain: {lingering}')
            check_ports([backend_port, *ports.values()])
            report['cleanup']['ports_released'] = True
        except BaseException as exc:
            report['passed'] = False
            report['cleanup']['error'] = str(exc)
            raise
        finally:
            (case_dir / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'PASS {scenario}: responses, billing, single completion and cleanup', flush=True)


def build(output, name, command, timeout):
    with Processes(output) as processes:
        proc = processes.start(name, command, cwd=ROOT)
        code = proc.wait(timeout=timeout)
        require(code == 0, f'{name} failed with {code}; see {output / (name + ".log")}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=Path(os.environ.get('BUILD_DIR', ROOT / 'build/local')))
    parser.add_argument('--skip-build', action='store_true', help='Use existing Cocoon binaries; still build the test backend')
    parser.add_argument('--scenario', choices=(*SCENARIOS, 'all'), default='normal')
    parser.add_argument('--output-dir', type=Path, help='New directory for retained configs, logs and result.json')
    parser.add_argument('--startup-timeout', type=float, default=45)
    parser.add_argument('--strict-faults', action='store_true', help='Compatibility flag; all fault checks are now always strict')
    args = parser.parse_args()
    require(args.startup_timeout > 0, 'startup timeout must be positive')
    args.build_dir = args.build_dir.resolve()
    if args.output_dir:
        output = args.output_dir.resolve()
        output.mkdir(parents=True, exist_ok=False)
    else:
        output = Path(tempfile.mkdtemp(prefix='cocoon-smoke-'))
    output.chmod(0o700)
    print(f'Artifacts: {output}', flush=True)
    sources = ['CMakeLists.txt', 'benchmark/server.go', 'benchmark/server_test.go', 'benchmark/smoke-local.py',
               'boost-http/http-client.cpp', 'boost-http/http.cpp', 'boost-http/http.h',
               'runners/helpers/ValidateRequest.cpp', 'runners/helpers/ValidateRequest.h',
               'runners/worker/WorkerRunningRequest.cpp', 'runners/worker/WorkerRunningRequest.hpp',
               'runners/worker/WorkerUplinkMonitor.cpp', 'runners/client/ClientRunningRequest.cpp',
               'runners/client/ClientRunningRequest.h', 'test/test-http-client.cpp', 'test/test-answer-postprocessor.cpp']
    (output / 'source-sha256.json').write_text(json.dumps(
        {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}, indent=2) + '\n')
    try:
        if not args.skip_build:
            build(output, 'build-cocoon', [sys.executable, str(ROOT / 'scripts/cocoon-launch'), '--local-all',
                  '--build-dir', str(args.build_dir), '--just-build'], 1800)
        if not args.skip_build:
            build(output, 'build-response-tests', ['cmake', '--build', str(args.build_dir), '--target',
                  'test-http-client', 'test-answer-postprocessor', 'encrypt-message', '-j', '4'], 1800)
        for binary in ('test-http-client', 'test-answer-postprocessor'):
            build(output, binary, [str(args.build_dir / binary)], 30)
        backend_binary = output / 'backend'
        build(output, 'build-backend', ['go', 'build', '-o', str(backend_binary), str(ROOT / 'benchmark/server.go')], 120)
        selected = SCENARIOS if args.scenario == 'all' else (args.scenario,)
        for scenario in selected:
            run_case(args, scenario, output, backend_binary)
    except (Exception, SystemExit) as exc:
        print(f'FAIL: {exc}; artifacts: {output}', file=sys.stderr, flush=True)
        return exc.code if isinstance(exc, SystemExit) else 1
    print(f'PASS: {len(selected)} scenario(s); logs and reports: {output}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
