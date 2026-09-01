"""SwarmLink time sync -- docs/swarmlink-protocol.md section 1.

The agent periodically sends `time_req` (embedding its own monotonic
send time t0) and receives `time_resp` (echoing t0, plus the master's
receive/send timestamps t1/t2). On arrival (agent-stamped t3) this module
computes the classic NTP-style offset and round-trip time:

    offset = ((t1 - t0) + (t2 - t3)) / 2      # master_clock - agent_clock, ns
    rtt    = (t3 - t0) - (t2 - t1)             # ns

All four timestamps are nanosecond integers on their *own* clock's
time.monotonic_ns() (docs/swarmlink-protocol.md #4: "wall clocks are
never trusted") -- t0/t3 are the agent's, t1/t2 are the master's.

A sliding window of the last 8 samples is kept; the *offset* used for
scheduling is always the one from the sample with the **minimum RTT**
(the least noisy estimate), not necessarily the most recent sample.

`slewed_offset_ns()` is what callers should use to convert between the
agent's local monotonic clock and the master's: it moves toward the
target (min-RTT) offset at up to `MAX_SLEW_NS_PER_S` while the agent is
PLAYING (so mid-performance corrections are inaudible/invisible -- no
sudden jumps), and steps immediately otherwise (nothing is playing yet,
so there's nothing to protect).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

# Per docs/swarmlink-protocol.md #1.
WINDOW_SIZE = 8
DEGRADED_RTT_MS = 50.0
DEGRADED_NO_SAMPLE_S = 10.0
MAX_SLEW_NS_PER_S = 5_000_000  # 5 ms/s


def slew_towards(current: float, target: float, max_rate_per_s: float, dt_s: float) -> float:
    """Move `current` towards `target` by at most `max_rate_per_s * dt_s`."""
    if dt_s <= 0:
        return current
    diff = target - current
    max_step = max_rate_per_s * dt_s
    if abs(diff) <= max_step:
        return target
    return current + (max_step if diff > 0 else -max_step)


@dataclass(frozen=True)
class TimeSample:
    offset_ns: float
    rtt_ns: float
    received_at_ns: int  # agent monotonic time the sample was recorded (t3)


class Clock:
    """Tracks master<->agent offset from time_req/time_resp exchanges."""

    def __init__(
        self,
        window_size: int = WINDOW_SIZE,
        degraded_rtt_ms: float = DEGRADED_RTT_MS,
        degraded_no_sample_s: float = DEGRADED_NO_SAMPLE_S,
        max_slew_ns_per_s: float = MAX_SLEW_NS_PER_S,
    ):
        self._window_size = window_size
        self._degraded_rtt_ms = degraded_rtt_ms
        self._degraded_no_sample_s = degraded_no_sample_s
        self._max_slew_ns_per_s = max_slew_ns_per_s

        self._lock = threading.Lock()
        self._samples: deque[TimeSample] = deque(maxlen=window_size)
        self._created_ns = time.monotonic_ns()

        self._applied_offset_ns: float = 0.0
        self._last_slew_ns: Optional[int] = None

    # -- feeding samples ---------------------------------------------------

    def record_exchange(self, t0: int, t1: int, t2: int, t3: Optional[int] = None) -> TimeSample:
        """Record one time_req/time_resp round trip. `t3` defaults to
        `time.monotonic_ns()` (the moment the reply is being processed).
        """
        if t3 is None:
            t3 = time.monotonic_ns()
        offset = ((t1 - t0) + (t2 - t3)) / 2.0
        rtt = (t3 - t0) - (t2 - t1)
        sample = TimeSample(offset_ns=offset, rtt_ns=rtt, received_at_ns=t3)
        with self._lock:
            self._samples.append(sample)
        return sample

    # -- window queries ------------------------------------------------

    def best_sample(self) -> Optional[TimeSample]:
        """The sample with the minimum RTT in the current window."""
        with self._lock:
            if not self._samples:
                return None
            return min(self._samples, key=lambda s: s.rtt_ns)

    def target_offset_ns(self) -> Optional[float]:
        best = self.best_sample()
        return best.offset_ns if best is not None else None

    def best_rtt_ms(self) -> Optional[float]:
        best = self.best_sample()
        return best.rtt_ns / 1e6 if best is not None else None

    def sample_count(self) -> int:
        with self._lock:
            return len(self._samples)

    def last_sample_at_ns(self) -> Optional[int]:
        with self._lock:
            if not self._samples:
                return None
            return self._samples[-1].received_at_ns

    def degraded(self, now_ns: Optional[int] = None) -> bool:
        """True when the best-window RTT exceeds the threshold or no
        sample has succeeded for `degraded_no_sample_s` seconds.
        """
        now_ns = now_ns if now_ns is not None else time.monotonic_ns()
        last = self.last_sample_at_ns()
        baseline = last if last is not None else self._created_ns
        if (now_ns - baseline) / 1e9 > self._degraded_no_sample_s:
            return True
        rtt_ms = self.best_rtt_ms()
        if rtt_ms is not None and rtt_ms > self._degraded_rtt_ms:
            return True
        return False

    # -- slewed offset used for scheduling / drift correction --------------

    def update_applied_offset(self, playing: bool, now_ns: Optional[int] = None) -> float:
        """Advance the slewed applied offset toward the current target.

        Call periodically (e.g. once per playback tick). While `playing`
        is True, movement is capped at `max_slew_ns_per_s`; otherwise the
        applied offset steps directly to the target (nothing is playing
        yet, so a step can't cause an audible/visible glitch).
        """
        now_ns = now_ns if now_ns is not None else time.monotonic_ns()
        target = self.target_offset_ns()
        with self._lock:
            if target is None:
                self._last_slew_ns = now_ns
                return self._applied_offset_ns
            if self._last_slew_ns is None:
                self._applied_offset_ns = target
                self._last_slew_ns = now_ns
                return self._applied_offset_ns
            dt_s = (now_ns - self._last_slew_ns) / 1e9
            self._last_slew_ns = now_ns
            if not playing:
                self._applied_offset_ns = target
            else:
                self._applied_offset_ns = slew_towards(
                    self._applied_offset_ns, target, self._max_slew_ns_per_s, dt_s
                )
            return self._applied_offset_ns

    def applied_offset_ns(self) -> float:
        with self._lock:
            return self._applied_offset_ns

    # -- conversions ---------------------------------------------------

    def local_time_for_master(self, master_ns: int) -> int:
        """Local (agent monotonic) time corresponding to a master
        monotonic timestamp, i.e. `at_master_time - offset` from the
        `play`/`seek` command handling in swarmlink-protocol.md #3.
        """
        return int(master_ns - self.applied_offset_ns())

    def estimated_master_time(self, local_ns: Optional[int] = None) -> int:
        local_ns = local_ns if local_ns is not None else time.monotonic_ns()
        return int(local_ns + self.applied_offset_ns())

    # -- telemetry -------------------------------------------------------

    def telemetry_offset_ms(self) -> float:
        return self.applied_offset_ns() / 1e6

    def telemetry_rtt_ms(self) -> float:
        rtt = self.best_rtt_ms()
        return rtt if rtt is not None else 0.0
