"""Tests for python/duck_agent/clock.py: min-RTT offset selection, degraded
detection, and slew-limited offset application.

All timestamps are synthetic nanosecond integers -- no real sleeping is
needed for the offset-selection/degraded tests. The slew test uses
explicit `now_ns` arguments to `update_applied_offset` rather than real
time.sleep(), so it's fast and not flaky under load.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duck_agent.clock import Clock, slew_towards  # noqa: E402

NS_PER_S = 1_000_000_000


class SlewTowardsTest(unittest.TestCase):
    def test_reaches_target_when_within_max_step(self) -> None:
        self.assertEqual(slew_towards(0.0, 1.0, max_rate_per_s=10.0, dt_s=1.0), 1.0)

    def test_capped_by_max_rate(self) -> None:
        result = slew_towards(0.0, 100.0, max_rate_per_s=5.0, dt_s=1.0)
        self.assertAlmostEqual(result, 5.0)

    def test_moves_negative_direction(self) -> None:
        result = slew_towards(10.0, 0.0, max_rate_per_s=5.0, dt_s=1.0)
        self.assertAlmostEqual(result, 5.0)

    def test_zero_dt_is_noop(self) -> None:
        self.assertEqual(slew_towards(1.0, 100.0, max_rate_per_s=5.0, dt_s=0.0), 1.0)


class MinRttOffsetSelectionTest(unittest.TestCase):
    def test_offset_taken_from_minimum_rtt_sample(self) -> None:
        clock = Clock()
        # Sample 1: t0=0, t1=1000, t2=1000, t3=3000 -> big rtt (network jitter)
        clock.record_exchange(t0=0, t1=1_000, t2=1_000, t3=3_000)
        # Sample 2: much tighter round trip, different (correct) offset.
        clock.record_exchange(t0=10_000, t1=10_500, t2=10_500, t3=10_600)
        best = clock.best_sample()
        self.assertIsNotNone(best)
        # sample 2 rtt = (10600-10000) - (10500-10500) = 600
        # sample 1 rtt = (3000-0) - (1000-1000) = 3000
        self.assertAlmostEqual(best.rtt_ns, 600)
        self.assertAlmostEqual(clock.target_offset_ns(), best.offset_ns)

    def test_offset_formula(self) -> None:
        clock = Clock()
        # offset = ((t1-t0)+(t2-t3))/2
        t0, t1, t2, t3 = 0, 100, 120, 200
        clock.record_exchange(t0, t1, t2, t3)
        expected_offset = ((t1 - t0) + (t2 - t3)) / 2.0
        self.assertAlmostEqual(clock.target_offset_ns(), expected_offset)

    def test_window_caps_at_eight_samples(self) -> None:
        clock = Clock()
        for i in range(20):
            clock.record_exchange(t0=i * 1000, t1=i * 1000 + 10, t2=i * 1000 + 10, t3=i * 1000 + 20)
        self.assertEqual(clock.sample_count(), 8)


class DegradedTest(unittest.TestCase):
    def test_not_degraded_with_fresh_low_rtt_sample(self) -> None:
        clock = Clock()
        now = 1_000_000_000
        clock.record_exchange(t0=0, t1=10, t2=10, t3=20)  # tiny rtt
        self.assertFalse(clock.degraded(now_ns=now))

    def test_degraded_when_rtt_exceeds_threshold(self) -> None:
        clock = Clock()
        now = 0
        # rtt = (t3-t0)-(t2-t1) = 60_000_000 - 0 = 60ms > 50ms threshold
        clock.record_exchange(t0=0, t1=0, t2=0, t3=60_000_000)
        self.assertTrue(clock.degraded(now_ns=now))

    def test_degraded_when_no_sample_for_10s(self) -> None:
        clock = Clock()
        clock.record_exchange(t0=0, t1=10, t2=10, t3=20)
        far_future = 20 + 11 * NS_PER_S
        self.assertTrue(clock.degraded(now_ns=far_future))

    def test_not_degraded_just_under_10s_since_last_sample(self) -> None:
        clock = Clock()
        clock.record_exchange(t0=0, t1=10, t2=10, t3=20)
        just_under = 20 + int(9.5 * NS_PER_S)
        self.assertFalse(clock.degraded(now_ns=just_under))

    def test_degraded_before_any_sample_after_grace_period(self) -> None:
        clock = Clock(degraded_no_sample_s=10.0)
        # No samples at all yet -- degraded() should become true 10s after
        # the clock was created, using its own creation time as baseline.
        self.assertTrue(clock.degraded(now_ns=clock._created_ns + 11 * NS_PER_S))


class ApplyOffsetSlewTest(unittest.TestCase):
    def test_steps_immediately_when_not_playing(self) -> None:
        clock = Clock()
        clock.record_exchange(t0=0, t1=1_000_000_000, t2=1_000_000_000, t3=0)  # offset ~= 1e9 ns
        applied = clock.update_applied_offset(playing=False, now_ns=0)
        self.assertAlmostEqual(applied, clock.target_offset_ns())

    def test_slews_gradually_while_playing(self) -> None:
        clock = Clock()
        # Sample 1: rtt=1000ns, offset=1e9ns -- this is the initial "best"
        # sample (it's currently the only one).
        clock.record_exchange(t0=0, t1=1_000_000_500, t2=1_000_000_500, t3=1000)
        target = clock.target_offset_ns()
        self.assertAlmostEqual(target, 1_000_000_000)

        # First call establishes a baseline (steps to target on first call,
        # since there's nothing to slew from yet).
        clock.update_applied_offset(playing=True, now_ns=0)
        self.assertAlmostEqual(clock.applied_offset_ns(), target)

        # Sample 2: rtt=500ns (strictly lower than sample 1's 1000ns, so
        # it becomes the new min-RTT / "best" sample unambiguously) with
        # offset=0. Confirm the *next* update moves toward the new target
        # capped at 5 ms/s while playing, not stepped.
        clock.record_exchange(t0=0, t1=250, t2=250, t3=500)
        self.assertAlmostEqual(clock.target_offset_ns(), 0.0)

        applied_after_1s = clock.update_applied_offset(playing=True, now_ns=NS_PER_S)
        # Capped at 5ms/s * 1s = 5_000_000 ns movement toward 0.
        self.assertAlmostEqual(applied_after_1s, target - 5_000_000, delta=1.0)
        self.assertGreater(applied_after_1s, 0.0)  # hasn't reached target yet

    def test_reaches_target_eventually_while_playing(self) -> None:
        clock = Clock()
        clock.record_exchange(t0=0, t1=1_000_000, t2=1_000_000, t3=0)  # 1ms offset, small
        clock.update_applied_offset(playing=True, now_ns=0)
        applied = clock.update_applied_offset(playing=True, now_ns=NS_PER_S)  # 1s later, 5ms budget > 1ms diff
        self.assertAlmostEqual(applied, clock.target_offset_ns())


class ConversionTest(unittest.TestCase):
    def test_local_time_for_master_uses_applied_offset(self) -> None:
        clock = Clock()
        clock.record_exchange(t0=0, t1=1_000_000, t2=1_000_000, t3=0)  # offset = 1_000_000
        clock.update_applied_offset(playing=False, now_ns=0)
        self.assertAlmostEqual(clock.applied_offset_ns(), 1_000_000)
        # local = master_ns - offset
        self.assertEqual(clock.local_time_for_master(5_000_000), 4_000_000)

    def test_estimated_master_time_uses_applied_offset(self) -> None:
        clock = Clock()
        clock.record_exchange(t0=0, t1=1_000_000, t2=1_000_000, t3=0)
        clock.update_applied_offset(playing=False, now_ns=0)
        self.assertEqual(clock.estimated_master_time(local_ns=2_000_000), 3_000_000)


class TelemetryFieldsTest(unittest.TestCase):
    def test_telemetry_offset_and_rtt_ms(self) -> None:
        clock = Clock()
        clock.record_exchange(t0=0, t1=2_000_000, t2=2_000_000, t3=0)  # offset=2ms, rtt=0
        clock.update_applied_offset(playing=False, now_ns=0)
        self.assertAlmostEqual(clock.telemetry_offset_ms(), 2.0)
        self.assertAlmostEqual(clock.telemetry_rtt_ms(), 0.0)

    def test_telemetry_defaults_with_no_samples(self) -> None:
        clock = Clock()
        self.assertAlmostEqual(clock.telemetry_offset_ms(), 0.0)
        self.assertAlmostEqual(clock.telemetry_rtt_ms(), 0.0)


if __name__ == "__main__":
    unittest.main()
