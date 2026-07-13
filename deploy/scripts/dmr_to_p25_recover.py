#!/usr/bin/env python3

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path


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


def service_main_pid(service):
    pid = run(["systemctl", "show", service, "--property=MainPID", "--value"]).strip()
    if not pid or pid == "0":
        return ""
    return pid


def service_age_seconds(service):
    pid = service_main_pid(service)
    if not pid:
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


def restart_bridge(args):
    subprocess.run(["systemctl", "restart", args.bridge_service], check=True)
    print(f"restarted={args.bridge_service} reason=bridge_watchdog")


def recovery_reason(worst_watchdog_seconds, max_watchdog_seconds):
    if worst_watchdog_seconds >= max_watchdog_seconds:
        return "bridge_watchdog"
    return ""


def main():
    parser = argparse.ArgumentParser(description="Recover dmr-to-p25 bridge when a BM downlink call wedges.")
    parser.add_argument("--window", default="15 minutes ago")
    parser.add_argument("--bridge-service", default="dvmbridge-dmr-to-p25.service")
    parser.add_argument("--host-service", default="dvmhost.service")
    parser.add_argument("--max-watchdog-seconds", type=int, default=120)
    parser.add_argument("--cooldown-seconds", type=int, default=300)
    parser.add_argument("--idle-window", default="45 seconds ago")
    parser.add_argument("--stamp", type=Path, default=Path("/home/quantar/quantar-runtime/dmr_to_p25_recover.stamp"))
    parser.add_argument("--min-interval", type=float, default=180.0)
    args = parser.parse_args()

    bridge_age = service_age_seconds(args.bridge_service)
    host_age = service_age_seconds(args.host_service)
    bridge_pid = service_main_pid(args.bridge_service)
    host_pid = service_main_pid(args.host_service)

    bridge_logs = run([
        "journalctl",
        f"_PID={bridge_pid}",
        "--since",
        args.window,
        "--no-pager",
        "-o",
        "cat",
    ])
    recent_logs = run([
        "journalctl",
        f"_PID={bridge_pid}",
        f"_PID={host_pid}",
        "--since",
        args.idle_window,
        "--no-pager",
        "-o",
        "cat",
    ])
    host_logs = run([
        "journalctl",
        f"_PID={host_pid}",
        "--since",
        args.window,
        "--no-pager",
        "-o",
        "cat",
    ])

    match_seconds = []
    for line in bridge_logs.splitlines():
        match = re.search(r"Network watchdog, call end, dur = (\d+)s", line)
        if match:
            match_seconds.append(int(match.group(1)))

    worst = max(match_seconds) if match_seconds else 0
    bridge_has_downlink = "DMR, VOICE" in bridge_logs
    host_has_network_call = (
        "P25 Net network voice transmission" in host_logs or
        "P25, LDU1" in host_logs or
        "P25, LDU2" in host_logs
    )
    host_watchdog_count = count_matches(host_logs, "P25 Net network watchdog has expired")
    recent_voice = (
        count_matches(recent_logs, "DMR, VOICE") +
        count_matches(recent_logs, "P25, LDU1") +
        count_matches(recent_logs, "P25, LDU2")
    )

    print(
        f"worst_watchdog_seconds={worst} "
        f"bridge_age_seconds={bridge_age} "
        f"host_age_seconds={host_age} "
        f"bridge_pid={bridge_pid or 0} "
        f"host_pid={host_pid or 0} "
        f"bridge_has_downlink={int(bridge_has_downlink)} "
        f"host_has_network_call={int(host_has_network_call)} "
        f"host_watchdog_count={host_watchdog_count} "
        f"recent_voice={recent_voice}"
    )

    if not host_pid:
        subprocess.run(["systemctl", "start", args.host_service], check=True)
        print(f"started={args.host_service} reason=inactive")
        return 0

    if min(bridge_age, host_age) < args.cooldown_seconds:
        return 0

    # Host recovery is deliberately owned by dvmhost_recover.py, which uses
    # stricter evidence. Restarting dvmhost here would discard ARS/TMS/LRRP
    # sessions because of a single transient network watchdog event.
    reason = recovery_reason(worst, args.max_watchdog_seconds)
    if not reason:
        return 0

    if throttle_active(args.stamp, args.min_interval):
        print("skip=throttled")
        return 0
    touch_stamp(args.stamp)

    restart_bridge(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"command failed: {exc}", file=sys.stderr)
        raise SystemExit(exc.returncode or 1)
