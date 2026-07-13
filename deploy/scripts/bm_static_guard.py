#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_config import read_brandmeister_device, read_static_talkgroups


def get_profile(device_id: int, timeout: float):
    import requests

    url = f"https://api.brandmeister.network/v2/device/{device_id}/profile"
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def throttle_active(stamp_path: Path, min_interval_seconds: float) -> bool:
    if min_interval_seconds <= 0:
        return False
    try:
        age = time.time() - stamp_path.stat().st_mtime
    except FileNotFoundError:
        return False
    return age < min_interval_seconds


def touch_stamp(stamp_path: Path) -> None:
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.touch()


def read_device_and_slot(config_path: Path) -> tuple[int, int]:
    return read_brandmeister_device(config_path)


def read_required_talkgroups(config_path: Path) -> set[int]:
    return set(read_static_talkgroups(config_path))


def static_talkgroups_for_slot(payload: dict, slot: int) -> set[int]:
    talkgroups: set[int] = set()
    for entry in payload.get("staticSubscriptions", []):
        try:
            entry_slot = int(entry.get("slot", -1))
            talkgroup = int(entry["talkgroup"])
        except (KeyError, TypeError, ValueError):
            continue
        if entry_slot == slot:
            talkgroups.add(talkgroup)
    return talkgroups


def main():
    parser = argparse.ArgumentParser(description="Ensure configured static BM talkgroups remain visible after dynamic TG expiry.")
    parser.add_argument("--config", type=Path, default=Path("/home/quantar/quantar-runtime/quantarbridge.yml"))
    parser.add_argument("--device-id", type=int)
    parser.add_argument("--slot", type=int)
    parser.add_argument("--required-tg", type=int, action="append", default=[])
    parser.add_argument("--restart", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--stamp", type=Path, default=Path("/home/quantar/quantar-runtime/bm_static_guard.stamp"))
    parser.add_argument("--min-interval", type=float, default=600.0)
    args = parser.parse_args()

    configured_device_id, configured_slot = read_device_and_slot(args.config)
    if args.device_id is not None and args.device_id != configured_device_id:
        print(
            f"Configured repeater ID {configured_device_id} overrides stale command-line device ID {args.device_id}",
            file=sys.stderr,
        )
    if args.slot is not None and args.slot != configured_slot:
        print(
            f"Configured API slot {configured_slot} overrides stale command-line slot {args.slot}",
            file=sys.stderr,
        )
    device_id = configured_device_id
    slot = configured_slot
    if slot not in (0, 1, 2):
        raise RuntimeError("BrandMeister API slot must be 0, 1 or 2")

    if throttle_active(args.stamp, args.min_interval):
        print(json.dumps({"deviceId": device_id, "slot": slot, "missing": [], "skipped": "throttled"}, separators=(",", ":")))
        return 0
    touch_stamp(args.stamp)

    payload = get_profile(device_id, args.timeout)
    static_tgs = static_talkgroups_for_slot(payload, slot)

    configured_required_tgs = read_required_talkgroups(args.config)
    required_tgs = configured_required_tgs or set(args.required_tg)
    missing = sorted(tg for tg in required_tgs if tg not in static_tgs)
    print(json.dumps({"deviceId": device_id, "slot": slot, "staticTalkgroups": sorted(static_tgs), "missing": missing}, separators=(",", ":")))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"Service restart failed: {exc}", file=sys.stderr)
        raise SystemExit(exc.returncode or 1)
    except Exception as exc:
        print(f"BrandMeister guard failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
