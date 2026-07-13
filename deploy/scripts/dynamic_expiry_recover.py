#!/usr/bin/env python3

import argparse
import sys
import time
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_config import read_brandmeister_device


def parse_brandmeister_device(config_path: Path) -> tuple[int, int]:
    return read_brandmeister_device(config_path)


def load_api_key(path: Path) -> str:
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError(f"BM API key file is empty: {path}")
    return key


def brandmeister_action(device_id: int, slot: int, action: str, api_key: str, timeout_seconds: int) -> None:
    url = f"https://api.brandmeister.network/v2/device/{device_id}/action/{action}/{slot}"
    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()


def fetch_profile(device_id: int, api_key: str, timeout_seconds: int) -> dict:
    response = requests.get(
        f"https://api.brandmeister.network/v2/device/{device_id}/profile",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.json()


def wait_for_dynamic_clear(device_id: int, slot: int, expired_tg: int, api_key: str, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        profile = fetch_profile(device_id, api_key, timeout_seconds)
        dynamic = profile.get("dynamicSubscriptions", [])
        still_active = False
        for entry in dynamic:
            try:
                talkgroup = int(entry.get("talkgroup", 0))
                entry_slot = int(entry.get("slot", 0))
            except (TypeError, ValueError):
                continue
            if talkgroup == expired_tg and entry_slot == slot:
                still_active = True
                break
        if not still_active:
            return True
        brandmeister_action(device_id, slot, "dropCallRoute", api_key, timeout_seconds)
        brandmeister_action(device_id, slot, "dropDynamicGroups", api_key, timeout_seconds)
        time.sleep(0.5)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover BM->P25 path after dynamic TG expiry.")
    parser.add_argument("--trigger", type=Path, default=Path("/home/quantar/quantar-runtime/dynamic_expired.trigger"))
    parser.add_argument("--config", type=Path, default=Path("/home/quantar/quantar-runtime/quantarbridge.yml"))
    parser.add_argument("--api-key-file", type=Path, default=Path("/home/quantar/quantar-runtime/bm_api.key"))
    parser.add_argument("--slot", type=int, choices=(0, 1, 2), help="Override BrandMeister API slot")
    parser.add_argument("--wait-ready-seconds", type=int, default=20)
    args = parser.parse_args()

    expired_tg = 0
    if args.trigger.exists():
        try:
            expired_tg = int(args.trigger.read_text(encoding="utf-8").strip())
        except ValueError:
            expired_tg = 0
    args.trigger.unlink(missing_ok=True)

    api_key = load_api_key(args.api_key_file)
    device_id, configured_api_slot = parse_brandmeister_device(args.config)
    api_slot = args.slot if args.slot is not None else configured_api_slot
    brandmeister_action(device_id, api_slot, "dropCallRoute", api_key, args.wait_ready_seconds)
    brandmeister_action(device_id, api_slot, "dropDynamicGroups", api_key, args.wait_ready_seconds)
    if expired_tg != 0:
        wait_for_dynamic_clear(device_id, api_slot, expired_tg, api_key, args.wait_ready_seconds)

    print(f"cleared_dynamic_route={expired_tg or 'none'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
