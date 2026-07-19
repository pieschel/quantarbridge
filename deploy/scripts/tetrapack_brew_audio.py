#!/usr/bin/env python3
"""Bridge Quantar P25 PCM audio to TETRAPACK BREW group audio."""

from __future__ import annotations

import argparse
import collections
import ctypes
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import random
import re
import signal
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
import uuid

try:
    import yaml
except Exception:  # pragma: no cover - validated at service startup
    yaml = None


BREW_CLASS_SUBSCRIBER = 0xF0
BREW_CLASS_CALL_CONTROL = 0xF1
BREW_CLASS_FRAME = 0xF2
BREW_CLASS_ERROR = 0xF3

SUBSCRIBER_DEREGISTER = 0
SUBSCRIBER_REGISTER = 1
SUBSCRIBER_REREGISTER = 2
SUBSCRIBER_AFFILIATE = 8
SUBSCRIBER_DEAFFILIATE = 9

CALL_STATE_GROUP_TX = 2
CALL_STATE_GROUP_IDLE = 3
CALL_STATE_SHORT_TRANSFER = 11
FRAME_TYPE_TRAFFIC_CHANNEL = 0
FRAME_TYPE_SDS_TRANSFER = 1
FRAME_TYPE_SDS_REPORT = 2

RTP_HEADER_BYTES = 12
PCM_SAMPLES_20MS = 160
PCM_BYTES_20MS = PCM_SAMPLES_20MS * 2
RTP_PACKET_BYTES = RTP_HEADER_BYTES + PCM_BYTES_20MS + 8

TETRA_SAMPLES_30MS = 240
TETRA_SAMPLES_60MS = TETRA_SAMPLES_30MS * 2
TETRA_BITS_30MS = 137
TETRA_BITS_60MS = TETRA_BITS_30MS * 2
TETRA_CODED_BYTES = 18
TETRA_PACKED_BYTES = 35
TETRA_STE_BYTES = 36

ARS_REGISTRATION_RE = re.compile(
    r"recognized Motorola SCEP ARS registration, llId = (\d+)"
)
ARS_REFRESH_RE = re.compile(
    r"(?:accepted|recognized) Motorola ARS refresh.*llId = (\d+)"
)
ARS_DISCONNECT_RE = re.compile(
    r"(?:DISCONNECT|deregistration).*llId = (\d+)", re.IGNORECASE
)
ARS_SESSION_RESET_MARKER = "Motorola LRRP Initial Delay:"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uuid_text(wire: bytes) -> str:
    try:
        return str(uuid.UUID(bytes_le=wire))
    except Exception:
        return wire.hex()


def get_packed_bit(data: bytes, bit_index: int) -> int:
    return (data[bit_index // 8] >> (7 - (bit_index % 8))) & 1


def set_packed_bit(data: bytearray, bit_index: int, value: int) -> None:
    if value & 1:
        data[bit_index // 8] |= 1 << (7 - (bit_index % 8))


def split_tmd_block(data: bytes) -> tuple[bytes, bytes]:
    if len(data) == TETRA_STE_BYTES:
        packed = data[1:]
    elif len(data) == TETRA_PACKED_BYTES:
        packed = data
    else:
        raise ValueError(f"invalid TETRA block length {len(data)}")

    frames = [bytearray(TETRA_CODED_BYTES), bytearray(TETRA_CODED_BYTES)]
    for bit_index in range(TETRA_BITS_60MS):
        frame_index = bit_index // TETRA_BITS_30MS
        frame_bit = bit_index % TETRA_BITS_30MS
        set_packed_bit(frames[frame_index], frame_bit, get_packed_bit(packed, bit_index))
    return bytes(frames[0]), bytes(frames[1])


def join_tmd_block(frame_a: bytes, frame_b: bytes) -> bytes:
    if len(frame_a) != TETRA_CODED_BYTES or len(frame_b) != TETRA_CODED_BYTES:
        raise ValueError("a TETRA codec frame must contain 18 bytes")
    packed = bytearray(TETRA_PACKED_BYTES)
    for bit_index in range(TETRA_BITS_60MS):
        frame = frame_a if bit_index < TETRA_BITS_30MS else frame_b
        frame_bit = bit_index % TETRA_BITS_30MS
        set_packed_bit(packed, bit_index, get_packed_bit(frame, frame_bit))
    return b"\x00" + bytes(packed)


def scale_pcm(samples: list[int], gain: float, peak_limit: int) -> list[int]:
    if gain == 1.0 and all(-peak_limit <= value <= peak_limit for value in samples):
        return samples
    output: list[int] = []
    for value in samples:
        adjusted = int(round(value * gain))
        output.append(max(-peak_limit, min(peak_limit, adjusted)))
    return output


class CodecLibrary:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lib = ctypes.CDLL(str(path))
        self.lib.tetra_encoder_create.restype = ctypes.c_void_p
        self.lib.tetra_decoder_create.restype = ctypes.c_void_p
        self.lib.tetra_codec_destroy.argtypes = [ctypes.c_void_p]
        self.lib.tetra_encode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.POINTER(ctypes.c_uint8),
        ]
        self.lib.tetra_decode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
        ]

    def encoder(self) -> "CodecHandle":
        return CodecHandle(self, self.lib.tetra_encoder_create(), True)

    def decoder(self) -> "CodecHandle":
        return CodecHandle(self, self.lib.tetra_decoder_create(), False)


class CodecHandle:
    def __init__(self, owner: CodecLibrary, pointer: int, encoder: bool) -> None:
        if not pointer:
            raise RuntimeError("tetra-codec allocation failed")
        self.owner = owner
        self.pointer = ctypes.c_void_p(pointer)
        self.is_encoder = encoder
        self.closed = False

    def encode(self, samples: list[int]) -> bytes:
        if not self.is_encoder or len(samples) != TETRA_SAMPLES_30MS:
            raise ValueError("tetra_encode requires 240 PCM samples")
        pcm = (ctypes.c_int16 * TETRA_SAMPLES_30MS)(*samples)
        coded = (ctypes.c_uint8 * TETRA_CODED_BYTES)()
        self.owner.lib.tetra_encode(self.pointer, pcm, coded)
        return bytes(coded)

    def decode(self, coded_bytes: bytes, bad_frame: bool = False) -> list[int]:
        if self.is_encoder or len(coded_bytes) != TETRA_CODED_BYTES:
            raise ValueError("tetra_decode requires one 18-byte codec frame")
        coded = (ctypes.c_uint8 * TETRA_CODED_BYTES).from_buffer_copy(coded_bytes)
        pcm = (ctypes.c_int16 * TETRA_SAMPLES_30MS)()
        self.owner.lib.tetra_decode(self.pointer, coded, pcm, 1 if bad_frame else 0)
        return list(pcm)

    def close(self) -> None:
        if not self.closed:
            self.owner.lib.tetra_codec_destroy(self.pointer)
            self.closed = True

    def __enter__(self) -> "CodecHandle":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def build_subscriber_message(message_type: int, issi: int, groups: list[int] | None = None) -> bytes:
    now_ns = time.time_ns()
    payload = struct.pack(
        "<BBIQI",
        BREW_CLASS_SUBSCRIBER,
        message_type,
        issi,
        now_ns // 1_000_000_000,
        now_ns % 1_000_000_000,
    )
    if groups:
        payload += b"".join(struct.pack("<I", group) for group in groups)
    return payload


def build_group_tx(call_uuid: bytes, source: int, destination: int) -> bytes:
    return (
        bytes((BREW_CLASS_CALL_CONTROL, CALL_STATE_GROUP_TX))
        + call_uuid
        + struct.pack("<IIBBH", source, destination, 0, 0, 0)
    )


def build_group_idle(call_uuid: bytes, cause: int = 0) -> bytes:
    return bytes((BREW_CLASS_CALL_CONTROL, CALL_STATE_GROUP_IDLE)) + call_uuid + bytes((cause & 0xFF,))


def build_voice_frame(call_uuid: bytes, ste_data: bytes) -> bytes:
    if len(ste_data) != TETRA_STE_BYTES:
        raise ValueError("BREW voice data must be a 36-byte STE block")
    return (
        bytes((BREW_CLASS_FRAME, FRAME_TYPE_TRAFFIC_CHANNEL))
        + call_uuid
        + struct.pack("<H", len(ste_data) * 8)
        + ste_data
    )


def parse_dvm_rtp(packet: bytes) -> tuple[int, int, int, bool, bytes] | None:
    if len(packet) < RTP_PACKET_BYTES or packet[0] >> 6 != 2:
        return None
    if packet[1] & 0x7F:
        return None
    sequence = struct.unpack_from("!H", packet, 2)[0]
    marker = bool(packet[1] & 0x80)
    pcm = packet[RTP_HEADER_BYTES:RTP_HEADER_BYTES + PCM_BYTES_20MS]
    destination, source = struct.unpack_from("!II", packet, RTP_HEADER_BYTES + PCM_BYTES_20MS)
    return source, destination, sequence, marker, pcm


def build_dvm_rtp(
    pcm: bytes,
    source: int,
    destination: int,
    sequence: int,
    timestamp: int,
    ssrc: int,
    marker: bool,
) -> bytes:
    if len(pcm) != PCM_BYTES_20MS:
        raise ValueError("DVM RTP payload must contain 320 bytes of PCM")
    second = 0x80 if marker else 0x00
    return (
        struct.pack("!BBHII", 0x80, second, sequence & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc)
        + pcm
        + struct.pack("!II", destination, source)
    )


class AtomicStatus:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {
            "mode": "tetrapack-brew-audio",
            "startedAt": utc_now(),
            "connected": False,
            "registered": False,
            "affiliated": False,
            "activeUplink": None,
            "activeDownlink": None,
            "lastError": None,
            "counters": {},
        }

    def set(self, **values: Any) -> None:
        with self.lock:
            self.data.update(values)

    def increment(self, key: str, amount: int = 1) -> None:
        with self.lock:
            counters = self.data.setdefault("counters", {})
            counters[key] = int(counters.get(key, 0)) + amount

    def write(self) -> None:
        with self.lock:
            snapshot = dict(self.data)
            snapshot["updatedAt"] = utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)


@dataclass(frozen=True)
class PcmInputConfig:
    address: str
    port: int


@dataclass(frozen=True)
class PcmOutputConfig:
    address: str
    port: int


@dataclass
class AudioConfig:
    enabled: bool
    existing_brew_config: Path
    existing_brew_module: Path
    quantarbridge_config: Path
    codec_library: Path
    status_file: Path
    log_file: Path
    dynamic_state_file: Path
    observed_issis_file: Path
    dvmhost_log_dir: Path
    local_issis: list[int]
    p25_to_brew: dict[int, int]
    brew_to_p25: dict[int, int]
    static_brew_groups: set[int]
    dynamic_timeout_seconds: int
    disconnect_talkgroup: int
    pcm_input: PcmInputConfig
    pcm_output: PcmOutputConfig
    sms_command_dir: Path | None = None
    jitter_frames: int = 4
    rebuffer_frames: int = 2
    uplink_initial_frames: int = 6
    uplink_inactivity_ms: int = 420
    uplink_gain: float = 2.0
    downlink_gain: float = 1.0
    uplink_peak_limit: int = 24000
    downlink_peak_limit: int = 24000
    user_agent: str = "quantarbridge-brew-audio/1"

    @classmethod
    def load(cls, path: Path) -> "AudioConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("BREW audio config must be a JSON object")
        if yaml is None:
            raise RuntimeError("PyYAML is required for the BREW audio service")

        def resolve(value: str) -> Path:
            candidate = Path(value).expanduser()
            return candidate if candidate.is_absolute() else (path.parent / candidate).resolve()

        quantarbridge_config = resolve(
            str(raw.get("quantarbridgeConfig", path.parent / "quantarbridge.yml"))
        )
        runtime = yaml.safe_load(quantarbridge_config.read_text(encoding="utf-8")) or {}
        routing = runtime.get("routing", {}) or {}
        mappings = routing.get("talkgroupMappings", raw.get("talkgroupMappings", []))
        p25_to_brew: dict[int, int] = {}
        brew_to_p25: dict[int, int] = {}
        for item in mappings:
            if not isinstance(item, dict):
                raise ValueError("talkgroupMappings entries must be objects")
            p25 = int(item["p25"])
            brew = int(item.get("brew", item.get("brandmeister", 0)))
            if not 1 <= p25 <= 0xFFFFFF or not 1 <= brew <= 0xFFFFFF:
                raise ValueError("talkgroupMappings values must be between 1 and 16777215")
            if p25 in p25_to_brew or brew in brew_to_p25:
                raise ValueError("talkgroupMappings must be unique in both directions")
            p25_to_brew[p25] = brew
            brew_to_p25[brew] = p25

        static_groups = {
            int(value)
            for value in routing.get("staticTalkgroups", raw.get("staticTalkgroups", []))
        }
        if any(group < 1 or group > 0xFFFFFF for group in static_groups):
            raise ValueError("staticTalkgroups values must be between 1 and 16777215")
        peak_limit = max(1000, min(32767, int(raw.get("pcmPeakLimit", 24000))))
        local_issis = sorted({int(value) for value in raw.get("localIssis", [])})
        if any(issi < 1 or issi > 0xFFFFFF for issi in local_issis):
            raise ValueError("localIssis values must be between 1 and 16777215")
        disconnect_talkgroup = int(
            routing.get("disconnectTalkgroup", raw.get("disconnectTalkgroup", 4000))
        )
        if not 1 <= disconnect_talkgroup <= 0xFFFFFF:
            raise ValueError("disconnectTalkgroup must be between 1 and 16777215")
        return cls(
            enabled=bool(raw.get("enabled", True)),
            existing_brew_config=resolve(str(raw["existingBrewConfig"])),
            existing_brew_module=resolve(str(raw["existingBrewModule"])),
            quantarbridge_config=quantarbridge_config,
            codec_library=resolve(str(raw["codecLibrary"])),
            status_file=resolve(str(raw["statusFile"])),
            log_file=resolve(str(raw["logFile"])),
            dynamic_state_file=resolve(
                str(raw.get("dynamicStateFile", quantarbridge_config.parent / "dynamic_routes.state"))
            ),
            observed_issis_file=resolve(
                str(raw.get("observedIssisFile", quantarbridge_config.parent / "brew-audio-observed-issis.json"))
            ),
            dvmhost_log_dir=resolve(
                str(raw.get("dvmhostLogDir", quantarbridge_config.parent / "log"))
            ),
            local_issis=local_issis,
            p25_to_brew=p25_to_brew,
            brew_to_p25=brew_to_p25,
            static_brew_groups=static_groups,
            dynamic_timeout_seconds=max(
                10,
                int(routing.get("dynamicTimeoutSeconds", raw.get("dynamicTimeoutSeconds", 600))),
            ),
            disconnect_talkgroup=disconnect_talkgroup,
            pcm_input=PcmInputConfig(
                str(raw["p25PcmInput"].get("address", "127.0.0.1")),
                int(raw["p25PcmInput"]["port"]),
            ),
            pcm_output=PcmOutputConfig(
                str(raw["p25PcmOutput"].get("address", "127.0.0.1")),
                int(raw["p25PcmOutput"]["port"]),
            ),
            sms_command_dir=(
                resolve(str(raw["smsCommandDir"]))
                if raw.get("smsCommandDir")
                else None
            ),
            jitter_frames=max(2, int(raw.get("jitterFrames", 4))),
            rebuffer_frames=max(1, int(raw.get("rebufferFrames", 2))),
            uplink_initial_frames=max(3, int(raw.get("uplinkInitialFrames", 6))),
            uplink_inactivity_ms=max(250, int(raw.get("uplinkInactivityMs", 420))),
            uplink_gain=float(raw.get("uplinkGain", 2.0)),
            downlink_gain=float(raw.get("downlinkGain", 1.0)),
            uplink_peak_limit=max(
                1000, min(32767, int(raw.get("uplinkPeakLimit", peak_limit)))
            ),
            downlink_peak_limit=max(
                1000, min(32767, int(raw.get("downlinkPeakLimit", peak_limit)))
            ),
            user_agent=str(raw.get("userAgent", "quantarbridge-brew-audio/1")),
        )


@dataclass
class DynamicRoute:
    last_active_monotonic: float
    last_active_epoch: float


class TalkgroupRouter:
    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        self.dynamic: dict[int, DynamicRoute] = {}

    def brew_for_p25(self, p25_talkgroup: int) -> int | None:
        if p25_talkgroup == self.config.disconnect_talkgroup:
            return None
        return self.config.p25_to_brew.get(p25_talkgroup, p25_talkgroup)

    def p25_for_brew(self, brew_talkgroup: int) -> int | None:
        if not self.is_active(brew_talkgroup):
            return None
        mapped = self.config.brew_to_p25.get(brew_talkgroup)
        if mapped is not None:
            return mapped
        return brew_talkgroup

    def activate(self, brew_talkgroup: int, now_monotonic: float, now_epoch: float) -> bool:
        if self.is_static(brew_talkgroup):
            return False
        created = brew_talkgroup not in self.dynamic
        self.dynamic[brew_talkgroup] = DynamicRoute(now_monotonic, now_epoch)
        return created

    def restore(self, brew_talkgroup: int, last_active_epoch: float, now_epoch: float) -> bool:
        age = max(0.0, now_epoch - last_active_epoch)
        if age >= self.config.dynamic_timeout_seconds or self.is_static(brew_talkgroup):
            return False
        self.dynamic[brew_talkgroup] = DynamicRoute(
            last_active_monotonic=time.monotonic() - age,
            last_active_epoch=last_active_epoch,
        )
        return True

    def expire(self, now_monotonic: float) -> list[int]:
        expired = [
            talkgroup
            for talkgroup, route in self.dynamic.items()
            if now_monotonic - route.last_active_monotonic > self.config.dynamic_timeout_seconds
        ]
        for talkgroup in expired:
            self.dynamic.pop(talkgroup, None)
        return sorted(expired)

    def clear(self) -> list[int]:
        cleared = sorted(self.dynamic)
        self.dynamic.clear()
        return cleared

    def groups(self) -> list[int]:
        return sorted(self.config.static_brew_groups | set(self.dynamic))

    def is_static(self, brew_talkgroup: int) -> bool:
        return brew_talkgroup in self.config.static_brew_groups

    def is_active(self, brew_talkgroup: int) -> bool:
        return self.is_static(brew_talkgroup) or brew_talkgroup in self.dynamic

    def snapshot(self) -> list[dict[str, int | float | str]]:
        return [
            {
                "talkgroup": talkgroup,
                "lastActiveEpoch": route.last_active_epoch,
                "expiresEpoch": route.last_active_epoch + self.config.dynamic_timeout_seconds,
            }
            for talkgroup, route in sorted(self.dynamic.items())
        ]


def read_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return "unknown"


def load_dynamic_routes(path: Path, router: TalkgroupRouter, now_epoch: float | None = None) -> list[int]:
    if not path.exists():
        return []
    restored: list[int] = []
    current_epoch = time.time() if now_epoch is None else now_epoch
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) != 2:
            continue
        try:
            talkgroup = int(fields[0])
            last_active_epoch = float(fields[1])
        except ValueError:
            continue
        if 1 <= talkgroup <= 0xFFFFFF and router.restore(talkgroup, last_active_epoch, current_epoch):
            restored.append(talkgroup)
    return sorted(restored)


def save_dynamic_routes(path: Path, router: TalkgroupRouter) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    lines = [f"# boot_id={read_boot_id()}"]
    lines.extend(
        f"{talkgroup} {route.last_active_epoch:.6f}"
        for talkgroup, route in sorted(router.dynamic.items())
    )
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def load_observed_issis(path: Path) -> set[int]:
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    values = raw.get("issis", []) if isinstance(raw, dict) else raw
    if not isinstance(values, list):
        return set()
    result: set[int] = set()
    for value in values:
        try:
            issi = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= issi <= 0xFFFFFF:
            result.add(issi)
    return result


def save_observed_issis(path: Path, issis: set[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"issis": sorted(issis)}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def apply_ars_log_line(issis: set[int], line: str) -> tuple[str, int | None]:
    if ARS_SESSION_RESET_MARKER in line:
        issis.clear()
        return "reset", None
    match = ARS_REGISTRATION_RE.search(line) or ARS_REFRESH_RE.search(line)
    if match:
        issi = int(match.group(1))
        if 1 <= issi <= 0xFFFFFF:
            issis.add(issi)
            return "register", issi
    match = ARS_DISCONNECT_RE.search(line)
    if match:
        issi = int(match.group(1))
        issis.discard(issi)
        return "deregister", issi
    return "none", None


def discover_registered_issis(log_dir: Path) -> tuple[set[int], Path | None, int]:
    try:
        files = sorted(log_dir.glob("dvmhost-20??-??-??.log"), key=lambda path: path.name)
    except OSError:
        return set(), None, 0
    if not files:
        return set(), None, 0
    path = files[-1]
    issis: set[int] = set()
    offset = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                apply_ars_log_line(issis, line)
            offset = stream.tell()
    except OSError:
        return set(), None, 0
    return issis, path, offset


def discover_affiliation_anchor(log_dir: Path) -> int | None:
    try:
        files = sorted(log_dir.glob("dvmhost-20??-??-??.log"), key=lambda path: path.name)
    except OSError:
        return None
    for path in reversed(files[-2:]):
        latest_issi: int | None = None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    match = ARS_REGISTRATION_RE.search(line) or ARS_REFRESH_RE.search(line)
                    if match:
                        candidate = int(match.group(1))
                        if 1 <= candidate <= 0xFFFFFF:
                            latest_issi = candidate
        except OSError:
            continue
        if latest_issi is not None:
            return latest_issi
    return None


def load_brew_dependencies(config: AudioConfig) -> tuple[Any, Any, Any, Any, Any]:
    spec = importlib.util.spec_from_file_location("quantarbridge_brew_runtime", config.existing_brew_module)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load BREW module {config.existing_brew_module}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    bridge_config = module.load_config(config.existing_brew_config)
    brew = bridge_config.brew
    if not brew.enabled or not brew.username or not brew.password:
        raise RuntimeError("BREW is disabled or its protected runtime credentials are unavailable")
    if module.requests is None or module.HTTPDigestAuth is None or module.websocket is None:
        raise RuntimeError("requests and websocket-client are required")
    return brew, module, module.requests, module.HTTPDigestAuth, module.websocket


class BrewTransport:
    def __init__(
        self,
        config: AudioConfig,
        status: AtomicStatus,
        on_binary: Callable[[bytes], None],
        on_connected: Callable[[], None],
        stop_event: threading.Event,
    ) -> None:
        self.config = config
        self.status = status
        self.on_binary = on_binary
        self.on_connected = on_connected
        self.stop_event = stop_event
        (
            self.brew,
            self.brew_module,
            self.requests,
            self.digest_auth,
            self.websocket,
        ) = load_brew_dependencies(config)
        self.socket: Any | None = None
        self.send_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="brew-transport", daemon=True)
        self.connected = threading.Event()

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float = 3.0) -> None:
        self.thread.join(timeout)

    def send(self, payload: bytes) -> bool:
        return self.send_many([payload])

    def send_many(self, payloads: list[bytes]) -> bool:
        with self.send_lock:
            ws = self.socket
            if ws is None or not self.connected.is_set():
                return False
            try:
                for payload in payloads:
                    ws.send_binary(payload)
                return True
            except Exception as exc:
                logging.warning("BREW send failed: %s", type(exc).__name__)
                self.status.set(lastError=f"brew_send:{type(exc).__name__}")
                try:
                    ws.close()
                except Exception:
                    pass
                return False

    def close_socket(self) -> None:
        with self.send_lock:
            ws = self.socket
            self.socket = None
            self.connected.clear()
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def _discover_endpoint(self) -> str:
        base = self.brew.base_url.rstrip("/")
        headers = {
            "User-Agent": self.config.user_agent,
            "X-Brew-Mode": "Basestation",
            "X-Brew-Version": "1",
        }
        response = self.requests.get(
            f"{base}/brew/",
            headers=headers,
            auth=self.digest_auth(self.brew.username, self.brew.password),
            timeout=self.brew.request_timeout_seconds,
        )
        response.raise_for_status()
        endpoint_path = response.text.strip()
        if not endpoint_path.startswith("/"):
            endpoint_path = "/" + endpoint_path
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):] + endpoint_path
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):] + endpoint_path
        raise ValueError("unsupported BREW base URL")

    def _run(self) -> None:
        delay = 1.0
        while not self.stop_event.is_set():
            try:
                endpoint = self._discover_endpoint()
                ws = self.websocket.create_connection(
                    endpoint,
                    timeout=2.0,
                    subprotocols=["brew"],
                    header=[
                        f"User-Agent: {self.config.user_agent}",
                        "X-Brew-Mode: Basestation",
                        "X-Brew-Version: 1",
                    ],
                    enable_multithread=True,
                )
                ws.settimeout(0.5)
                with self.send_lock:
                    self.socket = ws
                    self.connected.set()
                self.status.set(connected=True, connectedAt=utc_now(), lastError=None)
                logging.info("BREW WebSocket connected in Basestation mode")
                delay = 1.0
                self.on_connected()
                last_ping = time.monotonic()

                while not self.stop_event.is_set():
                    try:
                        frame = ws.recv()
                    except self.websocket.WebSocketTimeoutException:
                        if time.monotonic() - last_ping >= 20.0:
                            with self.send_lock:
                                if self.socket is ws:
                                    ws.ping()
                            last_ping = time.monotonic()
                        continue
                    if frame is None:
                        raise RuntimeError("BREW WebSocket closed")
                    if isinstance(frame, bytes):
                        self.on_binary(frame)
            except Exception as exc:
                if not self.stop_event.is_set():
                    logging.warning("BREW connection unavailable: %s", type(exc).__name__)
                    self.status.set(
                        connected=False,
                        registered=False,
                        affiliated=False,
                        lastError=f"brew_connection:{type(exc).__name__}",
                    )
            finally:
                self.close_socket()
                self.status.set(connected=False, registered=False, affiliated=False)
            if not self.stop_event.wait(delay):
                delay = min(15.0, delay * 2.0)


@dataclass
class PcmFrame:
    source: int
    destination: int
    sequence: int
    marker: bool
    pcm: bytes
    received_at: float


@dataclass
class UplinkCall:
    source: int
    p25_destination: int
    brew_destination: int
    call_uuid: bytes
    encoder: CodecHandle
    pcm_samples: list[int] = field(default_factory=list)
    voice_frames: int = 0


@dataclass
class DownlinkCall:
    call_uuid: bytes
    source: int
    brew_destination: int
    p25_destination: int
    decoder: CodecHandle
    frames: collections.deque[bytes] = field(default_factory=collections.deque)
    created_at: float = field(default_factory=time.monotonic)
    last_frame_at: float = field(default_factory=time.monotonic)
    ended: bool = False
    started: bool = False
    underruns: int = 0


@dataclass
class PendingSds:
    source: int
    destination: int
    received_at: float = field(default_factory=time.monotonic)


class BrewAudioBridge:
    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self.status = AtomicStatus(config.status_file)
        self.router = TalkgroupRouter(config)
        self.router_lock = threading.Lock()
        restored = load_dynamic_routes(config.dynamic_state_file, self.router)
        self.local_issis_lock = threading.Lock()
        self.configured_local_issis = set(config.local_issis)
        self.affiliation_anchor_issis = set(self.configured_local_issis)
        discovered_issis, registration_log_path, registration_log_offset = (
            discover_registered_issis(config.dvmhost_log_dir)
        )
        observed_issis = load_observed_issis(config.observed_issis_file)
        self.local_issis = self.configured_local_issis | discovered_issis
        if registration_log_path is None:
            self.local_issis |= observed_issis
        if not self.local_issis:
            affiliation_anchor = discover_affiliation_anchor(config.dvmhost_log_dir)
            if affiliation_anchor is not None:
                self.local_issis.add(affiliation_anchor)
                self.affiliation_anchor_issis.add(affiliation_anchor)
        self.registration_log_path = registration_log_path
        self.registration_log_offset = registration_log_offset
        self.codec = CodecLibrary(config.codec_library)
        self.transport = BrewTransport(
            config,
            self.status,
            self._on_brew_binary,
            self._on_brew_connected,
            self.stop_event,
        )
        try:
            configured_issi = int(self.transport.brew.username)
        except (TypeError, ValueError):
            configured_issi = 0
        if not self.local_issis and 1 <= configured_issi <= 0xFFFFFF:
            self.local_issis.add(configured_issi)
        if self.local_issis:
            save_observed_issis(config.observed_issis_file, self.local_issis)
        if restored:
            logging.info("Restored %u dynamic BREW group(s): %s", len(restored), restored)

        self.uplink_queue: collections.deque[PcmFrame] = collections.deque()
        self.uplink_condition = threading.Condition()
        self.last_p25_rx = 0.0
        self.active_uplink: UplinkCall | None = None
        self.owned_uuids: dict[bytes, float] = {}

        self.downlink_lock = threading.Lock()
        self.downlink_calls: dict[bytes, DownlinkCall] = {}
        self.ignored_downlink_uuids: set[bytes] = set()
        self.active_downlink_uuid: bytes | None = None
        self.pending_sds: dict[bytes, PendingSds] = {}

        self.input_socket: socket.socket | None = None
        self.output_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp_sequence = 0
        self.rtp_timestamp = random.getrandbits(32)
        self.rtp_ssrc = 0x42524557
        self.last_disconnect_at = 0.0
        try:
            self.routing_config_mtime_ns = config.quantarbridge_config.stat().st_mtime_ns
        except OSError:
            self.routing_config_mtime_ns = -1

        self.threads = [
            threading.Thread(target=self._udp_receive_loop, name="p25-pcm-receive", daemon=True),
            threading.Thread(target=self._uplink_loop, name="p25-to-brew", daemon=True),
            threading.Thread(target=self._downlink_loop, name="brew-to-p25", daemon=True),
            threading.Thread(target=self._registration_loop, name="ars-registration-monitor", daemon=True),
            threading.Thread(target=self._routing_loop, name="talkgroup-routing", daemon=True),
            threading.Thread(target=self._status_loop, name="status-writer", daemon=True),
        ]
        if self.config.sms_command_dir is not None:
            self.threads.append(
                threading.Thread(target=self._sms_command_loop, name="brew-sms-command", daemon=True)
            )

    def start(self) -> None:
        logging.info(
            "Starting BREW audio bridge, P25 PCM input=%s:%u output=%s:%u",
            self.config.pcm_input.address,
            self.config.pcm_input.port,
            self.config.pcm_output.address,
            self.config.pcm_output.port,
        )
        self.status.set(
            localIssis=sorted(self.local_issis),
            staticTalkgroups=sorted(self.config.static_brew_groups),
            dynamicTalkgroups=self.router.snapshot(),
            talkgroupMappings=[
                {"p25": p25, "brew": brew} for p25, brew in sorted(self.config.p25_to_brew.items())
            ],
            p25PcmInput=f"{self.config.pcm_input.address}:{self.config.pcm_input.port}",
            p25PcmOutput=f"{self.config.pcm_output.address}:{self.config.pcm_output.port}",
        )
        self.transport.start()
        for thread in self.threads:
            thread.start()

    def request_stop(self) -> None:
        self.stop_event.set()
        with self.uplink_condition:
            self.uplink_condition.notify_all()
        if self.input_socket is not None:
            try:
                self.input_socket.close()
            except Exception:
                pass

    def stop(self) -> None:
        active = self.active_uplink
        if active is not None:
            self._finish_uplink(active)
            self.active_uplink = None
        if self.transport.connected.is_set():
            with self.router_lock:
                groups = self.router.groups()
            with self.local_issis_lock:
                local_issis = sorted(self.local_issis)
            for issi in local_issis:
                self.transport.send(build_subscriber_message(SUBSCRIBER_DEAFFILIATE, issi, groups))
                self.transport.send(build_subscriber_message(SUBSCRIBER_DEREGISTER, issi))
            time.sleep(0.15)
        self.request_stop()
        self.transport.close_socket()
        self.transport.join()
        for thread in self.threads:
            thread.join(2.0)
        with self.downlink_lock:
            for call in self.downlink_calls.values():
                call.decoder.close()
            self.downlink_calls.clear()
        self.output_socket.close()
        self.status.set(stoppedAt=utc_now(), connected=False, registered=False, affiliated=False)
        self.status.write()
        logging.info("BREW audio bridge stopped")

    def _on_brew_connected(self) -> None:
        with self.router_lock:
            groups = self.router.groups()
        with self.local_issis_lock:
            local_issis = sorted(self.local_issis)
        register_ok = True
        affiliate_ok = True
        for issi in local_issis:
            register_ok &= self.transport.send(build_subscriber_message(SUBSCRIBER_REGISTER, issi))
        time.sleep(0.1)
        for issi in local_issis:
            affiliate_ok &= self.transport.send(build_subscriber_message(SUBSCRIBER_AFFILIATE, issi, groups))
        self.status.set(registered=bool(register_ok), affiliated=bool(affiliate_ok))
        if register_ok and affiliate_ok:
            logging.info("Registered %u local ISSI(s) and affiliated %u BREW group(s)", len(local_issis), len(groups))

    def _ensure_local_issi(self, issi: int) -> None:
        if not 1 <= issi <= 0xFFFFFF:
            return
        with self.local_issis_lock:
            if issi in self.local_issis:
                return
            self.local_issis.add(issi)
            local_issis = set(self.local_issis)
        save_observed_issis(self.config.observed_issis_file, local_issis)
        self.status.set(localIssis=sorted(local_issis))
        logging.info("Learned local P25 ISSI %u from RF uplink", issi)
        if self.transport.connected.is_set():
            with self.router_lock:
                groups = self.router.groups()
            self.transport.send(build_subscriber_message(SUBSCRIBER_REGISTER, issi))
            self.transport.send(build_subscriber_message(SUBSCRIBER_AFFILIATE, issi, groups))

    def _remove_local_issi(self, issi: int) -> None:
        if issi in self.affiliation_anchor_issis:
            return
        with self.local_issis_lock:
            if issi not in self.local_issis:
                return
            self.local_issis.remove(issi)
            local_issis = set(self.local_issis)
        if self.transport.connected.is_set():
            with self.router_lock:
                groups = self.router.groups()
            self.transport.send(build_subscriber_message(SUBSCRIBER_DEAFFILIATE, issi, groups))
            self.transport.send(build_subscriber_message(SUBSCRIBER_DEREGISTER, issi))
        save_observed_issis(self.config.observed_issis_file, local_issis)
        self.status.set(localIssis=sorted(local_issis))
        logging.info("Removed local P25 ISSI %u after ARS deregistration", issi)

    def _reset_local_ars_issis(self) -> None:
        with self.local_issis_lock:
            removable = sorted(self.local_issis - self.affiliation_anchor_issis)
        for issi in removable:
            self._remove_local_issi(issi)
        if removable:
            logging.info("Cleared %u learned ISSI(s) after DVMHost session reset", len(removable))

    def _process_ars_log_line(self, line: str) -> None:
        scratch: set[int] = set()
        action, issi = apply_ars_log_line(scratch, line)
        if action == "register" and issi is not None:
            self._ensure_local_issi(issi)
        elif action == "deregister" and issi is not None:
            self._remove_local_issi(issi)
        elif action == "reset":
            self._reset_local_ars_issis()

    def _registration_loop(self) -> None:
        while not self.stop_event.wait(0.5):
            try:
                files = sorted(
                    self.config.dvmhost_log_dir.glob("dvmhost-20??-??-??.log"),
                    key=lambda path: path.name,
                )
                latest = files[-1] if files else None
                if latest is None:
                    continue
                if latest != self.registration_log_path:
                    self.registration_log_path = latest
                    self.registration_log_offset = 0
                size = latest.stat().st_size
                if size < self.registration_log_offset:
                    self.registration_log_offset = 0
                if size == self.registration_log_offset:
                    continue
                with latest.open("r", encoding="utf-8", errors="replace") as stream:
                    stream.seek(self.registration_log_offset)
                    for line in stream:
                        self._process_ars_log_line(line)
                    self.registration_log_offset = stream.tell()
            except OSError as exc:
                logging.debug("ARS registration log follow failed: %s", exc)

    def _process_sms_command(self, path: Path) -> bool:
        if not self.transport.connected.is_set():
            return False
        command = json.loads(path.read_text(encoding="utf-8"))
        source = int(command["sourceRid"])
        target = int(command["targetRid"])
        text = str(command["text"]).strip()
        if not 1 <= source <= 0xFFFFFF or not 1 <= target <= 0xFFFFFF:
            raise ValueError("SMS sourceRid and targetRid must fit in 24 bits")
        if not text:
            raise ValueError("SMS text must not be empty")

        self._ensure_local_issi(source)
        session_id = uuid.uuid4()
        message_reference = int(time.time_ns() // 1_000_000) & 0xFF
        payload = self.transport.brew_module.build_text_sds_type4_pdu(
            text, message_reference
        )
        frames = [
            self.transport.brew_module.build_brew_short_transfer(
                session_id, source, target
            ),
            self.transport.brew_module.build_brew_sds_transfer(session_id, payload),
        ]
        release = self.transport.brew_module.build_brew_call_release(
            session_id, cause=0
        )
        if release is not None:
            frames.append(release)
        if not self.transport.send_many(frames):
            return False

        assert self.config.sms_command_dir is not None
        result_dir = self.config.sms_command_dir.parent / "brew-audio-results"
        result_dir.mkdir(parents=True, exist_ok=True)
        result_path = result_dir / path.name
        temporary = result_path.with_suffix(result_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    **command,
                    "status": "sent",
                    "sentAt": utc_now(),
                    "sessionId": str(session_id),
                    "messageReference": message_reference,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(result_path)
        path.unlink(missing_ok=True)
        self.status.increment("brewSmsCommandsSent")
        logging.info("BREW SMS sent on shared session src=%u dst=%u", source, target)
        return True

    def _sms_command_loop(self) -> None:
        assert self.config.sms_command_dir is not None
        command_dir = self.config.sms_command_dir
        error_dir = command_dir.parent / "brew-audio-errors"
        command_dir.mkdir(parents=True, exist_ok=True)
        error_dir.mkdir(parents=True, exist_ok=True)
        while not self.stop_event.wait(0.1):
            for path in sorted(command_dir.glob("*.json")):
                try:
                    if not self._process_sms_command(path):
                        break
                except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    destination = error_dir / path.name
                    if path.exists():
                        path.replace(destination)
                    error_path = error_dir / f"{path.stem}.error.txt"
                    error_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
                    self.status.increment("invalidBrewSmsCommands")
                    logging.warning("Rejected BREW SMS command %s: %s", path.name, exc)

    def _send_affiliation(self, message_type: int, groups: list[int]) -> None:
        if not groups or not self.transport.connected.is_set():
            return
        with self.local_issis_lock:
            local_issis = sorted(self.local_issis)
        for issi in local_issis:
            self.transport.send(build_subscriber_message(message_type, issi, groups))

    def _on_brew_binary(self, frame: bytes) -> None:
        if len(frame) < 2:
            return
        message_class, message_type = frame[0], frame[1]
        if message_class == BREW_CLASS_SUBSCRIBER:
            with self.local_issis_lock:
                local_issis = set(self.local_issis)
            if len(frame) >= 6 and struct.unpack_from("<I", frame, 2)[0] in local_issis:
                self.status.increment("localSubscriberEvents")
            return
        if message_class == BREW_CLASS_ERROR:
            self.status.increment("brewServerErrors")
            self.status.set(lastError=f"brew_server_f3_type_{message_type}")
            logging.warning("BREW server returned F3 error type=%u length=%u", message_type, len(frame))
            return
        if message_class == BREW_CLASS_FRAME and message_type == FRAME_TYPE_SDS_REPORT:
            self.status.increment("brewSmsReportsReceived")
            return
        if len(frame) < 18:
            return
        call_uuid = frame[2:18]

        now = time.monotonic()
        self.pending_sds = {
            key: pending
            for key, pending in self.pending_sds.items()
            if now - pending.received_at < 30.0
        }

        if message_class == BREW_CLASS_CALL_CONTROL and message_type == CALL_STATE_SHORT_TRANSFER:
            if len(frame) < 26:
                self.status.increment("invalidBrewSmsHeaders")
                return
            source, destination = struct.unpack_from("<II", frame, 18)
            self.pending_sds[call_uuid] = PendingSds(source, destination, now)
            self.status.increment("brewSmsHeadersReceived")
            logging.info(
                "BREW SDS header received uuid=%s src=%u dst=%u",
                uuid_text(call_uuid),
                source,
                destination,
            )
            return

        if message_class == BREW_CLASS_FRAME and message_type == FRAME_TYPE_SDS_TRANSFER:
            self._handle_inbound_sds(call_uuid, frame)
            return

        self._expire_owned_uuids()
        if message_class == BREW_CLASS_CALL_CONTROL:
            if message_type == CALL_STATE_GROUP_TX and len(frame) >= 30:
                source, destination = struct.unpack_from("<II", frame, 18)
                self._start_or_update_downlink(call_uuid, source, destination)
            elif message_type == CALL_STATE_GROUP_IDLE:
                self._end_downlink(call_uuid)
            return

        if message_class == BREW_CLASS_FRAME and message_type == FRAME_TYPE_TRAFFIC_CHANNEL:
            if len(frame) < 20:
                return
            length_bits = struct.unpack_from("<H", frame, 18)[0]
            payload = frame[20:]
            self._queue_downlink_voice(call_uuid, length_bits, payload)

    def _handle_inbound_sds(self, call_uuid: bytes, frame: bytes) -> None:
        if len(frame) < 20:
            self.status.increment("invalidBrewSmsFrames")
            return
        pending = self.pending_sds.pop(call_uuid, None)
        if pending is None:
            self.status.increment("orphanBrewSmsFrames")
            logging.warning("BREW SDS payload without header uuid=%s", uuid_text(call_uuid))
            return

        with self.local_issis_lock:
            is_local = pending.destination in self.local_issis
        if not is_local:
            self.status.increment("nonLocalBrewSmsFrames")
            logging.info(
                "Ignoring BREW SDS for non-local ISSI uuid=%s src=%u dst=%u",
                uuid_text(call_uuid),
                pending.source,
                pending.destination,
            )
            return

        length_bits = struct.unpack_from("<H", frame, 18)[0]
        payload = frame[20 : 20 + (length_bits + 7) // 8]
        text = self.transport.brew_module.parse_text_sds_type4_pdu(payload, length_bits)
        if not text:
            self.status.increment("unsupportedBrewSmsFrames")
            logging.warning(
                "Unsupported BREW SDS payload uuid=%s src=%u dst=%u bits=%u",
                uuid_text(call_uuid),
                pending.source,
                pending.destination,
                length_bits,
            )
            return

        if self.config.sms_command_dir is None:
            self.status.increment("undeliverableBrewSmsFrames")
            logging.warning("BREW SDS cannot be delivered because smsCommandDir is disabled")
            return
        outbox = self.config.sms_command_dir.parent / "p25-outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        event_id = f"brew-sds-{int(time.time_ns() // 1_000_000)}-{uuid_text(call_uuid)}"
        path = outbox / f"{event_id}.yaml"
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            f"sourceRid: {pending.source}\n"
            f"targetRid: {pending.destination}\n"
            f'textHex: "{text.encode("utf-8").hex()}"\n',
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)

        session_id = uuid.UUID(bytes_le=call_uuid)
        report = self.transport.brew_module.build_brew_sds_report(session_id, status=0)
        self.transport.send(report)
        self.status.increment("brewSmsMessagesDelivered")
        logging.info(
            "BREW SDS queued for P25 uuid=%s src=%u dst=%u chars=%u",
            uuid_text(call_uuid),
            pending.source,
            pending.destination,
            len(text),
        )

    def _start_or_update_downlink(self, call_uuid: bytes, source: int, destination: int) -> None:
        with self.router_lock:
            p25_destination = self.router.p25_for_brew(destination)
        if p25_destination is None:
            self.ignored_downlink_uuids.add(call_uuid)
            self.status.increment("unsubscribedDownlinkCalls")
            logging.info(
                "Ignoring BREW downlink for unsubscribed TG uuid=%s src=%u brew_tg=%u",
                uuid_text(call_uuid),
                source,
                destination,
            )
            return
        with self.local_issis_lock:
            local_issis = set(self.local_issis)
        if call_uuid in self.owned_uuids or (
            source in local_issis and self.active_uplink is not None
        ):
            self.ignored_downlink_uuids.add(call_uuid)
            self.status.increment("suppressedEchoCalls")
            return
        with self.downlink_lock:
            call = self.downlink_calls.get(call_uuid)
            if call is None:
                competing = any(
                    existing.call_uuid != call_uuid and not existing.ended
                    for existing in self.downlink_calls.values()
                )
                if competing:
                    self.ignored_downlink_uuids.add(call_uuid)
                    self.status.increment("downlinkBusyCalls")
                    logging.warning(
                        "Ignoring simultaneous BREW downlink uuid=%s src=%u tg=%u; P25 channel busy",
                        uuid_text(call_uuid),
                        source,
                        destination,
                    )
                    return
                call = DownlinkCall(
                    call_uuid=call_uuid,
                    source=source,
                    brew_destination=destination,
                    p25_destination=p25_destination,
                    decoder=self.codec.decoder(),
                )
                self.downlink_calls[call_uuid] = call
                logging.info(
                    "BREW downlink call start uuid=%s src=%u brew_tg=%u p25_tg=%u",
                    uuid_text(call_uuid),
                    source,
                    destination,
                    p25_destination,
                )
                self.status.increment("downlinkCalls")
            else:
                call.source = source
                call.ended = False

    def _end_downlink(self, call_uuid: bytes) -> None:
        if call_uuid in self.ignored_downlink_uuids:
            self.ignored_downlink_uuids.discard(call_uuid)
            return
        with self.downlink_lock:
            call = self.downlink_calls.get(call_uuid)
            if call is not None:
                call.ended = True
                logging.info("BREW downlink call idle uuid=%s queued=%u", uuid_text(call_uuid), len(call.frames))

    def _queue_downlink_voice(self, call_uuid: bytes, length_bits: int, payload: bytes) -> None:
        if call_uuid in self.owned_uuids or call_uuid in self.ignored_downlink_uuids:
            self.status.increment("suppressedEchoVoiceFrames")
            return
        if len(payload) < TETRA_STE_BYTES:
            self.status.increment("invalidBrewVoiceFrames")
            return
        with self.downlink_lock:
            call = self.downlink_calls.get(call_uuid)
            if call is None:
                self.status.increment("orphanBrewVoiceFrames")
                return
            if len(call.frames) >= 24:
                call.frames.popleft()
                self.status.increment("downlinkJitterDrops")
            call.frames.append(payload[:TETRA_STE_BYTES])
            call.last_frame_at = time.monotonic()
            self.status.increment("brewVoiceFramesReceived")
            if length_bits not in (TETRA_BITS_60MS, TETRA_STE_BYTES * 8):
                self.status.increment("unusualBrewVoiceLengths")

    def _udp_receive_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        sock.bind((self.config.pcm_input.address, self.config.pcm_input.port))
        sock.settimeout(0.2)
        self.input_socket = sock
        while not self.stop_event.is_set():
            try:
                packet, _address = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            parsed = parse_dvm_rtp(packet)
            if parsed is None:
                self.status.increment("invalidP25PcmPackets")
                continue
            source, destination, sequence, marker, pcm = parsed
            if destination == self.config.disconnect_talkgroup:
                if marker and time.monotonic() - self.last_disconnect_at >= 0.5:
                    self.last_disconnect_at = time.monotonic()
                    self._clear_dynamic_routes()
                self.status.increment("disconnectP25PcmPackets")
                continue
            with self.router_lock:
                brew_destination = self.router.brew_for_p25(destination)
            if brew_destination is None:
                self.status.increment("unmappedP25PcmPackets")
                continue
            self._ensure_local_issi(source)
            now = time.monotonic()
            frame = PcmFrame(source, destination, sequence, marker, pcm, now)
            with self.uplink_condition:
                if len(self.uplink_queue) >= 180:
                    self.uplink_queue.popleft()
                    self.status.increment("uplinkQueueDrops")
                self.uplink_queue.append(frame)
                self.last_p25_rx = now
                self.uplink_condition.notify()
            self.status.increment("p25PcmFramesReceived")

    def _uplink_loop(self) -> None:
        next_due = 0.0
        while not self.stop_event.is_set():
            frame: PcmFrame | None = None
            with self.uplink_condition:
                while not self.stop_event.is_set():
                    active = self.active_uplink
                    if self.uplink_queue and (
                        active is not None or len(self.uplink_queue) >= self.config.uplink_initial_frames
                    ):
                        frame = self.uplink_queue.popleft()
                        break
                    if active is not None and not self.uplink_queue:
                        quiet_ms = (time.monotonic() - self.last_p25_rx) * 1000.0
                        if quiet_ms >= self.config.uplink_inactivity_ms:
                            break
                    self.uplink_condition.wait(0.02)

            active = self.active_uplink
            if frame is None:
                if active is not None and (time.monotonic() - self.last_p25_rx) * 1000.0 >= self.config.uplink_inactivity_ms:
                    self._finish_uplink(active)
                    self.active_uplink = None
                    next_due = 0.0
                continue

            if active is None or active.source != frame.source or active.p25_destination != frame.destination:
                if active is not None:
                    self._finish_uplink(active)
                active = self._begin_uplink(frame.source, frame.destination)
                self.active_uplink = active
                next_due = time.monotonic()
                if active is None:
                    continue

            if next_due > 0.0:
                delay = next_due - time.monotonic()
                if delay > 0:
                    self.stop_event.wait(delay)
                if time.monotonic() - next_due > 0.12:
                    next_due = time.monotonic()
            next_due += 0.020

            samples = list(struct.unpack("<160h", frame.pcm))
            active.pcm_samples.extend(samples)
            if len(active.pcm_samples) >= TETRA_SAMPLES_60MS:
                block = active.pcm_samples[:TETRA_SAMPLES_60MS]
                del active.pcm_samples[:TETRA_SAMPLES_60MS]
                self._send_uplink_voice(active, block)

    def _begin_uplink(self, source: int, p25_destination: int) -> UplinkCall | None:
        with self.router_lock:
            brew_destination = self.router.brew_for_p25(p25_destination)
            if brew_destination is None:
                return None
            created = self.router.activate(brew_destination, time.monotonic(), time.time())
            save_dynamic_routes(self.config.dynamic_state_file, self.router)
            dynamic_snapshot = self.router.snapshot()
            dynamic_active = brew_destination in self.router.dynamic
        if dynamic_active:
            logging.info("Updated dynamic TG %u from RF activity", brew_destination)
            self.status.set(dynamicTalkgroups=dynamic_snapshot)
            if created:
                self._send_affiliation(SUBSCRIBER_AFFILIATE, [brew_destination])
        if not self.transport.connected.is_set():
            self.status.increment("uplinkCallsDroppedDisconnected")
            return None
        call_uuid = uuid.uuid4().bytes_le
        call = UplinkCall(
            source=source,
            p25_destination=p25_destination,
            brew_destination=brew_destination,
            call_uuid=call_uuid,
            encoder=self.codec.encoder(),
        )
        self.owned_uuids[call_uuid] = time.monotonic() + 30.0
        if not self.transport.send(build_group_tx(call_uuid, source, brew_destination)):
            call.encoder.close()
            return None
        logging.info(
            "P25 uplink call start uuid=%s src=%u p25_tg=%u brew_tg=%u",
            uuid_text(call_uuid),
            source,
            p25_destination,
            brew_destination,
        )
        self.status.increment("uplinkCalls")
        self.status.set(
            activeUplink={
                "uuid": uuid_text(call_uuid),
                "source": source,
                "p25Talkgroup": p25_destination,
                "brewTalkgroup": brew_destination,
                "startedAtEpoch": time.time(),
            }
        )
        return call

    def _send_uplink_voice(self, call: UplinkCall, samples: list[int]) -> None:
        samples = scale_pcm(samples, self.config.uplink_gain, self.config.uplink_peak_limit)
        coded_a = call.encoder.encode(samples[:TETRA_SAMPLES_30MS])
        coded_b = call.encoder.encode(samples[TETRA_SAMPLES_30MS:])
        ste = join_tmd_block(coded_a, coded_b)
        if self.transport.send(build_voice_frame(call.call_uuid, ste)):
            call.voice_frames += 1
            self.status.increment("brewVoiceFramesSent")
        else:
            self.status.increment("brewVoiceFramesSendFailed")

    def _finish_uplink(self, call: UplinkCall) -> None:
        if call.pcm_samples:
            padded = call.pcm_samples + [0] * (TETRA_SAMPLES_60MS - len(call.pcm_samples))
            self._send_uplink_voice(call, padded[:TETRA_SAMPLES_60MS])
            call.pcm_samples.clear()
        self.transport.send(build_group_idle(call.call_uuid))
        self.owned_uuids[call.call_uuid] = time.monotonic() + 10.0
        call.encoder.close()
        logging.info("P25 uplink call idle uuid=%s frames=%u", uuid_text(call.call_uuid), call.voice_frames)
        self.status.set(activeUplink=None)

    def _downlink_loop(self) -> None:
        next_due = 0.0
        while not self.stop_event.is_set():
            call: DownlinkCall | None = None
            ste: bytes | None = None
            with self.downlink_lock:
                if self.active_downlink_uuid is None:
                    candidates = sorted(self.downlink_calls.values(), key=lambda item: item.created_at)
                    for candidate in candidates:
                        required = self.config.rebuffer_frames if candidate.started else self.config.jitter_frames
                        if len(candidate.frames) >= required or (candidate.ended and candidate.frames):
                            self.active_downlink_uuid = candidate.call_uuid
                            candidate.started = True
                            self.rtp_sequence = 0
                            self.rtp_timestamp = random.getrandbits(32)
                            next_due = time.monotonic()
                            self.status.set(
                                activeDownlink={
                                    "uuid": uuid_text(candidate.call_uuid),
                                    "source": candidate.source,
                                    "brewTalkgroup": candidate.brew_destination,
                                    "p25Talkgroup": candidate.p25_destination,
                                    "startedAtEpoch": time.time(),
                                }
                            )
                            break

                if self.active_downlink_uuid is not None:
                    call = self.downlink_calls.get(self.active_downlink_uuid)
                    if call is None:
                        self.active_downlink_uuid = None
                    elif call.frames:
                        ste = call.frames.popleft()
                    elif call.ended:
                        self._remove_downlink_locked(call)
                        call = None
                    elif time.monotonic() - call.last_frame_at > 0.14:
                        call.underruns += 1
                        call.started = False
                        self.status.increment("downlinkUnderruns")
                        self.active_downlink_uuid = None
                        self.status.set(activeDownlink=None)
                        call = None

            if call is None or ste is None:
                self.stop_event.wait(0.005)
                continue

            delay = next_due - time.monotonic()
            if delay > 0:
                self.stop_event.wait(delay)
            if time.monotonic() - next_due > 0.18:
                next_due = time.monotonic()
            self._play_downlink_block(call, ste)
            next_due += 0.060

    def _play_downlink_block(self, call: DownlinkCall, ste: bytes) -> None:
        try:
            coded_a, coded_b = split_tmd_block(ste)
            samples = call.decoder.decode(coded_a) + call.decoder.decode(coded_b)
        except Exception as exc:
            logging.warning("TETRA downlink decode failed: %s", type(exc).__name__)
            self.status.increment("downlinkDecodeFailures")
            return
        samples = scale_pcm(samples, self.config.downlink_gain, self.config.downlink_peak_limit)
        block_start = time.monotonic()
        for index in range(3):
            if self.stop_event.is_set():
                return
            target = block_start + index * 0.020
            delay = target - time.monotonic()
            if delay > 0:
                self.stop_event.wait(delay)
            chunk = samples[index * PCM_SAMPLES_20MS:(index + 1) * PCM_SAMPLES_20MS]
            pcm = struct.pack("<160h", *chunk)
            packet = build_dvm_rtp(
                pcm,
                call.source,
                call.p25_destination,
                self.rtp_sequence,
                self.rtp_timestamp,
                self.rtp_ssrc,
                self.rtp_sequence == 0,
            )
            self.output_socket.sendto(packet, (self.config.pcm_output.address, self.config.pcm_output.port))
            self.rtp_sequence = (self.rtp_sequence + 1) % 65535
            self.rtp_timestamp = (self.rtp_timestamp + PCM_SAMPLES_20MS) & 0xFFFFFFFF
            self.status.increment("p25PcmFramesSent")

    def _remove_downlink_locked(self, call: DownlinkCall) -> None:
        call.decoder.close()
        self.downlink_calls.pop(call.call_uuid, None)
        if self.active_downlink_uuid == call.call_uuid:
            self.active_downlink_uuid = None
        self.status.set(activeDownlink=None)
        logging.info(
            "BREW downlink playout complete uuid=%s underruns=%u",
            uuid_text(call.call_uuid),
            call.underruns,
        )

    def _expire_owned_uuids(self) -> None:
        now = time.monotonic()
        expired = [key for key, expiry in self.owned_uuids.items() if expiry <= now]
        for key in expired:
            self.owned_uuids.pop(key, None)
            self.ignored_downlink_uuids.discard(key)

    def _clear_dynamic_routes(self) -> None:
        with self.router_lock:
            cleared = self.router.clear()
            save_dynamic_routes(self.config.dynamic_state_file, self.router)
            dynamic_snapshot = self.router.snapshot()
        self.status.set(dynamicTalkgroups=dynamic_snapshot)
        if cleared:
            self._send_affiliation(SUBSCRIBER_DEAFFILIATE, cleared)
        logging.info("Received disconnect TG %u from RF side, clearing dynamic TG state", self.config.disconnect_talkgroup)

    def _routing_loop(self) -> None:
        while not self.stop_event.wait(1.0):
            self._reload_routing_if_changed()
            with self.router_lock:
                expired = self.router.expire(time.monotonic())
                if expired:
                    save_dynamic_routes(self.config.dynamic_state_file, self.router)
                dynamic_snapshot = self.router.snapshot()
            if not expired:
                continue
            self._send_affiliation(SUBSCRIBER_DEAFFILIATE, expired)
            self.status.set(dynamicTalkgroups=dynamic_snapshot)
            for talkgroup in expired:
                logging.info("Dynamic TG %u expired locally", talkgroup)

    def _reload_routing_if_changed(self) -> None:
        try:
            mtime_ns = self.config.quantarbridge_config.stat().st_mtime_ns
            if mtime_ns == self.routing_config_mtime_ns:
                return
            runtime = yaml.safe_load(
                self.config.quantarbridge_config.read_text(encoding="utf-8")
            ) or {}
            routing = runtime.get("routing", {}) or {}
            p25_to_brew: dict[int, int] = {}
            brew_to_p25: dict[int, int] = {}
            for item in routing.get("talkgroupMappings", []):
                p25 = int(item["p25"])
                brew = int(item.get("brew", item.get("brandmeister", 0)))
                if not 1 <= p25 <= 0xFFFFFF or not 1 <= brew <= 0xFFFFFF:
                    raise ValueError("talkgroup mapping is outside the 24-bit range")
                if p25 in p25_to_brew or brew in brew_to_p25:
                    raise ValueError("talkgroup mapping is not unique")
                p25_to_brew[p25] = brew
                brew_to_p25[brew] = p25
            static_groups = {int(value) for value in routing.get("staticTalkgroups", [])}
            if any(group < 1 or group > 0xFFFFFF for group in static_groups):
                raise ValueError("static talkgroup is outside the 24-bit range")
            dynamic_timeout = max(10, int(routing.get("dynamicTimeoutSeconds", 600)))
            disconnect_talkgroup = int(routing.get("disconnectTalkgroup", 4000))
            if not 1 <= disconnect_talkgroup <= 0xFFFFFF:
                raise ValueError("disconnect talkgroup is outside the 24-bit range")
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            self.status.set(lastError=f"routing_reload_{type(exc).__name__}")
            logging.warning("Routing reload failed: %s", exc)
            return

        with self.router_lock:
            old_groups = set(self.router.groups())
            self.config.p25_to_brew = p25_to_brew
            self.config.brew_to_p25 = brew_to_p25
            self.config.static_brew_groups = static_groups
            self.config.dynamic_timeout_seconds = dynamic_timeout
            self.config.disconnect_talkgroup = disconnect_talkgroup
            for talkgroup in list(self.router.dynamic):
                if self.router.is_static(talkgroup):
                    self.router.dynamic.pop(talkgroup, None)
            new_groups = set(self.router.groups())
            save_dynamic_routes(self.config.dynamic_state_file, self.router)
            dynamic_snapshot = self.router.snapshot()
        self.routing_config_mtime_ns = mtime_ns
        self._send_affiliation(SUBSCRIBER_DEAFFILIATE, sorted(old_groups - new_groups))
        self._send_affiliation(SUBSCRIBER_AFFILIATE, sorted(new_groups - old_groups))
        self.status.set(
            lastError=None,
            staticTalkgroups=sorted(static_groups),
            dynamicTalkgroups=dynamic_snapshot,
            talkgroupMappings=[
                {"p25": p25, "brew": brew} for p25, brew in sorted(p25_to_brew.items())
            ],
        )
        logging.info(
            "Reloaded routing: mappings=%u static=%u timeout=%us",
            len(p25_to_brew),
            len(static_groups),
            dynamic_timeout,
        )

    def _status_loop(self) -> None:
        while not self.stop_event.wait(1.0):
            try:
                with self.router_lock:
                    self.status.set(
                        staticTalkgroups=sorted(self.config.static_brew_groups),
                        dynamicTalkgroups=self.router.snapshot(),
                    )
                self.status.write()
            except Exception as exc:
                logging.warning("Status write failed: %s", type(exc).__name__)


def configure_logging(path: Path, verbose: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=2, encoding="utf-8")
    console = logging.StreamHandler()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
        handlers=[handler, console],
    )
    if path.exists():
        os.chmod(path, 0o600)


def run_self_test(config: AudioConfig) -> None:
    codec = CodecLibrary(config.codec_library)

    rng = random.Random(0x42524557)
    frame_a = bytearray(TETRA_CODED_BYTES)
    frame_b = bytearray(TETRA_CODED_BYTES)
    for bit_index in range(TETRA_BITS_30MS):
        set_packed_bit(frame_a, bit_index, rng.getrandbits(1))
        set_packed_bit(frame_b, bit_index, rng.getrandbits(1))
    ste = join_tmd_block(bytes(frame_a), bytes(frame_b))
    split_a, split_b = split_tmd_block(ste)
    assert split_a == bytes(frame_a) and split_b == bytes(frame_b)

    with codec.encoder() as encoder, codec.decoder() as decoder:
        decoded: list[int] = []
        for frame_index in range(20):
            samples = [
                int(8000 * math.sin(2 * math.pi * 1000 * (frame_index * TETRA_SAMPLES_60MS + index) / 8000))
                for index in range(TETRA_SAMPLES_60MS)
            ]
            block = join_tmd_block(
                encoder.encode(samples[:TETRA_SAMPLES_30MS]),
                encoder.encode(samples[TETRA_SAMPLES_30MS:]),
            )
            coded_a, coded_b = split_tmd_block(block)
            decoded.extend(decoder.decode(coded_a))
            decoded.extend(decoder.decode(coded_b))
        rms = math.sqrt(sum(value * value for value in decoded) / len(decoded))
        assert rms > 1000

    pcm = struct.pack("<160h", *range(PCM_SAMPLES_20MS))
    packet = build_dvm_rtp(pcm, 1000001, 999, 42, 123456, 9000112, True)
    parsed = parse_dvm_rtp(packet)
    assert parsed is not None
    source, destination, sequence, marker, parsed_pcm = parsed
    assert (source, destination, sequence, marker, parsed_pcm) == (1000001, 999, 42, True, pcm)

    test_uuid = uuid.uuid4().bytes_le
    assert len(build_subscriber_message(SUBSCRIBER_REGISTER, 1000001)) == 18
    assert len(build_subscriber_message(SUBSCRIBER_AFFILIATE, 1000001, [983872])) == 22
    assert len(build_group_tx(test_uuid, 1000001, 983872)) == 30
    assert len(build_voice_frame(test_uuid, ste)) == 56
    assert len(build_group_idle(test_uuid)) == 19
    print(f"self_test=ok codec_rms={rms:.1f} ste_bytes={len(ste)} rtp_bytes={len(packet)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = AudioConfig.load(args.config)
    if args.self_test:
        run_self_test(config)
        return 0

    configure_logging(config.log_file, args.verbose)
    if not config.enabled:
        logging.info("BREW audio bridge is disabled in %s", args.config)
        return 0
    worker = BrewAudioBridge(config)

    def stop_handler(_signum: int, _frame: object) -> None:
        worker.request_stop()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    worker.start()
    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    try:
        while not worker.stop_event.wait(0.25):
            if deadline is not None and time.monotonic() >= deadline:
                break
    finally:
        worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
