import unittest
from unittest.mock import patch

import app


class NumberTests(unittest.TestCase):
    def test_number_handles_missing_values(self):
        self.assertIsNone(app._number("N/A"))
        self.assertIsNone(app._number("[N/A]"))
        self.assertIsNone(app._number(""))

    def test_number_converts_valid_values(self):
        self.assertEqual(app._number(" 42 ", integer=True), 42)
        self.assertEqual(app._number("96.60"), 96.6)

    def test_percent_is_bounded(self):
        self.assertEqual(app._percent(25, 100), 25)
        self.assertEqual(app._percent(120, 100), 100)
        self.assertIsNone(app._percent(10, 0))


class CollectorTests(unittest.TestCase):
    @patch("app.subprocess.run")
    def test_nvidia_csv_is_mapped(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = (
            "0, NVIDIA Test GPU, GPU-abc, 73, 14, 4096, 16384, "
            "61, 125.5, 250, 2100, 44\n"
        )
        devices = app.read_nvidia()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["name"], "NVIDIA Test GPU")
        self.assertEqual(devices[0]["utilization"], 73)
        self.assertEqual(devices[0]["memoryPercent"], 25)

    def test_demo_snapshot_has_complete_gpu(self):
        payload = app.GPUMonitor(demo=True).snapshot()
        self.assertTrue(payload["demo"])
        self.assertEqual(len(payload["gpus"]), 2)
        self.assertEqual({gpu["id"] for gpu in payload["gpus"]}, {"demo-gpu-0", "demo-gpu-1"})
        self.assertTrue(all("utilization" in gpu for gpu in payload["gpus"]))


if __name__ == "__main__":
    unittest.main()
