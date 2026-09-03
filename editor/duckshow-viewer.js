// duckshow-viewer.js — pose derivation and the editor-facing stage-view
// controller (docs/viewer.md). No DOM/GL of its own: this module turns a
// .duckshow document + a moment in show time into plain pose/trail/label
// data, and a StageViewer class that wires that data to an injected
// renderer plus the canvas input (orbit, dolly, mark drag).
//
// Reuses editor/duckshow-core.js for everything protocol-shaped (the
// sampler and dead-reckoning integrator); nothing here reimplements them.
// "show" parameters below are the raw parsed .duckshow document (as
// returned by core.parseShow / held as the editor's live state) — the same
// object core.getMark/setMark expect, so the top-level "editor.marks"
// block is visible. Passing an already-normalized show still works for
// every function except mark resolution (normalizeShow() does not carry
// unknown top-level fields like "editor" through).
//
// ---------------------------------------------------------------------
// Renderer contract (implemented by editor/viewer-gl.js + viewer-duck.js,
// injected into StageViewer's constructor — duck-typed, only `render` is
// required):
//
//   renderer.resize(width, height, dpr)   canvas size changed
//   renderer.setPalette(paletteMap)       Map<role, {hue,saturation,
//                                          lightness,rgb:[r,g,b],hex}>,
//                                          called whenever the cast changes
//   renderer.render(frame)                draw one frame:
//     frame.t              show time (s)
//     frame.poses          [{role,x,y,heading,headYaw,headPitch,headRoll,
//                             neckPitch,bodyZ,bodyRoll,bodyPitch,
//                             mouthOpen,walkPhase,resting,dimmed}, ...]  --
//                           docs/viewer.md "Pose in, pixels out". `resting`
//                           (bool, optional) is the ground-truth "at rest
//                           right now" signal a walk cycle needs and
//                           walkPhase alone can't safely provide across an
//                           arbitrary time jump; a pose source that omits
//                           it (e.g. a future MuJoCo stream) is still valid.
//                           `dimmed` (bool, optional) marks a role rehearsal
//                           solo/mute is holding neutral right now
//                           (docs/authoring.md rehearsal tools) — a renderer
//                           that doesn't distinguish it can safely ignore it.
//     frame.trails         Map<role, [{x,y,brightness}, ...]>  brightness
//                           0..1, brightest = current position
//     frame.labels         [{role,text,t,kind:'skill'|'sound'}, ...]
//     frame.selectedRole   string | null
//     frame.camera         {eye:[x,y,z], target:[x,y,z], up:[x,y,z],
//                            fovY (radians), aspect}
//     frame.marks          Map<role, {x,y,heading}>  for start-mark rings
//   renderer.dispose()                    release GL resources
//
// StageViewer never renders on a permanent loop: setShow/setTime/
// setSelectedRole/orbit/dolly/mark-drag each paint at most once per
// animation frame (render on change), and a camera-preset switch only
// keeps the frame loop alive for the ~0.5 s of its own ease.
// ---------------------------------------------------------------------

import { normalizeShow, createSampler, integrate, getMark } from './duckshow-core.js';

// ---------------------------------------------------------------------------
// Small maths helpers — no dependency, just what a stage camera needs.
// ---------------------------------------------------------------------------

function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
function lerp(a, b, t) { return a + (b - a) * t; }
function deg(d) { return (d * Math.PI) / 180; }
function round(v, digits) { const m = 10 ** digits; return Math.round(v * m) / m; }

function vAdd(a, b) { return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]; }
function vSub(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
function vScale(a, s) { return [a[0] * s, a[1] * s, a[2] * s]; }
function vCross(a, b) {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}
function vLen(a) { return Math.hypot(a[0], a[1], a[2]); }
function vNorm(a) { const n = vLen(a); return n > 1e-12 ? [a[0] / n, a[1] / n, a[2] / n] : [0, 0, 0]; }

// ---------------------------------------------------------------------------
// Stage marks — position/heading a role starts from. Explicit marks live
// under the raw document's top-level "editor.marks" (docs/authoring.md §3);
// a role with none gets a sensible spread instead of stacking at the origin.
// ---------------------------------------------------------------------------

/** Metres between adjacent roles in the default line-up (unset marks only). */
export const DEFAULT_MARK_SPACING = 0.8;

/** A row formation across the lateral (y) axis, centred on x=0/y=0. */
export function defaultMarkFor(index, total) {
  const y = total > 1 ? (index - (total - 1) / 2) * DEFAULT_MARK_SPACING : 0;
  return { x: 0, y, heading: 0 };
}

function hasExplicitMark(show, role) {
  const marks = show && show.editor && show.editor.marks;
  return Boolean(marks) && Object.prototype.hasOwnProperty.call(marks, role);
}

/** The mark a role's dead reckoning should start from: editor.marks[role] if set, else a spread default. */
export function resolveMark(show, role, index = 0, total = 1) {
  if (hasExplicitMark(show, role)) return getMark(show, role);
  return defaultMarkFor(index, total);
}

/** resolveMark() for every role in the cast, keyed by role name. */
export function resolveMarks(show) {
  const norm = normalizeShow(show);
  const marks = new Map();
  norm.cast.forEach((member, i) => marks.set(member.role, resolveMark(show, member.role, i, norm.cast.length)));
  return marks;
}

// ---------------------------------------------------------------------------
// Dead reckoning — precomputed once per show edit, sampled cheaply per frame.
// Position/heading reuse core.integrate() verbatim; walkPhase is derived
// from the *distance* integrate() already produced between consecutive
// samples, so it never re-touches locomotion/servo logic itself.
// ---------------------------------------------------------------------------

export const DEFAULT_DT = 0.02; // matches duckshow-core's integrate() default and the editor's frame step
export const STOP_SPEED_EPS = 0.005; // m/s below which the duck is considered "at rest"
export const PHASE_PER_METRE = (2 * Math.PI) / 0.10; // one stride cycle per 10 cm walked

/**
 * Dead-reckoned path for one role, sampled every `dt` from t=0 to the show's
 * end (core.integrate semantics), plus a derived walkPhase per sample.
 * Call once per show edit (or per mark drag); interpolate with
 * sampleRolePath() on every playhead change instead of recomputing.
 */
export function precomputeRolePath(show, role, mark = { x: 0, y: 0, heading: 0 }, dt = DEFAULT_DT) {
  const raw = integrate(show, role, dt, { start: mark });
  const n = raw.length;
  const t = new Float64Array(n);
  const x = new Float64Array(n);
  const y = new Float64Array(n);
  const heading = new Float64Array(n);
  const walkPhase = new Float64Array(n);
  const speed = new Float64Array(n); // m/s between sample i-1 and i (0 at i=0) — lets a consumer ask "is this duck moving *right now*" without inferring it from walkPhase deltas across arbitrary renders (a scrub can jump many strides in one call).
  let phase = 0;
  for (let i = 0; i < n; i++) {
    const p = raw[i];
    t[i] = p.t; x[i] = p.x; y[i] = p.y; heading[i] = p.heading;
    if (i > 0) {
      const h = t[i] - t[i - 1];
      const dist = Math.hypot(x[i] - x[i - 1], y[i] - y[i - 1]);
      const s = h > 0 ? dist / h : 0;
      speed[i] = s;
      if (s > STOP_SPEED_EPS) phase += dist * PHASE_PER_METRE; // else: hold — settles to a neutral stand
    }
    walkPhase[i] = phase;
  }
  return { role, dt, mark, t, x, y, heading, walkPhase, speed };
}

/** precomputeRolePath() for every cast role. `marks` defaults to resolveMarks(show). */
export function precomputeShowPaths(show, opts = {}) {
  const dt = opts.dt || DEFAULT_DT;
  const marks = opts.marks || resolveMarks(show);
  const norm = normalizeShow(show);
  const paths = new Map();
  for (const member of norm.cast) {
    const role = member.role;
    paths.set(role, precomputeRolePath(show, role, marks.get(role) || defaultMarkFor(0, 1), dt));
  }
  return paths;
}

function singlePointPath(role, mark) {
  return { role, dt: DEFAULT_DT, mark, t: [0], x: [mark.x], y: [mark.y], heading: [mark.heading], walkPhase: [0], speed: [0] };
}

/** Interpolate a precomputed path at an arbitrary show time (holds at both ends). */
export function sampleRolePath(path, tQuery) {
  const n = path.t.length;
  if (n === 0) return { x: 0, y: 0, heading: 0, walkPhase: 0, speed: 0 };
  if (n === 1 || tQuery <= path.t[0]) {
    return { x: path.x[0], y: path.y[0], heading: path.heading[0], walkPhase: path.walkPhase[0], speed: path.speed[0] };
  }
  const last = n - 1;
  if (tQuery >= path.t[last]) {
    return { x: path.x[last], y: path.y[last], heading: path.heading[last], walkPhase: path.walkPhase[last], speed: path.speed[last] };
  }
  // The grid is uniform (dt) except possibly its final, shorter segment, so a
  // direct index guess lands on (or one step from) the right bracket — O(1)
  // amortized, no per-frame binary search over the whole path.
  let i = clamp(Math.floor((tQuery - path.t[0]) / path.dt), 0, n - 2);
  while (i < n - 2 && path.t[i + 1] <= tQuery) i++;
  while (i > 0 && path.t[i] > tQuery) i--;
  const t0 = path.t[i], t1 = path.t[i + 1];
  const frac = t1 > t0 ? (tQuery - t0) / (t1 - t0) : 0;
  return {
    x: lerp(path.x[i], path.x[i + 1], frac),
    y: lerp(path.y[i], path.y[i + 1], frac),
    heading: lerp(path.heading[i], path.heading[i + 1], frac),
    walkPhase: lerp(path.walkPhase[i], path.walkPhase[i + 1], frac),
    speed: lerp(path.speed[i], path.speed[i + 1], frac),
  };
}

// ---------------------------------------------------------------------------
// Rehearsal state — solo/mute (docs/authoring.md rehearsal tools, M3). This
// is editor *session* state, never show content: it never reads or writes
// the .duckshow document, and unlike the start marks above it is never
// eligible for the top-level "editor" field either — the editor persists it,
// if at all, in localStorage only, exactly like the name-label preference.
//
// Mixing-desk semantics: solo is exclusive (at most one role) and overrides
// mute outright rather than combining with it. Soloing/un-soloing never
// touches the muted set itself — toggleMute is the only thing that does —
// so un-soloing simply resumes reading whatever mutes were already set.
// ---------------------------------------------------------------------------

/** A fresh rehearsal state: nothing soloed, nothing muted. */
export function createRehearsalState() {
  return { solo: null, muted: new Set() };
}

/** Solo `role`, or un-solo if it is already the soloed role. Exclusive: soloing a new role replaces any previous one. */
export function toggleSolo(state, role) {
  return { solo: state.solo === role ? null : role, muted: state.muted };
}

/** Force solo off (e.g. its role left the cast). No-op if nothing is soloed. */
export function clearSolo(state) {
  return state.solo == null ? state : { solo: null, muted: state.muted };
}

/** Toggle whether `role` is muted. Independent of solo — see resolveNeutralRoles() for how the two combine. */
export function toggleMute(state, role) {
  const muted = new Set(state.muted);
  if (muted.has(role)) muted.delete(role); else muted.add(role);
  return { solo: state.solo, muted };
}

/**
 * The roles that should be held at their neutral standing pose right now
 * (docs/authoring.md: "every other duck holds its neutral standing pose at
 * its mark"), given the current solo/mute state and cast. Soloing shrinks
 * the active set to just the soloed role, full stop — the muted set is not
 * consulted at all while a solo is active, which is what makes un-soloing a
 * pure restore rather than a merge.
 */
export function resolveNeutralRoles(state, roleNames) {
  const neutral = new Set();
  if (state.solo != null && roleNames.includes(state.solo)) {
    for (const r of roleNames) if (r !== state.solo) neutral.add(r);
    return neutral;
  }
  for (const r of roleNames) if (state.muted.has(r)) neutral.add(r);
  return neutral;
}

/** Drop any solo/mute referring to a role no longer in the cast (e.g. role removed). Identity-preserving when nothing is stale. */
export function pruneRehearsalState(state, roleNames) {
  const cast = new Set(roleNames);
  const solo = state.solo != null && cast.has(state.solo) ? state.solo : null;
  const staleMute = [...state.muted].some((r) => !cast.has(r));
  const muted = staleMute ? new Set([...state.muted].filter((r) => cast.has(r))) : state.muted;
  if (solo === state.solo && muted === state.muted) return state;
  return { solo, muted };
}

/** Carry a solo/mute reference across a role rename (from -> to). Identity-preserving when `from` was neither soloed nor muted. */
export function renameInRehearsalState(state, from, to) {
  if (from === to) return state;
  const solo = state.solo === from ? to : state.solo;
  let muted = state.muted;
  if (muted.has(from)) { muted = new Set(muted); muted.delete(from); muted.add(to); }
  if (solo === state.solo && muted === state.muted) return state;
  return { solo, muted };
}

// ---------------------------------------------------------------------------
// Pose derivation — position/heading/walkPhase from the precomputed path;
// head/pose/mouth pass straight through the sampler (docs/viewer.md §1/§3).
// ---------------------------------------------------------------------------

/** The pose a role at rest on its mark shows: mixing-desk "neutral" for a soloed-out or muted role (docs/authoring.md rehearsal tools). */
function neutralPoseAt(role, mark) {
  return {
    role, x: mark.x, y: mark.y, heading: mark.heading,
    headYaw: 0, headPitch: 0, headRoll: 0, neckPitch: 0,
    bodyZ: 0, bodyRoll: 0, bodyPitch: 0, mouthOpen: 0,
    walkPhase: 0, resting: true,
    // Optional per docs/viewer.md's renderer contract, same as `resting` —
    // a rehearsal-neutral duck should render visibly subdued (dim body, no
    // trail/label) rather than simply standing still and fully lit, so a
    // renderer can tell "this duck isn't performing" from the pose alone.
    dimmed: true,
  };
}

/**
 * One role's full viewer pose at time t. `path` is a precomputeRolePath()
 * result. `opts.neutral` forces the rehearsal-neutral standing pose above
 * instead of sampling the role's own tracks — set it for a role
 * resolveNeutralRoles() says should be held right now.
 */
export function derivePose(show, role, t, path, opts = {}) {
  if (opts.neutral) return neutralPoseAt(role, (path && path.mark) || { x: 0, y: 0, heading: 0 });
  const sampler = createSampler(show, role);
  const s = sampler.at(t);
  const { x, y, heading, walkPhase, speed } = sampleRolePath(path, t);
  const head = s.head || { neck_pitch: 0, head_pitch: 0, head_yaw: 0, head_roll: 0 };
  const pose = s.pose || { z: 0, roll: 0, pitch: 0 };
  const mouth = s.mouth || { open: 0 };
  return {
    role,
    x, y, heading,
    headYaw: head.head_yaw, headPitch: head.head_pitch, headRoll: head.head_roll, neckPitch: head.neck_pitch,
    bodyZ: pose.z, bodyRoll: pose.roll, bodyPitch: pose.pitch,
    mouthOpen: mouth.open,
    walkPhase,
    // Local ground truth for "is this duck moving at time t", read straight
    // off the path's own dead reckoning rather than inferred from how far
    // walkPhase happens to have drifted between two arbitrary renderer
    // calls — a scrub across a walk segment can jump walkPhase by many
    // strides in one draw() even though the duck is at rest at both ends
    // (docs/viewer.md "Motion": settle to a neutral stand, never freeze
    // mid-stride). Optional for any pose producer that has no notion of
    // it (e.g. a future MuJoCo stream) — consumers fall back sensibly.
    resting: speed <= STOP_SPEED_EPS,
  };
}

/**
 * derivePose() for the whole cast. `paths` is a precomputeShowPaths()
 * result (or Map role->path). `opts.neutralRoles` (Set<string>, optional) —
 * see resolveNeutralRoles() — forces those roles to their rehearsal-neutral
 * pose instead of sampling.
 */
export function deriveShowPoses(show, t, paths, opts = {}) {
  const norm = normalizeShow(show);
  const neutralRoles = opts.neutralRoles || null;
  return norm.cast.map((member, i) => {
    const path = (paths && paths.get(member.role)) || singlePointPath(member.role, resolveMark(show, member.role, i, norm.cast.length));
    return derivePose(show, member.role, t, path, { neutral: Boolean(neutralRoles && neutralRoles.has(member.role)) });
  });
}

// ---------------------------------------------------------------------------
// Trails — dead-reckoned path so far, brightest at the current position.
// ---------------------------------------------------------------------------

export const TRAIL_MAX_POINTS = 240;

/**
 * Trail points from the path's start up to time t, downsampled to at most
 * `maxPoints`. brightness runs 0 (oldest/start) -> 1 (current position);
 * `boost` (the selected role) raises the floor so even the oldest point
 * still reads clearly.
 */
export function deriveTrail(path, t, maxPoints = TRAIL_MAX_POINTS, boost = false) {
  const n = path.t.length;
  if (n === 0) return [];
  let end;
  if (t >= path.t[n - 1]) end = n - 1;
  else if (t <= path.t[0]) end = 0;
  else {
    let lo = 0, hi = n - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (path.t[mid] <= t) lo = mid; else hi = mid - 1; }
    end = lo;
  }
  const count = end + 1;
  const step = Math.max(1, Math.ceil(count / maxPoints));
  const idx = [];
  for (let i = 0; i <= end; i += step) idx.push(i);
  if (idx[idx.length - 1] !== end) idx.push(end);
  const m = idx.length;
  return idx.map((i, k) => {
    const frac = m <= 1 ? 1 : k / (m - 1);
    return { x: path.x[i], y: path.y[i], brightness: boost ? 0.5 + 0.5 * frac : frac };
  });
}

// ---------------------------------------------------------------------------
// Event labels — skills/sounds have no pose, so they surface as a floating
// label for a short window around their fire time (docs/viewer.md §4/editor
// integration). `mode` events are not shown: a mode event switches drive
// mode ("walk"/"roller"), which has no visible pose of its own to label —
// it is not the same thing as installing a custom .onnx (that is a
// pre-show config step: pointing a fixed policy slot at a file, see
// docs/duckshow-format.md "Custom .onnx policies").
// ---------------------------------------------------------------------------

export const DEFAULT_EVENT_LABEL_WINDOW = { before: 0.1, after: 1.0 };

export function deriveEventLabels(show, t, window = DEFAULT_EVENT_LABEL_WINDOW) {
  const norm = normalizeShow(show);
  const before = window && typeof window.before === 'number' ? window.before : DEFAULT_EVENT_LABEL_WINDOW.before;
  const after = window && typeof window.after === 'number' ? window.after : DEFAULT_EVENT_LABEL_WINDOW.after;
  const labels = [];
  for (const member of norm.cast) {
    const role = member.role;
    for (const e of norm.tracksFor(role).events) {
      let text = null, kind = null;
      if (e.do !== null && e.do !== undefined) { text = e.do; kind = 'skill'; }
      else if (e.sound !== null && e.sound !== undefined) { text = e.sound; kind = 'sound'; }
      else continue; // mode event, or malformed (no action) — validator's problem, not ours
      if (t >= e.t - before && t <= e.t + after) labels.push({ role, text, t: e.t, kind });
    }
  }
  labels.sort((a, b) => a.t - b.t);
  return labels;
}

// ---------------------------------------------------------------------------
// Role colour palette — N hues walking the circle at even spacing, skipping
// the muddy yellow-green band. Derived from a role's index in the
// *alphabetically sorted* cast, so the mapping never depends on cast array
// order (stable under reordering) and is a pure function of the current
// role set (stable/well-defined however many roles are added).
// ---------------------------------------------------------------------------

export const PALETTE_SATURATION = 0.68;
// Lowered from 0.58 (tuned for the old dark-stage floor, where it read as
// pastel/washed-out) to a mid-to-deep tone that keeps contrast against the
// light neutral-grey floor (docs/viewer.md "Colour."). Saturation kept as-is.
export const PALETTE_LIGHTNESS = 0.4;
const HUE_SKIP_START = 55; // degrees — muddy yellow-green band, excluded
const HUE_SKIP_END = 100;
const HUE_USABLE_SPAN = 360 - (HUE_SKIP_END - HUE_SKIP_START);

/** Hue (degrees) for slot `index` of `total` evenly-spaced slots, skipping [55,100). */
export function roleHue(index, total) {
  if (!(total > 0)) return HUE_SKIP_END;
  const pos = ((index % total) / total) * HUE_USABLE_SPAN;
  return (HUE_SKIP_END + pos) % 360;
}

function hslToRgb(h, s, l) {
  if (s === 0) return [l, l, l];
  const hue2rgb = (p, q, tIn) => {
    let tt = tIn;
    if (tt < 0) tt += 1;
    if (tt > 1) tt -= 1;
    if (tt < 1 / 6) return p + (q - p) * 6 * tt;
    if (tt < 1 / 2) return q;
    if (tt < 2 / 3) return p + (q - p) * (2 / 3 - tt) * 6;
    return p;
  };
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  return [hue2rgb(p, q, h + 1 / 3), hue2rgb(p, q, h), hue2rgb(p, q, h - 1 / 3)];
}

function toHex2(v) { return Math.round(clamp(v, 0, 1) * 255).toString(16).padStart(2, '0'); }
function rgbToHex(rgb) { return `#${toHex2(rgb[0])}${toHex2(rgb[1])}${toHex2(rgb[2])}`; }

/**
 * If `prevRoles` and `newRoles` are the same length and differ at exactly
 * one index, that's an in-place rename (`{from, to}` at that index) —
 * exactly the shape `core.renameRole` produces. Any other change (add,
 * remove, reorder, more than one differing slot) returns null. Used by
 * `roleColorPaletteContinuous` below to carry colours forward across a
 * rename; see its doc comment.
 */
export function singleRenameAt(prevRoles, newRoles) {
  if (!Array.isArray(prevRoles) || !Array.isArray(newRoles) || prevRoles.length !== newRoles.length) return null;
  let from = null, to = null, diffCount = 0;
  for (let i = 0; i < prevRoles.length; i++) {
    if (prevRoles[i] !== newRoles[i]) {
      diffCount++;
      if (diffCount > 1) return null;
      from = prevRoles[i];
      to = newRoles[i];
    }
  }
  return diffCount === 1 ? { from, to } : null;
}

/**
 * role -> {role,hue,saturation,lightness,rgb:[r,g,b] (0..1),hex} for every
 * name in `roleNames`. Deterministic per role, order-independent.
 */
export function roleColorPalette(roleNames, opts = {}) {
  const s = opts.saturation ?? PALETTE_SATURATION;
  const l = opts.lightness ?? PALETTE_LIGHTNESS;
  const sorted = Array.from(new Set(roleNames)).sort();
  const n = sorted.length;
  const palette = new Map();
  sorted.forEach((role, i) => {
    const hue = roleHue(i, n);
    const rgb = hslToRgb(hue / 360, s, l);
    palette.set(role, { role, hue, saturation: s, lightness: l, rgb, hex: rgbToHex(rgb) });
  });
  return palette;
}

/**
 * `roleColorPalette` for `roleNames`, but reusing `prevPalette` (that same
 * function's result from the previous call, keyed by `prevRoleNames`)
 * instead of recomputing from scratch when exactly one role was renamed in
 * place (`singleRenameAt` above) — every unaffected role keeps its exact
 * previous colour object, and the renamed role carries its own previous
 * colour forward under the new name. Any other change (add, remove,
 * reorder, more than one rename at once, or no usable previous palette)
 * falls back to a full, fresh `roleColorPalette(roleNames)`.
 *
 * Why this needs to exist at all: `roleColorPalette` is deliberately a
 * pure function of the *set* of role names, sorted alphabetically — that
 * is what makes it survive a cast *reorder* untouched (see its own tests).
 * The flip side is that a *rename* changes the name set, which can shift
 * that alphabetical rank — and so the hue — for every OTHER role too. A
 * live authoring session (`StageViewer`, and `duckshow-editor.html`'s own
 * hand-rolled equivalent) calls this on every edit instead, so "rename one
 * duck" never means "half the flock changes colour" mid-show-design.
 * `roleColorPalette` itself is left exactly as-is: this wraps it rather
 * than changing its sort, so the "stable under cast reorder" contract
 * those tests pin down stays intact for every other caller.
 */
export function roleColorPaletteContinuous(prevRoleNames, prevPalette, roleNames) {
  if (Array.isArray(prevRoleNames) && prevPalette && prevPalette.size) {
    const renamed = singleRenameAt(prevRoleNames, roleNames);
    if (renamed) {
      const fromColor = prevPalette.get(renamed.from);
      if (fromColor) {
        const palette = new Map();
        let ok = true;
        for (const role of roleNames) {
          if (role === renamed.to) {
            palette.set(role, { ...fromColor, role });
          } else {
            const existing = prevPalette.get(role);
            if (!existing) { ok = false; break; }
            palette.set(role, existing);
          }
        }
        if (ok) return palette;
      }
    }
  }
  return roleColorPalette(roleNames);
}

// ---------------------------------------------------------------------------
// Camera — orbit presets as spherical coordinates around a fixed stage
// target, eased (not cut) between them. No matrix maths lives here: the
// renderer turns {eye,target,up,fovY} into view/projection matrices itself.
// ---------------------------------------------------------------------------

export const CAMERA_PRESET_NAMES = Object.freeze(['house', 'threeQuarter', 'top']);
export const CAMERA_EASE_DURATION = 0.5; // seconds — docs/viewer.md "Camera"
const STAGE_TARGET = [0, 0, 0.15]; // roughly duck-centre height

// azimuth: around world +z, 0 = viewed from +x. elevation: angle above the
// target's horizontal plane (90° = straight down). radius: metres from target.
const CAMERA_PRESET_DEFS = {
  house: { azimuth: deg(0), elevation: deg(18), radius: 3.6, fovY: deg(42) }, // audience: eye height, front & centre, slight downward tilt
  threeQuarter: { azimuth: deg(50), elevation: deg(32), radius: 3.2, fovY: deg(46) }, // raised, off to one side
  top: { azimuth: deg(0), elevation: deg(89.99), radius: 4.2, fovY: deg(36) }, // straight down: formations and marks
};

/** The named preset's camera state (a plain, immutable-by-convention object). */
export function cameraPresetState(name, target = STAGE_TARGET) {
  const def = CAMERA_PRESET_DEFS[name];
  if (!def) throw new RangeError(`unknown camera preset ${JSON.stringify(name)}; expected one of ${CAMERA_PRESET_NAMES.join(', ')}`);
  return { azimuth: def.azimuth, elevation: def.elevation, radius: def.radius, fovY: def.fovY, target: target.slice() };
}

/** easeInOutCubic: 0 and 1 exact, strictly increasing, non-linear in between. */
export function easeInOutCubic(t) {
  const x = clamp(t, 0, 1);
  return x < 0.5 ? 4 * x * x * x : 1 - ((-2 * x + 2) ** 3) / 2;
}

function blendAngle(a, b, t) {
  const twoPi = Math.PI * 2;
  const d = (((b - a + Math.PI) % twoPi) + twoPi) % twoPi - Math.PI;
  return a + d * t;
}

/** Linear blend of two camera states (t in [0,1], already eased by the caller if wanted). */
export function blendCameraStates(a, b, t) {
  const tt = clamp(t, 0, 1);
  return {
    azimuth: blendAngle(a.azimuth, b.azimuth, tt),
    elevation: lerp(a.elevation, b.elevation, tt),
    radius: lerp(a.radius, b.radius, tt),
    fovY: lerp(a.fovY, b.fovY, tt),
    target: [lerp(a.target[0], b.target[0], tt), lerp(a.target[1], b.target[1], tt), lerp(a.target[2], b.target[2], tt)],
  };
}

/** blendCameraStates() through easeInOutCubic — what a preset switch animates along. */
export function easeCamera(a, b, t) {
  return blendCameraStates(a, b, easeInOutCubic(t));
}

/** Eye position for a camera state. */
export function cameraStateToEye(camera) {
  const { azimuth, elevation, radius, target } = camera;
  const ce = Math.cos(elevation), se = Math.sin(elevation);
  return [
    target[0] + radius * ce * Math.cos(azimuth),
    target[1] + radius * ce * Math.sin(azimuth),
    target[2] + radius * se,
  ];
}

/** Up vector for a camera state — swaps to an azimuth-tracking vector near the poles to avoid the lookAt singularity. */
export function cameraStateToUp(camera) {
  if (Math.abs(Math.cos(camera.elevation)) < 1e-2) {
    return [-Math.cos(camera.azimuth), -Math.sin(camera.azimuth), 0];
  }
  return [0, 0, 1];
}

// ---------------------------------------------------------------------------
// Frame assembly — everything a renderer needs for one paint, minus camera
// (which StageViewer owns because it also needs it for orbit/mark-drag
// raycasting; deriveFrame() stays camera-agnostic and pure).
// ---------------------------------------------------------------------------

export function deriveFrame(show, t, paths, opts = {}) {
  const neutralRoles = opts.neutralRoles || null;
  const poses = deriveShowPoses(show, t, paths, { neutralRoles });
  // A rehearsal-neutral role (docs/authoring.md rehearsal tools) is dimmed
  // in `poses` above and gets no trail or event label at all here — it
  // isn't performing right now, so there is nothing in motion to trace or
  // announce; an empty trail array reads as "no trail" to every renderer
  // the same way a role with no locomotion track already does.
  const trails = new Map();
  for (const [role, path] of paths) {
    trails.set(role, neutralRoles && neutralRoles.has(role) ? [] : deriveTrail(path, t, opts.trailMaxPoints, role === opts.selectedRole));
  }
  const labels = deriveEventLabels(show, t, opts.eventLabelWindow)
    .filter((label) => !(neutralRoles && neutralRoles.has(label.role)));
  return { t, poses, trails, labels, selectedRole: opts.selectedRole || null };
}

// ---------------------------------------------------------------------------
// Camera-ray helpers for canvas input (orbit hit-testing a mark, dragging it
// across the floor plane). Small and local — not a matrix library.
// ---------------------------------------------------------------------------

function cameraBasis(camera) {
  const eye = cameraStateToEye(camera);
  const forward = vNorm(vSub(camera.target, eye));
  const worldUp = cameraStateToUp(camera);
  const right = vNorm(vCross(forward, worldUp));
  const up = vCross(right, forward);
  return { eye, forward, right, up };
}

/** World-space ray through a canvas-normalized point (ndcX/ndcY in [-1,1]). */
function screenToRay(camera, ndcX, ndcY, aspect) {
  const { eye, forward, right, up } = cameraBasis(camera);
  const tanHalf = Math.tan(camera.fovY / 2);
  const dir = vNorm(vAdd(vAdd(forward, vScale(right, ndcX * tanHalf * aspect)), vScale(up, ndcY * tanHalf)));
  return { origin: eye, dir };
}

/** Where a ray crosses the world floor (z = planeZ), or null if it points away/parallel. */
function rayPlaneZ(ray, planeZ = 0) {
  const denom = ray.dir[2];
  if (Math.abs(denom) < 1e-9) return null;
  const tHit = (planeZ - ray.origin[2]) / denom;
  if (tHit < 0) return null;
  return [ray.origin[0] + ray.dir[0] * tHit, ray.origin[1] + ray.dir[1] * tHit, planeZ];
}

// ---------------------------------------------------------------------------
// StageViewer — the controller the editor drives.
// ---------------------------------------------------------------------------

const MIN_ELEVATION = deg(-5);
const MAX_ELEVATION = deg(89.99);
const MIN_RADIUS = 1.2;
const MAX_RADIUS = 10;
const ORBIT_SPEED = 0.008; // radians per pixel of drag
const DOLLY_SPEED = 0.0015; // exponential radius change per wheel-delta unit
const MARK_HIT_RADIUS = 0.14; // metres — pick radius for grabbing a start mark in Top view

function defaultClock() {
  return typeof performance !== 'undefined' ? performance.now() : Date.now();
}

export class StageViewer {
  /**
   * @param {*} canvas   the editor's canvas element, or null/undefined for
   *                     headless/logic-only use (no input wiring, renders
   *                     synchronously instead of batching via rAF).
   * @param {*} renderer see the "Renderer contract" comment at the top of
   *                     this file. Only `render` is required; everything
   *                     else is called if present.
   * @param {object} [options]
   * @param {number} [options.dt]                 dead-reckoning grid step (s)
   * @param {() => number} [options.now]           wall-clock source (ms); for deterministic tests
   * @param {number} [options.trailMaxPoints]
   * @param {{before:number,after:number}} [options.eventLabelWindow]
   * @param {(role:string, mark:{x,y,heading}) => void} [options.onMarkChange]
   *        called while a start mark is being dragged in Top view, so the
   *        editor can persist it under show.editor.marks.
   * @param {string} [options.initialCameraPreset] default 'house'
   */
  constructor(canvas, renderer, options = {}) {
    this.canvas = canvas || null;
    this.renderer = renderer || {};
    this._dt = options.dt || DEFAULT_DT;
    this._now = typeof options.now === 'function' ? options.now : defaultClock;
    this._trailMaxPoints = options.trailMaxPoints || TRAIL_MAX_POINTS;
    this._eventWindow = options.eventLabelWindow || DEFAULT_EVENT_LABEL_WINDOW;
    this._onMarkChange = typeof options.onMarkChange === 'function' ? options.onMarkChange : null;

    this._show = null;
    this._paths = new Map();
    this._marks = new Map();
    this._palette = new Map();
    this._time = 0;
    this._selectedRole = null;

    this._presetName = options.initialCameraPreset || 'house';
    this._camera = cameraPresetState(this._presetName);
    this._transition = null;

    this._dragging = null;
    this._dragStartMark = null;
    this._rafHandle = null;
    this._loopHandle = null;
    this._renderDirty = false;
    this._lastAspect = 16 / 9;

    this._attachInput();
  }

  // -- state -----------------------------------------------------------

  /** Load a show: precomputes every role's dead-reckoned path and palette. Call once per edit. */
  setShow(show) {
    const prevNorm = this._show ? normalizeShow(this._show) : null;
    this._show = show;
    const norm = normalizeShow(show);
    this._marks = resolveMarks(show);
    this._paths = new Map();
    for (const member of norm.cast) {
      this._paths.set(member.role, precomputeRolePath(show, member.role, this._marks.get(member.role), this._dt));
    }
    // roleColorPaletteContinuous (see its own doc comment): carries colours
    // forward across an in-place rename instead of letting a rename's
    // change to the alphabetically-sorted name set reshuffle every OTHER
    // role's hue too — a real risk of roleColorPalette's own, deliberate,
    // order-independent design (nasty mid-show-design surprise otherwise).
    this._palette = roleColorPaletteContinuous(prevNorm ? prevNorm.roleNames() : null, this._palette, norm.roleNames());
    if (typeof this.renderer.setPalette === 'function') this.renderer.setPalette(this._palette);
    this._scheduleRender();
  }

  setTime(t) {
    this._time = t;
    this._scheduleRender();
  }

  setSelectedRole(role) {
    this._selectedRole = role || null;
    this._scheduleRender();
  }

  getShow() { return this._show; }
  getTime() { return this._time; }
  getSelectedRole() { return this._selectedRole; }
  getPalette() { return this._palette; }
  getMarks() { return this._marks; }
  getCameraState() { return this._camera; }
  getCameraPreset() { return this._presetName; }

  // -- camera ------------------------------------------------------------

  /** Switch to a named preset ('house' | 'threeQuarter' | 'top'), easing over ~0.5 s. */
  setCameraPreset(name) {
    if (!CAMERA_PRESET_NAMES.includes(name)) {
      throw new RangeError(`unknown camera preset ${JSON.stringify(name)}; expected one of ${CAMERA_PRESET_NAMES.join(', ')}`);
    }
    this._presetName = name;
    this._transition = { from: this._camera, to: cameraPresetState(name), start: this._now(), duration: CAMERA_EASE_DURATION };
    this._doRender(); // paint the still-current (t=0) state immediately
    this._ensureTransitionLoop();
  }

  /**
   * Advance an in-progress preset transition to wall-clock `nowMs`. Returns
   * whether the transition is still running. The class drives this itself
   * via requestAnimationFrame when available; exposed directly so headless
   * callers (tests, or a host driving its own loop) can step it exactly.
   */
  tickCamera(nowMs) {
    if (!this._transition) return false;
    const { from, to, start, duration } = this._transition;
    const rawT = duration > 0 ? clamp((nowMs - start) / (duration * 1000), 0, 1) : 1;
    this._camera = easeCamera(from, to, rawT);
    if (rawT >= 1) { this._transition = null; return false; }
    return true;
  }

  _ensureTransitionLoop() {
    if (this._loopHandle != null || typeof requestAnimationFrame !== 'function') return; // headless: caller drives tickCamera()
    const step = () => {
      this._loopHandle = null;
      const stillGoing = this.tickCamera(this._now());
      this._doRender();
      if (stillGoing) this._ensureTransitionLoop();
    };
    this._loopHandle = requestAnimationFrame(step);
  }

  // -- rendering ---------------------------------------------------------

  resize(width, height) {
    if (this.canvas) {
      if (typeof width === 'number') this.canvas.width = width;
      if (typeof height === 'number') this.canvas.height = height;
    }
    if (typeof this.renderer.resize === 'function') {
      this.renderer.resize(width, height, typeof devicePixelRatio !== 'undefined' ? devicePixelRatio : 1);
    }
    this._scheduleRender();
  }

  _scheduleRender() {
    this._renderDirty = true;
    if (this._rafHandle != null) return;
    if (typeof requestAnimationFrame === 'function' && this.canvas) {
      this._rafHandle = requestAnimationFrame(() => {
        this._rafHandle = null;
        if (this._renderDirty) this._doRender();
      });
    } else {
      this._doRender();
    }
  }

  _cameraFrame() {
    const eye = cameraStateToEye(this._camera);
    const up = cameraStateToUp(this._camera);
    const aspect = this.canvas && this.canvas.clientWidth && this.canvas.clientHeight
      ? this.canvas.clientWidth / this.canvas.clientHeight
      : this._lastAspect;
    this._lastAspect = aspect;
    return { eye, target: this._camera.target.slice(), up, fovY: this._camera.fovY, aspect };
  }

  _doRender() {
    this._renderDirty = false;
    if (!this._show || typeof this.renderer.render !== 'function') return;
    const frame = deriveFrame(this._show, this._time, this._paths, {
      selectedRole: this._selectedRole,
      trailMaxPoints: this._trailMaxPoints,
      eventLabelWindow: this._eventWindow,
    });
    frame.camera = this._cameraFrame();
    frame.marks = this._marks;
    this.renderer.render(frame);
  }

  // -- input: orbit-drag, scroll-dolly, mark-drag (Top view only) --------

  _attachInput() {
    const c = this.canvas;
    if (!c || typeof c.addEventListener !== 'function') return;
    this._onPointerDown = (ev) => this._handlePointerDown(ev);
    this._onPointerMove = (ev) => this._handlePointerMove(ev);
    this._onPointerUp = (ev) => this._handlePointerUp(ev);
    this._onWheel = (ev) => this._handleWheel(ev);
    c.addEventListener('pointerdown', this._onPointerDown);
    c.addEventListener('pointermove', this._onPointerMove);
    c.addEventListener('pointerup', this._onPointerUp);
    c.addEventListener('pointercancel', this._onPointerUp);
    c.addEventListener('wheel', this._onWheel, { passive: false });
  }

  _detachInput() {
    const c = this.canvas;
    if (!c || typeof c.removeEventListener !== 'function') return;
    c.removeEventListener('pointerdown', this._onPointerDown);
    c.removeEventListener('pointermove', this._onPointerMove);
    c.removeEventListener('pointerup', this._onPointerUp);
    c.removeEventListener('pointercancel', this._onPointerUp);
    c.removeEventListener('wheel', this._onWheel);
  }

  _pointerNdc(ev) {
    const rect = this.canvas.getBoundingClientRect ? this.canvas.getBoundingClientRect() : { left: 0, top: 0, width: this.canvas.clientWidth, height: this.canvas.clientHeight };
    const w = rect.width || this.canvas.clientWidth || 1;
    const h = rect.height || this.canvas.clientHeight || 1;
    return { ndcX: ((ev.clientX - rect.left) / w) * 2 - 1, ndcY: 1 - ((ev.clientY - rect.top) / h) * 2, aspect: w / h };
  }

  _hitTestMark(ev) {
    const { ndcX, ndcY, aspect } = this._pointerNdc(ev);
    const hit = rayPlaneZ(screenToRay(this._camera, ndcX, ndcY, aspect), 0);
    if (!hit) return null;
    let best = null, bestDist = MARK_HIT_RADIUS;
    for (const [role, mark] of this._marks) {
      const d = Math.hypot(mark.x - hit[0], mark.y - hit[1]);
      if (d < bestDist) { bestDist = d; best = role; }
    }
    return best;
  }

  _dragMark(ev) {
    const { ndcX, ndcY, aspect } = this._pointerNdc(ev);
    const hit = rayPlaneZ(screenToRay(this._camera, ndcX, ndcY, aspect), 0);
    if (!hit) return;
    const role = this._dragging.role;
    const base = this._dragStartMark;
    let mark;
    if (ev.altKey || ev.shiftKey) {
      mark = { x: base.x, y: base.y, heading: round(Math.atan2(hit[1] - base.y, hit[0] - base.x), 4) };
    } else {
      mark = { x: round(hit[0], 3), y: round(hit[1], 3), heading: base.heading };
    }
    this._marks.set(role, mark);
    this._paths.set(role, precomputeRolePath(this._show, role, mark, this._dt));
    if (this._onMarkChange) this._onMarkChange(role, mark);
    this._scheduleRender();
  }

  _handlePointerDown(ev) {
    this._transition = null;
    if (this._presetName === 'top' && this._show) {
      const hit = this._hitTestMark(ev);
      if (hit) {
        this._dragging = { type: 'mark', role: hit, pointerId: ev.pointerId };
        this._dragStartMark = { ...this._marks.get(hit) };
        if (this.canvas.setPointerCapture) { try { this.canvas.setPointerCapture(ev.pointerId); } catch { /* unsupported */ } }
        this._dragMark(ev);
        return;
      }
    }
    this._dragging = { type: 'orbit', pointerId: ev.pointerId, lastX: ev.clientX, lastY: ev.clientY };
    if (this.canvas.setPointerCapture) { try { this.canvas.setPointerCapture(ev.pointerId); } catch { /* unsupported */ } }
  }

  _handlePointerMove(ev) {
    if (!this._dragging) return;
    if (this._dragging.type === 'mark') { this._dragMark(ev); return; }
    const dx = ev.clientX - this._dragging.lastX;
    const dy = ev.clientY - this._dragging.lastY;
    this._dragging.lastX = ev.clientX; this._dragging.lastY = ev.clientY;
    this._camera = {
      ...this._camera,
      azimuth: this._camera.azimuth - dx * ORBIT_SPEED,
      elevation: clamp(this._camera.elevation + dy * ORBIT_SPEED, MIN_ELEVATION, MAX_ELEVATION),
    };
    this._presetName = null; // manual orbit no longer matches a named preset
    this._scheduleRender();
  }

  _handlePointerUp(ev) {
    if (this._dragging && this.canvas.releasePointerCapture) { try { this.canvas.releasePointerCapture(ev.pointerId); } catch { /* unsupported */ } }
    this._dragging = null;
    this._dragStartMark = null;
  }

  _handleWheel(ev) {
    if (ev.preventDefault) ev.preventDefault();
    const factor = Math.exp(ev.deltaY * DOLLY_SPEED);
    this._camera = { ...this._camera, radius: clamp(this._camera.radius * factor, MIN_RADIUS, MAX_RADIUS) };
    this._scheduleRender();
  }

  // -- lifecycle -----------------------------------------------------------

  dispose() {
    this._detachInput();
    if (this._rafHandle != null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this._rafHandle);
    if (this._loopHandle != null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this._loopHandle);
    this._rafHandle = null; this._loopHandle = null; this._transition = null;
    if (typeof this.renderer.dispose === 'function') this.renderer.dispose();
  }
}

export default StageViewer;
