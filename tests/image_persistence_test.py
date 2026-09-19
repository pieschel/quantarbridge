"""Exercise the generated native-BM profile, including real restart target lookup."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dashboard.app import DashboardConfig, SettingsManager, RuntimeState, RestartCoordinator


class ImagePersistenceTest(unittest.TestCase):
    def test_native_profile_save_survives_fresh_dashboard_process(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            subprocess.run([
                sys.executable, str(ROOT / "scripts/configure.py"),
                "--runtime-dir", str(runtime), "--install-dir", str(ROOT),
                "--bm-id", "123456", "--bm-callsign", "N0CALL",
                "--bm-master", "2622.master.brandmeister.network",
                "--rx-frequency", "430800000", "--tx-frequency", "438800000",
                "--bm-password-stdin",
            ], input="BRANDMEISTER_PASSWORD\n", text=True, check=True, capture_output=True)
            config = DashboardConfig.load(runtime / "quantar-dashboard.json")
            manager = SettingsManager(config, RuntimeState(), RestartCoordinator(config.restart_targets))
            values = manager.read()
            values.update(brandmeisterPassword="NEW_TEST_PASSWORD", dynamicTimeoutSeconds=900,
                          talkgroupMappings=[{"p25": 101, "brandmeister": 262000}])
            with patch.object(RestartCoordinator, "_restart_systemd") as restart:
                result = manager.update(values)
            self.assertTrue(result["changed"])
            self.assertNotIn("brew-audio", result["restarted"])
            self.assertGreater(restart.call_count, 0)
            # A separate interpreter has no in-memory settings to fall back on.
            code = ("import json,sys; from dashboard.app import *; "
                    "c=DashboardConfig.load(Path(sys.argv[1])); "
                    "print(json.dumps(SettingsManager(c,RuntimeState(),RestartCoordinator(c.restart_targets)).read()))")
            loaded = subprocess.run([sys.executable, "-c", code, str(runtime / "quantar-dashboard.json")],
                                    cwd=ROOT, text=True, capture_output=True, check=True)
            saved = json.loads(loaded.stdout)
            self.assertEqual(900, saved["dynamicTimeoutSeconds"])
            self.assertEqual(values["talkgroupMappings"], saved["talkgroupMappings"])
            before = {p.name: p.read_bytes() for p in runtime.iterdir() if p.is_file()}
            # Rerunning initial setup must not silently reset existing files.
            again = subprocess.run([
                sys.executable, str(ROOT / "scripts/configure.py"),
                "--runtime-dir", str(runtime), "--install-dir", str(ROOT),
                "--bm-id", "123456", "--bm-callsign", "N0CALL",
                "--bm-master", "2622.master.brandmeister.network",
                "--rx-frequency", "430800000", "--tx-frequency", "438800000",
                "--bm-password-stdin",
            ], input="OTHER_TEST_PASSWORD\n", text=True, capture_output=True)
            self.assertNotEqual(0, again.returncode)
            self.assertEqual(before, {p.name: p.read_bytes() for p in runtime.iterdir() if p.is_file()})


if __name__ == "__main__":
    unittest.main()
