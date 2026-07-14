#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

import yaml


IGNORED_DIRECTORIES = {
    ".git",
    ".codex-remote-attachments",
    "build",
    ".cache",
    "__pycache__",
}
FORBIDDEN_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".pdf",
    ".pcap",
    ".pcapng",
    ".p12",
    ".pfx",
    ".pem",
    ".key",
}
FORBIDDEN_NAMES = {
    ".env",
    "bm_api.key",
    "dashboard-auth.json",
    "secrets.txt",
}
TEXT_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "Windows user path": re.compile(r"(?i)\b[A-Z]:[\\/]Users[\\/][^\\/\s]+"),
    "personal home path": re.compile(
        r"(?i)/h[o]me/(?!(?:quantar)(?:/|\b)|\$\{?[A-Z_])[^/\s]+"
    ),
    "private LAN address": re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    "private packet-data address": re.compile(r"\b10\.95\.\d{1,3}\.\d{1,3}\b"),
}
SEVEN_DIGIT_RADIO_ID = re.compile(r"\b2[0-9]{6}\b")
PLACEHOLDER_VALUES = {
    "",
    "LOCAL_FNE_PASSWORD",
    "BRANDMEISTER_PASSWORD",
    "API_KEY",
    "PASSWORD",
    "SECRET",
}


def iter_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if any(part in IGNORED_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            yield path


def inspect_secret_fields(value: Any, location: str, findings: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{location}.{key}"
            lowered = str(key).lower()
            references_secret_file = lowered.endswith(("file", "path"))
            if (
                any(marker in lowered for marker in ("password", "secret", "apikey", "api_key"))
                and not references_secret_file
            ):
                if isinstance(item, str) and item not in PLACEHOLDER_VALUES:
                    findings.append(f"non-placeholder secret field: {child}")
            inspect_secret_fields(item, child, findings)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            inspect_secret_fields(item, f"{location}[{index}]", findings)


def load_structured(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def audit(root: Path) -> list[str]:
    findings: list[str] = []
    for path in iter_files(root):
        relative = path.relative_to(root)
        lowered_name = path.name.lower()
        if lowered_name in FORBIDDEN_NAMES:
            findings.append(f"forbidden runtime file: {relative}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(f"forbidden binary or credential file: {relative}")
        if path.stat().st_size > 5 * 1024 * 1024:
            findings.append(f"unexpected file larger than 5 MiB: {relative}")
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(f"unexpected binary file: {relative}")
            continue

        for label, pattern in TEXT_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{label}: {relative}")
        if "vendor" not in relative.parts and SEVEN_DIGIT_RADIO_ID.search(text):
            findings.append(f"non-placeholder seven-digit radio ID: {relative}")
        if path.parent == root / "deploy" / "examples" and path.suffix.lower() in {
            ".json",
            ".yml",
            ".yaml",
        }:
            try:
                inspect_secret_fields(load_structured(path), str(relative), findings)
            except (json.JSONDecodeError, yaml.YAMLError) as error:
                findings.append(f"invalid structured template {relative}: {error}")

    return sorted(set(findings))


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit a public QuantarBridge source tree.")
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    findings = audit(root)
    if findings:
        print("Public-tree audit failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    count = sum(1 for _ in iter_files(root))
    print(f"public_tree_audit=ok files={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
