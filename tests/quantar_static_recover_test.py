#!/usr/bin/env python3

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "quantar_static_recover.py"
SPEC = importlib.util.spec_from_file_location("quantar_static_recover", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StaticRecoveryTest(unittest.TestCase):
    def test_recovery_preserves_p25_host_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "quantarbridge.yml"
            config.write_text("brandmeister:\n  voiceEnabled: true\n", encoding="utf-8")
            with mock.patch.object(MODULE, "CONFIG", config), mock.patch.object(
                MODULE.subprocess, "run"
            ) as run:
                self.assertEqual(MODULE.main(), 0)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(
            ["systemctl", "restart", "dvmbridge-dmr-to-p25.service"],
            commands,
        )
        self.assertFalse(any("dvmhost.service" in command for command in commands))

    def test_brew_audio_mode_skips_legacy_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "quantarbridge.yml"
            config.write_text("brandmeister:\n  voiceEnabled: false\n", encoding="utf-8")
            with mock.patch.object(MODULE, "CONFIG", config), mock.patch.object(
                MODULE.subprocess, "run"
            ) as run:
                self.assertEqual(MODULE.main(), 0)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
