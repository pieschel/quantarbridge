import importlib.util
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "bm_to_p25_recover.py"
SPEC = importlib.util.spec_from_file_location("bm_to_p25_recover", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class BrandmeisterToP25RecoverTest(unittest.TestCase):
    @mock.patch.object(MODULE.subprocess, "run")
    def test_recovery_restarts_only_stateless_downlink_bridge(self, run):
        MODULE.restart_downlink_bridge()
        run.assert_called_once_with(
            ["systemctl", "restart", "dvmbridge-dmr-to-p25.service"],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
