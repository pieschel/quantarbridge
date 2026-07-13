#!/usr/bin/env python3

import subprocess
from pathlib import Path


ROOT = Path("/home/quantar/quantarbridge/deploy/scripts")
TRIGGER = Path("/home/quantar/quantar-runtime/dynamic_expired.trigger")
CONFIG = Path("/home/quantar/quantar-runtime/quantarbridge.yml")
STATE = Path("/home/quantar/quantar-runtime/dynamic_routes.state")
API_KEY = Path("/home/quantar/quantar-runtime/bm_api.key")


def main() -> int:
    # First clear any stale BM dynamic route and republish the primary static TG.
    subprocess.run(
        [
            "/usr/bin/python3",
            str(ROOT / "dynamic_expiry_recover.py"),
            "--trigger",
            str(TRIGGER),
            "--config",
            str(CONFIG),
            "--api-key-file",
            str(API_KEY),
        ],
        check=True,
    )

    # Flush the stateless downlink bridge without resetting the P25 host. The
    # host owns ARS/TMS/LRRP sessions which must survive a dynamic TG expiry.
    subprocess.run(
        ["systemctl", "restart", "dvmbridge-dmr-to-p25.service"],
        check=True,
    )
    subprocess.run(
        [
            "/usr/bin/python3",
            str(ROOT / "ensure_static_tg.py"),
            "--config",
            str(CONFIG),
            "--state",
            str(STATE),
            "--api-key-file",
            str(API_KEY),
        ],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
