#!/usr/bin/env python3

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, List

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_config import read_brandmeister_device, read_brandmeister_voice_enabled

ALWAYS_SEND_PEERS = []
INCLUSION_PEERS = [9000110, 9000111, 9000101, 9000112]
PREFERRED_PEERS = []

LOCAL_PEER_CONFIGS = (
    ("dvmhost-config.yml", ("network", "id")),
    ("dvmbridge-p25-to-dmr.yml", ("network", "id")),
    ("quantarbridge.yml", ("fne", "peerId")),
    ("dvmbridge-dmr-to-p25.yml", ("network", "id")),
)


def brew_audio_owns_voice(config_path: Path) -> bool:
    return not read_brandmeister_voice_enabled(config_path)

SMS_CONFIG_BLOCK = """
sms:
  enabled: true
  bindAddress: "127.0.0.1"
  arsPort: 4015
  tmsPort: 4017
  outboundAddress: "127.0.0.1"
  outboundArsPort: 4005
  outboundTmsPort: 4007
  outboundMode: "brandmeister"
  bmSourceIp: "auto"
  bmTargetIp: "auto"
  bmSlot: 2
  inboxPath: "/home/quantar/quantar-runtime/sms/quantarbridge-inbox"
  outboxPath: "/home/quantar/quantar-runtime/sms/outbox"
  sentPath: "/home/quantar/quantar-runtime/sms/sent"
  p25OutboxPath: "/home/quantar/quantar-runtime/sms/p25-outbox"
  serviceRoutePath: "/home/quantar/quantar-runtime/sms/service-routes"
  pollIntervalMs: 100
  maxPacketBytes: 2048
  decodeUtf16Le: true
""".strip()


def load_api_key(path: Path | None) -> str | None:
    if path is None:
        return None
    key = path.read_text(encoding="utf-8").strip()
    return key or None


def static_talkgroups_for_slot(payload: dict, slot: int) -> List[int]:
    talkgroups = []
    for entry in payload.get("staticSubscriptions", []):
        try:
            entry_slot = int(entry["slot"])
            talkgroup = int(entry["talkgroup"])
        except (KeyError, TypeError, ValueError):
            continue
        if entry_slot == slot:
            talkgroups.append(talkgroup)
    return sorted(set(talkgroups))


def configured_static_talkgroups(payload: dict, slot: int) -> List[int]:
    talkgroups = static_talkgroups_for_slot(payload, slot)
    if talkgroups or slot == 0:
        return talkgroups

    legacy_talkgroups = static_talkgroups_for_slot(payload, 0)
    if legacy_talkgroups:
        print(
            f"Using legacy slot 0 subscriptions until the repeater profile is moved to TS{slot}",
            file=sys.stderr,
        )
    return legacy_talkgroups


def fetch_static_talkgroups(device_id: int, slot: int, timeout: float, api_key: str | None) -> List[int]:
    import requests

    url = f"https://api.brandmeister.network/v2/device/{device_id}/profile"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = requests.get(url, timeout=timeout, headers=headers)
    response.raise_for_status()
    payload = response.json()

    return configured_static_talkgroups(payload, slot)


def write_text_if_changed(path: Path, new_text: str) -> bool:
    current_text = path.read_text(encoding="utf-8") if path.exists() else ""
    if current_text == new_text:
        return False

    path.write_text(new_text, encoding="utf-8")
    return True


def format_static_talkgroups_block(static_talkgroups: Iterable[int]) -> str:
    lines = ["  staticTalkgroups:"]
    for tg in static_talkgroups:
        lines.append(f"    - {tg}")
    return "\n".join(lines) + "\n"


def ensure_talkgroup_mappings(text: str) -> str:
    if re.search(r"(?m)^  talkgroupMappings\s*:", text):
        return text

    static_pattern = re.compile(
        r"(?m)^  staticTalkgroups:[^\r\n]*\r?\n"
        r"(?:^    - [^\r\n]*(?:\r?\n|$))*"
    )
    match = static_pattern.search(text)
    if not match:
        raise RuntimeError("Could not find routing.staticTalkgroups for talkgroup mappings")
    return text[:match.end()] + "  talkgroupMappings: []\n" + text[match.end():]


def read_configured_device_id(path: Path) -> int:
    return read_brandmeister_device(path)[0]


def read_configured_api_slot(path: Path) -> int:
    return read_brandmeister_device(path)[1]


def update_quantarbridge_config(path: Path, static_talkgroups: Iterable[int]) -> bool:
    current_text = path.read_text(encoding="utf-8")
    replacement = format_static_talkgroups_block(static_talkgroups)
    pattern = re.compile(
        r"(?m)^  staticTalkgroups:[^\r\n]*\r?\n"
        r"(?:^    - [^\r\n]*(?:\r?\n|$))*"
    )
    if not pattern.search(current_text):
        raise RuntimeError(f"Could not find routing.staticTalkgroups in {path}")
    updated_text = pattern.sub(replacement, current_text, count=1)
    updated_text = ensure_talkgroup_mappings(updated_text)
    if not re.search(r"(?m)^sms:\s*$", updated_text):
        updated_text = updated_text.rstrip() + "\n\n" + SMS_CONFIG_BLOCK + "\n"
    return write_text_if_changed(path, updated_text)


def _read_yaml_integer(path: Path, keys: tuple[str, ...]) -> int:
    """Read one integer from the small mapping-only runtime YAML subset."""
    stack: list[tuple[int, str]] = []
    matches: list[str] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"( *)([A-Za-z0-9_-]+):(?:[ \t]*(.*))?", line)
        if match is None:
            continue

        indent = len(match.group(1))
        key = match.group(2)
        value = (match.group(3) or "").strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()

        current_keys = tuple(item[1] for item in stack) + (key,)
        if current_keys == keys:
            matches.append(value)
        if not value:
            stack.append((indent, key))

    if len(matches) != 1:
        raise RuntimeError(f"Could not read {'.'.join(keys)} from {path}")

    value = re.sub(r"\s+#.*$", "", matches[0]).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    if not re.fullmatch(r"[0-9]+", value):
        raise RuntimeError(f"Could not read integer {'.'.join(keys)} from {path}")
    return int(value)


def configured_local_peer_ids(runtime_dir: Path) -> list[int]:
    existing = [
        runtime_dir / name
        for name, _ in LOCAL_PEER_CONFIGS
        if (runtime_dir / name).exists()
    ]
    if not existing:
        return list(INCLUSION_PEERS)
    if len(existing) != len(LOCAL_PEER_CONFIGS):
        missing = [name for name, _ in LOCAL_PEER_CONFIGS if not (runtime_dir / name).exists()]
        raise RuntimeError(f"Could not discover all local FNE peers; missing: {', '.join(missing)}")

    peers: list[int] = []
    for name, keys in LOCAL_PEER_CONFIGS:
        path = runtime_dir / name
        peer_id = _read_yaml_integer(path, keys)
        if not 1 <= peer_id <= 0xFFFFFFFF:
            raise RuntimeError(f"Invalid local FNE peer ID in {path}: {peer_id}")
        if peer_id not in peers:
            peers.append(peer_id)
    return peers


def build_talkgroup_rules(
    static_talkgroups: Iterable[int],
    inclusion_peers: Iterable[int] = INCLUSION_PEERS,
):
    lines = ["groupVoice:"]
    always_lines = ["      always: []"]
    if ALWAYS_SEND_PEERS:
        always_lines = ["      always:"]
        always_lines.extend(f"        - {peer_id}" for peer_id in ALWAYS_SEND_PEERS)
    inclusion_peers = list(inclusion_peers)
    inclusion_lines = ["      inclusion: []"]
    if inclusion_peers:
        inclusion_lines = ["      inclusion:"]
        inclusion_lines.extend(f"        - {peer_id}" for peer_id in inclusion_peers)
    preferred_lines = ["      preferred: []"]
    if PREFERRED_PEERS:
        preferred_lines = ["      preferred:"]
        preferred_lines.extend(f"        - {peer_id}" for peer_id in PREFERRED_PEERS)
    lines.extend(
        [
            "  - name: BM Dynamic Slot2",
            "    alias: BrandMeister",
            "    config:",
            "      active: true",
            "      affiliated: false",
            *inclusion_lines,
            "      exclusion: []",
            "      rewrite: []",
            *always_lines,
            *preferred_lines,
            "      rid_permitted: []",
            "    source:",
            "      tgid: 0",
            "      slot: 2",
        ]
    )
    return "\n".join(lines) + "\n"


def update_talkgroup_rules(path: Path, static_talkgroups: Iterable[int]) -> bool:
    inclusion_peers = configured_local_peer_ids(path.parent)
    return write_text_if_changed(path, build_talkgroup_rules(static_talkgroups, inclusion_peers))


def restart_services(services: Iterable[str]) -> None:
    requested = list(dict.fromkeys(services))
    if "dvmfne.service" in requested:
        paired = (
            "dvmfne.service",
            "dvmbridge-p25-to-dmr.service",
            "dvmbridge-dmr-to-p25.service",
        )
        requested = [*paired, *(service for service in requested if service not in paired)]

    for service in requested:
        if service in {"dvmhost.service", "tetrapack-brew-bridge.service"}:
            print(f"restart skipped for stateful service: {service}")
            continue
        subprocess.run(["systemctl", "restart", service], check=True)


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
    parser = argparse.ArgumentParser(description="Sync BrandMeister static talkgroups into local runtime config.")
    parser.add_argument("--device-id", type=int)
    parser.add_argument("--quantarbridge-config", type=Path, required=True)
    parser.add_argument("--talkgroup-rules", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--restart", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--stamp", type=Path, default=Path("/home/quantar/quantar-runtime/bm_static_sync.stamp"))
    parser.add_argument("--min-interval", type=float, default=300.0)
    args = parser.parse_args()

    configured_device_id = read_configured_device_id(args.quantarbridge_config)
    if args.device_id is not None and args.device_id != configured_device_id:
        print(
            f"Configured repeater ID {configured_device_id} overrides stale command-line device ID {args.device_id}",
            file=sys.stderr,
        )
    device_id = configured_device_id
    api_slot = read_configured_api_slot(args.quantarbridge_config)

    if throttle_active(args.stamp, args.min_interval):
        print(json.dumps({"deviceId": device_id, "changed": False, "skipped": "throttled"}, separators=(",", ":")))
        return 0
    touch_stamp(args.stamp)

    api_key = load_api_key(args.api_key_file)
    static_talkgroups = fetch_static_talkgroups(device_id, api_slot, args.timeout, api_key)

    changed = False
    changed |= update_quantarbridge_config(args.quantarbridge_config, static_talkgroups)
    changed |= update_talkgroup_rules(args.talkgroup_rules, static_talkgroups)

    summary = {
        "deviceId": device_id,
        "slot": api_slot,
        "staticTalkgroups": static_talkgroups,
        "changed": changed,
    }
    print(json.dumps(summary, separators=(",", ":")))

    if changed and args.restart and not brew_audio_owns_voice(args.quantarbridge_config):
        restart_services(args.restart)
    elif changed and args.restart:
        print("restart_skipped=tetrapack-brew-audio-hot-reload")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"Service restart failed: {exc}", file=sys.stderr)
        raise SystemExit(exc.returncode or 1)
    except Exception as exc:
        print(f"BrandMeister sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
