"""Exercise setup output with the real DVM parser, without starting radios."""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy/scripts"))
from dashboard.app import DashboardConfig, SettingsManager, RuntimeState
from bm_static_sync import update_quantarbridge_config


class NoServiceRestarter:
    def restart(self, names):
        return list(names)

PARSER = Path(sys.argv[1]).resolve()
FILES = ("dvmhost-config.yml", "dvmfne-config.yml",
         "dvmbridge-p25-to-dmr.yml", "dvmbridge-dmr-to-p25.yml")

with tempfile.TemporaryDirectory() as directory:
    runtime = Path(directory)
    subprocess.run([
        sys.executable, str(ROOT / "scripts/configure.py"),
        "--runtime-dir", str(runtime), "--install-dir", str(ROOT),
        "--bm-id", "123456", "--bm-callsign", "N0CALL",
        "--bm-master", "2622.master.brandmeister.network",
        "--rx-frequency", "430800000", "--tx-frequency", "438800000",
        "--bm-password-stdin",
    ], input="BRANDMEISTER_PASSWORD\n", text=True, check=True, capture_output=True)
    subprocess.run([str(PARSER), *(str(runtime / f) for f in FILES)], check=True)

    # Exercise the operator's real sequence: sync to zero subscriptions,
    # read/save dashboard settings, then read/parse them again after restart.
    update_quantarbridge_config(runtime / "quantarbridge.yml", [])
    config = DashboardConfig.load(runtime / "quantar-dashboard.json")
    manager = SettingsManager(config, RuntimeState(), NoServiceRestarter())
    settings = manager.read()
    assert settings["staticTalkgroups"] == []
    settings["gps"]["updateIntervalSeconds"] = 600
    settings["dynamicTimeoutSeconds"] = 900
    assert manager.update(settings)["changed"]
    subprocess.run([str(PARSER), *(str(runtime / f) for f in FILES)], check=True)
    reread = SettingsManager(config, RuntimeState(), NoServiceRestarter()).read()
    assert reread["gps"]["updateIntervalSeconds"] == 600
    assert reread["dynamicTimeoutSeconds"] == 900
    assert reread["staticTalkgroups"] == []

    # Prove the test detects both original, standards-valid YAML failures.
    for name, text in (
        ("empty-quote", "network:\n  presharedKey: ''\n"),
        ("indentless-list", "system:\n  voiceChNo:\n  - channelId: 2\n"),
    ):
        bad = runtime / (name + ".yml")
        bad.write_text(text)
        result = subprocess.run([str(PARSER), str(bad)], capture_output=True)
        if result.returncode == 0:
            raise AssertionError("Regression fixture unexpectedly accepted: " + name)
print("Generated setup configuration accepted by DVM parser; original failures detected")
