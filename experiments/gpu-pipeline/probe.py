"""API fixtures executed in the head namespace; no API is exposed on the host."""
import json
from pathlib import Path
import statistics
import sys
import time
import urllib.request

BASE = 'http://127.0.0.1:30000'
FIXTURES = ['Reply with exactly OK.', 'What is 2 + 2? Answer with just the number.',
            'Continue the sequence, using commas: 1, 2, 3, 4,']


def payload(prompt, **extra):
    return dict(model='pilot', messages=[{'role': 'user', 'content': prompt}],
                temperature=0, seed=42, max_tokens=64,
                chat_template_kwargs={'enable_thinking': False}, **extra)


def request(body, timeout=90):
    req = urllib.request.Request(BASE + '/v1/chat/completions',
                                 json.dumps(body).encode(), {'Content-Type': 'application/json'})
    return urllib.request.urlopen(req, timeout=timeout)


def stream(body, cancel=False, fault=False):
    start = time.monotonic()
    texts, arrivals, usage, finish, done = [], [], None, None, False
    error = None
    try:
        with request(dict(body, stream=True, stream_options={'include_usage': True}), timeout=60) as response:
            for line in response:
                if not line.startswith(b'data: '):
                    continue
                data = line[6:].strip()
                if data == b'[DONE]':
                    done = True
                    break
                event = json.loads(data)
                if event.get('error'):
                    error = event['error']
                if event.get('usage'):
                    usage = event['usage']
                for choice in event.get('choices', []):
                    text = choice.get('delta', {}).get('content')
                    if text:
                        texts.append(text)
                        arrivals.append(time.monotonic())
                        if fault:
                            Path('/artifacts/fault-started').touch()
                    finish = choice.get('finish_reason') or finish
                if cancel and len(arrivals) >= 4:
                    break
    except Exception as exc:
        error = repr(exc)
    elapsed = time.monotonic() - start
    gaps = [1000 * (b-a) for a, b in zip(arrivals, arrivals[1:])]
    return {'text': ''.join(texts), 'usage': usage, 'finish_reason': finish, 'done': done,
            'error': error, 'seconds': elapsed, 'content_chunks': len(arrivals),
            'ttft_ms': (arrivals[0]-start)*1000 if arrivals else None,
            'inter_chunk_ms_p50': statistics.median(gaps) if gaps else None,
            'inter_chunk_ms_p95': sorted(gaps)[int((len(gaps)-1)*.95)] if gaps else None}


def check_usage(usage):
    assert usage and usage['prompt_tokens'] > 0 and usage['completion_tokens'] > 0, usage
    assert usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'], usage


def suite():
    with request(payload('Say hello.')) as response:
        json.load(response)  # explicit warmup, excluded from timings
    fixtures = []
    for prompt in FIXTURES:
        start = time.monotonic()
        with request(payload(prompt, logprobs=True, top_logprobs=1)) as response:
            obj = json.load(response)
        check_usage(obj.get('usage'))
        assert obj['choices'][0]['finish_reason'] in ('stop', 'length'), obj
        fixtures.append({'prompt': prompt, 'seconds': time.monotonic()-start, 'response': obj})
    sse = stream(payload(FIXTURES[2]))
    assert sse['done'] and sse['finish_reason'] and not sse['error'], sse
    check_usage(sse['usage'])
    assert sse['content_chunks'] > 1, sse
    assert sse['text'] == fixtures[2]['response']['choices'][0]['message']['content'], sse
    stop_body = payload('Count from 1 to 20, separated by commas.', stop=['5'])
    with request(stop_body) as response:
        stopped = json.load(response)
    assert stopped['choices'][0]['finish_reason'] == 'stop', stopped
    assert '5' not in stopped['choices'][0]['message']['content'], stopped
    long = payload('Count from 1 to 1000, separated by commas.', ignore_eos=True)
    long['max_tokens'] = 1024
    cancelled = stream(long, cancel=True)
    assert cancelled['content_chunks'] >= 4 and not cancelled['done'], cancelled
    start = time.monotonic()
    with request(payload('Reply with exactly OK.')) as response:
        recovered = json.load(response)
    check_usage(recovered.get('usage'))
    recovered_seconds = time.monotonic()-start
    assert recovered_seconds < 10, ('request did not recover promptly after cancellation', recovered_seconds)
    prefill = payload('Read the following text and reply with OK: ' + 'test ' * 2048)
    prefill['max_tokens'] = 32
    start = time.monotonic()
    with request(prefill) as response:
        long_context = json.load(response)
    check_usage(long_context.get('usage'))
    assert 2048 <= long_context['usage']['prompt_tokens'] < 4096, long_context['usage']
    return {'fixtures': fixtures, 'sse': sse, 'stop': stopped, 'cancel': cancelled,
            'request_after_cancel_seconds': recovered_seconds,
            'long_context': {'response': long_context, 'seconds': time.monotonic()-start}, 'passed': True}


if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'ready':
        with urllib.request.urlopen(BASE + '/v1/models', timeout=3) as response:
            print(response.read().decode())
    elif mode == 'suite':
        result = suite()
        Path('/artifacts/api.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
    elif mode == 'fault':
        body = payload('Count all integers starting from 1, separated by commas.', ignore_eos=True)
        body['max_tokens'] = 2048
        result = stream(body, fault=True)
        timed_out = 'TimeoutError' in str(result['error'])
        result['failure_mode'] = 'client-timeout' if timed_out else ('transport-error' if result['error'] else 'incomplete-eof')
        result['passed'] = bool(result['content_chunks'] and not result['finish_reason'] and
                                (not result['done'] or result['error']) and not timed_out and result['seconds'] < 90)
        Path('/artifacts/fault.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
        sys.exit(0 if result['passed'] else 1)
