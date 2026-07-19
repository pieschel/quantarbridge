#!/usr/bin/env python3

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_config import read_brandmeister_voice_enabled


def brew_audio_owns_voice(config_path):
    if not config_path.exists():
        return False
    return not read_brandmeister_voice_enabled(config_path)


def run(command):
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def count_matches(text, needle):
    return sum(1 for line in text.splitlines() if needle in line)


def service_age_seconds(service):
    pid = run(["systemctl", "show", service, "--property=MainPID", "--value"]).strip()
    if not pid or pid == "0":
        return 0
    age_text = run(["ps", "-o", "etimes=", "-p", pid]).strip()
    return int(age_text or "0")


def throttle_active(stamp_path, min_interval_seconds):
    if min_interval_seconds <= 0:
        return False
    try:
        age = time.time() - stamp_path.stat().st_mtime
    except FileNotFoundError:
        return False
    return age < min_interval_seconds


def touch_stamp(stamp_path):
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.touch()


def restart_downlink_bridge(service="dvmbridge-dmr-to-p25.service"):
    subprocess.run(["systemctl", "restart", service], check=True)
    print(f"restarted={service}")


def main():
    parser = argparse.ArgumentParser(description="Recover BM->P25 path when BM traffic reaches quantarbridge but not dvmhost.")
    parser.add_argument("--window", default="10 minutes ago")
    parser.add_argument("--min-qb-frames", type=int, default=2)
    parser.add_argument("--min-bridge-p25", type=int, default=4)
    parser.add_argument("--max-host-p25-net", type=int, default=0)
    parser.add_argument("--cooldown-seconds", type=int, default=300)
    parser.add_argument("--stamp", type=Path, default=Path("/home/quantar/quantar-runtime/bm_to_p25_recover.stamp"))
    parser.add_argument("--min-interval", type=float, default=600.0)
    parser.add_argument("--config", type=Path, default=Path("/home/quantar/quantar-runtime/quantarbridge.yml"))
    args = parser.parse_args()

    if brew_audio_owns_voice(args.config):
        print("skip=tetrapack-brew-audio-owns-voice")
        return 0

    if throttle_active(args.stamp, args.min_interval):
        print("skip=throttled")
        return 0
    touch_stamp(args.stamp)

    bridge_age = service_age_seconds("dvmbridge-dmr-to-p25.service")
    host_age = service_age_seconds("dvmhost.service")

    qb_logs = run([
        "journalctl",
        "-u",
        "quantarbridge.service",
        "--since",
        args.window,
        "--no-pager",
        "-o",
        "cat",
    ])
    bridge_logs = run([
        "journalctl",
        "-u",
        "dvmbridge-dmr-to-p25.service",
        "--since",
        args.window,
        "--no-pager",
        "-o",
        "cat",
    ])
    host_logs = run([
        "journalctl",
        "-u",
        "dvmhost.service",
        "--since",
        args.window,
        "--no-pager",
        "-o",
        "cat",
    ])

    qb_frames = count_matches(qb_logs, "Forwarding BrandMeister DMR to FNE")
    bridge_p25 = count_matches(bridge_logs, "P25, LDU1") + count_matches(bridge_logs, "P25, LDU2")
    host_p25 = (
        count_matches(host_logs, "P25 Net network voice transmission")
        + count_matches(host_logs, "P25, LDU1")
        + count_matches(host_logs, "P25, LDU2")
    )

    print(
        f"qb_frames={qb_frames} "
        f"bridge_p25={bridge_p25} "
        f"host_p25={host_p25} "
        f"bridge_age_seconds={bridge_age} "
        f"host_age_seconds={host_age}"
    )

    if min(bridge_age, host_age) < args.cooldown_seconds:
        return 0

    if qb_frames < args.min_qb_frames:
        return 0

    if bridge_p25 < args.min_bridge_p25:
        return 0

    if host_p25 > args.max_host_p25_net:
        return 0

    # This detector observes the stateless BM downlink bridge. The dedicated
    # dvmhost watchdog owns host recovery and requires stronger evidence, so a
    # missing log correlation here must not destroy ARS/TMS/LRRP sessions.
    restart_downlink_bridge()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"command failed: {exc}", file=sys.stderr)
        raise SystemExit(exc.returncode or 1)
