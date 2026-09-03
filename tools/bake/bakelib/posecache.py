"""Assembles and writes a duckbake/1 pose cache -- see docs/bake-format.md
for the full schema this mirrors.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import time
from pathlib import Path

import mujoco
import numpy as np
import onnxruntime as ort

from . import duckmodel, policyset, sim

CACHE_FORMAT = "duckbake/1"

# Bumped whenever this baker's observation/action/output conventions
# change in a way that would make an old cache silently wrong rather than
# just stale -- folded into the cache key alongside the show hash and
# policy hashes docs/viewer.md already calls for ("a cache keyed by show
# hash and policy versions").
BAKE_LAYOUT_VERSION = 1

_ROUND = {
    "x": 4, "y": 4, "heading": 5,
    "headYaw": 5, "headPitch": 5, "headRoll": 5, "neckPitch": 5,
    "bodyZ": 5, "bodyRoll": 5, "bodyPitch": 5,
    "mouthOpen": 4, "walkPhase": 4,
}


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _round_list(arr: np.ndarray, ndigits: int) -> list[float]:
    return [round(float(v), ndigits) for v in arr]


def build_cache(
    *,
    show_path: Path,
    show_bytes: bytes,
    show_doc: dict,
    policies: policyset.PolicySet,
    results: dict[str, sim.RoleBakeResult],
    duration: float,
    wall_clock_s: float,
    roles_requested: list[str],
) -> dict:
    show_hash = _sha256_bytes(show_bytes)
    physics_params = {
        "timestep": duckmodel.PHYSICS_TIMESTEP,
        "decimation": duckmodel.CONTROL_DECIMATION,
        "control_hz": duckmodel.CONTROL_HZ,
        "scene": duckmodel.SCENE_FILENAME,
    }
    key_material = "|".join([
        f"show={show_hash}",
        f"policies={policies.combined_hash}",
        f"physics={json.dumps(physics_params, sort_keys=True)}",
        f"layout={BAKE_LAYOUT_VERSION}",
    ])
    cache_key = _sha256_bytes(key_material.encode("utf-8"))

    frame_rate = duckmodel.CONTROL_HZ
    poses = {}
    log: list[dict] = []
    unsimulated_roles = []
    fallen_roles = []
    for role, r in results.items():
        poses[role] = {
            "x": _round_list(r.x, _ROUND["x"]),
            "y": _round_list(r.y, _ROUND["y"]),
            "heading": _round_list(r.heading, _ROUND["heading"]),
            "headYaw": _round_list(r.head_yaw, _ROUND["headYaw"]),
            "headPitch": _round_list(r.head_pitch, _ROUND["headPitch"]),
            "headRoll": _round_list(r.head_roll, _ROUND["headRoll"]),
            "neckPitch": _round_list(r.neck_pitch, _ROUND["neckPitch"]),
            "bodyZ": _round_list(r.body_z, _ROUND["bodyZ"]),
            "bodyRoll": _round_list(r.body_roll, _ROUND["bodyRoll"]),
            "bodyPitch": _round_list(r.body_pitch, _ROUND["bodyPitch"]),
            "mouthOpen": _round_list(r.mouth_open, _ROUND["mouthOpen"]),
            "walkPhase": _round_list(r.walk_phase, _ROUND["walkPhase"]),
        }
        for entry in r.log:
            log.append(entry.to_json())
        if not r.simulated:
            unsimulated_roles.append(role)
        if any(e.kind == "fell" for e in r.log):
            fallen_roles.append(role)

    log.sort(key=lambda e: (e["role"], e["t"]))

    return {
        "format": CACHE_FORMAT,
        "cache_key": cache_key,
        "show": {
            "path": str(show_path),
            "sha256": show_hash,
            "name": (show_doc.get("meta") or {}).get("name"),
            "duration": duration,
        },
        "policies": {
            "dir": str(policies.policies_dir),
            "combined_sha256": policies.combined_hash,
            "file_sha256": policies.file_hashes,
            "locomotion_policy": policyset.LOCOMOTION_POLICY_FILE,
        },
        "physics": physics_params,
        "bake": {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "baker": "tools/bake",
            "layout_version": BAKE_LAYOUT_VERSION,
            "wall_clock_s": round(wall_clock_s, 3),
            "roles_requested": roles_requested,
            "engine": {
                "python": sys.version.split()[0],
                "mujoco": mujoco.__version__,
                "onnxruntime": ort.__version__,
                "numpy": np.__version__,
                "platform": platform.platform(),
                "machine": platform.machine(),
            },
        },
        "frame_rate": frame_rate,
        "roles": list(results.keys()),
        "unsimulated_roles": unsimulated_roles,
        "fallen_roles": fallen_roles,
        "poses": poses,
        "log": log,
    }


def write_cache(cache: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))
