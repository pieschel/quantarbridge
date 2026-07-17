import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "bm_static_sync.py"
)
SPEC = importlib.util.spec_from_file_location("bm_static_sync", SCRIPT_PATH)
SYNC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SYNC)

GUARD_PATH = (
    Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "bm_static_guard.py"
)
GUARD_SPEC = importlib.util.spec_from_file_location("bm_static_guard", GUARD_PATH)
GUARD = importlib.util.module_from_spec(GUARD_SPEC)
assert GUARD_SPEC.loader is not None
GUARD_SPEC.loader.exec_module(GUARD)


class BrandmeisterStaticSyncTest(unittest.TestCase):
    def test_runtime_mapping_is_preserved_while_static_tgs_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quantarbridge.yml"
            path.write_text(
                """brandmeister:
  repeaterId: 123456799
routing:
  staticTalkgroups:
    - 262001
  talkgroupMappings:
    - p25: 1234
      brandmeister: 26299

sms:
  enabled: true
""",
                encoding="utf-8",
            )

            self.assertTrue(SYNC.update_quantarbridge_config(path, [262, 262001]))
            updated = path.read_text(encoding="utf-8")
            self.assertIn("    - 262\n", updated)
            self.assertIn("    - p25: 1234\n      brandmeister: 26299\n", updated)
            self.assertEqual(123456799, SYNC.read_configured_device_id(path))

    def test_missing_mapping_gets_an_empty_configurable_list(self):
        source = """routing:
  staticTalkgroups:
    - 262001
sms:
  enabled: true
"""
        updated = SYNC.ensure_talkgroup_mappings(source)
        self.assertIn("  talkgroupMappings: []\n", updated)

    def test_repeater_profile_is_filtered_to_configured_timeslot(self):
        payload = {
            "staticSubscriptions": [
                {"talkgroup": "262", "slot": "1"},
                {"talkgroup": "262000", "slot": "2"},
                {"talkgroup": "262001", "slot": "2"},
                {"talkgroup": "262002", "slot": "0"},
            ]
        }
        self.assertEqual(
            [262000, 262001], SYNC.static_talkgroups_for_slot(payload, 2)
        )

    def test_api_slot_is_zero_for_hotspot_and_timeslot_for_repeater(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quantarbridge.yml"
            path.write_text(
                """brandmeister:
  repeaterId: 123456789
  timeslot: 2
""",
                encoding="utf-8",
            )
            self.assertEqual(0, SYNC.read_configured_api_slot(path))
            path.write_text(
                """brandmeister:
  repeaterId: 1000001
  timeslot: 2
""",
                encoding="utf-8",
            )
            self.assertEqual(0, SYNC.read_configured_api_slot(path))
            path.write_text(
                """brandmeister:
  repeaterId: 123456
  timeslot: 2
""",
                encoding="utf-8",
            )
            self.assertEqual(2, SYNC.read_configured_api_slot(path))

    def test_repeater_profile_uses_slot_zero_during_initial_migration(self):
        payload = {
            "staticSubscriptions": [
                {"talkgroup": "262000", "slot": "0"},
            ]
        }
        self.assertEqual(
            [262000], SYNC.configured_static_talkgroups(payload, 2)
        )

    def test_talkgroup_rules_use_configured_runtime_peer_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            configs = {
                "dvmhost-config.yml": "network:\n  id: 9100110\n",
                "dvmbridge-p25-to-dmr.yml": "network:\n  id: 9100111 # bridge\n",
                "quantarbridge.yml": 'fne:\n  peerId: "9100101"\n',
                "dvmbridge-dmr-to-p25.yml": "network:\n  id: 9100112\n",
            }
            for name, config in configs.items():
                (runtime / name).write_text(config, encoding="utf-8")

            rules = runtime / "talkgroup_rules.yml"
            self.assertTrue(SYNC.update_talkgroup_rules(rules, [262000]))
            rendered = rules.read_text(encoding="utf-8")

            for peer_id in (9100110, 9100111, 9100101, 9100112):
                self.assertIn(f"        - {peer_id}\n", rendered)
            self.assertNotIn("        - 9000110\n", rendered)

    def test_partial_runtime_peer_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / "dvmhost-config.yml").write_text(
                "network:\n  id: 9100110\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(RuntimeError, "missing"):
                SYNC.update_talkgroup_rules(runtime / "talkgroup_rules.yml", [])

    def test_static_guard_reads_required_talkgroups_from_runtime_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quantarbridge.yml"
            path.write_text(
                """brandmeister:
  repeaterId: 123456
  timeslot: 2
routing:
  staticTalkgroups:
    - 262000
    - invalid
""",
                encoding="utf-8",
            )
            self.assertEqual((123456, 2), GUARD.read_device_and_slot(path))
            self.assertEqual({262000}, GUARD.read_required_talkgroups(path))

    @mock.patch.object(SYNC.subprocess, "run")
    def test_static_sync_restarts_both_bridges_after_fne(self, run):
        services = [
            "dvmfne.service",
            "quantarbridge.service",
            "dvmbridge-dmr-to-p25.service",
            "dvmhost.service",
            "tetrapack-brew-bridge.service",
        ]
        SYNC.restart_services(services)
        self.assertEqual(
            [
                mock.call(["systemctl", "restart", service], check=True)
                for service in (
                    "dvmfne.service",
                    "dvmbridge-p25-to-dmr.service",
                    "dvmbridge-dmr-to-p25.service",
                    "quantarbridge.service",
                )
            ],
            run.call_args_list,
        )
        flattened = " ".join(str(call) for call in run.call_args_list)
        self.assertNotIn("dvmhost.service", flattened)
        self.assertNotIn("tetrapack-brew-bridge.service", flattened)

    def test_audio_bridges_follow_fne_restarts(self):
        deploy = SCRIPT_PATH.parents[1]
        for name in (
            "dvmbridge-p25-to-dmr.service",
            "dvmbridge-dmr-to-p25.service",
        ):
            unit = (deploy / name).read_text(encoding="utf-8")
            self.assertIn("Requires=dvmfne.service", unit)
            self.assertIn("PartOf=dvmfne.service", unit)


if __name__ == "__main__":
    unittest.main()
