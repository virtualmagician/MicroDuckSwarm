// viewer-duck.js — the duck, built entirely from viewer-gl.js primitives
// (docs/viewer.md "The duck"). No dependencies, no CDN, no build step, and
// never a Pollen mesh: every part below is one of viewer-gl.js's hand-
// written primitive generators.
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
  eggMesh, uvSphereMesh, cylinderMesh, coneMesh, discMesh, torusMesh, extrudedQuadMesh,
  uploadMesh, bindMeshAttribs, drawMesh,
  mat4Chain, mat4FromTranslation, mat4Translate, mat4RotateX,
  mat4FromXRotation, mat4FromYRotation, mat4FromZRotation,
  mat3FromMat4Rigid,
  clamp01,
} from './viewer-gl.js';

// ---------------------------------------------------------------------------
// Real-world proportions (docs/viewer.md "The duck"): 25 cm tall, 14 cm
// wide, so blocking distances read honestly against the floor grid.
// ---------------------------------------------------------------------------

export const DUCK_HEIGHT_M = 0.25;
export const DUCK_WIDTH_M = 0.14;

// Hip height at rest (== total leg length), the height StageRenderer
// places pose.bodyZ=0 at. Legs are built to exactly this length so the
// feet touch the floor (y=0) in the neutral stand.
export const STAND_HEIGHT_M = 0.075;

// ---------------------------------------------------------------------------
// Part dimensions (metres), tuned to the proportions above.
// ---------------------------------------------------------------------------

const BODY_RX = 0.050, BODY_RY = 0.055, BODY_RZ = 0.056; // taller than wide (ry > rx, rz)
const BODY_TOP_TAPER = 0.42;
const BODY_BOTTOM_TAPER = 0.08;

// A real notch: tall and noticeably narrower than both the body and the
// head it carries, so body -> neck -> head reads as three distinct masses
// from the side (docs/viewer.md "no neck, so head and body are one blob").
const NECK_R_TOP = 0.014, NECK_R_BOTTOM = 0.020, NECK_HEIGHT = 0.030;

// Smaller than the old 0.028 on purpose — the head must read clearly
// smaller than the body, not just separated from it by the neck.
const HEAD_R = 0.024;
const EYE_R = 0.0068;

// A proper flat wide bill: BEAK_WIDTH is ~71% of the head's own diameter
// (2*HEAD_R), well inside the 60-75% the art direction calls for, and
// BEAK_THICKNESS is a small fraction of that width so it reads as a flat
// spatula rather than a cone. length ~= width so the tip clearly protrudes
// past the head's silhouette instead of hugging it.
const BEAK_LENGTH = 0.034, BEAK_WIDTH = 0.034, BEAK_THICKNESS = 0.0055;
const BEAK_MAX_ANGLE = 0.55; // radians each half rotates open at mouthOpen=1

const UPPER_LEG_LEN = 0.038, LOWER_LEG_LEN = STAND_HEIGHT_M - UPPER_LEG_LEN;
const LEG_R_HIP = 0.0075, LEG_R_KNEE = 0.006, LEG_R_FOOT = 0.005;
const HIP_X = 0.021; // lateral hip offset from centre (left/right)
// Wider than the leg (LEG_R_FOOT above) and big enough to read as a flat
// paddle-foot rather than vanish against the floor at typical zoom.
const FOOT_RX = 0.017, FOOT_RZ = 0.030;

const TAIL_LEN = 0.032, TAIL_R = 0.017;

// Narrow and close to the (now much slimmer) neck — a band, not a donut.
const COLLAR_MAJOR_R = 0.019, COLLAR_TUBE_R = 0.0032;

const REST_LEAN = 0.07; // slight forward lean at rest (Art direction)
// A duck standing still still has bent knees, not locked-straight sticks;
// legSwing blends from this toward the dynamic walk-cycle bend as
// standAmount rises (docs/viewer.md "a visible knee").
const REST_KNEE_BEND = 0.18;

const CREAM = [0.90, 0.84, 0.71];
const LEG_COLOR = [0.76, 0.50, 0.27];
// Warm amber/orange, visibly brighter/more saturated than both the cream
// body and the leg colour so the bill catches the key light and reads at
// small size (docs/viewer.md "The beak barely exists").
const BEAK_COLOR = [0.95, 0.62, 0.18];
const EYE_COLOR = [0.035, 0.032, 0.038];
// How much of a role's raw hue shows through the collar band and the
// floor start-mark ring — mixed toward CREAM/dimmed rather than drawn at
// full saturation, per docs/viewer.md "used sparingly" (defect #2: the
// band was "the brightest thing on the body").
export const ROLE_BAND_MIX = 0.55;

const MATTE = { shininess: 8, specular: 0.12 };
const GLOSSY = { shininess: 50, specular: 0.72 };
// A tight specular lobe (like GLOSSY, used for the beak) collapses to a
// sub-pixel glint on something as small as an eye at typical render size —
// it reads as flat dark, not glossy. A broader, stronger lobe lights up
// enough of the tiny sphere's front to actually read as a catch-light
// (docs/viewer.md defect #7).
const EYE_GLOSSY = { shininess: 14, specular: 0.95 };

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
function legSwing(walkPhase, standAmount, side) {
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
  'body', 'neck', 'head', 'beakUpper', 'beakLower', 'eyeL', 'eyeR',
  'legUpperL', 'legLowerL', 'footL', 'legUpperR', 'legLowerR', 'footR',
  'tail', 'collar',
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
    mat4FromTranslation([0, STAND_HEIGHT_M + BODY_RY + (pose.bodyZ || 0) + walk.bob, 0]),
    mat4FromZRotation((pose.bodyRoll || 0) + walk.rock),
    mat4FromXRotation(REST_LEAN + (pose.bodyPitch || 0)),
  );
  const body = mat4Chain(rootModel, bodyLocal);

  const neckLocal = mat4Chain(
    mat4FromTranslation([0, BODY_RY * 0.82, BODY_RZ * 0.55]),
    mat4FromXRotation(pose.neckPitch || 0),
  );
  const neck = mat4Chain(body, neckLocal);

  const headLocal = mat4Chain(
    mat4FromTranslation([0, NECK_HEIGHT, 0.006]),
    mat4FromYRotation(pose.headYaw || 0),
    mat4FromXRotation(pose.headPitch || 0),
    mat4FromZRotation((pose.headRoll || 0) + walk.headCounter),
  );
  const head = mat4Chain(neck, headLocal);

  const mouthOpen = clamp01(pose.mouthOpen || 0);
  // Both halves hinge at the same y (0, not +-0.003) so mouthOpen=0 closes
  // the beak with no gap along the centreline — each half's own thickness
  // (BEAK_THICKNESS top / *0.8 bottom, see partMeshSpecs) already gives the
  // closed bill its shape; the hinge itself must meet exactly.
  // Anchored shallower than the old 0.78 * HEAD_R: BEAK_WIDTH is now wide
  // enough (60-75% of the head's diameter) that at 0.78 the head sphere's
  // own cross-section had already narrowed past the beak's base width,
  // leaving small dark gaps at the bill's rear corners where background
  // showed through. 0.68 keeps the head's cross-section wider than the
  // beak's base so it fully backs it with no gap.
  const beakUpper = mat4Chain(head, mat4FromTranslation([0, 0, HEAD_R * 0.68]), mat4FromXRotation(-mouthOpen * BEAK_MAX_ANGLE));
  const beakLower = mat4Chain(head, mat4FromTranslation([0, 0, HEAD_R * 0.68]), mat4FromXRotation(mouthOpen * BEAK_MAX_ANGLE));

  // Pulled forward (higher Z) and in a touch (lower |X|) from the head's
  // equator so both eyes stay visible in a three-quarter view instead of
  // one rolling around to the side (docs/viewer.md "The eyes read as dark
  // scratches").
  const eyeL = mat4Chain(head, mat4FromTranslation([HEAD_R * 0.62, HEAD_R * 0.15, HEAD_R * 0.68]));
  const eyeR = mat4Chain(head, mat4FromTranslation([-HEAD_R * 0.62, HEAD_R * 0.15, HEAD_R * 0.68]));

  const collar = mat4Chain(neck, mat4FromTranslation([0, -NECK_HEIGHT * 0.05, 0]));

  // The anchor is the cone's own centre (mat4Chain applies the rotation
  // before this translation), so half of TAIL_LEN sits in front of it and
  // half behind: at -0.88 that put the open base only ~7mm shy of the
  // body's own back surface — close enough that the (now uncapped, see
  // partMeshSpecs) hollow base peeked past it as a small unlit dot when
  // orbiting round to view the tail. -0.72 buries the whole base well
  // inside the body so it's occluded from every angle; the tip still
  // clears the surface by the same margin as before.
  const tailLocal = mat4Chain(
    mat4FromTranslation([0, BODY_RY * 0.15, -BODY_RZ * 0.72]),
    mat4FromXRotation(-Math.PI / 2 + 0.32),
  );
  const tail = mat4Chain(body, tailLocal);

  const leg = (side) => {
    const { thigh, knee } = legSwing(walkPhase, standAmount, side);
    // Hip drops by the same (pose.bodyZ || 0) offset as the body above, so a
    // crouch (or a rise) moves the whole duck — hip, knee, foot — together
    // instead of the egg body sliding down through legs pinned at a fixed
    // stand height (docs/viewer.md "a visible knee" / legible crouch).
    const hip = mat4Chain(rootModel, mat4FromTranslation([side * HIP_X, STAND_HEIGHT_M + (pose.bodyZ || 0), 0]), mat4FromXRotation(thigh));
    const upperLegModel = mat4Translate(hip, [0, -UPPER_LEG_LEN / 2, 0]);
    const kneeJoint = mat4RotateX(mat4Translate(hip, [0, -UPPER_LEG_LEN, 0]), knee);
    const lowerLegModel = mat4Translate(kneeJoint, [0, -LOWER_LEG_LEN / 2, 0]);
    const footJoint = mat4RotateX(mat4Translate(kneeJoint, [0, -LOWER_LEG_LEN, 0]), -(thigh + knee));
    const footModel = mat4Translate(footJoint, [0, 0, FOOT_RZ * 0.28]);
    return { upperLegModel, lowerLegModel, footModel };
  };
  const left = leg(1);
  const right = leg(-1);

  return {
    body, neck, head, beakUpper, beakLower, eyeL, eyeR, collar, tail,
    legUpperL: left.upperLegModel, legLowerL: left.lowerLegModel, footL: left.footModel,
    legUpperR: right.upperLegModel, legLowerR: right.lowerLegModel, footR: right.footModel,
  };
}

// ---------------------------------------------------------------------------
// GL: build meshes once, draw per duck.
// ---------------------------------------------------------------------------

function partMeshSpecs() {
  return {
    body: eggMesh({ rx: BODY_RX, ry: BODY_RY, rz: BODY_RZ, topTaper: BODY_TOP_TAPER, bottomTaper: BODY_BOTTOM_TAPER, latBands: 14, lonBands: 18 }),
    neck: cylinderMesh({ radiusTop: NECK_R_TOP, radiusBottom: NECK_R_BOTTOM, height: NECK_HEIGHT, radialSegments: 12, capTop: false, capBottom: false }),
    head: uvSphereMesh(HEAD_R, 12, 16),
    // tipWidth/tipThickness closer to 1 than the old 0.55/0.4 — less of a
    // sharp taper to a point, more of a rounded spatula end (docs/viewer.md
    // "rounded at the tip", "a spatula, not a cone").
    beakUpper: extrudedQuadMesh({ length: BEAK_LENGTH, width: BEAK_WIDTH, thicknessTop: BEAK_THICKNESS, thicknessBottom: 0, tipWidth: 0.68, tipThicknessTop: 0.5, tipThicknessBottom: 0.5 }),
    beakLower: extrudedQuadMesh({ length: BEAK_LENGTH * 0.92, width: BEAK_WIDTH * 0.92, thicknessTop: 0, thicknessBottom: BEAK_THICKNESS * 0.8, tipWidth: 0.68, tipThicknessTop: 0.5, tipThicknessBottom: 0.5 }),
    eye: uvSphereMesh(EYE_R, 8, 10),
    upperLeg: cylinderMesh({ radiusTop: LEG_R_KNEE, radiusBottom: LEG_R_HIP, height: UPPER_LEG_LEN, radialSegments: 8, capTop: false, capBottom: false }),
    lowerLeg: cylinderMesh({ radiusTop: LEG_R_FOOT, radiusBottom: LEG_R_KNEE, height: LOWER_LEG_LEN, radialSegments: 8, capTop: false, capBottom: false }),
    foot: discMesh({ rx: FOOT_RX, rz: FOOT_RZ, segments: 14 }),
    // capBottom: false — the base sits just inside the egg body (see the
    // tail's translation in computeDuckPose), so a flat base cap would only
    // ever be seen edge-on as a dark, unlit disc punched into the body
    // silhouette (it faces away from every light). Leaving it open costs
    // nothing: the body's own shell occludes the hollow base from outside.
    tail: coneMesh({ radius: TAIL_R, height: TAIL_LEN, radialSegments: 10, capBottom: false }),
    collar: torusMesh({ majorRadius: COLLAR_MAJOR_R, minorRadius: COLLAR_TUBE_R, majorSegments: 22, minorSegments: 8 }),
  };
}

/** Which shared mesh + material + colour each part in computeDuckPose()'s output uses. */
const PART_RENDER = {
  body: { mesh: 'body', material: MATTE, colorRole: 'body' },
  neck: { mesh: 'neck', material: MATTE, colorRole: 'body' },
  head: { mesh: 'head', material: MATTE, colorRole: 'body' },
  beakUpper: { mesh: 'beakUpper', material: GLOSSY, colorRole: 'beak' },
  beakLower: { mesh: 'beakLower', material: GLOSSY, colorRole: 'beak' },
  eyeL: { mesh: 'eye', material: EYE_GLOSSY, colorRole: 'eye' },
  eyeR: { mesh: 'eye', material: EYE_GLOSSY, colorRole: 'eye' },
  legUpperL: { mesh: 'upperLeg', material: MATTE, colorRole: 'leg' },
  legLowerL: { mesh: 'lowerLeg', material: MATTE, colorRole: 'leg' },
  footL: { mesh: 'foot', material: MATTE, colorRole: 'leg' },
  legUpperR: { mesh: 'upperLeg', material: MATTE, colorRole: 'leg' },
  legLowerR: { mesh: 'lowerLeg', material: MATTE, colorRole: 'leg' },
  footR: { mesh: 'foot', material: MATTE, colorRole: 'leg' },
  tail: { mesh: 'tail', material: MATTE, colorRole: 'body' },
  collar: { mesh: 'collar', material: MATTE, colorRole: 'role' },
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

/**
 * Draw one duck. `litProgram` is viewer-gl.js's StageRenderer's compiled
 * lit program ({program, uniforms}), already `gl.useProgram`'d with
 * uViewProj/uEyePos set by the caller. `roleColorRgb` ([r,g,b] 0..1) tints
 * only the role-colour collar band; the body stays cream, per the Art
 * direction. `rimBoost` (0..1) lifts the rim light for a selected duck.
 */
export function drawDuck(gl, litProgram, assets, rootModel, pose, walkState, roleColorRgb, rimBoost) {
  const parts = computeDuckPose(rootModel, pose, walkState);
  const { uniforms } = litProgram;
  gl.uniform1f(uniforms.uRimBoost, rimBoost || 0);

  // The collar band shows the role colour, but mixed down toward CREAM
  // rather than at full saturation — docs/viewer.md "used sparingly"; a
  // raw role hue on the collar was the brightest thing on the body and
  // pulled the eye away from the face (defect #2).
  const raw = roleColorRgb || CREAM;
  const bandColor = [
    CREAM[0] + (raw[0] - CREAM[0]) * ROLE_BAND_MIX,
    CREAM[1] + (raw[1] - CREAM[1]) * ROLE_BAND_MIX,
    CREAM[2] + (raw[2] - CREAM[2]) * ROLE_BAND_MIX,
  ];

  for (const name of PART_NAMES) {
    const model = parts[name];
    const render = PART_RENDER[name];
    const glMesh = assets.meshes[render.mesh];
    const color = render.colorRole === 'eye' ? EYE_COLOR
      : render.colorRole === 'leg' ? LEG_COLOR
        : render.colorRole === 'beak' ? BEAK_COLOR
          : render.colorRole === 'role' ? bandColor
            : CREAM;

    gl.uniformMatrix4fv(uniforms.uModel, false, model);
    // Every part matrix in computeDuckPose() is rotation+translation only
    // (no mat4FromScale/mat4Scale anywhere in this file) — the cheap rigid
    // extraction is exact here, not an approximation; see its doc comment
    // in viewer-gl.js for when that stops being true.
    gl.uniformMatrix3fv(uniforms.uNormalMatrix, false, mat3FromMat4Rigid(model));
    gl.uniform3f(uniforms.uBaseColor, color[0], color[1], color[2]);
    gl.uniform1f(uniforms.uShininess, render.material.shininess);
    gl.uniform1f(uniforms.uSpecularStrength, render.material.specular);

    bindMeshAttribs(gl, glMesh, 0, 1);
    drawMesh(gl, glMesh);
  }
}
