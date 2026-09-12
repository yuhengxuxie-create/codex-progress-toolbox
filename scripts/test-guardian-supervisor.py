"""Offline policy tests; no Task Scheduler registration or production process access."""
import importlib.util
from pathlib import Path
import unittest
import tempfile
import sys
import os

spec = importlib.util.spec_from_file_location("supervisor", Path(__file__).parents[1] / "installer/guardian-supervisor.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def status(**changes):
    value = dict(schema_version=1, available=True, desired_state="running",
                 guardian=dict(running=False, healthy=False, pid=42, creation_time=123, heartbeat_at=1))
    value.update(changes)
    return value


class RecoveryPolicyTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows token API')
    def test_current_windows_user_sid_is_resolved(self):
        self.assertRegex(module.current_user_sid(), r'^S-1-5-21-\d+-\d+-\d+-\d+$')

    def test_stop_preserves_communication_recovery(self):
        action, state = module.evaluate(status(desired_state="stopped"), {"suspect_since":1}, 200)
        self.assertEqual(action, "crashed")
        self.assertEqual(state["expected_pid"], 42)

    def test_explicit_exit_and_maintenance_never_recover(self):
        for intent in ("exited", "maintenance"):
            action, state = module.evaluate(status(desired_state=intent), {"suspect_since":1}, 200)
            self.assertEqual(action, "suppressed")
            self.assertNotIn("expected_pid", state)

    def test_observation_and_backoff_prevent_restart_storm(self):
        action, state = module.evaluate(status(), {}, 100)
        self.assertEqual(action, "observing")
        self.assertEqual(module.evaluate(status(), state, 110)[0], "observing")
        action, state = module.evaluate(status(), state, 220)
        self.assertEqual(action, "crashed")
        self.assertEqual(state["next_attempt_at"], 280)
        action, state = module.evaluate(status(), state, 230)
        self.assertEqual(action, "observing")
        self.assertEqual(len(state["attempts"]), 1)

    def test_three_attempts_latch_until_explicit_reset(self):
        original = {"attempts":[100,250,450],"suspect_since":450,"next_attempt_at":1050}
        action, state = module.evaluate(status(), original, 600)
        self.assertEqual(action, "circuit_open")
        self.assertTrue(state["circuit_open"])
        self.assertEqual(module.evaluate(status(), state, 5000)[0], "circuit_open")

    def test_future_clock_and_invalid_identity_fail_closed(self):
        future = status(guardian=dict(running=True,healthy=False,pid=42,creation_time=123,heartbeat_at=500))
        self.assertEqual(module.evaluate(future,{"suspect_since":1},200)[0], "unavailable")
        missing = status(guardian=dict(running=True,healthy=False,pid=0,creation_time=0,heartbeat_at=1))
        self.assertEqual(module.evaluate(missing,{"suspect_since":1},200)[0], "unavailable")
        self.assertEqual(module.evaluate(status(),{"last_check_at":500},200)[0], "unavailable")

    def test_zero_identity_only_for_absent_instance(self):
        absent = status(guardian=dict(running=False,healthy=False,pid=None,creation_time=None,heartbeat_at=None))
        action, state = module.evaluate(absent,{"suspect_since":1},200)
        self.assertEqual((action,state["expected_pid"],state["expected_creation_time"]),("crashed",0,0))

    def test_unconfigured_never_bootstraps_service(self):
        self.assertEqual(module.evaluate(status(available=False),{},200)[0], "unavailable")

    def test_live_without_first_heartbeat_eventually_recovers(self):
        value=status(guardian=dict(running=True,healthy=False,pid=42,creation_time=123,heartbeat_at=None))
        self.assertEqual(module.evaluate(value,{'suspect_since':100},189)[0], 'observing')
        self.assertEqual(module.evaluate(value,{'suspect_since':100},190)[0], 'unresponsive')

    def test_cli_timeout_and_overflow_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'progress-wx.py'
            config=dict(python=sys.executable,backend=directory)
            path.write_text('import time; time.sleep(60)')
            with self.assertRaises(TimeoutError):
                module.run_cli(config,'guardian-status',timeout=.15)
            path.write_text('print("x"*70000)')
            with self.assertRaisesRegex(RuntimeError,'too_large'):
                module.run_cli(config,'guardian-status',timeout=3)

    def test_corrupt_and_oversized_sidecar_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            for content in ('broken', '[]', ' '*65537):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    module.read_json(path)

    def test_healthy_interval_resets_transient_budget(self):
        healthy = status(guardian=dict(running=True,healthy=True,pid=42,creation_time=123,heartbeat_at=200))
        action,state=module.evaluate(healthy,{"healthy_since":50,"attempts":[1,2]},200)
        self.assertEqual((action,state["attempts"]),("healthy",[]))


if __name__ == "__main__":
    unittest.main()
