"""
Tests for Threshold Declarative Configuration (tests/test_config.py).
Verifies:
1. Loading and parsing valid JSON configuration.
2. Validation rules (household, timezone, time formats, duplicate IDs, day names).
3. Seeding routines into SQLite store.
4. Preserving mutable routine facts (last_satisfied_at, snoozed_until) on re-seeding.
5. Device ID verification against Ring client with warning generation.
6. Threshold example config validity.
"""

from datetime import datetime, timezone
import json
import os
import sqlite3
import tempfile
import unittest

from src.threshold.config import (
    AgentConfig,
    RoutineConfig,
    ThresholdConfig,
    find_config_file,
    load_config_file,
    load_config_from_dict,
    seed_routines_from_config,
    validate_config,
)
from src.threshold.store import init_db, list_routines, update_routine_last_satisfied


class MockRingDeviceClient:
    """Mock RingClient returning known device list for verification."""

    def __init__(self, known_ids):
        self.known_ids = known_ids

    def list_devices(self):
        return [{"id": did, "attributes": {"name": f"Device {did}"}} for did in self.known_ids]


class TestThresholdConfig(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_load_valid_config_from_dict(self):
        """Valid configuration dictionary is successfully parsed into ThresholdConfig."""
        data = {
            "household": "Grandma's House",
            "timezone": "America/Chicago",
            "poll_interval_seconds": 180,
            "tick_interval_seconds": 30,
            "callback_interval_seconds": 1.5,
            "dashboard_port": 8080,
            "routines": [
                {
                    "id": "morning_medicine",
                    "name": "Morning Medicine",
                    "device_id": "ava1.ring.device.SENSOR_1",
                    "window_start": "07:30",
                    "window_end": "09:00",
                    "days": ["mon", "tue", "wed", "thu", "fri"],
                    "escalation_delay_minutes": 20,
                    "snapshot_device_id": "ava1.ring.device.CAM_1",
                }
            ],
        }
        cfg = load_config_from_dict(data)
        self.assertEqual(cfg.household, "Grandma's House")
        self.assertEqual(cfg.timezone, "America/Chicago")
        self.assertEqual(cfg.poll_interval_seconds, 180.0)
        self.assertEqual(len(cfg.routines), 1)

        r = cfg.routines[0]
        self.assertEqual(r.id, "morning_medicine")
        self.assertEqual(r.window_start, "07:30")
        self.assertEqual(r.window_end, "09:00")
        self.assertEqual(r.snapshot_device_id, "ava1.ring.device.CAM_1")

    def test_example_config_file_is_valid(self):
        """threshold.example.json in repo root parses and validates cleanly."""
        self.assertTrue(os.path.exists("threshold.example.json"))
        cfg = load_config_file("threshold.example.json")
        self.assertEqual(cfg.household, "Mom's House — Dallas")
        self.assertEqual(len(cfg.routines), 2)
        self.assertEqual(cfg.routines[0].id, "morning_medicine")
        self.assertEqual(cfg.routines[1].id, "evening_porch")

    def test_validation_empty_household_raises_value_error(self):
        """Empty household name raises ValueError."""
        data = {"household": "", "timezone": "America/Chicago", "routines": []}
        with self.assertRaises(ValueError) as ctx:
            load_config_from_dict(data)
        self.assertIn("household", str(ctx.exception).lower())

    def test_validation_invalid_timezone_raises_value_error(self):
        """Invalid timezone name raises ValueError."""
        data = {"household": "Test Home", "timezone": "Not/A_Real_Timezone", "routines": []}
        with self.assertRaises(ValueError) as ctx:
            load_config_from_dict(data)
        self.assertIn("timezone", str(ctx.exception).lower())

    def test_validation_invalid_window_time_format(self):
        """Invalid HH:MM window times raise ValueError."""
        for bad_time in ["25:00", "7:00", "07:60", "bad", "12:0"]:
            data = {
                "household": "Test Home",
                "timezone": "America/Chicago",
                "routines": [
                    {
                        "id": "rt_1",
                        "name": "Test",
                        "device_id": "dev_1",
                        "window_start": bad_time,
                        "window_end": "09:00",
                    }
                ],
            }
            with self.assertRaises(ValueError):
                load_config_from_dict(data)

    def test_validation_duplicate_routine_id(self):
        """Duplicate routine IDs raise ValueError."""
        data = {
            "household": "Test Home",
            "timezone": "America/Chicago",
            "routines": [
                {"id": "rt_dupe", "name": "First", "device_id": "dev_1", "window_start": "07:00", "window_end": "08:00"},
                {"id": "rt_dupe", "name": "Second", "device_id": "dev_2", "window_start": "08:00", "window_end": "09:00"},
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            load_config_from_dict(data)
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_validation_invalid_days(self):
        """Unrecognized day name raises ValueError."""
        data = {
            "household": "Test Home",
            "timezone": "America/Chicago",
            "routines": [
                {
                    "id": "rt_1",
                    "name": "Test",
                    "device_id": "dev_1",
                    "window_start": "07:00",
                    "window_end": "08:00",
                    "days": ["mon", "funday"],
                }
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            load_config_from_dict(data)
        self.assertIn("funday", str(ctx.exception).lower())

    def test_seed_routines_from_config(self):
        """seed_routines_from_config inserts routines into SQLite database."""
        cfg = load_config_file("threshold.example.json")
        res = seed_routines_from_config(self.conn, cfg)

        self.assertEqual(res["seeded_count"], 2)
        self.assertEqual(res["routines"], ["morning_medicine", "evening_porch"])

        db_routines = list_routines(self.conn)
        self.assertEqual(len(db_routines), 2)
        r0 = db_routines[0]
        self.assertEqual(r0["routine_id"], "evening_porch")
        self.assertEqual(r0["snapshot_device_id"], "ava1.ring.device.EXAMPLE_FRONT_DOORBELL")

    def test_seed_routines_preserves_existing_facts(self):
        """Re-seeding routines preserves last_satisfied_at facts."""
        cfg = load_config_file("threshold.example.json")
        seed_routines_from_config(self.conn, cfg)

        # Record last_satisfied_at fact
        test_ts = "2026-10-13T08:15:00Z"
        update_routine_last_satisfied(self.conn, "morning_medicine", test_ts)

        # Re-seed config
        seed_routines_from_config(self.conn, cfg)

        # Verify fact preserved
        cur = self.conn.execute("SELECT last_satisfied_at FROM routines WHERE routine_id = 'morning_medicine'")
        row = cur.fetchone()
        self.assertEqual(row[0], test_ts)

    def test_seed_routines_with_device_verification_generates_warnings(self):
        """Unknown device_id generates non-fatal warnings when client provided."""
        cfg = load_config_file("threshold.example.json")
        mock_client = MockRingDeviceClient(known_ids=["ava1.ring.device.DIFFERENT_DEVICE"])
        res = seed_routines_from_config(self.conn, cfg, client=mock_client)  # type: ignore
        self.assertEqual(res["seeded_count"], 2)
        self.assertTrue(len(res["warnings"]) > 0)
        self.assertTrue(any("was not found" in w for w in res["warnings"]))

    def test_validation_zero_duration_window(self):
        """Identical window_start and window_end raises ValueError (zero-duration window)."""
        data = {
            "household": "Test Home",
            "timezone": "America/Chicago",
            "routines": [
                {
                    "id": "rt_zero",
                    "name": "Zero Duration",
                    "device_id": "dev_1",
                    "window_start": "08:00",
                    "window_end": "08:00",
                }
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            load_config_from_dict(data)
        self.assertIn("zero-duration", str(ctx.exception).lower())

    def test_env_var_expansion_in_config(self):
        """Environment variables (${VAR}) in configuration are expanded."""
        os.environ["TEST_RING_DOORBELL"] = "ava1.ring.device.EXPANDED_DOORBELL"
        os.environ["TEST_HOUSEHOLD_NAME"] = "Expanded Household"
        try:
            data = {
                "household": "${TEST_HOUSEHOLD_NAME}",
                "timezone": "America/Chicago",
                "routines": [
                    {
                        "id": "rt_env",
                        "name": "Doorbell Routine",
                        "device_id": "${TEST_RING_DOORBELL}",
                        "window_start": "08:00",
                        "window_end": "09:00",
                    }
                ],
            }
            cfg = load_config_from_dict(data)
            self.assertEqual(cfg.household, "Expanded Household")
            self.assertEqual(cfg.routines[0].device_id, "ava1.ring.device.EXPANDED_DOORBELL")
        finally:
            del os.environ["TEST_RING_DOORBELL"]
            del os.environ["TEST_HOUSEHOLD_NAME"]

    def test_seed_routines_with_purge_unlisted(self):
        """purge_unlisted=True removes existing routines from DB that are not in the config."""
        # First seed with example (morning_medicine, evening_porch)
        cfg_full = load_config_file("threshold.example.json")
        seed_routines_from_config(self.conn, cfg_full)
        self.assertEqual(len(list_routines(self.conn)), 2)

        # Create smaller config with only morning_medicine
        single_data = {
            "household": "Test Home",
            "timezone": "America/Chicago",
            "routines": [
                {
                    "id": "morning_medicine",
                    "name": "Morning Meds",
                    "device_id": "dev_1",
                    "window_start": "07:00",
                    "window_end": "08:00",
                }
            ],
        }
        cfg_single = load_config_from_dict(single_data)

        # Seed with purge_unlisted=True
        res = seed_routines_from_config(self.conn, cfg_single, purge_unlisted=True)
        self.assertEqual(res["seeded_count"], 1)
        self.assertEqual(res["purged_count"], 1)
        self.assertEqual(res["purged_routines"], ["evening_porch"])

        remaining = list_routines(self.conn)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["routine_id"], "morning_medicine")

    def test_cli_init_command(self):
        """python3 -m src.threshold init creates config from template and respects force flag."""
        import argparse
        from src.threshold.__main__ import run_init_command

        with tempfile.TemporaryDirectory() as tmpdir:
            target_file = os.path.join(tmpdir, "test_threshold.json")
            args = argparse.Namespace(target=target_file, force=False)

            # First run: should create file
            code = run_init_command(args)
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(target_file))

            # Modify file
            with open(target_file, "w") as f:
                f.write('{"custom": true}')

            # Second run without force: should fail
            code_fail = run_init_command(args)
            self.assertEqual(code_fail, 1)

            # Third run with force: should succeed and overwrite
            args.force = True
            code_force = run_init_command(args)
            self.assertEqual(code_force, 0)
            with open(target_file, "r") as f:
                content = f.read()
            self.assertIn("Mom's House", content)

    def test_agent_config_defaults(self):
        """ThresholdConfig parses agent defaults cleanly."""
        data = {
            "household": "Test House",
            "timezone": "America/Chicago",
            "routines": [],
        }
        cfg = load_config_from_dict(data)
        self.assertIsNotNone(cfg.agent)
        self.assertFalse(cfg.agent.enabled)
        self.assertEqual(cfg.agent.provider, "mock")
        self.assertEqual(cfg.agent.quiet_hours, ["23:00", "05:00"])
        self.assertEqual(cfg.agent.dedupe_minutes, 30)
        self.assertEqual(cfg.agent.max_per_tick, 3)

    def test_agent_config_validation_rules(self):
        """validate_config enforces strict agent configuration rules when enabled."""
        base_data = {
            "household": "Test House",
            "timezone": "America/Chicago",
            "routines": [],
        }

        # 1. Quiet hours length != 2
        bad_qh = dict(base_data, agent={"enabled": True, "quiet_hours": ["23:00"]})
        with self.assertRaises(ValueError) as ctx:
            load_config_from_dict(bad_qh)
        self.assertIn("quiet_hours", str(ctx.exception))

        # 2. Quiet hours invalid time format
        bad_time = dict(base_data, agent={"enabled": True, "quiet_hours": ["25:00", "05:00"]})
        with self.assertRaises(ValueError) as ctx:
            load_config_from_dict(bad_time)
        self.assertIn("quiet_hours", str(ctx.exception))

        # 3. Unknown provider
        bad_prov = dict(base_data, agent={"enabled": True, "provider": "llama"})
        with self.assertRaises(ValueError) as ctx:
            load_config_from_dict(bad_prov)
        self.assertIn("Unknown agent provider", str(ctx.exception))

        # 4. Unimplemented provider
        unimp_prov = dict(base_data, agent={"enabled": True, "provider": "anthropic"})
        with self.assertRaises(ValueError) as ctx:
            load_config_from_dict(unimp_prov)
        self.assertIn("not implemented yet", str(ctx.exception))

        # 5. Invalid max_per_tick
        bad_tick = dict(base_data, agent={"enabled": True, "max_per_tick": 0})
        with self.assertRaises(ValueError) as ctx:
            load_config_from_dict(bad_tick)
        self.assertIn("max_per_tick", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


