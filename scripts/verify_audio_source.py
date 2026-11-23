#!/usr/bin/env python3
"""Check that the patched DVMHost build uses the released audio source closure."""

import argparse
import hashlib
import json
from pathlib import Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Patched DVMHost source directory")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (repo / "patches" / "dvmhost-direct-audio.manifest.json").read_text(encoding="utf-8")
    )
    if manifest["schema"] != 1:
        parser.error("Unsupported audio source manifest schema")
    patch = repo / "patches" / manifest["patch"]
    errors = []
    if sha256(patch.read_bytes()) != manifest["patch_sha256"]:
        errors.append("Audio patch does not match its release manifest")
    for relative, expected in manifest["files"].items():
        path = args.source / relative
        if not path.is_file():
            errors.append(f"Missing audio source: {relative}")
            continue
        # The public patch uses LF; permit an equivalent Windows checkout.
        data = path.read_bytes().replace(b"\r\n", b"\n")
        if sha256(data) != expected["public_source_sha256"]:
            errors.append(f"Audio source differs from the release: {relative}")
    if errors:
        parser.exit(1, "\n".join(errors) + "\n")
    print(f"audio_source_manifest=passed files={len(manifest['files'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
