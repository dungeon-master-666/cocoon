import copy
import tempfile
import struct
from pathlib import Path
import unittest

from pilot import HERE, Pilot, backend_argv, compare, environment, read, write
from evidence import capture_summary, network_isolation


class PilotTest(unittest.TestCase):
    def setUp(self):
        self.profiles, self.lab = read(HERE/'profiles.json'), read(HERE/'lab.json')

    def test_transport_has_only_socket_on_overlay(self):
        for host in self.lab['hosts']:
            env = environment(host)
            self.assertEqual(env['NCCL_NET'], 'Socket')
            self.assertEqual(env['NCCL_SOCKET_IFNAME'], '=wg0')
            self.assertEqual(env['GLOO_SOCKET_IFNAME'], 'wg0')
            for name in ('NCCL_IB_DISABLE', 'NCCL_SHM_DISABLE', 'NCCL_P2P_DISABLE'):
                self.assertEqual(env[name], '1')

    def test_vllm_member_is_headless_and_has_correct_rank(self):
        argv = backend_argv(self.profiles, self.lab, 'vllm', 'large', 2, 1)
        for flag, value in [('--pipeline-parallel-size', '2'), ('--tensor-parallel-size', '1'),
                            ('--node-rank', '1'), ('--nnodes', '2'), ('--cpu-offload-gb', '0')]:
            self.assertEqual(argv[argv.index(flag)+1], value)
        self.assertIn('--headless', argv)
        self.assertNotIn('--host', argv)

    def test_sglang_rendezvous_uses_overlay(self):
        for rank in range(2):
            argv = backend_argv(self.profiles, self.lab, 'sglang', 'small', 2, rank)
            self.assertEqual(argv[argv.index('--dist-init-addr')+1], '10.231.0.1:29500')
            self.assertEqual(argv[argv.index('--node-rank')+1], str(rank))
            self.assertEqual(argv[argv.index('--host')+1], '127.0.0.1')

    def test_images_and_models_are_immutable(self):
        for spec in self.profiles['backends'].values():
            self.assertRegex(spec['image'], r'@sha256:[0-9a-f]{64}$')
        for spec in self.profiles['models'].values():
            self.assertRegex(spec['revision'], r'^[0-9a-f]{40}$')

    def test_cannot_accept_production(self):
        self.profiles['mode'] = 'production'
        with self.assertRaises(ValueError):
            Pilot(self.lab, self.profiles)

    def test_invalid_topology(self):
        with self.assertRaises(AssertionError):
            backend_argv(self.profiles, self.lab, 'vllm', 'small', 1, 1)

    def test_capture_rejects_plain_tcp(self):
        def packet(protocol):
            ethernet = bytes(12) + b'\x08\x00'
            ipv4 = b'\x45' + bytes(8) + bytes([protocol]) + bytes(10)
            return ethernet + ipv4 + struct.pack('!HH', 51888, 51888) + bytes(8)
        def pcap(frames):
            return (struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 80, 1) +
                    b''.join(struct.pack('<IIII', 0, 0, len(f), len(f))+f for f in frames))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'capture.pcap'
            path.write_bytes(pcap([packet(17)]))
            self.assertTrue(capture_summary(path, 51888)['passed'])
            path.write_bytes(pcap([packet(17), packet(6)]))
            self.assertFalse(capture_summary(path, 51888)['passed'])

    def test_isolation_rejects_extra_interface(self):
        with self.assertRaises(AssertionError):
            network_isolation({'interfaces': '[{"ifname":"lo"},{"ifname":"wg0"},{"ifname":"eth0"}]'})

    def test_comparison_rejects_wrong_cut_and_logprob_drift(self):
        base = {'fixtures': [{'prompt': 'q', 'response': {'usage': {'prompt_tokens': 1, 'completion_tokens': 1},
            'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop',
                         'logprobs': {'content': [{'token': 'OK', 'logprob': -0.1}]}}]}}]}
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp)/'pp1', Path(tmp)/'pp2'
            a.mkdir(); b.mkdir()
            for pp, path in [(1, a), (2, b)]:
                write(path/'result.json', {'backend': 'vllm', 'model': 'small', 'pp': pp})
                write(path/'profiles.json', self.profiles)
            write(a/'api.json', base)
            write(b/'api.json', base)
            self.assertTrue(compare(a, b)['passed'])
            wrong = copy.deepcopy(base)
            wrong['fixtures'][0]['response']['choices'][0]['message']['content'] = 'wrong'
            write(b/'api.json', wrong)
            with self.assertRaises(AssertionError):
                compare(a, b)
            wrong = copy.deepcopy(base)
            wrong['fixtures'][0]['response']['choices'][0]['logprobs']['content'][0]['logprob'] = -1
            write(b/'api.json', wrong)
            with self.assertRaises(AssertionError):
                compare(a, b)


if __name__ == '__main__':
    unittest.main()
