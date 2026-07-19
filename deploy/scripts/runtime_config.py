#!/usr/bin/env python3

from pathlib import Path


def _section_lines(text: str, section: str) -> list[str]:
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == f"{section}:" and not line[:1].isspace()),
        -1,
    )
    if start < 0:
        raise RuntimeError(f"Missing YAML section: {section}")

    result: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped and not line[:1].isspace() and not stripped.startswith("#"):
            break
        result.append(line)
    return result


def _section_scalar(text: str, section: str, key: str) -> str | None:
    prefix = f"  {key}:"
    for line in _section_lines(text, section):
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].split("#", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        return value
    return None


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in ("true", "yes", "on", "1"):
        return True
    if normalized in ("false", "no", "off", "0"):
        return False
    raise RuntimeError(f"Invalid YAML boolean: {value}")


def read_brandmeister_device(config_path: Path) -> tuple[int, int]:
    text = config_path.read_text(encoding="utf-8")
    try:
        device_id = int(_section_scalar(text, "brandmeister", "repeaterId") or "")
        raw_timeslot = _section_scalar(text, "brandmeister", "timeslot")
        timeslot = (
            int(raw_timeslot)
            if raw_timeslot is not None
            else (1 if _parse_bool(_section_scalar(text, "brandmeister", "slot1")) else 2)
        )
    except ValueError as exc:
        raise RuntimeError(f"Invalid BrandMeister device configuration in {config_path}") from exc
    if device_id <= 0 or timeslot not in (1, 2):
        raise RuntimeError(f"Invalid BrandMeister device configuration in {config_path}")
    return device_id, 0 if device_id > 999_999 else timeslot


def read_brandmeister_voice_enabled(config_path: Path, default: bool = True) -> bool:
    text = config_path.read_text(encoding="utf-8")
    return _parse_bool(
        _section_scalar(text, "brandmeister", "voiceEnabled"), default=default
    )


def read_static_talkgroups(config_path: Path) -> list[int]:
    text = config_path.read_text(encoding="utf-8")
    lines = _section_lines(text, "routing")
    key_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "staticTalkgroups:" and line.startswith("  ")),
        -1,
    )
    if key_index < 0:
        return []

    talkgroups: list[int] = []
    for line in lines[key_index + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indentation = len(line) - len(line.lstrip())
        if indentation <= 2:
            break
        if not stripped.startswith("- "):
            continue
        try:
            talkgroup = int(stripped[2:].split("#", 1)[0].strip())
        except ValueError:
            continue
        if talkgroup > 0:
            talkgroups.append(talkgroup)
    return sorted(set(talkgroups))
