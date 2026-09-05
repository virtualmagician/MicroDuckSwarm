"""In-memory state for one mock duck.

Tracks the last-value-wins continuous intents (robot.move/head/pose/mouth),
a discrete-mode string, and a very rough dead-reckoning kinematic estimate
derived by integrating robot.move velocities over time. None of this is
part of the real robotd-api.md surface; it exists purely so the mock is
useful for a later top-down viewer (see the nonstandard `mock.state`
debug request in server.py).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class DuckState:
    name: str
    battery_pct: float = 87.0

    # Discrete/runtime state
    mode: str = "idle"
    enabled: bool = True

    # Dead-reckoning kinematic estimate (mock-only, not part of robotd-api.md)
    x: float = 0.0
    y: float = 0.0
    heading: float = 0.0

    # Last-value-wins continuous intents
    last_move: dict[str, float] = field(
        default_factory=lambda: {"vx": 0.0, "vy": 0.0, "vyaw": 0.0}
    )
    last_head: dict[str, float] = field(
        default_factory=lambda: {
            "neck_pitch": 0.0,
            "head_pitch": 0.0,
            "head_yaw": 0.0,
            "head_roll": 0.0,
        }
    )
    last_pose: dict[str, Any] = field(
        default_factory=lambda: {"z": 0.0, "roll": 0.0, "pitch": 0.0, "active": False}
    )
    last_mouth: dict[str, float] = field(default_factory=lambda: {"open": 0.0})
    last_do: Optional[str] = None
    last_sound: Optional[dict[str, Any]] = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _start_monotonic: float = field(default_factory=time.monotonic, repr=False, compare=False)

    # -- setters (called from the connection handler on receipt of a notification) --

    def set_move(self, params: dict[str, Any]) -> None:
        with self._lock:
            self.last_move = {
                "vx": float(params.get("vx", 0.0)),
                "vy": float(params.get("vy", 0.0)),
                "vyaw": float(params.get("vyaw", 0.0)),
            }

    def set_head(self, params: dict[str, Any]) -> None:
        with self._lock:
            self.last_head = {
                "neck_pitch": float(params.get("neck_pitch", 0.0)),
                "head_pitch": float(params.get("head_pitch", 0.0)),
                "head_yaw": float(params.get("head_yaw", 0.0)),
                "head_roll": float(params.get("head_roll", 0.0)),
            }

    def set_pose(self, params: dict[str, Any]) -> None:
        with self._lock:
            self.last_pose = {
                "z": float(params.get("z", 0.0)),
                "roll": float(params.get("roll", 0.0)),
                "pitch": float(params.get("pitch", 0.0)),
                "active": bool(params.get("active", False)),
            }

    def set_mouth(self, params: dict[str, Any]) -> None:
        with self._lock:
            self.last_mouth = {"open": float(params.get("open", 0.0))}

    def stop(self) -> None:
        """robot.stop: zero locomotion immediately.

        docs/robotd-api.md is explicit that this leaves the duck standing --
        `enabled` is untouched here on purpose.
        """
        with self._lock:
            self.last_move = {"vx": 0.0, "vy": 0.0, "vyaw": 0.0}

    def relax(self) -> None:
        """robot.relax: release torque.

        Modelled, not documented -- see the warning in
        docs/swarmlink-protocol.md. robotd's own signature is `{}` -> ack and
        says nothing about the effect, so this encodes the assumption the
        agent is written against: torque off, and no velocity left standing
        for the deadman to inherit.
        """
        with self._lock:
            self.enabled = False
            self.last_move = {"vx": 0.0, "vy": 0.0, "vyaw": 0.0}

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self.enabled = bool(on)

    # -- kinematics --

    def integrate(self, dt: float) -> None:
        """Dead-reckon x/y/heading forward by dt seconds using last_move.

        Trunk-frame vx/vy are rotated into the world frame by the current
        heading estimate before integrating; vyaw integrates directly into
        heading. This is a mock-only convenience, not a physical model.
        """
        if dt <= 0.0:
            return
        with self._lock:
            if not self.enabled:
                # Torque off: whatever velocity was last commanded, the duck
                # is not going anywhere. Without this a relaxed duck would
                # keep dead-reckoning across the stage in telemetry.
                return
            vx, vy, vyaw = (
                self.last_move["vx"],
                self.last_move["vy"],
                self.last_move["vyaw"],
            )
            self.heading += vyaw * dt
            c = math.cos(self.heading)
            s = math.sin(self.heading)
            self.x += (vx * c - vy * s) * dt
            self.y += (vx * s + vy * c) * dt

    # -- snapshots for RPC replies --

    def head_snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self.last_head)

    def mock_state_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "x": self.x,
                "y": self.y,
                "heading": self.heading,
                "mode": self.mode,
                "last_intents": {
                    "move": dict(self.last_move),
                    "head": dict(self.last_head),
                    "pose": dict(self.last_pose),
                    "mouth": dict(self.last_mouth),
                    "do": self.last_do,
                    "sound": self.last_sound,
                },
            }

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            uptime_s = time.monotonic() - self._start_monotonic
            return {
                "battery_pct": self.battery_pct,
                "cpu_temp_c": 42.0,
                "uptime_s": uptime_s,
                "enabled": self.enabled,
                "errors": [],
                "ok": True,
            }
