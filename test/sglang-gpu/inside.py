#!/usr/bin/env python3
"""Acceptance driver inside the head's dev underlay container.

The outer controller owns the two containers and performs the requested remote
member fault after observing fault-request.json. No test control TCP service.
"""
import argparse
import concurrent.futures
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('worker_suite', ROOT / 'test/test-pipeline-worker.py')
s9 = importlib.util.module_from_spec(spec); spec.loader.exec_module(s9)
smoke, require, wait = s9.smoke, s9.require, s9.wait


def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.replace(path)


def health(head):
    conn = s9.group.local.UnixHTTP(head.control('status')['health_socket'])
    try:
        conn.request('GET', '/health'); r = conn.getresponse()
        require(r.status == 200, 'backend helper unhealthy')
        return json.loads(r.read())
    finally:
        conn.close()


def metrics(head):
    namespace = head.control('status')['group']['network']['namespace']
    code = 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:30000/metrics",timeout=3).read().decode())'
    text = subprocess.check_output(['ip', 'netns', 'exec', namespace, 'python3', '-c', code], text=True, timeout=5)
    values = {}
    for line in text.splitlines():
        if line.startswith(('sglang:num_running_reqs{', 'sglang:num_queue_reqs{', 'sglang:num_used_tokens{', 'sglang:token_usage{')):
            name = line.split('{')[0]
            values.setdefault(name, []).append(float(line.rsplit(' ', 1)[1]))
    return values


def idle(head, timeout=40):
    # Pinned SGLang refreshes idle gauges every 30 seconds. This bound is
    # observation latency, not a claim about the time of actual KV release.
    def check():
        h = health(head)
        m = metrics(head)
        names = ('sglang:num_running_reqs', 'sglang:num_queue_reqs', 'sglang:num_used_tokens', 'sglang:token_usage')
        if h['active_requests'] == 0 and all(k in m and all(v == 0 for v in m[k]) for k in names):
            return {'helper': h, 'metrics': m}
    return wait(check, timeout)


def member_resources(output, phase):
    request_id = uuid.uuid4().hex
    save(output/'member-resource-request.json', {'id':request_id,'phase':phase})
    def reply():
        path = output/'member-resource-response.json'
        if path.exists():
            value = json.loads(path.read_text())
            return value if value['id'] == request_id else None
    result = wait(reply, 60)
    require(result['passed'], 'member KV resource check failed: ' + str(result))
    return result


def success(result, stream, completion):
    require(result['status'] == 200 and not result['transport_error'], f'failed inference: {result}')
    if stream:
        events = [x[6:] for x in result['body'].splitlines() if x.startswith('data: ')]
        require(events.count('[DONE]') == 1 and events[-1] == '[DONE]', 'wrong SSE terminal')
        values = [json.loads(e) for e in events[:-1]]
        require(all('error' not in v for v in values), 'SSE error')
        choices = [c for v in values for c in v['choices']]
        require(sum(c.get('finish_reason') in ('length', 'stop') for c in choices) == 1, 'wrong finish count')
        content = ''.join(c.get('text', c.get('delta', {}).get('content', '')) or '' for c in choices)
        usages = [v['usage'] for v in values if v.get('usage')]
        require(len(usages) == 1, 'usage missing/duplicated')
        usage = usages[0]
        require(len(result['read_times']) > 1 and result['read_times'][-1] - result['read_times'][0] > .05, 'buffered SSE')
    else:
        value = json.loads(result['body'])
        require('error' not in value and len(value['choices']) == 1, 'invalid JSON completion')
        c = value['choices'][0]
        require(c['finish_reason'] in ('stop', 'length'), 'invalid finish reason')
        content = c['text'] if completion else c['message']['content']
        usage = value['usage']
    require(isinstance(content, str) and content.strip(), 'empty generated content')
    require(usage['prompt_tokens'] > 0 and usage['completion_tokens'] > 0, 'missing actual token usage')
    require(usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'], 'wrong total tokens')
    # This pinned SGLang returns null details with radix cache disabled.
    require((usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0) == 0, 'unexpected prefix cache')
    require(usage['total_cost'] == 2 * usage['total_tokens'], 'wrong tariff')
    return usage


def run(args):
    output = args.output_dir
    output.mkdir(exist_ok=True, parents=True)
    head = s9.ExternalNode(args.head_run)
    smoke.MODEL = s9.MODEL = args.model
    report = {'passed': False, 'model': args.model, 'cases': []}
    ports = {}
    try:
        def ready(previous_epoch=None):
            status = head.status()
            require(status.get('state') != 'FAILED', 'agent failed: ' + str(status.get('failure')))
            return status.get('group_ready') and status.get('epoch') != previous_epoch
        print('Waiting for SGLang PP warmup:', args.model, flush=True)
        wait(ready, 1200)
        print('PASS full-model warmup', flush=True)
        initial = head.control('status')
        require(initial['profile'].startswith('sglang-'), 'not the SGLang agent')
        require(initial['hardware_attested'] is False, 'dev accidentally claims attestation')
        offset, ports, _ = smoke.choose_ports()
        with smoke.Processes(output, grace=10) as processes:
            processes.start('launcher', [sys.executable, '-u', str(ROOT / 'scripts/cocoon-launch'), '--local-all',
                '--skip-build', '--build-dir', str(args.build_dir), '--local-run-dir', str(output / 'stack'),
                '--local-port-offset', str(offset), '--local-backend', '127.0.0.1:18080', '--model', args.model])
            smoke.wait_ready(processes, lambda: smoke.models_ready(ports['client_http']), 60)
            require(smoke.request(ports['worker_http'], '/request/change_coefficient?coefficient=1')['status'] == 200, 'cannot set test tariff')
            smoke.wait_ready(processes, lambda: [w['coefficient'] for w in smoke.statistics(ports)['proxy']['worker_connections']] == [1000], 5)
            smoke.wait_ready(processes, lambda: smoke.encryption_ready(ports['proxy_http']), 45)
            require(len(smoke.statistics(ports)['proxy']['worker_connections']) == 1, 'group advertises multiple workers')
            key = output / 'client-key.bin'; key.write_bytes(os.urandom(32)); key.chmod(0o600)

            def payload(stream=False, completion=False, count=24, timeout=30):
                return {'model': args.model, 'stream': stream, 'max_tokens': count, 'temperature': 0, 'timeout': timeout,
                        **({'prompt': 'Write a short story about a friendly robot.'} if completion else {
                            'messages': [{'role': 'user', 'content': 'Write a short story about a friendly robot.'}],
                            'chat_template_kwargs': {'enable_thinking': False}})}

            def paid(body, stream=False, completion=False, encrypted=False):
                before = smoke.statistics(ports)
                result = s9.request(ports['client_http'], '/v1/completions' if completion else '/v1/chat/completions',
                    smoke.crypt(args, key, body) if encrypted else body)
                if encrypted: result = smoke.decrypt_response(args, key, result, stream)
                usage = success(result, stream, completion)
                accounting = s9.accounting(ports, before, True, tokens=usage['total_tokens'])
                clean = idle(head)
                return {'usage': usage, 'accounting': accounting, 'resources': clean,
                        'stream': stream, 'completion': completion, 'encrypted': encrypted,
                        'seconds': result['seconds']}

            for encrypted in (False, True):
                for completion in (False, True):
                    for stream in (False, True):
                        record = paid(payload(stream, completion), stream, completion, encrypted)
                        report['cases'].append({'name': 'Cocoon text API', **record})
                        print('PASS Cocoon API', stream, completion, encrypted, flush=True)
            long = payload(count=2)
            long['messages'][0]['content'] = ' a' * 2050
            record = paid(long)
            require(record['usage']['prompt_tokens'] > 2048, 'long prefill did not cross the old chunk boundary')
            report['cases'].append({'name': 'long prefill without KV leak', **record})

            # Real gate disconnect, without implying external client -> TL cancellation.
            before_helper = health(head)
            import http.client
            conn = http.client.HTTPConnection('127.0.0.1', 18080, timeout=10)
            body = payload(True, count=512); body.pop('timeout'); body['ignore_eos'] = True
            conn.request('POST', '/v1/chat/completions', json.dumps(body), {s9.ID: 'gpu-disconnect', s9.TIMEOUT: '60', 'Content-Type': 'application/json'})
            response = conn.getresponse(); require(response.status == 200, 'direct stream failed')
            require(response.read1(1024), 'no direct streamed data')
            active = wait(lambda: (m if any(v > 0 for v in m.get('sglang:num_used_tokens', [])) else None)
                          if (m := metrics(head)) else None, 10)
            member_active = member_resources(output, 'active')
            response.close(); conn.close()
            cleaned = idle(head)
            member_clean = member_resources(output, 'idle')
            require(cleaned['helper']['cancelled'] > before_helper['cancelled'], 'helper did not cancel')
            require(head.control('status')['epoch'] == initial['epoch'], 'ordinary cancellation restarted group')
            report['cases'].append({'name': 'disconnect releases real KV slots', 'resources': cleaned,
                                   'head_active':active,'member_active':member_active,'member_idle':member_clean})

            before = smoke.statistics(ports)
            body = payload(True, count=512, timeout=.8)
            body['messages'][0]['content'] = 'Write a detailed story of at least 2000 words about a friendly robot exploring a new planet.'
            timed = s9.request(ports['client_http'], body=body)
            report['deadline_response'] = timed
            require(timed['transport_error'] and '[DONE]' not in timed['body'], 'deadline falsely succeeded: ' + str(timed))
            report['cases'].append({'name': 'Cocoon deadline', 'accounting': s9.accounting(ports, before, False),
                                   'resources': idle(head), 'member_resources':member_resources(output, 'idle')})
            report['cases'].append({'name': 'paid request after cancellation', **paid(payload())})

            # The outer controller kills only the current member backend. A
            # marker is written only after real encrypted SSE has begun.
            old = head.control('status')
            before = smoke.statistics(ports)
            event = threading.Event()
            body = payload(True, count=512, timeout=60)
            body['messages'][0]['content'] = 'Write a detailed story of at least 2000 words about a friendly robot exploring a new planet.'
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                pending = pool.submit(s9.request, ports['client_http'], body=smoke.crypt(args, key, body),
                    on_part=lambda part: event.set() if b'data: ' in part else None)
                require(event.wait(10), 'fault stream did not start')
                save(output / 'fault-request.json', {'op': 'kill-member-backend', 'epoch': old['epoch']})
                result = pending.result(timeout=30)
            result = smoke.decrypt_response(args, key, result, True)
            require(result['transport_error'] and '[DONE]' not in result['body'], 'member loss falsely succeeded')
            failed_accounting = s9.accounting(ports, before, False)
            def disabled():
                s = smoke.statistics(ports)
                return not s['worker']['status']['enabled'] and all(not w['enabled'] for w in s['proxy']['worker_connections'])
            wait(disabled, 10)
            require(s9.request(18080, '/v1/models')['status'] == 503, 'gate ready during restart')
            require(s9.request(18080, body=payload(), headers={s9.ID:'recovery',s9.TIMEOUT:'10'})['status'] == 503, 'inference admitted during restart')
            wait(lambda: ready(old['epoch']), 1200)
            new = head.control('status')
            require(new['process']['pid'] != old['process']['pid'], 'old backend reused')
            require(not s9.group.local.alive(old['process']['pgid'], group=True), 'old process group survived')
            require(not Path(old['backend_socket']).exists(), 'old socket survived')
            smoke.wait_ready(processes, lambda: smoke.models_ready(ports['client_http']), 20)
            report['cases'].append({'name': 'member failure and paid recovery', 'failure_accounting': failed_accounting,
                'old_epoch': old['epoch'], 'new_epoch': new['epoch'], **paid(payload())})
        report['requests_passed'] = True
        require(all(not s9.group.local.alive(proc.pid, group=True) for _, proc in processes.children), 'Cocoon process survived')
        report['passed'] = True
    except BaseException as exc:
        report['error'] = str(exc)
        raise
    finally:
        save(output / 'result.json', report)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--head-run', type=Path, required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--build-dir', type=Path, default=Path('/work/build'))
    run(p.parse_args())
