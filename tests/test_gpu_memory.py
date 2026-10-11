import plistlib
import subprocess
import unittest
from unittest.mock import patch

from asitop import utils


class GPUMemoryTests(unittest.TestCase):
    def test_reads_accelerator_allocation_as_bytes(self):
        payload = plistlib.dumps([{'PerformanceStatistics': {
            'Alloc system memory': 3 * 2**30, 'In use system memory': 2**30}}])
        with patch.object(utils.subprocess, 'check_output', return_value=payload) as read:
            self.assertEqual(utils.get_gpu_memory_bytes(), 3 * 2**30)
        read.assert_called_once_with(
            ['/usr/sbin/ioreg', '-a', '-r', '-c', 'IOAccelerator', '-d', '1'],
            timeout=1, stderr=subprocess.DEVNULL)

    def test_missing_invalid_and_failed_readings_are_unavailable(self):
        payloads = [b'bad plist', b'<?xml version="1.0"?><plist><array>',
                    plistlib.dumps([]), plistlib.dumps({}),
                    plistlib.dumps([1, {'PerformanceStatistics': []}])]
        payloads += [plistlib.dumps([{'PerformanceStatistics': {
            'Alloc system memory': value}}]) for value in [-1, True, '123']]
        for payload in payloads:
            with self.subTest(payload=payload), patch.object(
                    utils.subprocess, 'check_output', return_value=payload):
                self.assertIsNone(utils.get_gpu_memory_bytes())
        for error in [OSError(), subprocess.TimeoutExpired('ioreg', 1),
                      subprocess.CalledProcessError(1, 'ioreg')]:
            with patch.object(utils.subprocess, 'check_output', side_effect=error):
                self.assertIsNone(utils.get_gpu_memory_bytes())

    def test_zero_is_valid(self):
        with patch.object(utils.subprocess, 'check_output', return_value=plistlib.dumps(
                [{'PerformanceStatistics': {'Alloc system memory': 0}}])):
            self.assertEqual(utils.get_gpu_memory_bytes(), 0)
