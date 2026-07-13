import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "ensure_static_tg.py"
SPEC = importlib.util.spec_from_file_location("ensure_static_tg", SCRIPT)
ENSURE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ENSURE)


class EnsureStaticTalkgroupTest(unittest.TestCase):
    def test_brandmeister_dynamic_subscription_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "quantarbridge.yml"
            state = root / "dynamic_routes.state"
            key = root / "bm_api.key"
            stamp = root / "ensure.stamp"
            config.write_text(
                """brandmeister:
  repeaterId: 123456
  timeslot: 2
  transport: \"mmdvm-gateway\"
routing:
  staticTalkgroups:
    - 262000
""",
                encoding="utf-8",
            )
            key.write_text("test-key\n", encoding="utf-8")

            argv = [
                "ensure_static_tg.py",
                "--config",
                str(config),
                "--state",
                str(state),
                "--api-key-file",
                str(key),
                "--stamp",
                str(stamp),
                "--min-interval",
                "0",
            ]
            profile = {
                "dynamicSubscriptions": [{"talkgroup": "262002", "slot": "2"}]
            }
            with (
                mock.patch.object(ENSURE, "fetch_profile", return_value=profile),
                mock.patch.object(ENSURE.subprocess, "run") as run,
                mock.patch.object(ENSURE.sys, "argv", argv),
                mock.patch("builtins.print") as output,
            ):
                self.assertEqual(0, ENSURE.main())

            run.assert_not_called()
            output.assert_called_once_with("skip=bm-dynamic-active")

    def test_static_talkgroup_is_restored_without_dynamic_subscription(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "quantarbridge.yml"
            key = root / "bm_api.key"
            config.write_text(
                """brandmeister:
  repeaterId: 123456
  timeslot: 2
  transport: \"mmdvm-gateway\"
routing:
  staticTalkgroups:
    - 262000
""",
                encoding="utf-8",
            )
            key.write_text("test-key\n", encoding="utf-8")

            argv = [
                "ensure_static_tg.py",
                "--config",
                str(config),
                "--state",
                str(root / "dynamic_routes.state"),
                "--api-key-file",
                str(key),
                "--stamp",
                str(root / "ensure.stamp"),
                "--min-interval",
                "0",
            ]
            with (
                mock.patch.object(
                    ENSURE, "fetch_profile", return_value={"dynamicSubscriptions": []}
                ),
                mock.patch.object(ENSURE.subprocess, "run") as run,
                mock.patch.object(ENSURE.sys, "argv", argv),
            ):
                self.assertEqual(0, ENSURE.main())

            run.assert_called_once_with(
                [
                    "mosquitto_pub",
                    "-h",
                    "127.0.0.1",
                    "-t",
                    "dmr-gateway/dynamic",
                    "-m",
                    "DynTG 2 262000",
                ],
                check=True,
            )


if __name__ == "__main__":
    unittest.main()
