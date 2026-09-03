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


class PolicyManifestError(ValueError):
    pass


@dataclass(frozen=True)
class PolicySet:
    manifest: dict
    policies_dir: Path
    locomotion_session: ort.InferenceSession
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

    return PolicySet(
        manifest=manifest,
        policies_dir=policies_dir,
        locomotion_session=session,
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


def run_locomotion_policy(policies: PolicySet, obs: np.ndarray) -> np.ndarray:
    """obs: (61,) float32/float64 -> returns (14,) float32 raw action."""
    session = policies.locomotion_session
    input_name = session.get_inputs()[0].name
    out = session.run(None, {input_name: obs.reshape(1, OBS_LEN).astype(np.float32)})
    return np.asarray(out[0], dtype=np.float32).reshape(ACTION_LEN)
