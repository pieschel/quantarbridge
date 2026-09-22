import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ConfigureDirectBrandmeisterTest(unittest.TestCase):
    def test_dvm_writer_preserves_nonempty_secrets_and_non_dvm_yaml(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("configure_test", ROOT / "scripts/configure.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = {"network": {"presharedKey": "EXAMPLE_KEY", "rpcPassword": "EXAMPLE_PASSWORD"},
                   "system": {"secure": {"key": "EXAMPLE_KEY"}}}
        with tempfile.TemporaryDirectory() as directory:
            for name in ("dvmhost-config.yml", "quantarbridge.yml"):
                path = Path(directory) / name
                module.write_yaml(path, payload)
                self.assertEqual(payload, yaml.safe_load(path.read_text()))
            empty = {"network": {"presharedKey": "", "rpcPassword": ""}}
            path = Path(directory) / "quantarbridge.yml"
            module.write_yaml(path, empty)
            self.assertEqual(empty, yaml.safe_load(path.read_text()))

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
            self.assertTrue(uplink["system"]["directImbeToAmbe"])
            self.assertEqual(1.58, uplink["system"]["directImbeGainAdjust"])
            self.assertTrue(downlink["system"]["directAmbeToImbe"])
            self.assertEqual(4.0, downlink["system"]["directAmbeSpectralScale"])
            self.assertEqual(1.0, downlink["system"]["directAmbeGainAdjust"])
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
