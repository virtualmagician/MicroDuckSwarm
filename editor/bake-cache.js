// bake-cache.js — pure parsing/validation/sampling for a duckbake/1 pose
// cache (docs/bake-format.md, produced by tools/bake — Feature A of
// docs/viewer.md "Create Preview (baked physics)"). No DOM, no GL: this
// module turns cache JSON + a show time into the exact same
// {role,x,y,heading,headYaw,headPitch,headRoll,neckPitch,bodyZ,bodyRoll,
// bodyPitch,mouthOpen,walkPhase} pose shape duckshow-viewer.js's
// deriveShowPoses() produces for the kinematic path (docs/viewer.md "Pose
// in, pixels out") — so the editor's renderer needs no changes to play
// either stream (see viewer-gl.js's StageRenderer.draw(poses)).
//
// The cache IS recorded data (docs/bake-format.md: "poses[role] carries
// exactly the field names ... renderer contract uses ... Numeric arrays,
// not per-frame objects"), so there is no dead reckoning to redo here —
// only array indexing plus a light linear interpolation between adjacent
// recorded frames so scrubbing stays smooth between the cache's 50 Hz
// samples, mirroring duckshow-viewer.js's own sampleRolePath().

export const BAKE_FORMAT = 'duckbake/1';

const POSE_FIELDS = [
  'x', 'y', 'heading', 'headYaw', 'headPitch', 'headRoll', 'neckPitch',
  'bodyZ', 'bodyRoll', 'bodyPitch', 'mouthOpen', 'walkPhase',
];

function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
function lerp(a, b, t) { return a + (b - a) * t; }

/** True if `obj` at least declares itself a duckbake/1 document — the cheap check before validateBakeCache()'s thorough one. */
export function isBakeCache(obj) {
  return Boolean(obj) && typeof obj === 'object' && obj.format === BAKE_FORMAT;
}

/**
 * Full structural validation. Throws a plain, human-readable Error
 * describing the first problem found; returns `cache` unchanged when it
 * passes. Deliberately permissive about anything docs/bake-format.md
 * doesn't require for playback (log entries, engine info, policy
 * hashes) — unknown/extra fields are ignored, matching this project's
 * show-file-compatibility convention (CLAUDE.md #4) rather than
 * duplicating it, since a pose cache is the same kind of "unknown fields
 * ignored" JSON document.
 */
export function validateBakeCache(cache) {
  if (!cache || typeof cache !== 'object') throw new Error('bake cache: not a JSON object');
  if (cache.format !== BAKE_FORMAT) {
    throw new Error(`bake cache: unrecognised format ${JSON.stringify(cache.format)} (expected "${BAKE_FORMAT}")`);
  }
  if (!cache.show || typeof cache.show.sha256 !== 'string' || !cache.show.sha256) {
    throw new Error('bake cache: missing show.sha256');
  }
  if (typeof cache.frame_rate !== 'number' || !(cache.frame_rate > 0)) {
    throw new Error('bake cache: missing or invalid frame_rate');
  }
  if (!Array.isArray(cache.roles) || cache.roles.length === 0) {
    throw new Error('bake cache: missing or empty roles[]');
  }
  if (!cache.poses || typeof cache.poses !== 'object') {
    throw new Error('bake cache: missing poses{}');
  }
  for (const role of cache.roles) {
    const p = cache.poses[role];
    if (!p || typeof p !== 'object') throw new Error(`bake cache: poses.${role} missing`);
    for (const field of POSE_FIELDS) {
      const arr = p[field];
      if (!arr || typeof arr.length !== 'number') throw new Error(`bake cache: poses.${role}.${field} missing`);
    }
    const n = p.x.length;
    for (const field of POSE_FIELDS) {
      if (p[field].length !== n) throw new Error(`bake cache: poses.${role}.${field} length (${p[field].length}) does not match poses.${role}.x (${n})`);
    }
  }
  return cache;
}

/** sha256 (lowercase hex) of `text`, via the standard Web Crypto API — available in every modern browser and in Node >= 19, no dependency. */
export async function sha256Hex(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * Compare a cache's recorded show hash against the raw text of the show
 * currently open (must be the exact bytes last loaded from disk — an
 * edited-since-load document cannot be byte-verified, see
 * duckshow-editor.html's own showText tracking). Never throws for a
 * mismatch — that is the expected, reportable outcome the caller must
 * handle plainly (docs/viewer.md Feature A: "say so plainly rather than
 * replaying something misleading"), not an exceptional one.
 */
export async function checkShowHashMatch(cache, showText) {
  const actual = await sha256Hex(showText);
  return { ok: actual === cache.show.sha256, expected: cache.show.sha256, actual };
}

/** Number of recorded frames for `role` (all fields share one length, enforced by validateBakeCache). */
export function frameCountFor(cache, role) {
  const p = cache.poses[role];
  return p ? p.x.length : 0;
}

/**
 * One role's pose at show time `t`, linearly interpolated between the two
 * recorded frames bracketing it (held flat at both ends) — smooth
 * scrubbing at any zoom without re-simulating anything, since every value
 * here is already-recorded physics output. Returns null for a role the
 * cache never recorded (e.g. a role added to the show after baking; the
 * hash check above is what should have already refused that mismatch).
 */
export function poseAtTime(cache, role, t) {
  const p = cache.poses[role];
  if (!p) return null;
  const n = p.x.length;
  if (n === 0) return null;
  const raw = t * cache.frame_rate;
  const i0 = clamp(Math.floor(raw), 0, n - 1);
  const i1 = clamp(i0 + 1, 0, n - 1);
  const frac = i1 > i0 ? clamp(raw - i0, 0, 1) : 0;
  const pose = { role };
  for (const field of POSE_FIELDS) pose[field] = lerp(p[field][i0], p[field][i1], frac);
  return pose;
}

/** poseAtTime() for every role the cache recorded — a drop-in replacement for duckshow-viewer.js's deriveShowPoses() output shape. */
export function poseAllAtTime(cache, t) {
  return cache.roles.map((role) => poseAtTime(cache, role, t)).filter(Boolean);
}

export const BAKE_TRAIL_MAX_POINTS = 240;

/**
 * Trail points (x/y/brightness, brightest = current position) from the
 * start of the recording up to time t, downsampled to at most
 * `maxPoints` — the same shape and brightness convention as
 * duckshow-viewer.js's deriveTrail(), read from the cache's own recorded
 * x/y rather than a dead-reckoned path.
 */
export function bakeTrail(cache, role, t, maxPoints = BAKE_TRAIL_MAX_POINTS, boost = false) {
  const p = cache.poses[role];
  if (!p) return [];
  const n = p.x.length;
  if (n === 0) return [];
  const end = clamp(Math.round(t * cache.frame_rate), 0, n - 1);
  const count = end + 1;
  const step = Math.max(1, Math.ceil(count / maxPoints));
  const idx = [];
  for (let i = 0; i <= end; i += step) idx.push(i);
  if (idx[idx.length - 1] !== end) idx.push(end);
  const m = idx.length;
  return idx.map((i, k) => {
    const frac = m <= 1 ? 1 : k / (m - 1);
    return { x: p.x[i], y: p.y[i], brightness: boost ? 0.5 + 0.5 * frac : frac };
  });
}

/** Roles the baker declined to simulate (roller mode) or that fell during the bake — informational, surfaced in the status line when a cache loads. */
export function summarize(cache) {
  return {
    roles: cache.roles.length,
    duration: cache.show ? cache.show.duration : null,
    unsimulatedRoles: Array.isArray(cache.unsimulated_roles) ? cache.unsimulated_roles : [],
    fallenRoles: Array.isArray(cache.fallen_roles) ? cache.fallen_roles : [],
  };
}
