import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "scripts"
    / "tetrapack_brew_bridge.py"
)
SPEC = importlib.util.spec_from_file_location("tetrapack_brew_bridge", SCRIPT_PATH)
BRIDGE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = BRIDGE
SPEC.loader.exec_module(BRIDGE)


class FailingBrewClient:
    def send_sms(self, source_rid, target_rid, text):
        raise AssertionError("external messages must not use BREW")


class RecordingBrewClient:
    def __init__(self):
        self.calls = []

    def send_sms(self, source_rid, target_rid, text):
        self.calls.append((source_rid, target_rid, text))
        return {"status": "sent"}


class TetrapackBridgeTest(unittest.TestCase):
    def test_local_p25_message_keeps_sender_and_target_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = BRIDGE.BridgeConfig(
                inbox_dir=root / "inbox",
                outbox_dir=root / "outbox",
                p25_outbox_dir=root / "p25-outbox",
                processed_dir=root / "processed",
                error_dir=root / "error",
                local_loop_enabled=True,
            )
            BRIDGE.ensure_dirs(config)
            now = time.monotonic()
            pending = BRIDGE.PendingText(
                source_rid=1000001,
                target_rid=1000002,
                local_candidate=True,
                first_seen=now,
                updated_at=now,
                fragments=["Test"],
                event_names=["host-local"],
            )

            result = BRIDGE.flush_pending_text(config, FailingBrewClient(), pending)

            self.assertEqual("queued", result["status"])
            self.assertEqual("local_p25", result["transport"])
            queued = list(config.p25_outbox_dir.glob("*.yaml"))
            self.assertEqual(1, len(queued))
            self.assertEqual(
                "sourceRid: 1000001\n"
                "targetRid: 1000002\n"
                'textHex: "54657374"\n',
                queued[0].read_text(encoding="utf-8"),
            )
            self.assertEqual([], list(config.outbox_dir.glob("*.json")))

    def test_lrrp_report_is_queued_for_brandmeister_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = BRIDGE.BridgeConfig(
                inbox_dir=root / "inbox",
                outbox_dir=root / "outbox",
                processed_dir=root / "processed",
                error_dir=root / "error",
            )
            BRIDGE.ensure_dirs(config)
            event_path = config.inbox_dir / "host-lrrp.json"
            raw_packet = (
                "4500002d0001000040111477c633640acb00710a"
                "c02ec02e0019c2f3070f220400000001662266666607d27d28"
            )
            event_path.write_text(
                json.dumps(
                    {
                        "application": "motorola_lrrp",
                        "sourceRid": 1000002,
                        "targetRid": 262999,
                        "rawIpPacketHex": raw_packet,
                    }
                ),
                encoding="utf-8",
            )

            BRIDGE.process_event(config, FailingBrewClient(), {}, event_path)

            queued = list(config.outbox_dir.glob("*.json"))
            self.assertEqual(1, len(queued))
            body = json.loads(queued[0].read_text(encoding="utf-8"))
            self.assertEqual("brandmeister", body["route"])
            self.assertEqual("lrrp", body["channel"])
            self.assertEqual(1000002, body["sourceRid"])
            self.assertEqual(262999, body["targetRid"])
            self.assertEqual(raw_packet, body["rawIpPacketHex"])
            self.assertTrue((config.processed_dir / event_path.name).exists())

    def test_configured_direct_message_uses_brew_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = BRIDGE.BridgeConfig(
                inbox_dir=root / "inbox",
                outbox_dir=root / "outbox",
                processed_dir=root / "processed",
                error_dir=root / "error",
                brew_target_rids={262993, 1000001},
            )
            config.brew.enabled = True
            BRIDGE.ensure_dirs(config)
            now = time.monotonic()
            pending = BRIDGE.PendingText(
                source_rid=1000002,
                target_rid=1000001,
                local_candidate=False,
                first_seen=now,
                updated_at=now,
                fragments=["Direkttest4"],
                event_names=["host-test"],
            )
            brew = RecordingBrewClient()

            result = BRIDGE.flush_pending_text(config, brew, pending)

            self.assertEqual("sent", result["status"])
            self.assertEqual("tetrapack_brew", result["transport"])
            self.assertEqual([(1000002, 1000001, "Direkttest4")], brew.calls)
            self.assertEqual([], list(config.outbox_dir.glob("*.json")))

    def test_unlisted_external_message_uses_packet_data_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = BRIDGE.BridgeConfig(
                inbox_dir=root / "inbox",
                outbox_dir=root / "outbox",
                processed_dir=root / "processed",
                error_dir=root / "error",
                brew_target_rids={262993, 1000001},
            )
            BRIDGE.ensure_dirs(config)
            now = time.monotonic()
            pending = BRIDGE.PendingText(
                source_rid=1000002,
                target_rid=1000103,
                local_candidate=False,
                first_seen=now,
                updated_at=now,
                fragments=["Fallback"],
                event_names=["host-fallback"],
            )

            result = BRIDGE.flush_pending_text(config, FailingBrewClient(), pending)

            self.assertEqual("queued", result["status"])
            self.assertEqual("brandmeister_packet_data", result["transport"])
            queued = list(config.outbox_dir.glob("*.json"))
            self.assertEqual(1, len(queued))
            body = json.loads(queued[0].read_text(encoding="utf-8"))
            self.assertEqual("brandmeister", body["route"])
            self.assertEqual(1000002, body["sourceRid"])
            self.assertEqual(1000103, body["targetRid"])
            self.assertEqual("Fallback", body["text"])
            self.assertNotIn("sendArsFirst", body)

    def test_weather_service_stays_on_brew_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = BRIDGE.BridgeConfig(
                inbox_dir=root / "inbox",
                outbox_dir=root / "outbox",
                processed_dir=root / "processed",
                error_dir=root / "error",
            )
            config.brew.enabled = True
            BRIDGE.ensure_dirs(config)
            now = time.monotonic()
            pending = BRIDGE.PendingText(
                source_rid=1000002,
                target_rid=262993,
                local_candidate=False,
                first_seen=now,
                updated_at=now,
                fragments=["Wx example"],
                event_names=["host-weather"],
            )
            brew = RecordingBrewClient()

            result = BRIDGE.flush_pending_text(config, brew, pending)

            self.assertEqual("sent", result["status"])
            self.assertEqual("tetrapack_brew", result["transport"])
            self.assertEqual([(1000002, 262993, "Wx example")], brew.calls)
            self.assertEqual([], list(config.outbox_dir.glob("*.json")))


if __name__ == "__main__":
    unittest.main()
