#!/usr/bin/env python3
"""Reject volatile/read-only configuration storage before initial setup."""
import json
from pathlib import Path
import subprocess
import sys


def check(path: Path) -> None:
    while not path.exists():
        path = path.parent
    result = subprocess.run(
        ["findmnt", "--json", "--target", str(path), "--output", "FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True,
    )
    mount = json.loads(result.stdout)["filesystems"][0]
    if mount["fstype"] in {"tmpfs", "ramfs", "overlay", "aufs", "squashfs"} or "ro" in mount["options"].split(","):
        raise RuntimeError(f"{path}: configuration storage is volatile or read-only; disable the overlay/read-only filesystem before setup")


if __name__ == "__main__":
    for argument in sys.argv[1:]:
        check(Path(argument).resolve())
