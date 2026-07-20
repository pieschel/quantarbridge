import importlib.util
import json
import math
import struct
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "scripts"
    / "tetrapack_brew_audio.py"
)
SPEC = importlib.util.spec_from_file_location("tetrapack_brew_audio", SCRIPT_PATH)
AUDIO = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = AUDIO
SPEC.loader.exec_module(AUDIO)


def make_config(root: Path):
    return AUDIO.AudioConfig(
        enabled=True,
        existing_brew_config=root / "brew.json",
        existing_brew_module=root / "brew.py",
        quantarbridge_config=root / "quantarbridge.yml",
        codec_library=root / "libtetra-codec.so",
        status_file=root / "status.json",
        log_file=root / "audio.log",
        dynamic_state_file=root / "dynamic_routes.state",
        observed_issis_file=root / "observed.json",
        dvmhost_log_dir=root / "log",
        local_issis=[],
        p25_to_brew={999: 983872},
        brew_to_p25={983872: 999},
        static_brew_groups={262},
        dynamic_timeout_seconds=600,
        disconnect_talkgroup=4000,
        pcm_input=AUDIO.PcmInputConfig("127.0.0.1", 31120),
        pcm_output=AUDIO.PcmOutputConfig("127.0.0.1", 31121),
    )


class TetrapackBrewAudioTest(unittest.TestCase):
    def test_runtime_routing_overrides_stale_audio_config_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "quantarbridge.yml").write_text(
                "routing:\n"
                "  staticTalkgroups: [262]\n"
                "  talkgroupMappings:\n"
                "    - p25: 999\n"
                "      brandmeister: 26291\n"
                "  dynamicTimeoutSeconds: 900\n"
                "  disconnectTalkgroup: 4001\n",
                encoding="utf-8",
            )
            audio_config = {
                "existingBrewConfig": "brew.json",
                "existingBrewModule": "brew.py",
                "quantarbridgeConfig": "quantarbridge.yml",
                "codecLibrary": "libtetra-codec.so",
                "statusFile": "status.json",
                "logFile": "audio.log",
                "p25PcmInput": {"port": 31120},
                "p25PcmOutput": {"port": 31121},
                "staticTalkgroups": [983872],
                "talkgroupMappings": [{"p25": 999, "brew": 983872}],
                "dynamicTimeoutSeconds": 600,
                "disconnectTalkgroup": 4000,
            }
            path = root / "audio.json"
            path.write_text(json.dumps(audio_config), encoding="utf-8")

            config = AUDIO.AudioConfig.load(path)

            self.assertEqual({262}, config.static_brew_groups)
            self.assertEqual({999: 26291}, config.p25_to_brew)
            self.assertEqual({26291: 999}, config.brew_to_p25)
            self.assertEqual(900, config.dynamic_timeout_seconds)
            self.assertEqual(4001, config.disconnect_talkgroup)

    def test_inbound_brew_sds_is_queued_for_registered_p25_radio(self):
        test_case = self

        class FakeBrewModule:
            @staticmethod
            def parse_text_sds_type4_pdu(payload, length_bits):
                test_case.assertEqual(len(payload) * 8, length_bits)
                return payload.decode("utf-8")

            @staticmethod
            def build_brew_sds_report(session_id, status=0):
                return b"report:" + session_id.bytes_le + bytes((status,))

        class FakeTransport:
            brew_module = FakeBrewModule

            def __init__(self):
                self.frames = []

            def send(self, frame):
                self.frames.append(frame)
                return True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            config.sms_command_dir = root / "sms" / "brew-audio-outbox"
            bridge = object.__new__(AUDIO.BrewAudioBridge)
            bridge.config = config
            bridge.transport = FakeTransport()
            bridge.status = AUDIO.AtomicStatus(root / "status.json")
            bridge.local_issis_lock = threading.Lock()
            bridge.local_issis = {1000002}
            bridge.pending_sds = {}
            bridge.owned_uuids = {}

            call_uuid = bytes.fromhex("8aa5b78d6053f04f929dcf20b578cdce")
            source = 1000001
            target = 1000002
            header = (
                bytes((AUDIO.BREW_CLASS_CALL_CONTROL, AUDIO.CALL_STATE_SHORT_TRANSFER))
                + call_uuid
                + struct.pack("<II", source, target)
                + bytes(32)
            )
            payload = b"Private test"
            transfer = (
                bytes((AUDIO.BREW_CLASS_FRAME, AUDIO.FRAME_TYPE_SDS_TRANSFER))
                + call_uuid
                + struct.pack("<H", len(payload) * 8)
                + payload
            )

            bridge._on_brew_binary(header)
            bridge._on_brew_binary(transfer)

            queued = list((root / "sms" / "p25-outbox").glob("*.yaml"))
            self.assertEqual(1, len(queued))
            body = queued[0].read_text(encoding="utf-8")
            self.assertIn("sourceRid: 1000001", body)
            self.assertIn("targetRid: 1000002", body)
            self.assertIn(payload.hex(), body)
            self.assertEqual(1, len(bridge.transport.frames))
            self.assertTrue(bridge.transport.frames[0].startswith(b"report:"))

    def test_sms_command_is_sent_over_the_audio_transport(self):
        class FakeBrewModule:
            @staticmethod
            def build_text_sds_type4_pdu(text, message_reference):
                return text.encode("utf-8") + bytes((message_reference,))

            @staticmethod
            def build_brew_short_transfer(session_id, source, target):
                return b"short"

            @staticmethod
            def build_brew_sds_transfer(session_id, payload):
                return b"sds:" + payload

            @staticmethod
            def build_brew_call_release(session_id, cause=0):
                return b"release"

        class FakeTransport:
            def __init__(self):
                self.connected = threading.Event()
                self.connected.set()
                self.brew_module = FakeBrewModule
                self.frames = []

            def send_many(self, frames):
                self.frames.append(frames)
                return True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            config.sms_command_dir = root / "brew-audio-outbox"
            config.sms_command_dir.mkdir()
            command = config.sms_command_dir / "command.json"
            command.write_text(
                '{"sourceRid": 1000001, "targetRid": 262993, "text": "WX"}',
                encoding="utf-8",
            )
            bridge = object.__new__(AUDIO.BrewAudioBridge)
            bridge.config = config
            bridge.transport = FakeTransport()
            bridge.status = AUDIO.AtomicStatus(root / "status.json")
            observed = []
            bridge._ensure_local_issi = observed.append

            self.assertTrue(bridge._process_sms_command(command))

            self.assertEqual([1000001], observed)
            self.assertEqual(b"short", bridge.transport.frames[0][0])
            self.assertTrue(bridge.transport.frames[0][1].startswith(b"sds:WX"))
            self.assertEqual(b"release", bridge.transport.frames[0][2])
            self.assertFalse(command.exists())
            results = list((root / "brew-audio-results").glob("*.json"))
            self.assertEqual(1, len(results))
            self.assertEqual("sent", json.loads(results[0].read_text())["status"])

    def test_pcm_scaling_uses_a_hard_symmetric_peak_limit(self):
        self.assertEqual(
            [-24000, -2000, 0, 2000, 24000],
            AUDIO.scale_pcm([-20000, -1000, 0, 1000, 20000], 2.0, 24000),
        )

    def test_uplink_processor_removes_dc_and_preserves_peak_shape(self):
        processor = AUDIO.UplinkAudioProcessor(
            high_pass_hz=180.0,
            presence_gain=0.15,
            high_cut_hz=3400.0,
        )
        output = processor.process([4000] * 480, 1.0, 24000)

        self.assertLess(abs(output[-1]), 10)
        self.assertLessEqual(max(abs(value) for value in output), 24000)

    def test_uplink_processor_presence_boosts_fast_changes(self):
        processor = AUDIO.UplinkAudioProcessor(presence_gain=0.2)

        output = processor.process([0, 1000, -1000, 1000], 1.0, 24000)

        self.assertEqual([0, 1200, -1400, 1400], output)

    def test_uplink_processor_limits_the_whole_block_without_flat_clipping(self):
        processor = AUDIO.UplinkAudioProcessor()

        output = processor.process([-20000, -10000, 0, 10000, 20000], 2.0, 24000)

        self.assertEqual([-24000, -12000, 0, 12000, 24000], output)

    def test_uplink_de_esser_targets_high_band_without_lowering_voice_body(self):
        sample_count = 800
        low_tone = [
            int(round(4000 * math.sin(2.0 * math.pi * 500 * index / 8000)))
            for index in range(sample_count)
        ]
        high_tone = [
            int(round(4000 * math.sin(2.0 * math.pi * 3000 * index / 8000)))
            for index in range(sample_count)
        ]

        low_output = AUDIO.UplinkAudioProcessor(
            de_esser_crossover_hz=2200.0,
            de_esser_threshold=250.0,
            de_esser_strength=0.55,
        ).process(low_tone, 1.0, 24000)
        high_processor = AUDIO.UplinkAudioProcessor(
            de_esser_crossover_hz=2200.0,
            de_esser_threshold=250.0,
            de_esser_strength=0.55,
        )
        high_output = high_processor.process(high_tone, 1.0, 24000)

        low_ratio = sum(abs(value) for value in low_output[160:]) / sum(
            abs(value) for value in low_tone[160:]
        )
        high_ratio = sum(abs(value) for value in high_output[160:]) / sum(
            abs(value) for value in high_tone[160:]
        )
        self.assertGreater(low_ratio, 0.9)
        self.assertLess(high_ratio, 0.8)
        self.assertGreater(high_processor.de_esser_reduced_samples, 0)
        self.assertGreater(high_processor.de_esser_max_reduction_db, 1.0)

    def test_brew_error_reason_is_bounded_and_safe_for_logs(self):
        frame = bytes((AUDIO.BREW_CLASS_ERROR, 1)) + b"restricted\x00ignored\x01"
        self.assertEqual("restricted?ignored?", AUDIO.brew_error_reason(frame))
        rejected = bytes(
            (AUDIO.BREW_CLASS_ERROR, 1, AUDIO.BREW_CLASS_CALL_CONTROL, 3)
        ) + bytes(17)
        self.assertEqual(
            "rejected_class=0xf1 rejected_type=3",
            AUDIO.brew_error_reason(rejected),
        )
        call_uuid = uuid.UUID("cd8be847-0a18-4a41-85ea-421afc4a081a")
        restricted = (
            bytes((AUDIO.BREW_CLASS_ERROR, AUDIO.BREW_TYPE_RESTRICTED))
            + call_uuid.bytes_le
            + b"\x01\x02\x03"
        )
        self.assertEqual(
            "call_uuid=cd8be847-0a18-4a41-85ea-421afc4a081a detail=010203",
            AUDIO.brew_error_reason(restricted),
        )

    def test_pcm_rms_reports_measured_signal_level(self):
        self.assertEqual(0.0, AUDIO.pcm_rms(0, 0))
        self.assertEqual(1000.0, AUDIO.pcm_rms(2_000_000, 2))

    def test_subscriber_refresh_reregisters_and_reaffiliates_known_radios(self):
        class FakeTransport:
            def __init__(self):
                self.connected = threading.Event()
                self.connected.set()
                self.frames = []

            def send(self, frame):
                self.frames.append(frame)
                return True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = object.__new__(AUDIO.BrewAudioBridge)
            bridge.config = make_config(root)
            bridge.router = AUDIO.TalkgroupRouter(bridge.config)
            bridge.router_lock = threading.Lock()
            bridge.local_issis = {1000001}
            bridge.local_issis_lock = threading.Lock()
            bridge.transport = FakeTransport()
            bridge.status = AUDIO.AtomicStatus(root / "status.json")
            bridge.last_subscriber_refresh = 0.0

            self.assertTrue(
                bridge._refresh_brew_subscribers({1000001}, trigger="test")
            )

            self.assertEqual(2, len(bridge.transport.frames))
            self.assertEqual(
                (AUDIO.BREW_CLASS_SUBSCRIBER, AUDIO.SUBSCRIBER_REREGISTER),
                tuple(bridge.transport.frames[0][:2]),
            )
            self.assertEqual(
                (AUDIO.BREW_CLASS_SUBSCRIBER, AUDIO.SUBSCRIBER_AFFILIATE),
                tuple(bridge.transport.frames[1][:2]),
            )
            self.assertEqual(
                1,
                bridge.status.data["counters"]["brewSubscriberRefreshes"],
            )

    def test_restricted_brew_response_is_scoped_to_the_call(self):
        class FakeTransport:
            def __init__(self):
                self.closed = False

            def close_socket(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            bridge = object.__new__(AUDIO.BrewAudioBridge)
            bridge.transport = FakeTransport()
            bridge.status = AUDIO.AtomicStatus(Path(directory) / "status.json")
            frame = bytes(
                (
                    AUDIO.BREW_CLASS_ERROR,
                    AUDIO.BREW_TYPE_RESTRICTED,
                    AUDIO.BREW_CLASS_CALL_CONTROL,
                    AUDIO.CALL_STATE_GROUP_IDLE,
                )
            ) + bytes(17)

            bridge._on_brew_binary(frame)

            self.assertFalse(bridge.transport.closed)
            self.assertEqual(
                1,
                bridge.status.data["counters"]["brewRestrictedCalls"],
            )

    def test_dvm_rtp_round_trip_keeps_radio_and_talkgroup_metadata(self):
        pcm = struct.pack("<160h", *range(160))
        packet = AUDIO.build_dvm_rtp(
            pcm, 1000001, 999, 42, 123456, 9000112, True
        )
        self.assertEqual(
            (1000001, 999, 42, True, pcm), AUDIO.parse_dvm_rtp(packet)
        )

    def test_mapping_is_only_routable_while_statically_or_dynamically_subscribed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            router = AUDIO.TalkgroupRouter(config)
            self.assertEqual(983872, router.brew_for_p25(999))
            self.assertIsNone(router.p25_for_brew(983872))
            self.assertEqual(262, router.p25_for_brew(262))
            self.assertEqual([262], router.groups())

            self.assertTrue(router.activate(983872, 10.0, 1000.0))
            self.assertEqual(999, router.p25_for_brew(983872))
            self.assertTrue(router.activate(26291, 20.0, 1010.0))
            self.assertEqual(26291, router.p25_for_brew(26291))
            self.assertEqual([262, 26291, 983872], router.groups())

            self.assertEqual([983872], router.expire(611.0))
            self.assertIsNone(router.p25_for_brew(983872))
            self.assertEqual([262, 26291], router.groups())

    def test_unsubscribed_mapped_downlink_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = object.__new__(AUDIO.BrewAudioBridge)
            bridge.router = AUDIO.TalkgroupRouter(make_config(root))
            bridge.router_lock = threading.Lock()
            bridge.ignored_downlink_uuids = set()
            bridge.status = AUDIO.AtomicStatus(root / "status.json")

            call_uuid = bytes.fromhex("8aa5b78d6053f04f929dcf20b578cdce")
            bridge._start_or_update_downlink(call_uuid, 1000001, 983872)

            self.assertIn(call_uuid, bridge.ignored_downlink_uuids)
            self.assertEqual(
                1,
                bridge.status.data["counters"]["unsubscribedDownlinkCalls"],
            )

    def test_dynamic_route_is_only_extended_by_explicit_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            router = AUDIO.TalkgroupRouter(config)
            router.activate(26291, 100.0, 1000.0)
            self.assertEqual([], router.expire(699.9))
            router.activate(26291, 700.0, 1600.0)
            self.assertEqual([], router.expire(1299.9))
            self.assertEqual([26291], router.expire(1300.1))

    def test_dynamic_state_and_observed_issis_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root)
            router = AUDIO.TalkgroupRouter(config)
            now_epoch = time.time()
            router.activate(26291, time.monotonic(), now_epoch)
            AUDIO.save_dynamic_routes(config.dynamic_state_file, router)
            restored = AUDIO.TalkgroupRouter(config)
            self.assertEqual(
                [26291],
                AUDIO.load_dynamic_routes(
                    config.dynamic_state_file, restored, now_epoch=now_epoch + 10
                ),
            )
            AUDIO.save_observed_issis(config.observed_issis_file, {1000001, 1000002})
            self.assertEqual(
                {1000001, 1000002},
                AUDIO.load_observed_issis(config.observed_issis_file),
            )

    def test_ars_log_discovery_tracks_registration_reset_and_deregistration(self):
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            log_path = log_dir / "dvmhost-2026-07-19.log"
            log_path.write_text(
                "recognized Motorola SCEP ARS registration, llId = 1000001, subscriberIp = x\n"
                "Motorola LRRP Initial Delay: 5\n"
                "accepted Motorola ARS refresh, llId = 1000002, subscriberIp = y\n"
                "Motorola ARS deregistration, llId = 1000002\n"
                "recognized Motorola SCEP ARS registration, llId = 1000001, subscriberIp = x\n",
                encoding="utf-8",
            )
            issis, current, offset = AUDIO.discover_registered_issis(log_dir)
            self.assertEqual({1000001}, issis)
            self.assertEqual(log_path, current)
            self.assertEqual(log_path.stat().st_size, offset)
            self.assertEqual(1000001, AUDIO.discover_affiliation_anchor(log_dir))


if __name__ == "__main__":
    unittest.main()
