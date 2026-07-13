#!/usr/bin/env python3

import importlib.util
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "quantar_static_recover.py"
SPEC = importlib.util.spec_from_file_location("quantar_static_recover", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StaticRecoveryTest(unittest.TestCase):
    def test_recovery_preserves_p25_host_sessions(self):
        with mock.patch.object(MODULE.subprocess, "run") as run:
            self.assertEqual(MODULE.main(), 0)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(
            ["systemctl", "restart", "dvmbridge-dmr-to-p25.service"],
            commands,
        )
        self.assertFalse(any("dvmhost.service" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
