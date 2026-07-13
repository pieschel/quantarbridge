#!/usr/bin/env python3

import argparse
import subprocess
import sys


def run_command(command):
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def count_matches(text, needle):
    return sum(1 for line in text.splitlines() if needle in line)


def service_age_seconds(service):
    pid = run_command(["systemctl", "show", service, "--property=MainPID", "--value"]).strip()
    if not pid or pid == "0":
        return 0
    age_text = run_command(["ps", "-o", "etimes=", "-p", pid]).strip()
    return int(age_text or "0")


def main():
    parser = argparse.ArgumentParser(description="Restart dvmhost when the DFSI/V24 path appears stuck.")
    parser.add_argument("--window", default="10 minutes ago")
    parser.add_argument("--service", default="dvmhost.service")
    parser.add_argument("--min-watchdog-expiries", type=int, default=5)
    parser.add_argument("--min-net-calls", type=int, default=3)
    parser.add_argument("--cooldown-seconds", type=int, default=300)
    args = parser.parse_args()

    service_age = service_age_seconds(args.service)

    logs = run_command([
        "journalctl",
        "-u",
        args.service,
        "--since",
        args.window,
        "--no-pager",
        "-o",
        "cat",
    ])

    watchdog_expiries = count_matches(logs, "P25 Net network watchdog has expired")
    net_calls = count_matches(logs, "P25 Net network voice transmission")
    modem_ready = count_matches(logs, "Modem Ready [Direct Mode / V.24]")

    print(
        f"watchdog_expiries={watchdog_expiries} "
        f"net_calls={net_calls} "
        f"modem_ready={modem_ready} "
        f"service_age_seconds={service_age}"
    )

    if service_age < args.cooldown_seconds:
        return 0

    if watchdog_expiries < args.min_watchdog_expiries:
        return 0

    if net_calls < args.min_net_calls:
        return 0

    subprocess.run(["systemctl", "restart", args.service], check=True)
    print(f"restarted={args.service}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"command failed: {exc}", file=sys.stderr)
        raise SystemExit(exc.returncode or 1)
