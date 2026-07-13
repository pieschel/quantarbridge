#!/usr/bin/env python3

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_config import read_brandmeister_device, read_static_talkgroups


def parse_static_talkgroups(config_path: Path) -> list[int]:
    return read_static_talkgroups(config_path)


def has_dynamic_state(state_path: Path) -> bool:
    if not state_path.exists():
        return False
    for line in state_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        return True
    return False


def parse_brandmeister_device(config_path: Path) -> tuple[int, int]:
    return read_brandmeister_device(config_path)


def parse_transport(config_path: Path) -> str:
    text = config_path.read_text(encoding="utf-8")
    match = re.search(r'(?m)^  transport:\s*"([^"]+)"\s*$', text)
    if not match:
        return "brandmeister"
    return match.group(1).strip()


def load_api_key(path: Path) -> str:
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError(f"BM API key file is empty: {path}")
    return key


def fetch_profile(device_id: int, api_key: str) -> dict:
    import requests

    response = requests.get(
        f"https://api.brandmeister.network/v2/device/{device_id}/profile",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def has_subscription_on_slot(subscriptions: object, slot: int) -> bool:
    if not isinstance(subscriptions, list):
        return False
    for entry in subscriptions:
        if not isinstance(entry, dict):
            continue
        try:
            if int(entry.get("slot", -1)) == slot:
                return True
        except (TypeError, ValueError):
            continue
    return False


def brandmeister_action(device_id: int, slot: int, action: str, api_key: str) -> None:
    import requests

    response = requests.get(
        f"https://api.brandmeister.network/v2/device/{device_id}/action/{action}/{slot}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=20,
    )
    response.raise_for_status()


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure the primary static TG stays selected when no dynamic TG is active.")
    parser.add_argument("--config", type=Path, default=Path("/home/quantar/quantar-runtime/quantarbridge.yml"))
    parser.add_argument("--state", type=Path, default=Path("/home/quantar/quantar-runtime/dynamic_routes.state"))
    parser.add_argument("--topic", default="dmr-gateway/dynamic")
    parser.add_argument("--slot", type=int, default=2, help="Local DMRGateway slot")
    parser.add_argument("--api-slot", type=int, choices=(0, 1, 2))
    parser.add_argument("--api-key-file", type=Path, default=Path("/home/quantar/quantar-runtime/bm_api.key"))
    parser.add_argument("--stamp", type=Path, default=Path("/home/quantar/quantar-runtime/ensure_static_tg.stamp"))
    parser.add_argument("--min-interval", type=float, default=300.0)
    args = parser.parse_args()

    if has_dynamic_state(args.state):
        print("skip=dynamic-active")
        return 0

    static_tgs = parse_static_talkgroups(args.config)
    if not static_tgs:
        print("skip=no-static")
        return 0

    if throttle_active(args.stamp, args.min_interval):
        print("skip=throttled")
        return 0
    touch_stamp(args.stamp)

    transport = parse_transport(args.config)
    device_id, configured_api_slot = parse_brandmeister_device(args.config)
    api_slot = args.api_slot if args.api_slot is not None else configured_api_slot
    api_key = load_api_key(args.api_key_file)
    profile = fetch_profile(device_id, api_key)
    dynamic_on_slot = has_subscription_on_slot(profile.get("dynamicSubscriptions"), api_slot)
    if dynamic_on_slot:
        brandmeister_action(device_id, api_slot, "dropCallRoute", api_key)
        brandmeister_action(device_id, api_slot, "dropDynamicGroups", api_key)
        print("cleared_bm_dynamic=true")

    tg = static_tgs[0]
    if transport == "mmdvm-gateway":
        subprocess.run(["mosquitto_pub", "-h", "127.0.0.1", "-t", args.topic, "-m", f"DynTG {args.slot} {tg}"], check=True)
    print(f"ensured_static_tg={tg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
