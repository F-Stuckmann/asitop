import unittest
from unittest.mock import Mock, patch

from asitop.asitop import (
    _build_processor_widgets,
    _processor_title,
    _update_processor_widgets,
    main,
)
from asitop.parsers import parse_cpu_metrics, parse_gpu_metrics


def make_reading(timestamp):
    cpu_metrics = parse_cpu_metrics({
        "processor": {
            "clusters": [
                {
                    "name": "P0-Cluster",
                    "freq_hz": 2000e6,
                    "idle_ratio": 0.50,
                    "cpus": [{
                        "cpu": 0,
                        "freq_hz": 2000e6,
                        "idle_ratio": 0.75,
                    }],
                },
                {
                    "name": "S-Cluster",
                    "freq_hz": 4000e6,
                    "idle_ratio": 0.25,
                    "cpus": [{
                        "cpu": 1,
                        "freq_hz": 4000e6,
                        "idle_ratio": 0.50,
                    }],
                },
            ],
            "ane_energy": 0,
            "cpu_energy": 2000,
            "gpu_energy": 1000,
            "combined_power": 3000,
        }
    })
    gpu_metrics = parse_gpu_metrics({
        "gpu": {
            "freq_hz": 500,
            "idle_ratio": 0.75,
        }
    })
    return cpu_metrics, gpu_metrics, "Nominal", None, timestamp


class ProcessorWidgetTests(unittest.TestCase):
    def setUp(self):
        self.groups = make_reading(1)[0]["cluster_groups"]

    def test_widgets_follow_observed_cluster_families(self):
        _, group_gauges, core_gauges, _, _ = _build_processor_widgets(
            self.groups,
            color=2,
            show_cores=True,
        )

        self.assertEqual(["P", "S"], list(group_gauges))
        self.assertEqual([0], list(core_gauges["P"]))
        self.assertEqual([1], list(core_gauges["S"]))

    def test_widget_updates_use_matching_family_and_core(self):
        _, group_gauges, core_gauges, _, _ = _build_processor_widgets(
            self.groups,
            color=2,
            show_cores=True,
        )

        _update_processor_widgets(
            self.groups,
            group_gauges,
            core_gauges,
            show_cores=True,
        )

        self.assertEqual("P-CPU Usage: 25% @ 2000 MHz", group_gauges["P"].title)
        self.assertEqual(25, group_gauges["P"].value)
        self.assertEqual("S-CPU Usage: 50% @ 4000 MHz", group_gauges["S"].title)
        self.assertEqual("Core 2: 50%", core_gauges["S"][1].title)

    def test_processor_title_uses_observed_core_counts(self):
        self.assertEqual(
            "Apple M5 Pro (Cores: 1P+1S+20GPU)",
            _processor_title("Apple M5 Pro", self.groups, 20),
        )



if __name__ == "__main__":
    unittest.main()
