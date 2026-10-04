"""Small offline evidence checks; no dependency on tcpdump or root locally."""
import json
from pathlib import Path
import struct


def network_isolation(status):
    interfaces = json.loads(status['interfaces'])
    assert {x['ifname'] for x in interfaces} == {'lo', 'wg0'}, interfaces
    routes = json.loads(status['routes'])
    assert routes and all(x['dev'] == 'wg0' and x.get('dst') != 'default' for x in routes), routes
    container = status['container']
    assert container['HostConfig']['NetworkMode'] == 'none'
    assert not container['HostConfig']['Privileged']
    assert 'NET_ADMIN' not in (container['HostConfig'].get('CapAdd') or [])
    assert not container['HostConfig']['PortBindings']
    assert all(m['Destination'] != '/var/run/docker.sock' for m in container['Mounts'])
    env = dict(x.split('=', 1) for x in container['Config']['Env'])
    assert env['NCCL_NET'] == 'Socket' and env['NCCL_SOCKET_IFNAME'] == '=wg0'
    assert env['GLOO_SOCKET_IFNAME'] == 'wg0'
    for name in ('NCCL_IB_DISABLE', 'NCCL_P2P_DISABLE', 'NCCL_SHM_DISABLE'):
        assert env[name] == '1'
    return {'passed': True, 'interfaces': ['lo', 'wg0'], 'default_route': False}


def capture_summary(path, wg_port):
    counts = {'packets': 0, 'wireguard_udp': 0, 'tcp': 0, 'other_udp': 0}
    with Path(path).open('rb') as source:
        header = source.read(24)
        endian = {b'\xd4\xc3\xb2\xa1': '<', b'\xa1\xb2\xc3\xd4': '>'}[header[:4]]
        assert struct.unpack(endian+'I', header[20:24])[0] == 1, 'expected Ethernet pcap'
        while record := source.read(16):
            assert len(record) == 16
            size = struct.unpack(endian+'IIII', record)[2]
            frame = source.read(size)
            assert len(frame) == size
            counts['packets'] += 1
            offset, ethertype = 14, int.from_bytes(frame[12:14], 'big')
            while ethertype in (0x8100, 0x88a8):
                ethertype = int.from_bytes(frame[offset+2:offset+4], 'big')
                offset += 4
            if ethertype != 0x0800:
                continue
            protocol = frame[offset+9]
            if protocol == 6:
                counts['tcp'] += 1
            if protocol == 17:
                udp = offset + 4 * (frame[offset] & 15)
                ports = struct.unpack('!HH', frame[udp:udp+4])
                counts['wireguard_udp' if ports == (wg_port, wg_port) else 'other_udp'] += 1
    counts['passed'] = counts['wireguard_udp'] > 0 and counts['tcp'] == 0 and counts['other_udp'] == 0
    return counts
