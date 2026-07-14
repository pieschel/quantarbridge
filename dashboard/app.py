#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import configparser
import csv
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml


LOG = logging.getLogger("quantar-dashboard")
PBKDF2_ITERATIONS = 600_000
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
SESSION_IDLE_SECONDS = 60 * 60
MAX_JSON_BYTES = 128 * 1024
BM_PROFILE_REFRESH_SECONDS = 20
RADIO_REGISTRATION_TIMEOUT_SECONDS = 70 * 60
IDENTITY_REFRESH_SECONDS = 24 * 60 * 60
IDENTITY_NEGATIVE_REFRESH_SECONDS = 6 * 60 * 60
IDENTITY_RETRY_SECONDS = 5 * 60
IDENTITY_REQUEST_SPACING_SECONDS = 0.25
SETTINGS_RESTART_IDLE_GUARD_SECONDS = 15
RSSI_SAMPLE_HISTORY_LIMIT = 100_000
RSSI_START_TOLERANCE_SECONDS = 0.75
RSSI_END_TOLERANCE_SECONDS = 0.25


class SettingsBusyError(ValueError):
    pass


def utc_iso(epoch: float | None = None) -> str:
    value = time.time() if epoch is None else epoch
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="milliseconds")


def decode_motorola_lrrp_position(packet_hex: str) -> tuple[float, float] | None:
    try:
        packet = bytes.fromhex(packet_hex)
    except (TypeError, ValueError):
        return None
    if len(packet) < 39 or packet[0] >> 4 != 4 or packet[9] != 0x11:
        return None

    ip_header_length = (packet[0] & 0x0F) * 4
    if ip_header_length < 20 or len(packet) < ip_header_length + 10:
        return None
    udp_length = int.from_bytes(packet[ip_header_length + 4 : ip_header_length + 6], "big")
    if udp_length < 10 or ip_header_length + udp_length > len(packet):
        return None

    payload = packet[ip_header_length + 8 : ip_header_length + udp_length]
    if len(payload) < 2 or (payload[0] & 0x7F) not in (0x07, 0x0D):
        return None
    report_length = payload[1] + 2
    if report_length > len(payload):
        return None

    offset = 2
    if offset + 2 <= report_length and payload[offset] == 0x22:
        request_id_length = payload[offset + 1]
        offset += 2 + request_id_length

    while offset < report_length:
        token = payload[offset]
        if token == 0x34:
            offset += 6
            continue
        if token == 0x37:
            offset += 1
            while offset < report_length:
                byte = payload[offset]
                offset += 1
                if byte & 0x80 == 0:
                    break
            continue
        if token in (0x51, 0x55, 0x66, 0x69) and offset + 9 <= report_length:
            latitude_raw, longitude_raw = struct.unpack(">ii", payload[offset + 1 : offset + 9])
            latitude = latitude_raw * 180.0 / 2**32
            longitude = longitude_raw * 360.0 / 2**32
            if -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0:
                return round(latitude, 7), round(longitude, 7)
        return None
    return None


def atomic_write(path: Path, payload: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target_mode = mode
    if target_mode is None and path.exists():
        target_mode = path.stat().st_mode & 0o777
    if target_mode is None:
        target_mode = 0o600

    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temp_path.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, target_mode)
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class DashboardConfig:
    listen_address: str
    port: int
    auth_file: Path
    static_dir: Path
    runtime_dir: Path
    log_dir: Path
    quantarbridge_config: Path
    dvmhost_config: Path
    dmr_gateway_config: Path
    dmr_to_p25_config: Path
    p25_to_dmr_config: Path
    rid_file: Path
    backup_dir: Path
    bm_api_key_file: Path
    location_event_dir: Path
    identity_cache_file: Path
    secure_cookies: bool
    service_units: tuple[dict[str, Any], ...]
    restart_targets: dict[str, dict[str, Any]]
    public_ars_server_address: str = ""

    @classmethod
    def load(cls, path: Path) -> "DashboardConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        base = path.parent

        def resolve(name: str, default: str) -> Path:
            value = Path(str(raw.get(name, default))).expanduser()
            return value if value.is_absolute() else (base / value).resolve()

        port = int(raw.get("port", 8088))
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")

        return cls(
            listen_address=str(raw.get("listenAddress", "127.0.0.1")),
            port=port,
            auth_file=resolve("authFile", "dashboard-auth.json"),
            static_dir=resolve("staticDir", "../../dashboard/static"),
            runtime_dir=resolve("runtimeDir", "."),
            log_dir=resolve("logDir", "log"),
            quantarbridge_config=resolve("quantarbridgeConfig", "quantarbridge.yml"),
            dvmhost_config=resolve("dvmhostConfig", "dvmhost-config.yml"),
            dmr_gateway_config=resolve("dmrGatewayConfig", "DMRGateway.ini"),
            dmr_to_p25_config=resolve(
                "dmrToP25Config", "dvmbridge-dmr-to-p25.yml"
            ),
            p25_to_dmr_config=resolve(
                "p25ToDmrConfig", "dvmbridge-p25-to-dmr.yml"
            ),
            rid_file=resolve("ridFile", "rid_acl.dat"),
            backup_dir=resolve("backupDir", "dashboard-backups"),
            bm_api_key_file=resolve("bmApiKeyFile", "bm_api.key"),
            location_event_dir=resolve("locationEventDir", "sms/processed"),
            identity_cache_file=resolve(
                "identityCacheFile", "dashboard-identity-cache.json"
            ),
            secure_cookies=bool(raw.get("secureCookies", False)),
            service_units=tuple(raw.get("serviceUnits", [])),
            restart_targets=dict(raw.get("restartTargets", {})),
            public_ars_server_address=str(
                raw.get("publicArsServerAddress", "")
            ).strip(),
        )


class AuthStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    @staticmethod
    def _password_record(password: str) -> dict[str, Any]:
        if len(password) < 12:
            raise ValueError("Das Passwort muss mindestens 12 Zeichen lang sein.")
        salt = secrets.token_bytes(24)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
        )
        return {
            "algorithm": "pbkdf2-sha256",
            "iterations": PBKDF2_ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
            "hash": base64.b64encode(digest).decode("ascii"),
            "updatedAt": utc_iso(),
        }

    def initialize(self, username: str, password: str, force: bool = False) -> None:
        username = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
            raise ValueError("Ungültiger Benutzername.")
        with self._lock:
            if self.path.exists() and not force:
                raise FileExistsError(f"Auth file already exists: {self.path}")
            payload = {
                "version": 1,
                "users": {username: self._password_record(password)},
            }
            atomic_write(
                self.path,
                (json.dumps(payload, indent=2) + "\n").encode("utf-8"),
                mode=0o600,
            )

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            raise RuntimeError(
                f"Dashboard authentication is not initialized: {self.path}"
            )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != 1 or not isinstance(payload.get("users"), dict):
            raise RuntimeError("Invalid dashboard auth file")
        return payload

    def verify(self, username: str, password: str) -> bool:
        with self._lock:
            record = self._read().get("users", {}).get(username)
            if not isinstance(record, dict):
                hashlib.pbkdf2_hmac(
                    "sha256",
                    password.encode("utf-8"),
                    b"missing-user-padding",
                    PBKDF2_ITERATIONS,
                )
                return False
            try:
                salt = base64.b64decode(record["salt"], validate=True)
                expected = base64.b64decode(record["hash"], validate=True)
                iterations = int(record["iterations"])
            except (KeyError, TypeError, ValueError):
                return False
            actual = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt, iterations
            )
            return hmac.compare_digest(actual, expected)

    def change_password(self, username: str, current: str, new: str) -> None:
        with self._lock:
            if not self.verify(username, current):
                raise PermissionError("Das aktuelle Passwort ist nicht korrekt.")
            payload = self._read()
            payload["users"][username] = self._password_record(new)
            atomic_write(
                self.path,
                (json.dumps(payload, indent=2) + "\n").encode("utf-8"),
                mode=0o600,
            )


@dataclass
class Session:
    username: str
    csrf_token: str
    created_at: float
    last_seen: float


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> tuple[str, Session]:
        now = time.time()
        token = secrets.token_urlsafe(48)
        session = Session(username, secrets.token_urlsafe(32), now, now)
        with self._lock:
            self._cleanup(now)
            self._sessions[token] = session
        return token, session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            self._cleanup(now)
            session = self._sessions.get(token)
            if session is None:
                return None
            session.last_seen = now
            return session

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_user(self, username: str, keep_token: str | None = None) -> None:
        with self._lock:
            for token in list(self._sessions):
                if token != keep_token and self._sessions[token].username == username:
                    del self._sessions[token]

    def _cleanup(self, now: float) -> None:
        for token, session in list(self._sessions.items()):
            expired = now - session.created_at > SESSION_MAX_AGE_SECONDS
            idle = now - session.last_seen > SESSION_IDLE_SECONDS
            if expired or idle:
                del self._sessions[token]


class LoginLimiter:
    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def retry_after(self, address: str) -> int:
        now = time.time()
        with self._lock:
            attempts = self._attempts[address]
            while attempts and now - attempts[0] > self.window_seconds:
                attempts.popleft()
            if len(attempts) < self.max_attempts:
                return 0
            return max(1, int(self.window_seconds - (now - attempts[0])))

    def fail(self, address: str) -> None:
        with self._lock:
            self._attempts[address].append(time.time())

    def success(self, address: str) -> None:
        with self._lock:
            self._attempts.pop(address, None)


class RidDirectory:
    def __init__(self, path: Path):
        self.path = path
        self._mtime_ns = -1
        self._entries: dict[int, str] = {}
        self._lock = threading.Lock()

    def refresh(self) -> None:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return
        if mtime_ns == self._mtime_ns:
            return
        entries: dict[int, str] = {}
        with self.path.open("r", encoding="utf-8", errors="replace", newline="") as stream:
            for row in csv.reader(stream):
                if not row:
                    continue
                try:
                    radio_id = int(row[0].strip())
                except ValueError:
                    continue
                label = row[2].strip() if len(row) > 2 else ""
                if label:
                    entries[radio_id] = label
        with self._lock:
            self._entries = entries
            self._mtime_ns = mtime_ns

    def snapshot(self) -> dict[int, str]:
        self.refresh()
        with self._lock:
            return dict(self._entries)


class IdentityDirectory:
    _radio_url = "https://radioid.net/api/dmr/user/?id={radio_id}"
    _talkgroup_url = "https://api.brandmeister.network/v2/talkgroup/{talkgroup}"

    def __init__(self, cache_path: Path, bm_api_key_file: Path):
        self.cache_path = cache_path
        self.bm_api_key_file = bm_api_key_file
        self._condition = threading.Condition(threading.RLock())
        self._radios: dict[str, dict[str, Any]] = {}
        self._talkgroups: dict[str, dict[str, Any]] = {}
        self._pending_radios: set[int] = set()
        self._pending_talkgroups: set[int] = set()
        self._retry_after: dict[tuple[str, int], float] = {}
        self._last_success: float | None = None
        self._last_error: str | None = None
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run, name="identity-directory-monitor", daemon=True
        )
        self._load()

    @staticmethod
    def _text(value: Any) -> str:
        return " ".join(str(value or "").split())

    @classmethod
    def _parse_radio_payload(
        cls, radio_id: int, payload: dict[str, Any]
    ) -> dict[str, str] | None:
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError("Invalid RadioID response")
        for entry in results:
            if not isinstance(entry, dict):
                continue
            try:
                candidate_id = int(entry.get("radio_id", entry.get("id", 0)))
            except (TypeError, ValueError):
                continue
            if candidate_id != radio_id:
                continue
            first_name = cls._text(entry.get("fname"))
            surname = cls._text(entry.get("surname"))
            full_name = " ".join(part for part in (first_name, surname) if part)
            return {
                "callsign": cls._text(entry.get("callsign")).upper(),
                "name": full_name or cls._text(entry.get("name")),
                "city": cls._text(entry.get("city")),
                "state": cls._text(entry.get("state")),
                "country": cls._text(entry.get("country")),
            }
        return None

    @classmethod
    def _parse_talkgroup_payload(
        cls, talkgroup: int, payload: dict[str, Any]
    ) -> dict[str, str] | None:
        try:
            candidate_id = int(payload.get("ID", payload.get("id", 0)))
        except (TypeError, ValueError):
            raise ValueError("Invalid BrandMeister talkgroup response") from None
        if candidate_id != talkgroup:
            return None
        name = cls._text(payload.get("Name", payload.get("name")))
        return {"name": name} if name else None

    def _load(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOG.warning("Cannot read identity cache %s: %s", self.cache_path, exc)
            return
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return

        def valid_entries(value: Any) -> dict[str, dict[str, Any]]:
            if not isinstance(value, dict):
                return {}
            entries: dict[str, dict[str, Any]] = {}
            for raw_id, raw_entry in value.items():
                try:
                    item_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if item_id <= 0 or not isinstance(raw_entry, dict):
                    continue
                entries[str(item_id)] = dict(raw_entry)
            return entries

        with self._condition:
            self._radios = valid_entries(payload.get("radios"))
            self._talkgroups = valid_entries(payload.get("talkgroups"))
            try:
                self._last_success = float(payload.get("savedAt", 0)) or None
            except (TypeError, ValueError):
                self._last_success = None

    def _save(self) -> None:
        with self._condition:
            payload = {
                "version": 1,
                "savedAt": self._last_success or time.time(),
                "radios": self._radios,
                "talkgroups": self._talkgroups,
            }
        try:
            atomic_write(
                self.cache_path,
                (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                    "utf-8"
                ),
            )
        except OSError as exc:
            LOG.warning("Cannot write identity cache %s: %s", self.cache_path, exc)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._thread.join(timeout=3)

    @staticmethod
    def _needs_refresh(entry: dict[str, Any] | None, now: float) -> bool:
        if not entry:
            return True
        try:
            updated_at = float(entry.get("updatedAt", 0))
        except (TypeError, ValueError):
            return True
        ttl = (
            IDENTITY_NEGATIVE_REFRESH_SECONDS
            if entry.get("notFound")
            else IDENTITY_REFRESH_SECONDS
        )
        return now - updated_at >= ttl

    def observe(self, status: dict[str, Any]) -> None:
        radio_ids: set[int] = set()
        talkgroups: set[int] = set()
        for radio in status.get("radios", []):
            try:
                radio_ids.add(int(radio.get("id", 0)))
            except (AttributeError, TypeError, ValueError):
                continue
        for call in [
            *status.get("activeCalls", []),
            *status.get("recentCalls", []),
        ]:
            try:
                radio_ids.add(int(call.get("sourceId", 0)))
                talkgroups.add(
                    int(call.get("mappedTalkgroup") or call.get("talkgroup", 0))
                )
            except (AttributeError, TypeError, ValueError):
                continue
        subscriptions = status.get("talkgroups", {})
        for category in ("static", "dynamic", "timed"):
            for entry in subscriptions.get(category, []):
                try:
                    talkgroups.add(int(entry.get("talkgroup", 0)))
                except (AttributeError, TypeError, ValueError):
                    continue
        connection = status.get("connection", {})
        for entry in connection.get("talkgroupMappings", []):
            try:
                talkgroups.add(int(entry.get("brandmeister", 0)))
            except (AttributeError, TypeError, ValueError):
                continue

        radio_ids.discard(0)
        talkgroups.discard(0)
        now = time.time()
        queued = False
        with self._condition:
            for radio_id in radio_ids:
                if self._needs_refresh(self._radios.get(str(radio_id)), now):
                    self._pending_radios.add(radio_id)
                    queued = True
            for talkgroup in talkgroups:
                if self._needs_refresh(self._talkgroups.get(str(talkgroup)), now):
                    self._pending_talkgroups.add(talkgroup)
                    queued = True
            if queued:
                self._condition.notify()

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            radios = {
                int(radio_id): {
                    key: value
                    for key, value in entry.items()
                    if key not in {"updatedAt", "notFound"}
                }
                for radio_id, entry in self._radios.items()
                if not entry.get("notFound")
            }
            talkgroups = {
                int(talkgroup): {
                    key: value
                    for key, value in entry.items()
                    if key not in {"updatedAt", "notFound"}
                }
                for talkgroup, entry in self._talkgroups.items()
                if not entry.get("notFound")
            }
            populated = bool(radios or talkgroups)
            status = "stale" if self._last_error and populated else (
                "error" if self._last_error else ("ok" if self._last_success else "loading")
            )
            return {
                "radios": radios,
                "talkgroups": talkgroups,
                "status": {
                    "state": status,
                    "lastUpdated": utc_iso(self._last_success)
                    if self._last_success
                    else None,
                    "error": self._last_error,
                    "radioCount": len(radios),
                    "talkgroupCount": len(talkgroups),
                },
            }

    @staticmethod
    def _read_json(request: Request) -> dict[str, Any]:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read(256 * 1024).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid directory response")
        return payload

    def _fetch_radio(self, radio_id: int) -> dict[str, str] | None:
        request = Request(
            self._radio_url.format(radio_id=radio_id),
            headers={
                "Accept": "application/json",
                "User-Agent": "QuantarBridge-Dashboard/1.2 (operator N0CALL)",
            },
        )
        return self._parse_radio_payload(radio_id, self._read_json(request))

    def _fetch_talkgroup(self, talkgroup: int) -> dict[str, str] | None:
        api_key = self.bm_api_key_file.read_text(encoding="utf-8").strip()
        if not api_key:
            raise ValueError("BrandMeister API key is missing")
        request = Request(
            self._talkgroup_url.format(talkgroup=talkgroup),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "QuantarBridge-Dashboard/1.2",
            },
        )
        try:
            payload = self._read_json(request)
        except HTTPError as exc:
            if exc.code == HTTPStatus.NOT_FOUND:
                return None
            raise
        return self._parse_talkgroup_payload(talkgroup, payload)

    def _next_job(self) -> tuple[str, int] | None:
        now = time.time()
        jobs = [
            *(('radio', item_id) for item_id in sorted(self._pending_radios)),
            *(('talkgroup', item_id) for item_id in sorted(self._pending_talkgroups)),
        ]
        for job in jobs:
            if self._retry_after.get(job, 0) <= now:
                if job[0] == "radio":
                    self._pending_radios.discard(job[1])
                else:
                    self._pending_talkgroups.discard(job[1])
                return job
        return None

    def _run(self) -> None:
        while True:
            with self._condition:
                job = self._next_job()
                while job is None and not self._stopping:
                    self._condition.wait(timeout=30)
                    job = self._next_job()
                if self._stopping:
                    return

            kind, item_id = job
            try:
                result = (
                    self._fetch_radio(item_id)
                    if kind == "radio"
                    else self._fetch_talkgroup(item_id)
                )
            except (OSError, ValueError, HTTPError, URLError, json.JSONDecodeError) as exc:
                LOG.warning("%s directory lookup for %s failed: %s", kind, item_id, exc)
                with self._condition:
                    self._last_error = f"{kind} lookup temporarily unavailable"
                    self._retry_after[(kind, item_id)] = time.time() + IDENTITY_RETRY_SECONDS
                    if kind == "radio":
                        self._pending_radios.add(item_id)
                    else:
                        self._pending_talkgroups.add(item_id)
                continue

            now = time.time()
            entry: dict[str, Any] = {"updatedAt": now}
            if result is None:
                entry["notFound"] = True
            else:
                entry.update(result)
            with self._condition:
                target = self._radios if kind == "radio" else self._talkgroups
                target[str(item_id)] = entry
                self._retry_after.pop((kind, item_id), None)
                self._last_success = now
                self._last_error = None
            self._save()
            time.sleep(IDENTITY_REQUEST_SPACING_SECONDS)


class RuntimeState:
    _time_re = re.compile(r"\b(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}\.\d{3})")
    _registration_re = re.compile(
        r"recognized Motorola SCEP ARS registration, llId = (\d+), "
        r"subscriberIp = ([^,]+), serverIp = ([^,]+)"
    )
    _refresh_re = re.compile(
        r"(?:accepted|recognized) Motorola ARS refresh.*llId = (\d+).*subscriberIp = ([^,]+)"
    )
    _tms_re = re.compile(r"Motorola TMS service acknowledged.*llId = (\d+)")
    _disconnect_re = re.compile(r"(?:DISCONNECT|deregistration).*llId = (\d+)", re.IGNORECASE)
    _gps_request_re = re.compile(r"sending Motorola LRRP .*request, llId = (\d+)")
    _gps_fix_re = re.compile(r"Motorola LRRP location report received, sourceRid = (\d+)")
    _gps_no_fix_re = re.compile(
        r"Motorola LRRP (?:response has no usable position|request was rejected).*sourceRid = (\d+)"
    )
    _source_re = re.compile(r"(?:sourceRid|llId) = (\d+)")
    _call_start_re = re.compile(
        r"P25 (RF|Net) (?:RF|network|Net) voice transmission from (\d+) to TG (\d+)"
    )
    _call_end_re = re.compile(
        r"P25 (RF|Net) (?:RF|network|Net) end of transmission(?:, ([0-9.]+) seconds)?"
    )
    _rssi_sample_re = re.compile(
        r"Quantar V\.24 RSSI sample, ldu = 1, rssi1 = (\d+)"
    )
    _dynamic_update_re = re.compile(
        r"(?:Updated|Refreshed) dynamic TG (\d+) from (?:RF|BrandMeister) activity"
    )
    _dynamic_end_re = re.compile(
        r"Dynamic TG (\d+) (?:expired locally|fully released)"
    )
    _downlink_start_re = re.compile(
        r"Forwarding BrandMeister DMR to FNE srcId=(\d+) dstId=(\d+)"
    )
    _downlink_terminator_re = re.compile(
        r"Flushing delayed BM DMR terminator to FNE srcId=(\d+) dstId=(\d+)"
    )

    def __init__(self):
        self._lock = threading.RLock()
        self._radios: dict[int, dict[str, Any]] = {}
        self._active_calls: dict[str, dict[str, Any]] = {}
        self._call_history: deque[dict[str, Any]] = deque(maxlen=80)
        self._rssi_samples: deque[tuple[float, int]] = deque(
            maxlen=RSSI_SAMPLE_HISTORY_LIMIT
        )
        self._last_call_activity = 0.0
        self._services: list[dict[str, Any]] = []
        self._bm_state = "unknown"
        self._bm_last_change: float | None = None
        self._mappings: list[dict[str, int]] = []
        self._configured_ars_server_address = ""
        self._observed_ars_server_address = ""
        self._positions: dict[int, dict[str, Any]] = {}
        self._dynamic_activity: dict[int, float] = {}
        self._dynamic_timeout_seconds = 600
        self._bm_subscriptions: dict[str, list[dict[str, int]]] = {
            "static": [],
            "dynamic": [],
            "timed": [],
        }
        self._bm_profile_updated_at: float | None = None
        self._bm_profile_error: str | None = None

    @classmethod
    def _timestamp(cls, line: str) -> float:
        match = cls._time_re.search(line)
        if not match:
            return time.time()
        value = datetime.strptime(
            f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M:%S.%f"
        )
        return value.astimezone().timestamp()

    def set_mappings(self, mappings: Iterable[dict[str, int]]) -> None:
        with self._lock:
            self._mappings = [dict(entry) for entry in mappings]

    def set_connection_config(
        self, mappings: Iterable[dict[str, int]], ars_server_address: str
    ) -> None:
        with self._lock:
            self._mappings = [dict(entry) for entry in mappings]
            self._configured_ars_server_address = str(ars_server_address).strip()

    def set_talkgroup_config(self, dynamic_timeout_seconds: int) -> None:
        with self._lock:
            self._dynamic_timeout_seconds = max(1, int(dynamic_timeout_seconds))

    def update_position(
        self, radio_id: int, latitude: float, longitude: float, timestamp: float
    ) -> None:
        with self._lock:
            previous = self._positions.get(radio_id)
            if previous and float(previous["updatedAt"]) > timestamp:
                return
            self._positions[radio_id] = {
                "latitude": latitude,
                "longitude": longitude,
                "updatedAt": timestamp,
            }

    @staticmethod
    def _normalize_subscriptions(entries: Any) -> list[dict[str, int]]:
        if not isinstance(entries, list):
            return []
        normalized: dict[tuple[int, int], dict[str, int]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                talkgroup = int(entry.get("talkgroup", 0))
                slot = int(entry.get("slot", 0))
            except (TypeError, ValueError):
                continue
            if talkgroup <= 0 or slot not in (0, 1, 2):
                continue
            normalized[(talkgroup, slot)] = {"talkgroup": talkgroup, "slot": slot}
        return sorted(normalized.values(), key=lambda item: (item["slot"], item["talkgroup"]))

    def set_brandmeister_profile(self, profile: dict[str, Any]) -> None:
        with self._lock:
            self._bm_subscriptions = {
                "static": self._normalize_subscriptions(
                    profile.get("staticSubscriptions")
                ),
                "dynamic": self._normalize_subscriptions(
                    profile.get("dynamicSubscriptions")
                ),
                "timed": self._normalize_subscriptions(
                    profile.get("timedSubscriptions")
                ),
            }
            self._bm_profile_updated_at = time.time()
            self._bm_profile_error = None

    def set_brandmeister_profile_error(self, message: str) -> None:
        with self._lock:
            self._bm_profile_error = message

    def reset_radios(self, timestamp: float) -> None:
        with self._lock:
            self._radios.clear()
            for direction in list(self._active_calls):
                self._finish_call(direction, timestamp, reason="host_restart")

    def process_dvmhost_line(self, line: str) -> None:
        timestamp = self._timestamp(line)
        with self._lock:
            match = self._rssi_sample_re.search(line)
            if match:
                self._record_rssi_sample(timestamp, int(match.group(1)))
                return

            if "Motorola LRRP Initial Delay:" in line:
                self.reset_radios(timestamp)
                return

            match = self._registration_re.search(line)
            if match:
                radio_id = int(match.group(1))
                server_ip = match.group(3).strip()
                if server_ip:
                    self._observed_ars_server_address = server_ip
                previous = self._radios.get(radio_id, {})
                self._radios[radio_id] = {
                    **previous,
                    "id": radio_id,
                    "registered": True,
                    "subscriberIp": match.group(2).strip(),
                    "serverIp": server_ip,
                    "registeredAt": previous.get("registeredAt", timestamp),
                    "lastSeen": timestamp,
                    "tms": previous.get("tms", False),
                    "gpsStatus": previous.get("gpsStatus", "waiting"),
                    "gpsLastAt": previous.get("gpsLastAt"),
                }
                return

            match = self._refresh_re.search(line)
            if match:
                radio_id = int(match.group(1))
                previous = self._radios.get(radio_id, {})
                self._radios[radio_id] = {
                    **previous,
                    "id": radio_id,
                    "registered": True,
                    "subscriberIp": match.group(2).strip(),
                    "serverIp": previous.get("serverIp", ""),
                    "registeredAt": previous.get("registeredAt", timestamp),
                    "lastSeen": timestamp,
                    "tms": previous.get("tms", False),
                    "gpsStatus": previous.get("gpsStatus", "waiting"),
                    "gpsLastAt": previous.get("gpsLastAt"),
                }
                return

            match = self._tms_re.search(line)
            if match:
                radio = self._radios.get(int(match.group(1)))
                if radio:
                    radio["tms"] = True
                    radio["lastSeen"] = timestamp
                return

            match = self._disconnect_re.search(line)
            if match:
                self._radios.pop(int(match.group(1)), None)
                return

            match = self._gps_request_re.search(line)
            if match:
                radio = self._radios.get(int(match.group(1)))
                if radio:
                    radio["gpsStatus"] = "querying"
                return

            match = self._gps_fix_re.search(line)
            if match:
                radio = self._radios.get(int(match.group(1)))
                if radio:
                    radio["gpsStatus"] = "fix"
                    radio["gpsLastAt"] = timestamp
                    radio["lastSeen"] = timestamp
                return

            match = self._gps_no_fix_re.search(line)
            if match:
                radio = self._radios.get(int(match.group(1)))
                if radio:
                    radio["gpsStatus"] = "no_fix"
                    radio["gpsLastAt"] = timestamp
                    radio["lastSeen"] = timestamp
                return

            if "Motorola TMS" in line or "Motorola LRRP" in line:
                match = self._source_re.search(line)
                if match and int(match.group(1)) in self._radios:
                    self._radios[int(match.group(1))]["lastSeen"] = timestamp

    def process_activity_line(self, line: str) -> None:
        timestamp = self._timestamp(line)
        with self._lock:
            match = self._call_start_re.search(line)
            if match:
                direction = "uplink" if match.group(1) == "RF" else "downlink"
                self._start_call(
                    direction,
                    int(match.group(2)),
                    int(match.group(3)),
                    timestamp,
                )
                return

            match = self._call_end_re.search(line)
            if match:
                direction = "uplink" if match.group(1) == "RF" else "downlink"
                parsed_duration = float(match.group(2)) if match.group(2) else None
                self._finish_call(direction, timestamp, parsed_duration=parsed_duration)

    def _start_call(
        self,
        direction: str,
        source_id: int,
        talkgroup: int,
        timestamp: float,
    ) -> None:
        self._last_call_activity = max(self._last_call_activity, timestamp)
        active = self._active_calls.get(direction)
        if (
            active
            and active["sourceId"] == source_id
            and active["talkgroup"] == talkgroup
        ):
            return
        if active:
            self._finish_call(direction, timestamp, reason="superseded")
        call = {
            "id": f"{int(timestamp * 1000)}-{direction}",
            "direction": direction,
            "sourceId": source_id,
            "talkgroup": talkgroup,
            "startedAt": timestamp,
            "endedAt": None,
            "durationSeconds": 0.0,
        }
        self._active_calls[direction] = call
        if direction == "uplink":
            self._set_call_rssi(
                call,
                self._buffered_rssi(
                    timestamp - RSSI_START_TOLERANCE_SECONDS,
                    timestamp + RSSI_START_TOLERANCE_SECONDS,
                ),
            )

    def _finish_call(
        self,
        direction: str,
        timestamp: float,
        parsed_duration: float | None = None,
        reason: str = "normal",
    ) -> None:
        self._last_call_activity = max(self._last_call_activity, timestamp)
        call = self._active_calls.pop(direction, None)
        if call is None:
            return
        call["endedAt"] = timestamp
        call["durationSeconds"] = round(
            parsed_duration
            if parsed_duration is not None
            else max(0.0, timestamp - call["startedAt"]),
            1,
        )
        call["endReason"] = reason
        if direction == "uplink":
            self._set_call_rssi(
                call,
                self._buffered_rssi(
                    call["startedAt"] - RSSI_START_TOLERANCE_SECONDS,
                    timestamp + RSSI_END_TOLERANCE_SECONDS,
                ),
            )
            self._publish_radio_rssi(call)
        self._call_history.appendleft(call)

    def _buffered_rssi(self, start: float, end: float) -> list[tuple[float, int]]:
        return [sample for sample in self._rssi_samples if start <= sample[0] <= end]

    @staticmethod
    def _set_call_rssi(
        call: dict[str, Any], readings: Iterable[tuple[float, int]]
    ) -> None:
        values = list(readings)
        call["_rssiReadings"] = values
        if not values:
            call.pop("signal", None)
            return
        raw_values = [value for _, value in values]
        call["signal"] = {
            "kind": "quantarRelative",
            "current": raw_values[-1],
            "average": round(sum(raw_values) / len(raw_values), 1),
            "minimum": min(raw_values),
            "maximum": max(raw_values),
            "samples": len(raw_values),
            "updatedAt": values[-1][0],
        }

    def _record_rssi_sample(self, timestamp: float, raw_value: int) -> None:
        if not 0 <= raw_value <= 255:
            return
        sample = (timestamp, raw_value)
        self._rssi_samples.append(sample)

        call = self._active_calls.get("uplink")
        if call and timestamp >= call["startedAt"] - RSSI_START_TOLERANCE_SECONDS:
            readings = list(call.get("_rssiReadings", []))
            readings.append(sample)
            self._set_call_rssi(call, readings)
            self._publish_radio_rssi(call)
            return

        for historic_call in self._call_history:
            if historic_call["direction"] != "uplink":
                continue
            if (
                historic_call["startedAt"] - RSSI_START_TOLERANCE_SECONDS
                <= timestamp
                <= historic_call["endedAt"] + RSSI_END_TOLERANCE_SECONDS
            ):
                readings = list(historic_call.get("_rssiReadings", []))
                readings.append(sample)
                self._set_call_rssi(historic_call, readings)
                self._publish_radio_rssi(historic_call)
                return

    def _publish_radio_rssi(self, call: dict[str, Any]) -> None:
        signal = call.get("signal")
        radio = self._radios.get(int(call["sourceId"]))
        if not signal or not radio:
            return
        radio["signal"] = dict(signal)

    def restart_guard_remaining(
        self,
        quiet_seconds: int = SETTINGS_RESTART_IDLE_GUARD_SECONDS,
        now: float | None = None,
    ) -> int:
        current = time.time() if now is None else now
        with self._lock:
            for direction, call in list(self._active_calls.items()):
                if current - call["startedAt"] > 180:
                    self._finish_call(direction, current, reason="timeout")
            if self._active_calls:
                return max(1, int(math.ceil(quiet_seconds)))
            if self._last_call_activity <= 0:
                return 0
            idle_seconds = max(0.0, current - self._last_call_activity)
            return max(0, int(math.ceil(quiet_seconds - idle_seconds)))

    def process_brandmeister_line(self, line: str) -> None:
        timestamp = self._timestamp(line)
        dynamic_update = self._dynamic_update_re.search(line)
        dynamic_end = self._dynamic_end_re.search(line)
        downlink_start = self._downlink_start_re.search(line)
        downlink_end = self._downlink_terminator_re.search(line)
        local_disconnect = (
            "Received disconnect TG" in line
            and "clearing dynamic TG state" in line
        )
        with self._lock:
            if downlink_start:
                self._start_call(
                    "downlink",
                    int(downlink_start.group(1)),
                    int(downlink_start.group(2)),
                    timestamp,
                )

            if downlink_end:
                source_id = int(downlink_end.group(1))
                talkgroup = int(downlink_end.group(2))
                active = self._active_calls.get("downlink")
                if (
                    active
                    and active["sourceId"] == source_id
                    and active["talkgroup"] == talkgroup
                    and active["startedAt"] <= timestamp
                ):
                    self._finish_call("downlink", timestamp)
                else:
                    candidates = [
                        call
                        for call in self._call_history
                        if call["direction"] == "downlink"
                        and call["sourceId"] == source_id
                        and call["talkgroup"] == talkgroup
                        and call["startedAt"] <= timestamp
                        and (
                            call.get("endedAt") is None
                            or timestamp <= call["endedAt"]
                        )
                    ]
                    if candidates:
                        call = max(candidates, key=lambda item: item["startedAt"])
                        call["endedAt"] = timestamp
                        call["durationSeconds"] = round(
                            max(0.0, timestamp - call["startedAt"]), 1
                        )
                        call["endReason"] = "normal"

            if dynamic_update:
                self._dynamic_activity[int(dynamic_update.group(1))] = timestamp
            elif dynamic_end:
                self._dynamic_activity.pop(int(dynamic_end.group(1)), None)
            elif local_disconnect:
                self._dynamic_activity.clear()
                self._finish_call("downlink", timestamp, reason="disconnect")

        state: str | None = None
        if "BrandMeister login complete" in line:
            state = "connected"
        elif "Opened BrandMeister socket" in line or "waiting for acknowledgement" in line:
            state = "connecting"
        elif re.search(
            r"BrandMeister (?:login rejected|closed the session|connection timed out)|"
            r"Unable to (?:open|resolve).*BrandMeister|BrandMeister socket read failed",
            line,
        ):
            state = "disconnected"
        if state is not None:
            with self._lock:
                self._bm_state = state
                self._bm_last_change = timestamp

    def set_services(self, services: list[dict[str, Any]]) -> None:
        with self._lock:
            self._services = services

    def snapshot(
        self,
        labels: dict[int, str],
        radio_identities: dict[int, dict[str, Any]] | None = None,
        talkgroup_identities: dict[int, dict[str, Any]] | None = None,
        directory_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        radio_identities = radio_identities or {}
        talkgroup_identities = talkgroup_identities or {}

        def identity_payload(radio_id: int, local_label: str) -> dict[str, Any]:
            remote = radio_identities.get(radio_id, {})
            callsign = str(remote.get("callsign", ""))
            name = str(remote.get("name", ""))
            location = ", ".join(
                str(remote.get(key, ""))
                for key in ("city", "state", "country")
                if remote.get(key)
            )
            return {
                "id": radio_id,
                "callsign": callsign,
                "name": name,
                "location": location,
                "localLabel": local_label,
                "displayName": callsign or name or local_label or f"RID {radio_id}",
                "resolved": bool(remote),
            }

        def talkgroup_name(talkgroup: int | None) -> str:
            if not talkgroup:
                return ""
            return str(talkgroup_identities.get(int(talkgroup), {}).get("name", ""))

        def subscription_payload(entry: dict[str, Any]) -> dict[str, Any]:
            item = dict(entry)
            item["name"] = talkgroup_name(item.get("talkgroup"))
            return item

        with self._lock:
            for direction, call in list(self._active_calls.items()):
                if now - call["startedAt"] > 180:
                    self._finish_call(direction, now, reason="timeout")

            mappings_by_p25 = {
                int(entry["p25"]): int(entry["brandmeister"])
                for entry in self._mappings
            }
            connection_mappings = [
                {
                    "p25": p25,
                    "brandmeister": brandmeister,
                    "name": talkgroup_name(brandmeister),
                }
                for p25, brandmeister in sorted(mappings_by_p25.items())
            ]
            radios = []
            for radio_id, radio in self._radios.items():
                if not radio.get("registered"):
                    continue
                if now - float(radio.get("lastSeen", 0)) > RADIO_REGISTRATION_TIMEOUT_SECONDS:
                    continue
                item = dict(radio)
                item["label"] = labels.get(radio_id, "")
                item["identity"] = identity_payload(radio_id, item["label"])
                position = self._positions.get(radio_id)
                if position:
                    item["position"] = {
                        "latitude": position["latitude"],
                        "longitude": position["longitude"],
                        "updatedAt": utc_iso(position["updatedAt"]),
                    }
                else:
                    item["position"] = None
                for key in ("registeredAt", "lastSeen", "gpsLastAt"):
                    item[key] = utc_iso(item[key]) if item.get(key) else None
                if item.get("signal"):
                    item["signal"] = dict(item["signal"])
                    item["signal"]["updatedAt"] = utc_iso(
                        item["signal"]["updatedAt"]
                    )
                radios.append(item)
            radios.sort(key=lambda item: item["id"])

            def call_payload(call: dict[str, Any]) -> dict[str, Any]:
                item = dict(call)
                item.pop("_rssiReadings", None)
                item["sourceLabel"] = labels.get(item["sourceId"], "")
                item["mappedTalkgroup"] = mappings_by_p25.get(item["talkgroup"])
                item["sourceIdentity"] = identity_payload(
                    item["sourceId"], item["sourceLabel"]
                )
                item["talkgroupName"] = talkgroup_name(
                    item["mappedTalkgroup"] or item["talkgroup"]
                )
                item["startedAt"] = utc_iso(item["startedAt"])
                item["endedAt"] = utc_iso(item["endedAt"]) if item.get("endedAt") else None
                if item.get("signal"):
                    item["signal"] = dict(item["signal"])
                    item["signal"]["updatedAt"] = utc_iso(
                        item["signal"]["updatedAt"]
                    )
                if item["endedAt"] is None:
                    item["durationSeconds"] = round(now - call["startedAt"], 1)
                return item

            active_calls = [
                call_payload(call)
                for call in sorted(
                    self._active_calls.values(), key=lambda entry: entry["startedAt"]
                )
            ]
            call_history = [call_payload(call) for call in self._call_history]
            services = [dict(entry) for entry in self._services]
            critical = [entry for entry in services if entry.get("critical", True)]
            running = sum(1 for entry in critical if entry.get("running"))
            system_state = "healthy" if critical and running == len(critical) else "degraded"

            dynamic_by_key = {
                (entry["talkgroup"], entry["slot"]): dict(entry)
                for entry in self._bm_subscriptions["dynamic"]
            }
            for talkgroup, last_active in self._dynamic_activity.items():
                if now - last_active <= self._dynamic_timeout_seconds:
                    matching_keys = [key for key in dynamic_by_key if key[0] == talkgroup]
                    if not matching_keys:
                        dynamic_by_key[(talkgroup, 0)] = {
                            "talkgroup": talkgroup,
                            "slot": 0,
                        }

            dynamic_subscriptions = []
            for entry in dynamic_by_key.values():
                item: dict[str, Any] = dict(entry)
                last_active = self._dynamic_activity.get(item["talkgroup"])
                item["lastActiveAt"] = utc_iso(last_active) if last_active else None
                item["expiresAt"] = (
                    utc_iso(last_active + self._dynamic_timeout_seconds)
                    if last_active
                    else None
                )
                item["remainingSeconds"] = (
                    max(0, int(last_active + self._dynamic_timeout_seconds - now))
                    if last_active
                    else None
                )
                item["name"] = talkgroup_name(item["talkgroup"])
                dynamic_subscriptions.append(item)
            dynamic_subscriptions.sort(key=lambda item: (item["slot"], item["talkgroup"]))

            talkgroup_count = (
                len(self._bm_subscriptions["static"])
                + len(dynamic_subscriptions)
                + len(self._bm_subscriptions["timed"])
            )

            return {
                "serverTime": utc_iso(now),
                "summary": {
                    "registeredRadios": len(radios),
                    "activeCalls": len(active_calls),
                    "runningServices": running,
                    "totalServices": len(critical),
                    "subscribedTalkgroups": talkgroup_count,
                    "systemState": system_state,
                },
                "radioRegistrationTimeoutSeconds": RADIO_REGISTRATION_TIMEOUT_SECONDS,
                "brandmeister": {
                    "state": self._bm_state,
                    "lastChange": utc_iso(self._bm_last_change)
                    if self._bm_last_change
                    else None,
                },
                "connection": {
                    "arsServerAddress": (
                        self._configured_ars_server_address
                        or self._observed_ars_server_address
                    ),
                    "talkgroupMappings": connection_mappings,
                },
                "talkgroups": {
                    "status": "stale" if self._bm_profile_error else (
                        "ok" if self._bm_profile_updated_at else "loading"
                    ),
                    "lastUpdated": utc_iso(self._bm_profile_updated_at)
                    if self._bm_profile_updated_at
                    else None,
                    "error": self._bm_profile_error,
                    "dynamicTimeoutSeconds": self._dynamic_timeout_seconds,
                    "static": [
                        subscription_payload(entry)
                        for entry in self._bm_subscriptions["static"]
                    ],
                    "dynamic": dynamic_subscriptions,
                    "timed": [
                        subscription_payload(entry)
                        for entry in self._bm_subscriptions["timed"]
                    ],
                },
                "identityDirectory": dict(directory_status or {}),
                "radios": radios,
                "activeCalls": active_calls,
                "recentCalls": call_history[:30],
                "services": services,
            }


class LogMonitor:
    def __init__(self, config: DashboardConfig, state: RuntimeState):
        self.config = config
        self.state = state
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        specs = [
            ("dvmhost-20??-??-??.log", self.state.process_dvmhost_line),
            ("dvmhost-20??-??-??.activity.log", self.state.process_activity_line),
            ("quantarbridge-20??-??-??.log", self.state.process_brandmeister_line),
        ]
        for pattern, callback in specs:
            files = sorted(self.config.log_dir.glob(pattern), key=lambda path: path.name)
            for path in files[-2:]:
                self._scan(path, callback)
            current = files[-1] if files else None
            offset = current.stat().st_size if current else 0
            thread = threading.Thread(
                target=self._follow,
                args=(pattern, callback, current, offset),
                name=f"log-{pattern}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)

    @staticmethod
    def _scan(path: Path, callback: Callable[[str], None]) -> None:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    callback(line)
        except OSError as exc:
            LOG.warning("Cannot scan %s: %s", path, exc)

    def _latest(self, pattern: str) -> Path | None:
        files = list(self.config.log_dir.glob(pattern))
        return max(files, key=lambda path: path.name) if files else None

    def _follow(
        self,
        pattern: str,
        callback: Callable[[str], None],
        current: Path | None,
        offset: int,
    ) -> None:
        stream: io.TextIOWrapper | None = None
        next_discovery = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            discover = stream is None or now >= next_discovery
            latest = self._latest(pattern) if discover else current
            if discover:
                next_discovery = now + 10.0
            if latest is None:
                self._stop.wait(0.5)
                continue
            try:
                changed = current != latest
                truncated = (
                    discover and current == latest and latest.stat().st_size < offset
                )
                if stream is None or changed or truncated:
                    if stream is not None:
                        stream.close()
                    current = latest
                    offset = 0 if changed or truncated else offset
                    stream = current.open("r", encoding="utf-8", errors="replace")
                    stream.seek(offset)
                line = stream.readline()
                if line:
                    offset = stream.tell()
                    callback(line)
                    continue
            except OSError as exc:
                LOG.warning("Log follow failed for %s: %s", pattern, exc)
                if stream is not None:
                    stream.close()
                    stream = None
            self._stop.wait(0.25)
        if stream is not None:
            stream.close()


class LocationMonitor:
    _event_time_re = re.compile(r"^host-raw-(\d{13})-")

    def __init__(self, event_dir: Path, state: RuntimeState):
        self.event_dir = event_dir
        self.state = state
        self._seen: set[str] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="location-monitor", daemon=True
        )

    def start(self) -> None:
        self._scan()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _process(self, path: Path) -> None:
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
            if event.get("application") != "motorola_lrrp":
                return
            radio_id = int(event.get("sourceRid", 0))
            position = decode_motorola_lrrp_position(
                str(event.get("rawIpPacketHex") or event.get("hexIpPacket") or "")
            )
            if radio_id <= 0 or position is None:
                return
            match = self._event_time_re.match(path.name)
            timestamp = (
                int(match.group(1)) / 1000.0 if match else path.stat().st_mtime
            )
            self.state.update_position(radio_id, position[0], position[1], timestamp)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            LOG.debug("Skipping location event %s: %s", path, exc)

    def _scan(self) -> None:
        try:
            paths = sorted(self.event_dir.glob("host-raw-*.json"), key=lambda path: path.name)
        except OSError as exc:
            LOG.warning("Cannot scan location events in %s: %s", self.event_dir, exc)
            return
        for path in paths:
            if path.name in self._seen:
                continue
            self._seen.add(path.name)
            self._process(path)

    def _run(self) -> None:
        while not self._stop.wait(5):
            self._scan()


class BrandmeisterProfileMonitor:
    def __init__(self, config: DashboardConfig, state: RuntimeState):
        self.config = config
        self.state = state
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="brandmeister-profile-monitor", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _fetch(self) -> dict[str, Any]:
        bridge = yaml.safe_load(
            self.config.quantarbridge_config.read_text(encoding="utf-8")
        )
        device_id = int(bridge.get("brandmeister", {}).get("repeaterId", 0))
        api_key = self.config.bm_api_key_file.read_text(encoding="utf-8").strip()
        if device_id <= 0 or not api_key:
            raise ValueError("BrandMeister-Geräte-ID oder API-Key fehlt")
        request = Request(
            f"https://api.brandmeister.network/v2/device/{device_id}/profile",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "quantar-dashboard/1.1",
            },
        )
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read(512 * 1024).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Ungültige Antwort der BrandMeister-API")
        return payload

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.state.set_brandmeister_profile(self._fetch())
            except (OSError, ValueError, HTTPError, URLError, json.JSONDecodeError) as exc:
                LOG.warning("BrandMeister profile refresh failed: %s", exc)
                self.state.set_brandmeister_profile_error(
                    "BrandMeister-Profil ist vorübergehend nicht erreichbar."
                )
            self._stop.wait(BM_PROFILE_REFRESH_SECONDS)


class ServiceMonitor:
    def __init__(self, config: DashboardConfig, state: RuntimeState):
        self.config = config
        self.state = state
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="service-monitor", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    @staticmethod
    def _find_process(match: str) -> int | None:
        proc = Path("/proc")
        if not proc.exists() or not match:
            return None
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                    "utf-8", errors="replace"
                )
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if match in command:
                return int(entry.name)
        return None

    @staticmethod
    def _unit_status(unit: str) -> dict[str, str]:
        if not unit:
            return {}
        try:
            result = subprocess.run(
                [
                    "systemctl",
                    "show",
                    unit,
                    "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp",
                    "--no-pager",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        return values

    def collect(self) -> list[dict[str, Any]]:
        services: list[dict[str, Any]] = []
        for spec in self.config.service_units:
            values = self._unit_status(str(spec.get("unit", "")))
            pid = int(values.get("MainPID", "0") or 0)
            running = values.get("ActiveState") == "active" and pid > 0
            source = "systemd"
            if not running and spec.get("processMatch"):
                matched_pid = self._find_process(str(spec["processMatch"]))
                if matched_pid:
                    pid = matched_pid
                    running = True
                    source = "process"
            services.append(
                {
                    "id": str(spec.get("id", spec.get("unit", "service"))),
                    "label": str(spec.get("label", spec.get("unit", "Dienst"))),
                    "running": running,
                    "state": "running" if running else values.get("ActiveState", "unknown"),
                    "subState": values.get("SubState", ""),
                    "pid": pid or None,
                    "startedAt": values.get("ExecMainStartTimestamp") or None,
                    "source": source,
                    "critical": bool(spec.get("critical", True)),
                }
            )
        return services

    def _run(self) -> None:
        while not self._stop.is_set():
            self.state.set_services(self.collect())
            self._stop.wait(10)


class RestartCoordinator:
    def __init__(self, targets: dict[str, dict[str, Any]]):
        self.targets = targets

    def restart(self, names: Iterable[str]) -> list[str]:
        restarted: list[str] = []
        for name in names:
            spec = self.targets.get(name)
            if not spec:
                raise RuntimeError(f"Restart target is not configured: {name}")
            target_type = spec.get("type")
            if target_type == "systemd":
                self._restart_systemd(str(spec["unit"]))
            elif target_type == "process":
                self._restart_process(spec)
            else:
                raise RuntimeError(f"Unsupported restart target type: {target_type}")
            restarted.append(name)
        return restarted

    @staticmethod
    def _main_pid(unit: str) -> int:
        result = subprocess.run(
            ["systemctl", "show", unit, "--property=MainPID", "--value"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        try:
            return int(result.stdout.strip())
        except ValueError:
            return 0

    def _restart_systemd(self, unit: str) -> None:
        try:
            result = subprocess.run(
                ["sudo", "-n", "/usr/bin/systemctl", "restart", unit],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
            if result.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass

        old_pid = self._main_pid(unit)
        if old_pid <= 0:
            raise RuntimeError(f"{unit} has no running process")
        proc_path = Path(f"/proc/{old_pid}")
        if not proc_path.exists() or proc_path.stat().st_uid != os.getuid():
            raise PermissionError(f"Cannot restart {unit} without service permission")
        os.kill(old_pid, signal.SIGKILL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(0.5)
            new_pid = self._main_pid(unit)
            if new_pid > 0 and new_pid != old_pid and Path(f"/proc/{new_pid}").exists():
                return
        raise RuntimeError(f"{unit} did not return after restart")

    @staticmethod
    def _matching_pids(match: str) -> list[int]:
        matches: list[int] = []
        proc = Path("/proc")
        if not proc.exists():
            return matches
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                    "utf-8", errors="replace"
                )
                owner = entry.stat().st_uid
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if owner == os.getuid() and match in command:
                matches.append(int(entry.name))
        return matches

    def _restart_process(self, spec: dict[str, Any]) -> None:
        match = str(spec["match"])
        for pid in self._matching_pids(match):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
        deadline = time.monotonic() + 6
        while self._matching_pids(match) and time.monotonic() < deadline:
            time.sleep(0.2)
        for pid in self._matching_pids(match):
            os.kill(pid, signal.SIGKILL)

        transient_prefix = str(spec.get("transientUnitPrefix", "")).strip()
        if transient_prefix:
            unit_name = f"{transient_prefix}-{int(time.time())}"
            command = [
                "systemd-run",
                "--user",
                f"--unit={unit_name}",
                "--collect",
                "--property=Restart=on-failure",
                "--property=RestartSec=3s",
                f"--property=WorkingDirectory={spec.get('workingDirectory') or '/'}",
                *[str(part) for part in spec["command"]],
            ]
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                if result.returncode == 0:
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        if self._matching_pids(match):
                            return
                        time.sleep(0.25)
            except (OSError, subprocess.TimeoutExpired):
                LOG.warning("Transient user unit could not be started; using process fallback")

        log_path = Path(str(spec["logFile"]))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab", buffering=0) as output:
            process = subprocess.Popen(
                [str(part) for part in spec["command"]],
                cwd=str(spec.get("workingDirectory") or "/"),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        time.sleep(1.5)
        if process.poll() is not None:
            raise RuntimeError(
                f"Managed process exited during restart with code {process.returncode}"
            )


class SettingsManager:
    _AUDIO_DEFAULTS: dict[str, Any] = {
        "rxAudioGain": 1.0,
        "vocoderDecoderAudioGain": 3.0,
        "vocoderDecoderAutoGain": False,
        "txAudioGain": 1.0,
        "vocoderEncoderAudioGain": 3.0,
    }
    _P25_AUDIO_DEFAULTS: dict[str, Any] = {
        "p25EncodePresenceGain": 0.0,
        "p25EncodeAgc": False,
        "p25EncodeAgcTargetRms": 6500.0,
        "p25EncodeAgcMinGain": 0.55,
        "p25EncodeAgcMaxGain": 1.9,
        "p25EncodeAgcAttack": 0.4,
        "p25EncodeAgcRelease": 0.06,
        "p25EncodeAgcPeakLimit": 26000.0,
    }

    def __init__(
        self,
        config: DashboardConfig,
        state: RuntimeState,
        restarter: RestartCoordinator,
    ):
        self.config = config
        self.state = state
        self.restarter = restarter
        self._lock = threading.RLock()

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid YAML document: {path}")
        return payload

    @classmethod
    def _read_audio(cls, path: Path, p25_output: bool) -> dict[str, Any]:
        document = cls._read_yaml(path)
        system = document.get("system", {})
        if not isinstance(system, dict):
            raise ValueError(f"Invalid system section: {path}")
        defaults = dict(cls._AUDIO_DEFAULTS)
        if p25_output:
            defaults.update(cls._P25_AUDIO_DEFAULTS)
        values: dict[str, Any] = {}
        for key, default in defaults.items():
            raw = system.get(key, default)
            if isinstance(default, bool):
                if not isinstance(raw, bool):
                    raise ValueError(f"Invalid boolean audio value {key}: {path}")
                values[key] = raw
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid audio value {key}: {path}") from exc
            if not math.isfinite(value):
                raise ValueError(f"Invalid audio value {key}: {path}")
            values[key] = value
        return values

    def read(self) -> dict[str, Any]:
        with self._lock:
            bridge = self._read_yaml(self.config.quantarbridge_config)
            host = self._read_yaml(self.config.dvmhost_config)
            dmr_to_p25_audio = self._read_audio(
                self.config.dmr_to_p25_config, p25_output=True
            )
            p25_to_dmr_audio = self._read_audio(
                self.config.p25_to_dmr_config, p25_output=False
            )
            bm = bridge.get("brandmeister", {})
            routing = bridge.get("routing", {})
            p25 = host.get("protocols", {}).get("p25", {})
            location = p25.get("motorolaLocation", {})
            packet_data = p25.get("motorolaPacketData", {})
            mappings = [
                {
                    "p25": int(entry.get("p25", 0)),
                    "brandmeister": int(entry.get("brandmeister", 0)),
                }
                for entry in routing.get("talkgroupMappings", [])
                if isinstance(entry, dict)
            ]
            self.state.set_connection_config(
                mappings,
                str(packet_data.get("arsServerAddress", "")).strip()
                or self.config.public_ars_server_address,
            )
            self.state.set_talkgroup_config(
                int(routing.get("dynamicTimeoutSeconds", 600))
            )
            version_source = (
                self.config.quantarbridge_config.read_bytes()
                + self.config.dvmhost_config.read_bytes()
                + self.config.dmr_to_p25_config.read_bytes()
                + self.config.p25_to_dmr_config.read_bytes()
            )
            return {
                "version": hashlib.sha256(version_source).hexdigest()[:16],
                "repeaterId": int(bm.get("repeaterId", 0)),
                "brandmeisterCallsign": str(bm.get("callsign", "")),
                "brandmeisterTimeslot": int(
                    bm.get("timeslot", 1 if bm.get("slot1", False) else 2)
                ),
                "brandmeisterRxFrequency": int(bm.get("rxFrequency", 0)),
                "brandmeisterTxFrequency": int(bm.get("txFrequency", 0)),
                "brandmeisterAddress": str(bm.get("address", "")),
                "brandmeisterPasswordConfigured": bool(bm.get("password")),
                "dynamicTimeoutSeconds": int(
                    routing.get("dynamicTimeoutSeconds", 600)
                ),
                "talkgroupMappings": mappings,
                "staticTalkgroups": [int(value) for value in routing.get("staticTalkgroups", [])],
                "gps": {
                    "initialDelaySeconds": int(location.get("initialDelaySeconds", 5)),
                    "updateIntervalSeconds": int(location.get("updateIntervalSeconds", 300)),
                    "noFixRetrySeconds": int(location.get("noFixRetrySeconds", 60)),
                },
                "audio": {
                    "dmrToP25": dmr_to_p25_audio,
                    "p25ToDmr": p25_to_dmr_audio,
                },
            }

    @staticmethod
    def _validate_int(
        value: Any, field: str, minimum: int, maximum: int
    ) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{field} ist ungültig.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} ist ungültig.") from exc
        if not minimum <= parsed <= maximum:
            raise ValueError(f"{field} muss zwischen {minimum} und {maximum} liegen.")
        return parsed

    @staticmethod
    def _validate_float(
        value: Any, field: str, minimum: float, maximum: float
    ) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{field} ist ungültig.")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} ist ungültig.") from exc
        if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
            raise ValueError(
                f"{field} muss zwischen {minimum:g} und {maximum:g} liegen."
            )
        return round(parsed, 4)

    @staticmethod
    def _validate_bool(value: Any, field: str) -> bool:
        if not isinstance(value, bool):
            raise ValueError(f"{field} ist ungültig.")
        return value

    @classmethod
    def _validate_audio_direction(
        cls, payload: Any, label: str, p25_output: bool
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(f"Audio {label} ist ungültig.")
        values: dict[str, Any] = {
            "rxAudioGain": cls._validate_float(
                payload.get("rxAudioGain"), f"{label}: Eingangspegel", 0.0, 5.0
            ),
            "vocoderDecoderAudioGain": cls._validate_float(
                payload.get("vocoderDecoderAudioGain"),
                f"{label}: Decoder-Pegel",
                0.0,
                5.0,
            ),
            "vocoderDecoderAutoGain": cls._validate_bool(
                payload.get("vocoderDecoderAutoGain"),
                f"{label}: Decoder-Automatik",
            ),
            "txAudioGain": cls._validate_float(
                payload.get("txAudioGain"), f"{label}: Ausgangspegel", 0.0, 5.0
            ),
            "vocoderEncoderAudioGain": cls._validate_float(
                payload.get("vocoderEncoderAudioGain"),
                f"{label}: Encoder-Pegel",
                0.0,
                5.0,
            ),
        }
        if not p25_output:
            return values

        values.update(
            {
                "p25EncodePresenceGain": cls._validate_float(
                    payload.get("p25EncodePresenceGain"),
                    f"{label}: Sprachpräsenz",
                    0.0,
                    0.8,
                ),
                "p25EncodeAgc": cls._validate_bool(
                    payload.get("p25EncodeAgc"), f"{label}: P25-Pegelautomatik"
                ),
                "p25EncodeAgcTargetRms": cls._validate_float(
                    payload.get("p25EncodeAgcTargetRms"),
                    f"{label}: AGC-Zielpegel",
                    1000.0,
                    12000.0,
                ),
                "p25EncodeAgcMinGain": cls._validate_float(
                    payload.get("p25EncodeAgcMinGain"),
                    f"{label}: minimale AGC-Verstärkung",
                    0.1,
                    1.0,
                ),
                "p25EncodeAgcMaxGain": cls._validate_float(
                    payload.get("p25EncodeAgcMaxGain"),
                    f"{label}: maximale AGC-Verstärkung",
                    0.1,
                    4.0,
                ),
                "p25EncodeAgcAttack": cls._validate_float(
                    payload.get("p25EncodeAgcAttack"),
                    f"{label}: AGC-Anregelung",
                    0.01,
                    1.0,
                ),
                "p25EncodeAgcRelease": cls._validate_float(
                    payload.get("p25EncodeAgcRelease"),
                    f"{label}: AGC-Rückregelung",
                    0.01,
                    1.0,
                ),
                "p25EncodeAgcPeakLimit": cls._validate_float(
                    payload.get("p25EncodeAgcPeakLimit"),
                    f"{label}: Spitzenbegrenzung",
                    8000.0,
                    32000.0,
                ),
            }
        )
        if values["p25EncodeAgcMaxGain"] < values["p25EncodeAgcMinGain"]:
            raise ValueError(
                f"{label}: Die maximale AGC-Verstärkung darf nicht kleiner als die minimale sein."
            )
        return values

    def _validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        required_network_fields = {
            "brandmeisterCallsign",
            "brandmeisterTimeslot",
            "brandmeisterRxFrequency",
            "brandmeisterTxFrequency",
        }
        if not required_network_fields.issubset(payload):
            raise ValueError(
                "Die Browseroberfläche ist veraltet. Bitte die Seite vollständig neu laden."
            )
        repeater_id = self._validate_int(
            payload.get("repeaterId"), "Repeater-ID", 1, 4_294_967_295
        )
        callsign_value = payload.get("brandmeisterCallsign", "")
        if not isinstance(callsign_value, str):
            raise ValueError("Das BrandMeister-Rufzeichen ist ungültig.")
        callsign = callsign_value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9/-]{2,7}", callsign):
            raise ValueError("Das BrandMeister-Rufzeichen ist ungültig.")
        timeslot = self._validate_int(
            payload.get("brandmeisterTimeslot"), "BrandMeister-Zeitschlitz", 1, 2
        )
        rx_frequency = self._validate_int(
            payload.get("brandmeisterRxFrequency"), "Repeater-RX-Frequenz", 10_000_000, 999_999_999
        )
        tx_frequency = self._validate_int(
            payload.get("brandmeisterTxFrequency"), "Repeater-TX-Frequenz", 10_000_000, 999_999_999
        )
        password = payload.get("brandmeisterPassword", "")
        if password is None:
            password = ""
        if (
            not isinstance(password, str)
            or len(password) > 128
            or any(character in password for character in "\r\n\0")
        ):
            raise ValueError("Das BrandMeister-Passwort ist ungültig.")

        dynamic_timeout_seconds = self._validate_int(
            payload.get("dynamicTimeoutSeconds"),
            "Ablaufzeit dynamischer Talkgroups",
            10,
            86_400,
        )

        raw_mappings = payload.get("talkgroupMappings")
        if not isinstance(raw_mappings, list) or not 1 <= len(raw_mappings) <= 64:
            raise ValueError("Es muss mindestens eine Talkgroup-Zuordnung geben.")
        mappings: list[dict[str, int]] = []
        seen_p25: set[int] = set()
        seen_bm: set[int] = set()
        for index, raw in enumerate(raw_mappings, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"Talkgroup-Zuordnung {index} ist ungültig.")
            p25 = self._validate_int(raw.get("p25"), f"P25-TG {index}", 1, 16_777_215)
            bm = self._validate_int(
                raw.get("brandmeister"), f"BrandMeister-TG {index}", 1, 16_777_215
            )
            if p25 in seen_p25 or bm in seen_bm:
                raise ValueError("Talkgroup-Zuordnungen müssen eindeutig sein.")
            seen_p25.add(p25)
            seen_bm.add(bm)
            mappings.append({"p25": p25, "brandmeister": bm})

        gps = payload.get("gps")
        if not isinstance(gps, dict):
            raise ValueError("GPS-Abfrage ist ungültig.")
        gps_values = {
            "initialDelaySeconds": self._validate_int(
                gps.get("initialDelaySeconds"), "GPS-Startverzögerung", 1, 300
            ),
            "updateIntervalSeconds": self._validate_int(
                gps.get("updateIntervalSeconds"), "GPS-Aktualisierung", 30, 86_400
            ),
            "noFixRetrySeconds": self._validate_int(
                gps.get("noFixRetrySeconds"), "GPS-Wiederholung", 15, 3_600
            ),
        }
        audio = payload.get("audio")
        if not isinstance(audio, dict):
            raise ValueError("Audioeinstellungen sind ungültig.")
        audio_values = {
            "dmrToP25": self._validate_audio_direction(
                audio.get("dmrToP25"), "DMR nach P25", p25_output=True
            ),
            "p25ToDmr": self._validate_audio_direction(
                audio.get("p25ToDmr"), "P25 nach DMR", p25_output=False
            ),
        }
        return {
            "repeaterId": repeater_id,
            "brandmeisterCallsign": callsign,
            "brandmeisterTimeslot": timeslot,
            "brandmeisterRxFrequency": rx_frequency,
            "brandmeisterTxFrequency": tx_frequency,
            "brandmeisterPassword": password,
            "dynamicTimeoutSeconds": dynamic_timeout_seconds,
            "talkgroupMappings": mappings,
            "gps": gps_values,
            "audio": audio_values,
        }

    @staticmethod
    def _section_bounds(lines: list[str], section: str) -> tuple[int, int]:
        start = -1
        for index, line in enumerate(lines):
            if line.rstrip("\r\n") == f"{section}:":
                start = index
                break
        if start < 0:
            raise ValueError(f"Missing YAML section: {section}")
        end = len(lines)
        for index in range(start + 1, len(lines)):
            stripped = lines[index].strip()
            if stripped and not lines[index][0].isspace() and not stripped.startswith("#"):
                end = index
                break
        return start, end

    @classmethod
    def _replace_section_scalar(
        cls, text: str, section: str, key: str, value: str
    ) -> str:
        lines = text.splitlines(keepends=True)
        start, end = cls._section_bounds(lines, section)
        for index in range(start + 1, end):
            if re.match(rf"^  {re.escape(key)}\s*:", lines[index]):
                ending = "\r\n" if lines[index].endswith("\r\n") else "\n"
                lines[index] = f"  {key}: {value}{ending}"
                return "".join(lines)
        raise ValueError(f"Missing YAML field: {section}.{key}")

    @classmethod
    def _upsert_section_scalar(
        cls, text: str, section: str, key: str, value: str
    ) -> str:
        lines = text.splitlines(keepends=True)
        start, end = cls._section_bounds(lines, section)
        for index in range(start + 1, end):
            if re.match(rf"^  {re.escape(key)}\s*:", lines[index]):
                ending = "\r\n" if lines[index].endswith("\r\n") else "\n"
                lines[index] = f"  {key}: {value}{ending}"
                return "".join(lines)
        newline = "\r\n" if "\r\n" in text else "\n"
        lines.insert(end, f"  {key}: {value}{newline}")
        return "".join(lines)

    @staticmethod
    def _yaml_scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            rendered = f"{float(value):.4f}".rstrip("0").rstrip(".")
            return f"{rendered}.0" if "." not in rendered else rendered
        raise TypeError(f"Unsupported YAML scalar: {type(value).__name__}")

    @classmethod
    def _replace_mapping_block(
        cls, text: str, mappings: list[dict[str, int]]
    ) -> str:
        lines = text.splitlines(keepends=True)
        start, section_end = cls._section_bounds(lines, "routing")
        key_index = -1
        for index in range(start + 1, section_end):
            if re.match(r"^  talkgroupMappings\s*:", lines[index]):
                key_index = index
                break
        if key_index < 0:
            raise ValueError("Missing YAML field: routing.talkgroupMappings")
        block_end = key_index + 1
        while block_end < section_end:
            line = lines[block_end]
            if not line.strip():
                break
            indent = len(line) - len(line.lstrip(" "))
            if indent <= 2:
                break
            block_end += 1
        newline = "\r\n" if "\r\n" in text else "\n"
        replacement = [f"  talkgroupMappings:{newline}"]
        for mapping in mappings:
            replacement.append(f"    - p25: {mapping['p25']}{newline}")
            replacement.append(
                f"      brandmeister: {mapping['brandmeister']}{newline}"
            )
        lines[key_index:block_end] = replacement
        return "".join(lines)

    @staticmethod
    def _replace_indented_scalar(text: str, key: str, value: int) -> str:
        lines = text.splitlines(keepends=True)
        matches = [
            index
            for index, line in enumerate(lines)
            if re.match(rf"^      {re.escape(key)}\s*:", line)
        ]
        if len(matches) != 1:
            raise ValueError(f"Expected one YAML field named {key}, found {len(matches)}")
        index = matches[0]
        ending = "\r\n" if lines[index].endswith("\r\n") else "\n"
        lines[index] = f"      {key}: {value}{ending}"
        return "".join(lines)

    @staticmethod
    def _write_ini_password(path: Path, password: str) -> bytes:
        text = path.read_text(encoding="utf-8")
        parser = configparser.RawConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        parser.read_string(text)
        section = "DMR Network 1"
        if not parser.has_section(section):
            raise ValueError(f"Missing [{section}] in {path}")
        lines = text.splitlines(keepends=True)
        start = next(
            (index for index, line in enumerate(lines) if line.strip() == f"[{section}]"),
            -1,
        )
        if start < 0:
            raise ValueError(f"Missing [{section}] in {path}")
        end = next(
            (
                index
                for index in range(start + 1, len(lines))
                if lines[index].lstrip().startswith("[")
            ),
            len(lines),
        )
        for index in range(start + 1, end):
            if re.match(r"^Password\s*=", lines[index], re.IGNORECASE):
                ending = "\r\n" if lines[index].endswith("\r\n") else "\n"
                lines[index] = f"Password={password}{ending}"
                return "".join(lines).encode("utf-8")
        raise ValueError(f"Missing Password in [{section}] in {path}")

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = self._validate(payload)
        with self._lock:
            bridge = self._read_yaml(self.config.quantarbridge_config)
            host = self._read_yaml(self.config.dvmhost_config)
            current_dmr_to_p25_audio = self._read_audio(
                self.config.dmr_to_p25_config, p25_output=True
            )
            current_p25_to_dmr_audio = self._read_audio(
                self.config.p25_to_dmr_config, p25_output=False
            )
            bm = bridge.setdefault("brandmeister", {})
            routing = bridge.setdefault("routing", {})
            location = (
                host.setdefault("protocols", {})
                .setdefault("p25", {})
                .setdefault("motorolaLocation", {})
            )

            current_id = int(bm.get("repeaterId", 0))
            current_callsign = str(bm.get("callsign", "")).strip().upper()
            current_timeslot = int(
                bm.get("timeslot", 1 if bm.get("slot1", False) else 2)
            )
            current_slot1 = bool(bm.get("slot1", False))
            current_slot2 = bool(bm.get("slot2", False))
            sms = bridge.get("sms", {})
            current_sms_slot = int(sms.get("bmSlot", current_timeslot)) if isinstance(sms, dict) else current_timeslot
            timeslot_config_changed = (
                "timeslot" not in bm
                or current_timeslot != values["brandmeisterTimeslot"]
                or current_slot1 != (values["brandmeisterTimeslot"] == 1)
                or current_slot2 != (values["brandmeisterTimeslot"] == 2)
                or (isinstance(sms, dict) and "bmSlot" in sms and current_sms_slot != values["brandmeisterTimeslot"])
            )
            current_rx_frequency = int(bm.get("rxFrequency", 0))
            current_tx_frequency = int(bm.get("txFrequency", 0))
            current_dynamic_timeout = int(
                routing.get("dynamicTimeoutSeconds", 600)
            )
            current_mappings = [
                {
                    "p25": int(entry.get("p25", 0)),
                    "brandmeister": int(entry.get("brandmeister", 0)),
                }
                for entry in routing.get("talkgroupMappings", [])
                if isinstance(entry, dict)
            ]
            current_gps = {
                "initialDelaySeconds": int(location.get("initialDelaySeconds", 5)),
                "updateIntervalSeconds": int(location.get("updateIntervalSeconds", 300)),
                "noFixRetrySeconds": int(location.get("noFixRetrySeconds", 60)),
            }
            password_changed = bool(values["brandmeisterPassword"])
            dmr_to_p25_changed = (
                current_dmr_to_p25_audio != values["audio"]["dmrToP25"]
            )
            p25_to_dmr_changed = (
                current_p25_to_dmr_audio != values["audio"]["p25ToDmr"]
            )
            bridge_changed = (
                current_id != values["repeaterId"]
                or current_callsign != values["brandmeisterCallsign"]
                or timeslot_config_changed
                or current_rx_frequency != values["brandmeisterRxFrequency"]
                or current_tx_frequency != values["brandmeisterTxFrequency"]
                or current_dynamic_timeout != values["dynamicTimeoutSeconds"]
                or current_mappings != values["talkgroupMappings"]
                or password_changed
            )
            host_changed = current_gps != values["gps"]

            if not any(
                (
                    bridge_changed,
                    host_changed,
                    dmr_to_p25_changed,
                    p25_to_dmr_changed,
                )
            ):
                return {"changed": False, "restarted": [], "settings": self.read()}

            targets: list[str] = []
            if password_changed and self.config.dmr_gateway_config.exists():
                targets.append("dmrgateway")
            if bridge_changed:
                targets.append("quantarbridge")
            if host_changed:
                targets.append("dvmhost")
            if dmr_to_p25_changed:
                targets.append("dmr-to-p25")
            if p25_to_dmr_changed:
                targets.append("p25-to-dmr")
            if dmr_to_p25_changed or p25_to_dmr_changed:
                targets.append("dvmfne")

            guard_remaining = self.state.restart_guard_remaining()
            if targets and guard_remaining > 0:
                raise SettingsBusyError(
                    "Funkverkehr ist noch aktiv oder gerade erst beendet. "
                    f"Bitte nach {guard_remaining} Sekunden Funkruhe erneut speichern; "
                    "es wurde nichts ge\u00e4ndert."
                )

            originals: dict[Path, bytes] = {}
            pending: dict[Path, bytes] = {}
            if bridge_changed:
                original = self.config.quantarbridge_config.read_bytes()
                originals[self.config.quantarbridge_config] = original
                updated = original.decode("utf-8")
                if current_id != values["repeaterId"]:
                    updated = self._replace_section_scalar(
                        updated, "brandmeister", "repeaterId", str(values["repeaterId"])
                    )
                if current_callsign != values["brandmeisterCallsign"]:
                    updated = self._upsert_section_scalar(
                        updated,
                        "brandmeister",
                        "callsign",
                        json.dumps(values["brandmeisterCallsign"], ensure_ascii=True),
                    )
                if timeslot_config_changed:
                    updated = self._upsert_section_scalar(
                        updated, "brandmeister", "timeslot", str(values["brandmeisterTimeslot"])
                    )
                    updated = self._upsert_section_scalar(
                        updated,
                        "brandmeister",
                        "slot1",
                        self._yaml_scalar(values["brandmeisterTimeslot"] == 1),
                    )
                    updated = self._upsert_section_scalar(
                        updated,
                        "brandmeister",
                        "slot2",
                        self._yaml_scalar(values["brandmeisterTimeslot"] == 2),
                    )
                    if re.search(r"(?m)^sms:\s*$", updated):
                        updated = self._upsert_section_scalar(
                            updated, "sms", "bmSlot", str(values["brandmeisterTimeslot"])
                        )
                if current_rx_frequency != values["brandmeisterRxFrequency"]:
                    updated = self._upsert_section_scalar(
                        updated, "brandmeister", "rxFrequency", str(values["brandmeisterRxFrequency"])
                    )
                if current_tx_frequency != values["brandmeisterTxFrequency"]:
                    updated = self._upsert_section_scalar(
                        updated, "brandmeister", "txFrequency", str(values["brandmeisterTxFrequency"])
                    )
                if password_changed:
                    updated = self._replace_section_scalar(
                        updated,
                        "brandmeister",
                        "password",
                        json.dumps(values["brandmeisterPassword"], ensure_ascii=True),
                    )
                if current_dynamic_timeout != values["dynamicTimeoutSeconds"]:
                    updated = self._upsert_section_scalar(
                        updated,
                        "routing",
                        "dynamicTimeoutSeconds",
                        str(values["dynamicTimeoutSeconds"]),
                    )
                if current_mappings != values["talkgroupMappings"]:
                    updated = self._replace_mapping_block(
                        updated, values["talkgroupMappings"]
                    )
                pending[self.config.quantarbridge_config] = updated.encode("utf-8")
            if host_changed:
                original = self.config.dvmhost_config.read_bytes()
                originals[self.config.dvmhost_config] = original
                updated = original.decode("utf-8")
                for key, value in values["gps"].items():
                    updated = self._replace_indented_scalar(updated, key, value)
                pending[self.config.dvmhost_config] = updated.encode("utf-8")
            audio_changes = (
                (
                    self.config.dmr_to_p25_config,
                    current_dmr_to_p25_audio,
                    values["audio"]["dmrToP25"],
                    dmr_to_p25_changed,
                ),
                (
                    self.config.p25_to_dmr_config,
                    current_p25_to_dmr_audio,
                    values["audio"]["p25ToDmr"],
                    p25_to_dmr_changed,
                ),
            )
            for path, current_audio, requested_audio, changed in audio_changes:
                if not changed:
                    continue
                original = path.read_bytes()
                originals[path] = original
                updated = original.decode("utf-8")
                for key, value in requested_audio.items():
                    if current_audio.get(key) == value:
                        continue
                    updated = self._upsert_section_scalar(
                        updated, "system", key, self._yaml_scalar(value)
                    )
                pending[path] = updated.encode("utf-8")
            if password_changed and self.config.dmr_gateway_config.exists():
                originals[self.config.dmr_gateway_config] = (
                    self.config.dmr_gateway_config.read_bytes()
                )
                pending[self.config.dmr_gateway_config] = self._write_ini_password(
                    self.config.dmr_gateway_config, values["brandmeisterPassword"]
                )

            backup_name = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
            backup_path = self.config.backup_dir / backup_name
            backup_path.mkdir(parents=True, exist_ok=False)
            for source, content in originals.items():
                atomic_write(backup_path / source.name, content, mode=0o600)

            try:
                for path, content in pending.items():
                    yaml.safe_load(content) if path.suffix in {".yml", ".yaml"} else None
                    atomic_write(path, content)
                restarted = self.restarter.restart(targets)
            except Exception:
                LOG.exception("Applying settings failed; restoring previous runtime files")
                for path, content in originals.items():
                    atomic_write(path, content)
                try:
                    self.restarter.restart(targets)
                except Exception:
                    LOG.exception("Restart after settings rollback also failed")
                raise

            self.state.set_mappings(values["talkgroupMappings"])
            return {
                "changed": True,
                "restarted": restarted,
                "backup": str(backup_path),
                "settings": self.read(),
            }


class DashboardApplication:
    def __init__(self, config: DashboardConfig):
        self.config = config
        self.auth = AuthStore(config.auth_file)
        self.sessions = SessionStore()
        self.login_limiter = LoginLimiter()
        self.state = RuntimeState()
        self.rids = RidDirectory(config.rid_file)
        self.identities = IdentityDirectory(
            config.identity_cache_file, config.bm_api_key_file
        )
        self.log_monitor = LogMonitor(config, self.state)
        self.location_monitor = LocationMonitor(config.location_event_dir, self.state)
        self.brandmeister_profile_monitor = BrandmeisterProfileMonitor(
            config, self.state
        )
        self.service_monitor = ServiceMonitor(config, self.state)
        self.settings = SettingsManager(
            config, self.state, RestartCoordinator(config.restart_targets)
        )

    def start(self) -> None:
        self.auth._read()
        self.settings.read()
        self.log_monitor.start()
        self.location_monitor.start()
        self.brandmeister_profile_monitor.start()
        self.identities.start()
        self.service_monitor.start()

    def stop(self) -> None:
        self.log_monitor.stop()
        self.location_monitor.stop()
        self.brandmeister_profile_monitor.stop()
        self.identities.stop()
        self.service_monitor.stop()

    def status(self) -> dict[str, Any]:
        directory = self.identities.snapshot()
        status = self.state.snapshot(
            self.rids.snapshot(),
            directory["radios"],
            directory["talkgroups"],
            directory["status"],
        )
        self.identities.observe(status)
        return status


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "QuantarDashboard/1.0"
    protocol_version = "HTTP/1.1"
    app: DashboardApplication

    def log_message(self, fmt: str, *args: Any) -> None:
        logger = LOG.debug if self.path.startswith("/api/status") else LOG.info
        logger("%s - %s", self.client_address[0], fmt % args)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; "
            "img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )

    def _send_bytes(
        self,
        status: int,
        payload: bytes,
        content_type: str,
        cache_control: str = "no-store",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(
        self,
        status: int,
        payload: dict[str, Any] | list[Any],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._send_bytes(
            status,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
            extra_headers=extra_headers,
        )

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Ungültige Anfrage.") from exc
        if length <= 0 or length > MAX_JSON_BYTES:
            raise ValueError("Ungültige Anfragegröße.")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise ValueError("Content-Type muss application/json sein.")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Ungültiges JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON-Objekt erwartet.")
        return payload

    def _session_token(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        value = cookie.get("qb_session")
        return value.value if value else None

    def _session(self) -> tuple[str | None, Session | None]:
        token = self._session_token()
        return token, self.app.sessions.get(token)

    def _require_session(self, csrf: bool = False) -> tuple[str, Session] | None:
        token, session = self._session()
        if not token or not session:
            self._error(HTTPStatus.UNAUTHORIZED, "Anmeldung erforderlich.")
            return None
        if csrf and not hmac.compare_digest(
            self.headers.get("X-CSRF-Token", ""), session.csrf_token
        ):
            self._error(HTTPStatus.FORBIDDEN, "Sicherheits-Token ist ungültig.")
            return None
        return token, session

    def _cookie_header(self, token: str, max_age: int) -> str:
        parts = [
            f"qb_session={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={max_age}",
        ]
        if self.app.config.secure_cookies:
            parts.append("Secure")
        return "; ".join(parts)

    def do_HEAD(self) -> None:
        self._handle_get(head_only=True)

    def do_GET(self) -> None:
        self._handle_get(head_only=False)

    def _handle_get(self, head_only: bool) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"status": "ok", "time": utc_iso()})
            return
        if path == "/api/status":
            self._json(HTTPStatus.OK, self.app.status())
            return
        if path == "/api/auth/session":
            _, session = self._session()
            if session:
                self._json(
                    HTTPStatus.OK,
                    {
                        "authenticated": True,
                        "username": session.username,
                        "csrfToken": session.csrf_token,
                    },
                )
            else:
                self._json(HTTPStatus.OK, {"authenticated": False})
            return
        if path == "/api/settings":
            if not self._require_session():
                return
            try:
                self._json(HTTPStatus.OK, self.app.settings.read())
            except Exception:
                LOG.exception("Reading dashboard settings failed")
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Einstellungen konnten nicht gelesen werden.")
            return
        self._serve_static(path, head_only)

    def _serve_static(self, path: str, head_only: bool) -> None:
        del head_only
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/assets/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
            "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/assets/vendor/leaflet/leaflet.css": (
                "vendor/leaflet/leaflet.css",
                "text/css; charset=utf-8",
            ),
            "/assets/vendor/leaflet/leaflet.js": (
                "vendor/leaflet/leaflet.js",
                "text/javascript; charset=utf-8",
            ),
        }
        entry = files.get(path)
        if entry is None:
            self._error(HTTPStatus.NOT_FOUND, "Nicht gefunden.")
            return
        file_path = self.app.config.static_dir / entry[0]
        try:
            payload = file_path.read_bytes()
        except FileNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, "Nicht gefunden.")
            return
        cache = "no-store" if entry[0] == "index.html" else "public, max-age=300"
        self._send_bytes(HTTPStatus.OK, payload, entry[1], cache_control=cache)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/auth/login":
                self._login()
            elif path == "/api/auth/logout":
                self._logout()
            elif path == "/api/auth/password":
                self._change_password()
            else:
                self._error(HTTPStatus.NOT_FOUND, "Nicht gefunden.")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/settings":
            self._error(HTTPStatus.NOT_FOUND, "Nicht gefunden.")
            return
        authenticated = self._require_session(csrf=True)
        if not authenticated:
            return
        try:
            result = self.app.settings.update(self._body())
            self._json(HTTPStatus.OK, result)
        except SettingsBusyError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            LOG.exception("Applying dashboard settings failed")
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Änderungen konnten nicht angewendet werden; die vorherige Konfiguration wurde wiederhergestellt.",
            )

    def _login(self) -> None:
        address = self.client_address[0]
        retry_after = self.app.login_limiter.retry_after(address)
        if retry_after:
            self._error(
                HTTPStatus.TOO_MANY_REQUESTS,
                f"Zu viele Anmeldeversuche. Erneut in {retry_after} Sekunden.",
            )
            return
        body = self._body()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not self.app.auth.verify(username, password):
            self.app.login_limiter.fail(address)
            self._error(HTTPStatus.UNAUTHORIZED, "Benutzername oder Passwort ist falsch.")
            return
        self.app.login_limiter.success(address)
        token, session = self.app.sessions.create(username)
        self._json(
            HTTPStatus.OK,
            {
                "authenticated": True,
                "username": username,
                "csrfToken": session.csrf_token,
            },
            {"Set-Cookie": self._cookie_header(token, SESSION_MAX_AGE_SECONDS)},
        )

    def _logout(self) -> None:
        authenticated = self._require_session(csrf=True)
        if not authenticated:
            return
        token, _ = authenticated
        self.app.sessions.revoke(token)
        self._json(
            HTTPStatus.OK,
            {"authenticated": False},
            {"Set-Cookie": self._cookie_header("", 0)},
        )

    def _change_password(self) -> None:
        authenticated = self._require_session(csrf=True)
        if not authenticated:
            return
        token, session = authenticated
        body = self._body()
        current = str(body.get("currentPassword", ""))
        new = str(body.get("newPassword", ""))
        try:
            self.app.auth.change_password(session.username, current, new)
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, str(exc))
            return
        self.app.sessions.revoke_user(session.username, keep_token=token)
        self._json(HTTPStatus.OK, {"changed": True})


def build_handler(app: DashboardApplication) -> type[DashboardHandler]:
    class BoundHandler(DashboardHandler):
        pass

    BoundHandler.app = app
    return BoundHandler


class DashboardHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            LOG.debug("Client %s closed the connection", client_address[0])
            return
        super().handle_error(request, client_address)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quantarbridge repeater dashboard")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/home/quantar/quantar-runtime/quantar-dashboard.json"),
    )
    parser.add_argument("--init-auth", action="store_true")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = DashboardConfig.load(args.config)
    if args.init_auth:
        if not args.password_stdin:
            raise ValueError("--init-auth requires --password-stdin")
        password = sys.stdin.read().rstrip("\r\n")
        AuthStore(config.auth_file).initialize(args.username, password, force=args.force)
        print(f"initialized={config.auth_file} user={args.username}")
        return 0
    if args.check:
        app = DashboardApplication(config)
        app.auth._read()
        app.settings.read()
        print("configuration=ok")
        return 0

    app = DashboardApplication(config)
    app.start()
    server = DashboardHTTPServer(
        (config.listen_address, config.port), build_handler(app)
    )
    server.daemon_threads = True

    def request_shutdown(signum: int, frame: Any) -> None:
        del signum, frame
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    LOG.info("Dashboard listening on http://%s:%u", config.listen_address, config.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
