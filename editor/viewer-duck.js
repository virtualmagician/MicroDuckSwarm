// viewer-duck.js — the duck, built entirely from viewer-gl.js primitives
// (docs/viewer.md "The duck — model the real robot"). No dependencies, no
// CDN, no build step, and never a Pollen mesh: every part below is one of
// viewer-gl.js's hand-written primitive generators, sized and coloured to
// read as the real MicroDuck — a BDX-style walking robot, not a bathtub
// duck: a rounded-rectangular camera head with one big lens, a yellow
// wraparound bill, a short dark articulated neck, a small silver-and-dark
// servo torso, and long dark mechanical legs carrying most of the height.
//
// This module owns:
//   - real-world proportions (DUCK_HEIGHT_M / DUCK_WIDTH_M) and the part
//     hierarchy built from them, driven by a pose object
//     {x, y, heading, headYaw, headPitch, headRoll, neckPitch, bodyZ,
//      bodyRoll, bodyPitch, mouthOpen, walkPhase} — the same shape
//     viewer-gl.js's StageRenderer.draw(poses) receives per duck.
//   - the walk cycle: alternating legs, a slight body bob/rock, the head
//     counter-rotating to stay level, settling to a neutral stand when
//     walkPhase holds (updateWalkState tracks that across draw() calls).
//   - GL mesh building (buildDuckAssets/disposeDuckAssets) and drawing
//     (drawDuck) against viewer-gl.js's "lit" shader program.
//
// computeDuckPose() is pure (no GL, no canvas) so the hierarchy maths is
// importable and reasoned about on its own; only buildDuckAssets/drawDuck/
// disposeDuckAssets touch a GL context, and only when called — nothing at
// module scope does.

import {
  roundedBoxMesh, cylinderMesh, discMesh, extrudedQuadMesh,
  uploadMesh, bindMeshAttribs, drawMesh,
  mat4Chain, mat4FromTranslation, mat4Translate, mat4RotateX,
  mat4FromXRotation, mat4FromYRotation, mat4FromZRotation,
  mat3FromMat4Rigid,
  clamp01,
} from './viewer-gl.js';

// ---------------------------------------------------------------------------
// Real-world proportions (docs/viewer.md "The duck"): 25 cm tall, 14 cm
// wide, so blocking distances read honestly against the floor grid. The
// legs carry most of the height — the body sitting on top of them is
// deliberately small.
// ---------------------------------------------------------------------------

export const DUCK_HEIGHT_M = 0.25;
export const DUCK_WIDTH_M = 0.14;

// Hip height at rest (== total leg length), the height StageRenderer
// places pose.bodyZ=0 at. Legs are built to exactly this length so the
// feet touch the floor (y=0) in the neutral stand.
export const STAND_HEIGHT_M = 0.135;

// ---------------------------------------------------------------------------
// Part dimensions (metres), tuned to the proportions above.
// ---------------------------------------------------------------------------

// Torso: a small light-grey/silver servo block, not a soft body mass —
// deliberately smaller than the head so the legs read as the dominant
// structure (docs/viewer.md "small relative to the legs").
const BODY_HX = 0.030, BODY_HY = 0.024, BODY_HZ = 0.028, BODY_RADIUS = 0.009;
// A darker mechanical detail plate on the torso's lower front (PCB/servo
// housing look) so the body reads as "machined parts", not a plain block.
const BODY_DETAIL_HX = 0.017, BODY_DETAIL_HY = 0.009, BODY_DETAIL_HZ = 0.005, BODY_DETAIL_RADIUS = 0.003;
// Small rounded hip panels, mounted each side of the torso — these (not
// the whole body) carry the role tint, per the colour split.
const HIP_PANEL_HX = 0.0095, HIP_PANEL_HY = 0.015, HIP_PANEL_HZ = 0.017, HIP_PANEL_RADIUS = 0.006;

// Neck: a short dark articulated STACK of small blocks (mechanism, never
// a smooth organic neck) — three, tapering slightly as they climb toward
// the head, each separated enough to read as a distinct joint.
const NECK_HEIGHT = 0.046;
// Round 2 (v3): each block's own hy previously summed to more than the
// gap between yFrac centres, so the three blocks overlapped into one
// solid taper with no visible seam — not the "distinct joint" stack the
// art direction asks for. Shrunk vertically and spread further apart so
// a real gap of dark background shows between each block.
const NECK_BLOCKS = [
  { yFrac: 0.08, hx: 0.0158, hy: 0.0062, hz: 0.0168 },
  { yFrac: 0.50, hx: 0.0136, hy: 0.0058, hz: 0.0146 },
  { yFrac: 0.92, hx: 0.0114, hy: 0.0054, hz: 0.0124 },
];
const NECK_BLOCK_RADIUS = 0.0035;
// How far forward of the neck's own top the head's centre sits — the
// head's bulk hangs out in front of the neck, not balanced directly atop
// it (docs/viewer.md "head carried out in front of the feet"). Round 1
// (v3): the reference's hallmark silhouette is the head projecting well
// past the feet on a pronounced forward lean — the previous 0.011 kept
// the head almost directly above the hips, reading as merely "tilted"
// rather than "carried out in front".
const HEAD_FORWARD_OF_NECK = 0.017;
// Round 2 (v3): spreading the neck blocks apart (see NECK_BLOCKS above)
// pushed the topmost block's own top edge down relative to before, and a
// positive offset here left a visible gap of bare background between the
// head's underside and the neck — a "floating head" instead of a
// carried one. Negative tucks the head down over the stack so it reads
// as seated on the neck from every camera angle, not just square-on.
const HEAD_UP_FROM_NECK = -0.006;

// Head: a rounded RECTANGULAR shell (capsule/loaf on its side) — wider
// and deeper than it is tall, never a sphere or a dome. Depth (front-back,
// hz) is the longest axis, matching the reference photography's "loaf
// lying on its side" silhouette.
// Round 2 (v3): HEAD_RADIUS was 75% of HEAD_HY — enough rounding to read
// as a smooth pod/bullet rather than a "rounded RECTANGULAR shell". Cut
// back so the box's flat top/side panels actually show, with the
// rounding reading as eased edges rather than the whole shape.
const HEAD_HX = 0.034, HEAD_HY = 0.020, HEAD_HZ = 0.038, HEAD_RADIUS = 0.010;

// Eye: one big circular camera lens on the FRONT of the head, nearly the
// shell's own height — the single feature that carries recognition, so it
// must be large and crisp. Modelled as two coaxial discs facing forward
// (docs/viewer.md "a dark glossy disc with a visible concentric ring"): a
// slightly larger bezel disc behind a smaller glossy lens disc set proud
// of it, so the bezel reads as a ring around the lens rather than a flat
// dot.
const EYE_BEZEL_R = 0.0180; // ~90% of HEAD_HY*2 (0.040) — "nearly the height of the shell"
const EYE_LENS_R = 0.0118; // a wider gap to EYE_BEZEL_R than before, so the ring reads as a band, not a sliver
const EYE_PROUD = 0.0018; // how far the lens disc sits ahead of the bezel disc
const EYE_Y = HEAD_HY * 0.06; // just above the head's own vertical centre
// Proud enough that the bezel clears the shell's own rounded corner
// falloff around it (EYE_BEZEL_R is bigger than HEAD_HY - HEAD_RADIUS,
// so the disc's own rim overlaps the curved part of the shell, not just
// the small flat patch at its centre) — round 1 (v3): the old 1.2mm was
// too close to the shell to read as its own protruding lens barrel; the
// bezel's satin grey blended into the shell's own highlight instead of
// standing out as a ring.
const EYE_Z = HEAD_HZ + 0.006;

// Bill: a flat yellow band wrapping the underside and front edge of the
// head, projecting forward as a broad spatula. billBand is fixed to the
// head (the wraparound rim); billJaw is the piece mouthOpen hinges down,
// revealing mouthInterior — a lighter fixed panel tucked just behind it.
const BILL_ANCHOR_Y = -HEAD_HY * 0.78;
const BILL_ANCHOR_Z = HEAD_HZ * 0.55;
const BILL_LENGTH = 0.036, BILL_WIDTH = 0.048; // width ~70% of the head's own diameter (2*HEAD_HX)
const BILL_BAND_THICK = 0.0060;
const BILL_JAW_THICK = 0.0052;
const BILL_MAX_ANGLE = 0.85; // radians the jaw hinges down at mouthOpen=1 (single moving half, so a wider swing than a symmetric clamshell)
const MOUTH_INTERIOR_LENGTH = 0.026, MOUTH_INTERIOR_WIDTH = 0.040;

// Legs: long, dark, visibly articulated — thigh and shin segments with a
// chunky joint block at hip and knee. These carry most of the duck's
// height (docs/viewer.md), so UPPER_LEG_LEN + LOWER_LEG_LEN == STAND_HEIGHT_M.
const UPPER_LEG_LEN = 0.072, LOWER_LEG_LEN = STAND_HEIGHT_M - UPPER_LEG_LEN;
const LEG_R_HIP = 0.0105, LEG_R_KNEE = 0.0075, LEG_R_ANKLE = 0.0058;
const HIP_X = 0.045; // lateral hip offset from centre (left/right) — a wide, stable stance
const HIP_JOINT = { hx: 0.0135, hy: 0.0125, hz: 0.0135, radius: 0.0055 };
const KNEE_JOINT = { hx: 0.0115, hy: 0.0105, hz: 0.0115, radius: 0.0048 };

// Feet: large flat yellow feet, oversized and slightly upturned at the
// front, with a darker sole. footSole is a full-size dark oval flush with
// the ground; footTop is a smaller yellow oval a hair above it, leaving a
// dark rim visible all round — the "darker sole" (a flat mesh has no
// underside of its own to colour separately, so the sole is a distinct,
// slightly larger part underneath rather than a second face).
const FOOT_RX = 0.031, FOOT_RZ = 0.050;
const FOOT_TOP_SCALE = 0.86;
const FOOT_TOP_LIFT = 0.0006; // clears the sole so the two don't z-fight
const FOOT_FORWARD_OFFSET = FOOT_RZ * 0.32; // foot centred ahead of the ankle, not under it
// The upturn tilts the plate about its own BACK edge, not its centre: a
// centre pivot lifts the toe but sinks the heel by the same amount below
// the floor. Pivoting at the back edge instead keeps that edge at the
// ankle's own (already floor-level) height and only the front rises.
const FOOT_TOE_UPTURN = 0.10; // radians

const REST_LEAN = 0.46; // pronounced forward crouch lean at rest (Art direction: "ready to move even at rest")
// The body's forward lean would otherwise carry straight through the neck
// and droop the head/eye down toward the ground — real BDX heads keep the
// camera roughly level regardless of body lean, the way a person crouching
// still looks ahead rather than at their own feet. The neck counters most
// (not all — a small residual forward gaze reads as attentive, not
// robotic) of REST_LEAN by default; pose.neckPitch still adds on top.
const HEAD_LEVEL_COUNTER = REST_LEAN * 0.85;
// A duck standing still still has bent knees, not locked-straight sticks;
// legSwing blends from this toward the dynamic walk-cycle bend as
// standAmount rises (docs/viewer.md "a visible knee").
// Exported because editor/duck-mesh.js has to SUBTRACT it: this pedestal
// exists for the primitive duck below, whose neutral leg is a straight
// stick, but the real MicroDuck skeleton is already crouched at its own
// MJCF STAND keyframe (the crouch lives in hip_pitch/ankle; STAND's knee is
// -0.0049 rad, i.e. essentially straight). Adding this on top of that
// double-crouches the real duck and floats its feet about a centimetre off
// the floor. See duck-mesh.js buildJointAngles().
export const REST_KNEE_BEND = 0.40;

// Colour split (docs/viewer.md "Colour split" / "Colour"): white/cream
// head shell and light-grey body panels take the role hue as a tint;
// yellow bill and feet are the product's identity and are never
// recoloured per role; the rest is dark mechanism or joint silver.
const HEAD_BASE = [0.94, 0.92, 0.885];
const HIP_PANEL_BASE = [0.70, 0.715, 0.73];
const BODY_COLOR = [0.62, 0.635, 0.655];
const DARK_MECH = [0.11, 0.115, 0.125];
const JOINT_SILVER = [0.74, 0.755, 0.775];
const BILL_YELLOW = [0.98, 0.80, 0.10];
const FOOT_SOLE_DARK = [0.10, 0.10, 0.11];
const EYE_LENS_COLOR = [0.030, 0.028, 0.035];
// Brightened well past the old near-black bezel (0.17): against the
// near-black lens it was invisible at render scale — no "visible
// concentric ring", just a slightly-larger dark disc. A satin metal tone
// close to JOINT_SILVER reads as a distinct bezel ring at a glance, the
// way the reference photography's lens rim does, without tipping into a
// role-hue colour the spec reserves for the shell and hip panels only.
const EYE_BEZEL_COLOR = [0.50, 0.51, 0.53];
const MOUTH_INTERIOR_COLOR = [0.85, 0.72, 0.66];

// How much of a role's raw hue tints the head shell and the hip panels —
// mixed toward each part's own base colour rather than drawn at full
// saturation, so the flock stays identifiable without losing the
// product's white/cream-and-yellow look (docs/viewer.md "Colour split").
// The head (large, central) reads clearly at a lighter mix; the hip
// panels are small enough to want a stronger one.
export const ROLE_BAND_MIX = 0.34;
const HIP_TINT_MIX = 0.62;

const MATTE = { shininess: 8, specular: 0.12 };
const SATIN = { shininess: 22, specular: 0.38 }; // brushed-metal joints and eye bezel
const GLOSSY = { shininess: 50, specular: 0.72 }; // glossy yellow bill plastic
// A tight specular lobe (like GLOSSY) collapses to a sub-pixel glint on
// something as small as the eye lens at typical render size — it reads as
// flat dark, not glossy. A broader, stronger lobe lights up enough of the
// disc's face to actually read as a catch-light.
const EYE_GLOSSY = { shininess: 14, specular: 0.95 };

function mixColor(base, tint, t) {
  return [
    base[0] + (tint[0] - base[0]) * t,
    base[1] + (tint[1] - base[1]) * t,
    base[2] + (tint[2] - base[2]) * t,
  ];
}

// ---------------------------------------------------------------------------
// Walk cycle (pure maths — no GL).
// ---------------------------------------------------------------------------

/**
 * Per-leg swing/lift for one walk-cycle phase, in radians (one full stride
 * is 2*PI — duckshow-viewer.js's precomputeRolePath integrates walkPhase at
 * exactly that rate: PHASE_PER_METRE = 2*PI/0.10, "one stride cycle per
 * 10 cm walked"). `side` is +1 (left, local +X) or -1 (right, local -X) —
 * legs alternate by starting half a cycle (PI radians) apart. `standAmount`
 * (0..1) fades the whole cycle out to a neutral stand. Do not re-scale
 * walkPhase by 2*PI here: it already *is* an angle, not a 0..1 fraction of
 * one — that mismatch previously made every duck's legs cycle roughly 2*PI
 * times faster than the distance they'd actually covered, a blur instead
 * of the waddle the art direction asks for.
 */
export function legSwing(walkPhase, standAmount, side) {
  const phase = walkPhase + (side < 0 ? Math.PI : 0);
  const thigh = Math.sin(phase) * 0.5 * standAmount;
  const lift = Math.max(0, Math.sin(phase + 0.55));
  // At full rest (standAmount=0) the knee still holds REST_KNEE_BEND rather
  // than locking straight; that fades over to the dynamic walk-cycle bend
  // as standAmount rises toward 1.
  const knee = REST_KNEE_BEND * (1 - standAmount) + lift * 0.85 * standAmount;
  return { thigh, knee };
}

/** Body bob/rock and the head's counter-rotation, from the walk phase (radians — see legSwing). */
function bodyWalkMotion(walkPhase, standAmount) {
  const phase = walkPhase;
  const bob = Math.abs(Math.sin(phase)) * 0.006 * standAmount;
  const rock = Math.sin(phase) * 0.11 * standAmount;
  return { bob, rock, headCounter: -rock * 0.6 };
}

/**
 * Advance the per-duck walk state given the pose's current walkPhase, the
 * real time (seconds) elapsed since the previous call, and (when the pose
 * source provides one, duckshow-viewer.js's derivePose always does)
 * `resting` — whether the duck is actually at rest *at this pose's time*.
 * `prev` is the value this returned last time (undefined on the first call
 * for a role). standAmount eases toward 1 while the duck is moving and back
 * toward 0 (neutral stand) once it rests — this is what lets a duck that
 * has stopped walking settle instead of freezing mid-stride (docs/viewer.md
 * "Motion").
 *
 * `resting` is read directly rather than inferred from consecutive calls'
 * walkPhase delta: walkPhase is an unbounded accumulating angle, so a
 * scrub that jumps the playhead across a whole walk segment can move it by
 * several strides in a single call even though the duck is at rest at both
 * the old and the new time — a delta-based guess misreads that as motion
 * and eases the legs toward a bogus mid-stride pose. When a pose source
 * has no notion of `resting` (e.g. a future MuJoCo stream), fall back to
 * the old delta heuristic so the walk cycle still degrades sensibly.
 */
export function updateWalkState(prev, walkPhase, dtSeconds, resting) {
  const p = prev || { lastPhase: walkPhase, standAmount: 0 };
  const dt = Math.max(0, Math.min(0.25, dtSeconds || 0));
  let moving;
  if (typeof resting === 'boolean') {
    moving = !resting;
  } else {
    let dPhase = walkPhase - p.lastPhase;
    dPhase -= Math.round(dPhase / (2 * Math.PI)) * (2 * Math.PI); // shortest distance around the 2*PI loop
    moving = Math.abs(dPhase) > 1e-5;
  }
  const rate = moving ? 7 : 4.5;
  const k = dt > 0 ? 1 - Math.exp(-rate * dt) : (moving ? 1 : 0);
  const target = moving ? 1 : 0;
  const standAmount = clamp01(p.standAmount + (target - p.standAmount) * k);
  return { lastPhase: walkPhase, standAmount };
}

// ---------------------------------------------------------------------------
// Pure hierarchy: pose (+ walk state) -> world-space mat4 per part.
// ---------------------------------------------------------------------------

const PART_NAMES = [
  'body', 'bodyDetail', 'hipL', 'hipR',
  'neckBlock0', 'neckBlock1', 'neckBlock2',
  'head', 'eyeBezel', 'eyeLens', 'billBand', 'billJaw', 'mouthInterior',
  'hipJointL', 'legUpperL', 'kneeJointL', 'legLowerL', 'footSoleL', 'footTopL',
  'hipJointR', 'legUpperR', 'kneeJointR', 'legLowerR', 'footSoleR', 'footTopR',
];

/**
 * Compute every part's world-space model matrix for one duck. `rootModel`
 * is the duck's placement (position + heading) as built by the caller
 * (StageRenderer maps pose.x/y/heading into render space and supplies
 * this); everything here is relative to it. Pure — no GL.
 */
export function computeDuckPose(rootModel, pose, walkState) {
  const standAmount = (walkState && walkState.standAmount) || 0;
  const walkPhase = pose.walkPhase || 0;
  const walk = bodyWalkMotion(walkPhase, standAmount);

  const bodyLocal = mat4Chain(
    mat4FromTranslation([0, STAND_HEIGHT_M + BODY_HY + (pose.bodyZ || 0) + walk.bob, 0]),
    mat4FromZRotation((pose.bodyRoll || 0) + walk.rock),
    mat4FromXRotation(REST_LEAN + (pose.bodyPitch || 0)),
  );
  const body = mat4Chain(rootModel, bodyLocal);

  const bodyDetail = mat4Chain(body, mat4FromTranslation([0, -BODY_HY * 0.35, BODY_HZ + BODY_DETAIL_HZ * 0.7]));
  const hipL = mat4Chain(body, mat4FromTranslation([BODY_HX + HIP_PANEL_HX * 0.75, -BODY_HY * 0.20, BODY_HZ * 0.05]));
  const hipR = mat4Chain(body, mat4FromTranslation([-(BODY_HX + HIP_PANEL_HX * 0.75), -BODY_HY * 0.20, BODY_HZ * 0.05]));

  const neckRoot = mat4Chain(
    body,
    mat4FromTranslation([0, BODY_HY * 0.75, BODY_HZ * 0.45]),
    mat4FromXRotation(-HEAD_LEVEL_COUNTER + (pose.neckPitch || 0)),
  );
  const neckBlockModel = (i) => mat4Chain(neckRoot, mat4FromTranslation([0, NECK_BLOCKS[i].yFrac * NECK_HEIGHT, 0]));
  const neckBlock0 = neckBlockModel(0);
  const neckBlock1 = neckBlockModel(1);
  const neckBlock2 = neckBlockModel(2);

  const headLocal = mat4Chain(
    mat4FromTranslation([0, NECK_HEIGHT + HEAD_UP_FROM_NECK, HEAD_FORWARD_OF_NECK]),
    mat4FromYRotation(pose.headYaw || 0),
    mat4FromXRotation(pose.headPitch || 0),
    mat4FromZRotation((pose.headRoll || 0) + walk.headCounter),
  );
  const head = mat4Chain(neckRoot, headLocal);

  // Eye: two coaxial forward-facing discs (see EYE_* comments above) —
  // rotating a disc (native normal +Y) by +90° about X turns it to face
  // +Z, i.e. straight out of the head's front.
  const eyeBezel = mat4Chain(head, mat4FromTranslation([0, EYE_Y, EYE_Z]), mat4FromXRotation(Math.PI / 2));
  const eyeLens = mat4Chain(head, mat4FromTranslation([0, EYE_Y, EYE_Z + EYE_PROUD]), mat4FromXRotation(Math.PI / 2));

  // billBand never rotates with mouthOpen — it is the fixed wraparound rim
  // that pairs with the hinging jaw below it to read as one solid spatula
  // when closed.
  const billAnchor = mat4Chain(head, mat4FromTranslation([0, BILL_ANCHOR_Y, BILL_ANCHOR_Z]));
  const billBand = billAnchor;
  const mouthOpen = clamp01(pose.mouthOpen || 0);
  const billJaw = mat4Chain(billAnchor, mat4FromXRotation(mouthOpen * BILL_MAX_ANGLE));
  const mouthInterior = mat4Chain(head, mat4FromTranslation([0, BILL_ANCHOR_Y + 0.003, BILL_ANCHOR_Z * 0.65]), mat4FromXRotation(0.25));

  const leg = (side) => {
    const { thigh, knee } = legSwing(walkPhase, standAmount, side);
    // Hip drops by the same (pose.bodyZ || 0) offset as the body above, so a
    // crouch (or a rise) moves the whole duck — hip, knee, foot — together
    // instead of the body sliding down through legs pinned at a fixed
    // stand height (docs/viewer.md "a visible knee" / legible crouch).
    const hipAnchor = mat4Chain(rootModel, mat4FromTranslation([side * HIP_X, STAND_HEIGHT_M + (pose.bodyZ || 0), 0]));
    const hip = mat4Chain(hipAnchor, mat4FromXRotation(thigh));
    const hipJointModel = hipAnchor; // the hip servo housing stays put; only the thigh below it swings
    const upperLegModel = mat4Translate(hip, [0, -UPPER_LEG_LEN / 2, 0]);
    const kneeJoint = mat4RotateX(mat4Translate(hip, [0, -UPPER_LEG_LEN, 0]), knee);
    const lowerLegModel = mat4Translate(kneeJoint, [0, -LOWER_LEG_LEN / 2, 0]);
    const footJoint = mat4RotateX(mat4Translate(kneeJoint, [0, -LOWER_LEG_LEN, 0]), -(thigh + knee));
    const footPivot = mat4Translate(footJoint, [0, 0, FOOT_FORWARD_OFFSET - FOOT_RZ]); // the plate's back edge
    const footModel = mat4RotateX(footPivot, -FOOT_TOE_UPTURN);
    const footSoleModel = mat4Translate(footModel, [0, 0, FOOT_RZ]); // plate centre, FOOT_RZ ahead of the pivot
    const footTopModel = mat4Translate(footModel, [0, FOOT_TOP_LIFT, FOOT_RZ]);
    return { hipJointModel, upperLegModel, kneeJoint, lowerLegModel, footSoleModel, footTopModel };
  };
  const left = leg(1);
  const right = leg(-1);

  return {
    body, bodyDetail, hipL, hipR,
    neckBlock0, neckBlock1, neckBlock2,
    head, eyeBezel, eyeLens, billBand, billJaw, mouthInterior,
    hipJointL: left.hipJointModel, legUpperL: left.upperLegModel, kneeJointL: left.kneeJoint,
    legLowerL: left.lowerLegModel, footSoleL: left.footSoleModel, footTopL: left.footTopModel,
    hipJointR: right.hipJointModel, legUpperR: right.upperLegModel, kneeJointR: right.kneeJoint,
    legLowerR: right.lowerLegModel, footSoleR: right.footSoleModel, footTopR: right.footTopModel,
  };
}

// ---------------------------------------------------------------------------
// GL: build meshes once, draw per duck.
// ---------------------------------------------------------------------------

function partMeshSpecs() {
  return {
    body: roundedBoxMesh({ hx: BODY_HX, hy: BODY_HY, hz: BODY_HZ, radius: BODY_RADIUS, segments: 3 }),
    bodyDetail: roundedBoxMesh({ hx: BODY_DETAIL_HX, hy: BODY_DETAIL_HY, hz: BODY_DETAIL_HZ, radius: BODY_DETAIL_RADIUS, segments: 2 }),
    hipPanel: roundedBoxMesh({ hx: HIP_PANEL_HX, hy: HIP_PANEL_HY, hz: HIP_PANEL_HZ, radius: HIP_PANEL_RADIUS, segments: 2 }),
    neckBlock0: roundedBoxMesh({ hx: NECK_BLOCKS[0].hx, hy: NECK_BLOCKS[0].hy, hz: NECK_BLOCKS[0].hz, radius: NECK_BLOCK_RADIUS, segments: 2 }),
    neckBlock1: roundedBoxMesh({ hx: NECK_BLOCKS[1].hx, hy: NECK_BLOCKS[1].hy, hz: NECK_BLOCKS[1].hz, radius: NECK_BLOCK_RADIUS, segments: 2 }),
    neckBlock2: roundedBoxMesh({ hx: NECK_BLOCKS[2].hx, hy: NECK_BLOCKS[2].hy, hz: NECK_BLOCKS[2].hz, radius: NECK_BLOCK_RADIUS, segments: 2 }),
    head: roundedBoxMesh({ hx: HEAD_HX, hy: HEAD_HY, hz: HEAD_HZ, radius: HEAD_RADIUS, segments: 4 }),
    eyeBezel: discMesh({ rx: EYE_BEZEL_R, rz: EYE_BEZEL_R, segments: 24 }),
    eyeLens: discMesh({ rx: EYE_LENS_R, rz: EYE_LENS_R, segments: 20 }),
    // tipWidth/tipThickness close to 1: a rounded spatula end, not a sharp
    // taper to a point (docs/viewer.md "a broad spatula").
    billBand: extrudedQuadMesh({ length: BILL_LENGTH, width: BILL_WIDTH, thicknessTop: BILL_BAND_THICK, thicknessBottom: 0, tipWidth: 0.74, tipThicknessTop: 0.55, tipThicknessBottom: 0.55 }),
    billJaw: extrudedQuadMesh({ length: BILL_LENGTH * 0.94, width: BILL_WIDTH * 0.94, thicknessTop: 0, thicknessBottom: BILL_JAW_THICK, tipWidth: 0.74, tipThicknessTop: 0.55, tipThicknessBottom: 0.55 }),
    mouthInterior: discMesh({ rx: MOUTH_INTERIOR_WIDTH / 2, rz: MOUTH_INTERIOR_LENGTH, segments: 4 }),
    hipJoint: roundedBoxMesh({ hx: HIP_JOINT.hx, hy: HIP_JOINT.hy, hz: HIP_JOINT.hz, radius: HIP_JOINT.radius, segments: 2 }),
    kneeJoint: roundedBoxMesh({ hx: KNEE_JOINT.hx, hy: KNEE_JOINT.hy, hz: KNEE_JOINT.hz, radius: KNEE_JOINT.radius, segments: 2 }),
    upperLeg: cylinderMesh({ radiusTop: LEG_R_HIP, radiusBottom: LEG_R_KNEE, height: UPPER_LEG_LEN, radialSegments: 10, capTop: false, capBottom: false }),
    lowerLeg: cylinderMesh({ radiusTop: LEG_R_KNEE, radiusBottom: LEG_R_ANKLE, height: LOWER_LEG_LEN, radialSegments: 10, capTop: false, capBottom: false }),
    footSole: discMesh({ rx: FOOT_RX, rz: FOOT_RZ, segments: 18 }),
    footTop: discMesh({ rx: FOOT_RX * FOOT_TOP_SCALE, rz: FOOT_RZ * FOOT_TOP_SCALE, segments: 18 }),
  };
}

/** Which shared mesh + material + colour each part in computeDuckPose()'s output uses. */
const PART_RENDER = {
  body: { mesh: 'body', material: MATTE, colorRole: 'body' },
  bodyDetail: { mesh: 'bodyDetail', material: MATTE, colorRole: 'mech' },
  hipL: { mesh: 'hipPanel', material: MATTE, colorRole: 'hip' },
  hipR: { mesh: 'hipPanel', material: MATTE, colorRole: 'hip' },
  neckBlock0: { mesh: 'neckBlock0', material: MATTE, colorRole: 'mech' },
  neckBlock1: { mesh: 'neckBlock1', material: MATTE, colorRole: 'mech' },
  neckBlock2: { mesh: 'neckBlock2', material: MATTE, colorRole: 'mech' },
  head: { mesh: 'head', material: MATTE, colorRole: 'head' },
  eyeBezel: { mesh: 'eyeBezel', material: SATIN, colorRole: 'eyeBezel' },
  eyeLens: { mesh: 'eyeLens', material: EYE_GLOSSY, colorRole: 'eyeLens' },
  billBand: { mesh: 'billBand', material: GLOSSY, colorRole: 'bill' },
  billJaw: { mesh: 'billJaw', material: GLOSSY, colorRole: 'bill' },
  mouthInterior: { mesh: 'mouthInterior', material: MATTE, colorRole: 'mouthInterior' },
  hipJointL: { mesh: 'hipJoint', material: SATIN, colorRole: 'joint' },
  hipJointR: { mesh: 'hipJoint', material: SATIN, colorRole: 'joint' },
  legUpperL: { mesh: 'upperLeg', material: MATTE, colorRole: 'mech' },
  legUpperR: { mesh: 'upperLeg', material: MATTE, colorRole: 'mech' },
  kneeJointL: { mesh: 'kneeJoint', material: SATIN, colorRole: 'joint' },
  kneeJointR: { mesh: 'kneeJoint', material: SATIN, colorRole: 'joint' },
  legLowerL: { mesh: 'lowerLeg', material: MATTE, colorRole: 'mech' },
  legLowerR: { mesh: 'lowerLeg', material: MATTE, colorRole: 'mech' },
  footSoleL: { mesh: 'footSole', material: MATTE, colorRole: 'sole' },
  footSoleR: { mesh: 'footSole', material: MATTE, colorRole: 'sole' },
  footTopL: { mesh: 'footTop', material: MATTE, colorRole: 'bill' },
  footTopR: { mesh: 'footTop', material: MATTE, colorRole: 'bill' },
};

/**
 * Build every duck part mesh once and upload it. Returns an opaque handle
 * to pass to drawDuck() / disposeDuckAssets(). Only touches GL when called.
 */
export function buildDuckAssets(gl) {
  const specs = partMeshSpecs();
  const meshes = {};
  for (const [name, mesh] of Object.entries(specs)) meshes[name] = uploadMesh(gl, mesh);
  return { meshes };
}

export function disposeDuckAssets(gl, assets) {
  if (!assets) return;
  for (const glMesh of Object.values(assets.meshes)) {
    gl.deleteBuffer(glMesh.vbo);
    gl.deleteBuffer(glMesh.ibo);
  }
}

// How dark a rehearsal-neutral duck (docs/authoring.md rehearsal tools:
// soloed-out or muted) renders relative to a normal one — dark enough that
// the soloed duck is unmistakably the one to watch, but never fully black,
// so its role colour and silhouette are still legible at a glance.
export const DIMMED_FACTOR = 0.34;

/**
 * Draw one duck. `litProgram` is viewer-gl.js's StageRenderer's compiled
 * lit program ({program, uniforms}), already `gl.useProgram`'d with
 * uViewProj/uEyePos set by the caller. `roleColorRgb` ([r,g,b] 0..1) tints
 * the head shell and the hip panels only — bill and feet stay yellow on
 * every duck, per the Art direction's colour split. `rimBoost` (0..1)
 * lifts the rim light for a selected duck. `dim` (0..1, default 1) darkens
 * every part uniformly — the rehearsal solo/mute "subdued" treatment.
 */
export function drawDuck(gl, litProgram, assets, rootModel, pose, walkState, roleColorRgb, rimBoost, dim = 1) {
  const parts = computeDuckPose(rootModel, pose, walkState);
  const { uniforms } = litProgram;
  gl.uniform1f(uniforms.uRimBoost, rimBoost || 0);

  const raw = roleColorRgb || HEAD_BASE;
  const headColor = mixColor(HEAD_BASE, raw, ROLE_BAND_MIX);
  const hipColor = mixColor(HIP_PANEL_BASE, raw, HIP_TINT_MIX);

  for (const name of PART_NAMES) {
    const model = parts[name];
    const render = PART_RENDER[name];
    const glMesh = assets.meshes[render.mesh];
    const color = render.colorRole === 'head' ? headColor
      : render.colorRole === 'hip' ? hipColor
        : render.colorRole === 'body' ? BODY_COLOR
          : render.colorRole === 'mech' ? DARK_MECH
            : render.colorRole === 'joint' ? JOINT_SILVER
              : render.colorRole === 'bill' ? BILL_YELLOW
                : render.colorRole === 'sole' ? FOOT_SOLE_DARK
                  : render.colorRole === 'eyeLens' ? EYE_LENS_COLOR
                    : render.colorRole === 'eyeBezel' ? EYE_BEZEL_COLOR
                      : render.colorRole === 'mouthInterior' ? MOUTH_INTERIOR_COLOR
                        : HEAD_BASE;

    gl.uniformMatrix4fv(uniforms.uModel, false, model);
    // Every part matrix in computeDuckPose() is rotation+translation only
    // (no mat4FromScale/mat4Scale anywhere in this file — the ovoid feet
    // are two differently-sized discMesh calls, not one mesh scaled at
    // draw time) — the cheap rigid extraction is exact here, not an
    // approximation; see its doc comment in viewer-gl.js for when that
    // stops being true.
    gl.uniformMatrix3fv(uniforms.uNormalMatrix, false, mat3FromMat4Rigid(model));
    gl.uniform3f(uniforms.uBaseColor, color[0] * dim, color[1] * dim, color[2] * dim);
    gl.uniform1f(uniforms.uShininess, render.material.shininess);
    gl.uniform1f(uniforms.uSpecularStrength, render.material.specular);

    bindMeshAttribs(gl, glMesh, 0, 1);
    drawMesh(gl, glMesh);
  }
}
