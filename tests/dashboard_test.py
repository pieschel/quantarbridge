import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.app import (
    AuthStore,
    BrandmeisterProfileMonitor,
    DashboardConfig,
    IdentityDirectory,
    LoginLimiter,
    RestartCoordinator,
    RuntimeState,
    SettingsBusyError,
    SettingsManager,
    decode_motorola_lrrp_position,
)


class TwoCycleStop:
    def __init__(self):
        self.cycles = 0

    def is_set(self):
        return self.cycles >= 2

    def wait(self, _timeout):
        self.cycles += 1
        return self.is_set()


class RecordingRestarter:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def restart(self, names):
        names = list(names)
        self.calls.append(names)
        if self.fail:
            self.fail = False
            raise RuntimeError("restart failed")
        return names


class RestartCoordinatorTest(unittest.TestCase):
    def test_restarts_user_service_without_sudo(self):
        coordinator = RestartCoordinator(
            {
                "brew-audio": {
                    "type": "systemd-user",
                    "unit": "tetrapack-brew-audio.service",
                }
            }
        )

        with patch("dashboard.app.subprocess.run") as run:
            restarted = coordinator.restart(["brew-audio"])

        self.assertEqual(["brew-audio"], restarted)
        run.assert_called_once_with(
            ["systemctl", "--user", "restart", "tetrapack-brew-audio.service"],
            check=True,
            capture_output=True,
            text=True,
            timeout=12,
        )


def make_config(root: Path) -> DashboardConfig:
    static_dir = root / "static"
    static_dir.mkdir()
    return DashboardConfig(
        listen_address="127.0.0.1",
        port=8088,
        auth_file=root / "auth.json",
        static_dir=static_dir,
        runtime_dir=root,
        log_dir=root / "log",
        quantarbridge_config=root / "quantarbridge.yml",
        dvmhost_config=root / "dvmhost-config.yml",
        dmr_gateway_config=root / "DMRGateway.ini",
        dmr_to_p25_config=root / "dvmbridge-dmr-to-p25.yml",
        p25_to_dmr_config=root / "dvmbridge-p25-to-dmr.yml",
        brew_audio_config=root / "tetrapack-brew-audio.json",
        brew_audio_status_file=root / "brew-audio-status.json",
        rid_file=root / "rid_acl.dat",
        backup_dir=root / "backups",
        bm_api_key_file=root / "bm_api.key",
        location_event_dir=root / "sms" / "processed",
        identity_cache_file=root / "dashboard-identity-cache.json",
        secure_cookies=False,
        service_units=(),
        restart_targets={},
    )


def write_runtime(config: DashboardConfig) -> None:
    config.log_dir.mkdir()
    config.quantarbridge_config.write_text(
        """brandmeister:
  repeaterId: 123456789
  password: old-password
  address: bm.example
  callsign: N0CALL
  rxFrequency: 430800000
  txFrequency: 438800000
  timeslot: 2
  slot1: false
  slot2: true
routing:
  staticTalkgroups:
    - 262001
  dynamicTimeoutSeconds: 600
  talkgroupMappings:
    - p25: 101
      brandmeister: 262000
sms:
  bmSlot: 2
""",
        encoding="utf-8",
    )
    config.dvmhost_config.write_text(
        """protocols:
  p25:
    motorolaPacketData:
      arsServerAddress: 10.0.0.2
      arsPeerAddress: 10.0.0.1
    motorolaLocation:
      initialDelaySeconds: 5
      updateIntervalSeconds: 300
      noFixRetrySeconds: 60
system:
  config:
    sysId: 001
""",
        encoding="utf-8",
    )
    config.dmr_gateway_config.write_text(
        """[General]
Timeout=10

[DMR Network 1]
Enabled=1
Password=old-password
""",
        encoding="utf-8",
    )
    config.dmr_to_p25_config.write_text(
        """system:
  identity: BRIDGE-DMR-P25
  rxAudioGain: 0.3
  vocoderDecoderAudioGain: 0.4
  vocoderDecoderAutoGain: false
  txAudioGain: 1.1
  vocoderEncoderAudioGain: 0.0
""",
        encoding="utf-8",
    )
    config.p25_to_dmr_config.write_text(
        """system:
  identity: BRIDGE-P25-DMR
  rxAudioGain: 1.0
  vocoderDecoderAudioGain: 0.4
  vocoderDecoderAutoGain: false
  txAudioGain: 2.0
  vocoderEncoderAudioGain: 0.0
  dmrEncodeHighCutHz: 2500
""",
        encoding="utf-8",
    )
    config.brew_audio_config.write_text(
        json.dumps({"enabled": True, "uplinkGain": 2.0}, indent=2) + "\n",
        encoding="utf-8",
    )


def audio_settings() -> dict:
    return {
        "dmrToP25": {
            "rxAudioGain": 0.3,
            "vocoderDecoderAudioGain": 0.4,
            "vocoderDecoderAutoGain": False,
            "vocoderDecoderUvQuality": 12,
            "txAudioGain": 1.1,
            "vocoderEncoderAudioGain": 0.0,
            "p25EncodePresenceGain": 0.0,
            "p25EncodeHighCutHz": 2500.0,
            "p25EncodeAgc": False,
            "p25EncodeAgcTargetRms": 6500.0,
            "p25EncodeAgcMinGain": 0.55,
            "p25EncodeAgcMaxGain": 1.9,
            "p25EncodeAgcAttack": 0.4,
            "p25EncodeAgcRelease": 0.06,
            "p25EncodeAgcPeakLimit": 24000.0,
        },
        "p25ToDmr": {
            "rxAudioGain": 1.0,
            "vocoderDecoderAudioGain": 0.4,
            "vocoderDecoderAutoGain": False,
            "txAudioGain": 2.0,
            "vocoderEncoderAudioGain": 0.0,
            "dmrEncodeHighCutHz": 2500.0,
        },
    }


def network_settings(
    repeater_id: int = 123456789,
    callsign: str = "N0CALL",
    timeslot: int = 2,
    rx_frequency: int = 430800000,
    tx_frequency: int = 438800000,
) -> dict:
    return {
        "repeaterId": repeater_id,
        "brandmeisterCallsign": callsign,
        "brandmeisterTimeslot": timeslot,
        "brandmeisterRxFrequency": rx_frequency,
        "brandmeisterTxFrequency": tx_frequency,
    }


class BrandmeisterProfileMonitorTest(unittest.TestCase):
    def test_transient_yaml_error_does_not_stop_profile_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RuntimeState()
            monitor = BrandmeisterProfileMonitor(
                make_config(Path(directory)), state
            )
            responses = iter(
                [
                    yaml.YAMLError("transient invalid YAML"),
                    {
                        "staticSubscriptions": [
                            {"talkgroup": "262", "slot": "2"}
                        ],
                        "dynamicSubscriptions": [],
                        "timedSubscriptions": [],
                    },
                ]
            )

            def fetch():
                response = next(responses)
                if isinstance(response, Exception):
                    raise response
                return response

            monitor._fetch = fetch
            monitor._stop = TwoCycleStop()
            monitor._run()

            talkgroups = state.snapshot({})["talkgroups"]
            self.assertEqual("ok", talkgroups["status"])
            self.assertEqual(262, talkgroups["static"][0]["talkgroup"])


class AuthStoreTest(unittest.TestCase):
    def test_initialization_verification_and_password_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            store = AuthStore(path)
            store.initialize("admin", "InitialStrongPass!!")

            self.assertTrue(store.verify("admin", "InitialStrongPass!!"))
            self.assertFalse(store.verify("admin", "wrong-password"))
            self.assertNotIn("InitialStrongPass!!", path.read_text(encoding="utf-8"))

            store.change_password("admin", "InitialStrongPass!!", "AnotherStrongPass!!")
            self.assertFalse(store.verify("admin", "InitialStrongPass!!"))
            self.assertTrue(store.verify("admin", "AnotherStrongPass!!"))

    def test_login_limiter_unlocks_after_success(self):
        limiter = LoginLimiter(max_attempts=2, window_seconds=60)
        limiter.fail("127.0.0.1")
        limiter.fail("127.0.0.1")
        self.assertGreater(limiter.retry_after("127.0.0.1"), 0)
        limiter.success("127.0.0.1")
        self.assertEqual(0, limiter.retry_after("127.0.0.1"))


class RuntimeStateTest(unittest.TestCase):
    def test_brandmeister_and_radioid_directory_payloads_are_normalized(self):
        radio = IdentityDirectory._parse_radio_payload(
            1000002,
            {
                "results": [
                    {
                        "radio_id": 1000002,
                        "callsign": "n0call",
                        "fname": "Alex",
                        "surname": "Example",
                        "city": "Example City",
                        "state": "Bayern",
                        "country": "Germany",
                    }
                ]
            },
        )
        talkgroup = IdentityDirectory._parse_talkgroup_payload(
            262000, {"ID": 262000, "Name": "Example Talkgroup"}
        )

        self.assertEqual("N0CALL", radio["callsign"])
        self.assertEqual("Alex Example", radio["name"])
        self.assertEqual("Example Talkgroup", talkgroup["name"])

    def test_motorola_lrrp_point_is_decoded(self):
        packet = (
            "4500002d0001000040111477c633640acb00710a"
            "c02ec02e0019c2f3070f220400000001662266666607d27d28"
        )

        position = decode_motorola_lrrp_position(packet)

        self.assertIsNotNone(position)
        self.assertAlmostEqual(24.1875, position[0], places=5)
        self.assertAlmostEqual(11.0, position[1], places=5)

    def test_radio_registration_gps_and_calls_are_correlated(self):
        state = RuntimeState()
        state.set_mappings([{"p25": 101, "brandmeister": 262000}])
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        state.process_dvmhost_line(
            f"I: {timestamp} (RF) recognized Motorola SCEP ARS registration, "
            "llId = 1000002, subscriberIp = 10.0.0.21, serverIp = 10.0.0.2, n = 1"
        )
        state.process_dvmhost_line(
            f"I: {timestamp} Motorola TMS service acknowledged 00023F00, "
            "llId = 1000002; messaging available"
        )
        state.process_dvmhost_line(
            f"W: {timestamp} Motorola LRRP response has no usable position: "
            "GPS fix is not accurate enough, sourceRid = 1000002, resultCode = $200"
        )
        state.process_activity_line(
            "A: 2025-01-01 12:00:00.000 P25 RF RF voice transmission from 1000002 to TG 101"
        )
        state.process_activity_line(
            "A: 2025-01-01 12:00:01.300 P25 RF RF end of transmission, 1.3 seconds, BER: 0.0%"
        )
        state.update_position(1000002, 51.0, 10.0, time.time())

        snapshot = state.snapshot(
            {1000002: "Example APX"},
            {
                1000002: {
                    "callsign": "N0CALL",
                    "name": "Alex",
                    "city": "Example City",
                    "state": "Bayern",
                    "country": "Germany",
                }
            },
            {262000: {"name": "Example Talkgroup"}},
        )
        self.assertEqual(1, snapshot["summary"]["registeredRadios"])
        self.assertEqual("10.0.0.2", snapshot["connection"]["arsServerAddress"])
        self.assertTrue(snapshot["radios"][0]["tms"])
        self.assertEqual("no_fix", snapshot["radios"][0]["gpsStatus"])
        self.assertEqual("Example APX", snapshot["radios"][0]["label"])
        self.assertEqual("N0CALL", snapshot["radios"][0]["identity"]["displayName"])
        self.assertAlmostEqual(51.0, snapshot["radios"][0]["position"]["latitude"])
        self.assertEqual(262000, snapshot["recentCalls"][0]["mappedTalkgroup"])
        self.assertEqual("N0CALL", snapshot["recentCalls"][0]["sourceIdentity"]["callsign"])
        self.assertEqual("Example Talkgroup", snapshot["recentCalls"][0]["talkgroupName"])
        self.assertEqual(1.3, snapshot["recentCalls"][0]["durationSeconds"])

    def test_quantar_rssi_is_correlated_with_uplink_calls_and_radios(self):
        state = RuntimeState()
        started_at = time.time()

        def stamp(offset: float) -> str:
            return datetime.fromtimestamp(started_at + offset).strftime(
                "%Y-%m-%d %H:%M:%S.%f"
            )[:-3]

        state.process_dvmhost_line(
            f"I: {stamp(-1.0)} (RF) recognized Motorola SCEP ARS registration, "
            "llId = 1000002, subscriberIp = 10.0.0.20, serverIp = 10.0.0.2, n = 1"
        )
        state.process_dvmhost_line(
            f"I: {stamp(-0.1)} Quantar V.24 RSSI sample, ldu = 1, rssi1 = 66"
        )
        state.process_activity_line(
            f"A: {stamp(0.0)} P25 RF RF voice transmission from 1000002 to TG 101"
        )
        state.process_dvmhost_line(
            f"I: {stamp(0.4)} Quantar V.24 RSSI sample, ldu = 1, rssi1 = 78"
        )
        state.process_dvmhost_line(
            f"I: {stamp(0.8)} Quantar V.24 RSSI sample, ldu = 1, rssi1 = 72"
        )

        active = state.snapshot({})["activeCalls"][0]
        self.assertEqual(
            {
                "kind": "quantarRelative",
                "current": 72,
                "average": 72.0,
                "minimum": 66,
                "maximum": 78,
                "samples": 3,
                "updatedAt": active["signal"]["updatedAt"],
            },
            active["signal"],
        )
        self.assertNotIn("_rssiReadings", active)

        state.process_activity_line(
            f"A: {stamp(1.0)} P25 RF RF end of transmission, 1.0 seconds, BER: 0.0%"
        )
        snapshot = state.snapshot({})
        self.assertEqual(72.0, snapshot["recentCalls"][0]["signal"]["average"])
        self.assertEqual(3, snapshot["recentCalls"][0]["signal"]["samples"])
        self.assertEqual(72, snapshot["radios"][0]["signal"]["current"])

    def test_quantar_rssi_loaded_before_activity_log_is_recovered(self):
        state = RuntimeState()
        for timestamp, value in (
            ("12:00:00.100", 60),
            ("12:00:00.500", 70),
            ("12:00:00.900", 80),
        ):
            state.process_dvmhost_line(
                f"I: 2025-01-01 {timestamp} Quantar V.24 RSSI sample, "
                f"ldu = 1, rssi1 = {value}"
            )

        state.process_activity_line(
            "A: 2025-01-01 12:00:00.200 P25 RF RF voice transmission "
            "from 1000002 to TG 101"
        )
        state.process_activity_line(
            "A: 2025-01-01 12:00:01.000 P25 RF RF end of transmission, "
            "0.8 seconds, BER: 0.0%"
        )

        signal = state.snapshot({})["recentCalls"][0]["signal"]
        self.assertEqual(70.0, signal["average"])
        self.assertEqual(60, signal["minimum"])
        self.assertEqual(80, signal["maximum"])
        self.assertEqual(3, signal["samples"])

    def test_quantar_rssi_is_not_assigned_to_downlink_calls(self):
        state = RuntimeState()
        started_at = time.time()
        start = datetime.fromtimestamp(started_at).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]
        sample = datetime.fromtimestamp(started_at + 0.2).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]
        state.process_activity_line(
            f"A: {start} P25 Net network voice transmission from 1000002 to TG 101"
        )
        state.process_dvmhost_line(
            f"I: {sample} Quantar V.24 RSSI sample, "
            "ldu = 1, rssi1 = 90"
        )

        self.assertNotIn("signal", state.snapshot({})["activeCalls"][0])

    def test_stale_radio_registration_is_hidden_until_next_refresh(self):
        state = RuntimeState()
        state.process_dvmhost_line(
            "I: 2025-01-01 12:05:00.000 (RF) recognized Motorola SCEP ARS registration, "
            "llId = 1000001, subscriberIp = 10.0.0.20, serverIp = 10.0.0.2, n = 1"
        )
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        state.process_dvmhost_line(
            f"I: {timestamp} sending Motorola LRRP immediate location request, "
            "llId = 1000001, requestId = 1, subscriberIp = 10.0.0.20, port = 4001"
        )

        snapshot = state.snapshot({})
        self.assertEqual(0, snapshot["summary"]["registeredRadios"])
        self.assertEqual([], snapshot["radios"])
        self.assertEqual(70 * 60, snapshot["radioRegistrationTimeoutSeconds"])

        state.process_dvmhost_line(
            f"I: {timestamp} accepted Motorola ARS refresh, llId = 1000001, "
            "subscriberIp = 10.0.0.20"
        )

        snapshot = state.snapshot({})
        self.assertEqual(1, snapshot["summary"]["registeredRadios"])
        self.assertEqual(1000001, snapshot["radios"][0]["id"])

        restarted_state = RuntimeState()
        restarted_state.process_dvmhost_line(
            f"I: {timestamp} accepted Motorola ARS refresh, llId = 1000001, "
            "subscriberIp = 10.0.0.20"
        )
        snapshot = restarted_state.snapshot({})
        self.assertEqual(1, snapshot["summary"]["registeredRadios"])
        self.assertEqual(1000001, snapshot["radios"][0]["id"])

    def test_radio_disconnect_removes_registration_immediately(self):
        state = RuntimeState()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        state.process_dvmhost_line(
            f"I: {timestamp} (RF) recognized Motorola SCEP ARS registration, "
            "llId = 1000001, subscriberIp = 10.0.0.20, serverIp = 10.0.0.2, n = 1"
        )

        state.process_dvmhost_line(
            f"I: {timestamp} (RF) P25, PDU (Packet Data Unit), DISCONNECT "
            "(Registration Request Disconnect), llId = 1000001"
        )

        snapshot = state.snapshot({})
        self.assertEqual(0, snapshot["summary"]["registeredRadios"])
        self.assertEqual([], snapshot["radios"])

    def test_brandmeister_subscriptions_include_dynamic_expiry(self):
        state = RuntimeState()
        state.set_talkgroup_config(600)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        state.process_brandmeister_line(
            f"I: {timestamp} (HOST) Updated dynamic TG 262002 from RF activity"
        )
        state.set_brandmeister_profile(
            {
                "staticSubscriptions": [
                    {"talkgroup": "262000", "slot": "0"}
                ],
                "dynamicSubscriptions": [{"talkgroup": "262002", "slot": "2"}],
                "timedSubscriptions": [],
            }
        )

        talkgroups = state.snapshot(
            {}, talkgroup_identities={262000: {"name": "Example Static"}, 262002: {"name": "Example Dynamic"}}
        )["talkgroups"]

        self.assertEqual("ok", talkgroups["status"])
        self.assertEqual(262000, talkgroups["static"][0]["talkgroup"])
        self.assertEqual("Example Static", talkgroups["static"][0]["name"])
        self.assertEqual(262002, talkgroups["dynamic"][0]["talkgroup"])
        self.assertEqual("Example Dynamic", talkgroups["dynamic"][0]["name"])
        self.assertIsNotNone(talkgroups["dynamic"][0]["expiresAt"])
        self.assertGreaterEqual(talkgroups["dynamic"][0]["remainingSeconds"], 598)

    def test_latest_rf_activity_resets_dynamic_expiry(self):
        state = RuntimeState()
        state.set_talkgroup_config(600)
        first = datetime.fromtimestamp(time.time() - 300)
        latest = datetime.fromtimestamp(time.time() - 30)
        for timestamp in (first, latest):
            state.process_brandmeister_line(
                f"I: {timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} "
                "(HOST) Updated dynamic TG 262002 from RF activity"
            )
        state.set_brandmeister_profile(
            {
                "staticSubscriptions": [],
                "dynamicSubscriptions": [{"talkgroup": "262002", "slot": "2"}],
                "timedSubscriptions": [],
            }
        )

        dynamic = state.snapshot({})["talkgroups"]["dynamic"][0]

        self.assertGreaterEqual(dynamic["remainingSeconds"], 568)
        self.assertLessEqual(dynamic["remainingSeconds"], 570)

    def test_brandmeister_terminators_close_and_correct_downlink_calls(self):
        state = RuntimeState()
        state.process_activity_line(
            "A: 2025-01-01 12:10:00.000 P25 Net network voice transmission "
            "from 1000101 to TG 101"
        )
        state.process_activity_line(
            "A: 2025-01-01 12:11:00.000 P25 Net network voice transmission "
            "from 1000102 to TG 101"
        )
        state.process_brandmeister_line(
            "I: 2025-01-01 12:10:05.500 (HOST) Flushing delayed BM DMR "
            "terminator to FNE srcId=1000101 dstId=101 slot=2"
        )
        state.process_brandmeister_line(
            "I: 2025-01-01 12:11:00.400 (HOST) Flushing delayed BM DMR "
            "terminator to FNE srcId=1000102 dstId=101 slot=2"
        )

        snapshot = state.snapshot({})
        calls = {call["sourceId"]: call for call in snapshot["recentCalls"]}

        self.assertEqual([], snapshot["activeCalls"])
        self.assertEqual(5.5, calls[1000101]["durationSeconds"])
        self.assertEqual(0.4, calls[1000102]["durationSeconds"])
        self.assertEqual("normal", calls[1000101]["endReason"])

    def test_forwarded_brandmeister_call_sets_channel_busy_without_duplicate(self):
        state = RuntimeState()
        started_at = time.time()
        start = datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        duplicate = datetime.fromtimestamp(started_at + 0.004).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]
        ended = datetime.fromtimestamp(started_at + 5.5).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]
        state.process_brandmeister_line(
            f"I: {start} (HOST) Forwarding BrandMeister DMR to FNE "
            "srcId=1000101 dstId=101 slot=2"
        )

        active = state.snapshot({})["activeCalls"]
        self.assertEqual(1, len(active))
        self.assertEqual("downlink", active[0]["direction"])
        self.assertEqual(1000101, active[0]["sourceId"])
        self.assertEqual(101, active[0]["talkgroup"])

        state.process_activity_line(
            f"A: {duplicate} P25 Net network voice transmission from 1000101 to TG 101"
        )
        snapshot = state.snapshot({})
        self.assertEqual(1, len(snapshot["activeCalls"]))
        self.assertEqual([], snapshot["recentCalls"])

        state.process_brandmeister_line(
            f"I: {ended} (HOST) Flushing delayed BM DMR "
            "terminator to FNE srcId=1000101 dstId=101 slot=2"
        )
        snapshot = state.snapshot({})
        self.assertEqual([], snapshot["activeCalls"])
        self.assertEqual(5.5, snapshot["recentCalls"][0]["durationSeconds"])
        self.assertEqual("normal", snapshot["recentCalls"][0]["endReason"])

    def test_local_disconnect_closes_downlink_without_terminator(self):
        state = RuntimeState()
        started_at = time.time()
        start = datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        disconnected = datetime.fromtimestamp(started_at + 2.0).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]
        state.process_brandmeister_line(
            f"I: {start} (HOST) Forwarding BrandMeister DMR to FNE "
            "srcId=1000101 dstId=101 slot=2"
        )
        state.process_brandmeister_line(
            f"I: {disconnected} (HOST) Received disconnect TG 4000 from RF side, "
            "clearing dynamic TG state"
        )

        snapshot = state.snapshot({})
        self.assertEqual([], snapshot["activeCalls"])
        self.assertEqual(2.0, snapshot["recentCalls"][0]["durationSeconds"])
        self.assertEqual("disconnect", snapshot["recentCalls"][0]["endReason"])

    def test_restart_guard_requires_continuous_idle_time(self):
        state = RuntimeState()

        self.assertEqual(0, state.restart_guard_remaining(15, now=100.0))
        state._start_call("downlink", 1000101, 101, 100.0)
        self.assertEqual(15, state.restart_guard_remaining(15, now=105.0))
        state._finish_call("downlink", 108.0)
        self.assertEqual(11, state.restart_guard_remaining(15, now=112.0))
        self.assertEqual(0, state.restart_guard_remaining(15, now=123.0))

    def test_restart_guard_expires_call_without_terminator(self):
        state = RuntimeState()
        started_at = time.time() - 76.0
        state._start_call("downlink", 1000101, 101, started_at)

        self.assertEqual(
            15, state.restart_guard_remaining(15, now=started_at + 76.0)
        )
        snapshot = state.snapshot({})
        self.assertEqual([], snapshot["activeCalls"])
        self.assertEqual("timeout", snapshot["recentCalls"][0]["endReason"])
        self.assertEqual(76.0, snapshot["recentCalls"][0]["durationSeconds"])


class IdentityDirectoryTest(unittest.TestCase):
    def test_cached_names_remain_available_without_network_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_path = root / "identity-cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "savedAt": time.time(),
                        "radios": {
                            "1000002": {
                                "updatedAt": time.time(),
                                "callsign": "N0CALL",
                                "name": "Alex",
                            }
                        },
                        "talkgroups": {
                            "262000": {
                                "updatedAt": time.time(),
                                "name": "Example Talkgroup",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            directory_cache = IdentityDirectory(cache_path, root / "bm-api.key")
            snapshot = directory_cache.snapshot()

            self.assertEqual("N0CALL", snapshot["radios"][1000002]["callsign"])
            self.assertEqual("Example Talkgroup", snapshot["talkgroups"][262000]["name"])
            self.assertEqual("ok", snapshot["status"]["state"])


class SettingsManagerTest(unittest.TestCase):
    @staticmethod
    def remove_packet_data_config(config):
        host = yaml.safe_load(config.dvmhost_config.read_text(encoding="utf-8"))
        host["protocols"]["p25"].pop("motorolaPacketData", None)
        config.dvmhost_config.write_text(
            yaml.safe_dump(host, sort_keys=False), encoding="utf-8"
        )

    def test_read_uses_known_good_dmr_to_p25_audio_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            write_runtime(config)
            config.dmr_to_p25_config.write_text(
                "system:\n  identity: BRIDGE-DMR-P25\n", encoding="utf-8"
            )
            manager = SettingsManager(config, RuntimeState(), RecordingRestarter())

            settings = manager.read()

            self.assertEqual(audio_settings()["dmrToP25"], settings["audio"]["dmrToP25"])

    def test_read_uses_known_good_p25_to_dmr_audio_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            write_runtime(config)
            config.p25_to_dmr_config.write_text(
                "system:\n  identity: BRIDGE-P25-DMR\n", encoding="utf-8"
            )
            manager = SettingsManager(config, RuntimeState(), RecordingRestarter())

            settings = manager.read()

            self.assertEqual(audio_settings()["p25ToDmr"], settings["audio"]["p25ToDmr"])

    def test_read_does_not_clear_observed_ars_server_address(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            write_runtime(config)
            self.remove_packet_data_config(config)
            state = RuntimeState()
            state.process_dvmhost_line(
                "I: 2025-01-01 12:05:00.000 (RF) recognized Motorola SCEP "
                "ARS registration, llId = 1000001, subscriberIp = 10.0.0.20, "
                "serverIp = 10.0.0.2, n = 1"
            )
            manager = SettingsManager(config, state, RecordingRestarter())

            manager.read()

            self.assertEqual(
                "10.0.0.2", state.snapshot({})["connection"]["arsServerAddress"]
            )

    def test_dashboard_config_can_publish_explicit_ars_server_address(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                make_config(Path(directory)),
                public_ars_server_address="10.0.0.3",
            )
            write_runtime(config)
            self.remove_packet_data_config(config)
            state = RuntimeState()
            manager = SettingsManager(config, state, RecordingRestarter())

            manager.read()

            self.assertEqual(
                "10.0.0.3", state.snapshot({})["connection"]["arsServerAddress"]
            )

    def test_dashboard_config_loads_public_ars_server_address(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "quantar-dashboard.json"
            config_path.write_text(
                json.dumps({"publicArsServerAddress": "10.0.0.3"}),
                encoding="utf-8",
            )

            config = DashboardConfig.load(config_path)

            self.assertEqual("10.0.0.3", config.public_ars_server_address)

    def test_read_publishes_only_public_connection_values(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            write_runtime(config)
            state = RuntimeState()
            manager = SettingsManager(config, state, RecordingRestarter())

            manager.read()
            connection = state.snapshot(
                {}, {}, {262000: {"name": "Example Talkgroup"}}
            )["connection"]

            self.assertEqual("10.0.0.2", connection["arsServerAddress"])
            self.assertEqual(
                [
                    {
                        "p25": 101,
                        "brandmeister": 262000,
                        "name": "Example Talkgroup",
                    }
                ],
                connection["talkgroupMappings"],
            )
            self.assertNotIn("arsPeerAddress", connection)
            self.assertNotIn("10.0.0.1", json.dumps(connection))

    def test_outdated_settings_payload_requests_full_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            write_runtime(config)
            manager = SettingsManager(
                config, RuntimeState(), RecordingRestarter()
            )

            with self.assertRaisesRegex(ValueError, "vollständig neu laden"):
                manager.update({"repeaterId": 123456})

    def test_legacy_slot_flags_are_migrated_to_explicit_timeslot(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            write_runtime(config)
            legacy = config.quantarbridge_config.read_text(encoding="utf-8").replace(
                "  timeslot: 2\n", ""
            )
            config.quantarbridge_config.write_text(legacy, encoding="utf-8")
            restarter = RecordingRestarter()
            manager = SettingsManager(config, RuntimeState(), restarter)

            result = manager.update(
                {
                    **network_settings(),
                    "brandmeisterPassword": "",
                    "dynamicTimeoutSeconds": 600,
                    "talkgroupMappings": [{"p25": 101, "brandmeister": 262000}],
                    "gps": {
                        "initialDelaySeconds": 5,
                        "updateIntervalSeconds": 300,
                        "noFixRetrySeconds": 60,
                    },
                    "audio": audio_settings(),
                }
            )

            bridge = yaml.safe_load(
                config.quantarbridge_config.read_text(encoding="utf-8")
            )
            self.assertTrue(result["changed"])
            self.assertEqual(2, bridge["brandmeister"]["timeslot"])
            self.assertFalse(bridge["brandmeister"]["slot1"])
            self.assertTrue(bridge["brandmeister"]["slot2"])
            self.assertEqual(2, bridge["sms"]["bmSlot"])
            self.assertEqual([["quantarbridge"]], restarter.calls)

    def test_settings_are_written_backed_up_and_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            write_runtime(config)
            state = RuntimeState()
            restarter = RecordingRestarter()
            manager = SettingsManager(config, state, restarter)

            result = manager.update(
                {
                    **network_settings(
                        repeater_id=123456799,
                        callsign="N0CALL",
                        timeslot=1,
                        rx_frequency=430800000,
                        tx_frequency=438800000,
                    ),
                    "brandmeisterPassword": "new-bm-password",
                    "dynamicTimeoutSeconds": 900,
                    "talkgroupMappings": [
                        {"p25": 101, "brandmeister": 262000},
                        {"p25": 1234, "brandmeister": 262001},
                    ],
                    "gps": {
                        "initialDelaySeconds": 10,
                        "updateIntervalSeconds": 600,
                        "noFixRetrySeconds": 90,
                    },
                    "audio": audio_settings(),
                }
            )

            bridge = yaml.safe_load(config.quantarbridge_config.read_text(encoding="utf-8"))
            host = yaml.safe_load(config.dvmhost_config.read_text(encoding="utf-8"))
            self.assertTrue(result["changed"])
            self.assertEqual(123456799, bridge["brandmeister"]["repeaterId"])
            self.assertEqual("N0CALL", bridge["brandmeister"]["callsign"])
            self.assertEqual(1, bridge["brandmeister"]["timeslot"])
            self.assertTrue(bridge["brandmeister"]["slot1"])
            self.assertFalse(bridge["brandmeister"]["slot2"])
            self.assertEqual(430800000, bridge["brandmeister"]["rxFrequency"])
            self.assertEqual(438800000, bridge["brandmeister"]["txFrequency"])
            self.assertEqual(1, bridge["sms"]["bmSlot"])
            self.assertEqual("new-bm-password", bridge["brandmeister"]["password"])
            self.assertEqual(900, bridge["routing"]["dynamicTimeoutSeconds"])
            self.assertEqual(2, len(bridge["routing"]["talkgroupMappings"]))
            self.assertEqual(
                600,
                host["protocols"]["p25"]["motorolaLocation"]["updateIntervalSeconds"],
            )
            self.assertIn(
                "    sysId: 001\n",
                config.dvmhost_config.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                [["dmrgateway", "quantarbridge", "sms-bridge", "dvmhost", "brew-audio"]],
                restarter.calls,
            )
            self.assertTrue((Path(result["backup"]) / "quantarbridge.yml").exists())
            self.assertNotIn("new-bm-password", json.dumps(result))

    def test_audio_change_updates_only_selected_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            write_runtime(config)
            restarter = RecordingRestarter()
            manager = SettingsManager(config, RuntimeState(), restarter)
            audio = audio_settings()
            audio["dmrToP25"]["txAudioGain"] = 7.5
            audio["dmrToP25"]["p25EncodePresenceGain"] = 0.15
            audio["dmrToP25"]["p25EncodeHighCutHz"] = 2700.0
            audio["dmrToP25"]["vocoderDecoderUvQuality"] = 7

            result = manager.update(
                {
                    **network_settings(),
                    "brandmeisterPassword": "",
                    "dynamicTimeoutSeconds": 600,
                    "talkgroupMappings": [
                        {"p25": 101, "brandmeister": 262000}
                    ],
                    "gps": {
                        "initialDelaySeconds": 5,
                        "updateIntervalSeconds": 300,
                        "noFixRetrySeconds": 60,
                    },
                    "audio": audio,
                }
            )

            dmr_to_p25 = yaml.safe_load(
                config.dmr_to_p25_config.read_text(encoding="utf-8")
            )
            self.assertTrue(result["changed"])
            self.assertEqual(7.5, dmr_to_p25["system"]["txAudioGain"])
            self.assertEqual(0.15, dmr_to_p25["system"]["p25EncodePresenceGain"])
            self.assertEqual(2700.0, dmr_to_p25["system"]["p25EncodeHighCutHz"])
            self.assertEqual(7, dmr_to_p25["system"]["vocoderDecoderUvQuality"])
            self.assertEqual([["dmr-to-p25"]], restarter.calls)
            self.assertTrue(
                (Path(result["backup"]) / "dvmbridge-dmr-to-p25.yml").exists()
            )

    def test_p25_uplink_audio_change_reconnects_after_fne_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            write_runtime(config)
            restarter = RecordingRestarter()
            manager = SettingsManager(config, RuntimeState(), restarter)
            audio = audio_settings()
            audio["p25ToDmr"]["txAudioGain"] = 2.4
            audio["p25ToDmr"]["dmrEncodeHighCutHz"] = 2300.0

            manager.update(
                {
                    **network_settings(),
                    "brandmeisterPassword": "",
                    "dynamicTimeoutSeconds": 600,
                    "talkgroupMappings": [
                        {"p25": 101, "brandmeister": 262000}
                    ],
                    "gps": {
                        "initialDelaySeconds": 5,
                        "updateIntervalSeconds": 300,
                        "noFixRetrySeconds": 60,
                    },
                    "audio": audio,
                }
            )

            p25_to_dmr = yaml.safe_load(
                config.p25_to_dmr_config.read_text(encoding="utf-8")
            )
            self.assertEqual(2.4, p25_to_dmr["system"]["txAudioGain"])
            self.assertEqual(2300.0, p25_to_dmr["system"]["dmrEncodeHighCutHz"])
            self.assertEqual([["p25-to-dmr", "brew-audio"]], restarter.calls)

    def test_audio_change_is_rejected_during_recent_radio_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            write_runtime(config)
            original = config.dmr_to_p25_config.read_bytes()
            state = RuntimeState()
            state._start_call("downlink", 1000101, 101, time.time())
            restarter = RecordingRestarter()
            manager = SettingsManager(config, state, restarter)
            audio = audio_settings()
            audio["dmrToP25"]["txAudioGain"] = 2.8

            with self.assertRaisesRegex(SettingsBusyError, "Funkruhe"):
                manager.update(
                    {
                        **network_settings(),
                        "brandmeisterPassword": "",
                        "dynamicTimeoutSeconds": 600,
                        "talkgroupMappings": [
                            {"p25": 101, "brandmeister": 262000}
                        ],
                        "gps": {
                            "initialDelaySeconds": 5,
                            "updateIntervalSeconds": 300,
                            "noFixRetrySeconds": 60,
                        },
                        "audio": audio,
                    }
                )

            self.assertEqual(original, config.dmr_to_p25_config.read_bytes())
            self.assertEqual([], restarter.calls)

    def test_dynamic_talkgroup_timeout_restarts_only_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            write_runtime(config)
            state = RuntimeState()
            restarter = RecordingRestarter()
            manager = SettingsManager(config, state, restarter)

            result = manager.update(
                {
                    **network_settings(),
                    "brandmeisterPassword": "",
                    "dynamicTimeoutSeconds": 1_200,
                    "talkgroupMappings": [
                        {"p25": 101, "brandmeister": 262000}
                    ],
                    "gps": {
                        "initialDelaySeconds": 5,
                        "updateIntervalSeconds": 300,
                        "noFixRetrySeconds": 60,
                    },
                    "audio": audio_settings(),
                }
            )

            bridge = yaml.safe_load(
                config.quantarbridge_config.read_text(encoding="utf-8")
            )
            self.assertTrue(result["changed"])
            self.assertEqual(1_200, bridge["routing"]["dynamicTimeoutSeconds"])
            self.assertEqual(1_200, result["settings"]["dynamicTimeoutSeconds"])
            self.assertEqual([["quantarbridge", "brew-audio"]], restarter.calls)
            self.assertEqual(
                1_200, state.snapshot({})["talkgroups"]["dynamicTimeoutSeconds"]
            )

    def test_failed_restart_restores_all_runtime_files(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            write_runtime(config)
            originals = {
                path: path.read_bytes()
                for path in (
                    config.quantarbridge_config,
                    config.dvmhost_config,
                    config.dmr_gateway_config,
                )
            }
            restarter = RecordingRestarter(fail=True)
            manager = SettingsManager(config, RuntimeState(), restarter)

            with self.assertLogs("quantar-dashboard", level="ERROR"):
                with self.assertRaises(RuntimeError):
                    manager.update(
                        {
                            **network_settings(repeater_id=123456799),
                            "brandmeisterPassword": "new-bm-password",
                            "dynamicTimeoutSeconds": 900,
                            "talkgroupMappings": [{"p25": 101, "brandmeister": 262000}],
                            "gps": {
                                "initialDelaySeconds": 10,
                                "updateIntervalSeconds": 600,
                                "noFixRetrySeconds": 90,
                            },
                            "audio": audio_settings(),
                        }
                    )

            for path, content in originals.items():
                self.assertEqual(content, path.read_bytes())
            self.assertEqual(2, len(restarter.calls))


if __name__ == "__main__":
    unittest.main()
