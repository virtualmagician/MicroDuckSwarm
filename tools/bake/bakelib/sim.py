"""Per-role physics loop: steps one duck through its whole show and
returns the pose-cache arrays plus this role's bake-log entries.

Ducks never interact physically (docs/viewer.md: "they share a floor,
never each other") -- each role gets its own MjData against a single
shared, already-compiled MjModel (loading the ~26 MB of STL meshes is the
expensive part of a fresh load, ~0.3 s measured on this machine; MjData
construction is microseconds, so sharing the compiled model across roles
in one process is a real, safe speedup, not a fidelity shortcut).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import mujoco
import numpy as np

from . import bam_actuator, duckmodel, marks, observation, policyset

CONTROL_HZ = duckmodel.CONTROL_HZ
CONTROL_DECIMATION = duckmodel.CONTROL_DECIMATION

# docs/bake-parts.md §1b: "The observation normalizer is baked into the
# ONNX graph ... so a consumer does not reimplement normalization" -- but
# nothing in that research pass pins down the action *de*-normalization
# (whether the network's raw output is already a radians offset, or needs
# scaling first). policies/manifest.json gives an explicit action_scale
# for the two roller-family policies (0.8) but not for alpha_walking or
# alpha_stand, which this baker reads as "1.0 by omission" -- the
# manifest's own schema treats the field as optional per-policy, and the
# roller entries look like the exception being called out, not the rule.
# NOT independently confirmed from microduck_rl's training config. If the
# baked gait looks like it's barely moving from the STAND pose (actions
# too small) or thrashing wildly (actions too large), this is the first
# constant to revisit.
LOCOMOTION_ACTION_SCALE = 1.0

# editor/duckshow-viewer.js's own constants (duplicated here, not derived
# from any physics fact -- see marks.py's module docstring for why this
# split across languages exists at all). Reused verbatim so a baked
# walkPhase and a kinematic walkPhase mean the same animation-cycle
# distance, even though the *distance* each is computed from now differs
# (dead-reckoned commanded distance for the kinematic path; real
# physics-simulated distance for this bake -- exactly the kind of gap
# docs/viewer.md's "the payoff: the diff" wants visible).
PHASE_PER_METRE = (2.0 * math.pi) / 0.10
STOP_SPEED_EPS = 0.005

# Fall detection (this baker's own heuristic, not a number from any
# source): a trunk height under a third of nominal standing, or a
# roll/pitch tilt past ~60 degrees, is not a duck that recovers.
FALL_HEIGHT_FRACTION = 0.35
FALL_TILT_RAD = 1.0


def _quat_to_yaw_pitch_roll(quat_wxyz: np.ndarray) -> tuple[float, float, float]:
    """Extrinsic Z-Y-X (yaw, then pitch, then roll) Euler decomposition.
    This baker's own choice of convention for turning the trunk's physics
    orientation into the pose-cache's heading/bodyRoll/bodyPitch fields --
    the .duckshow format and docs/robotd-api.md define roll/pitch only as
    small deltas-from-upright (never a full 3D decomposition, since no real
    command needs one), so there is no upstream convention to match here.
    Standard robotics 3-2-1 sequence; see docs/bake-format.md.
    """
    w, x, y, z = quat_wxyz
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    sinp = 2.0 * (w * y - z * x)
    sinp = max(-1.0, min(1.0, sinp))
    pitch = math.asin(sinp)
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    return yaw, pitch, roll


@dataclass
class RoleBakeLogEntry:
    role: str
    t: float
    kind: str  # "skill_driven" | "skill_unsimulated" | "mode_unsimulated" | "fell"
    detail: str

    def to_json(self) -> dict:
        return {"role": self.role, "t": round(self.t, 3), "kind": self.kind, "detail": self.detail}


@dataclass
class RoleBakeResult:
    role: str
    frame_count: int
    x: np.ndarray
    y: np.ndarray
    heading: np.ndarray
    head_yaw: np.ndarray
    head_pitch: np.ndarray
    head_roll: np.ndarray
    neck_pitch: np.ndarray
    body_z: np.ndarray
    body_roll: np.ndarray
    body_pitch: np.ndarray
    mouth_open: np.ndarray
    walk_phase: np.ndarray
    # Per-frame angle of every actuated joint not already carried by a flat
    # field above -- the ten leg joints (docs/bake-format.md's optional
    # `poses[role].joints`). Without this the cache records no leg motion at
    # all and the renderer re-synthesises the legs procedurally from
    # walkPhase, so a "baked physics" preview shows the kinematic waddle.
    joints: dict[str, np.ndarray] = field(default_factory=dict)
    log: list[RoleBakeLogEntry] = field(default_factory=list)
    simulated: bool = True  # False for a role this baker could not drive at all (e.g. roller mode)


class _SkillDriver:
    """Decides, per control tick, which policy drives the duck and with what
    command block -- the base locomotion policy, or a skill's own.

    docs/bake-format.md "Skills: four of five now driven" states the contract
    and names the three inferences this implements. Nothing here blends or
    eases across a hand-off: physics state (prev_action included) carries
    across untouched, which is what a real robot does and what makes an
    abrupt-looking boundary a real report rather than a hidden one.
    """

    def __init__(self, policies):
        self.policies = policies
        self.active = None      # SkillPolicy currently driving, or None
        self.started_t = 0.0    # show time the current window began
        self.seated = False     # sit_toggle is a STATE, not an impulse
        self.unwinding = False  # seated -> standing, running out unwind_s
        self.unwind_timed_out = False  # a stand-up that never reached upright

    def trigger(self, skill: str, t: float):
        """A `do` event fired. Returns the SkillPolicy that will drive it, or
        None if this baker has no policy for it (roller-family only)."""
        sp = self.policies.skills.get(skill)
        if sp is None:
            return None
        if skill == "sit_toggle":
            # Inference 3: sitting holds until toggled back. A toggle while
            # seated begins the unwind from wherever the ramp had reached --
            # which is exactly shows/octet's `reed`, two toggles 2.0 s apart
            # against a 2.0 s ramp.
            if self.seated:
                self.seated, self.unwinding = False, True
            else:
                self.seated, self.unwinding = True, False
        self.active, self.started_t = sp, t
        return sp

    # A stand-up that hands back mid-transition topples the duck: measured on
    # shows/octet's `reed`, alpha_walking.onnx took over at exactly unwind_s
    # with the trunk still at 0.068 m of its 0.120 m nominal and put it on the
    # floor within 0.3 s. manifest.json's unwind_s is a nominal duration, not
    # a guarantee about the physics, so the hand-off waits for the duck to
    # actually be standing again -- with a hard cap so a stand-up that never
    # completes cannot hold the policy forever.
    UPRIGHT_HEIGHT_FRACTION = 0.90
    UNWIND_TIMEOUT_FACTOR = 3.0

    def _expired(self, t: float, height_ratio: float | None = None) -> bool:
        sp = self.active
        if sp is None:
            return True
        elapsed = t - self.started_t
        if sp.kind == "scripted":
            if self.seated:
                return False  # hold the seated posture indefinitely
            unwind = sp.unwind_s or 0.0
            if elapsed < unwind:
                return False
            if height_ratio is None or height_ratio >= self.UPRIGHT_HEIGHT_FRACTION:
                return True
            if elapsed >= unwind * self.UNWIND_TIMEOUT_FACTOR:
                self.unwind_timed_out = True
                return True
            return False  # still getting up; do not hand back yet
        return elapsed >= (sp.duration_s or 0.0)

    def current(self, t: float, height_ratio: float | None = None):
        """(session, action_scale, command_override or None) for this tick.
        `height_ratio` is trunk height over nominal standing height, used only
        to decide when a stand-up has actually finished."""
        if self.active is not None and self._expired(t, height_ratio):
            self.active, self.unwinding = None, False
        if self.active is None:
            return self.policies.locomotion_session, LOCOMOTION_ACTION_SCALE, None
        sp = self.active
        return sp.session, sp.action_scale, self._command_override(sp, t)

    def _command_override(self, sp, t: float):
        """The FULL 13-slot command block this skill wants, or None to leave
        the show's own command in place.

        Everything outside the skill's own encoded slots is zero, including
        head_pose and body_pose. That is a correction to this baker's first
        reading, which passed the show's authored head/body commands through
        on the grounds that the observation needs all 13 values anyway.
        Measured consequence: shows/octet's `reed` sat down at a pitch of
        -1.09 rad (62 deg reclined) instead of the -0.03 the same policy
        produces from a zeroed command, and alpha_sitstand.onnx cannot stand
        back up from that posture -- the duck held the recline through its
        whole unwind and then toppled the moment alpha_walking.onnx took
        over. A skill policy's command contract is what manifest.json states
        for it; feeding it anything else is handing it values it was never
        trained on."""
        enc = sp.command.get("encoding")
        if enc == "posture_flag":
            flag = float(sp.command.get("sit", 1.0)) if self.seated else float(sp.command.get("stand", 0.0))
            cmd = np.zeros(observation.COMMAND_LEN, dtype=np.float32)
            cmd[0] = flag
            return cmd
        if enc == "phase":
            # Inference 1: two slots + a period + an end phase is a cyclic
            # encoding. phase sweeps 0 -> end_phase across the clip in real
            # time, which is exactly what duration_s == period_s * end_phase
            # says (2.8 == 4.0 * 0.7 here; 3.5 == 5.0 * 0.7 for roller_crouch).
            period = float(sp.command.get("period_s", 1.0)) or 1.0
            end_phase = float(sp.command.get("end_phase", 1.0))
            phase = min((t - self.started_t) / period, end_phase)
            ang = 2.0 * math.pi * phase
            cmd = np.zeros(observation.COMMAND_LEN, dtype=np.float32)
            cmd[0], cmd[1] = math.cos(ang), math.sin(ang)
            return cmd
        # No manifest command at all: an entirely zero command block, which
        # is the `idle` value alpha_sitstand's own entry names for that state.
        return np.zeros(observation.COMMAND_LEN, dtype=np.float32)


_SKILL_POLICY_HINT = {
    "kick_left": "ball_kick_left.onnx (episodic; needs the ball.xml prop, not loaded by this baker)",
    "kick_right": "ball_kick_right.onnx (episodic; needs the ball.xml prop, not loaded by this baker)",
    "sit_toggle": "alpha_sitstand.onnx (scripted, posture_flag command on twist.vx per manifest.json)",
    "roulade": "roulade.onnx (episodic, chained)",
    "ground_pick": "alpha_ground_pick.onnx (episodic, phase-encoded command per manifest.json)",
}


def simulate_role(
    duck: duckmodel.DuckModel,
    policies: policyset.PolicySet,
    show_doc: dict,
    sampler,  # duckshow.Sampler
    role: str,
    role_index: int,
    role_total: int,
    duration: float,
    progress=None,  # optional callable(role, k, frame_count)
) -> RoleBakeResult:
    frame_count = max(1, round(duration * CONTROL_HZ))

    mark = marks.resolve_mark(show_doc, role, role_index, role_total)

    tracks = sampler.tracks
    mode_events = [e.mode for e in tracks.events if e.mode is not None]
    # Scoped per-window as of 2026-09-04 (docs/bake-format.md "What isn't
    # simulated"). This used to bail out for the WHOLE role the moment a
    # non-walk mode appeared anywhere in it, which is why
    # shows/showcase/showcase.duckshow.json -- the show written to demonstrate
    # every policy -- baked to 41.75 s of a completely motionless duck: its
    # single roller event at t=31.0 discarded the 74% of the show that is
    # plain walk mode and perfectly drivable. The frame loop below now freezes
    # only across the roller stretch itself.
    #
    # The all-or-nothing path is kept for the one case it is still right for:
    # a role that is in a non-walk mode from the very start has no walk-mode
    # stretch to simulate, and a per-frame freeze would just be a slower way
    # of producing the same static array.
    if mode_events and all(m != "walk" for m in mode_events) and (sampler.mode_at(0.0) or "walk") != "walk":
        # docs/duckshow-format.md: "the only two values real robotd
        # accepts" are "walk"/"roller". This baker only loads the legged
        # (walk-mode) MJCF and alpha_walking.onnx -- a roller-mode show
        # would need robot_allcollisions_rollers.xml and roller.onnx,
        # neither wired up here. Rather than silently simulate the wrong
        # physical machine for that stretch of the show, log it plainly
        # and still emit a full-length array (idle standing throughout)
        # so the cache stays well-formed.
        log = [RoleBakeLogEntry(
            role=role, t=0.0, kind="mode_unsimulated",
            detail="show uses a 'roller' mode event; this baker only drives the legged "
                   "(walk-mode) model. Role held static at its mark instead of simulated.",
        )]
        return _static_role_result(duck, role, frame_count, mark, sampler, log)

    data = mujoco.MjData(duck.model)
    # Build this role's BAM controller before reset_to_mark's mj_forward --
    # mirrors bam.mujoco.Simulator.reset()'s own order (construct the
    # controller against a freshly-zeroed MjData, *then* set qpos/qvel and
    # call mj_forward), and also resets the shared model's
    # dof_frictionloss/dof_damping back to zero first (see
    # bam_actuator.new_controller's docstring for why that matters when one
    # compiled MjModel is reused across every role).
    controller = bam_actuator.new_controller(duck.bam_rig, duck.model, data)
    duckmodel.reset_to_mark(duck, data, mark.x, mark.y, mark.heading)

    x = np.zeros(frame_count)
    y = np.zeros(frame_count)
    heading = np.zeros(frame_count)
    head_yaw = np.zeros(frame_count)
    head_pitch = np.zeros(frame_count)
    head_roll = np.zeros(frame_count)
    neck_pitch = np.zeros(frame_count)
    body_z = np.zeros(frame_count)
    body_roll = np.zeros(frame_count)
    body_pitch = np.zeros(frame_count)
    mouth_open = np.zeros(frame_count)
    joint_angles = {name: np.zeros(frame_count) for name in duckmodel.LEG_JOINT_NAMES}

    log: list[RoleBakeLogEntry] = []
    fallen_logged = False
    prev_action = np.zeros(policyset.ACTION_LEN, dtype=np.float32)
    prev_event_t = -1e-9
    skill_driver = _SkillDriver(policies)
    frozen_mode = None   # the non-walk mode currently being held, or None while simulating
    any_frozen = False   # did any window get held? -> role still reported in unsimulated_roles

    hi = {name: 7 + idx for name, idx in duckmodel.HEAD_JOINT_INDICES.items()}
    li = {name: 7 + idx for name, idx in duckmodel.LEG_JOINT_INDICES.items()}

    for k in range(frame_count):
        t = k / CONTROL_HZ
        sample = sampler.at(t)

        for e in sampler.events_between(prev_event_t, t):
            if e.do is not None:
                driven = skill_driver.trigger(e.do, t)
                if driven is None:
                    hint = _SKILL_POLICY_HINT.get(e.do, "no policy entry found in manifest.json")
                    log.append(RoleBakeLogEntry(
                        role=role, t=e.t, kind="skill_unsimulated", detail=(
                            f"'{e.do}' event not driven by physics -- the base locomotion "
                            f"policy keeps running through it unchanged. Would need {hint}. "
                            f"See docs/bake-format.md \"What isn't simulated\"."
                        ),
                    ))
                else:
                    log.append(RoleBakeLogEntry(
                        role=role, t=e.t, kind="skill_driven", detail=(
                            f"'{e.do}' driven by {driven.file} ({driven.kind}) against the "
                            f"walk-mode model. See docs/bake-format.md \"Skills: four of "
                            f"five now driven\" for the plant approximation and the three "
                            f"command-encoding inferences this relies on."
                        ),
                    ))
        prev_event_t = t

        quat = np.array(data.sensordata[
            duck.imu_sensor_adr["orientation"][0]:duck.imu_sensor_adr["orientation"][0] + 4
        ])
        yaw, pitch, roll = _quat_to_yaw_pitch_roll(quat)

        x[k] = data.qpos[0]
        y[k] = data.qpos[1]
        heading[k] = yaw
        body_z[k] = data.qpos[2] - duck.nominal_height
        body_roll[k] = roll
        body_pitch[k] = pitch
        # Straight off live MuJoCo state, exactly like the head joints below
        # -- these are what make a baked preview show baked legs instead of
        # the procedural walk cycle.
        for name, adr in li.items():
            joint_angles[name][k] = data.qpos[adr]
        neck_pitch[k] = data.qpos[hi["neck_pitch"]]
        head_pitch[k] = data.qpos[hi["head_pitch"]]
        head_yaw[k] = data.qpos[hi["head_yaw"]]
        head_roll[k] = data.qpos[hi["head_roll"]]
        # mouthOpen is never touched by physics -- the trained action space
        # is 14 joints and excludes the mouth (docs/bake-parts.md §3.1,
        # docs/viewer.md "What it cannot do"). Pass the show's own mouth
        # track straight through, exactly like the kinematic path.
        mouth_open[k] = sample.mouth.open if sample.mouth is not None else 0.0

        # A duck that is deliberately sitting is low and pitched, which is
        # exactly what the fall heuristic looks for -- baking the showcase
        # reported `reed` as having fallen 0.96 s after its sit_toggle, at a
        # trunk height of 0.041 m against a 0.042 m threshold. That is a duck
        # sitting down, correctly. Fall detection is a heuristic about
        # UNINTENDED collapse, so it does not apply while a scripted posture
        # policy is deliberately putting the duck on the floor.
        posture_held = skill_driver.seated or (
            skill_driver.active is not None and skill_driver.active.kind == "scripted"
        )
        if not fallen_logged and not posture_held:
            fell = (data.qpos[2] < duck.nominal_height * FALL_HEIGHT_FRACTION) or \
                   (abs(roll) > FALL_TILT_RAD) or (abs(pitch) > FALL_TILT_RAD)
            if fell:
                fallen_logged = True
                log.append(RoleBakeLogEntry(
                    role=role, t=t, kind="fell", detail=(
                        f"trunk height {data.qpos[2]:.3f} m (nominal {duck.nominal_height:.3f} m), "
                        f"roll {roll:.2f} rad, pitch {pitch:.2f} rad."
                    ),
                ))

        if progress is not None:
            progress(role, k, frame_count)

        if k == frame_count - 1:
            break

        # Roller (or any non-walk) stretch: this baker loads only the legged
        # walk model, so driving alpha_walking.onnx here would simulate the
        # wrong physical machine. Freeze instead -- skip the policy and the
        # physics steps, so the pose recorded above simply holds -- and log
        # the window once, at its own start time. Walk stretches on either
        # side are simulated normally; see docs/bake-format.md "What isn't
        # simulated". Resuming afterwards is an honest discontinuity: the
        # frozen legged state is not a valid roller state, and nothing here
        # pretends it is.
        mode_now = sampler.mode_at(t) or "walk"
        if mode_now != "walk":
            if frozen_mode != mode_now:
                frozen_mode = mode_now
                any_frozen = True
                log.append(RoleBakeLogEntry(
                    role=role, t=t, kind="mode_unsimulated", detail=(
                        f"'{mode_now}' mode from t={t:.2f}s: this baker drives only the legged "
                        f"(walk-mode) model, so physics is frozen and the pose held for this "
                        f"window. The role's walk-mode stretches are simulated normally. "
                        f"See docs/bake-format.md \"What isn't simulated\"."
                    ),
                ))
            continue
        frozen_mode = None

        # Which policy owns this tick -- alpha_walking, or a skill's own.
        session, action_scale, cmd_override = skill_driver.current(
            t, height_ratio=data.qpos[2] / duck.nominal_height)
        obs = observation.build_observation(duck, data, prev_action, sample.locomotion, sample.head, sample.pose)
        if cmd_override is not None:
            # The whole 13-slot command block, not just twist -- see
            # _SkillDriver._command_override for the measurement behind that.
            obs[observation.PROPRIO_LEN:] = cmd_override
        action = policyset.run_session(session, obs)
        q_target = duck.stand_joint_qpos + action.astype(np.float64) * action_scale
        # The policy's target *angle* updates once per control tick (50 Hz)
        # -- same as before BAM. What changes is what turns that angle into
        # torque: controller.update() now recomputes BAM's firmware
        # voltage-control law + load-dependent friction every *physics*
        # substep (200 Hz), not just once per control tick, exactly mirroring
        # bam.mujoco.Simulator.step()'s own one-update()-per-mj_step() loop
        # (docs/bake-parts.md §3.5 step 4) -- a real servo's internal PID
        # loop runs far faster than the 50 Hz policy that sets its target.
        for name, val in zip(duckmodel.JOINT_NAMES, q_target):
            controller.set_q_target(name, val)
        prev_action = action

        for _ in range(CONTROL_DECIMATION):
            controller.update()
            mujoco.mj_step(duck.model, data)

    walk_phase = _walk_phase_from_xy(x, y)

    return RoleBakeResult(
        role=role, frame_count=frame_count,
        x=x, y=y, heading=heading,
        head_yaw=head_yaw, head_pitch=head_pitch, head_roll=head_roll, neck_pitch=neck_pitch,
        body_z=body_z, body_roll=body_roll, body_pitch=body_pitch,
        mouth_open=mouth_open, walk_phase=walk_phase, joints=joint_angles,
        log=log, simulated=not any_frozen,
    )


def _walk_phase_from_xy(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Cumulative stride phase from the *actual* physics trajectory, same
    formula as editor/duckshow-viewer.js's precomputeRolePath (distance
    since the last sample, scaled by PHASE_PER_METRE, held while the duck
    is below STOP_SPEED_EPS). This bake's control tick spacing is exactly
    1/50 s = 0.02 s, i.e. the same dt the kinematic path's DEFAULT_DT uses,
    so per-frame distances are directly comparable between the two paths.
    """
    n = len(x)
    phase = np.zeros(n)
    acc = 0.0
    dt = 1.0 / CONTROL_HZ
    for i in range(1, n):
        dist = math.hypot(x[i] - x[i - 1], y[i] - y[i - 1])
        speed = dist / dt if dt > 0 else 0.0
        if speed > STOP_SPEED_EPS:
            acc += dist * PHASE_PER_METRE
        phase[i] = acc
    return phase


def _static_role_result(duck, role, frame_count, mark, sampler, log) -> RoleBakeResult:
    """A role this baker declines to simulate at all (roller mode). Holds
    at its mark, standing, for the whole show -- mouthOpen still passes
    through (it never depended on physics anyway).
    """
    ones = np.ones(frame_count)
    mouth = np.zeros(frame_count)
    for k in range(frame_count):
        s = sampler.at(k / CONTROL_HZ)
        mouth[k] = s.mouth.open if s.mouth is not None else 0.0
    return RoleBakeResult(
        role=role, frame_count=frame_count,
        x=mark.x * ones, y=mark.y * ones, heading=mark.heading * ones,
        head_yaw=np.zeros(frame_count), head_pitch=np.zeros(frame_count),
        head_roll=np.zeros(frame_count), neck_pitch=np.zeros(frame_count),
        body_z=np.zeros(frame_count), body_roll=np.zeros(frame_count), body_pitch=np.zeros(frame_count),
        mouth_open=mouth, walk_phase=np.zeros(frame_count),
        # Hold every leg joint at the MJCF STAND keyframe for the whole show.
        # Emitting the block (rather than omitting it) is deliberate: without
        # it the renderer falls back to synthesising legs from walkPhase, and
        # a role held static would still get a procedural rest pose derived
        # from a different source than the one it is actually standing in.
        # Standing still should mean standing in STAND, explicitly.
        joints={
            name: duck.stand_joint_qpos[idx] * ones
            for name, idx in duckmodel.LEG_JOINT_INDICES.items()
        },
        log=log, simulated=False,
    )
