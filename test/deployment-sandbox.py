#!/usr/bin/env python3
"""Read-only host-side audit of a running deployment rank (requires root)."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def require(value, message):
    if not value:
        raise AssertionError(message)


def audit(state):
    state = Path(state)
    record = json.loads((state / 'container.json').read_text())
    status = json.loads((state / 'agent-status.json').read_text())
    obj = json.loads(subprocess.check_output(['docker', 'inspect', record['id']], text=True))[0]
    require(obj['State']['Running'], 'container is not running')
    init = obj['State']['Pid']
    root = Path('/proc') / str(init) / 'root'
    engine = (root / 'run/netns' / status['group']['network']['namespace']).stat().st_ino
    underlay = (root / 'run/netns/underlay').stat().st_ino
    require(engine != underlay, 'engine shares underlay namespace')
    cgroup = Path(record['cgroup'])
    pids = {int(pid) for p in cgroup.rglob('cgroup.procs') for pid in p.read_text().split()}
    require(record.get('cgroup_validated') and init in pids, 'container cgroup is not observable')
    processes = []
    for pid in sorted(pids):
        proc = Path('/proc') / str(pid)
        try:
            fields = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines() if ':' in line)
            processes.append({'pid': pid, 'uid': list(map(int, fields['Uid'].split())),
                              'gid': list(map(int, fields['Gid'].split())),
                              'netns': (proc / 'ns/net').stat().st_ino,
                              'pidns': (proc / 'ns/pid').stat().st_ino,
                              'caps': {key: int(fields[key].strip(), 16) for key in ('CapEff', 'CapBnd', 'CapPrm', 'CapInh', 'CapAmb')},
                              'no_new_privs': fields['NoNewPrivs'].strip(),
                              'cgroup': (proc / 'cgroup').read_text().strip(),
                              'command': (proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')})
        except FileNotFoundError:
            require(not proc.exists(), 'live process disappeared during audit')
    underlay_pids = [p['pid'] for p in processes if p['netns'] == underlay]
    require(underlay_pids, 'no underlay processes found')
    sockets = {}
    for table, column in (('tcp', 9), ('tcp6', 9), ('udp', 9), ('udp6', 9),
                          ('raw', 9), ('raw6', 9), ('netlink', 9), ('packet', 8), ('unix', 6)):
        path = Path('/proc') / str(underlay_pids[0]) / 'net' / table
        if table == 'packet' and not path.exists():continue
        rows = path.read_text().splitlines()[1:]
        for row in rows:
            inode = row.split()[column]
            if inode != '0':
                sockets['socket:[' + inode + ']'] = (table, row)
    backend = [p for p in processes if p['uid'][0] == 65534]
    services = [p for p in processes if p['uid'][0] == 10001]
    require(backend, 'no sandboxed backend/helper processes')
    private_pidns = (Path('/proc') / str(init) / 'ns/pid').stat().st_ino
    require(private_pidns != Path('/proc/1/ns/pid').stat().st_ino, 'container shares host PID namespace')
    for proc in backend:
        require(proc['uid'] == [65534] * 4 and proc['gid'] == [65534] * 4, 'backend credentials differ')
        require(proc['netns'] == engine, 'backend is outside engine network namespace')
        require(proc['pidns'] == private_pidns and record['id'] in proc['cgroup'], 'backend escaped PID/cgroup boundary')
        require(not any(proc['caps'].values()) and proc['no_new_privs'] == '1', 'backend has privileges')
        descriptors = []
        private_uds = 0
        for fd in (Path('/proc') / str(proc['pid']) / 'fd').iterdir():
            try:
                target = os.readlink(fd)
            except FileNotFoundError:
                continue
            origin = sockets.get(target)
            # Agent/gate intentionally connect across namespaces to these two
            # pathname UDS. An accepted Unix socket can appear in the connector's
            # netns table (observed for health.sock); it grants no IP networking.
            allowed_uds = bool(origin and origin[0] == 'unix' and
                               origin[1].split(maxsplit=7)[-1] in
                               (status['backend_socket'], status['health_socket']))
            require((target not in sockets or allowed_uds) and target != 'net:[' + str(underlay) + ']',
                    'backend underlay FD: '+str(proc['pid'])+' '+target+' '+str(sockets.get(target)))
            require('docker.sock' not in target and 'containerd.sock' not in target, 'backend has runtime socket FD')
            private_uds += int(allowed_uds)
            descriptors.append(target)
        proc['fd_count'] = len(descriptors)
        proc['accepted_private_uds'] = private_uds
        proc['fd_audit'] = 'no underlay IP/namespace FD; only configured private UDS allowed'
    for proc in services:
        require(proc['uid'] == [10001] * 4 and proc['gid'] == [10001] * 4, 'service credentials differ')
        # Services drop all usable capabilities via setuid. Their inherited
        # bounding ceiling is inert: NoNewPrivs forbids acquiring capabilities
        # from an executable, and permitted/inheritable/ambient sets are empty.
        require(proc['netns'] == underlay and not any(proc['caps'][key] for key in ('CapEff', 'CapPrm', 'CapInh', 'CapAmb')) and proc['no_new_privs'] == '1',
                'service namespace/privileges differ')
    return {'ok': True, 'container': record['id'], 'cgroup': str(cgroup), 'engine_netns': engine,
            'underlay_netns': underlay, 'underlay_socket_count': len(sockets),
            'backend': backend, 'services': services}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.state), indent=2))
