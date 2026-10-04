#!/usr/bin/env python3
"""Generate portable per-host dev bundles. No SSH, host mutation or secrets."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shutil

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def keys(obj, required, optional=()):
    if not isinstance(obj, dict) or not set(required) <= set(obj) or set(obj) - set(required) - set(optional):
        raise ValueError('missing or unknown fields: expected ' + ', '.join(required))


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'expected integer in {low}..{high}')
    return value


def ipv4(value):
    ip = ipaddress.IPv4Address(value)
    if (str(ip) != value or ip.is_loopback or ip.is_multicast or ip.is_unspecified or
            int(ip) == 0xffffffff or ip in ipaddress.ip_network('10.231.0.0/24')):
        raise ValueError('expected unicast IPv4 outside engine overlay')
    return value


def absolute(value):
    if not isinstance(value, str) or not value.startswith('/') or any(c in value for c in ',\n\r\x00'):
        raise ValueError('expected absolute path without commas or newlines')
    if '..' in Path(value).parts:
        raise ValueError('parent traversal in path')
    return value


def cpus(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*', value):
        raise ValueError('invalid CPU set')
    result = set()
    for part in value.split(','):
        lo, _, hi = part.partition('-')
        lo, hi = int(lo), int(hi or lo)
        if not 0 <= lo <= hi <= 65535:
            raise ValueError('invalid CPU range')
        result.update(range(lo, hi + 1))
    return result


def validate(config, artifact, models):
    config = json.loads(json.dumps(config))
    keys(config, ('schema', 'mode', 'backend', 'model', 'hosts', 'ranks'),
         ('limits', 'services', 'startup_timeout'))
    if type(config['schema']) is not int or config['schema'] != 1 or config['mode'] != 'dev':
        raise ValueError('only schema 1, explicit dev mode is supported')
    backend = config['backend']
    if backend not in ('simulator', 'sglang', 'vllm'):
        raise ValueError('unsupported backend')
    if config['model'] not in (('simulator',) if backend == 'simulator' else ('small', 'large')):
        raise ValueError('unsupported model profile')
    keys(artifact, ('schema', 'mode', 'backend', 'architecture', 'image_id', 'model_catalog_sha256'),
         ('base_image', 'source_sha256'))
    if (type(artifact['schema']) is not int or artifact['schema'] != 1 or artifact['mode'] != 'dev' or artifact['backend'] != backend or
            artifact['architecture'] not in ('amd64', 'arm64') or
            not re.fullmatch(r'sha256:[0-9a-f]{64}', artifact['image_id']) or
            artifact['model_catalog_sha256'] != digest(models)):
        raise ValueError('image artifact does not match selected dev profile/catalogue')
    limits = config.setdefault('limits', {})
    keys(limits, (), ('max_model_len', 'max_num_seqs', 'max_num_batched_tokens'))
    gpu = backend != 'simulator'
    context = limits.setdefault('max_model_len', 4096 if gpu else 512)
    integer(context, 512 if gpu else 16, 4096 if gpu else 512)
    integer(limits.setdefault('max_num_seqs', 1), 1, 1 if gpu else 2)
    integer(limits.setdefault('max_num_batched_tokens', context), context, 4096 if gpu else 512)
    integer(config.setdefault('startup_timeout', 900 if gpu else 60), 10, 1200)
    hosts = config['hosts']
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 2:
        raise ValueError('one or two hosts required')
    host_ids = set()
    for host in hosts:
        keys(host, ('id', 'hostname', 'interface'))
        if (not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}', host['id']) or
                not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,252}', host['hostname']) or
                not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', host['interface']) or host['id'] in host_ids):
            raise ValueError('invalid or duplicate host')
        host_ids.add(host['id'])
    if len({h['hostname'] for h in hosts}) != len(hosts):
        raise ValueError('use a single host entry for ranks on the same machine')
    if not isinstance(config['ranks'], list) or len(config['ranks']) != 2:
        raise ValueError('exactly two ranks required, head then member')
    devices, endpoints, cpu_sets = set(), set(), {}
    for node in config['ranks']:
        keys(node, ('host', 'underlay', 'cpus', 'memory_mib'), ('gpu', 'model_root', 'gateway'))
        if node['host'] not in host_ids:
            raise ValueError('rank refers to unknown host')
        address = ipaddress.IPv4Interface(node['underlay'])
        ipv4(str(address.ip))
        if address.network.prefixlen > 30 or address.ip in (address.network.network_address, address.network.broadcast_address):
            raise ValueError('underlay must be a usable LAN address/prefix')
        if str(address.ip) in endpoints:
            raise ValueError('duplicate underlay address')
        endpoints.add(str(address.ip))
        if 'gateway' in node:
            ipv4(node['gateway'])
            if ipaddress.IPv4Address(node['gateway']) not in address.network or node['gateway'] == str(address.ip):
                raise ValueError('gateway must be on the selected LAN')
        selected = cpus(node['cpus'])
        if selected & cpu_sets.setdefault(node['host'], set()):
            raise ValueError('overlapping CPU assignments on one host')
        cpu_sets[node['host']].update(selected)
        node['cpus'] = ','.join(map(str, sorted(selected)))
        integer(node['memory_mib'], 512, 1048576)
        if gpu:
            if not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]', node.get('gpu', '')):
                raise ValueError('GPU must be a PCI BDF, for example 0000:01:00.0')
            device = (node['host'], node['gpu'])
            if device in devices:
                raise ValueError('GPU assigned to multiple ranks')
            devices.add(device)
            absolute(node.get('model_root', ''))
        elif 'gpu' in node or 'model_root' in node:
            raise ValueError('simulator does not take a GPU/model mount')
    if {r['host'] for r in config['ranks']} != host_ids:
        raise ValueError('unused host entry')
    services = config.setdefault('services', {'mode': 'local-fake-ton', 'coefficient': 1000})
    keys(services, ('mode', 'coefficient'), ('proxy_ip', 'proxy_worker_port', 'proxy_client_port', 'key_manager_ip', 'key_manager_port'))
    integer(services['coefficient'], 0, 1000000000)
    if services['mode'] == 'external-fake-ton':
        for name in ('proxy_ip', 'key_manager_ip'):
            ipv4(services[name])
        for name in ('proxy_worker_port', 'proxy_client_port', 'key_manager_port'):
            integer(services[name], 1024, 65535)
    elif services != {'mode': 'local-fake-ton', 'coefficient': services['coefficient']}:
        raise ValueError('unknown services mode/fields')
    return config


def runtime(config, rank, models, deployment_id=None):
    node, peer = config['ranks'][rank], config['ranks'][1-rank]
    backend = config['backend']
    if backend == 'simulator':
        profile, model, identity = 'simulator-dev-pp2-wg-v1', 'cocoon-simulator', 'cocoon-simulator@v1:dev-fixture'
    else:
        m = models[config['model']]['model']
        profile = f"{backend}-qwen3-{'0.6b' if config['model']=='small' else '14b'}-dev-pp2-wg-v1"
        model, identity = m['id'], m['id'] + '@' + m['revision']
    agent = {'profile': profile, 'rank': rank, 'role': 'head' if rank == 0 else 'member',
             'limits': config['limits'], 'group': {'peer_port' if rank == 0 else 'listen_port': 12310},
             'network': {'underlay_ip': str(ipaddress.IPv4Interface(node['underlay']).ip),
                         'peer_ip': str(ipaddress.IPv4Interface(peer['underlay']).ip)}}
    service = None
    if deployment_id is not None:agent['deployment_id']=deployment_id
    if rank == 0:
        agent['gate'] = {'listen_port': 18080}
        s = config['services']
        if s['mode'] == 'external-fake-ton':
            agent['network']['service_egress'] = [
                {'ip': s['proxy_ip'], 'port': s['proxy_worker_port']},
                {'ip': s['key_manager_ip'], 'port': s['key_manager_port']}]
        service = {**s, 'model': model, 'model_identifier': identity,
                   'max_active_requests': config['limits']['max_num_seqs'],
                   'forward_requests_to': '127.0.0.1:18080'}
    return agent, service


def generate(config, artifact, output, models=None):
    models = models or json.loads((HERE.parent/'sglang-models.json').read_text())
    config = validate(config, artifact, models)
    tooling={name:hashlib.sha256((HERE/name).read_bytes()).hexdigest() for name in ('bundle.py','host.py')}
    identity = digest({'config': config, 'artifact': artifact, 'tooling':tooling})
    output = Path(output)
    if output.exists():
        raise ValueError('output directory must be new')
    output.mkdir(parents=True)
    for host in config['hosts']:
        dest = output/host['id'];dest.mkdir()
        local_ranks = [i for i,n in enumerate(config['ranks']) if n['host']==host['id']]
        payload = {'schema': 1, 'deployment_id': identity, 'host': host['id'],
                   'config': config, 'artifact': artifact, 'tooling':tooling,'models': models, 'ranks': local_ranks}
        (dest/'bundle.json').write_text(json.dumps(payload,indent=2)+'\n')
        for rank in local_ranks:
            agent, service = runtime(config,rank,models,identity)
            (dest/f'agent-{rank}.json').write_text(json.dumps(agent,indent=2)+'\n')
            if service:
                (dest/'services.json').write_text(json.dumps(service,indent=2)+'\n')
        for name in ('bundle.py','host.py'):
            shutil.copyfile(HERE/name,dest/name)
        (dest/'pipeline-deploy').write_text('#!/bin/sh\nset -eu\nexec python3 -B "$(dirname "$0")/host.py" "$@"\n')
        (dest/'pipeline-deploy').chmod(0o755)
        hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(dest.iterdir())}
        (dest/'files.sha256.json').write_text(json.dumps(hashes,indent=2)+'\n')
    return {'deployment_id':identity,'bundles':[str(output/h['id']) for h in config['hosts']]}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--artifact',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(generate(json.loads(args.config.read_text()),json.loads(args.artifact.read_text()),args.output),indent=2))
    except (ValueError,KeyError,OSError) as exc:
        parser.exit(1,'Bundle generation failed: '+str(exc)+'\n')
