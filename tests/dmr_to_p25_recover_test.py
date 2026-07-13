#!/usr/bin/env python3

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "dmr_to_p25_recover.py"
SPEC = importlib.util.spec_from_file_location("dmr_to_p25_recover", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RecoveryReasonTest(unittest.TestCase):
    def test_idle_or_uncorrelated_downlink_does_not_restart(self):
        self.assertEqual(MODULE.recovery_reason(0, 120), "")

    def test_bridge_watchdog_only_restarts_bridge(self):
        self.assertEqual(MODULE.recovery_reason(120, 120), "bridge_watchdog")

    def test_short_bridge_watchdog_does_not_restart_host(self):
        self.assertEqual(MODULE.recovery_reason(119, 120), "")


if __name__ == "__main__":
    unittest.main()
