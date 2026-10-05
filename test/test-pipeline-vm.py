#!/usr/bin/env python3
"""Offline contracts for the dev VFIO VM launcher."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('vm',ROOT/'pipeline/deployment/vm-host.py')
vm=importlib.util.module_from_spec(spec);spec.loader.exec_module(vm)


class VM(unittest.TestCase):
    def setUp(self):
        self.config={'schema':1,'mode':'dev','name':'cocoon-pipeline-vm-head','hostname':'host-1',
            'gpu':'0000:01:00.0','interface':'eth0','guest_lan':'192.168.100.22/24','mtu':1400,
            'vcpus':8,'memory_mib':32768,'disk_gib':200,'base_image':'/images/ubuntu.img',
            'base_sha256':'a'*64,'model_root':'/models','artifact_root':'/artifacts',
            'ssh_public_key':'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'}

    def test_rejects_unsafe_config(self):
        for field,value in [('schema',True),('mode','production'),('gpu','0000:01:00.1'),('name','other-vm'),
                            ('model_root','/'),('guest_lan','127.0.0.1/8'),('vcpus',True),
                            ('ssh_public_key','-----BEGIN OPENSSH PRIVATE KEY-----')]:
            with self.subTest(field=field),self.assertRaises(ValueError):vm.validate({**self.config,field:value})

    def test_qemu_owns_entire_gpu_and_readonly_shares(self):
        xml=ET.fromstring(vm.domain_xml(vm.validate(self.config),Path('/vm')))
        self.assertEqual(xml.get('type'),'kvm')
        devices=xml.findall('devices/hostdev')
        self.assertEqual(len(devices),2)
        self.assertEqual([e.find('source/address').get('function') for e in devices],['0x0','0x1'])
        self.assertTrue(all(e.get('managed')=='yes' for e in devices))
        self.assertTrue(all(e.find('readonly') is not None for e in xml.findall('devices/filesystem')))
        self.assertIsNone(xml.find('launchSecurity'))
        self.assertEqual(xml.find('devices/interface[@type="direct"]/source').get('dev'),'eth0')

    def test_cloud_init_has_separate_lan_and_management(self):
        user,network=vm.cloud_config(self.config)
        self.assertFalse(user['ssh_pwauth']);self.assertTrue(user['disable_root'])
        self.assertTrue(network['ethernets']['mgmt0']['dhcp4'])
        self.assertFalse(network['ethernets']['lan0']['dhcp4'])
        self.assertNotIn('gateway4',network['ethernets']['lan0'])
        self.assertEqual(network['ethernets']['lan0']['addresses'],['192.168.100.22/24'])
        self.assertNotEqual(vm.mac(self.config,0),vm.mac(self.config,1))

    def test_existing_foreign_domain_is_not_touched(self):
        foreign='<domain><name>cocoon-pipeline-vm-head</name><uuid>other</uuid></domain>'
        with mock.patch.object(vm,'virsh',side_effect=[mock.Mock(stdout=self.config['name']+'\n'),mock.Mock(stdout=foreign)]):
            with self.assertRaisesRegex(RuntimeError,'another configuration'):vm.domain(self.config)

    def test_libvirt_query_failure_is_not_absence(self):
        with mock.patch.object(vm,'virsh',side_effect=RuntimeError('connection refused')):
            with self.assertRaisesRegex(RuntimeError,'connection refused'):vm.domain(self.config,missing=True)

    def test_monitor_processes_are_detected_without_compute_work(self):
        device=mock.Mock();device.is_char_device.return_value=True
        with mock.patch.object(Path,'glob',return_value=[device]),mock.patch.object(vm,'run',return_value=mock.Mock(returncode=0,stdout=' 17 17 29',stderr='')):
            self.assertEqual(vm.gpu_users(),[17,29])

    def test_device_user_query_failure_is_not_an_idle_gpu(self):
        device=mock.Mock();device.is_char_device.return_value=True
        with mock.patch.object(Path,'glob',return_value=[device]),mock.patch.object(vm,'run',return_value=mock.Mock(returncode=2,stdout='',stderr='denied')):
            with self.assertRaisesRegex(RuntimeError,'cannot check'):vm.gpu_users()

    def test_paused_vm_is_not_an_idempotent_successful_start(self):
        with mock.patch.object(vm,'domain'),mock.patch.object(vm,'stopped',return_value=False),mock.patch.object(vm,'virsh',return_value=mock.Mock(stdout='paused\n')):
            with self.assertRaisesRegex(RuntimeError,'not running'):vm.start(self.config,Path('/unused'))

    def test_failed_driver_restore_does_not_release_ownership(self):
        with tempfile.TemporaryDirectory() as temp:
            d=Path(temp);state={'drivers':{'0000:01:00.0':'nvidia'},'persistenced':True,'released':False}
            vm.save(d/'state.json',state)
            with mock.patch.object(vm,'pci_state',return_value={'0000:01:00.0':'vfio-pci'}),mock.patch.object(vm,'run') as run:
                with self.assertRaisesRegex(RuntimeError,'not restored'):vm.restore_services(self.config,d)
                run.assert_not_called()
            self.assertFalse(json.loads((d/'state.json').read_text())['released'])

    def test_restores_only_previously_running_persistence_service(self):
        for active in (True,False):
            with self.subTest(active=active),tempfile.TemporaryDirectory() as temp:
                d=Path(temp);drivers={'0000:01:00.0':'nvidia','0000:01:00.1':'snd_hda_intel'}
                vm.save(d/'state.json',{'drivers':drivers,'persistenced':active,'released':False})
                with mock.patch.object(vm,'pci_state',return_value=drivers),mock.patch.object(vm,'run') as run:
                    vm.restore_services(self.config,d)
                    self.assertEqual(run.call_count,int(active))
                self.assertTrue(json.loads((d/'state.json').read_text())['released'])


if __name__=='__main__':unittest.main()
