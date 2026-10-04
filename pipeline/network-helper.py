#!/usr/bin/env python3
"""Private, fixed-profile Linux network guardian; stdin is an anonymous owner pipe.

Never accepts shell commands. WireGuard secret material stays in process/pipe
memory, then in the kernel; no key files, argv secrets, or `wg show ... dump`.
"""
import base64
import ctypes
import ipaddress
import json
import os
import re
import resource
import select
import signal
import socket
import stat
import subprocess
import sys
import time

IP, WG, NFT = '/usr/sbin/ip', '/usr/bin/wg', '/usr/sbin/nft'
NSENTER = '/usr/bin/nsenter'
OWNERS = '/run/cocoon-pipeline-net'
STOP = False


def command(args, data=None):
    # ip's namespace switch also remounts sysfs, which requires the host PID
    # namespace on some nested/container kernels. Only switch network namespace;
    # retain the private PID/mount namespace and the existing read-only sysfs.
    if args[:2] == [IP, '-n']:
        args = [NSENTER, '--net=/run/netns/' + args[2], IP, *args[3:]]
    elif args[:3] == [IP, 'netns', 'exec']:
        args = [NSENTER, '--net=/run/netns/' + args[3], *args[4:]]
    try:
        result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=1.5, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError('network command timeout') from None
    if result.returncode:
        # Command stderr can contain secret input from wg; never forward it.
        raise RuntimeError('network command failed: ' + os.path.basename(args[0]))
    return result.stdout


def status(state, **fields):
    try:
        print(json.dumps({'state': state, **fields}, separators=(',', ':')), flush=True)
    except BrokenPipeError:
        pass  # The owner may have died; cleanup must still run.


def stop(*unused):
    global STOP
    STOP = True


def setns(fd):
    if ctypes.CDLL(None, use_errno=True).setns(fd, 0x40000000) != 0:
        raise RuntimeError('cannot enter network namespace')


class Network:
    def __init__(self, cfg):
        self.name = cfg['namespace']
        if not re.fullmatch(r'cp-[0-9a-f]{24}', self.name):
            raise ValueError('invalid namespace')
        self.table = 'cp_' + self.name[3:]
        self.temp = 'cp' + self.name[3:15]
        self.cleanup_only = cfg.get('cleanup', False)
        self.token = cfg['owner_token']
        if not re.fullmatch(r'[0-9a-f]{64}', self.token):
            raise ValueError('invalid ownership token')
        self.claim = OWNERS + '/' + self.name
        self.ownership = {'token': self.token, 'underlay_inode': os.stat('/proc/self/ns/net').st_ino}
        self.sock = None
        self.rank = cfg['rank']
        self.self = cfg['roster'][self.rank]
        self.peer = cfg['roster'][1-self.rank]
        self.underlay = str(ipaddress.IPv4Address(cfg['underlay_ip']))
        self.endpoint = str(ipaddress.IPv4Address(cfg['peer_ip']))
        self.service_egress = cfg.get('service_egress', [])
        if not isinstance(self.service_egress, list) or len(self.service_egress) > 8 or (self.rank != 0 and self.service_egress):
            raise ValueError('invalid service egress')
        for entry in self.service_egress:
            if not isinstance(entry, dict) or set(entry) != {'ip', 'port'}:
                raise ValueError('invalid service endpoint')
            ip = ipaddress.IPv4Address(entry['ip'])
            if (str(ip) != entry['ip'] or ip.is_loopback or ip.is_multicast or ip.is_unspecified or
                    str(ip) == '255.255.255.255' or ip in ipaddress.ip_network('10.231.0.0/24') or
                    type(entry['port']) is not int or not 1 <= entry['port'] <= 65535):
                raise ValueError('invalid service endpoint')
        self.overlay = self.self['overlay_ip']
        self.remote = self.peer['overlay_ip']
        self.public = base64.b64encode(bytes.fromhex(self.self['network_key'])).decode()
        self.peer_key = base64.b64encode(bytes.fromhex(self.peer['network_key'])).decode()
        self.key = cfg.pop('private_key', None)
        if not self.cleanup_only and (not self.key or len(base64.b64decode(self.key, validate=True)) != 32):
            raise ValueError('invalid key')
        self.expected_fw = None

    def ns(self, args, data=None):
        return command([IP, 'netns', 'exec', self.name, *args], data)

    def nft(self, engine=False):
        run = self.ns if engine else command
        return json.loads(run([NFT, '-j', 'list', 'table', 'inet', self.table]))

    def has_table(self, engine=False):
        run = self.ns if engine else command
        return any(e.get('table', {}).get('name') == self.table and e['table']['family'] == 'inet'
                   for e in json.loads(run([NFT, '-j', 'list', 'tables']))['nftables'])

    def claim_resources(self):
        # A key-free ownership record lets a replacement guardian clean up after
        # SIGKILL. Persist the complete claim before the first kernel mutation.
        os.makedirs(OWNERS, mode=0o700, exist_ok=True)
        info = os.lstat(OWNERS)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError('unsafe network ownership directory')
        if (os.path.lexists('/run/netns/' + self.name) or self.has_table() or
                any(link['ifname'] == self.temp for link in json.loads(command([IP, '-j', 'link'])))):
            raise RuntimeError('network resource already exists')
        fd = os.open(self.claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(self.ownership, stream)
            stream.flush()

    def owns_resources(self):
        try:
            fd = os.open(self.claim, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return False  # No mutation can precede the ownership claim.
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or
                    stat.S_IMODE(info.st_mode) != 0o600 or stream.read(4096) != json.dumps(self.ownership)):
                raise RuntimeError('network ownership mismatch')
        return True

    def setup(self):
        # This dev profile must never install a default-drop firewall in the
        # VM/host init namespace. Tests supply dedicated guest underlay netns.
        if os.stat('/proc/self/ns/net').st_ino == os.stat('/proc/1/ns/net').st_ino:
            raise RuntimeError('dev WireGuard requires a dedicated underlay namespace')
        addresses = json.loads(command([IP, '-j', '-4', 'addr']))
        if not any(a['local'] == self.underlay for iface in addresses for a in iface.get('addr_info', [])):
            raise RuntimeError('underlay address is not local')
        self.claim_resources()
        command([IP, 'netns', 'add', self.name])
        command([IP, 'link', 'add', self.temp, 'type', 'wireguard'])
        command([IP, 'link', 'set', self.temp, 'netns', self.name])
        command([IP, '-n', self.name, 'link', 'set', self.temp, 'name', 'wg0'])
        # Instantiate the UDP socket in its birthplace before/after moving the
        # interface: WireGuard retains the original underlay namespace.
        config = f'[Interface]\nPrivateKey = {self.key}\nListenPort = 51820\n[Peer]\nPublicKey = {self.peer_key}\nAllowedIPs = {self.remote}/32\nEndpoint = {self.endpoint}:51820\nPersistentKeepalive = 1\n'
        self.ns([WG, 'setconf', 'wg0', '/dev/stdin'], config)
        self.key = config = None
        command([IP, '-n', self.name, 'link', 'set', 'lo', 'up'])
        command([IP, '-n', self.name, 'addr', 'add', self.overlay + '/32', 'dev', 'wg0'])
        command([IP, '-n', self.name, 'link', 'set', 'wg0', 'mtu', '1320', 'up'])
        command([IP, '-n', self.name, 'route', 'add', self.remote + '/32', 'dev', 'wg0'])
        # No default route, no veth in the engine namespace, no plaintext NIC.
        base = f'''create table inet {self.table}
add chain inet {self.table} input {{ type filter hook input priority 10; policy drop; }}
add chain inet {self.table} output {{ type filter hook output priority 10; policy drop; }}
add chain inet {self.table} forward {{ type filter hook forward priority 10; policy drop; }}
add rule inet {self.table} input iifname "lo" accept
add rule inet {self.table} output oifname "lo" accept
'''
        underlay = base + f'''add rule inet {self.table} input ip saddr {self.endpoint} ip daddr {self.underlay} udp sport 51820 udp dport 51820 accept
add rule inet {self.table} output ip saddr {self.underlay} ip daddr {self.endpoint} udp sport 51820 udp dport 51820 accept
add rule inet {self.table} input ip saddr {self.endpoint} ip daddr {self.underlay} tcp dport 12310 accept
add rule inet {self.table} input ip saddr {self.endpoint} ip daddr {self.underlay} tcp sport 12310 ct state established accept
add rule inet {self.table} output ip saddr {self.underlay} ip daddr {self.endpoint} tcp dport 12310 accept
add rule inet {self.table} output ip saddr {self.underlay} ip daddr {self.endpoint} tcp sport 12310 ct state established accept
'''
        # Only the dedicated Cocoon service UID may use these explicit TCP
        # destinations. The backend remains in its separate overlay namespace.
        for entry in self.service_egress:
            ip, port = entry['ip'], entry['port']
            underlay += f'add rule inet {self.table} output meta skuid 10001 ip daddr {ip} tcp dport {port} accept\n'
            underlay += f'add rule inet {self.table} input ip saddr {ip} tcp sport {port} ct state established accept\n'
        command([NFT, '-f', '-'], underlay)
        engine = base + f'''add rule inet {self.table} input iifname "wg0" ip saddr {self.remote} ip daddr {self.overlay} accept
add rule inet {self.table} output oifname "wg0" ip saddr {self.overlay} ip daddr {self.remote} accept
'''
        self.ns([NFT, '-f', '-'], engine)
        self.expected_fw = (self.nft(), self.nft(True))
        self.verify()
        original = os.open('/proc/self/ns/net', os.O_RDONLY | os.O_CLOEXEC)
        engine_fd = os.open('/run/netns/' + self.name, os.O_RDONLY | os.O_CLOEXEC)
        try:
            setns(engine_fd)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind((self.overlay, 29998))
            self.sock.setblocking(False)
        finally:
            setns(original)
            os.close(engine_fd); os.close(original)

    def verify(self):
        links = json.loads(command([IP, '-n', self.name, '-j', 'link']))
        if {link['ifname'] for link in links} != {'lo', 'wg0'}:
            raise RuntimeError('engine interfaces changed')
        wg = next(link for link in links if link['ifname'] == 'wg0')
        if 'UP' not in wg['flags'] or wg['mtu'] != 1320:
            raise RuntimeError('WireGuard interface down or changed')
        routes = json.loads(command([IP, '-n', self.name, '-j', '-4', 'route', 'show', 'table', 'all']))
        if any(r.get('type', 'unicast') == 'unicast' and (r.get('dst') not in (self.remote, self.remote + '/32') or r.get('dev') != 'wg0') for r in routes):
            raise RuntimeError('unexpected engine route')
        if self.ns([WG, 'show', 'wg0', 'public-key']).strip() != self.public:
            raise RuntimeError('local network key changed')
        if self.ns([WG, 'show', 'wg0', 'allowed-ips']).split() != [self.peer_key, self.remote + '/32']:
            raise RuntimeError('WireGuard roster changed')
        if self.expected_fw and (self.nft(), self.nft(True)) != self.expected_fw:
            raise RuntimeError('network firewall changed')

    def run(self):
        last_reply = time.monotonic()
        next_check = next_probe = 0
        pending = {}
        healthy = False
        while not STOP:
            now = time.monotonic()
            if now >= next_check:
                self.verify()
                status('configured', healthy=healthy)
                next_check = now + 0.4
            if now >= next_probe:
                nonce = os.urandom(24)
                pending[nonce] = now
                pending = {key: sent for key, sent in pending.items() if now - sent < 2}
                self.sock.sendto(b'CP8Q' + nonce, (self.remote, 29998))
                next_probe = now + 0.2
            if now - last_reply > (2 if healthy else 15):
                raise RuntimeError('encrypted data probe deadline exceeded')
            readable, _, _ = select.select([0, self.sock], [], [], 0.05)
            if 0 in readable:
                if not os.read(0, 256): break
                raise RuntimeError('unexpected network owner command')
            if self.sock in readable:
                packet, source = self.sock.recvfrom(128)
                if source != (self.remote, 29998) or len(packet) != 28: continue
                if packet[:4] == b'CP8Q':
                    self.sock.sendto(b'CP8A' + packet[4:], source)
                elif packet[:4] == b'CP8A' and packet[4:] in pending:
                    last_reply = time.monotonic(); healthy = True
                    del pending[packet[4:]]

    def cleanup(self):
        self.key = None
        if self.sock: self.sock.close(); self.sock = None
        if not self.owns_resources(): return
        errors = []
        # Remove wg0 first: deleting a namespace mount alone leaves keys alive
        # while a backend or another inspector still holds that namespace.
        if os.path.exists('/run/netns/' + self.name):
            try:
                links = json.loads(command([IP, '-n', self.name, '-j', 'link']))
                for link in links:
                    if link['ifname'] in ('wg0', self.temp):
                        command([IP, '-n', self.name, 'link', 'del', link['ifname']])
                if self.has_table(True): self.ns([NFT, 'delete', 'table', 'inet', self.table])
                command([IP, 'netns', 'del', self.name])
            except Exception: errors.append('namespace cleanup failed')
        try:
            if any(link['ifname'] == self.temp for link in json.loads(command([IP, '-j', 'link']))):
                command([IP, 'link', 'del', self.temp])
        except Exception: errors.append('underlay link cleanup failed')
        try:
            if self.has_table(): command([NFT, 'delete', 'table', 'inet', self.table])
        except Exception: errors.append('firewall cleanup failed')
        if errors: raise RuntimeError('; '.join(errors))
        os.unlink(self.claim)


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE: no core/ptrace of key material.
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    network = None
    failure = None
    clean = False
    try:
        if os.geteuid() != 0: raise RuntimeError('root required')
        # Avoid buffered stdin: after this line the same fd is the owner lease.
        line = bytearray()
        while not line.endswith(b'\n') and len(line) <= 4096:
            data = os.read(0, 1)
            if not data: raise RuntimeError('owner left before network setup')
            line.extend(data)
        if len(line) > 4096: raise RuntimeError('network setup limit exceeded')
        cfg = json.loads(line)
        line[:] = b'\0' * len(line)
        network = Network(cfg)
        if not network.cleanup_only:
            network.setup()
            status('configured', healthy=False)
            network.run()
    except Exception as exc:
        failure = str(exc)
    finally:
        try:
            if network: network.cleanup()
            clean = True
        except Exception as exc:
            failure = str(exc)
        status('stopped', clean=clean, **({'failure': failure} if failure else {}))
    return 1 if failure else 0


if __name__ == '__main__':
    raise SystemExit(main())
