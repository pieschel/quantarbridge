#!/usr/bin/env python3
"""Migrate an existing private runtime to the TETRAPACK BREW audio path."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Any

import yaml


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def backup(path: Path, stamp: str) -> None:
    if not path.exists():
        return
    target = path.with_name(f"{path.name}.pre-brew-audio-{stamp}")
    shutil.copy2(path, target)
    os.chmod(target, path.stat().st_mode & 0o777)


def read_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"invalid YAML root: {path}")
    return payload


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(
        path,
        yaml.safe_dump(payload, sort_keys=False, default_flow_style=False).encode("utf-8"),
    )


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid JSON root: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(path, (json.dumps(payload, indent=2, ensure_ascii=True) + "\n").encode("utf-8"))


def replace_paths(value: Any, runtime_dir: Path, install_dir: Path) -> Any:
    if isinstance(value, dict):
        return {key: replace_paths(item, runtime_dir, install_dir) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_paths(item, runtime_dir, install_dir) for item in value]
    if isinstance(value, str):
        return value.replace("/home/quantar/quantar-runtime", str(runtime_dir)).replace(
            "/home/quantar/quantarbridge", str(install_dir)
        )
    return value


def update_audio_profiles(runtime_dir: Path) -> None:
    p25_path = runtime_dir / "dvmbridge-p25-to-dmr.yml"
    p25 = read_yaml(p25_path)
    p25.setdefault("system", {}).update(
        {
            "identity": "BRIDGE-P25-PCM-ONLY",
            "txMode": 1,
            "rxAudioGain": 1.0,
            "vocoderDecoderAudioGain": 0.4,
            "vocoderDecoderAutoGain": False,
            "vocoderDecoderUvQuality": 3,
            "txAudioGain": 2.0,
            "vocoderEncoderAudioGain": 0.0,
            "dmrEncodeHighCutHz": 2500,
            "dropTimeMs": 600,
            "udpAudio": True,
            "udpMetadata": True,
            "udpSendPort": 31120,
            "udpSendAddress": "127.0.0.1",
            "udpReceivePort": 31122,
            "udpReceiveAddress": "127.0.0.1",
            "udpRTPFrames": True,
            "udpIgnoreRTPTiming": True,
            "udpRTPContinuousSeq": False,
            "udpUseULaw": False,
            "udpUsrp": False,
            "udpFrameTiming": False,
        }
    )
    write_yaml(p25_path, p25)

    downlink_path = runtime_dir / "dvmbridge-dmr-to-p25.yml"
    downlink = read_yaml(downlink_path)
    downlink.setdefault("system", {}).update(
        {
            "identity": "BRIDGE-PCM-P25-ONLY",
            "txMode": 2,
            "rxAudioGain": 0.3,
            "vocoderDecoderAudioGain": 0.4,
            "vocoderDecoderAutoGain": False,
            "vocoderDecoderUvQuality": 12,
            "txAudioGain": 1.10,
            "vocoderEncoderAudioGain": 0.0,
            "p25EncodePresenceGain": 0.0,
            "p25EncodeHighCutHz": 2500,
            "p25EncodeAgc": False,
            "p25EncodeAgcPeakLimit": 24000,
            "dropTimeMs": 500,
            "udpAudio": True,
            "udpMetadata": True,
            "udpSendPort": 31123,
            "udpSendAddress": "127.0.0.1",
            "udpReceivePort": 31121,
            "udpReceiveAddress": "127.0.0.1",
            "udpRTPFrames": True,
            "udpIgnoreRTPTiming": True,
            "udpRTPContinuousSeq": False,
            "udpUseULaw": False,
            "udpUsrp": False,
            "udpFrameTiming": False,
        }
    )
    write_yaml(downlink_path, downlink)


def update_dashboard(path: Path, runtime_dir: Path, install_dir: Path) -> None:
    dashboard = read_json(path)
    audio_config = runtime_dir / "tetrapack-brew-audio.json"
    sms_config = runtime_dir / "tetrapack-brew-bridge.json"
    status_file = runtime_dir / "brew-audio-status.json"
    dashboard["brewAudioConfig"] = str(audio_config)
    dashboard["brewAudioStatusFile"] = str(status_file)

    services = dashboard.setdefault("serviceUnits", [])
    labels = {
        "p25-to-dmr": "P25 nach BREW PCM",
        "dmr-to-p25": "BREW PCM nach P25",
        "quantarbridge": "BrandMeister Datenbruecke",
    }
    for service in services:
        if isinstance(service, dict) and service.get("id") in labels:
            service["label"] = labels[str(service["id"])]
        if isinstance(service, dict) and service.get("id") == "sms-bridge":
            service["processMatch"] = f"{install_dir}/deploy/scripts/tetrapack_brew_bridge.py --config {sms_config}"
    services[:] = [service for service in services if not isinstance(service, dict) or service.get("id") != "brew-audio"]
    services.append(
        {
            "id": "brew-audio",
            "label": "TETRAPACK BREW Audio",
            "unit": "tetrapack-brew-audio.service",
            "userUnit": True,
            "processMatch": f"{install_dir}/deploy/scripts/tetrapack_brew_audio.py --config {audio_config}",
            "critical": True,
        }
    )
    dashboard.setdefault("restartTargets", {})["brew-audio"] = {
        "type": "systemd-user",
        "unit": "tetrapack-brew-audio.service",
    }
    sms_restart = dashboard.setdefault("restartTargets", {}).get("sms-bridge")
    if isinstance(sms_restart, dict) and sms_restart.get("type") == "process":
        sms_restart["match"] = f"{install_dir}/deploy/scripts/tetrapack_brew_bridge.py --config {sms_config}"
        sms_restart["command"] = [
            "/usr/bin/python3",
            f"{install_dir}/deploy/scripts/tetrapack_brew_bridge.py",
            "--config",
            str(sms_config),
        ]
    write_json(path, dashboard)


def migrate(args: argparse.Namespace) -> None:
    runtime_dir = args.runtime_dir.resolve()
    install_dir = args.install_dir.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    main_config = runtime_dir / "quantarbridge.yml"
    p25_config = runtime_dir / "dvmbridge-p25-to-dmr.yml"
    downlink_config = runtime_dir / "dvmbridge-dmr-to-p25.yml"
    canonical_brew = runtime_dir / "tetrapack-brew-bridge.json"
    legacy_brew = install_dir / "deploy" / "runtime" / "tetrapack-brew-bridge.json"
    dashboard_candidates = [
        runtime_dir / "quantar-dashboard.json",
        install_dir / "deploy" / "runtime" / "quantar-dashboard.json",
    ]
    dashboard_path = next((path for path in dashboard_candidates if path.exists()), None)
    source_brew = canonical_brew if canonical_brew.exists() else legacy_brew
    required = [main_config, p25_config, downlink_config, source_brew]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("missing runtime file(s): " + ", ".join(missing))

    brew = read_json(source_brew)
    brew_settings = brew.get("brew", {})
    main = read_yaml(main_config)
    if not brew_settings.get("enabled") or not brew_settings.get("username"):
        raise ValueError("BREW must be enabled and have a username before audio migration")
    if not brew_settings.get("password") and not main.get("brandmeister", {}).get("password"):
        raise ValueError("no protected BREW or BrandMeister device password is available")

    touched = [main_config, p25_config, downlink_config, canonical_brew]
    if dashboard_path is not None:
        touched.append(dashboard_path)
    for path in touched:
        backup(path, stamp)

    main.setdefault("brandmeister", {})["voiceEnabled"] = False
    write_yaml(main_config, main)
    update_audio_profiles(runtime_dir)
    if source_brew != canonical_brew:
        write_json(canonical_brew, brew)

    audio_template = read_json(install_dir / "deploy" / "examples" / "tetrapack-brew-audio.json")
    audio = replace_paths(audio_template, runtime_dir, install_dir)
    audio["existingBrewConfig"] = str(canonical_brew)
    audio["codecLibrary"] = str(args.codec_library.resolve())
    write_json(runtime_dir / "tetrapack-brew-audio.json", audio)
    if dashboard_path is not None:
        update_dashboard(dashboard_path, runtime_dir, install_dir)

    print(f"runtime={runtime_dir}")
    print(f"dashboard={dashboard_path or 'not-found'}")
    print("brew_audio_enabled=true")
    print("credentials_preserved=true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=Path("/home/quantar/quantar-runtime"))
    parser.add_argument("--install-dir", type=Path, default=Path("/home/quantar/quantarbridge"))
    parser.add_argument(
        "--codec-library",
        type=Path,
        default=Path("/home/quantar/src/tetra-codec/build/libtetra-codec.so"),
    )
    return parser.parse_args()


if __name__ == "__main__":
    migrate(parse_args())
