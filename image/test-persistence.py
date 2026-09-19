#!/usr/bin/env python3
"""Build-time config round trip. Run prepare, unmount/remount, then verify."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

INSTALL = Path("/home/quantar/quantarbridge")
RUNTIME = Path("/home/quantar/quantar-runtime")
MANIFEST = Path("/var/lib/quantarbridge-image-persistence-test.json")
sys.path.insert(0, str(INSTALL))
from dashboard.app import DashboardConfig, SettingsManager, RuntimeState, RestartCoordinator


def hashes():
    return {str(p.relative_to(RUNTIME)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(RUNTIME.rglob("*")) if p.is_file()}


if sys.argv[1] == "prepare":
    if any(RUNTIME.iterdir()) or MANIFEST.exists():
        raise RuntimeError("Persistence test requires an empty, unconfigured image")
    subprocess.run(["python3", str(INSTALL / "image/check-persistence.py"), str(RUNTIME), "/etc"], check=True)
    subprocess.run(["runuser", "-u", "quantar", "--", "python3", str(INSTALL / "scripts/configure.py"),
                    "--runtime-dir", str(RUNTIME), "--install-dir", str(INSTALL),
                    "--bm-id", "123456", "--bm-callsign", "N0CALL",
                    "--bm-master", "example.invalid", "--rx-frequency", "430800000",
                    "--tx-frequency", "438800000", "--bm-password-stdin"],
                   input="BRANDMEISTER_PASSWORD\n", text=True, check=True)
    subprocess.run(["runuser", "-u", "quantar", "--", "python3", str(INSTALL / "dashboard/app.py"),
                    "--config", str(RUNTIME / "quantar-dashboard.json"), "--init-auth",
                    "--username", "admin", "--password-stdin"],
                   input="IMAGE_TEST_PASSWORD_ONLY\n", text=True, check=True)
    (RUNTIME / ".configured").touch(mode=0o600)
    MANIFEST.write_text(json.dumps(hashes()))
    subprocess.run(["sync"], check=True)
elif sys.argv[1] == "verify":
    if hashes() != json.loads(MANIFEST.read_text()):
        raise RuntimeError("Configuration/authentication files changed across image remount")
    config = DashboardConfig.load(RUNTIME / "quantar-dashboard.json")
    settings = SettingsManager(config, RuntimeState(), RestartCoordinator(config.restart_targets)).read()
    assert settings["repeaterId"] == 123456
    assert settings["brandmeisterCallsign"] == "N0CALL"
    assert settings["brandmeisterPasswordConfigured"]
    assert not config.brew_audio_config.exists()
    # Fixtures belong only to this build-time test, never to a published image.
    for path in RUNTIME.iterdir():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    MANIFEST.unlink()
    assert not any(RUNTIME.iterdir())
    print("runtime_and_auth_hashes_after_unmount_remount=passed")
    print("fresh_dashboard_settings_read=passed")
    print("test_configuration_removed=passed")
    print("physical_raspberry_pi_reboot=not_tested")
else:
    raise SystemExit("Use prepare or verify")
