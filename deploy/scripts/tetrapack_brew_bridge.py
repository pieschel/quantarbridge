#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import requests
    from requests.auth import HTTPDigestAuth
except Exception:  # pragma: no cover - optional runtime dependency
    requests = None
    HTTPDigestAuth = None

try:
    import yaml
except Exception:  # pragma: no cover - optional runtime dependency
    yaml = None

try:
    import websocket
except Exception:  # pragma: no cover - optional runtime dependency
    websocket = None


@dataclass
class BrewConfig:
    enabled: bool = False
    base_url: str = "https://core.tetrapack.online"
    username: str = ""
    password: str = ""
    user_agent: str = "quantarbridge-sms/20260406"
    request_timeout_seconds: float = 10.0


class BrewAuthenticationError(RuntimeError):
    pass


PID_TEXT_MESSAGING = 0x82
MESSAGE_TYPE_SDS_TRANSFER = 0x0
DELIVERY_REPORT_RECEIVED = 0x1
SERVICE_SELECTION_INDIVIDUAL = 0x0
STORAGE_FORWARD_NONE = 0x0
TEXT_CODING_UTF16BE = 0x1A
BREW_CLASS_CALL_CONTROL = 0xF1
BREW_CLASS_FRAME_DATA = 0xF2
CALL_STATE_SHORT_TRANSFER = 11
CALL_STATE_CALL_RELEASE = 10
FRAME_TYPE_SDS_TRANSFER = 1
FRAME_TYPE_SDS_REPORT = 2


@dataclass
class BridgeConfig:
    inbox_dir: Path
    outbox_dir: Path
    processed_dir: Path
    error_dir: Path
    p25_outbox_dir: Path | None = None
    service_route_dir: Path | None = None
    brew_audio_outbox_dir: Path | None = None
    poll_interval_seconds: float = 0.25
    service_route_max_age_seconds: int = 900
    local_loop_enabled: bool = False
    brew_service_rids: set[int] = field(default_factory=lambda: {262993})
    brew: BrewConfig = field(default_factory=BrewConfig)

    def __post_init__(self) -> None:
        if self.p25_outbox_dir is None:
            self.p25_outbox_dir = self.outbox_dir.parent / "p25-outbox"
        if self.service_route_dir is None:
            self.service_route_dir = self.outbox_dir.parent / "service-routes"


@dataclass
class PendingText:
    source_rid: int
    target_rid: int
    local_candidate: bool
    first_seen: float
    updated_at: float
    fragments: list[str] = field(default_factory=list)
    event_names: list[str] = field(default_factory=list)


class BrewClient:
    def __init__(self, config: BrewConfig) -> None:
        self.config = config

    def send_sms(self, source_rid: int, target_rid: int, text: str) -> dict[str, Any]:
        if not self.config.enabled:
            return {
                "status": "deferred",
                "reason": "brew_transport_not_enabled",
                "base_url": self.config.base_url,
                "sourceRid": source_rid,
                "targetRid": target_rid,
                "text": text,
            }

        if not self.config.username or not self.config.password:
            return {
                "status": "error",
                "reason": "missing_brew_credentials",
                "base_url": self.config.base_url,
                "sourceRid": source_rid,
                "targetRid": target_rid,
                "text": text,
            }

        if websocket is None:
            return {
                "status": "error",
                "reason": "python_package_websocket_client_missing",
                "base_url": self.config.base_url,
                "sourceRid": source_rid,
                "targetRid": target_rid,
                "text": text,
            }

        if requests is None or HTTPDigestAuth is None:
            return {
                "status": "error",
                "reason": "python_package_requests_missing",
                "base_url": self.config.base_url,
                "sourceRid": source_rid,
                "targetRid": target_rid,
                "text": text,
            }

        message_reference = int(time.time_ns() // 1_000_000) & 0xFF
        payload = build_text_sds_type4_pdu(text, message_reference)
        session_id = uuid.uuid4()

        endpoint = self._get_endpoint()
        ws = websocket.create_connection(
            endpoint,
            timeout=self.config.request_timeout_seconds,
            subprotocols=["brew"],
            header=[
                f"User-Agent: {self.config.user_agent}",
                "X-Brew-Mode: Basestation",
                "X-Brew-Version: 1",
            ],
        )
        try:
            ws.send_binary(build_brew_short_transfer(session_id, source_rid, target_rid))
            ws.send_binary(build_brew_sds_transfer(session_id, payload))

            release_frame = build_brew_call_release(session_id, cause=0)
            if release_frame is not None:
                ws.send_binary(release_frame)

            responses = self._collect_responses(ws)
        finally:
            try:
                ws.close()
            except Exception:
                pass

        return {
            "status": "sent",
            "base_url": self.config.base_url,
            "endpoint": endpoint,
            "sourceRid": source_rid,
            "targetRid": target_rid,
            "text": text,
            "messageReference": message_reference,
            "sessionId": str(session_id),
            "sdsType4Hex": payload.hex(),
            "responses": responses,
        }

    def _get_endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        response = requests.get(
            f"{base}/brew/",
            headers={
                "User-Agent": self.config.user_agent,
                "X-Brew-Mode": "Basestation",
                "X-Brew-Version": "1",
            },
            auth=HTTPDigestAuth(self.config.username, self.config.password),
            timeout=self.config.request_timeout_seconds,
        )
        if response.status_code in (401, 403):
            raise BrewAuthenticationError(
                f"BREW authentication rejected (HTTP {response.status_code})"
            )
        response.raise_for_status()
        endpoint_path = response.text.strip()
        if not endpoint_path.startswith("/"):
            endpoint_path = f"/{endpoint_path}"

        if base.startswith("https://"):
            return f"wss://{base[len('https://'):]}{endpoint_path}"
        if base.startswith("http://"):
            return f"ws://{base[len('http://'):]}{endpoint_path}"
        raise ValueError(f"Unsupported BREW base URL: {base}")

    def _collect_responses(self, ws: Any) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        ws.settimeout(0.35)
        while True:
            try:
                frame = ws.recv()
            except Exception:
                break
            if isinstance(frame, bytes):
                frames.append(parse_brew_frame(frame))
            else:
                frames.append({"kind": "text", "text": str(frame)})
        return frames


class BrewAudioQueueClient:
    """Queue SDS commands for the audio worker that owns the BREW session."""

    def __init__(self, outbox_dir: Path) -> None:
        self.outbox_dir = outbox_dir

    def send_sms(self, source_rid: int, target_rid: int, text: str) -> dict[str, Any]:
        created_at_ms = time.time_ns() // 1_000_000
        command_id = f"{created_at_ms:013d}-{source_rid}-{target_rid}-{uuid.uuid4().hex}"
        path = self.outbox_dir / f"{command_id}.json"
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "commandId": command_id,
                    "createdAtMs": created_at_ms,
                    "sourceRid": source_rid,
                    "targetRid": target_rid,
                    "text": text,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        return {
            "status": "sent",
            "delivery": "queued_on_active_brew_session",
            "commandId": command_id,
            "commandPath": str(path),
            "sourceRid": source_rid,
            "targetRid": target_rid,
            "text": text,
        }


class BitWriter:
    def __init__(self) -> None:
        self._bits: list[int] = []

    def push(self, value: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            self._bits.append((value >> shift) & 1)

    def push_bytes(self, data: bytes) -> None:
        for byte in data:
            self.push(byte, 8)

    def to_bytes(self) -> bytes:
        if not self._bits:
            return b""
        padded = list(self._bits)
        while len(padded) % 8:
            padded.append(0)
        out = bytearray()
        for offset in range(0, len(padded), 8):
            value = 0
            for bit in padded[offset:offset + 8]:
                value = (value << 1) | bit
            out.append(value)
        return bytes(out)


def build_text_sds_type4_pdu(text: str, message_reference: int) -> bytes:
    writer = BitWriter()
    writer.push(PID_TEXT_MESSAGING, 8)
    writer.push(MESSAGE_TYPE_SDS_TRANSFER, 4)
    writer.push(DELIVERY_REPORT_RECEIVED, 2)
    writer.push(SERVICE_SELECTION_INDIVIDUAL, 1)
    writer.push(STORAGE_FORWARD_NONE, 1)
    writer.push(message_reference & 0xFF, 8)
    writer.push(0, 1)  # Time stamp used = no timestamp present.
    writer.push(TEXT_CODING_UTF16BE, 7)
    writer.push_bytes(text.encode("utf-16-be"))
    return writer.to_bytes()


def build_brew_short_transfer(session_id: uuid.UUID, source_rid: int, target_rid: int) -> bytes:
    number = bytes(32)
    return (
        bytes((BREW_CLASS_CALL_CONTROL, CALL_STATE_SHORT_TRANSFER))
        + session_id.bytes_le
        + int(source_rid).to_bytes(4, "little", signed=False)
        + int(target_rid).to_bytes(4, "little", signed=False)
        + number
    )


def build_brew_sds_transfer(session_id: uuid.UUID, payload: bytes) -> bytes:
    return (
        bytes((BREW_CLASS_FRAME_DATA, FRAME_TYPE_SDS_TRANSFER))
        + session_id.bytes_le
        + int(len(payload) * 8).to_bytes(2, "little", signed=False)
        + payload
    )


def build_brew_call_release(session_id: uuid.UUID, cause: int) -> bytes:
    return (
        bytes((BREW_CLASS_CALL_CONTROL, CALL_STATE_CALL_RELEASE))
        + session_id.bytes_le
        + int(cause & 0xFF).to_bytes(1, "little", signed=False)
    )


def parse_brew_frame(frame: bytes) -> dict[str, Any]:
    if len(frame) < 2:
        return {"kind": "binary", "hex": frame.hex(), "error": "too_short"}

    kind = frame[0]
    ftype = frame[1]
    parsed: dict[str, Any] = {
        "kind": f"0x{kind:02x}",
        "type": f"0x{ftype:02x}",
        "hex": frame.hex(),
    }

    if kind == BREW_CLASS_FRAME_DATA and len(frame) >= 20:
        parsed["sessionId"] = str(uuid.UUID(bytes_le=frame[2:18]))
        parsed["lengthBits"] = int.from_bytes(frame[18:20], "little", signed=False)
        payload = frame[20:]
        parsed["payloadHex"] = payload.hex()
        if ftype == FRAME_TYPE_SDS_REPORT and payload:
            parsed["status"] = payload[0]
    elif kind == BREW_CLASS_CALL_CONTROL and len(frame) >= 18:
        parsed["sessionId"] = str(uuid.UUID(bytes_le=frame[2:18]))

    return parsed


def load_config(path: Path) -> BridgeConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    brew_raw = raw.get("brew", {}) or {}
    brew_service_rids = raw.get("brewServiceRids")
    if brew_service_rids is None:
        # Backward compatibility for runtime files created before the key made
        # its service-only purpose explicit.
        brew_service_rids = raw.get("brewTargetRids", [262993])
    fallback_runtime = load_runtime_bm_defaults(path)
    outbox_dir = Path(raw.get("outboxDir", "/home/quantar/quantar-runtime/sms/outbox"))
    return BridgeConfig(
        inbox_dir=Path(raw.get("inboxDir", "/home/quantar/quantar-runtime/sms/inbox")),
        outbox_dir=outbox_dir,
        processed_dir=Path(raw.get("processedDir", "/home/quantar/quantar-runtime/sms/processed")),
        error_dir=Path(raw.get("errorDir", "/home/quantar/quantar-runtime/sms/error")),
        p25_outbox_dir=Path(raw.get("p25OutboxDir", outbox_dir.parent / "p25-outbox")),
        service_route_dir=Path(raw.get("serviceRouteDir", outbox_dir.parent / "service-routes")),
        brew_audio_outbox_dir=(
            Path(raw["brewAudioOutboxDir"])
            if raw.get("brewAudioOutboxDir")
            else None
        ),
        poll_interval_seconds=float(raw.get("pollIntervalSeconds", 0.25)),
        service_route_max_age_seconds=max(60, int(raw.get("serviceRouteMaxAgeSeconds", 900))),
        local_loop_enabled=bool(raw.get("localLoopEnabled", False)),
        brew_service_rids={int(value) for value in brew_service_rids},
        brew=BrewConfig(
            enabled=bool(brew_raw.get("enabled", False)),
            base_url=str(brew_raw.get("baseUrl", "https://core.tetrapack.online")),
            username=str(
                brew_raw.get("username")
                or os.environ.get("BREW_USERNAME", "")
                or os.environ.get("BM_API_USERNAME", "")
                or fallback_runtime.get("username", "")
            ),
            password=str(
                brew_raw.get("password")
                or os.environ.get("BREW_PASSWORD", "")
                or os.environ.get("BM_API_PASSWORD", "")
                or fallback_runtime.get("password", "")
            ),
            user_agent=str(brew_raw.get("userAgent", "quantarbridge-sms/20260406")),
            request_timeout_seconds=float(brew_raw.get("requestTimeoutSeconds", 10.0)),
        ),
    )


def load_runtime_bm_defaults(config_path: Path) -> dict[str, str]:
    candidates = [
        config_path.parent / "quantarbridge.yml",
        config_path.parents[2] / "runtime" / "quantarbridge.yml" if len(config_path.parents) >= 3 else None,
        Path("/home/quantar/quantar-runtime/quantarbridge.yml"),
    ]
    for candidate in candidates:
        if candidate is None or not candidate.exists():
            continue
        try:
            if yaml is None:
                continue
            raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        brandmeister = raw.get("brandmeister", {}) or {}
        username = (
            brandmeister.get("username")
            or raw.get("callsign")
            or os.environ.get("CALLSIGN", "")
        )
        password = brandmeister.get("password", "")
        if username or password:
            return {"username": str(username or ""), "password": str(password or "")}
    return {}


def ensure_dirs(config: BridgeConfig) -> None:
    config.inbox_dir.mkdir(parents=True, exist_ok=True)
    config.outbox_dir.mkdir(parents=True, exist_ok=True)
    config.processed_dir.mkdir(parents=True, exist_ok=True)
    config.error_dir.mkdir(parents=True, exist_ok=True)
    assert config.p25_outbox_dir is not None
    config.p25_outbox_dir.mkdir(parents=True, exist_ok=True)
    assert config.service_route_dir is not None
    config.service_route_dir.mkdir(parents=True, exist_ok=True)
    if config.brew_audio_outbox_dir is not None:
        config.brew_audio_outbox_dir.mkdir(parents=True, exist_ok=True)


def write_outbox_message(
    config: BridgeConfig,
    event: dict[str, Any],
    text: str,
    local_only: bool,
    route: str,
    channel: str = "tms",
    raw_ip_packet_hex: str = "",
) -> Path:
    source_rid = int(event["sourceRid"])
    target_rid = int(event["targetRid"])
    timestamp_ms = int(event.get("timestampMs", time.time_ns() // 1_000_000))
    session_id = str(event.get("sessionId", f"sms-{timestamp_ms}")).replace("/", "_")
    path = config.outbox_dir / f"{timestamp_ms}_{session_id}.json"
    body = {
        "route": route,
        "channel": channel,
        "sourceRid": target_rid if local_only else source_rid,
        "targetRid": source_rid if local_only else target_rid,
        "origin": {
            "sessionId": event.get("sessionId"),
            "application": event.get("application"),
            "sourceRid": source_rid,
            "targetRid": target_rid,
            "localLoop": local_only,
        },
    }
    if raw_ip_packet_hex:
        body["rawIpPacketHex"] = raw_ip_packet_hex
    elif text:
        body["text"] = text
    route_name = route.lower()
    if channel == "tms" and not raw_ip_packet_hex and route_name not in ("brandmeister", "bm"):
        body["sendArsFirst"] = True
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def write_p25_outbox_message(config: BridgeConfig, event: dict[str, Any], text: str) -> Path:
    source_rid = int(event["sourceRid"])
    target_rid = int(event["targetRid"])
    timestamp_ms = int(event.get("timestampMs", time.time_ns() // 1_000_000))
    session_id = str(event.get("sessionId", f"sms-{timestamp_ms}")).replace("/", "_")
    assert config.p25_outbox_dir is not None
    path = config.p25_outbox_dir / f"{timestamp_ms}_{session_id}.yaml"
    temp_path = path.with_suffix(path.suffix + ".tmp")
    text_hex = text.encode("utf-8").hex()
    temp_path.write_text(
        f"sourceRid: {source_rid}\n"
        f"targetRid: {target_rid}\n"
        f'textHex: "{text_hex}"\n',
        encoding="utf-8",
    )
    temp_path.replace(path)
    return path


def write_service_route(config: BridgeConfig, requester_rid: int, service_rid: int) -> Path:
    created_at_ms = time.time_ns() // 1_000_000
    expires_at_ms = created_at_ms + config.service_route_max_age_seconds * 1000
    assert config.service_route_dir is not None
    path = config.service_route_dir / (
        f"{created_at_ms:013d}-{service_rid}-{requester_rid}-{uuid.uuid4().hex}.json"
    )
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(
            {
                "createdAtMs": created_at_ms,
                "expiresAtMs": expires_at_ms,
                "serviceRid": service_rid,
                "requesterRid": requester_rid,
            },
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)
    return path


def normalize_event(path: Path) -> dict[str, Any]:
    event = json.loads(path.read_text(encoding="utf-8"))
    event["path"] = str(path)
    event["sessionId"] = event.get("sessionId") or path.stem
    return event


def choose_text(event: dict[str, Any]) -> str:
    for key in ("parsedText", "parsedTextFragment", "text"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def append_text_fragment(pending: PendingText, fragment: str, event_name: str) -> None:
    fragment = fragment.strip()
    if not fragment:
        return
    if any(fragment == existing or fragment in existing for existing in pending.fragments):
        return
    pending.fragments.append(fragment)
    pending.event_names.append(event_name)
    pending.updated_at = time.monotonic()


def flush_pending_text(config: BridgeConfig, brew: BrewClient, pending: PendingText) -> dict[str, Any]:
    text = "".join(pending.fragments).strip()
    synthetic_event = {
        "application": "motorola_tms",
        "sourceRid": pending.source_rid,
        "targetRid": pending.target_rid,
        "localDeliveryCandidate": pending.local_candidate,
        "sessionId": "collected-" + "-".join(pending.event_names[:2]),
    }

    if pending.local_candidate and config.local_loop_enabled:
        outbox_path = write_p25_outbox_message(config, synthetic_event, text)
        return {
            "status": "queued",
            "transport": "local_p25",
            "outboxPath": str(outbox_path),
            "sourceRid": pending.source_rid,
            "targetRid": pending.target_rid,
            "text": text,
            "fragments": pending.fragments,
        }

    if pending.target_rid in config.brew_service_rids:
        if not config.brew.enabled:
            return {
                "status": "held",
                "reason": "brew_transport_not_enabled_for_service_target",
                "transport": "tetrapack_brew",
                "sourceRid": pending.source_rid,
                "targetRid": pending.target_rid,
                "text": text,
                "fragments": pending.fragments,
            }
        route_path = write_service_route(
            config, pending.source_rid, pending.target_rid
        )
        try:
            result = brew.send_sms(pending.source_rid, pending.target_rid, text)
        except Exception:
            route_path.unlink(missing_ok=True)
            raise
        if result.get("status") != "sent":
            route_path.unlink(missing_ok=True)
        else:
            result["serviceRoutePath"] = str(route_path)
        result["transport"] = "tetrapack_brew"
        result["fragments"] = pending.fragments
        return result

    outbox_path = write_outbox_message(
        config, synthetic_event, text, local_only=False, route="brandmeister"
    )
    return {
        "status": "queued",
        "transport": "brandmeister_packet_data",
        "outboxPath": str(outbox_path),
        "sourceRid": pending.source_rid,
        "targetRid": pending.target_rid,
        "text": text,
        "fragments": pending.fragments,
    }


def process_event(config: BridgeConfig, brew: BrewClient, pending_texts: dict[tuple[int, int], PendingText], path: Path) -> None:
    event = normalize_event(path)
    text = choose_text(event)
    raw_ip_packet_hex = event.get("rawIpPacketHex") or event.get("hexIpPacket")
    if event.get("application") == "motorola_lrrp" and isinstance(raw_ip_packet_hex, str) and raw_ip_packet_hex:
        outbox_path = write_outbox_message(
            config,
            event,
            "",
            local_only=False,
            route="brandmeister",
            channel="lrrp",
            raw_ip_packet_hex=raw_ip_packet_hex,
        )
        result = {
            "status": "queued",
            "transport": "brandmeister_location_packet_data",
            "outboxPath": str(outbox_path),
            "sourceRid": int(event["sourceRid"]),
            "targetRid": int(event["targetRid"]),
            "rawIpPacket": True,
        }
        result_path = config.processed_dir / f"{path.stem}.result.json"
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        destination = config.processed_dir / path.name
        shutil.move(str(path), destination)
        return

    if event.get("application") in ("motorola_tms_raw", "motorola_ars_raw") and isinstance(raw_ip_packet_hex, str) and raw_ip_packet_hex:
        result = {
            "status": "dropped",
            "reason": "raw_motorola_packet_data_is_not_forwarded_to_brandmeister",
            "rawIpPacket": True,
            "sourceRid": int(event["sourceRid"]),
            "targetRid": int(event["targetRid"]),
        }
        result_path = config.processed_dir / f"{path.stem}.result.json"
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        destination = config.processed_dir / path.name
        shutil.move(str(path), destination)
        return

    if event.get("application") == "motorola_ars":
        result = {
            "status": "dropped",
            "reason": "ars_bootstrap_is_terminated_locally_until_tms_text_arrives",
            "sourceRid": int(event["sourceRid"]),
            "targetRid": int(event["targetRid"]),
        }
        result_path = config.processed_dir / f"{path.stem}.result.json"
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        destination = config.processed_dir / path.name
        shutil.move(str(path), destination)
        return

    if event.get("application") != "motorola_tms" or not text:
        destination = config.processed_dir / path.name
        shutil.move(str(path), destination)
        return

    source_rid = int(event["sourceRid"])
    target_rid = int(event["targetRid"])
    key = (source_rid, target_rid)
    now = time.monotonic()
    pending = pending_texts.get(key)
    if pending is None:
        pending = PendingText(
            source_rid=source_rid,
            target_rid=target_rid,
            local_candidate=bool(event.get("localDeliveryCandidate", False)),
            first_seen=now,
            updated_at=now,
        )
        pending_texts[key] = pending
    append_text_fragment(pending, text, path.stem)
    result = {
        "status": "pending_text_collection",
        "sourceRid": source_rid,
        "targetRid": target_rid,
        "textFragment": text,
    }
    result_path = config.processed_dir / f"{path.stem}.result.json"
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    destination = config.processed_dir / path.name
    shutil.move(str(path), destination)


def flush_ready_texts(config: BridgeConfig, brew: BrewClient, pending_texts: dict[tuple[int, int], PendingText], force: bool = False) -> int:
    now = time.monotonic()
    processed = 0
    for key, pending in list(pending_texts.items()):
        if not pending.fragments:
            del pending_texts[key]
            continue
        quiet = now - pending.updated_at
        age = now - pending.first_seen
        if not force and quiet < 5.0 and age < 8.0:
            continue
        try:
            result = flush_pending_text(config, brew, pending)
        except BrewAuthenticationError as exc:
            failure_result = {
                "status": "failed",
                "reason": "brew_authentication_rejected",
                "error": str(exc),
                "sourceRid": pending.source_rid,
                "targetRid": pending.target_rid,
                "text": "".join(pending.fragments),
            }
            failure_path = config.processed_dir / (
                f"failed-{int(time.time_ns() // 1_000_000)}-"
                f"{pending.source_rid}-{pending.target_rid}.result.json"
            )
            failure_path.write_text(
                json.dumps(failure_result, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(
                f"BREW authentication rejected sourceRid={pending.source_rid} "
                f"targetRid={pending.target_rid}; message will not be retried",
                file=sys.stderr,
                flush=True,
            )
            del pending_texts[key]
            processed += 1
            continue
        except Exception as exc:  # transient transport failures are retryable
            pending.first_seen = now
            pending.updated_at = now
            retry_result = {
                "status": "retrying",
                "reason": type(exc).__name__,
                "error": str(exc),
                "sourceRid": pending.source_rid,
                "targetRid": pending.target_rid,
                "text": "".join(pending.fragments),
            }
            retry_path = config.processed_dir / (
                f"retry-{int(time.time_ns() // 1_000_000)}-"
                f"{pending.source_rid}-{pending.target_rid}.result.json"
            )
            retry_path.write_text(json.dumps(retry_result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(
                f"BREW send retry scheduled sourceRid={pending.source_rid} "
                f"targetRid={pending.target_rid} error={exc}",
                file=sys.stderr,
                flush=True,
            )
            continue
        result_path = config.processed_dir / f"collected-{int(time.time_ns() // 1_000_000)}-{pending.source_rid}-{pending.target_rid}.result.json"
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        del pending_texts[key]
        processed += 1
    return processed


def run_once(config: BridgeConfig, brew: BrewClient, pending_texts: dict[tuple[int, int], PendingText], force_flush: bool = False) -> int:
    processed = 0
    for path in sorted(config.inbox_dir.glob("*.json")):
        try:
            process_event(config, brew, pending_texts, path)
            processed += 1
        except Exception as exc:  # pragma: no cover - runtime guard
            error_target = config.error_dir / path.name
            if path.exists():
                shutil.move(str(path), error_target)
            (config.error_dir / f"{path.stem}.error.txt").write_text(str(exc) + "\n", encoding="utf-8")
    processed += flush_ready_texts(config, brew, pending_texts, force=force_flush)
    return processed


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge quantarbridge SMS inbox events to local RF delivery and optional BREW backend delivery.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if not args.config.exists():
        print(f"disabled=missing_config path={args.config}")
        return 0

    config = load_config(args.config)
    ensure_dirs(config)
    brew = (
        BrewAudioQueueClient(config.brew_audio_outbox_dir)
        if config.brew_audio_outbox_dir is not None
        else BrewClient(config.brew)
    )
    pending_texts: dict[tuple[int, int], PendingText] = {}

    if args.once:
        return 0 if run_once(config, brew, pending_texts, force_flush=True) >= 0 else 1

    while True:
        run_once(config, brew, pending_texts)
        time.sleep(config.poll_interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
