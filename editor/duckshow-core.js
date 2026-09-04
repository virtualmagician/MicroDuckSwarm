// duckshow-core.js — pure ES module, no DOM, no dependencies.
//
// The third implementation of the .duckshow/1 contract (docs/duckshow-format.md),
// alongside python/duckshow (canonical) and SwarmLink/Sources/SwarmLink/DuckShow.swift.
//
//   parse / serialize   unknown JSON fields survive a round-trip untouched: the
//                       "show" object handled by every function here IS the
//                       parsed JSON document, edited by pure functions.
//   sampler             identical semantics to python/duckshow/sampler.py.
//   validator           identical rules, limits, issue order and message text to
//                       python/duckshow/validator.py + limits.py (checked
//                       against shows/fixtures/*.duckshow.json + expected.json).
//   beat grid           bpm / beat_offset -> beat times, snap.
//   dead reckoning      integrate(show, role) -> [{t, x, y, heading}] from
//                       trunk-frame velocities (x forward, y left, heading CCW).
//   edit operations     pure functions returning new show objects.
//
// Everything is plain data; nothing here touches the DOM, timers or I/O.

// ---------------------------------------------------------------------------
// Constants (mirrors python/duckshow/model.py + limits.py)
// ---------------------------------------------------------------------------

export const FORMAT = 'duckshow/1';
export const SUPPORTED_FORMAT_MAJOR = 1;

export const INTERP_STEP = 'step';
export const INTERP_LINEAR = 'linear';
export const INTERP_SMOOTH = 'smooth';
export const VALID_INTERPS = Object.freeze([INTERP_STEP, INTERP_LINEAR, INTERP_SMOOTH]);
export const DEFAULT_INTERP = INTERP_LINEAR;

export const SKILLS = Object.freeze(['ground_pick', 'kick_left', 'kick_right', 'sit_toggle', 'roulade']);
export const SOUND_TAGS = Object.freeze(['alarm', 'greet', 'inquire', 'peck', 'chirp', 'coo', 'wheee']);

// The only two drive-mode strings real robotd accepts over the wire
// (docs/robotd-api.md "Custom .onnx policies & modes"). There is no
// mechanism to register a custom-named mode -- a custom-trained gait is
// installed by pointing a fixed policy *slot* at a different .onnx file
// (requires.policies[].slot), never by inventing a new mode string. A
// `mode` event's value must be one of these two. Mirrors
// python/duckshow/limits.py's DRIVE_MODES.
export const DRIVE_MODES = Object.freeze(['walk', 'roller']);

// Per-skill occupancy durations (seconds), sourced from
// assets/microduck/policies/manifest.json (schema_version 2, control_hz 50;
// see docs/duckshow-format.md "Skill durations and occupancy" for the full
// authoring mapping table). Each of these `do` skills is an *episodic*
// policy clip: once started, it runs to completion, so a discrete event
// scheduled inside that window is scheduling against a duck that
// physically cannot have finished the first skill yet (see
// checkSkillOccupancyOverlap below). Mirrors python/duckshow/limits.py's
// SKILL_DURATIONS_S.
//
// `sit_toggle` (alpha_sitstand.onnx) is deliberately absent: the manifest
// marks it "kind": "scripted", not "episodic", and gives it a
// ramp_s/unwind_s posture transition rather than a fixed duration_s --
// docs/bake-format.md records that the hand-off semantics for a second
// sit_toggle mid-ramp are unverified. There is no confirmed number to warn
// against, so sit_toggle never occupies for the purposes of this check,
// neither as the earlier (occupying) skill nor the later (interrupting)
// one.
export const SKILL_DURATIONS_S = Object.freeze({
  ground_pick: 2.8, // alpha_ground_pick.onnx, walk-mode duration
  roulade: 1.0, // roulade.onnx
  kick_left: 0.5, // ball_kick_left.onnx
  kick_right: 0.5, // ball_kick_right.onnx
});

// ground_pick's occupancy in roller mode: the robot runs roller_crouch.onnx
// instead of alpha_ground_pick.onnx (docs/duckshow-format.md's authoring
// mapping table names roller_crouch as "the roller-mode variant of ground
// pick", never itself authored directly by a `do` event) -- a longer clip,
// not just a renamed one. Mirrors python/duckshow/limits.py's
// GROUND_PICK_ROLLER_DURATION_S.
export const GROUND_PICK_ROLLER_DURATION_S = 3.5;

// Skills whose manifest.json entry is "chain": true -- a repeat of one of
// these immediately after itself is the documented way to keep the effect
// going, not an authoring mistake, so the occupancy-overlap check below
// must never warn about that specific pairing. Mirrors
// python/duckshow/limits.py's CHAINING_SKILLS.
export const CHAINING_SKILLS = Object.freeze(['roulade']);

/**
 * Occupancy duration (seconds) for a `do` skill event, given the drive mode
 * active when it starts (a Sampler.modeAt() result: 'walk'/'roller'/null).
 * null when no confirmed duration exists (currently only sit_toggle).
 * Mirrors python/duckshow/limits.py's skill_duration_s.
 */
export function skillDurationS(skill, mode) {
  if (skill === 'ground_pick' && mode === 'roller') return GROUND_PICK_ROLLER_DURATION_S;
  return Object.prototype.hasOwnProperty.call(SKILL_DURATIONS_S, skill) ? SKILL_DURATIONS_S[skill] : null;
}

// Display-only metadata for the five `do` skills (the editor's event
// inspector and timeline occupancy bars) -- not consulted by validate();
// SKILL_DURATIONS_S / GROUND_PICK_ROLLER_DURATION_S above are the numbers
// that actually drive the validator warning.
export const SKILL_KIND = Object.freeze({
  ground_pick: 'episodic',
  roulade: 'episodic',
  kick_left: 'episodic',
  kick_right: 'episodic',
  sit_toggle: 'scripted',
});
export const SKILL_POLICY_FILE = Object.freeze({
  ground_pick: 'alpha_ground_pick.onnx',
  roulade: 'roulade.onnx',
  kick_left: 'ball_kick_left.onnx',
  kick_right: 'ball_kick_right.onnx',
  sit_toggle: 'alpha_sitstand.onnx',
});
export const GROUND_PICK_ROLLER_POLICY_FILE = 'roller_crouch.onnx';

/** Display metadata for one skill event: {kind, policy, duration_s}. */
export function skillInfo(skill, mode) {
  const rollerish = skill === 'ground_pick' && mode === 'roller';
  return {
    kind: Object.prototype.hasOwnProperty.call(SKILL_KIND, skill) ? SKILL_KIND[skill] : null,
    policy: rollerish ? GROUND_PICK_ROLLER_POLICY_FILE
      : (Object.prototype.hasOwnProperty.call(SKILL_POLICY_FILE, skill) ? SKILL_POLICY_FILE[skill] : null),
    duration_s: skillDurationS(skill, mode),
  };
}

export const CURVE_TRACKS = Object.freeze(['locomotion', 'head', 'pose', 'mouth']);
export const ALL_TRACKS = Object.freeze(['locomotion', 'head', 'pose', 'mouth', 'events', 'servo']);

// Same field names as python/duckshow/limits.py so the two tables can be diffed.
export const DEFAULT_LIMITS = Object.freeze({
  max_abs_vx: 0.25,
  max_abs_vy: 0.20,
  max_abs_vyaw: 1.5,
  max_abs_head_angle: 1.2,
  max_abs_pose_z: 0.05,
  max_abs_pose_roll: 0.5,
  max_abs_pose_pitch: 0.5,
  min_mouth_open: 0.0,
  max_mouth_open: 1.0,
  min_event_interval_s: 0.25,
  mode_locomotion_guard_s: 0.5,
});

/** Per-track scalar/boolean fields with their editing ranges (from the limits). */
export function trackFields(limits = DEFAULT_LIMITS) {
  return {
    locomotion: [
      { name: 'vx', min: -limits.max_abs_vx, max: limits.max_abs_vx, unit: 'm/s' },
      { name: 'vy', min: -limits.max_abs_vy, max: limits.max_abs_vy, unit: 'm/s' },
      { name: 'vyaw', min: -limits.max_abs_vyaw, max: limits.max_abs_vyaw, unit: 'rad/s' },
    ],
    head: ['neck_pitch', 'head_pitch', 'head_yaw', 'head_roll'].map((name) => ({
      name, min: -limits.max_abs_head_angle, max: limits.max_abs_head_angle, unit: 'rad',
    })),
    pose: [
      { name: 'z', min: -limits.max_abs_pose_z, max: limits.max_abs_pose_z, unit: 'm' },
      { name: 'roll', min: -limits.max_abs_pose_roll, max: limits.max_abs_pose_roll, unit: 'rad' },
      { name: 'pitch', min: -limits.max_abs_pose_pitch, max: limits.max_abs_pose_pitch, unit: 'rad' },
      { name: 'active', bool: true },
    ],
    mouth: [{ name: 'open', min: limits.min_mouth_open, max: limits.max_mouth_open, unit: '' }],
  };
}

export class DuckShowFormatError extends Error {
  constructor(message) {
    super(message);
    this.name = 'DuckShowFormatError';
  }
}

// ---------------------------------------------------------------------------
// Python-compatible formatting (so validator messages match python/duckshow
// byte for byte: str(float), repr(str), repr(tuple/list)).
// ---------------------------------------------------------------------------

/** str(float) as CPython prints it: '1.0', '0.5', '1e-05', '1e+16', 'inf', 'nan'. */
export function pyFloatStr(x) {
  if (Number.isNaN(x)) return 'nan';
  if (x === Infinity) return 'inf';
  if (x === -Infinity) return '-inf';
  if (x === 0) return Object.is(x, -0) ? '-0.0' : '0.0';
  const neg = x < 0;
  const [mant, expPart] = Math.abs(x).toExponential().split('e'); // shortest round-trip digits
  const e = parseInt(expPart, 10);
  const digits = mant.replace('.', '');
  let out;
  if (e < -4 || e >= 16) {
    const m = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    out = `${m}e${e < 0 ? '-' : '+'}${String(Math.abs(e)).padStart(2, '0')}`;
  } else if (e >= 0) {
    out = digits.length <= e + 1
      ? `${digits.padEnd(e + 1, '0')}.0`
      : `${digits.slice(0, e + 1)}.${digits.slice(e + 1)}`;
  } else {
    out = `0.${'0'.repeat(-e - 1)}${digits}`;
  }
  return neg ? `-${out}` : out;
}

/** repr(str) as CPython prints it (quote choice + escapes). */
export function pyReprStr(s) {
  const quote = s.includes("'") && !s.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of s) {
    const code = ch.codePointAt(0);
    if (ch === '\\') out += '\\\\';
    else if (ch === quote) out += `\\${quote}`;
    else if (ch === '\n') out += '\\n';
    else if (ch === '\r') out += '\\r';
    else if (ch === '\t') out += '\\t';
    else if (code < 0x20 || code === 0x7f) out += `\\x${code.toString(16).padStart(2, '0')}`;
    else out += ch;
  }
  return out + quote;
}

/** repr(value) for the JSON-ish values that can reach a validator message. */
export function pyRepr(v) {
  if (v === null || v === undefined) return 'None';
  if (typeof v === 'string') return pyReprStr(v);
  if (typeof v === 'boolean') return v ? 'True' : 'False';
  if (typeof v === 'number') return Number.isInteger(v) && Math.abs(v) < 1e21 && !Object.is(v, -0) ? String(v) : pyFloatStr(v);
  if (Array.isArray(v)) return `[${v.map(pyRepr).join(', ')}]`;
  if (typeof v === 'object') return `{${Object.entries(v).map(([k, x]) => `${pyReprStr(k)}: ${pyRepr(x)}`).join(', ')}}`;
  return String(v);
}

function pyTuple(items) {
  return `(${items.map(pyRepr).join(', ')})`;
}

function pyTypeName(v) {
  if (v === null || v === undefined) return 'NoneType';
  if (Array.isArray(v)) return 'list';
  if (typeof v === 'number') return Number.isInteger(v) ? 'int' : 'float';
  if (typeof v === 'boolean') return 'bool';
  if (typeof v === 'string') return 'str';
  return 'dict';
}

// ---------------------------------------------------------------------------
// Parse / normalize (mirrors python/duckshow/loader.py)
// ---------------------------------------------------------------------------

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function requireDict(value, fieldName) {
  if (!isPlainObject(value)) {
    throw new DuckShowFormatError(`expected ${pyReprStr(fieldName)} to be an object, got ${pyTypeName(value)}`);
  }
  return value;
}

function requireList(value, fieldName) {
  if (value === null || value === undefined) return [];
  if (!Array.isArray(value)) {
    throw new DuckShowFormatError(`expected ${pyReprStr(fieldName)} to be a list, got ${pyTypeName(value)}`);
  }
  return value;
}

function requireStr(value, fieldName) {
  if (typeof value !== 'string') {
    throw new DuckShowFormatError(`expected ${pyReprStr(fieldName)} to be a string, got ${pyTypeName(value)}`);
  }
  return value;
}

/** Python float(value) for JSON values: numbers, bools, numeric strings. */
function pyFloat(value) {
  if (typeof value === 'number') return value;
  if (typeof value === 'boolean') return value ? 1 : 0;
  if (typeof value === 'string') {
    const s = value.trim().toLowerCase().replace(/_/g, '');
    const m = /^([+-]?)(inf|infinity|nan)$/.exec(s);
    if (m) return m[2] === 'nan' ? NaN : (m[1] === '-' ? -Infinity : Infinity);
    if (/^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/.test(s)) return Number(s);
    throw new DuckShowFormatError(`malformed document: could not convert string to float: ${pyReprStr(value)}`);
  }
  throw new DuckShowFormatError(
    `malformed document: float() argument must be a string or a real number, not ${pyReprStr(pyTypeName(value))}`,
  );
}

/** Python bool(value) truthiness for JSON values. */
function pyBool(value) {
  if (value === null || value === undefined) return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'object') return Object.keys(value).length > 0;
  return Boolean(value);
}

// d.get(key, default)
function get(d, key, dflt = null) {
  return d[key] === undefined ? dflt : d[key];
}

// float(d[key])  -- KeyError when missing
function requiredFloat(d, key) {
  if (d[key] === undefined) throw new DuckShowFormatError(`malformed document: ${pyReprStr(key)}`);
  return pyFloat(d[key]);
}

// float(d.get(key, dflt))
function floatOr(d, key, dflt) {
  return pyFloat(get(d, key, dflt));
}

// float(d[key]) if d.get(key) is not None else None
function optionalFloat(d, key) {
  const v = get(d, key);
  return v === null ? null : pyFloat(v);
}

function checkFormatVersion(fmt) {
  if (typeof fmt !== 'string') {
    throw new DuckShowFormatError(`missing or non-string top-level 'format' field: ${pyRepr(fmt)}`);
  }
  const m = /^duckshow\/(\d+)$/.exec(fmt);
  if (!m) {
    throw new DuckShowFormatError(`unrecognized 'format' field ${pyReprStr(fmt)}; expected 'duckshow/<major>'`);
  }
  const major = parseInt(m[1], 10);
  if (major !== SUPPORTED_FORMAT_MAJOR) {
    throw new DuckShowFormatError(
      `unsupported duckshow format major version ${major}; this loader only supports duckshow/${SUPPORTED_FORMAT_MAJOR}`,
    );
  }
}

function parseMusic(d) {
  if (d === null) return null;
  d = requireDict(d, 'meta.music');
  return { file: get(d, 'file'), bpm: get(d, 'bpm'), beat_offset: floatOr(d, 'beat_offset', 0.0) };
}

function parseMeta(d) {
  if (d === null) d = {};
  d = requireDict(d, 'meta');
  return {
    name: get(d, 'name'),
    author: get(d, 'author'),
    created: get(d, 'created'),
    duration: optionalFloat(d, 'duration'),
    music: parseMusic(get(d, 'music')),
  };
}

// Mirrors python/duckshow/loader.py's _parse_policy: name/file/sha256/slot
// are all required strings. There is deliberately no `mode` field -- the
// drive-mode string sent at runtime by a `mode` event is always just
// "walk" or "roller" (see DRIVE_MODES / checkModeValue below), completely
// independent of which policy is behind a given slot. A `.duckshow` file
// written before that was clarified may still carry a `mode` key on a
// policy entry; it is simply never read here, same "unknown fields are
// ignored everywhere" discipline as the rest of this format -- and since
// serializeShow() writes the original parsed document (never this
// normalized view), that stray key is neither required nor re-emitted.
function parsePolicy(d) {
  d = requireDict(d, 'requires.policies[]');
  return {
    name: requireStr(get(d, 'name'), 'requires.policies[].name'),
    file: requireStr(get(d, 'file'), 'requires.policies[].file'),
    sha256: requireStr(get(d, 'sha256'), 'requires.policies[].sha256'),
    slot: requireStr(get(d, 'slot'), 'requires.policies[].slot'),
  };
}

function parseRequires(d) {
  if (d === null) return { policies: [] };
  d = requireDict(d, 'requires');
  return { policies: requireList(get(d, 'policies'), 'requires.policies').map(parsePolicy) };
}

function parseCast(raw) {
  return requireList(raw, 'cast').map((entry) => {
    entry = requireDict(entry, 'cast[]');
    return { role: requireStr(get(entry, 'role'), 'cast[].role'), notes: get(entry, 'notes') };
  });
}

function interpOf(d) {
  return get(d, 'interp', DEFAULT_INTERP);
}

function parseLocomotionKf(d) {
  d = requireDict(d, 'locomotion[]');
  return { t: requiredFloat(d, 't'), vx: floatOr(d, 'vx', 0.0), vy: floatOr(d, 'vy', 0.0), vyaw: floatOr(d, 'vyaw', 0.0), interp: interpOf(d) };
}

function parseHeadKf(d) {
  d = requireDict(d, 'head[]');
  return {
    t: requiredFloat(d, 't'),
    neck_pitch: floatOr(d, 'neck_pitch', 0.0),
    head_pitch: floatOr(d, 'head_pitch', 0.0),
    head_yaw: floatOr(d, 'head_yaw', 0.0),
    head_roll: floatOr(d, 'head_roll', 0.0),
    interp: interpOf(d),
  };
}

function parsePoseKf(d) {
  d = requireDict(d, 'pose[]');
  return {
    t: requiredFloat(d, 't'),
    z: floatOr(d, 'z', 0.0),
    roll: floatOr(d, 'roll', 0.0),
    pitch: floatOr(d, 'pitch', 0.0),
    active: pyBool(get(d, 'active', false)),
    interp: interpOf(d),
  };
}

function parseMouthKf(d) {
  d = requireDict(d, 'mouth[]');
  return { t: requiredFloat(d, 't'), open: floatOr(d, 'open', 0.0), interp: interpOf(d) };
}

function parseEvent(d, index) {
  d = requireDict(d, 'events[]');
  return { t: requiredFloat(d, 't'), do: get(d, 'do'), sound: get(d, 'sound'), hold: optionalFloat(d, 'hold'), mode: get(d, 'mode'), index };
}

function parseServoEvent(d, index) {
  d = requireDict(d, 'servo[]');
  return { t: requiredFloat(d, 't'), mode: get(d, 'mode', 'hold'), duration: optionalFloat(d, 'duration'), target: get(d, 'target'), index };
}

function parseRoleTracks(d) {
  return {
    locomotion: requireList(get(d, 'locomotion'), 'locomotion').map(parseLocomotionKf),
    head: requireList(get(d, 'head'), 'head').map(parseHeadKf),
    pose: requireList(get(d, 'pose'), 'pose').map(parsePoseKf),
    mouth: requireList(get(d, 'mouth'), 'mouth').map(parseMouthKf),
    events: requireList(get(d, 'events'), 'events').map(parseEvent),
    servo: requireList(get(d, 'servo'), 'servo').map(parseServoEvent),
  };
}

function emptyRoleTracks() {
  return { locomotion: [], head: [], pose: [], mouth: [], events: [], servo: [] };
}

function parseTracks(raw) {
  const tracks = new Map();
  if (raw === null) return tracks;
  raw = requireDict(raw, 'tracks');
  for (const [role, d] of Object.entries(raw)) {
    tracks.set(role, parseRoleTracks(requireDict(d, `tracks[${pyReprStr(role)}]`)));
  }
  return tracks;
}

const NORMALIZED = Symbol('duckshow.normalized');

/**
 * Shape-check a parsed document and return a normalized view of it — defaults
 * filled, numbers coerced like Python's float(), tracks as a Map keyed by role.
 * Throws DuckShowFormatError for anything the Python loader would reject.
 * Indices in the normalized keyframe/event arrays equal those in the document.
 */
export function normalizeShow(doc) {
  if (doc && doc[NORMALIZED]) return doc;
  if (!isPlainObject(doc)) throw new DuckShowFormatError('top-level .duckshow document must be a JSON object');
  checkFormatVersion(get(doc, 'format'));
  const norm = {
    format: doc.format,
    meta: parseMeta(get(doc, 'meta')),
    requires: parseRequires(get(doc, 'requires')),
    cast: parseCast(get(doc, 'cast')),
    tracks: parseTracks(get(doc, 'tracks')),
    roleNames() { return this.cast.map((c) => c.role); },
    tracksFor(role) { return this.tracks.get(role) || emptyRoleTracks(); },
  };
  Object.defineProperty(norm, NORMALIZED, { value: true, enumerable: false });
  return norm;
}

/** Parse a .duckshow document from JSON text. Returns the document itself. */
export function parseShow(text) {
  let doc;
  try {
    doc = JSON.parse(text);
  } catch (err) {
    throw new DuckShowFormatError(`invalid JSON: ${err.message}`);
  }
  normalizeShow(doc);
  return doc;
}

/** Serialize a show document (unknown fields included). Refuses non-finite numbers. */
export function serializeShow(show, indent = 2) {
  const text = JSON.stringify(show, (key, value) => {
    if (typeof value === 'number' && !Number.isFinite(value)) {
      throw new DuckShowFormatError(`non-finite number in field ${pyReprStr(key)} cannot be written to a .duckshow document`);
    }
    return value;
  }, indent);
  return `${text}\n`;
}

/** Normalized tracks for one role (all-empty when the role has no entry). */
export function roleTracks(show, role) {
  return normalizeShow(show).tracksFor(role);
}

// ---------------------------------------------------------------------------
// Sampler (mirrors python/duckshow/sampler.py)
// ---------------------------------------------------------------------------

const EPS = 1e-9;

export function smoothstep(x) {
  x = x < 0 ? 0 : (x > 1 ? 1 : x);
  return x * x * (3 - 2 * x);
}

function interpScalar(v0, v1, frac, interp) {
  if (interp === INTERP_STEP) return v0;
  if (interp === INTERP_SMOOTH) frac = smoothstep(frac);
  else frac = frac < 0 ? 0 : (frac > 1 ? 1 : frac); // linear and anything unrecognized
  return v0 + (v1 - v0) * frac;
}

function bisectRight(ts, t) {
  let lo = 0;
  let hi = ts.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (t < ts[mid]) hi = mid;
    else lo = mid + 1;
  }
  return lo;
}

function locate(keyframes, t) {
  if (keyframes.length === 0) return null;
  if (t <= keyframes[0].t) return { hold: keyframes[0] };
  if (t >= keyframes[keyframes.length - 1].t) return { hold: keyframes[keyframes.length - 1] };
  let i = bisectRight(keyframes.map((k) => k.t), t) - 1;
  if (i < 0) i = 0;
  if (i > keyframes.length - 2) i = keyframes.length - 2;
  const kf0 = keyframes[i];
  const kf1 = keyframes[i + 1];
  const span = kf1.t - kf0.t;
  return { kf0, kf1, frac: span <= 0 ? 0 : (t - kf0.t) / span };
}

function sampleFields(keyframes, t, fields) {
  const loc = locate(keyframes, t);
  if (loc === null) return null;
  const out = {};
  if (loc.hold) {
    for (const [name] of fields) out[name] = loc.hold[name];
    return out;
  }
  for (const [name, isBool] of fields) {
    const v0 = loc.kf0[name];
    out[name] = isBool ? v0 : interpScalar(v0, loc.kf1[name], loc.frac, loc.kf0.interp);
  }
  return out;
}

const LOCOMOTION_FIELDS = [['vx', false], ['vy', false], ['vyaw', false]];
const HEAD_FIELDS = [['neck_pitch', false], ['head_pitch', false], ['head_yaw', false], ['head_roll', false]];
const POSE_FIELDS = [['z', false], ['roll', false], ['pitch', false], ['active', true]];
const MOUTH_FIELDS = [['open', false]];

/**
 * Sampler for one role. `show` may be a document or a normalized show.
 *   at(t)                -> {t, locomotion|null, head|null, pose|null, mouth|null}
 *   eventsBetween(t0,t1) -> events with t in (t0, t1], sorted by t
 *   modeAt(t)            -> latest `mode` event with t <= given t, else null
 *   servoAt(t)           -> servo entry whose window contains t, else null
 */
export function createSampler(show, role) {
  const norm = normalizeShow(show);
  const tracks = norm.tracksFor(role);
  const duration = norm.meta.duration;
  return {
    role,
    tracks,
    duration,
    at(t) {
      let locomotion = null;
      if (tracks.locomotion.length) locomotion = sampleFields(tracks.locomotion, t, LOCOMOTION_FIELDS);
      if (locomotion !== null && duration !== null && t >= duration) locomotion = { vx: 0, vy: 0, vyaw: 0 };
      const head = tracks.head.length ? sampleFields(tracks.head, t, HEAD_FIELDS) : null;
      const pose = tracks.pose.length ? sampleFields(tracks.pose, t, POSE_FIELDS) : null;
      const mouth = tracks.mouth.length ? sampleFields(tracks.mouth, t, MOUTH_FIELDS) : null;
      return { t, locomotion, head, pose, mouth };
    },
    eventsBetween(t0, t1) {
      return tracks.events.filter((e) => t0 < e.t && e.t <= t1).sort((a, b) => a.t - b.t);
    },
    modeAt(t) {
      let best = null;
      for (const e of tracks.events) {
        if (e.mode !== null && e.t <= t && (best === null || e.t > best.t)) best = e;
      }
      return best === null ? null : best.mode;
    },
    servoAt(t) {
      const entries = tracks.servo.slice().sort((a, b) => a.t - b.t);
      let active = null;
      for (let i = 0; i < entries.length; i++) {
        const entry = entries[i];
        if (entry.t > t) break;
        let end = entry.duration !== null ? entry.t + entry.duration : Infinity;
        if (i + 1 < entries.length) end = Math.min(end, entries[i + 1].t);
        if (entry.t <= t && t < end) active = entry;
      }
      return active;
    },
  };
}

// ---------------------------------------------------------------------------
// Validator (mirrors python/duckshow/validator.py; same order, same text)
// ---------------------------------------------------------------------------

function issue(issues, severity, role, track, t, message) {
  issues.push({ severity, role, track, t, message });
}

function checkSortedUnique(issues, role, track, keyframes) {
  let prevT = null;
  for (const kf of keyframes) {
    if (!Number.isFinite(kf.t)) issue(issues, 'error', role, track, kf.t, `t=${pyFloatStr(kf.t)} is not a finite number`);
    else if (kf.t < 0) issue(issues, 'error', role, track, kf.t, `${track} keyframe t=${pyFloatStr(kf.t)} must be >= 0`);
    if (prevT !== null) {
      if (kf.t < prevT) issue(issues, 'error', role, track, kf.t, `${track} keyframes are not sorted by t`);
      else if (kf.t === prevT) issue(issues, 'error', role, track, kf.t, `duplicate t=${pyFloatStr(kf.t)} in ${track} track`);
    }
    prevT = kf.t;
  }
}

function checkInterpValid(issues, role, track, keyframes) {
  for (const kf of keyframes) {
    if (!VALID_INTERPS.includes(kf.interp)) {
      issue(issues, 'error', role, track, kf.t, `${track} keyframe interp=${pyRepr(kf.interp)} is not one of ${pyTuple(VALID_INTERPS)}`);
    }
  }
}

function checkScalarLimit(issues, role, track, t, name, value, limit) {
  if (!Number.isFinite(value)) {
    issue(issues, 'error', role, track, t, `${name}=${pyFloatStr(value)} is not a finite number`);
    return;
  }
  if (Math.abs(value) > limit + EPS) {
    issue(issues, 'error', role, track, t, `${name}=${pyFloatStr(value)} exceeds limit of +/-${pyFloatStr(limit)}`);
  }
}

function checkRange(issues, role, track, t, name, value, lo, hi) {
  if (!Number.isFinite(value)) {
    issue(issues, 'error', role, track, t, `${name}=${pyFloatStr(value)} is not a finite number`);
    return;
  }
  if (value < lo - EPS || value > hi + EPS) {
    issue(issues, 'error', role, track, t, `${name}=${pyFloatStr(value)} outside allowed range [${pyFloatStr(lo)}, ${pyFloatStr(hi)}]`);
  }
}

function checkEventDensity(issues, role, events, limits) {
  const ordered = events.slice().sort((a, b) => a.t - b.t);
  let prevT = null;
  for (const e of ordered) {
    if (prevT !== null && (e.t - prevT) < limits.min_event_interval_s - EPS) {
      issue(issues, 'error', role, 'events', e.t,
        `event at t=${pyFloatStr(e.t)} is less than ${pyFloatStr(limits.min_event_interval_s)}s after previous event at t=${pyFloatStr(prevT)}`);
    }
    prevT = e.t;
  }
}

function checkEventAction(issues, role, e) {
  const present = ['do', 'sound', 'mode'].filter((k) => e[k] !== null);
  if (present.length === 0) issue(issues, 'error', role, 'events', e.t, 'event has no action key (one of do/sound/mode required)');
  else if (present.length > 1) issue(issues, 'error', role, 'events', e.t, `event has more than one action key: ${pyRepr(present)}`);
  if (e.do !== null && !SKILLS.includes(e.do)) {
    issue(issues, 'error', role, 'events', e.t, `do=${pyRepr(e.do)} is not a recognized skill (expected one of ${pyTuple(SKILLS)})`);
  }
  if (e.sound !== null && !SOUND_TAGS.includes(e.sound)) {
    issue(issues, 'error', role, 'events', e.t, `sound=${pyRepr(e.sound)} is not a recognized sound tag (expected one of ${pyTuple(SOUND_TAGS)})`);
  }
}

function checkEventFields(issues, role, e) {
  if (!Number.isFinite(e.t)) issue(issues, 'error', role, 'events', e.t, `t=${pyFloatStr(e.t)} is not a finite number`);
  else if (e.t < 0) issue(issues, 'error', role, 'events', e.t, `event t=${pyFloatStr(e.t)} must be >= 0`);
  if (e.hold !== null && !Number.isFinite(e.hold)) {
    issue(issues, 'error', role, 'events', e.t, `hold=${pyFloatStr(e.hold)} is not a finite number`);
  }
}

// Mirrors python/duckshow/validator.py's _check_mode_value: a `mode`
// event's value must be a real robotd drive mode -- real hardware accepts
// exactly "walk" or "roller" over the wire and has no mechanism to
// register a custom-named mode (docs/robotd-api.md "Custom .onnx policies
// & modes"; docs/duckshow-format.md "Custom .onnx policies"). A
// custom-trained gait is installed by pointing a fixed policy *slot* at a
// different .onnx file (requires.policies[]), never by inventing a new
// mode string, so `requires.policies` plays no part in whether a `mode`
// event is valid.
function checkModeValue(issues, role, events) {
  for (const e of events) {
    if (e.mode !== null && !DRIVE_MODES.includes(e.mode)) {
      issue(issues, 'error', role, 'events', e.t, `mode=${pyRepr(e.mode)} is not a valid drive mode (expected one of ${pyTuple(DRIVE_MODES)})`);
    }
  }
}

function locomotionNonzeroInWindow(sampler, lo, hi) {
  const times = new Set([lo, hi]);
  for (const kf of sampler.tracks.locomotion) {
    if (lo <= kf.t && kf.t <= hi) times.add(kf.t);
  }
  for (const tt of [...times].sort((a, b) => a - b)) {
    const v = sampler.at(tt).locomotion;
    if (v === null) continue;
    if (Math.abs(v.vx) > EPS || Math.abs(v.vy) > EPS || Math.abs(v.vyaw) > EPS) return true;
  }
  return false;
}

function checkModeLocomotionOverlap(issues, role, norm, events, limits) {
  if (events.length === 0) return;
  const sampler = createSampler(norm, role);
  const guard = limits.mode_locomotion_guard_s;
  for (const e of events) {
    if (e.mode === null) continue;
    const lo = (e.t - guard) > 0 ? e.t - guard : 0; // Python max(0.0, x): keeps 0.0 unless x > 0
    const hi = e.t + guard;
    if (locomotionNonzeroInWindow(sampler, lo, hi)) {
      issue(issues, 'warning', role, 'events', e.t,
        `mode event ${pyRepr(e.mode)} at t=${pyFloatStr(e.t)} overlaps nonzero locomotion within +/-${pyFloatStr(guard)}s`);
    }
  }
}

// Mirrors python/duckshow/validator.py's _check_skill_occupancy_overlap:
// a `do` skill runs its whole episodic clip to completion once started
// (docs/duckshow-format.md "Skill durations and occupancy") -- unlike
// checkEventDensity's 0.25s spacing rule (command flooding, applies to
// every discrete event regardless of type), scheduling a second skill
// *inside* that window schedules against a duck that physically cannot
// have finished the first one yet. WARNING, not an error: the robot still
// accepts the command and something happens, so this is very likely not
// what the author meant rather than something unsafe.
//
// Only consecutive pairs of `do` events are compared, each against the
// skill immediately before it in time order, not every earlier skill.
// roulade is "chain": true in the manifest: a roulade immediately
// following a roulade is the documented way to keep rolling, not two
// skills contending for one window (CHAINING_SKILLS), so that specific
// pairing never warns. sit_toggle has no confirmed duration
// (skillDurationS returns null for it) and so never occupies here,
// whether it is the earlier or the later event.
function checkSkillOccupancyOverlap(issues, role, norm, events) {
  const skillEvents = events.filter((e) => e.do !== null).slice().sort((a, b) => a.t - b.t);
  if (skillEvents.length < 2) return;
  const sampler = createSampler(norm, role);
  let prev = skillEvents[0];
  for (const cur of skillEvents.slice(1)) {
    if (!(CHAINING_SKILLS.includes(prev.do) && cur.do === prev.do)) {
      const duration = skillDurationS(prev.do, sampler.modeAt(prev.t));
      if (duration !== null) {
        const end = prev.t + duration;
        if (cur.t < end - EPS) {
          const overlap = end - cur.t;
          issue(issues, 'warning', role, 'events', cur.t,
            `do=${pyRepr(cur.do)} at t=${pyFloatStr(cur.t)} begins ${pyFloatStr(overlap)}s into the `
            + `${pyFloatStr(duration)}s execution of do=${pyRepr(prev.do)} at t=${pyFloatStr(prev.t)}`);
        }
      }
    }
    prev = cur;
  }
}

function checkMetaDuration(issues, norm) {
  const duration = norm.meta.duration;
  if (duration === null) {
    issue(issues, 'error', null, null, null, 'meta.duration is required');
    return;
  }
  if (!Number.isFinite(duration) || duration <= 0) {
    issue(issues, 'error', null, null, null, `meta.duration=${pyFloatStr(duration)} must be a finite number > 0`);
  }
}

function checkServo(issues, role, entries) {
  for (const e of entries) {
    if (!Number.isFinite(e.t)) issue(issues, 'error', role, 'servo', e.t, `t=${pyFloatStr(e.t)} is not a finite number`);
    else if (e.t < 0) issue(issues, 'error', role, 'servo', e.t, `servo t=${pyFloatStr(e.t)} must be >= 0`);
    if (e.duration !== null) {
      if (!Number.isFinite(e.duration)) issue(issues, 'error', role, 'servo', e.t, `duration=${pyFloatStr(e.duration)} is not a finite number`);
      else if (e.duration <= 0) issue(issues, 'error', role, 'servo', e.t, `servo duration=${pyFloatStr(e.duration)} must be > 0`);
    }
    if (e.mode !== 'hold') {
      issue(issues, 'warning', role, 'servo', e.t, `servo mode ${pyRepr(e.mode)} is not honored by v1 agents (only 'hold' has any effect)`);
    }
  }
}

/**
 * Validate a show (document or normalized). Returns issues
 * [{severity: 'error'|'warning', role, track, t, message}] in the same order,
 * with the same text, as python/duckshow.validator.validate().
 */
export function validate(show, limits = DEFAULT_LIMITS) {
  const norm = normalizeShow(show);
  const issues = [];

  checkMetaDuration(issues, norm);

  for (const member of norm.cast) {
    const role = member.role;
    if (!norm.tracks.has(role)) {
      issue(issues, 'error', role, null, null, `cast role ${pyReprStr(role)} has no tracks entry`);
      continue;
    }
    const tracks = norm.tracks.get(role);

    for (const track of CURVE_TRACKS) checkSortedUnique(issues, role, track, tracks[track]);
    for (const track of CURVE_TRACKS) checkInterpValid(issues, role, track, tracks[track]);

    for (const kf of tracks.locomotion) {
      checkScalarLimit(issues, role, 'locomotion', kf.t, 'vx', kf.vx, limits.max_abs_vx);
      checkScalarLimit(issues, role, 'locomotion', kf.t, 'vy', kf.vy, limits.max_abs_vy);
      checkScalarLimit(issues, role, 'locomotion', kf.t, 'vyaw', kf.vyaw, limits.max_abs_vyaw);
    }
    for (const kf of tracks.head) {
      for (const name of ['neck_pitch', 'head_pitch', 'head_yaw', 'head_roll']) {
        checkScalarLimit(issues, role, 'head', kf.t, name, kf[name], limits.max_abs_head_angle);
      }
    }
    for (const kf of tracks.pose) {
      checkScalarLimit(issues, role, 'pose', kf.t, 'z', kf.z, limits.max_abs_pose_z);
      checkScalarLimit(issues, role, 'pose', kf.t, 'roll', kf.roll, limits.max_abs_pose_roll);
      checkScalarLimit(issues, role, 'pose', kf.t, 'pitch', kf.pitch, limits.max_abs_pose_pitch);
    }
    for (const kf of tracks.mouth) {
      checkRange(issues, role, 'mouth', kf.t, 'open', kf.open, limits.min_mouth_open, limits.max_mouth_open);
    }

    for (const e of tracks.events) {
      checkEventAction(issues, role, e);
      checkEventFields(issues, role, e);
    }
    checkEventDensity(issues, role, tracks.events, limits);
    checkModeValue(issues, role, tracks.events);
    checkModeLocomotionOverlap(issues, role, norm, tracks.events, limits);
    checkSkillOccupancyOverlap(issues, role, norm, tracks.events);

    checkServo(issues, role, tracks.servo);
  }

  return issues;
}

/** {errors: [...], warnings: [...]} split of validate(). */
export function validationReport(show, limits = DEFAULT_LIMITS) {
  const issues = validate(show, limits);
  return {
    issues,
    errors: issues.filter((i) => i.severity === 'error'),
    warnings: issues.filter((i) => i.severity === 'warning'),
  };
}

// ---------------------------------------------------------------------------
// Beat grid
// ---------------------------------------------------------------------------

export function beatPeriod(bpm) {
  return bpm > 0 ? 60 / bpm : NaN;
}

/**
 * Beat (or sub-beat) times in [0, duration]: beatOffset + k * 60/bpm/subdivision
 * for every integer k, including beats before the first downbeat.
 */
export function beatTimes(bpm, beatOffset = 0, duration = 0, subdivision = 1) {
  bpm = Number(bpm);
  beatOffset = Number(beatOffset) || 0;
  duration = Number(duration);
  subdivision = Number(subdivision) || 1;
  if (!(bpm > 0) || !(duration >= 0) || !(subdivision > 0) || !Number.isFinite(duration)) return [];
  const period = 60 / bpm / subdivision;
  const kStart = Math.ceil((0 - beatOffset) / period - 1e-9);
  const kEnd = Math.floor((duration - beatOffset) / period + 1e-9);
  const out = [];
  for (let k = kStart; k <= kEnd; k++) {
    const t = Math.round((beatOffset + k * period) * 1e9) / 1e9;
    if (t >= 0 && t <= duration) out.push(t);
  }
  return out;
}

/** Beat grid straight from meta.music (empty when there is no bpm). */
export function showBeatTimes(show, subdivision = 1) {
  const norm = normalizeShow(show);
  const music = norm.meta.music;
  if (!music || !(Number(music.bpm) > 0)) return [];
  return beatTimes(music.bpm, music.beat_offset, norm.meta.duration ?? 0, subdivision);
}

/** Nearest grid time to t (t itself when the grid is empty). */
export function snap(t, grid) {
  if (!grid || grid.length === 0) return t;
  const i = bisectRight(grid, t);
  const before = grid[i - 1];
  const after = grid[i];
  if (before === undefined) return after;
  if (after === undefined) return before;
  return (t - before) <= (after - t) ? before : after;
}

// ---------------------------------------------------------------------------
// Dead reckoning
// ---------------------------------------------------------------------------

/**
 * Integrate one role's locomotion track into a top-down path.
 * Trunk frame: vx forward, vy left, vyaw counter-clockwise (rad/s); world frame:
 * x right, y up, heading CCW from +x. Locomotion is zero at/after meta.duration
 * (sampler rule) and inside a servo "hold" window (v1 agent rule).
 * Returns [{t, x, y, heading}] with a point every dt seconds from t=0 to tEnd.
 */
export function integrate(show, role, dt = 0.02, opts = {}) {
  const norm = normalizeShow(show);
  const sampler = createSampler(norm, role);
  const start = opts.start || {};
  let x = Number(start.x) || 0;
  let y = Number(start.y) || 0;
  let heading = Number(start.heading) || 0;
  const tEnd = opts.tEnd ?? norm.meta.duration ?? 0;
  const out = [{ t: 0, x, y, heading }];
  if (!(dt > 0) || !(tEnd > 0) || !Number.isFinite(tEnd)) return out;
  const n = Math.ceil(tEnd / dt - 1e-9);
  for (let i = 0; i < n; i++) {
    const t0 = i * dt;
    const t1 = Math.min(tEnd, (i + 1) * dt);
    const h = t1 - t0;
    const v = sampler.at(t0).locomotion;
    let vx = 0;
    let vy = 0;
    let vyaw = 0;
    if (v !== null) ({ vx, vy, vyaw } = v);
    const servo = sampler.servoAt(t0);
    if (servo !== null && servo.mode === 'hold') { vx = 0; vy = 0; vyaw = 0; }
    const hm = heading + vyaw * h / 2; // midpoint heading for the displacement
    x += (vx * Math.cos(hm) - vy * Math.sin(hm)) * h;
    y += (vx * Math.sin(hm) + vy * Math.cos(hm)) * h;
    heading += vyaw * h;
    out.push({ t: t1, x, y, heading });
  }
  return out;
}

// ---------------------------------------------------------------------------
// Edit operations — pure: the input show is never mutated.
// ---------------------------------------------------------------------------

function clone(v) {
  if (Array.isArray(v)) return v.map(clone);
  if (v !== null && typeof v === 'object') {
    const out = {};
    for (const [k, x] of Object.entries(v)) out[k] = clone(x);
    return out;
  }
  return v;
}

function assertTrack(track) {
  if (!CURVE_TRACKS.includes(track)) throw new RangeError(`not a curve track: ${track}`);
}

function assertRole(show, role) {
  const cast = Array.isArray(show.cast) ? show.cast : [];
  if (!cast.some((c) => c && c.role === role)) throw new RangeError(`role not in cast: ${role}`);
}

function assertFiniteTime(t) {
  if (typeof t !== 'number' || !Number.isFinite(t)) throw new RangeError(`t must be a finite number, got ${t}`);
}

function ensureTrackList(show, role, track) {
  if (!isPlainObject(show.tracks)) show.tracks = {};
  if (!isPlainObject(show.tracks[role])) show.tracks[role] = {};
  if (!Array.isArray(show.tracks[role][track])) show.tracks[role][track] = [];
  return show.tracks[role][track];
}

function timeOf(entry) {
  const t = entry && entry.t;
  return typeof t === 'number' ? t : Number(t);
}

function insertSortedByT(list, entry) {
  let i = 0;
  while (i < list.length && !(timeOf(list[i]) > timeOf(entry))) i++;
  list.splice(i, 0, entry);
  return i;
}

/** Index of the keyframe at exactly t (-1 when there is none). */
export function keyframeIndexAt(show, role, track, t) {
  const list = show.tracks && show.tracks[role] && show.tracks[role][track];
  if (!Array.isArray(list)) return -1;
  return list.findIndex((kf) => timeOf(kf) === t);
}

/**
 * Add a keyframe ({t, ...fields}) into a curve track, kept sorted by t. If a
 * keyframe already sits at exactly t, the new fields are merged into it.
 */
export function addKeyframe(show, role, track, keyframe) {
  assertTrack(track);
  assertRole(show, role);
  assertFiniteTime(keyframe.t);
  if (keyframe.t < 0) throw new RangeError('t must be >= 0');
  const next = clone(show);
  const list = ensureTrackList(next, role, track);
  const existing = list.findIndex((kf) => timeOf(kf) === keyframe.t);
  if (existing >= 0) list[existing] = { ...list[existing], ...clone(keyframe) };
  else insertSortedByT(list, clone(keyframe));
  return next;
}

/**
 * Change a keyframe's time and/or values: changes = {t?, <field>?...}. A time
 * that collides with another keyframe of the same track is refused (the show
 * is returned unchanged) so the sorted-unique invariant always holds. Use
 * keyframeIndexAt() to find the keyframe again after a move.
 */
export function moveKeyframe(show, role, track, index, changes) {
  assertTrack(track);
  const list = show.tracks?.[role]?.[track];
  if (!Array.isArray(list) || index < 0 || index >= list.length) throw new RangeError(`no keyframe ${index} in ${role}/${track}`);
  const updated = { ...list[index] };
  for (const [k, v] of Object.entries(changes)) {
    if (v === undefined) delete updated[k];
    else updated[k] = clone(v);
  }
  if ('t' in changes) {
    assertFiniteTime(updated.t);
    if (updated.t < 0) updated.t = 0;
    if (list.some((kf, i) => i !== index && timeOf(kf) === updated.t)) return show;
  }
  const next = clone(show);
  const nextList = next.tracks[role][track];
  nextList.splice(index, 1);
  insertSortedByT(nextList, updated);
  return next;
}

export function deleteKeyframe(show, role, track, index) {
  assertTrack(track);
  const list = show.tracks?.[role]?.[track];
  if (!Array.isArray(list) || index < 0 || index >= list.length) throw new RangeError(`no keyframe ${index} in ${role}/${track}`);
  const next = clone(show);
  next.tracks[role][track].splice(index, 1);
  return next;
}

export function setInterp(show, role, track, index, interp) {
  if (!VALID_INTERPS.includes(interp)) throw new RangeError(`interp must be one of ${VALID_INTERPS.join('/')}`);
  return moveKeyframe(show, role, track, index, { interp });
}

/** linear -> smooth -> step -> linear (unknown values restart at linear). */
export function cycleInterp(interp) {
  const order = [INTERP_LINEAR, INTERP_SMOOTH, INTERP_STEP];
  const i = order.indexOf(interp ?? DEFAULT_INTERP);
  return order[(i + 1) % order.length];
}

/** Add a point event ({t, do|sound|mode, hold?}); inserted in t order. */
export function addEvent(show, role, event) {
  assertRole(show, role);
  assertFiniteTime(event.t);
  if (event.t < 0) throw new RangeError('t must be >= 0');
  const next = clone(show);
  const list = ensureTrackList(next, role, 'events');
  insertSortedByT(list, clone(event));
  return next;
}

/** Merge changes into an event (undefined deletes a key). Position is kept. */
export function updateEvent(show, role, index, changes) {
  const list = show.tracks?.[role]?.events;
  if (!Array.isArray(list) || index < 0 || index >= list.length) throw new RangeError(`no event ${index} in ${role}`);
  if ('t' in changes && changes.t !== undefined) {
    assertFiniteTime(changes.t);
    if (changes.t < 0) throw new RangeError('t must be >= 0');
  }
  const next = clone(show);
  const target = next.tracks[role].events[index];
  for (const [k, v] of Object.entries(changes)) {
    if (v === undefined) delete target[k];
    else target[k] = clone(v);
  }
  return next;
}

/** Replace the event's action with exactly one key: kind in do/sound/mode. */
export function setEventAction(show, role, index, kind, value) {
  if (!['do', 'sound', 'mode'].includes(kind)) throw new RangeError(`unknown event kind: ${kind}`);
  const changes = { do: undefined, sound: undefined, mode: undefined };
  if (kind !== 'sound') changes.hold = undefined;
  changes[kind] = value;
  return updateEvent(show, role, index, changes);
}

export function deleteEvent(show, role, index) {
  const list = show.tracks?.[role]?.events;
  if (!Array.isArray(list) || index < 0 || index >= list.length) throw new RangeError(`no event ${index} in ${role}`);
  const next = clone(show);
  next.tracks[role].events.splice(index, 1);
  return next;
}

/**
 * Merge changes into meta (one level; `music` merges one level deeper).
 * `undefined` deletes a key; music: null removes the music block.
 */
export function setMeta(show, changes) {
  const next = clone(show);
  if (!isPlainObject(next.meta)) next.meta = {};
  for (const [k, v] of Object.entries(changes)) {
    if (v === undefined) {
      delete next.meta[k];
    } else if (k === 'music' && isPlainObject(v)) {
      if (!isPlainObject(next.meta.music)) next.meta.music = {};
      for (const [mk, mv] of Object.entries(v)) {
        if (mv === undefined) delete next.meta.music[mk];
        else next.meta.music[mk] = clone(mv);
      }
    } else {
      next.meta[k] = clone(v);
    }
  }
  return next;
}

/** Append a role to the cast and give it an (empty) tracks entry. */
export function addRole(show, role, notes) {
  if (typeof role !== 'string' || role.trim() === '') throw new RangeError('role must be a non-empty string');
  const cast = Array.isArray(show.cast) ? show.cast : [];
  if (cast.some((c) => c && c.role === role)) throw new RangeError(`role already in cast: ${role}`);
  const next = clone(show);
  if (!Array.isArray(next.cast)) next.cast = [];
  const member = { role };
  if (notes !== undefined && notes !== null && notes !== '') member.notes = notes;
  next.cast.push(member);
  if (!isPlainObject(next.tracks)) next.tracks = {};
  if (!isPlainObject(next.tracks[role])) next.tracks[role] = {};
  return next;
}

/** Remove a role from cast, tracks and editor marks. */
export function removeRole(show, role) {
  const next = clone(show);
  if (Array.isArray(next.cast)) next.cast = next.cast.filter((c) => !(c && c.role === role));
  if (isPlainObject(next.tracks)) delete next.tracks[role];
  if (isPlainObject(next.editor) && isPlainObject(next.editor.marks)) delete next.editor.marks[role];
  return next;
}

/**
 * Rename a role, rewriting every place its name is a key: the `cast`
 * entry, the `tracks` key, and any `editor.marks` entry. One atomic edit —
 * either the whole show is rewritten consistently and the new show is
 * returned, or (on validation failure) it throws and `show` is never
 * touched, never even cloned.
 *
 * `to` is trimmed of surrounding whitespace before use. Renaming to the
 * same name (after trimming) is a no-op that still returns a fresh clone,
 * not an error.
 *
 * The `cast` array entry is rewritten **in place** (same index), never
 * removed and re-appended — this keeps the role's position in cast order
 * stable across a rename. That matters beyond tidiness: the timeline's
 * lane header colour (`duckshow-editor.html`'s `roleColor()`) is indexed
 * by cast order, so preserving the index keeps every *other* role's
 * header colour untouched by this rename. The 3D viewer's palette
 * (`roleColorPalette` in duckshow-viewer.js) is a different, independent
 * scheme — indexed by each role's rank in the *alphabetically sorted*
 * name set, by design, so that the palette survives a cast *reorder*
 * unchanged (see that function's tests). A rename changes the name set,
 * which can shift that alphabetical rank for roles this function never
 * touches. This function cannot fix that from here — it has no palette to
 * preserve, only a show document — so both `StageViewer.setShow` and
 * duckshow-editor.html's own `syncViewerShow()` separately call
 * `roleColorPaletteContinuous` (duckshow-viewer.js) on every edit instead
 * of a bare `roleColorPalette`, to carry the previous palette forward
 * across a same-shape, single-role rename instead of recomputing it; see
 * that function's doc comment.
 *
 * @throws {RangeError} `from` is not in the cast, `to` is empty or
 *   whitespace-only after trimming, or `to` (trimmed) already names a
 *   *different* role in the cast.
 */
export function renameRole(show, from, to) {
  if (typeof from !== 'string' || from === '') throw new RangeError('from must be a non-empty role name');
  const cast = Array.isArray(show.cast) ? show.cast : [];
  if (!cast.some((c) => c && c.role === from)) throw new RangeError(`role not in cast: ${pyReprStr(from)}`);
  const trimmed = typeof to === 'string' ? to.trim() : '';
  if (trimmed === '') throw new RangeError('role name cannot be empty');
  if (trimmed !== from && cast.some((c) => c && c.role === trimmed)) {
    throw new RangeError(`a role named ${pyReprStr(trimmed)} already exists`);
  }

  const next = clone(show);
  if (trimmed === from) return next; // no-op rename; still a fresh object per the "-> new show" contract

  next.cast = next.cast.map((c) => (c && c.role === from ? { ...c, role: trimmed } : c));
  if (isPlainObject(next.tracks) && Object.prototype.hasOwnProperty.call(next.tracks, from)) {
    next.tracks[trimmed] = next.tracks[from];
    delete next.tracks[from];
  }
  if (isPlainObject(next.editor) && isPlainObject(next.editor.marks)
      && Object.prototype.hasOwnProperty.call(next.editor.marks, from)) {
    next.editor.marks[trimmed] = next.editor.marks[from];
    delete next.editor.marks[from];
  }
  return next;
}

/** Stage start mark for a role from the top-level "editor" block ({x:0,y:0,heading:0} default). */
export function getMark(show, role) {
  const m = show.editor?.marks?.[role];
  return {
    x: Number(m?.x) || 0,
    y: Number(m?.y) || 0,
    heading: Number(m?.heading) || 0,
  };
}

export function getMarks(show) {
  const out = {};
  const roles = Array.isArray(show.cast) ? show.cast.map((c) => c && c.role).filter((r) => typeof r === 'string') : [];
  for (const role of roles) out[role] = getMark(show, role);
  return out;
}

/** Persist a start mark under editor.marks[role] (every loader ignores "editor"). */
export function setMark(show, role, mark) {
  const next = clone(show);
  if (!isPlainObject(next.editor)) next.editor = {};
  if (!isPlainObject(next.editor.marks)) next.editor.marks = {};
  next.editor.marks[role] = {
    x: Number(mark.x) || 0,
    y: Number(mark.y) || 0,
    heading: Number(mark.heading) || 0,
  };
  return next;
}

/** A fresh, valid show document. */
export function newShow({ name = 'Untitled', author = null, duration = 30, bpm = 120, beatOffset = 0, roles = ['lead'], created = null } = {}) {
  const meta = { name };
  if (author) meta.author = author;
  meta.created = created || new Date().toISOString().slice(0, 10);
  meta.duration = duration;
  if (bpm > 0) meta.music = { file: null, bpm, beat_offset: beatOffset };
  const tracks = {};
  for (const role of roles) tracks[role] = {};
  return {
    format: FORMAT,
    meta,
    requires: { policies: [] },
    cast: roles.map((role) => ({ role })),
    tracks,
  };
}
