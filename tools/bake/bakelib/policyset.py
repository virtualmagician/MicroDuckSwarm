"""Loads assets/microduck/policies/ -- the manifest, and the one policy
this v1 baker actually drives (alpha_walking.onnx). See
docs/bake-format.md "What isn't simulated" for why only one of the nine
policies is loaded eagerly here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort

OBS_LEN = 61
ACTION_LEN = 14

# The policy this baker drives for ordinary locomotion + standing, for
# every duck, regardless of whether the show's locomotion track ever goes
# to zero. docs/bake-parts.md's manifest table marks both alpha_walking and
# alpha_stand "perpetual" kind but documents no rule for when robotd
# switches between them -- robot.setMode only names "walk"/"roller"
# (docs/robotd-api.md), never a walk/stand distinction. Using alpha_walking
# alone for the whole bake, including t's where the locomotion command is
# exactly zero, is this baker's own simplification, not a confirmed fact
# about robotd's real behavior. Flagged in docs/bake-format.md.
LOCOMOTION_POLICY_FILE = "alpha_walking.onnx"



# The five `do` skills a .duckshow can author, mapped to the policy that
# drives each (manifest.json's own `name` field, where it has one). roller
# mode and roller_crouch are deliberately absent: they need
# robot_groundcontact_rollers.xml, a structurally different machine this
# baker does not load. See docs/bake-format.md "Skills: four of five now
# driven".
#
# roulade is deliberately absent despite having a policy and a duration.
# Driven against the full ground-contact model it does execute -- the duck
# launches (trunk z 0.117 -> 0.188) and rotates a clean 180 deg -- but after
# its stated 1.0 s it is still inverted, and manifest.json marks it
# "chain": true without naming what it chains into. Handing an upside-down
# duck back to alpha_walking.onnx corrupts the whole remainder of the bake,
# which is worse than not simulating it, so it stays logged as unsimulated
# until the recovery half of the chain is known. Measured, not assumed.
SKILL_POLICY_FILES = {
    "kick_left": "ball_kick_left.onnx",
    "kick_right": "ball_kick_right.onnx",
    "sit_toggle": "alpha_sitstand.onnx",
    "ground_pick": "alpha_ground_pick.onnx",
}


@dataclass(frozen=True)
class SkillPolicy:
    """One skill's ONNX session plus the manifest facts needed to drive it."""
    skill: str
    file: str
    session: ort.InferenceSession
    kind: str                      # "episodic" | "scripted"
    duration_s: float | None       # episodic only
    action_scale: float            # manifest `action_scale`, 1.0 by omission
    command: dict                  # manifest `command` block, {} when absent
    ramp_s: float | None           # scripted only
    unwind_s: float | None         # scripted only


class PolicyManifestError(ValueError):
    pass



@dataclass(frozen=True)
class PolicySet:
    manifest: dict
    policies_dir: Path
    locomotion_session: ort.InferenceSession
    # skill name -> SkillPolicy, for every skill in SKILL_POLICY_FILES whose
    # .onnx is present. Loaded eagerly: nine sessions is a few hundred ms
    # once per bake, against a per-role physics loop measured in seconds.
    skills: dict[str, 'SkillPolicy']
    # filename -> sha256 hex digest, for every .onnx + the manifest itself --
    # the "policy versions" half of docs/viewer.md's cache-key framing.
    file_hashes: dict[str, str]
    combined_hash: str  # sha256 over the sorted "name:hash" lines above


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_policy_set(policies_dir: Path) -> PolicySet:
    manifest_path = policies_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"{manifest_path} not found -- assets/microduck/policies/ is not populated "
            f"(docs/bake-parts.md §2)."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("obs_len") != OBS_LEN or manifest.get("action_len") != ACTION_LEN:
        raise PolicyManifestError(
            f"{manifest_path} declares obs_len={manifest.get('obs_len')} "
            f"action_len={manifest.get('action_len')}; this baker is built against "
            f"{OBS_LEN} -> {ACTION_LEN} (docs/bake-parts.md §1b) and refuses to guess "
            f"at a different contract."
        )

    file_hashes: dict[str, str] = {}
    onnx_files = sorted(p.name for p in policies_dir.glob("*.onnx"))
    if not onnx_files:
        raise FileNotFoundError(f"no .onnx files found under {policies_dir}")
    for name in onnx_files:
        file_hashes[name] = _sha256_file(policies_dir / name)
    file_hashes["manifest.json"] = _sha256_file(manifest_path)

    combined = "\n".join(f"{name}:{file_hashes[name]}" for name in sorted(file_hashes))
    combined_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()

    loco_path = policies_dir / LOCOMOTION_POLICY_FILE
    if not loco_path.exists():
        raise FileNotFoundError(f"{loco_path} not found")
    session = ort.InferenceSession(str(loco_path), providers=["CPUExecutionProvider"])
    _check_session_shape(session, loco_path)

    by_file = {e["file"]: e for e in manifest.get("policies", []) if isinstance(e, dict) and "file" in e}
    skills: dict[str, SkillPolicy] = {}
    for skill, filename in SKILL_POLICY_FILES.items():
        path = policies_dir / filename
        if not path.exists():
            continue  # a partial assets/ directory degrades to "unsimulated", never crashes
        entry = by_file.get(filename, {})
        skill_session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        _check_session_shape(skill_session, path)
        skills[skill] = SkillPolicy(
            skill=skill,
            file=filename,
            session=skill_session,
            kind=str(entry.get("kind", "episodic")),
            duration_s=(float(entry["duration_s"]) if "duration_s" in entry else None),
            # Absent means 1.0. The manifest gives an explicit action_scale only
            # for the roller family (0.8), which reads as the exception being
            # called out rather than the rule -- same reading sim.py's
            # LOCOMOTION_ACTION_SCALE already documents for alpha_walking.
            action_scale=float(entry.get("action_scale", 1.0)),
            command=dict(entry.get("command", {})),
            ramp_s=(float(entry["ramp_s"]) if "ramp_s" in entry else None),
            unwind_s=(float(entry["unwind_s"]) if "unwind_s" in entry else None),
        )

    return PolicySet(
        manifest=manifest,
        policies_dir=policies_dir,
        locomotion_session=session,
        skills=skills,
        file_hashes=file_hashes,
        combined_hash=combined_hash,
    )


def _check_session_shape(session: ort.InferenceSession, path: Path) -> None:
    inputs = session.get_inputs()
    outputs = session.get_outputs()
    if len(inputs) != 1 or len(outputs) != 1:
        raise PolicyManifestError(f"{path} has {len(inputs)} inputs / {len(outputs)} outputs, expected 1/1")
    in_shape = inputs[0].shape
    out_shape = outputs[0].shape
    if list(in_shape)[-1] != OBS_LEN or list(out_shape)[-1] != ACTION_LEN:
        raise PolicyManifestError(f"{path}: obs/action shape {in_shape}->{out_shape} != [.,{OBS_LEN}]->[.,{ACTION_LEN}]")


def run_session(session: ort.InferenceSession, obs: np.ndarray) -> np.ndarray:
    """obs: (61,) -> (14,) raw action, for any of the nine policies (they all
    share the same contract; manifest.json declares obs_len/action_len once,
    not per policy)."""
    input_name = session.get_inputs()[0].name
    out = session.run(None, {input_name: obs.reshape(1, OBS_LEN).astype(np.float32)})
    return np.asarray(out[0], dtype=np.float32).reshape(ACTION_LEN)


def run_locomotion_policy(policies: PolicySet, obs: np.ndarray) -> np.ndarray:
    """obs: (61,) float32/float64 -> returns (14,) float32 raw action."""
    session = policies.locomotion_session
    input_name = session.get_inputs()[0].name
    out = session.run(None, {input_name: obs.reshape(1, OBS_LEN).astype(np.float32)})
    return np.asarray(out[0], dtype=np.float32).reshape(ACTION_LEN)
