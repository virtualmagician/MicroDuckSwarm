"""Ports `better-actuator-models` (PyPI `better-actuator-models`, import
name `bam`, pinned 1.0.2 -- see requirements.txt) into the baker: swaps
MuJoCo's stock `<position>` PD actuator for the same XL330 voltage-control-
law torque/friction plant `alpha_walking.onnx` was actually trained
against. `docs/bake-format.md`'s "Known bug" section (now "Actuator model:
BAM ported" history) measured this substitution as the root cause of a
sharp low-speed walk-initiation bifurcation: below a sustained vx of about
0.21 m/s, the stock position actuator's soft spring (`kp=0.55`) cannot
deliver torque fast enough at small position errors to break the standing
fixed point, so the duck never steps. `docs/bake-parts.md` §3.5 scoped this
exact port in detail; this module follows its five steps.

Integration path: `bam.mujoco.MujocoController` -- the package's own
plain-NumPy CPU controller (`bam/mujoco.py`), not `bam.mjlab.BamActuator`
(the PyTorch + MuJoCo-Warp GPU path `microduck_rl`'s training code
actually drives). §3.5 step 4 already names `MujocoController` as
"no reimplementation ... this class already is the CPU torque/friction
loop" -- it performs the identical per-step computation (firmware voltage
control law -> DC-motor back-EMF torque -> load-dependent Coulomb/Stribeck
friction written into `dof_frictionloss`/`dof_damping`) against a bare
`MjModel`/`MjData`. `bam.mjlab` needs `mujoco_warp` and `torch`, neither
installed nor wanted in a single-process CPU bake.

De-randomization for a BAKE, not training. `microduck_rl`'s `develop`
branch (`src/mjlab_microduck/robot/microduck_constants.py`,
`_BAM_ACTUATOR_KWARGS`) domain-randomizes three things per training
episode: battery voltage, voltage sag under load, and command delay. A
bake must be deterministic -- the same show baked twice must produce the
same cache (`docs/bake-format.md` "Cache key") -- so none of the three is
ever resampled here; each is fixed at a single nominal value, chosen and
justified below. Also note: the version of `better-actuator-models`
`microduck_rl` actually pins is not this PyPI release -- its
`pyproject.toml` sources it from `github.com/Rhoban/bam`, branch
`mjlab_frictionloss` ("Switch to `main` (or a tag) once the mjlab-facing
API lands there"). That branch's `BamActuatorCfg` carries fields this
installed 1.0.2 does not (`vin_drop_gain_range`, `delay_min_lag`,
`delay_max_lag` are documented in this package's own `bam/mjlab.py`
docstring but are not actual dataclass fields on 1.0.2 -- confirmed by
direct read). Irrelevant to the CPU path used here either way: this
module never imports `bam.mjlab` at all.
"""

from __future__ import annotations

from dataclasses import dataclass

import mujoco
import numpy as np

from bam.model import Model, load_model
from bam.mujoco import MujocoController

MOTOR_NAME = "xl330"
MODEL_VARIANT = "m6"  # Stribeck load-dependent directional quadratic -- the
# variant microduck_constants.py's _BAM_ACTUATOR_KWARGS names (model="m6").

# Firmware proportional gain. NOT randomized in training (a single fixed
# value in _BAM_ACTUATOR_KWARGS, unlike vin/vin_drop/delay below) -- used
# as-is, no de-randomization decision needed.
KP_FW = 200.0

# --- De-randomization decisions (bake-only; training randomizes all three) ---
#
# 1. Battery voltage. Training samples a fresh per-episode vin uniformly
#    from VIN_RANGE_TRAIN at env reset and holds it for that episode
#    (_BAM_ACTUATOR_KWARGS: vin_range=(6.5, 8.2)). A bake has no notion of
#    "episode" and must be reproducible byte-for-byte across runs, so this
#    baker fixes vin at a single nominal value for every joint, every role,
#    the whole show: the midpoint of the trained range. Neither
#    microduck_constants.py nor friction_dr_bam.py states an explicit
#    non-randomized default (the class default on XL330Actuator itself,
#    vin=7.5, is the untrained fallback the real repo overrides via
#    vin_range) -- absent a stated single number, the midpoint of the
#    actual trained sampling range is the least-arbitrary "nominal" choice.
VIN_RANGE_TRAIN = (6.5, 8.2)
VIN_NOMINAL = (VIN_RANGE_TRAIN[0] + VIN_RANGE_TRAIN[1]) / 2.0  # 7.35 V

#    2. Voltage sag under load. Training additionally randomizes a
#    per-episode sag gain (vin_drop_gain_range=(0.0, 0.2)) modelling
#    battery + wire resistance dropping the effective voltage as current
#    draw rises, floored at vin_min=6.0. bam.mujoco.MujocoController models
#    this with its own, differently-shaped mechanism
#    (vin_drop_resistance: an Ohms value, V_drop = R * I) rather than
#    mjlab's gain-on-summed-torque formula -- the two are not numerically
#    interchangeable, so there is no single "equivalent" resistance to
#    carry the trained gain range over verbatim. This baker instead
#    de-randomizes sag to its distribution's own zero endpoint --
#    vin_drop_resistance=None, i.e. no sag, constant VIN_NOMINAL every
#    step -- rather than guess an interpolated value across two different
#    formulas. Zero is a real member of the trained range (not an
#    extrapolation), and "no sag" is the natural reading of "nominal,
#    non-randomized" for a term that is a randomized *perturbation* on top
#    of a baseline, not itself a baseline quantity.
VIN_DROP_GAIN_RANGE_TRAIN = (0.0, 0.2)  # documented, not applied -- see above
VIN_MIN_TRAIN = 6.0  # training's floor on sagged voltage; moot once sag is
# disabled (vin never moves from VIN_NOMINAL), kept here only as a record
# of what training used.

#    3. Command delay. Training randomizes a per-episode integer lag
#    (delay_min_lag=3, delay_max_lag=6 control-agnostic simulation steps)
#    modelling policy-to-motor latency. This is not modeled at all here --
#    honestly, not as a "nominal value" choice: bam.mujoco.MujocoController
#    (the CPU integration path this baker uses) has no delay parameter of
#    any kind; delay only exists on the mjlab/torch GPU actuator path this
#    baker deliberately does not use (see module docstring), and even
#    there only on the newer git-branch version microduck_rl pins, not on
#    this installed 1.0.2. This bake therefore runs at zero command delay
#    -- a real, named simplification, not a "nominal" pick from the
#    trained range (whose minimum was 3, not 0) -- and it is not a new gap
#    introduced by this port: the pre-BAM baker modeled zero delay too.
DELAY_RANGE_TRAIN_STEPS = (3, 6)  # documented, not modeled -- see above


def load_bam_model() -> Model:
    """Load the XL330 "m6" BAM friction model (bundled in the `bam` package
    itself, `bam/params/xl330/m6.json` -- no fetch, Apache-2.0) and pin it
    to this bake's nominal, non-randomized firmware gain and voltage.

    Re-verified 2026-09-04, directly against the installed package: without
    the two assignments below, `XL330Actuator.__init__`
    (`bam/dynamixel/actuator.py`) defaults to `kp=400, vin=7.5` -- the
    generic Dynamixel-XL330 defaults, not MicroDuck's trained values. The
    `model.actuator.kp = ...; model.actuator.vin = ...` pattern used here is
    the same one the `bam` package's own reference caller
    (`bam.mujoco.load_config`) uses to apply a non-default kp/vin, so this is
    the sanctioned way to override them, not a workaround. Confirmed applied
    (not silently defaulted) two ways: (1) direct read, these two lines run
    unconditionally every time this function is called; (2) numerically --
    the "Why BAM didn't move the threshold" small-signal-stiffness
    calculation in docs/bake-format.md (`kt*vin*kp_fw*error_gain/R ≈ 0.55
    Nm/rad`) only reproduces the stock actuator's `kp=0.55` using
    `kp_fw=200, vin=7.35` (the values applied here); the package's own
    defaults would give roughly double that stiffness. Also checked the
    counterfactual directly (a diagnostic sweep with the override
    disabled): even at the package's default `kp=400/vin=7.5`, commanded
    speeds of 0.10/0.15/0.20 m/s still converge to the same few-millimetre
    static correction (0.0062/0.0083/0.0101 m over a 3 s hold, against
    0.0055/0.0080/0.0102 m with the correct override) -- so even in the
    hypothetical world where this override was missing, it would not have
    explained the reported low-speed bug either. See docs/bake-format.md
    "BAM parameters: confirmed applied, not the bug" for the full writeup.
    """
    model = load_model(motor_name=MOTOR_NAME, model=MODEL_VARIANT)
    model.actuator.kp = KP_FW
    model.actuator.vin = VIN_NOMINAL
    return model


def edit_spec_to_bam(spec: "mujoco.MjSpec", joint_names: tuple[str, ...], bam_model: Model) -> float:
    """Convert the named joints' `<position>` actuators to `<motor>` (direct
    torque) mode on `spec`, in place, before it is compiled -- mirrors
    `bam.mjlab.BamActuator.edit_spec()`'s non-mjlab-specific steps exactly
    (`bam/mjlab.py` lines ~265-305, read directly): `set_to_motor()`
    (MuJoCo's own `MjSpec` method, not BAM-authored), a voltage-derived
    `forcerange` ceiling, `gear=[1,0,0,0,0,0]` (so `ctrl` becomes applied
    torque directly), and zeroing each joint's `armature`/`damping`/
    `frictionloss` to the values BAM itself supplies every step instead
    (`docs/bake-parts.md` §3.5's own description of this same edit).

    Returns `force_limit` (the `forcerange` ceiling, Nm) for callers that
    want to record it (e.g. in bake-log or debug output).
    """
    act = bam_model.actuator
    kt = bam_model.kt.value
    R = bam_model.R.value
    armature = act.get_extra_inertia()
    # Fixed vin (no per-env sampling here -- see VIN_NOMINAL above), so the
    # forcerange ceiling is just the torque at full duty cycle at that one
    # voltage, not a safety margin over a range (mjlab's edit_spec uses
    # max(vin_range) for exactly that safety-margin reason, which does not
    # apply once vin is a single fixed constant).
    force_limit = act.vin * kt / R

    target_set = set(joint_names)
    for mjact in spec.actuators:
        tgt = mjact.target
        tgt_name = tgt.name if hasattr(tgt, "name") else (str(tgt) if tgt else None)
        if tgt_name in target_set:
            mjact.set_to_motor()
            mjact.forcelimited = True
            mjact.forcerange = (-force_limit, force_limit)
            mjact.gear = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    for joint in spec.joints:
        if joint.name in target_set:
            joint.armature = float(armature)
            joint.damping = 0.0
            joint.frictionloss = 0.0

    return force_limit


@dataclass(frozen=True)
class BamRig:
    """Everything a role's simulation loop needs to drive BAM: the shared,
    nominal-configured friction model and the compiled model's dof
    addresses for the 14 actuated joints (used to reset per-role friction
    state -- see `new_controller`).
    """

    model: Model
    joint_names: tuple[str, ...]
    dof_indexes: np.ndarray  # (14,) into MjModel.dof_frictionloss / dof_damping
    force_limit: float


def build_rig(compiled_model: "mujoco.MjModel", joint_names: tuple[str, ...], bam_model: Model, force_limit: float) -> BamRig:
    dof_indexes = np.array([
        compiled_model.jnt_dofadr[mujoco.mj_name2id(compiled_model, mujoco.mjtObj.mjOBJ_JOINT, name)]
        for name in joint_names
    ])
    return BamRig(model=bam_model, joint_names=joint_names, dof_indexes=dof_indexes, force_limit=force_limit)


def new_controller(rig: BamRig, mujoco_model: "mujoco.MjModel", mujoco_data: "mujoco.MjData") -> MujocoController:
    """Build a fresh `MujocoController` for one role's simulation.

    `mujoco_model` is shared, read-only-*by-convention*, across every
    role's own `MjData` (`docs/bake-format.md` "Physics model and
    constants": loading the ~26 MB of meshes is the expensive part of a
    fresh load, so one compiled `MjModel` is reused). BAM breaks the
    "read-only" half of that convention in one specific way:
    `MujocoController.update()` writes fresh `dof_frictionloss`/
    `dof_damping` onto `mujoco_model` (not `mujoco_data`) every physics
    step, by design (`bam/mujoco.py` -- MuJoCo's own solver then applies
    that friction natively). Left alone, whatever a role's *last* physics
    step wrote there would leak into the *next* role's very first
    `mj_forward`/`mj_step` calls, before that role's own first
    `controller.update()` overwrites it -- a real, if narrow,
    cross-role dependency that the pre-BAM baker never had (the stock
    position actuator's `kp`/`kv`/`forcerange` are static XML defaults,
    never mutated at runtime). Explicitly zeroing these two arrays back to
    the post-`edit_spec` value here, before constructing this role's
    controller, removes that dependency -- every role starts from the same
    known state regardless of simulation order, keeping the "shared model
    is a safe speedup, not a fidelity shortcut" claim true with BAM in the
    loop too.
    """
    mujoco_model.dof_frictionloss[rig.dof_indexes] = 0.0
    mujoco_model.dof_damping[rig.dof_indexes] = 0.0
    return MujocoController(
        rig.model, list(rig.joint_names), mujoco_model, mujoco_data,
        vin_drop_resistance=None,  # de-randomized: no sag -- see VIN_DROP_GAIN_RANGE_TRAIN above
        vin_min=None,
    )
