import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ConfigureDirectBrandmeisterTest(unittest.TestCase):
    def test_default_runtime_uses_direct_voice_and_native_services(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            command = [
                sys.executable,
                str(ROOT / "scripts" / "configure.py"),
                "--runtime-dir",
                str(runtime),
                "--install-dir",
                str(ROOT),
                "--bm-id",
                "123456",
                "--bm-callsign",
                "N0CALL",
                "--bm-master",
                "2622.master.brandmeister.network",
                "--rx-frequency",
                "430800000",
                "--tx-frequency",
                "438800000",
                "--bm-password-stdin",
            ]
            result = subprocess.run(
                command,
                input="BRANDMEISTER_PASSWORD\n",
                text=True,
                capture_output=True,
                check=True,
            )

            bridge = yaml.safe_load((runtime / "quantarbridge.yml").read_text())
            uplink = yaml.safe_load(
                (runtime / "dvmbridge-p25-to-dmr.yml").read_text()
            )
            downlink = yaml.safe_load(
                (runtime / "dvmbridge-dmr-to-p25.yml").read_text()
            )
            sms = json.loads((runtime / "tetrapack-brew-bridge.json").read_text())
            dashboard = json.loads((runtime / "quantar-dashboard.json").read_text())

            self.assertTrue(bridge["brandmeister"]["voiceEnabled"])
            self.assertEqual("BRIDGE-P25-DMR", uplink["system"]["identity"])
            self.assertEqual(1.5, uplink["system"]["vocoderEncoderAudioGain"])
            self.assertEqual("BRIDGE-DMR-P25", downlink["system"]["identity"])
            self.assertFalse(downlink["system"]["udpAudio"])
            self.assertEqual(1.0, downlink["system"]["vocoderEncoderAudioGain"])
            self.assertFalse(sms["brew"]["enabled"])
            self.assertEqual([], sms["brewServiceRids"])
            self.assertEqual([262993], sms["brandmeisterServiceRids"])
            self.assertNotIn("brewAudioOutboxDir", sms)
            self.assertNotIn(
                "brew-audio", {item["id"] for item in dashboard["serviceUnits"]}
            )
            self.assertIn("voice_transport=brandmeister", result.stdout)
            self.assertNotIn("BRANDMEISTER_PASSWORD", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
