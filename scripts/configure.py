#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPOSITORY_ROOT / "deploy" / "examples"
DEFAULT_RUNTIME_DIR = Path("/home/quantar/quantar-runtime")
DEFAULT_INSTALL_DIR = Path("/home/quantar/quantarbridge")


def parse_repeater_id(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{5}", value):
        raise argparse.ArgumentTypeError("BrandMeister repeater ID must be six digits")
    return int(value)


def parse_frequency(value: str) -> int:
    try:
        frequency = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("frequency must be an integer in Hz") from error
    if not 1_000_000 <= frequency <= 1_000_000_000:
        raise argparse.ArgumentTypeError("frequency must be between 1 MHz and 1 GHz")
    return frequency


def parse_ipv4(value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as error:
        raise argparse.ArgumentTypeError("invalid IPv4 address") from error


def parse_callsign(value: str) -> str:
    callsign = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9/-]{3,16}", callsign):
        raise argparse.ArgumentTypeError("invalid callsign syntax")
    return callsign


def parse_hex(value: str, digits: int, label: str) -> str:
    normalized = value.strip().upper()
    if not re.fullmatch(rf"[0-9A-F]{{{digits}}}", normalized):
        raise argparse.ArgumentTypeError(f"{label} must contain exactly {digits} hex digits")
    return normalized


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def replace_paths(value: Any, runtime_dir: Path, install_dir: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: replace_paths(item, runtime_dir, install_dir)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [replace_paths(item, runtime_dir, install_dir) for item in value]
    if isinstance(value, str):
        return value.replace(
            str(DEFAULT_RUNTIME_DIR), str(runtime_dir)
        ).replace(str(DEFAULT_INSTALL_DIR), str(install_dir))
    return value


def load_yaml(name: str) -> dict[str, Any]:
    raw = yaml.safe_load((EXAMPLE_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"template {name} is not a YAML mapping")
    return raw


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(
        path,
        yaml.safe_dump(payload, sort_keys=False, default_flow_style=False),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a private QuantarBridge runtime configuration from public templates."
    )
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL_DIR)
    parser.add_argument("--bm-id", required=True, type=parse_repeater_id)
    parser.add_argument("--bm-callsign", required=True, type=parse_callsign)
    parser.add_argument("--bm-master", required=True)
    parser.add_argument("--brew-username", required=True)
    parser.add_argument("--rx-frequency", required=True, type=parse_frequency)
    parser.add_argument("--tx-frequency", required=True, type=parse_frequency)
    parser.add_argument("--serial-port", default="/dev/ttyUSB0")
    parser.add_argument("--ars-server-ip", type=parse_ipv4, default="10.0.0.2")
    parser.add_argument("--ars-peer-ip", type=parse_ipv4, default="10.0.0.1")
    parser.add_argument(
        "--p25-nac",
        type=lambda value: parse_hex(value, 3, "P25 NAC"),
        default="293",
    )
    parser.add_argument(
        "--p25-network-id",
        type=lambda value: parse_hex(value, 5, "P25 network ID"),
        default="BB800",
    )
    parser.add_argument(
        "--p25-system-id",
        type=lambda value: parse_hex(value, 3, "P25 system ID"),
        default="001",
    )
    parser.add_argument("--latitude", type=float, default=0.0)
    parser.add_argument("--longitude", type=float, default=0.0)
    parser.add_argument("--height", type=int, default=0)
    parser.add_argument("--power", type=int, default=25)
    parser.add_argument("--location", default="Quantar Bridge")
    parser.add_argument("--dashboard-listen", default="127.0.0.1")
    parser.add_argument("--dashboard-port", type=int, default=8088)
    parser.add_argument("--bm-password-stdin", action="store_true", required=True)
    parser.add_argument("--force", action="store_true")
    return parser


def validate_arguments(args: argparse.Namespace) -> None:
    if not args.runtime_dir.is_absolute() or not args.install_dir.is_absolute():
        raise ValueError("runtime and install directories must be absolute")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", args.bm_master) or "." not in args.bm_master:
        raise ValueError("BrandMeister master must be a hostname without a URL scheme")
    if not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,64}", args.brew_username):
        raise ValueError("BREW username contains unsupported characters")
    if not -90.0 <= args.latitude <= 90.0:
        raise ValueError("latitude must be between -90 and 90")
    if not -180.0 <= args.longitude <= 180.0:
        raise ValueError("longitude must be between -180 and 180")
    if not 0 <= args.height <= 10_000:
        raise ValueError("height must be between 0 and 10000 metres")
    if not 0 <= args.power <= 1_000:
        raise ValueError("power must be between 0 and 1000 watts")
    if not 1 <= args.dashboard_port <= 65535:
        raise ValueError("dashboard port must be between 1 and 65535")
    parse_ipv4(args.dashboard_listen)
    if any(character in args.serial_port for character in "\r\n\0"):
        raise ValueError("serial port contains invalid characters")


def configure(args: argparse.Namespace, brandmeister_password: str) -> None:
    runtime_dir = args.runtime_dir.resolve()
    install_dir = args.install_dir.resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime_dir, 0o700)

    managed_files = {
        "quantarbridge.yml",
        "dvmfne-config.yml",
        "dvmhost-config.yml",
        "dvmbridge-p25-to-dmr.yml",
        "dvmbridge-dmr-to-p25.yml",
        "quantar-dashboard.json",
        "tetrapack-brew-bridge.json",
        "tetrapack-brew-audio.json",
        "iden_table.dat",
        "peer_list.dat",
        "talkgroup_rules.yml",
        "RSSI.dat",
        "rid_acl.dat",
    }
    existing = sorted(path.name for path in runtime_dir.iterdir() if path.name in managed_files)
    if existing and not args.force:
        raise FileExistsError(
            "runtime configuration already exists; use --force only after making a backup"
        )

    local_fne_password = secrets.token_urlsafe(32)
    files: dict[str, dict[str, Any]] = {
        name: replace_paths(load_yaml(name), runtime_dir, install_dir)
        for name in (
            "quantarbridge.yml",
            "dvmfne-config.yml",
            "dvmhost-config.yml",
            "dvmbridge-p25-to-dmr.yml",
            "dvmbridge-dmr-to-p25.yml",
        )
    }

    bridge = files["quantarbridge.yml"]
    bridge["fne"]["password"] = local_fne_password
    bm = bridge["brandmeister"]
    bm.update(
        {
            "repeaterId": args.bm_id,
            "password": brandmeister_password,
            "address": args.bm_master,
            "callsign": args.bm_callsign,
            "rxFrequency": args.rx_frequency,
            "txFrequency": args.tx_frequency,
            "power": args.power,
            "latitude": f"{args.latitude:.6f}",
            "longitude": f"{args.longitude:.6f}",
            "height": str(args.height),
            "location": args.location,
        }
    )

    files["dvmfne-config.yml"]["master"]["password"] = local_fne_password
    host = files["dvmhost-config.yml"]
    host["network"]["password"] = local_fne_password
    host["protocols"]["p25"]["motorolaPacketData"] = {
        "arsServerAddress": args.ars_server_ip,
        "arsPeerAddress": args.ars_peer_ip,
    }
    host["system"]["identity"] = args.bm_callsign
    host["system"]["info"].update(
        {
            "latitude": args.latitude,
            "longitude": args.longitude,
            "height": args.height,
            "power": args.power,
            "location": args.location,
        }
    )
    host["system"]["cwId"]["callsign"] = args.bm_callsign
    host["system"]["modem"]["protocol"]["uart"]["port"] = args.serial_port
    host["system"]["config"].update(
        {
            "nac": int(args.p25_nac, 16),
            "netId": args.p25_network_id,
            "sysId": args.p25_system_id,
        }
    )

    for name in ("dvmbridge-p25-to-dmr.yml", "dvmbridge-dmr-to-p25.yml"):
        files[name]["network"]["password"] = local_fne_password
        files[name]["system"]["netId"] = args.p25_network_id
        files[name]["system"]["sysId"] = args.p25_system_id

    for name, payload in files.items():
        write_yaml(runtime_dir / name, payload)

    dashboard = replace_paths(
        json.loads((EXAMPLE_DIR / "quantar-dashboard.json").read_text(encoding="utf-8")),
        runtime_dir,
        install_dir,
    )
    dashboard["listenAddress"] = args.dashboard_listen
    dashboard["port"] = args.dashboard_port
    atomic_write(
        runtime_dir / "quantar-dashboard.json",
        json.dumps(dashboard, indent=2, ensure_ascii=True) + "\n",
    )

    tetrapack = replace_paths(
        json.loads((EXAMPLE_DIR / "tetrapack-brew-bridge.json").read_text(encoding="utf-8")),
        runtime_dir,
        install_dir,
    )
    tetrapack["brew"].update(
        {
            "enabled": True,
            "username": args.brew_username,
            "password": "",
        }
    )
    atomic_write(
        runtime_dir / "tetrapack-brew-bridge.json",
        json.dumps(tetrapack, indent=2, ensure_ascii=True) + "\n",
    )

    brew_audio = replace_paths(
        json.loads((EXAMPLE_DIR / "tetrapack-brew-audio.json").read_text(encoding="utf-8")),
        runtime_dir,
        install_dir,
    )
    atomic_write(
        runtime_dir / "tetrapack-brew-audio.json",
        json.dumps(brew_audio, indent=2, ensure_ascii=True) + "\n",
    )

    for name in ("iden_table.dat", "peer_list.dat", "RSSI.dat"):
        shutil.copyfile(EXAMPLE_DIR / name, runtime_dir / name)
        os.chmod(runtime_dir / name, 0o600)
    shutil.copyfile(EXAMPLE_DIR / "talkgroup_rules.yml", runtime_dir / "talkgroup_rules.yml")
    os.chmod(runtime_dir / "talkgroup_rules.yml", 0o600)
    atomic_write(runtime_dir / "rid_acl.dat", "")

    for directory in (
        runtime_dir / "log",
        runtime_dir / "dashboard-backups",
        runtime_dir / "sms" / "inbox",
        runtime_dir / "sms" / "outbox",
        runtime_dir / "sms" / "sent",
        runtime_dir / "sms" / "error",
        runtime_dir / "sms" / "processed",
        runtime_dir / "sms" / "p25-outbox",
        runtime_dir / "sms" / "p25-sent",
        runtime_dir / "sms" / "service-routes",
        runtime_dir / "sms" / "brew-audio-outbox",
        runtime_dir / "sms" / "brew-audio-results",
        runtime_dir / "sms" / "brew-audio-errors",
        runtime_dir / "sms" / "p25-failed",
        runtime_dir / "sms" / "quantarbridge-inbox",
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)

    print(f"runtime={runtime_dir}")
    print(f"brandmeister_repeater_id={args.bm_id}")
    print(f"ars_server={args.ars_server_ip}")
    print(f"brew_username={args.brew_username}")
    print("credentials_written=true")


def main() -> int:
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args()
    try:
        validate_arguments(args)
        brandmeister_password = sys.stdin.readline().rstrip("\r\n")
        if not brandmeister_password or any(
            character in brandmeister_password for character in "\r\n\0"
        ):
            raise ValueError("BrandMeister password from stdin is empty or invalid")
        configure(args, brandmeister_password)
    except (FileExistsError, OSError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
