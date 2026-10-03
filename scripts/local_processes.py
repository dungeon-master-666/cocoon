"""Process ownership for the local dev stack and its smoke tests (POSIX)."""

import os
import signal
import subprocess
import time
from pathlib import Path


def group_alive(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # A just-orphaned group can briefly contain only zombies on macOS.
        # Conservatively keep waiting until the kernel has reaped the group.
        return True


class Processes:
    def __init__(self, log_dir, grace=5):
        self.log_dir = Path(log_dir)
        self.grace = grace
        self.children = []
        self.starting = False
        self.pending_signal = None

    def __enter__(self):
        self.handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.handlers[sig] = signal.signal(sig, self.interrupted)
        return self

    def interrupted(self, sig, frame):
        if self.starting:
            self.pending_signal = sig
            return
        raise SystemExit(128 + sig)

    def start(self, name, command, **kwargs):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.starting = True
        try:
            with (self.log_dir / f'{name}.log').open('ab') as log:
                proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, start_new_session=True, **kwargs)
            self.children.append((name, proc))
        finally:
            self.starting = False
        if self.pending_signal is not None:
            raise SystemExit(128 + self.pending_signal)
        return proc

    def run(self, name, command, timeout, check=True, **kwargs):
        proc = self.start(name, command, **kwargs)
        code = proc.wait(timeout=timeout)
        if code and check:
            raise subprocess.CalledProcessError(code, command)
        # Synchronous helpers have completed; keep them out of the liveness check.
        # A helper that leaves descendants behind is an error, cleaned in __exit__.
        if group_alive(proc.pid):
            raise RuntimeError(f'{name} left child processes running')
        self.children.remove((name, proc))

    def check(self):
        for name, proc in self.children:
            if proc.poll() is not None:
                raise RuntimeError(f'{name} exited with {proc.returncode}; see {self.log_dir / (name + ".log")}')

    def stop(self):
        # Signal whole groups, even if the direct child has already exited.
        # A crashed launcher/compiler must not leave its descendants running.
        for _, proc in reversed(self.children):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + self.grace
        while time.monotonic() < deadline:
            for _, proc in self.children:
                proc.poll()
            if not any(group_alive(proc.pid) for _, proc in self.children):
                break
            time.sleep(0.05)
        for _, proc in reversed(self.children):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for _, proc in self.children:
            proc.wait(timeout=5)

    def __exit__(self, *exc):
        for sig in self.handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            self.stop()
        finally:
            for sig, handler in self.handlers.items():
                signal.signal(sig, handler)


LOCAL_PORTS = {
    'socks': 8116,
    'client_http': 10000, 'client_rpc': 10001,
    'proxy_http': 11000, 'proxy_worker_tls': 11001, 'proxy_client_tls': 11002,
    'proxy_worker_rpc': 11101, 'proxy_client_rpc': 11102,
    'worker_http': 12000, 'worker_rpc': 12001,
    'key_manager_http': 13000, 'key_manager_tls': 13001, 'key_manager_rpc': 13101,
}


def local_ports(offset):
    ports = {name: port + offset for name, port in LOCAL_PORTS.items()}
    if min(ports.values()) < 1024 or max(ports.values()) > 65535:
        raise ValueError('local port offset puts ports outside 1024..65535')
    return ports


def check_ports(ports):
    import socket
    sockets = []
    try:
        for port in ports:
            sock = socket.socket()
            sockets.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Probe the actual address used by the stack. On macOS a wildcard
            # bind can coexist with (and miss) an existing loopback listener.
            sock.bind(('127.0.0.1', port))
            sock.listen(1)
    finally:
        for sock in sockets:
            sock.close()
