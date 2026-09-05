// duck-mesh.js — the real MicroDuck, built from Pollen's own MJCF +
// STL meshes when a user has fetched them into the gitignored
// assets/microduck/ (docs/viewer.md Feature B "real meshes when
// available"; CLAUDE.md's licensing rule: those files are CC BY-SA-NC,
// never committed, never fetched automatically over the network — this
// module only ever reads them from the local dev server the person
// running the editor already started). Falls back to viewer-duck.js's
// primitive duck, silently and with no console error, whenever anything
// here is unavailable or fails — see loadRealDuckAssets().
//
// Reads assets/microduck/mjcf/robot_groundcontact.xml directly (the same
// file docs/bake-format.md's baker uses — "scene.xml (-> robot_ground
// contact.xml)" — the curated full-collision legged model, i.e. every
// body/geom a standing, walking, crouching duck needs). Only `class=
// "visual"` geoms are kept: every `class="collision"`/
// `"self_collision_only"` geom in this file reuses a mesh a visual geom
// at the same body already draws (checked by direct read), so this
// avoids drawing the same triangles twice rather than approximating
// anything.
//
// ---------------------------------------------------------------------
// Posing: the renderer contract (docs/viewer.md "Pose in, pixels out")
// only carries 8 joint-shaped values — headYaw/headPitch/headRoll/
// neckPitch and bodyZ/bodyRoll/bodyPitch (+walkPhase) — but the real
// skeleton has 14 hinge joints. The 4 head/neck fields map exactly onto
// the MJCF's neck_pitch/head_pitch/head_yaw/head_roll joints: both the
// kinematic sampler and a docs/bake-format.md pose cache already pass
// these through as literal joint radians (confirmed directly against a
// real bake — a role with no head track holds head_pitch=0, i.e. the
// hinge's own zero, while the SAME role's baked-physics result settles
// near the MJCF's own STAND keyframe value instead, exactly the
// intended/actual divergence docs/viewer.md's "Create Preview" is about)
// — so this module drives them the same way, no offset added.
//
// The 10 leg joints are driven one of two ways, in this order:
//
//   1. A BAKED pose carrying `joints` (docs/bake-format.md
//      `poses[role].joints`) supplies the real per-frame angle of each one,
//      straight off MuJoCo. That is what makes a baked preview show a real
//      gait -- actual foot placement, actual ground contact -- rather than
//      the stylised waddle below. The lookup is per joint, so a cache
//      recording only some still gets real angles for those.
//   2. Otherwise -- the kinematic preview, and any cache baked before that
//      block existed -- they are animated procedurally from walkPhase,
//      anchored at the MJCF's own STAND keyframe and swung with
//      viewer-duck.js's legSwing() shape (imported, not re-derived) so the
//      real duck's gait reads as the same "waddle" the primitive one does.
//      This is a named stylization, not a claim of physical accuracy,
//      exactly like the primitive duck's own walk cycle -- see
//      docs/viewer.md "What it is not".
// ---------------------------------------------------------------------
//
// Coordinate systems: the MJCF is X-forward, Y-left, Z-up (confirmed by
// direct read — trunk_base's quat is identity, and its children's own
// pos offsets read that way: the neck sits at +X/+Z from the trunk, the
// left hip at +Y, the right hip at -Y). viewer-gl.js's render space is
// Z-forward, X-left, Y-up. MJCF_TO_RENDER below is the fixed rotation
// (X,Y,Z)->(Y,Z,X) that converts one into the other — a pure axis
// permutation, determinant +1, no mirroring — applied exactly once, at
// the skeleton root, so every joint/body transform below it is built
// with plain MJCF-native quaternion maths and never has to think about
// the conversion again.

import {
  mat4Identity, mat4Chain, mat4FromTranslation, mat4FromQuat, mat4FromAxisAngle,
  mat4FromXRotation, mat4FromZRotation, mat3FromMat4Rigid,
  uploadMesh, bindMeshAttribs, drawMesh,
} from './viewer-gl.js';
import { legSwing, REST_KNEE_BEND } from './viewer-duck.js';
import { parseBinarySTL } from './stl-parser.js';

// Relative to editor/*.html, matching how ../shows/... is already
// resolved by duckshow-editor.html's own DEMO_URL.
export const MJCF_ASSET_BASE = '../assets/microduck/mjcf';
export const SKELETON_FILE = 'robot_groundcontact.xml'; // docs/bake-format.md's own choice; see this file's header comment

export const MJCF_TO_RENDER = new Float32Array([
  0, 0, 1, 0,
  1, 0, 0, 0,
  0, 1, 0, 0,
  0, 0, 0, 1,
]);

// scene.xml's own STAND keyframe (assets/microduck/mjcf/scene.xml,
// `<key name="STAND" .../>`) — trunk height at rest, and the resting
// angle of every leg joint the pose contract doesn't carry. Read
// directly, not guessed: qpos order is
// [x,y,z,qw,qx,qy,qz, L: hip_yaw,hip_roll,hip_pitch,knee,ankle,
//  neck_pitch,head_pitch,head_yaw,head_roll, R: hip_yaw,hip_roll,
//  hip_pitch,knee,ankle], matching the MJCF's own <actuator> order.
export const NOMINAL_TRUNK_Z = 0.12;
export const STAND_LEG = {
  left: { hip_yaw: 0, hip_roll: -0.08726646259971647, hip_pitch: -0.457924, knee: -0.004940, ankle: 0.452984 },
  right: { hip_yaw: 0, hip_roll: 0.08726646259971647, hip_pitch: 0.457924, knee: 0.004940, ankle: -0.452984 },
};

// Shell parts (near-white in their own <material rgba=...>) get the
// same role-hue tint the primitive duck's head shell gets — everything
// else keeps its real material colour. A small, explicit list rather
// than a colour-based heuristic, since the material list is short and
// known (docs/bake-parts.md §1a's own asset table).
const SHELL_MESHES = new Set(['left_shell', 'right_shell', 'top_head_shell', 'bottom_head_shell', 'trunk_base']);
export const SHELL_ROLE_TINT = 0.30;

function mixColor(base, tint, t) {
  return [base[0] + (tint[0] - base[0]) * t, base[1] + (tint[1] - base[1]) * t, base[2] + (tint[2] - base[2]) * t];
}

// ---------------------------------------------------------------------------
// MJCF parsing (browser DOMParser — this module only ever runs client-side).
// ---------------------------------------------------------------------------

function parseVec3(s, fallback) {
  if (!s) return fallback;
  const parts = s.trim().split(/\s+/).map(Number);
  return parts.length === 3 ? parts : fallback;
}
function parseQuat(s, fallback) {
  if (!s) return fallback;
  const parts = s.trim().split(/\s+/).map(Number);
  return parts.length === 4 ? parts : fallback;
}

function parseBodyElement(el) {
  const name = el.getAttribute('name');
  const pos = parseVec3(el.getAttribute('pos'), [0, 0, 0]);
  const quat = parseQuat(el.getAttribute('quat'), [1, 0, 0, 0]);
  let joint = null;
  const geoms = [];
  const children = [];
  for (const child of el.children) {
    const tag = child.tagName.toLowerCase();
    if (tag === 'joint') {
      joint = { name: child.getAttribute('name'), axis: parseVec3(child.getAttribute('axis'), [0, 0, 1]) };
    } else if (tag === 'geom' && child.getAttribute('class') === 'visual' && child.getAttribute('mesh')) {
      geoms.push({
        mesh: child.getAttribute('mesh'),
        material: child.getAttribute('material') || null,
        pos: parseVec3(child.getAttribute('pos'), [0, 0, 0]),
        quat: parseQuat(child.getAttribute('quat'), [1, 0, 0, 0]),
      });
    } else if (tag === 'body') {
      children.push(parseBodyElement(child));
    }
  }
  return { name, pos, quat, joint, geoms, children };
}

/**
 * Parse an MJCF document's worldbody + asset materials/mesh list into
 * { materials: Map<name,[r,g,b,a]>, meshFiles: Set<string> (no ".stl"),
 * trunk: BodyNode }. Throws on malformed XML or a missing <worldbody>/
 * root <body> — the caller (loadRealDuckAssets) treats any throw here
 * exactly like a missing file: silent fallback to the primitive duck.
 */
export function parseMjcfSkeleton(xmlText) {
  const doc = new DOMParser().parseFromString(xmlText, 'application/xml');
  const perr = doc.querySelector('parsererror');
  if (perr) throw new Error(`MJCF parse error: ${perr.textContent.slice(0, 200)}`);

  const materials = new Map();
  for (const m of doc.querySelectorAll('asset > material')) {
    const rgba = parseVec3(m.getAttribute('rgba'), [0.7, 0.7, 0.7]);
    const alpha = (m.getAttribute('rgba') || '').trim().split(/\s+/)[3];
    materials.set(m.getAttribute('name'), [rgba[0], rgba[1], rgba[2], alpha !== undefined ? Number(alpha) : 1]);
  }
  const meshFiles = new Set();
  for (const m of doc.querySelectorAll('asset > mesh')) {
    const file = m.getAttribute('file');
    if (file) meshFiles.add(file.replace(/\.stl$/i, ''));
  }
  const trunkEl = doc.querySelector('worldbody > body');
  if (!trunkEl) throw new Error('MJCF: no root <body> found under <worldbody>');
  return { materials, meshFiles, trunk: parseBodyElement(trunkEl) };
}

// ---------------------------------------------------------------------------
// Forward kinematics — pure, no GL. See this file's header for the joint
// mapping rationale.
// ---------------------------------------------------------------------------

/**
 * The 14 hinge joint angles for one duck's current pose/walk state.
 * Head/neck come straight from the pose (literal joint radians, see
 * header comment); the 10 leg joints are a stylized walk-cycle swing
 * anchored at STAND_LEG, mirrored per side to match how STAND's own
 * left/right values are already sign-mirrored for "the same" physical
 * angle (confirmed directly from scene.xml).
 */
export function buildJointAngles(pose, walkState) {
  const standAmount = (walkState && walkState.standAmount) || 0;
  const walkPhase = pose.walkPhase || 0;
  const angles = new Map([
    ['neck_pitch', pose.neckPitch || 0],
    ['head_pitch', pose.headPitch || 0],
    ['head_yaw', pose.headYaw || 0],
    ['head_roll', pose.headRoll || 0],
  ]);
  // A baked pose may carry the real per-frame angle of every leg joint,
  // straight off MuJoCo (docs/bake-format.md `poses[role].joints`). When it
  // does, use it: that is the whole point of baking, and it is the only way
  // the preview shows a real gait rather than the procedural walk cycle
  // below re-keyed off trunk displacement. The fallback is PER JOINT, not
  // all-or-nothing, so a cache recording only some joints still gets real
  // angles for those -- and every cache baked before the block existed
  // behaves exactly as it did before.
  const baked = pose.joints || null;
  const bakedOr = (name, computed) => {
    const v = baked ? baked[name] : undefined;
    return typeof v === 'number' && Number.isFinite(v) ? v : computed;
  };

  for (const [side, sign, prefix] of [[1, 1, 'left'], [-1, -1, 'right']]) {
    const { thigh, knee: kneeRaw } = legSwing(walkPhase, standAmount, side);
    // legSwing()'s knee carries a REST_KNEE_BEND pedestal that fades out as
    // standAmount rises. That pedestal is for the PRIMITIVE duck, whose
    // neutral leg is a straight stick and would otherwise lock out. The real
    // skeleton is already crouched at its own MJCF STAND keyframe -- the
    // crouch lives in hip_pitch/ankle, and STAND's knee is -0.0049 rad, i.e.
    // straight -- so adding 0.40 rad on top double-crouches it and floats the
    // soles about 1 cm off the floor. Take only the dynamic part here.
    const knee = kneeRaw - REST_KNEE_BEND * (1 - standAmount);
    const stand = sign > 0 ? STAND_LEG.left : STAND_LEG.right;
    angles.set(`${prefix}_hip_yaw`, bakedOr(`${prefix}_hip_yaw`, stand.hip_yaw));
    angles.set(`${prefix}_hip_roll`, bakedOr(`${prefix}_hip_roll`, stand.hip_roll));
    angles.set(`${prefix}_hip_pitch`, bakedOr(`${prefix}_hip_pitch`, stand.hip_pitch + sign * thigh));
    angles.set(`${prefix}_knee`, bakedOr(`${prefix}_knee`, stand.knee + sign * knee));
    // The three pitch joints do NOT share a world axis. Composing the body
    // quaternions and STAND's +/-5 deg hip_roll, the LEFT leg's hip_pitch and
    // ankle hinge about world (0, +0.9962, -0.0872) while the knee hinges
    // about the OPPOSITE direction (0, -0.9962, +0.0872); the right leg
    // mirrors all three. (Every joint's literal MJCF attribute is
    // axis="0 0 1" -- these are the composed world axes, not values you can
    // grep for in the XML.) So the sole's world pitch is
    // hip_pitch - knee + ankle, not the plain sum, and STAND satisfies that
    // identity exactly: -0.457924 - (-0.004940) + 0.452984 = 0.
    //
    // Holding the sole at its STAND orientation therefore means the ankle
    // ADDS the knee bend and SUBTRACTS the thigh swing. Subtracting both
    // doubled the knee into the ankle instead of cancelling it: at rest that
    // put -0.40 rad in, pitching both soles 46 deg off the floor, and during
    // a swing it reached 85 deg -- nearly vertical. Checked against MuJoCo's
    // own mj_forward at STAND, the corrected chain reproduces sole_left world
    // z 0.00282..0.01631 m to five decimals, and holds that orientation at
    // every walk phase and every standAmount.
    angles.set(`${prefix}_ankle`, bakedOr(`${prefix}_ankle`, stand.ankle + sign * (knee - thigh)));
  }
  return angles;
}

/**
 * Every visual geom's model matrix, in MJCF-native local coordinates
 * relative to the skeleton root (the trunk body's own listed pos/quat is
 * deliberately NOT applied — the caller supplies the trunk's placement
 * externally, from the pose, exactly like viewer-duck.js's rootModel).
 * `jointAngles` is a buildJointAngles() result. Pure, no GL.
 */
export function buildGeomInstances(skeleton, jointAngles) {
  const instances = [];
  const colorFor = (materialName) => {
    const rgba = materialName && skeleton.materials.get(materialName);
    return rgba ? [rgba[0], rgba[1], rgba[2]] : [0.7, 0.7, 0.7];
  };
  const visit = (body, parentMat, isRoot) => {
    const m = isRoot ? parentMat : mat4Chain(parentMat, mat4FromTranslation(body.pos), mat4FromQuat(body.quat));
    let jointed = m;
    if (!isRoot && body.joint) {
      const angle = jointAngles.get(body.joint.name) || 0;
      jointed = mat4Chain(m, mat4FromAxisAngle(body.joint.axis, angle));
    }
    for (const g of body.geoms) {
      const gm = mat4Chain(jointed, mat4FromTranslation(g.pos), mat4FromQuat(g.quat));
      instances.push({ mesh: g.mesh, color: colorFor(g.material), model: gm });
    }
    for (const child of body.children) visit(child, jointed, false);
  };
  visit(skeleton.trunk, mat4Identity(), true);
  return instances;
}

// ---------------------------------------------------------------------------
// Asset loading — the only place this module touches the network or GL.
// ---------------------------------------------------------------------------

/**
 * Fetch + parse the MJCF skeleton and every mesh it references, upload
 * each unique mesh to GL exactly once (shared across all 8+ ducks and
 * every body part that reuses it — "same geometry, per-duck transforms",
 * not one GPU-memory copy per occurrence), and return a handle for
 * drawRealDuck()/disposeRealDuckAssets(). Resolves to `null` on ANY
 * failure — missing directory, a 404 on one mesh, a parse error — and
 * never throws and never logs a console error: this is the expected,
 * silent state for a fresh clone (see this file's header comment).
 * `baseUrl` defaults to the real asset path; override only for tests
 * that don't touch the network.
 */
export async function loadRealDuckAssets(gl, { baseUrl = MJCF_ASSET_BASE, fetchImpl = fetch } = {}) {
  try {
    const skeletonRes = await fetchImpl(`${baseUrl}/${SKELETON_FILE}`, { cache: 'no-cache' });
    if (!skeletonRes.ok) return null;
    const xmlText = await skeletonRes.text();
    const skeleton = parseMjcfSkeleton(xmlText);

    const meshNames = [...skeleton.meshFiles];
    const fetched = await Promise.all(meshNames.map(async (name) => {
      const res = await fetchImpl(`${baseUrl}/assets/${name}.stl`, { cache: 'no-cache' });
      if (!res.ok) throw new Error(`missing mesh ${name}.stl (HTTP ${res.status})`);
      const buf = await res.arrayBuffer();
      return [name, parseBinarySTL(buf)];
    }));

    const meshesByFile = new Map();
    const triCountByFile = new Map();
    let uniqueTriangleCount = 0;
    for (const [name, mesh] of fetched) {
      meshesByFile.set(name, uploadMesh(gl, mesh));
      triCountByFile.set(name, mesh.triangleCount);
      uniqueTriangleCount += mesh.triangleCount;
    }

    // Per-duck rendered triangle count: sum over every geom OCCURRENCE
    // (a mesh reused N times in the skeleton counts N times here, since
    // it is genuinely drawn N times) — the number that actually drives
    // vertex/fragment cost per duck, distinct from uniqueTriangleCount
    // (the one-copy GPU memory footprint above).
    const instances = buildGeomInstances(skeleton, buildJointAngles({}, null));
    let perDuckTriangleCount = 0;
    for (const inst of instances) perDuckTriangleCount += triCountByFile.get(inst.mesh) || 0;

    return {
      skeleton,
      meshesByFile,
      meshCount: meshNames.length,
      instanceCount: instances.length,
      uniqueTriangleCount,
      perDuckTriangleCount,
    };
  } catch (_) {
    return null;
  }
}

export function disposeRealDuckAssets(gl, real) {
  if (!real) return;
  for (const glMesh of real.meshesByFile.values()) {
    gl.deleteBuffer(glMesh.vbo);
    gl.deleteBuffer(glMesh.ibo);
  }
}

// ---------------------------------------------------------------------------
// GL: draw one real duck. Reuses viewer-gl.js's already-bound "lit"
// shader program (same uModel/uNormalMatrix/uBaseColor/uShininess/
// uSpecularStrength/uRimBoost uniforms drawDuck() sets — see
// viewer-duck.js), so no second shader is compiled for this path.
// ---------------------------------------------------------------------------

const REAL_MATERIAL = { shininess: 14, specular: 0.22 }; // one plain matte-ish look for every part; the MJCF's own material rgba already does the colour differentiation

export function drawRealDuck(gl, litProgram, real, rootModel, pose, walkState, roleColorRgb, rimBoost, dim = 1) {
  const { uniforms } = litProgram;
  gl.uniform1f(uniforms.uRimBoost, rimBoost || 0);

  const bodyLocal = mat4Chain(
    mat4FromTranslation([0, NOMINAL_TRUNK_Z + (pose.bodyZ || 0), 0]),
    // Order matters and was inverted. mat4Chain(A, B) applies B first, so
    // listing roll before pitch made PITCH act first, while
    // tools/bake/bakelib/sim.py decomposes the trunk quaternion as extrinsic
    // Z-Y-X -- R = Rz(yaw)*Ry(pitch)*Rx(roll), i.e. ROLL first. Baking and
    // replaying a duck that was both rolled and pitched therefore did not
    // reproduce the orientation the physics actually had. Second-order for
    // the small deltas-from-upright the format defines, but the two halves
    // should agree exactly rather than nearly.
    mat4FromXRotation(pose.bodyPitch || 0),
    mat4FromZRotation(pose.bodyRoll || 0),
  );
  const realRoot = mat4Chain(rootModel, bodyLocal, MJCF_TO_RENDER);

  const jointAngles = buildJointAngles(pose, walkState);
  const instances = buildGeomInstances(real.skeleton, jointAngles);
  for (const inst of instances) {
    const glMesh = real.meshesByFile.get(inst.mesh);
    if (!glMesh) continue; // should never happen once loadRealDuckAssets() has succeeded — every referenced mesh was fetched
    const model = mat4Chain(realRoot, inst.model);
    const base = SHELL_MESHES.has(inst.mesh) ? mixColor(inst.color, roleColorRgb || inst.color, SHELL_ROLE_TINT) : inst.color;

    gl.uniformMatrix4fv(uniforms.uModel, false, model);
    gl.uniformMatrix3fv(uniforms.uNormalMatrix, false, mat3FromMat4Rigid(model));
    gl.uniform3f(uniforms.uBaseColor, base[0] * dim, base[1] * dim, base[2] * dim);
    gl.uniform1f(uniforms.uShininess, REAL_MATERIAL.shininess);
    gl.uniform1f(uniforms.uSpecularStrength, REAL_MATERIAL.specular);

    bindMeshAttribs(gl, glMesh, 0, 1);
    drawMesh(gl, glMesh);
  }
}
