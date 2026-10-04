#!/usr/bin/env python3
"""Step 9: real Cocoon -> head gate -> simulator; native and Linux WireGuard."""
import argparse
import concurrent.futures
import contextlib
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'cocoon-simulator'
ID = 'x-cocoon-pipeline-request-id'
TIMEOUT = 'x-cocoon-pipeline-timeout-seconds'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


smoke = module('smoke', 'benchmark/smoke-local.py')
group = module('group', 'test/test-pipeline-group.py')
smoke.MODEL = MODEL
require = smoke.require


def wait(action, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = action()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError('condition timed out')


class ExternalNode:
    def __init__(self, run, agent_pid=None):
        self.run = Path(run)
        self.agent_pid = agent_pid

    def status(self):
        try:
            return json.loads((self.run / 'status.json').read_text())
        except FileNotFoundError:
            return {}

    def control(self, op='status'):
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(3)
            conn.connect(str(self.run / 'control.sock'))
            conn.sendall(json.dumps({'op': op}).encode() + b'\n')
            with conn.makefile('rb') as reader:
                return json.loads(reader.readline(32768))


def payload(stream=False, completion=False, count=2, timeout=10):
    return {'model': MODEL, 'max_tokens': count, 'stream': stream, 'timeout': timeout,
            **({'prompt': 'hello pipeline'} if completion else {'messages': [{'role': 'user', 'content': 'hello pipeline'}]})}


def request(port, path='/v1/chat/completions', body=None, headers=None, on_part=None, method=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
    result = {'status': None, 'body': '', 'transport_error': None, 'read_times': []}
    start = time.monotonic()
    parts = []
    try:
        conn.request(method or ('POST' if body is not None else 'GET'), path,
                     json.dumps(body) if body is not None else None,
                     {'Content-Type': 'application/json', **(headers or {})})
        response = conn.getresponse()
        result['status'] = response.status
        result['content_type'] = response.getheader('Content-Type')
        while True:
            part = response.read1(4096)
            if not part:
                break
            parts.append(part)
            result['read_times'].append(time.monotonic() - start)
            if on_part:
                on_part(part)
    except http.client.IncompleteRead as exc:
        parts.append(exc.partial)
        result['transport_error'] = 'IncompleteRead'
    except (OSError, http.client.HTTPException) as exc:
        result['transport_error'] = str(exc)
    finally:
        conn.close()
    result['body'] = b''.join(parts).decode()
    result['seconds'] = time.monotonic() - start
    return result


def backend_stats(node):
    status = node.control('status')
    conn = group.local.UnixHTTP(status['health_socket'])
    try:
        conn.request('GET', '/health')
        response = conn.getresponse()
        require(response.status == 200, 'backend health failed')
        return json.loads(response.read())
    finally:
        conn.close()


def accounting(ports, before, success, tokens=4, count=1):
    old = smoke.accounting_values(before)
    target = {'queries': count, 'success': count * int(success), 'failed': count * int(not success)}
    expected = tokens * count if success else 0

    def done():
        after = smoke.statistics(ports)
        delta = {k: v - old[k] for k, v in smoke.accounting_values(after).items()}
        counters = {r: {k: after[r]['stats'][k][0] - before[r]['stats'][k][0] for k in target} for r in before}
        if all(v == expected for v in delta.values()) and all(v == target for v in counters.values()):
            require(all(c['running_queries'] == 0 and c['reserved_tokens'] == 0 for c in after['proxy']['clients']), 'reservations retained')
            return {'tokens': delta, 'counters': counters}
        return None
    result = wait(done, 5)
    time.sleep(0.15)
    require(done() == result, 'late/double terminal or billing')
    return result


def success(result, stream, completion=False):
    require(result['status'] == 200 and not result['transport_error'], f'HTTP failed: {result}')
    if stream:
        events = [line[6:] for line in result['body'].splitlines() if line.startswith('data: ')]
        require(events.count('[DONE]') == 1 and events[-1] == '[DONE]', 'SSE terminal invalid')
        values = [json.loads(event) for event in events[:-1]]
        text = ''.join(c.get('text', c.get('delta', {}).get('content', '')) for v in values for c in v['choices'])
        usages = [v['usage'] for v in values if v.get('usage')]
        require(len(usages) == 1, 'missing or duplicate usage')
        usage = usages[0]
        times = result['read_times']
        require(len(times) > 1 and times[-1] - times[0] >= 0.05, 'SSE buffered')
    else:
        value = json.loads(result['body'])
        choice = value['choices'][0]
        text = choice['text'] if completion else choice['message']['content']
        require(choice['finish_reason'] == 'stop', 'missing finish reason')
        usage = value['usage']
    require(text == 'simulated reply', f'wrong content: {text}')
    require(usage['prompt_tokens'] == 2 and usage['completion_tokens'] == 2 and usage['total_tokens'] == 4, f'wrong usage: {usage}')
    require(usage['total_cost'] == 8, 'wrong nonzero tariff')


def exercise(args, output, head, member, gate_port):
    report = {'platform': platform.platform(), 'cases': [], 'passed': False}
    ports = {}
    pids = []
    state = output / 'stack'
    try:
        # A healthy local backend alone must not open the gate.
        wait(lambda: head.status().get('local_ready') and not head.status().get('group_ready'), 20)
        local = head.control('status')
        conn = group.local.UnixHTTP(local['backend_socket'])
        conn.request('GET', '/v1/models')
        response = conn.getresponse()
        require(response.status == 200, 'local backend should already be healthy')
        response.read(); conn.close()
        require(request(gate_port, '/v1/models')['status'] == 503, 'gate accepted local readiness without group')
        require(request(gate_port, body=payload(), headers={ID: 'early', TIMEOUT: '5'})['status'] == 503, 'admission open during member warmup')
        report['cases'].append({'name': 'local readiness differs from group readiness', 'passed': True})
        wait(lambda: head.status().get('group_ready') and member.status().get('group_ready'), 20)
        require(request(gate_port, '/v1/models')['status'] == 200, 'ready group not exposed')
        require(member.control('status')['gate'] is None, 'member published an inference gate')

        # Gate endpoints and metadata are a local protocol, not a backend admin API.
        for method, path in [('GET', '/health'), ('GET', '/metrics'), ('POST', '/v1/chat/completions?x=1'),
                             ('POST', '/load_lora_adapter'), ('DELETE', '/v1/models')]:
            require(request(gate_port, path, {}, method=method)['status'] == 404, 'backend admin/path escaped allowlist')
        require(request(gate_port, body=payload())['status'] == 400, 'missing local metadata accepted')
        for value in ('0', '-1', 'nan', 'inf', '121', 'garbage'):
            require(request(gate_port, body=payload(), headers={ID: 'invalid-budget', TIMEOUT: value})['status'] == 400, 'invalid local budget accepted')
        report['cases'].append({'name': 'endpoint allowlist and metadata validation', 'passed': True})

        previous = head.control('status')['gate']['accepted']
        oversized = payload()
        oversized['messages'][0]['content'] = 'x' * 8192
        rejected = request(gate_port, body=oversized, headers={ID: 'oversized', TIMEOUT: '5'})
        require(rejected['transport_error'] and head.control('status')['gate']['accepted'] == previous,
                'oversized body reached backend')
        held = []
        try:
            for index in range(2):
                conn = http.client.HTTPConnection('127.0.0.1', gate_port, timeout=5)
                body = payload(count=64); body.pop('timeout')
                conn.request('POST', '/v1/chat/completions', json.dumps(body),
                             {ID: f'slot-{index}', TIMEOUT: '10', 'Content-Type': 'application/json'})
                held.append(conn)
                wait(lambda: backend_stats(head)['active_requests'] == index + 1)
                if index == 0:
                    require(request(gate_port, body=payload(), headers={ID: 'slot-0', TIMEOUT: '5'})['status'] == 429,
                            'duplicate identity accepted despite one free capacity slot')
            wait(lambda: backend_stats(head)['active_requests'] == 2)
            for identity in ('slot-0', 'overflow'):
                require(request(gate_port, body=payload(), headers={ID: identity, TIMEOUT: '5'})['status'] == 429,
                        'capacity/duplicate active identity accepted')
            require(request(gate_port, '/v1/models')['status'] == 200, 'inference saturation blocked readiness')
        finally:
            for conn in held:
                conn.close()
        wait(lambda: backend_stats(head)['active_requests'] == 0 and head.control('status')['gate']['active_requests'] == 0, 2)
        report['cases'].append({'name': 'body limit, capacity and duplicate request identity', 'passed': True})
        for fault in ('http-error', 'oversized'):
            body = payload(); body.pop('timeout'); body['simulator'] = {'fault': fault}
            result = request(gate_port, body=body, headers={ID: fault, TIMEOUT: '5'})
            if fault == 'http-error':
                require(result['status'] == 503 and not result['transport_error'] and 'injected backend failure' in result['body'],
                        'backend HTTP status/body lost')
            else:
                require(result['transport_error'], 'output byte limit not enforced')
            wait(lambda: backend_stats(head)['active_requests'] == 0 and head.control('status')['gate']['active_requests'] == 0, 2)
        report['cases'].append({'name': 'backend HTTP error and output limit', 'passed': True})

        # Direct worker-side disconnect and deadline must cancel a silent backend,
        # release its synthetic token reservation, and preserve group readiness.
        for kind in ('disconnect', 'deadline'):
            before = backend_stats(head)
            conn = http.client.HTTPConnection('127.0.0.1', gate_port, timeout=5)
            body = payload(count=64)
            body.pop('timeout')
            conn.request('POST', '/v1/chat/completions', json.dumps(body),
                         {ID: kind, TIMEOUT: '0.2' if kind == 'deadline' else '10', 'Content-Type': 'application/json'})
            wait(lambda: backend_stats(head)['active_requests'] == 1)
            if kind == 'disconnect':
                conn.close()
            else:
                try:
                    conn.getresponse().read()
                    raise AssertionError('deadline produced complete response')
                except (OSError, http.client.HTTPException):
                    pass
                conn.close()
            wait(lambda: backend_stats(head)['active_requests'] == 0 and head.control('status')['gate']['active_requests'] == 0, 2)
            after = backend_stats(head)
            require(after['cancelled_requests'] == before['cancelled_requests'] + 1 and after['prompt_tokens_in_flight'] == 0, 'backend cancellation/quota not confirmed')
            require(head.control('status')['group_ready'], 'request cancellation killed group')
            report['cases'].append({'name': 'gate ' + kind, 'passed': True})

        offset, ports, _ = smoke.choose_ports()
        with smoke.Processes(output, grace=10) as processes:
            launcher = processes.start('launcher', [sys.executable, '-u', str(ROOT / 'scripts/cocoon-launch'),
                         '--local-all', '--skip-build', '--build-dir', str(args.build_dir), '--local-run-dir', str(state),
                         '--local-port-offset', str(offset), '--local-backend', f'127.0.0.1:{gate_port}', '--model', MODEL])
            pids.append(launcher.pid)
            smoke.wait_ready(processes, lambda: smoke.models_ready(ports['client_http']), 60)
            changed = smoke.request(ports['worker_http'], '/request/change_coefficient?coefficient=1')
            require(changed['status'] == 200, 'tariff not set')
            smoke.wait_ready(processes, lambda: [w['coefficient'] for w in smoke.statistics(ports)['proxy']['worker_connections']] == [1000], 5)
            require(len(smoke.statistics(ports)['proxy']['worker_connections']) == 1, 'pipeline is more than one worker')
            smoke.wait_ready(processes, lambda: smoke.encryption_ready(ports['proxy_http']), 45)
            key = output / 'client-key.bin'
            key.write_bytes(os.urandom(32)); key.chmod(0o600)
            for encrypted in (False, True):
                for completion in (False, True):
                    for stream in (False, True):
                        body = payload(stream, completion)
                        if encrypted:
                            body = smoke.crypt(args, key, body)
                        before = smoke.statistics(ports)
                        result = request(ports['client_http'], '/v1/completions' if completion else '/v1/chat/completions', body,
                                         # Worker must remove every client-supplied reserved header.
                                         {ID.upper(): 'bad ! identity', TIMEOUT: '0'})
                        if encrypted:
                            result = smoke.decrypt_response(args, key, result, stream)
                        success(result, stream, completion)
                        report['cases'].append({'name': 'Cocoon success', 'stream': stream, 'completion': completion,
                                                'encrypted': encrypted, 'accounting': accounting(ports, before, True)})
                        print(f'PASS Cocoon JSON/SSE success: stream={stream}, completion={completion}, encrypted={encrypted}', flush=True)
            before = smoke.statistics(ports)
            timed = request(ports['client_http'], body=payload(True, count=64, timeout=0.4))
            require(timed['transport_error'] and '[DONE]' not in timed['body'], 'worker deadline falsely completed stream')
            report['cases'].append({'name': 'Cocoon deadline', 'accounting': accounting(ports, before, False)})
            wait(lambda: backend_stats(head)['active_requests'] == 0 and head.control('status')['gate']['active_requests'] == 0, 2)

            # Both rank death and loss of the agent/control channel must fail
            # the epoch. The second case uses the real lease budget.
            for kind in ('backend crash', 'control lease loss'):
                old_head, old_member = head.control('status'), member.control('status')
                old_epoch = old_head['epoch']
                before = smoke.statistics(ports)
                streaming = [threading.Event(), threading.Event()]
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    pending = [pool.submit(request, ports['client_http'],
                                           body=smoke.crypt(args, key, payload(True, count=64, timeout=15)) if index else payload(True, count=64, timeout=15),
                                           on_part=lambda part, event=event: event.set() if b'data: ' in part else None)
                               for index, event in enumerate(streaming)]
                    wait(lambda: all(event.is_set() for event in streaming) and head.control('status')['gate']['active_requests'] == 2, 3)
                    failed_at = time.monotonic()
                    member_pid = member.proc.pid if hasattr(member, 'proc') else member.agent_pid
                    os.kill(old_member['process']['pid'] if kind == 'backend crash' else member_pid,
                            signal.SIGKILL if kind == 'backend crash' else signal.SIGSTOP)
                    try:
                        results = [future.result(timeout=10) for future in pending]
                        failure_seconds = time.monotonic() - failed_at
                    finally:
                        if kind == 'control lease loss':
                            os.kill(member_pid, signal.SIGCONT)
                results[1] = smoke.decrypt_response(args, key, results[1], True)
                require(all(r['transport_error'] and '[DONE]' not in r['body'] for r in results), 'member loss falsely completed a stream')
                require(failure_seconds < 6, 'member loss did not cancel promptly')
                accounted = accounting(ports, before, False, count=2)
                # Observe disabled while the new epoch is warming, not just an empty
                # old process. Models/worker advertisement must reflect the gate.
                def disabled():
                    snapshot = smoke.statistics(ports)
                    return (not snapshot['worker']['status']['enabled'] and
                            all(not w['enabled'] for w in snapshot['proxy']['worker_connections']))
                wait(disabled, 5)
                require(request(gate_port, '/v1/models')['status'] == 503, 'gate ready during recovery')
                require(request(gate_port, body=payload(), headers={ID: 'during-recovery', TIMEOUT: '5'})['status'] == 503,
                        'gate admitted inference during recovery')
                require(head.control('status')['gate']['active_requests'] == 0, 'old epoch requests retained')
                wait(lambda: head.status().get('group_ready') and head.status().get('epoch') != old_epoch and member.status().get('group_ready'), 30)
                new_head = head.control('status')
                require(new_head['process']['pid'] != old_head['process']['pid'], 'backend reused across epochs')
                for old in (old_head, old_member):
                    require(not group.local.alive(old['process']['pgid'], group=True), 'old backend process group survived')
                    require(not Path(old['backend_socket']).exists(), 'old backend socket survived')
                fresh = backend_stats(head)
                require(fresh['active_requests'] == 0 and fresh['prompt_tokens_in_flight'] == 0 and fresh['completed_requests'] == 1, 'old requests/cache counters survived new backend')
                smoke.wait_ready(processes, lambda: smoke.models_ready(ports['client_http']), 10)
                after_before = smoke.statistics(ports)
                recovered = request(ports['client_http'], body=payload())
                success(recovered, False)
                report['cases'].append({'name': kind + ' and recovery', 'seconds': failure_seconds, 'cancelled_streams': 2,
                                        'old_epoch': old_epoch, 'new_epoch': new_head['epoch'], 'failure_accounting': accounted,
                                        'recovery_accounting': accounting(ports, after_before, True)})
                print(f'PASS {kind}: both streams aborted, zero billing, disabled worker, new epoch and paid recovery', flush=True)
                require(smoke.statistics(ports)['worker']['stats']['active_requests'] == 0, 'worker requests retained')
        report['requests_passed'] = True
    except BaseException as exc:
        report['error'] = str(exc)
        raise
    finally:
        if (state / 'processes.json').exists():
            pids += list(json.loads((state / 'processes.json').read_text())['processes'].values())
        report['cleanup'] = {'remaining_process_groups': [pid for pid in pids if smoke.group_alive(pid)]}
        try:
            require(not report['cleanup']['remaining_process_groups'], 'Cocoon processes survived')
            if ports:
                smoke.check_ports(ports.values())
            report['cleanup']['cocoon_ports_released'] = True
        except BaseException as exc:
            report['passed'] = False
            report['cleanup']['error'] = str(exc)
            raise
        finally:
            (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    head.control('stop')
    wait(lambda: head.status().get('state') == 'STOPPED' and member.status().get('state') == 'STOPPED', 15)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=ROOT / 'build/local')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--no-build', action='store_true')
    parser.add_argument('--network', action='store_true', help='Run real WireGuard in a disposable Linux VM as root')
    parser.add_argument('--external-head', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--external-member', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--external-member-pid', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--gate-port', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.build_dir = args.build_dir.resolve()
    if args.external_head:
        exercise(args, args.output_dir, ExternalNode(args.external_head), ExternalNode(args.external_member, args.external_member_pid), args.gate_port)
        return
    output = args.output_dir.resolve() if args.output_dir else Path(tempfile.mkdtemp(prefix='cp9-', dir='/tmp'))
    if args.output_dir:
        output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o711 if args.network else 0o700)
    print(f'Artifacts: {output}', flush=True)
    if not args.no_build:
        smoke.build(output, 'build', ['cmake', '--build', str(args.build_dir), '--target', 'pipeline-agent',
                   'pipeline-agent-dev', 'test-pipeline-profile', 'runners', 'encrypt-message', 'cocoon-subst', 'router', '-j4'], 1800)
    smoke.build(output, 'profiles', [str(args.build_dir / 'pipeline/test-pipeline-profile')], 15)
    sources = [p for d in ('pipeline', 'runners/worker') for p in (ROOT / d).glob('*') if p.is_file() and p.suffix in ('.cpp', '.h', '.hpp', '.py')]
    sources += [Path(__file__), ROOT / 'boost-http/pipeline-metadata.h', ROOT / 'pipeline/CMakeLists.txt']
    (output / 'source-sha256.json').write_text(json.dumps({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}, indent=2) + '\n')
    gate_port = group.port()
    if args.network:
        require(sys.platform == 'linux' and os.geteuid() == 0, 'network tests need root in a disposable Linux VM')
        network = module('network', 'test/test-pipeline-network.py')
        network.BIN, network.RESULTS = args.build_dir / 'pipeline', output
        root_firewall = network.command(['nft', '-j', 'list', 'ruleset'])
        with network.Lab(start=False) as lab:
            member = network.Node(lab, 1, simulator={'startup_delay_ms': 1000, 'warmup_delay_ms': 1200}); lab.nodes.append(member)
            head = network.Node(lab, 0, gate={'listen_port': gate_port}, simulator={'token_delay_ms': 100}); lab.nodes.append(head)
            subprocess.run(['nsenter', '--net=/run/netns/' + lab.ns[0], sys.executable, str(Path(__file__).resolve()),
                            '--build-dir', str(args.build_dir), '--external-head', str(head.run), '--external-member', str(member.run),
                            '--gate-port', str(gate_port), '--external-member-pid', str(member.proc.pid), '--output-dir', str(output)], check=True, timeout=300)
            for node in (head, member):
                node.proc.wait(timeout=10); node.clean()
            # The only gate listener is loopback in the head underlay namespace.
            for node in (head, member):
                require(not list(node.run.rglob('*.sock')), 'agent sockets retained')
        require(network.command(['nft', '-j', 'list', 'ruleset']) == root_firewall, 'root firewall changed')
        require(all(not Path('/run/netns', n).exists() for n in lab.ns), 'fixture namespace retained')
    else:
        group.BIN, group.RESULTS = args.build_dir / 'pipeline', output
        endpoint = group.port()
        with group.Node(1, endpoint, simulator={'startup_delay_ms': 1000, 'warmup_delay_ms': 1200}) as member, group.Node(0, endpoint, gate={'listen_port': gate_port}, simulator={'token_delay_ms': 100}) as head:
            exercise(args, output, head, member, gate_port)
            for node in (head, member):
                node.proc.wait(timeout=8); node.clean()
        smoke.check_ports([gate_port, endpoint])
    report_path = output / 'result.json'
    report = json.loads(report_path.read_text())
    require(report.get('requests_passed'), 'request acceptance did not finish')
    report['passed'] = True
    report['cleanup']['agents_and_backend_sockets_released'] = True
    report['cleanup']['wireguard_namespaces_and_firewall_checked'] = args.network
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(f'PASS pipeline worker acceptance ({"WireGuard" if args.network else "native"}); artifacts: {output}', flush=True)


if __name__ == '__main__':
    main()
