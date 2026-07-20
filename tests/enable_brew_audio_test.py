import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "enable_brew_audio.py"
SPEC = importlib.util.spec_from_file_location("enable_brew_audio", SCRIPT)
MIGRATION = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MIGRATION
SPEC.loader.exec_module(MIGRATION)


class EnableBrewAudioTest(unittest.TestCase):
    def test_migration_preserves_credentials_and_sets_tested_audio_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            install = root / "install"
            legacy = install / "deploy" / "runtime"
            examples = install / "deploy" / "examples"
            runtime.mkdir()
            legacy.mkdir(parents=True)
            examples.mkdir(parents=True)
            shutil.copy2(
                ROOT / "deploy" / "examples" / "tetrapack-brew-audio.json",
                examples / "tetrapack-brew-audio.json",
            )
            (runtime / "quantarbridge.yml").write_text(
                "brandmeister:\n  password: private-value\nrouting:\n  staticTalkgroups: [983872]\n",
                encoding="utf-8",
            )
            for name in ("dvmbridge-p25-to-dmr.yml", "dvmbridge-dmr-to-p25.yml"):
                (runtime / name).write_text(
                    "network:\n  password: local-secret\nsystem:\n  identity: OLD\n",
                    encoding="utf-8",
                )
            (legacy / "tetrapack-brew-bridge.json").write_text(
                json.dumps(
                    {
                        "brew": {
                            "enabled": True,
                            "username": "bridge-user",
                            "password": "",
                        }
                    }
                ),
                encoding="utf-8",
            )
            (legacy / "quantar-dashboard.json").write_text(
                json.dumps(
                    {
                        "serviceUnits": [
                            {
                                "id": "sms-bridge",
                                "processMatch": "legacy-command",
                            }
                        ],
                        "restartTargets": {
                            "sms-bridge": {
                                "type": "process",
                                "match": "legacy-command",
                                "command": ["legacy-command"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            codec = root / "libtetra-codec.so"
            codec.write_bytes(b"test")

            MIGRATION.migrate(
                argparse.Namespace(
                    runtime_dir=runtime,
                    install_dir=install,
                    codec_library=codec,
                )
            )

            bridge = yaml.safe_load((runtime / "quantarbridge.yml").read_text())
            self.assertFalse(bridge["brandmeister"]["voiceEnabled"])
            self.assertEqual("private-value", bridge["brandmeister"]["password"])
            uplink = yaml.safe_load((runtime / "dvmbridge-p25-to-dmr.yml").read_text())
            downlink = yaml.safe_load((runtime / "dvmbridge-dmr-to-p25.yml").read_text())
            self.assertEqual("local-secret", uplink["network"]["password"])
            self.assertEqual("BRIDGE-P25-PCM-ONLY", uplink["system"]["identity"])
            self.assertEqual(31120, uplink["system"]["udpSendPort"])
            self.assertEqual(1.0, uplink["system"]["vocoderDecoderAudioGain"])
            self.assertEqual(12, uplink["system"]["vocoderDecoderUvQuality"])
            self.assertEqual("BRIDGE-PCM-P25-ONLY", downlink["system"]["identity"])
            self.assertEqual(1.1, downlink["system"]["txAudioGain"])
            self.assertEqual(31121, downlink["system"]["udpReceivePort"])
            audio = json.loads((runtime / "tetrapack-brew-audio.json").read_text())
            self.assertEqual(2.0, audio["uplinkGain"])
            self.assertEqual(80, audio["uplinkHighPassHz"])
            self.assertEqual(0.12, audio["uplinkPresenceGain"])
            self.assertEqual(3200, audio["uplinkHighCutHz"])
            self.assertEqual(0.0, audio["uplinkDeEsserStrength"])
            self.assertEqual(str(codec), audio["codecLibrary"])
            self.assertEqual(
                str(runtime / "sms" / "brew-audio-outbox"),
                audio["smsCommandDir"],
            )
            brew = json.loads((runtime / "tetrapack-brew-bridge.json").read_text())
            self.assertEqual(
                str(runtime / "sms" / "brew-audio-outbox"),
                brew["brewAudioOutboxDir"],
            )
            dashboard = json.loads((legacy / "quantar-dashboard.json").read_text())
            self.assertIn("brew-audio", dashboard["restartTargets"])
            sms_config = runtime / "tetrapack-brew-bridge.json"
            sms_service = next(
                service for service in dashboard["serviceUnits"] if service["id"] == "sms-bridge"
            )
            self.assertIn(str(sms_config), sms_service["processMatch"])
            self.assertEqual(
                str(sms_config),
                dashboard["restartTargets"]["sms-bridge"]["command"][-1],
            )
            self.assertTrue(list(runtime.glob("quantarbridge.yml.pre-brew-audio-*")))


if __name__ == "__main__":
    unittest.main()
