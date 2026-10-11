import unittest
from unittest.mock import Mock, patch

from asitop import asitop as app, utils


class MemoryViewTests(unittest.TestCase):
    def test_memory_gauges_are_stacked_without_a_history_chart(self):
        panel, ram, gpu = app._build_memory_widgets(2)
        self.assertEqual(list(panel.items), [ram, gpu])
        self.assertIn('values overlap', panel.title)
        self.assertEqual([ram.color, gpu.color], [2, 2])
        app._update_gpu_memory(gpu, 3 * 2**30, 8 * 2**30)
        self.assertEqual(gpu.value, 37.5)
        self.assertIn('3.0/  8.0 GiB', gpu.title)
        app._update_gpu_memory(gpu, None, 8 * 2**30)
        self.assertIn('N/A', gpu.title)
        self.assertEqual(gpu.value, 0)
        app._update_gpu_memory(gpu, 0, 8 * 2**30)
        self.assertEqual(gpu.value, 0)

    def test_allocation_over_capacity_keeps_reading_and_caps_bar(self):
        _, _, gpu = app._build_memory_widgets(0)
        app._update_gpu_memory(gpu, 10 * 2**30, 8 * 2**30)
        self.assertEqual(gpu.value, 100)
        self.assertEqual(gpu.title, 'GPU allocated:  10.0/  8.0 GiB')
        self.assertEqual(gpu.color, 0)

    def test_missing_capacity_does_not_divide_by_zero(self):
        _, _, gpu = app._build_memory_widgets(2)
        app._update_gpu_memory(gpu, 3 * 2**30, 0)
        self.assertEqual(gpu.title, 'GPU allocated: N/A')
        self.assertEqual(gpu.value, 0)

    def test_gpu_capacity_uses_exact_ram_bytes(self):
        total = 8 * 2**30 + 12345
        with patch.object(utils.psutil, 'virtual_memory',
                          return_value=Mock(total=total, available=2**30)), \
             patch.object(utils.psutil, 'swap_memory', return_value=Mock(total=0, used=0)):
            metrics = utils.get_ram_metrics_dict()
        self.assertEqual(metrics['total_bytes'], total)

    def test_ram_and_gpu_numbers_align_with_different_digit_counts(self):
        _, ram, gpu = app._build_memory_widgets(2)
        app._update_ram_memory(ram, {
            'used_GB': 34.7, 'total_GB': 36.0, 'free_percent': 96,
            'swap_total_GB': 8.0, 'swap_used_GB': 7.0})
        app._update_gpu_memory(gpu, 3 * 2**30, 36 * 2**30)
        self.assertEqual(ram.title, 'RAM Usage:      34.7/ 36.0 GiB - swap:7.0/8.0 GiB')
        self.assertEqual(gpu.title, 'GPU allocated:   3.0/ 36.0 GiB')
        self.assertEqual(ram.title.index('/'), gpu.title.index('/'))
        self.assertEqual(ram.title.index('GiB'), gpu.title.index('GiB'))
        self.assertEqual(ram.value, 96)
