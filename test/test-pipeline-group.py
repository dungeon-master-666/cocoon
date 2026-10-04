#!/usr/bin/env python3
"""Step 7: two real agents, mutual TLS, group lifecycle and adversarial protocol tests."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('local_tests', ROOT / 'test/test-pipeline-agent.py')
local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local)
BIN = None
RESULTS = None


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Node:
    def __init__(self, rank, endpoint, **options):
        self.root = Path(tempfile.mkdtemp(prefix=f'r{rank}-', dir=RESULTS))
        self.run = self.root / 'run'
        group = {'peer_port' if rank == 0 else 'listen_port': endpoint}
        if 'certificate_base' in options:
            group['certificate_base'] = options.pop('certificate_base')
        config = {'profile': 'simulator-dev-pp2-v1', 'rank': rank, 'role': 'head' if rank == 0 else 'member',
                  'group': group, **options}
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps(config))
        self.log = (self.root / 'agent.log').open('w')
        self.proc = subprocess.Popen([str(BIN / 'pipeline-agent-dev'), '--config', str(self.config),
                                     '--run-dir', str(self.run)], stdout=self.log, stderr=subprocess.STDOUT,
                                     start_new_session=True)

    def status(self):
        try:
            return json.loads((self.run / 'status.json').read_text())
        except FileNotFoundError:
            return {}

    def events(self):
        events = []
        for line in (self.root / 'agent.log').read_text().splitlines():
            try:
                event = json.loads(line)
                if 'state' in event:
                    events.append(event)
            except ValueError:
                pass
        return events

    def wait(self, predicate, timeout=10):
        def check():
            status = self.status()
            if predicate(status):
                return status
            if self.proc.poll() is not None:
                raise AssertionError(f'agent exited: {status}; logs {self.root}')
            return None
        return local.wait_for(check, timeout)

    def ready(self):
        return self.wait(lambda s: s.get('group_ready'))

    def control(self, op):
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(2)
            conn.connect(str(self.run / 'control.sock'))
            conn.sendall(json.dumps({'op': op}).encode() + b'\n')
            with conn.makefile('rb') as reader:
                return json.loads(reader.readline(32768))

    def clean(self):
        for event in self.events():
            pid = event['process']['pgid']
            if pid > 0:
                assert not local.alive(pid, group=True), (pid, self.root)
        assert not list(self.run.rglob('*.sock')), self.root

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)
            self.proc.terminate()
            try:
                self.proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        # Emergency cleanup belongs to the test; assertions above must already
        # have proved agent cleanup on the successful path.
        for event in self.events():
            pid = event['process']['pgid']
            if pid > 0 and local.alive(pid, group=True):
                os.killpg(pid, signal.SIGKILL)
        self.log.close()


def certificate(variant='valid'):
    base = Path(tempfile.mkdtemp(prefix='cert-', dir=RESULTS)) / 'pipeline'
    meta = json.loads(subprocess.check_output([str(BIN / 'pipeline-dev-cert'), str(base), variant]))
    return base, meta


def tls_client(endpoint, base=None):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # Adversarial driver; agents verify both directions in other tests.
    if base:
        context.load_cert_chain(str(base) + '_cert.pem', str(base) + '_key.pem')
    return context.wrap_socket(socket.create_connection(('127.0.0.1', endpoint), timeout=2), server_hostname='localhost')


def exchange(conn, frame):
    body = json.dumps(frame, separators=(',', ':')).encode()
    conn.sendall(struct.pack('!I', len(body)) + body)
    def exact(count):
        result = b''
        while len(result) < count:
            data = conn.recv(count - len(result))
            if not data:
                raise EOFError('TLS peer closed')
            result += data
        return result
    length = struct.unpack('!I', exact(4))[0]
    assert length <= 65536
    return json.loads(exact(length))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Driver:
    """Authenticated dev head for sending deliberate protocol violations."""
    def __init__(self, endpoint, member):
        cert, identity = certificate()
        self.conn = tls_client(endpoint, cert)
        self.config = member.status()['config_digest']
        self.epoch = secrets.token_hex(32)
        self.sequence = 0
        self.peer = {'rank': 0, 'role': 'head', 'identity': identity['identity'],
                     'image_hash': identity['expected_dev_image'], 'boot_id': secrets.token_hex(32),
                     'network_key': secrets.token_hex(32), 'overlay_ip': '10.231.0.1',
                     'profile_id': 'simulator-dev-pp2-v1', 'backend_kind': 'simulator',
                     'local_checks': 'dev-model-fixture-no-gpu'}

    def frame(self, op, payload):
        self.sequence += 1
        return {'version': 1, 'kind': 'request', 'op': op, 'epoch': self.epoch,
                'config_digest': self.config, 'sequence': self.sequence,
                'request_id': secrets.token_hex(32), 'payload': payload}

    def prepare(self):
        challenge = secrets.token_hex(32)
        hi = exchange(self.conn, self.frame('Hello', {'peer': self.peer, 'challenge': challenge}))['payload']
        assert hi['echo'] == challenge
        roster = [self.peer, hi['peer']]
        self.roster_digest = digest(roster)
        frame = self.frame('Prepare', {'roster': roster, 'roster_digest': self.roster_digest,
                                      'group_id': digest({'head_key': self.peer['identity'],
                                                          'epoch': self.epoch, 'config_digest': self.config}),
                                      'echo': hi['challenge'], 'lease_ms': 1500})
        return frame

    def lifecycle(self, op, **extra):
        return self.frame(op, {'roster_digest': self.roster_digest, **extra})

    def warmup(self):
        def heartbeat():
            p = exchange(self.conn, self.lifecycle('Heartbeat'))['payload']
            return p.get('local_ready')
        local.wait_for(heartbeat, 2)
        assert exchange(self.conn, self.lifecycle('Ready'))['payload']['ready']


class GroupTests(unittest.TestCase):
    def test_wire_replay_stale_epoch_and_changed_payload(self):
        endpoint = port()
        with Node(1, endpoint) as member:
            member.wait(lambda s: s.get('state') == 'FORMING')
            driver = Driver(endpoint, member)
            with driver.conn:
                prepare = driver.prepare()
                prepared = exchange(driver.conn, prepare)
                self.assertTrue(prepared['payload']['prepared'])
                self.assertEqual(exchange(driver.conn, prepare), prepared)
                self.assertEqual(member.status()['process']['pid'], -1)
                commit = driver.lifecycle('Commit')
                committed = exchange(driver.conn, commit)
                self.assertTrue(committed['payload']['committed'])
                driver.warmup()
                first = member.ready()
                self.assertEqual(exchange(driver.conn, commit), committed)
                changed = copy.deepcopy(commit)
                changed['payload']['roster_digest'] = secrets.token_hex(32)
                self.assertIn('error', exchange(driver.conn, changed)['payload'])
                stale = driver.lifecycle('Stop', restart=True)
                stale['epoch'] = secrets.token_hex(32)
                self.assertIn('error', exchange(driver.conn, stale)['payload'])
                current = member.control('status')
                self.assertTrue(current['group_ready'])
                self.assertEqual(current['epoch'], first['epoch'])
                self.assertEqual(current['process']['pid'], first['process']['pid'])
                exchange(driver.conn, driver.lifecycle('Stop', restart=False))
            self.assertEqual(member.proc.wait(timeout=5), 0)
            member.clean()

    def test_replayed_commit_cannot_extend_lease(self):
        endpoint = port()
        with Node(1, endpoint) as member:
            member.wait(lambda s: s.get('state') == 'FORMING')
            driver = Driver(endpoint, member)
            with driver.conn:
                exchange(driver.conn, driver.prepare())
                commit = driver.lifecycle('Commit')
                committed = exchange(driver.conn, commit)
                first = member.wait(lambda s: s.get('local_ready'))
                started = time.monotonic()
                while time.monotonic() - started < 2.5:
                    try:
                        self.assertEqual(exchange(driver.conn, commit), committed)
                    except (OSError, EOFError):
                        break
                    time.sleep(0.08)
                else:
                    self.fail('replayed Commit kept lease alive')
                member.wait(lambda s: 'lease expired' in (s.get('failure') or s.get('last_failure', '')))
                local.wait_for(lambda: not local.alive(first['process']['pgid'], group=True), 3)
            member.proc.terminate(); member.proc.wait(timeout=5); member.clean()

    def test_duplicate_tls_identity_never_starts_backend(self):
        endpoint = port()
        cert, _ = certificate()
        with Node(1, endpoint, certificate_base=str(cert)) as member, Node(0, endpoint, certificate_base=str(cert)) as head:
            head.wait(lambda s: 'duplicate TLS' in (s.get('failure') or s.get('last_failure', '')))
            self.assertFalse(any(e['process']['pid'] > 0 for e in head.events() + member.events()))
            head.proc.terminate(); member.proc.terminate()
            head.proc.wait(timeout=5); member.proc.wait(timeout=5)
            head.clean(); member.clean()

    def test_unavailable_member_retries_are_bounded(self):
        with Node(0, port()) as head:
            self.assertEqual(head.proc.wait(timeout=18), 1)
            status = head.status()
            self.assertEqual(status['state'], 'FAILED')
            self.assertEqual(status['attempt'], 2)
            self.assertFalse(any(e['process']['pid'] > 0 for e in head.events()))
            epochs = {e['epoch'] for e in head.events()}
            self.assertEqual(len(epochs), 3)
            head.clean()

    def test_failed_member_warmup_never_opens_group(self):
        endpoint = port()
        with Node(1, endpoint, simulator={'scenario': 'warmup-error'}) as member, Node(0, endpoint) as head:
            self.assertEqual(member.proc.wait(timeout=12), 1)
            self.assertEqual(head.proc.wait(timeout=12), 1)
            for node in (head, member):
                self.assertFalse(any(e['group_ready'] for e in node.events()))
                self.assertEqual(node.status()['state'], 'FAILED')
                node.clean()

    def test_group_start_ready_and_graceful_stop(self):
        endpoint = port()
        with Node(1, endpoint) as member:
            member.wait(lambda s: s.get('state') == 'FORMING')
            self.assertEqual(member.status()['process']['pid'], -1)
            with Node(0, endpoint) as head:
                a, b = head.ready(), member.ready()
                self.assertEqual(a['epoch'], b['epoch'])
                self.assertEqual(a['group']['roster'], b['group']['roster'])
                self.assertEqual(a['group']['group_id'], b['group']['group_id'])
                self.assertNotEqual(a['group']['network_key'], b['group']['network_key'])
                # A new connection cannot replace the pinned coordinator.
                cert, _ = certificate()
                with self.assertRaises((OSError, EOFError)):
                    with tls_client(endpoint, cert) as extra:
                        if not extra.recv(1):
                            raise EOFError()
                self.assertTrue(head.control('status')['group_ready'])
                self.assertTrue(member.control('status')['group_ready'])
                for node in (head, member):
                    status = node.status()
                    self.assertFalse(status['hardware_attested'])
                    self.assertEqual(status['group']['transport'], 'mutual-tls-1.3')
                    first_start = next(e for e in node.events() if e['process']['pid'] > 0)
                    self.assertEqual(first_start['group']['state'], 'STARTING')
                    self.assertEqual(len(first_start['group']['roster']), 2)
                head.control('stop')
                self.assertEqual(head.proc.wait(timeout=5), 0)
                self.assertEqual(member.proc.wait(timeout=5), 0)
                head.clean(); member.clean()

    def test_backend_failure_recreates_epoch_and_keys(self):
        endpoint = port()
        with Node(1, endpoint) as member, Node(0, endpoint) as head:
            old_a, old_b = head.ready(), member.ready()
            os.kill(old_b['process']['pid'], signal.SIGKILL)
            head.wait(lambda s: not s.get('group_ready', True))
            new_a = head.wait(lambda s: s.get('group_ready') and s.get('epoch') != old_a['epoch'])
            new_b = member.wait(lambda s: s.get('group_ready') and s.get('epoch') == new_a['epoch'])
            for old, new in ((old_a, new_a), (old_b, new_b)):
                self.assertNotEqual(old['process']['pid'], new['process']['pid'])
                self.assertFalse(local.alive(old['process']['pgid'], group=True))
                self.assertNotEqual(old['group']['network_key'], new['group']['network_key'])
                self.assertEqual(old['boot_id'], new['boot_id'])
                self.assertNotEqual(old['group']['identity'], new['group']['identity'])
                self.assertEqual(old['config_digest'], new['config_digest'])
            head.control('stop')
            self.assertEqual(head.proc.wait(timeout=5), 0)
            self.assertEqual(member.proc.wait(timeout=5), 0)
            head.clean(); member.clean()

    def test_lease_expiry_when_coordinator_is_paused(self):
        endpoint = port()
        with Node(1, endpoint) as member, Node(0, endpoint) as head:
            a, b = head.ready(), member.ready()
            head.proc.send_signal(signal.SIGSTOP)
            try:
                member.wait(lambda s: not s.get('group_ready', True), timeout=3)
                local.wait_for(lambda: not local.alive(b['process']['pgid'], group=True), timeout=4)
            finally:
                head.proc.send_signal(signal.SIGCONT)
            new = head.wait(lambda s: s.get('group_ready') and s.get('epoch') != a['epoch'])
            member.wait(lambda s: s.get('group_ready') and s.get('epoch') == new['epoch'])
            head.control('stop')
            head.proc.wait(timeout=5); member.proc.wait(timeout=5)
            head.clean(); member.clean()

    def test_member_disconnect_closes_head_and_replacement_joins(self):
        endpoint = port()
        with Node(1, endpoint) as member, Node(0, endpoint) as head:
            a, b = head.ready(), member.ready()
            member.proc.terminate()
            member.proc.wait(timeout=5)
            head.wait(lambda s: not s.get('group_ready', True), timeout=3)
            local.wait_for(lambda: not local.alive(a['process']['pgid'], group=True), timeout=4)
            with Node(1, endpoint) as replacement:
                new = head.wait(lambda s: s.get('group_ready') and s.get('epoch') != a['epoch'])
                r = replacement.ready()
                self.assertNotEqual(b['boot_id'], r['boot_id'])
                self.assertEqual(new['epoch'], r['epoch'])
                head.control('stop')
                head.proc.wait(timeout=5); replacement.proc.wait(timeout=5)
                head.clean(); replacement.clean(); member.clean()

    def test_config_mismatch_never_starts_backend(self):
        endpoint = port()
        with Node(1, endpoint, limits={'max_num_seqs': 1}) as member, Node(0, endpoint) as head:
            head.wait(lambda s: 'config digest' in (s.get('failure') or s.get('last_failure', '')))
            self.assertFalse(any(e['process']['pid'] > 0 for e in head.events() + member.events()))
            head.proc.terminate(); member.proc.terminate()
            head.proc.wait(timeout=5); member.proc.wait(timeout=5)
            head.clean(); member.clean()

    def test_bad_server_evidence_never_starts_backend(self):
        endpoint = port()
        cert, _ = certificate('wrong-image')
        with Node(1, endpoint, certificate_base=str(cert)) as member, Node(0, endpoint) as head:
            head.wait(lambda s: 'TLS' in (s.get('failure') or s.get('last_failure', '')))
            self.assertFalse(any(e['process']['pid'] > 0 for e in head.events() + member.events()))
            head.proc.terminate(); member.proc.terminate()
            head.proc.wait(timeout=5); member.proc.wait(timeout=5)
            head.clean(); member.clean()

    def test_active_tls_session_closes_before_certificate_expiry(self):
        endpoint = port()
        cert, _ = certificate('short-lived')
        with Node(1, endpoint, certificate_base=str(cert)) as member, Node(0, endpoint) as head:
            a, b = head.ready(), member.ready()
            member.wait(lambda s: not s.get('group_ready', True), timeout=5)
            head.wait(lambda s: not s.get('group_ready', True), timeout=3)
            self.assertTrue(any('certificate renewal' in str(e.get('failure', ''))
                                for e in member.events() + head.events()))
            for old in (a, b):
                local.wait_for(lambda: not local.alive(old['process']['pgid'], group=True), 3)
            head.proc.terminate(); member.proc.terminate()
            head.proc.wait(timeout=5); member.proc.wait(timeout=5)
            head.clean(); member.clean()

    def test_no_certificate_and_oversized_frames_are_rejected(self):
        endpoint = port()
        with Node(1, endpoint) as member:
            member.wait(lambda s: s.get('state') == 'FORMING')
            with self.assertRaises((ssl.SSLError, OSError, EOFError)):
                with tls_client(endpoint) as conn:
                    conn.sendall(b'\0\0\0\2{}')
                    if not conn.recv(1):
                        raise EOFError()
            cert, _ = certificate()
            with tls_client(endpoint, cert) as conn:
                conn.sendall(struct.pack('!I', 65537))
                self.assertEqual(conn.recv(1), b'')
            self.assertEqual(member.status()['process']['pid'], -1)
            member.proc.terminate(); member.proc.wait(timeout=5); member.clean()


def main():
    global BIN, RESULTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=ROOT / 'build/local')
    parser.add_argument('--no-build', action='store_true')
    args = parser.parse_args()
    BIN = args.build_dir.resolve() / 'pipeline'
    RESULTS = Path(tempfile.mkdtemp(prefix='cpg-', dir='/tmp'))
    print(f'Artifacts: {RESULTS}', flush=True)
    if not args.no_build:
        subprocess.run(['cmake', '--build', str(args.build_dir), '--target', 'pipeline-agent', 'pipeline-agent-dev',
                        'pipeline-dev-cert', 'test-pipeline-membership', 'test-pipeline-profile', '-j', '4'], check=True)
    with (RESULTS / 'membership-unit.log').open('w') as log:
        subprocess.run([str(BIN / 'test-pipeline-profile')], stdout=log, stderr=subprocess.STDOUT, timeout=10, check=True)
        subprocess.run([str(BIN / 'test-pipeline-membership')], stdout=log, stderr=subprocess.STDOUT, timeout=40, check=True)
    print('PASS: C++ protocol and mutual TLS tests', flush=True)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GroupTests))
    files = list((ROOT / 'pipeline').rglob('*')) + [Path(__file__), ROOT / 'test/test-pipeline-membership.cpp']
    report = {'passed': result.wasSuccessful(), 'tests': result.testsRun, 'platform': platform.platform(),
              'seconds': time.monotonic() - started,
              'sources': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in files if p.is_file() and '__pycache__' not in p.parts},
              'failures': [(str(case), detail) for case, detail in result.failures + result.errors]}
    (RESULTS / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'Report: {RESULTS / "report.json"}', flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
