#!/usr/bin/env python3
"""Native lifecycle regression tests; requires the Cocoon binaries in BUILD_DIR."""
import errno
import json
import os
from pathlib import Path
import random
import signal
import socket
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from local_processes import Processes, check_ports, group_alive, local_ports

BUILD = Path(os.environ.get('BUILD_DIR', ROOT / 'build/local')).resolve()


def wait_for(probe, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError('condition timed out')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix='cocoon-lifecycle-'))
        print(f'\nArtifacts: {self.path}', flush=True)
        for _ in range(100):
            self.offset = random.randrange(2000, 45000)
            self.ports = local_ports(self.offset)
            try:
                check_ports(self.ports.values())
                break
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
        else:
            self.fail('no free ports')

    def command(self, build=BUILD):
        return [sys.executable, '-u', str(ROOT / 'scripts/cocoon-launch'), '--local-all', '--skip-build',
                '--build-dir', str(build), '--local-run-dir', str(self.path / 'state'),
                '--local-port-offset', str(self.offset)]

    def manifest(self):
        path = self.path / 'state/processes.json'
        return json.loads(path.read_text()) if path.exists() else {'processes': {}}

    def assert_clean(self):
        pids = list(self.manifest()['processes'].values())
        wait_for(lambda: not any(group_alive(pid) for pid in pids))
        check_ports(self.ports.values())

    def test_sigint_and_sigterm(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig):
                with Processes(self.path / 'logs', grace=8) as owner:
                    proc = owner.start('launcher', self.command())
                    wait_for(lambda: len(self.manifest()['processes']) == 5)
                    proc.send_signal(sig)
                    self.assertEqual(proc.wait(timeout=10), 128 + sig)
                self.assert_clean()
                (self.path / 'state').rename(self.path / f'state-{sig}')

    def test_local_all_auto_build(self):
        command = self.command()
        command.remove('--skip-build')
        with Processes(self.path / 'logs', grace=8) as owner:
            proc = owner.start('launcher', command)
            wait_for(lambda: len(self.manifest()['processes']) == 5, timeout=60)
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=10), 143)
        self.assertTrue((self.path / 'state/logs/build.log').is_file())
        self.assert_clean()

    def test_partial_start_failure(self):
        fake_build = self.path / 'build'
        (fake_build / 'tee').mkdir(parents=True)
        # Rendering succeeds and router/proxy start, then worker executable is missing.
        for name in ('tee/cocoon-subst', 'tee/router', 'proxy-runner'):
            (fake_build / name).symlink_to(BUILD / name)
        with Processes(self.path / 'logs', grace=8) as owner:
            proc = owner.start('launcher', self.command(fake_build))
            self.assertNotEqual(proc.wait(timeout=15), 0)
        self.assertGreaterEqual(len(self.manifest()['processes']), 1)
        self.assert_clean()

    def test_child_crash_stops_stack(self):
        with Processes(self.path / 'logs', grace=8) as owner:
            proc = owner.start('launcher', self.command())
            wait_for(lambda: len(self.manifest()['processes']) == 5)
            os.kill(self.manifest()['processes']['worker'], signal.SIGKILL)
            self.assertNotEqual(proc.wait(timeout=10), 0)
        self.assert_clean()

    def test_existing_directory_preserved(self):
        state = self.path / 'state'
        state.mkdir()
        (state / 'keep').write_text('existing data')
        with Processes(self.path / 'logs', grace=8) as owner:
            proc = owner.start('launcher', self.command())
            self.assertNotEqual(proc.wait(timeout=10), 0)
        self.assertEqual((state / 'keep').read_text(), 'existing data')
        self.assertEqual(list(state.iterdir()), [state / 'keep'])

    def test_busy_port_does_not_touch_owner(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', self.ports['proxy_worker_tls']))
            occupied.listen()
            with Processes(self.path / 'logs', grace=8) as owner:
                proc = owner.start('launcher', self.command())
                self.assertNotEqual(proc.wait(timeout=10), 0)
            self.assertFalse((self.path / 'state').exists())
            with socket.create_connection(occupied.getsockname(), timeout=1):
                pass

    def test_escalation_kills_descendant_after_leader_exit(self):
        ready = self.path / 'ready'
        child = ('import os,signal,time; from pathlib import Path; '
                 'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                 f'Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)')
        parent = ('import subprocess,sys,time; '
                  f'p=subprocess.Popen([sys.executable,"-c",{child!r}]); '
                  'time.sleep(60)')
        with Processes(self.path / 'logs', grace=0.3) as owner:
            proc = owner.start('parent', [sys.executable, '-c', parent])
            wait_for(ready.exists)
            # TERM stops parent; its child ignores TERM and needs group KILL.
        wait_for(lambda: not group_alive(proc.pid))

    def smoke_command(self, *extra):
        return [sys.executable, str(ROOT / 'benchmark/smoke-local.py'), '--skip-build',
                '--build-dir', str(BUILD), '--output-dir', str(self.path / 'smoke'), *extra]

    def assert_smoke_clean(self, scenario):
        report = json.loads((self.path / 'smoke' / scenario / 'result.json').read_text())
        self.assertFalse(report['cleanup']['remaining_process_groups'])
        self.assertTrue(report['cleanup']['ports_released'])
        self.assertTrue(all(not group_alive(pid) for pid in report['owned_pids']))
        check_ports([report['backend_port'], *report['ports'].values()])
        return report

    def test_smoke_readiness_timeout(self):
        with Processes(self.path / 'logs', grace=12) as owner:
            proc = owner.start('smoke', self.smoke_command('--startup-timeout', '0.01'))
            self.assertEqual(proc.wait(timeout=30), 1)
        report = self.assert_smoke_clean('normal')
        self.assertIn('readiness timed out', report['error'])

    def test_smoke_signal_during_request(self):
        with Processes(self.path / 'logs', grace=12) as owner:
            proc = owner.start('smoke', self.smoke_command('--scenario', 'hang'))
            log = self.path / 'smoke/hang/backend.log'
            wait_for(lambda: log.exists() and 'fault=hang' in log.read_text(), timeout=40)
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=15), 143)
        self.assert_smoke_clean('hang')


if __name__ == '__main__':
    unittest.main()
