#!/usr/bin/env python3
"""Step 8 acceptance in a Linux VM: real agents, WireGuard, namespaces and packets.

Run as root in a disposable Linux VM, never by weakening the host firewall.
Only uniquely named test namespaces/interfaces are created or removed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import select
import signal
import socket
import struct
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BIN = RESULTS = None


def command(args, **kwargs):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, check=True, timeout=10, **kwargs).stdout


def wait(action, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = action()
        if value: return value
        time.sleep(0.05)
    raise AssertionError('deadline waiting for test condition')


def alive(pid):
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False


class Node:
    def __init__(self, lab, rank, **options):
        self.lab, self.rank = lab, rank
        self.root = lab.root / f'r{rank}'
        self.root.mkdir(mode=0o711)
        self.run = self.root / 'run'
        self.config = self.root / 'config.json'
        cfg = {'profile': 'simulator-dev-pp2-wg-v1', 'rank': rank, 'role': 'head' if rank == 0 else 'member',
               'group': {'peer_port' if rank == 0 else 'listen_port': 12310},
               'network': {'underlay_ip': lab.ip[rank], 'peer_ip': lab.ip[1-rank]}, **options}
        self.config.write_text(json.dumps(cfg))
        self.log = (self.root / 'agent.log').open('w')
        # Preserve the mount namespace so agent-created /run/netns mounts are
        # visible to the independent inspector and backend launch wrapper.
        self.proc = subprocess.Popen(['nsenter', '--net=/run/netns/' + lab.ns[rank], str(BIN / 'pipeline-agent-dev'),
                                      '--config', str(self.config), '--run-dir', str(self.run)],
                                     stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)

    def status(self):
        try: return json.loads((self.run / 'status.json').read_text())
        except FileNotFoundError: return {}

    def until(self, predicate, timeout=20):
        def check():
            status = self.status()
            if predicate(status): return status
            if self.proc.poll() is not None:
                raise AssertionError(f'agent exited: {status}; logs {self.root}')
        return wait(check, timeout)

    def ready(self):
        return self.until(lambda s: s.get('group_ready'))

    def control(self, op):
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(3)
            conn.connect(str(self.run / 'control.sock'))
            conn.sendall(json.dumps({'op': op}).encode() + b'\n')
            with conn.makefile('rb') as reader:
                return json.loads(reader.readline(32768))

    def events(self):
        result = []
        for line in (self.root / 'agent.log').read_text().splitlines():
            try:
                event = json.loads(line)
                if 'state' in event: result.append(event)
            except ValueError: pass
        return result

    def clean(self):
        for event in self.events():
            network = (event.get('group') or {}).get('network') or {}
            for process in (event['process'], network.get('guardian', {}), network.get('cleanup_guardian', {})):
                pid = process.get('pid', -1)
                assert pid < 0 or not alive(pid), (pid, self.root)
            if network:
                assert not Path('/run/netns', network['namespace']).exists()
                assert not Path('/run/cocoon-pipeline-net', network['namespace']).exists()
        assert not list(self.run.rglob('*.sock'))
        tables = json.loads(command(['ip', 'netns', 'exec', self.lab.ns[self.rank], 'nft', '-j', 'list', 'tables']))
        assert not any(e.get('table', {}).get('name', '').startswith('cp_') for e in tables['nftables'])
        links = json.loads(command(['ip', '-n', self.lab.ns[self.rank], '-j', 'link']))
        assert {e['ifname'] for e in links} == {'lo', 'eth0'}

    def close(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)
            self.proc.terminate()
            try: self.proc.wait(timeout=12)
            except subprocess.TimeoutExpired: self.proc.kill(); self.proc.wait(timeout=3)
        self.log.close()


class Lab:
    def __init__(self, start=True):
        self.start = start
        self.root = Path(tempfile.mkdtemp(prefix='lab-', dir=RESULTS))
        self.root.chmod(0o711)
        self.suffix = secrets.token_hex(4)
        self.bridge = 'cb' + self.suffix
        self.ns = ['cu' + self.suffix + str(i) for i in range(2)]
        self.links = ['cv' + self.suffix + str(i) for i in range(2)]
        self.prefix = f'198.18.{secrets.randbelow(240)+1}'
        self.ip = [self.prefix + '.1', self.prefix + '.2']
        self.nodes = []
        self.capture = None
        self.pcap = self.root / 'underlay.pcap'
        self.created = []

    def __enter__(self):
        try:
            command(['ip', 'link', 'add', self.bridge, 'type', 'bridge']); self.created.append(('link', self.bridge))
            command(['ip', 'addr', 'add', self.prefix + '.254/24', 'dev', self.bridge])
            command(['ip', 'link', 'set', self.bridge, 'up'])
            for rank in range(2):
                ns, host = self.ns[rank], self.links[rank]
                command(['ip', 'netns', 'add', ns]); self.created.append(('netns', ns))
                command(['ip', 'link', 'add', host, 'type', 'veth', 'peer', 'name', 'eth0', 'netns', ns]); self.created.append(('link', host))
                command(['ip', 'link', 'set', host, 'master', self.bridge])
                command(['ip', 'link', 'set', host, 'up'])
                command(['ip', '-n', ns, 'link', 'set', 'lo', 'up'])
                command(['ip', '-n', ns, 'addr', 'add', self.ip[rank] + '/24', 'dev', 'eth0'])
                command(['ip', '-n', ns, 'link', 'set', 'eth0', 'up'])
            if not self.start: return self
            self.member = Node(self, 1); self.nodes.append(self.member)
            self.head = Node(self, 0); self.nodes.append(self.head)
            self.head.ready(); self.member.ready()
            # Inspect traffic while the group is admitted. Setup may legitimately
            # generate ICMP port-unreachable before the second WG socket exists.
            # Exclude bridge-origin IGMP (test fixture), retain ALL node traffic.
            self.capture = subprocess.Popen(['tcpdump', '--immediate-mode', '-U', '-n', '-i', self.bridge, '-w', str(self.pcap),
                                            'not', 'src', 'host', self.prefix + '.254'],
                                            stdout=subprocess.DEVNULL, stderr=(self.root / 'tcpdump.log').open('w'))
            wait(lambda: self.pcap.exists() and self.pcap.stat().st_size >= 24, 3)
            time.sleep(0.6)  # Include at least one authenticated control heartbeat.
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        for node in self.nodes: node.close()
        if self.capture:
            self.capture.send_signal(signal.SIGINT)
            self.capture.wait(timeout=3)
        # Emergency cleanup stays separate from clean() assertions and only
        # touches resources identified by this lab/its agents' status events.
        for node in self.nodes:
            for event in node.events():
                network = (event.get('group') or {}).get('network')
                for process in (event['process'], (network or {}).get('guardian', {}), (network or {}).get('cleanup_guardian', {})):
                    pid = process.get('pid', -1)
                    if pid > 0 and alive(pid):
                        try: os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                if network and Path('/run/netns', network['namespace']).exists():
                    subprocess.run(['ip', '-n', network['namespace'], 'link', 'del', 'wg0'], capture_output=True)
                    subprocess.run(['ip', 'netns', 'del', network['namespace']], capture_output=True)
        for kind, name in reversed(self.created):
            subprocess.run(['ip', kind, 'del', name], capture_output=True)

    def stop(self):
        self.head.control('stop')
        assert self.head.proc.wait(timeout=12) == 0
        assert self.member.proc.wait(timeout=12) == 0
        for node in self.nodes: node.clean()

    def fault(self, rank, protocol, remove=False):
        name = 'fault_' + self.suffix
        args = ['ip', 'netns', 'exec', self.ns[rank], 'nft']
        if remove:
            command([*args, 'delete', 'table', 'inet', name])
        else:
            port = 51820 if protocol == 'udp' else 12310
            rules = f'add table inet {name}\nadd chain inet {name} out {{ type filter hook output priority 0; policy accept; }}\nadd rule inet {name} out {protocol} dport {port} drop\nadd rule inet {name} out {protocol} sport {port} drop\n'
            command([*args, '-f', '-'], input=rules)


def transfer(pid, remote, marker):
    code = '''import socket,struct,sys
data=bytes.fromhex(sys.argv[2])*1024
with socket.create_connection((sys.argv[1],29999),timeout=2) as s:
 s.sendall(struct.pack('!I',len(data))+data)
 reply=b''
 while len(reply)<len(data)+4:
  part=s.recv(len(data)+4-len(reply))
  assert part
  reply+=part
 assert reply==struct.pack('!I',len(data))+data
print(len(data))
'''
    return int(command(['nsenter', '-t', str(pid), '-n', 'setpriv', '--reuid=65534', '--regid=65534',
                        '--clear-groups', '--bounding-set=-all', '--no-new-privs', 'python3', '-I', '-c', code, remote, marker.hex()]))


def capture_summary(path, marker):
    data = path.read_bytes()
    assert marker not in data, 'plaintext found on underlay'
    endian = '<' if data[:4] == b'\xd4\xc3\xb2\xa1' else '>'
    assert struct.unpack(endian + 'I', data[20:24])[0] == 1, 'expected Ethernet pcap'
    offset = 24
    counts = {'wireguard': 0, 'tls': 0, 'unexpected_ip': 0}
    while offset + 16 <= len(data):
        _, _, length, _ = struct.unpack(endian + 'IIII', data[offset:offset+16]); offset += 16
        packet = data[offset:offset+length]; offset += length
        if len(packet) < 34 or packet[12:14] != b'\x08\x00': continue
        ip = packet[14:]; header = (ip[0] & 15)*4
        if ip[9] not in (6, 17) or len(ip) < header+4:
            counts['unexpected_ip'] += 1; continue
        ports = struct.unpack('!HH', ip[header:header+4])
        if ip[9] == 17 and ports == (51820, 51820): counts['wireguard'] += 1
        elif ip[9] == 6 and 12310 in ports: counts['tls'] += 1
        else: counts['unexpected_ip'] += 1
    assert counts['wireguard'] > 10 and counts['tls'] > 0, counts
    return counts


class Tests(unittest.TestCase):
    def test_partial_setup_failure_cleans_before_retry(self):
        with Lab(start=False) as lab:
            # Occupy WG's underlay UDP port: setconf fails after namespace/link
            # creation. Cleanup must work although READY was never reached.
            code = 'import socket,time; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(("0.0.0.0",51820)); print("ready",flush=True); time.sleep(90)'
            blocker = subprocess.Popen(['nsenter', '--net=/run/netns/' + lab.ns[1], 'python3', '-I', '-u', '-c', code], stdout=subprocess.PIPE)
            try:
                self.assertTrue(select.select([blocker.stdout], [], [], 3)[0])
                self.assertEqual(blocker.stdout.readline(), b'ready\n')
                member = Node(lab, 1); lab.nodes.append(member)
                head = Node(lab, 0); lab.nodes.append(head)
                for node in (head, member):
                    self.assertEqual(node.proc.wait(timeout=45), 1)
                    self.assertEqual(node.status()['attempt'], 2)
                    self.assertFalse(any(e['process']['pid'] > 0 for e in node.events()))
                    node.clean()
                self.assertIn('network command failed: ip', member.status()['failure'])
                self.assertIsNone(blocker.poll(), 'cleanup stopped an unrelated process')
            finally:
                blocker.terminate(); blocker.wait(timeout=3); blocker.stdout.close()

    def test_encrypted_backend_traffic_and_isolation(self):
        marker = secrets.token_bytes(32)
        with Lab() as lab:
            statuses = [lab.head.ready(), lab.member.ready()]
            evidence = []
            for rank, (node, state) in enumerate(zip((lab.head, lab.member), statuses)):
                pid = state['process']['pid']
                ns = state['group']['network']['namespace']
                self.assertNotEqual(os.stat(f'/proc/{pid}/ns/net').st_ino, os.stat('/run/netns/' + lab.ns[rank]).st_ino)
                self.assertEqual(os.stat(f'/proc/{pid}/ns/net').st_ino, os.stat('/run/netns/' + ns).st_ino)
                proc = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
                self.assertEqual(proc['Uid'].split(), ['65534']*4)
                for cap in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
                    self.assertEqual(int(proc[cap], 16), 0)
                self.assertEqual(proc['NoNewPrivs'].strip(), '1')
                links = json.loads(command(['ip', '-n', ns, '-j', 'link']))
                self.assertEqual({i['ifname'] for i in links}, {'lo', 'wg0'})
                routes = json.loads(command(['ip', '-n', ns, '-j', 'route']))
                self.assertEqual(len(routes), 1)
                self.assertEqual(routes[0]['dev'], 'wg0')
                allowed = command(['ip', 'netns', 'exec', ns, 'wg', 'show', 'wg0', 'allowed-ips'])
                self.assertIn(f'10.231.0.{2-rank}/32', allowed)
                self.assertEqual(transfer(pid, f'10.231.0.{2-rank}', marker), 32768)
                evidence.append({'rank': rank, 'links': links, 'routes': routes, 'allowed_ips': allowed,
                                 'uid': proc['Uid'].strip(), 'capabilities': proc['CapEff'].strip()})
            # Ciphertext inspection before deliberately injecting denied packets.
            lab.capture.send_signal(signal.SIGINT); lab.capture.wait(timeout=3); lab.capture = None
            summary = capture_summary(lab.pcap, marker)
            self.assertEqual(summary['unexpected_ip'], 0)
            (lab.root / 'evidence.json').write_text(json.dumps({'network': evidence, 'capture': summary}, indent=2))
            for ip in lab.ip:
                for port in (29998, 29999, 8000):
                    with self.assertRaises(OSError):
                        socket.create_connection((ip, port), timeout=0.2).close()
            # No route to the underlay/Internet from the backend, even if code
            # chooses those destinations instead of the peer overlay address.
            ns = statuses[0]['group']['network']['namespace']
            for target in (lab.ip[1], '1.1.1.1'):
                probe = subprocess.run(['ip', '-n', ns, 'route', 'get', target], capture_output=True)
                self.assertNotEqual(probe.returncode, 0)
            # Even a deliberately listening service in the UNDERLAY must be
            # unreachable from the roster peer. Local success rules out the
            # misleading "connection refused because nothing listens" result.
            server_code = '''import socket,sys
s=socket.socket();s.bind((sys.argv[1],8000));s.listen();print('ready',flush=True)
while True:
 c,_=s.accept();c.sendall(b'listening');c.close()
'''
            server = subprocess.Popen(['nsenter', '--net=/run/netns/' + lab.ns[1], 'python3', '-I', '-u', '-c', server_code, lab.ip[1]], stdout=subprocess.PIPE)
            try:
                self.assertTrue(select.select([server.stdout], [], [], 3)[0])
                self.assertEqual(server.stdout.readline(), b'ready\n')
                client = 'import socket,sys; s=socket.create_connection((sys.argv[1],8000),timeout=.4); assert s.recv(32)==b"listening"'
                command(['nsenter', '--net=/run/netns/' + lab.ns[1], 'python3', '-I', '-c', client, lab.ip[1]])
                denied = subprocess.run(['nsenter', '--net=/run/netns/' + lab.ns[0], 'python3', '-I', '-c', client, lab.ip[1]], capture_output=True, timeout=3)
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn(b'TimeoutError', denied.stderr)
            finally:
                server.terminate(); server.wait(timeout=3); server.stdout.close()
            restricted = ['nsenter', '-t', str(statuses[0]['process']['pid']), '-n', 'setpriv', '--reuid=65534', '--regid=65534',
                          '--clear-groups', '--bounding-set=-all', '--no-new-privs']
            for attempt in (['ip', 'route', 'add', 'default', 'dev', 'wg0'], ['nsenter', '--net=/proc/1/ns/net', 'true']):
                denied = subprocess.run([*restricted, *attempt], capture_output=True, timeout=3)
                self.assertNotEqual(denied.returncode, 0)
            lab.stop()

    def test_guardian_sigkill_cleans_keys_before_retry(self):
        with Lab() as lab:
            old = lab.head.ready()
            network = old['group']['network']
            os.kill(network['guardian']['pid'], signal.SIGKILL)
            lab.head.until(lambda s: not s.get('group_ready', True), timeout=7)
            new = lab.head.until(lambda s: s.get('group_ready') and s['epoch'] != old['epoch'], timeout=30)
            self.assertFalse(Path('/run/netns', network['namespace']).exists())
            self.assertFalse(Path('/run/cocoon-pipeline-net', network['namespace']).exists())
            self.assertFalse(alive(network['guardian']['pid']))
            self.assertNotEqual(new['group']['network_key'], old['group']['network_key'])
            self.assertTrue(any((e.get('group') or {}).get('network', {}).get('cleanup_guardian', {}).get('pid', -1) > 0 for e in lab.head.events()))
            lab.member.ready(); lab.stop()

    def test_firewall_tampering_closes_group(self):
        with Lab() as lab:
            old = lab.head.ready()
            ns = old['group']['network']['namespace']
            table = 'cp_' + ns[3:]
            command(['ip', 'netns', 'exec', ns, 'nft', 'add', 'rule', 'inet', table, 'input', 'tcp', 'dport', '8000', 'drop'])
            lab.head.until(lambda s: not s.get('group_ready', True), timeout=7)
            lab.head.until(lambda s: s.get('group_ready') and s['epoch'] != old['epoch'])
            self.assertFalse(Path('/run/netns', ns).exists())
            lab.member.ready(); lab.stop()

    def test_data_loss_closes_group_and_recovers(self):
        with Lab() as lab:
            old = lab.head.ready()
            lab.fault(0, 'udp')
            lab.head.until(lambda s: not s.get('group_ready', True), timeout=7)
            lab.member.until(lambda s: not s.get('group_ready', True), timeout=7)
            lab.fault(0, 'udp', remove=True)
            new = lab.head.until(lambda s: s.get('group_ready') and s['epoch'] != old['epoch'])
            self.assertNotEqual(new['group']['network_key'], old['group']['network_key'])
            self.assertFalse(Path('/run/netns', old['group']['network']['namespace']).exists())
            self.assertFalse(alive(old['process']['pid']))
            lab.member.ready(); lab.stop()

    def test_control_loss_closes_group_and_recovers(self):
        with Lab() as lab:
            old = lab.head.ready()
            lab.fault(0, 'tcp')
            lab.head.until(lambda s: not s.get('group_ready', True), timeout=7)
            lab.member.until(lambda s: not s.get('group_ready', True), timeout=7)
            lab.fault(0, 'tcp', remove=True)
            lab.head.until(lambda s: s.get('group_ready') and s['epoch'] != old['epoch'])
            lab.member.ready(); lab.stop()

    def test_deleted_wireguard_has_no_plaintext_fallback(self):
        with Lab() as lab:
            old = lab.head.ready()
            guardian = old['group']['network']['guardian']['pid']
            lab.head.proc.send_signal(signal.SIGSTOP); os.kill(guardian, signal.SIGSTOP)
            try:
                ns = old['group']['network']['namespace']
                command(['ip', '-n', ns, 'link', 'del', 'wg0'])
                self.assertEqual({i['ifname'] for i in json.loads(command(['ip', '-n', ns, '-j', 'link']))}, {'lo'})
                for target in ('10.231.0.2', lab.ip[1], '1.1.1.1'):
                    self.assertNotEqual(subprocess.run(['ip', '-n', ns, 'route', 'get', target], capture_output=True).returncode, 0)
                with self.assertRaises(subprocess.CalledProcessError):
                    transfer(old['process']['pid'], '10.231.0.2', secrets.token_bytes(32))
            finally:
                os.kill(guardian, signal.SIGCONT); lab.head.proc.send_signal(signal.SIGCONT)
            lab.head.until(lambda s: not s.get('group_ready', True), timeout=7)
            new = lab.head.until(lambda s: s.get('group_ready') and s['epoch'] != old['epoch'])
            self.assertNotEqual(new['group']['network_key'], old['group']['network_key'])
            lab.member.ready(); lab.stop()


def main():
    global BIN, RESULTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--no-build', action='store_true')
    parser.add_argument('--test', help='One unittest method for diagnosis')
    args = parser.parse_args()
    if platform.system() != 'Linux' or os.geteuid() != 0:
        parser.error('requires root in a disposable Linux VM')
    BIN = args.build_dir.resolve() / 'pipeline'
    RESULTS = Path(tempfile.mkdtemp(prefix='cpn-', dir='/tmp')); RESULTS.chmod(0o711)
    print(f'Artifacts: {RESULTS}', flush=True)
    if not args.no_build:
        subprocess.run(['cmake', '--build', str(args.build_dir), '--target', 'pipeline-agent-dev',
                        'test-pipeline-profile', 'test-pipeline-membership', '-j', '4'], check=True)
    for test in ('test-pipeline-profile', 'test-pipeline-membership'):
        with (RESULTS / (test + '.log')).open('w') as log:
            subprocess.run([str(BIN / test)], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=40)
    print('PASS: C++ profiles, network protocol and mutual TLS policies', flush=True)
    before_ns = command(['ip', 'netns', 'list'])
    before_firewall = command(['nft', '-j', 'list', 'ruleset'])
    started = time.monotonic()
    suite = unittest.TestSuite([Tests(args.test)]) if args.test else unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    assert command(['ip', 'netns', 'list']) == before_ns, 'test namespaces leaked'
    assert command(['nft', '-j', 'list', 'ruleset']) == before_firewall, 'VM root firewall changed'
    files = list((ROOT / 'pipeline').rglob('*')) + [Path(__file__), ROOT / 'test/test-pipeline-profile.cpp', ROOT / 'test/test-pipeline-membership.cpp']
    report = {'passed': result.wasSuccessful(), 'tests': result.testsRun, 'platform': platform.platform(),
              'seconds': time.monotonic()-started,
              'tools': {tool: command(args).strip() for tool, args in {'ip': ['ip', '-V'], 'wg': ['wg', '--version'], 'nft': ['nft', '--version']}.items()},
              'sources': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file() and '__pycache__' not in p.parts},
              'failures': [(str(case), detail) for case, detail in result.errors + result.failures]}
    (RESULTS / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(f'Report: {RESULTS / "report.json"}', flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
