#!/usr/bin/env python3
"""Shared process ownership and bounded UDS -> fixed loopback HTTP bridge.

Launched only by the compiled adapter inside its engine namespace. No listening
TCP socket, arbitrary destination, shell, or runtime backend command override.
The importable Helper is also exercised against an HTTP fixture on macOS.
"""
import asyncio
import contextlib
import ctypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid

CHUNK = 16384
HEADER_LIMIT = 8192
BODY_LIMIT = 8192
WIRE_LIMIT = 2 * 1048576
ENDPOINT = ('127.0.0.1', 30000)


async def close(writer):
    if writer:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), .5)


def parse_fields(lines):
    fields = {}
    for line in lines:
        name, sep, val = line.partition(b':')
        if not sep or not name or any(c not in b'!#$%&\'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ' for c in name):
            raise ValueError('invalid HTTP header')
        name = name.lower()
        if name in fields:
            raise ValueError('duplicate HTTP header')
        fields[name] = val.strip()
    return fields


async def header(reader):
    value = await reader.readuntil(b'\r\n\r\n')
    if len(value) > HEADER_LIMIT:
        raise ValueError('HTTP header limit')
    lines = value[:-4].split(b'\r\n')
    fields = parse_fields(lines[1:])
    return lines[0], fields, value


async def relay_response(source, writer, complete):
    """Forward one bounded HTTP response, preserving its wire framing."""
    total = 0

    async def send(data, final=False):
        nonlocal total
        total += len(data)
        if total > WIRE_LIMIT:
            raise ValueError('response wire limit')
        writer.write(data)
        if final:
            # No await between queuing the last bytes and releasing admission:
            # gate can close and submit its next request as soon as it sees them.
            # The inference is over even if this final drain observes disconnect.
            complete()
        await writer.drain()

    async def body(length, final=False):
        while length:
            chunk = await source.read(min(CHUNK, length))
            if not chunk:
                raise asyncio.IncompleteReadError(b'', length)
            length -= len(chunk)
            await send(chunk, final=final and length == 0)

    async def line():
        value = await source.readuntil(b'\r\n')
        if len(value) > HEADER_LIMIT:
            raise ValueError('HTTP framing line limit')
        return value

    first, fields, raw = await header(source)
    status = first.split(b' ', 2)
    # Match gate: inference responses require a final, body-capable status.
    if (len(status) < 2 or status[0] not in (b'HTTP/1.0', b'HTTP/1.1') or
            len(status[1]) != 3 or not status[1].isdigit() or
            int(status[1]) < 200 or int(status[1]) in (204, 304)):
        raise ValueError('unsupported response status')
    transfer = fields.get(b'transfer-encoding')
    length = fields.get(b'content-length')
    if transfer is not None:
        if transfer.lower() != b'chunked' or length is not None:
            raise ValueError('unsupported response framing')
        await send(raw)
        while True:
            raw = await line()
            size = raw[:-2].split(b';', 1)[0]
            if not size or any(c not in b'0123456789abcdefABCDEF' for c in size):
                raise ValueError('invalid chunk size')
            size = int(size, 16)
            if size > WIRE_LIMIT - total:
                raise ValueError('response wire limit')
            await send(raw)
            if size == 0:
                trailers = []
                trailer_bytes = 0
                while True:
                    raw = await line()
                    trailer_bytes += len(raw)
                    if trailer_bytes > HEADER_LIMIT:
                        raise ValueError('HTTP trailer limit')
                    if raw == b'\r\n':
                        fields = parse_fields(trailers)
                        if b'content-length' in fields or b'transfer-encoding' in fields:
                            raise ValueError('framing field in trailers')
                        await send(raw, final=True)
                        return
                    trailers.append(raw[:-2])
                    await send(raw)
            await body(size)
            raw = await source.readexactly(2)
            if raw != b'\r\n':
                raise ValueError('invalid chunk terminator')
            await send(raw)
    elif length is not None:
        if not length.isdigit():
            raise ValueError('invalid response length')
        length = int(length)
        await send(raw, final=length == 0)
        await body(length, final=True)
    else:
        # HTTP permits close-delimited bodies only when no explicit framing is
        # supplied; EOF is still required in this case.
        await send(raw)
        while chunk := await source.read(CHUNK):
            await send(chunk)
        complete()


async def reply(writer, code, body):
    data = json.dumps(body, separators=(',', ':')).encode()
    writer.write(f'HTTP/1.1 {code} Response\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n'.encode() + data)
    await writer.drain()


class Helper:
    def __init__(self, config, backend_alive, *, endpoint=ENDPOINT):
        self.config, self.backend_alive, self.endpoint = config, backend_alive, endpoint
        self.servers = []
        self.tasks = set()
        self.active = set()
        self.connections = 0
        self.completed = 0
        self.cancelled = 0
        self.abort_failures = 0
        self.fatal = asyncio.Event()
        self.stopping = False

    async def connect(self):
        reader, writer = await asyncio.wait_for(asyncio.open_connection(*self.endpoint, limit=CHUNK), 2)
        writer.transport.set_write_buffer_limits(high=CHUNK, low=CHUNK // 2)
        writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
        return reader, writer

    async def control(self, path, body=None):
        reader, writer = await self.connect()
        try:
            payload = b'' if body is None else json.dumps(body).encode()
            writer.write((f'{"GET" if body is None else "POST"} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n').encode() + payload)
            await writer.drain()
            first, _, _ = await header(reader)
            code = int(first.split()[1])
            # No unbounded administrative response buffering; only status matters.
            return code
        finally:
            await close(writer)

    async def backend_health(self):
        return await asyncio.wait_for(self.control('/health'), 2) == 200

    def prepare_request(self, obj, rid):
        obj['rid'] = rid
        return ''

    async def abort(self, rid):
        self.cancelled += 1
        try:
            # Disconnect is also observed by SGLang. Repeat the addressed abort
            # after one scheduling interval to cover registration in flight.
            for delay in (0, .1):
                if delay:
                    await asyncio.sleep(delay)
                if await asyncio.wait_for(self.control('/abort_request', {'rid': rid}), 2) != 200:
                    raise RuntimeError('abort was not acknowledged')
        except Exception:
            self.abort_failures += 1
            # Never advertise a healthy group with unconfirmed cancellation.
            self.fatal.set()

    def accepted(self, reader, writer, health=False):
        if self.connections >= 16 or self.stopping:
            writer.close()
            return
        self.connections += 1
        task = asyncio.create_task(self.handle(reader, writer, health))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def start(self):
        cfg = self.config
        paths = [(cfg['health_socket'], True)]
        if cfg['rank'] == 0:
            paths.append((cfg['api_socket'], False))
        for path, health in paths:
            server = await asyncio.start_unix_server(lambda r, w, h=health: self.accepted(r, w, h), path, limit=CHUNK)
            os.chmod(path, 0o600)
            self.servers.append(server)

    async def stop(self):
        self.stopping = True
        for server in self.servers:
            server.close()
            await server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for key in ('api_socket', 'health_socket'):
            with contextlib.suppress(FileNotFoundError):
                Path(self.config[key]).unlink()

    async def handle(self, reader, writer, health):
        upstream = None
        watch = relay = None
        rid = None
        completed = False
        try:
            writer.transport.set_write_buffer_limits(high=CHUNK, low=CHUNK // 2)
            first, fields, _ = await asyncio.wait_for(header(reader), 2)
            method, path, version = first.decode('ascii').split(' ')
            if version != 'HTTP/1.1' or b'transfer-encoding' in fields:
                raise ValueError('unsupported request framing')
            length = fields.get(b'content-length', b'0')
            if not length.isdigit() or int(length) > BODY_LIMIT:
                raise ValueError('request body limit')
            body = await asyncio.wait_for(reader.readexactly(int(length)), 2)
            if health:
                if method != 'GET' or path != '/health' or body:
                    await reply(writer, 404, {'error': 'unknown health operation'})
                    return
                ok = self.backend_alive() and not self.fatal.is_set() and await self.backend_health()
                await reply(writer, 200 if ok else 503, {
                    'status': 'ok' if ok else 'unhealthy', 'backend_alive': bool(ok),
                    'rank': self.config['rank'], 'config_digest': self.config['config_digest'],
                    'backend_version': self.config['backend_version'], 'active_requests': len(self.active),
                    'completed': self.completed, 'cancelled': self.cancelled, 'abort_failures': self.abort_failures})
                return
            if method != 'POST' or path not in ('/v1/chat/completions', '/v1/completions'):
                await reply(writer, 404, {'error': 'unsupported API'})
                return
            if self.fatal.is_set() or not self.backend_alive():
                await reply(writer, 503, {'error': 'backend unavailable'})
                return
            if self.active:
                await reply(writer, 429, {'error': 'profile permits one inference request'})
                return
            obj = json.loads(body)
            if not isinstance(obj, dict) or obj.get('model') != self.config['model_manifest']['model']['id']:
                raise ValueError('wrong model')
            if obj.get('n', 1) != 1 or obj.get('best_of', 1) != 1 or not isinstance(obj.get('stream', False), bool):
                raise ValueError('profile supports one output sequence')
            if path == '/v1/completions' and not isinstance(obj.get('prompt'), str):
                raise ValueError('profile supports one text prompt')
            # These engine extensions can bypass the pinned model/profile. They
            # are not part of the supported text API; client rid is overwritten.
            for name in ('lora_path', 'custom_logit_processor', 'bootstrap_host', 'bootstrap_port',
                         'bootstrap_room', 'routed_dp_rank', 'disagg_prefill_dp_rank', 'session_params', 'background'):
                if name in obj:
                    raise ValueError('unsupported backend extension')
            request_id = uuid.uuid4().hex
            extra_headers = self.prepare_request(obj, request_id)
            payload = json.dumps(obj, separators=(',', ':')).encode()
            rid = request_id
            self.active.add(rid)
            source, upstream = await self.connect()
            upstream.write((f'POST {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Type: application/json\r\n{extra_headers}Content-Length: {len(payload)}\r\n\r\n').encode() + payload)
            # Start watching disconnect before waiting for backend headers or a
            # slow consumer. Both kernel and asyncio buffers have fixed bounds.
            watch = asyncio.create_task(reader.read(1))

            def complete():
                nonlocal completed
                completed = True
                self.completed += 1
                self.active.discard(rid)

            async def forward():
                await upstream.drain()
                await relay_response(source, writer, complete)

            relay = asyncio.create_task(forward())
            done, _ = await asyncio.wait((watch, relay), timeout=120, return_when=asyncio.FIRST_COMPLETED)
            if relay in done:
                await relay
        except (ValueError, KeyError, TypeError, UnicodeError, asyncio.LimitOverrunError):
            if rid is None:
                with contextlib.suppress(Exception):
                    await reply(writer, 400, {'error': 'invalid request for backend profile'})
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass  # No synthetic successful terminal on upstream failure.
        finally:
            for task in (watch, relay):
                if task:
                    task.cancel()
            await asyncio.gather(*(t for t in (watch, relay) if t), return_exceptions=True)
            await close(upstream)
            if rid and not completed:
                await self.abort(rid)
            if rid:
                self.active.discard(rid)
            await close(writer)
            self.connections -= 1


def verify_model(config):
    root = Path(config['model_path'])
    manifest = config['model_manifest']
    expected = manifest['files']
    actual = {str(p.relative_to(root)) for p in root.rglob('*')
              if p.is_file() and '.cache' not in p.parts and p.name != 'manifest.json'}
    if actual != set(expected):
        raise ValueError('model file set differs from compiled manifest')
    for name, info in expected.items():
        path = root / name
        if path.is_symlink() or path.stat().st_size != info['bytes']:
            raise ValueError('model artifact size/path mismatch')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while block := stream.read(16 << 20):
                digest.update(block)
        if digest.hexdigest() != info['sha256']:
            raise ValueError('model artifact digest mismatch')


def descendants():
    """All current descendants; subreaper adopts grandchildren on parent exit."""
    result, parents = set(), {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
            parents[int(path.parent.name)] = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
    frontier = {os.getpid()}
    while frontier:
        frontier = {p for p, parent in parents.items() if parent in frontier and p not in result}
        result.update(frontier)
    return result


async def run(config, helper_type=Helper, package='sglang'):
    if sys.platform != 'linux' or os.geteuid() == 0:
        raise RuntimeError('backend must run as an unprivileged Linux process')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
        raise RuntimeError('cannot own backend descendants')
    if importlib.metadata.version(package) != config['backend_version']:
        raise RuntimeError('backend version differs from profile')
    if not os.statvfs(config['model_path']).f_flag & os.ST_RDONLY:
        raise RuntimeError('model mount must be read-only')
    verify_model(config)
    child = subprocess.Popen(config['argv'], stdin=subprocess.DEVNULL, cwd=Path(config['api_socket']).parent)
    helper = helper_type(config, lambda: child.poll() is None)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    failed = False
    try:
        await helper.start()
        while not stopping.is_set():
            if child.poll() is not None or helper.fatal.is_set():
                failed = True
                break
            await asyncio.sleep(.05)
    finally:
        await helper.stop()
        # Signals stay within this subreaper's tree, including children that
        # changed process groups. Reap before reporting cleanup success.
        for sig, seconds in ((signal.SIGTERM, 2), (signal.SIGKILL, 3)):
            deadline = time.monotonic() + seconds
            while True:
                child.poll()
                owned = descendants()
                for pid in owned:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, sig)
                while True:
                    try:
                        pid, _ = os.waitpid(-1, os.WNOHANG)
                        if pid == 0:
                            break
                    except ChildProcessError:
                        break
                if not descendants() or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(.05)
        if descendants():
            raise RuntimeError('backend descendants survived cleanup')
    if failed:
        raise RuntimeError('backend process or cancellation failed')


def main(helper_type=Helper, package='sglang'):
    try:
        if len(sys.argv) != 2:
            raise ValueError('compiled adapter configuration required')
        asyncio.run(run(json.loads(sys.argv[1]), helper_type, package))
    except Exception as exc:
        # Do not log request bodies, upstream diagnostics, or user secrets.
        print('Backend failed: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
