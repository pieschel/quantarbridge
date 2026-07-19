import importlib.util
import json
import struct
import sys
import tempfile
import threading
import time
import unittest
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
        static_brew_groups={983872},
        dynamic_timeout_seconds=600,
        disconnect_talkgroup=4000,
        pcm_input=AUDIO.PcmInputConfig("127.0.0.1", 31120),
        pcm_output=AUDIO.PcmOutputConfig("127.0.0.1", 31121),
    )


class TetrapackBrewAudioTest(unittest.TestCase):
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

    def test_dvm_rtp_round_trip_keeps_radio_and_talkgroup_metadata(self):
        pcm = struct.pack("<160h", *range(160))
        packet = AUDIO.build_dvm_rtp(
            pcm, 1000001, 999, 42, 123456, 9000112, True
        )
        self.assertEqual(
            (1000001, 999, 42, True, pcm), AUDIO.parse_dvm_rtp(packet)
        )

    def test_explicit_mapping_is_static_and_identity_route_is_dynamic(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            router = AUDIO.TalkgroupRouter(config)
            self.assertEqual(983872, router.brew_for_p25(999))
            self.assertEqual(999, router.p25_for_brew(983872))
            self.assertFalse(router.activate(983872, 10.0, 1000.0))
            self.assertTrue(router.activate(26291, 20.0, 1010.0))
            self.assertEqual(26291, router.p25_for_brew(26291))
            self.assertEqual([26291, 983872], router.groups())

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
