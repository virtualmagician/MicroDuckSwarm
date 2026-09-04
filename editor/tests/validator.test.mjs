// Validator parity with python/duckshow/validator.py.
//
// 1. Data-driven: every shows/fixtures/*.duckshow.json against
//    shows/fixtures/expected.json — the same gate python/tests/test_validator.py
//    and SwarmLink/Tests/SwarmLinkTests/DuckShowFixtureTests.swift run.
// 2. Rule-by-rule cases mirroring python/tests/test_validator.py and
//    test_validator_findings.py, pinning the exact Python message text.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

import { parseShow, validate, validationReport, DEFAULT_LIMITS } from '../duckshow-core.js';

const FIXTURES = fileURLToPath(new URL('../../shows/fixtures/', import.meta.url));
const DEMO = fileURLToPath(new URL('../../shows/demo/demo.duckshow.json', import.meta.url));

const expected = JSON.parse(readFileSync(join(FIXTURES, 'expected.json'), 'utf8'));
const fixtureNames = Object.keys(expected).filter((k) => !k.startsWith('_')).sort();

test('every fixture file has an expected entry and vice versa', () => {
  const onDisk = readdirSync(FIXTURES)
    .filter((f) => f.endsWith('.duckshow.json'))
    .map((f) => f.replace(/\.duckshow\.json$/, ''))
    .sort();
  assert.deepEqual(onDisk, fixtureNames);
});

for (const name of fixtureNames) {
  test(`fixture parity: ${name}`, () => {
    const spec = expected[name];
    const show = parseShow(readFileSync(join(FIXTURES, `${name}.duckshow.json`), 'utf8'));
    const { errors, warnings } = validationReport(show);
    assert.equal(errors.length, spec.errors, `${name}: errors=${JSON.stringify(errors)}`);
    assert.equal(warnings.length, spec.warnings, `${name}: warnings=${JSON.stringify(warnings)}`);
    if (spec.error_substr) {
      assert.ok(errors.some((e) => e.message.includes(spec.error_substr)), `${name}: no error contains ${spec.error_substr}`);
    }
    if (spec.warning_substr) {
      assert.ok(warnings.some((w) => w.message.includes(spec.warning_substr)), `${name}: no warning contains ${spec.warning_substr}`);
    }
  });
}

test('demo show has no errors', () => {
  const show = parseShow(readFileSync(DEMO, 'utf8'));
  assert.deepEqual(validationReport(show).errors, []);
});

// --- rule-by-rule -----------------------------------------------------------

function show(tracks, { policies = [], duration = 10.0, cast } = {}) {
  return {
    format: 'duckshow/1',
    meta: { duration },
    requires: { policies },
    cast: cast ?? [{ role: 'lead' }],
    tracks: { lead: tracks },
  };
}
const messages = (issues) => issues.map((i) => i.message);
const errors = (s) => validate(s).filter((i) => i.severity === 'error');
const warnings = (s) => validate(s).filter((i) => i.severity === 'warning');

test('cast role without a tracks entry is an error (with role, no track/t)', () => {
  const doc = show({}, { cast: [{ role: 'lead' }, { role: 'ghost' }] });
  const issues = validate(doc);
  assert.deepEqual(issues, [{ severity: 'error', role: 'ghost', track: null, t: null, message: "cast role 'ghost' has no tracks entry" }]);
});

test('curve keyframes: unsorted, duplicate, negative, and a clean case', () => {
  assert.deepEqual(messages(errors(show({ locomotion: [{ t: 1.0 }, { t: 0.5 }] }))), ['locomotion keyframes are not sorted by t']);
  assert.deepEqual(messages(errors(show({ locomotion: [{ t: 1.0 }, { t: 1.0 }] }))), ['duplicate t=1.0 in locomotion track']);
  assert.deepEqual(messages(errors(show({ head: [{ t: -0.5 }] }))), ['head keyframe t=-0.5 must be >= 0']);
  assert.deepEqual(errors(show({ locomotion: [{ t: 0.0 }, { t: 1.0 }] })), []);
});

test('interp must be step/linear/smooth', () => {
  assert.deepEqual(messages(errors(show({ mouth: [{ t: 0, interp: 'bezier' }] }))),
    ["mouth keyframe interp='bezier' is not one of ('step', 'linear', 'smooth')"]);
  assert.deepEqual(messages(errors(show({ pose: [{ t: 0, interp: null }] }))),
    ["pose keyframe interp=None is not one of ('step', 'linear', 'smooth')"]);
  for (const interp of ['step', 'linear', 'smooth']) assert.deepEqual(errors(show({ mouth: [{ t: 0, interp }] })), []);
});

test('limits table: over-limit errors with Python float text, at-limit passes', () => {
  assert.deepEqual(messages(errors(show({ locomotion: [{ t: 0.0, vx: 0.5 }] }))), [`vx=0.5 exceeds limit of +/-${DEFAULT_LIMITS.max_abs_vx}`]);
  assert.deepEqual(errors(show({ locomotion: [{ t: 0.0, vx: DEFAULT_LIMITS.max_abs_vx, vy: -DEFAULT_LIMITS.max_abs_vy, vyaw: DEFAULT_LIMITS.max_abs_vyaw }] })), []);
  const overVy = Number((DEFAULT_LIMITS.max_abs_vy + 0.01).toFixed(4));
  assert.deepEqual(messages(errors(show({ locomotion: [{ t: 0.0, vy: overVy }] }))), [`vy=${overVy} exceeds limit of +/-${DEFAULT_LIMITS.max_abs_vy}`]);
  assert.deepEqual(messages(errors(show({ locomotion: [{ t: 0.0, vyaw: -2 }] }))), ['vyaw=-2.0 exceeds limit of +/-1.5']);
  assert.deepEqual(messages(errors(show({ head: [{ t: 0.0, head_yaw: 2.0 }] }))), ['head_yaw=2.0 exceeds limit of +/-1.2']);
  assert.deepEqual(messages(errors(show({ pose: [{ t: 0.0, z: 1.0 }] }))), ['z=1.0 exceeds limit of +/-0.05']);
  assert.deepEqual(messages(errors(show({ pose: [{ t: 0.0, roll: 0.6, pitch: -0.6 }] }))),
    ['roll=0.6 exceeds limit of +/-0.5', 'pitch=-0.6 exceeds limit of +/-0.5']);
  assert.deepEqual(messages(errors(show({ mouth: [{ t: 0.0, open: 1.5 }] }))), ['open=1.5 outside allowed range [0.0, 1.0]']);
  assert.deepEqual(messages(errors(show({ mouth: [{ t: 0.0, open: -0.1 }] }))), ['open=-0.1 outside allowed range [0.0, 1.0]']);
  assert.deepEqual(errors(show({ mouth: [{ t: 0.0, open: 0.0 }, { t: 1.0, open: 1.0 }] })), []);
});

test('issue records carry role, track and t', () => {
  const [i] = errors(show({ head: [{ t: 2.5, head_roll: 9 }] }));
  assert.deepEqual(i, { severity: 'error', role: 'lead', track: 'head', t: 2.5, message: 'head_roll=9.0 exceeds limit of +/-1.2' });
});

test('events: 0.25 s spacing per role, order-independent', () => {
  assert.deepEqual(messages(errors(show({ events: [{ t: 1.0, sound: 'chirp' }, { t: 1.1, sound: 'coo' }] }))),
    ['event at t=1.1 is less than 0.25s after previous event at t=1.0']);
  assert.deepEqual(errors(show({ events: [{ t: 1.0, sound: 'chirp' }, { t: 1.3, sound: 'coo' }] })), []);
  assert.deepEqual(errors(show({ events: [{ t: 1.0, sound: 'chirp' }, { t: 1.25, sound: 'coo' }] })), []); // exactly at the limit
  // unsorted events are fine (curve-track sort rule does not apply) but density still is checked in time order
  assert.deepEqual(messages(errors(show({ events: [{ t: 2.0, sound: 'chirp' }, { t: 1.9, sound: 'coo' }] }))),
    ['event at t=2.0 is less than 0.25s after previous event at t=1.9']);
});

test('events: exactly one action key, closed enums, negative t, hold', () => {
  assert.deepEqual(messages(errors(show({ events: [{ t: 1.0 }] }))), ['event has no action key (one of do/sound/mode required)']);
  assert.deepEqual(messages(errors(show({ events: [{ t: 1.0, do: 'kick_left', sound: 'chirp' }] }))),
    ["event has more than one action key: ['do', 'sound']"]);
  assert.deepEqual(messages(errors(show({ events: [{ t: 1.0, do: 'fly' }] }))),
    ["do='fly' is not a recognized skill (expected one of ('ground_pick', 'kick_left', 'kick_right', 'sit_toggle', 'roulade'))"]);
  assert.deepEqual(messages(errors(show({ events: [{ t: 1.0, sound: 'quack' }] }))),
    ["sound='quack' is not a recognized sound tag (expected one of ('alarm', 'greet', 'inquire', 'peck', 'chirp', 'coo', 'wheee'))"]);
  assert.deepEqual(messages(errors(show({ events: [{ t: -1, sound: 'coo', hold: 2 }] }))), ['event t=-1.0 must be >= 0']);
  assert.deepEqual(errors(show({ events: [{ t: 0, do: 'roulade' }, { t: 1, sound: 'wheee', hold: 1.5 }] })), []);
});

test('mode events: walk/roller validate clean; anything else is an error naming both valid values', () => {
  assert.deepEqual(errors(show({ events: [{ t: 1.0, mode: 'walk' }] })), []);
  assert.deepEqual(errors(show({ events: [{ t: 1.0, mode: 'roller' }] })), []);
  assert.deepEqual(messages(errors(show({ events: [{ t: 1.0, mode: 'moonwalk' }] }))),
    ["mode='moonwalk' is not a valid drive mode (expected one of ('walk', 'roller'))"]);
});

test('requires.policies plays no part in mode validation: a declared custom policy label never appears on the wire', () => {
  // The documented, correct pattern: a custom .onnx is installed by
  // pointing a fixed policy *slot* at it (requires.policies[].slot), and
  // played at runtime with an ordinary "walk"/"roller" mode event -- the
  // policy's `name` is a human label only and is never referenced by the
  // event (docs/duckshow-format.md "Custom .onnx policies").
  const withPolicy = show({ events: [{ t: 0.0, mode: 'walk' }] }, {
    policies: [{ name: 'moonwalk', file: 'policies/moonwalk.onnx', sha256: 'abc', slot: 'walk' }],
  });
  assert.deepEqual(errors(withPolicy), []);
});

test('mode event overlapping nonzero locomotion within +/-0.5 s warns', () => {
  const overlapping = show({
    locomotion: [{ t: 0.0, vx: 0.1 }, { t: 2.0, vx: 0.0 }],
    events: [{ t: 1.8, mode: 'roller' }],
  });
  assert.deepEqual(messages(warnings(overlapping)), ["mode event 'roller' at t=1.8 overlaps nonzero locomotion within +/-0.5s"]);

  const zeroNearby = show({ locomotion: [{ t: 0.0, vx: 0.0 }, { t: 2.0, vx: 0.0 }], events: [{ t: 1.0, mode: 'roller' }] });
  assert.deepEqual(warnings(zeroNearby), []);

  const far = show({ locomotion: [{ t: 0.0, vx: 0.1 }, { t: 1.0, vx: 0.0 }], events: [{ t: 5.0, mode: 'roller' }] });
  assert.deepEqual(warnings(far), []);

  // Window edge: locomotion becomes nonzero exactly at t+0.5 -> still inside the closed window.
  const edge = show({ locomotion: [{ t: 0.0, vx: 0.0, interp: 'step' }, { t: 5.5, vx: 0.1 }], events: [{ t: 5.0, mode: 'roller' }] });
  assert.equal(warnings(edge).length, 1);
  // Locomotion held after its last keyframe counts too; but at/after duration it is zeroed.
  const held = show({ locomotion: [{ t: 0.0, vx: 0.1 }], events: [{ t: 9.0, mode: 'roller' }] });
  assert.equal(warnings(held).length, 1);
  const afterEnd = show({ locomotion: [{ t: 0.0, vx: 0.1 }], events: [{ t: 10.5, mode: 'roller' }] });
  assert.equal(warnings(afterEnd).length, 0);
});

// --- skill occupancy overlap (mirrors python/tests/test_validator.py's SkillOccupancyOverlapTest) ---

test('a skill scheduled inside a previous skill\'s occupancy window warns naming both, the overlap, and the duration', () => {
  const s = show({ events: [{ t: 0.0, do: 'ground_pick' }, { t: 0.5, do: 'kick_left' }] });
  assert.deepEqual(messages(warnings(s)), [
    "do='kick_left' at t=0.5 begins 2.3s into the 2.8s execution of do='ground_pick' at t=0.0",
  ]);
  assert.deepEqual(errors(s), []);
});

test('a skill scheduled exactly at (or after) the occupancy boundary does not warn', () => {
  const s = show({ events: [{ t: 0.0, do: 'ground_pick' }, { t: 2.8, do: 'kick_left' }] });
  assert.equal(warnings(s).filter((w) => w.message.includes('execution of do=')).length, 0);
});

test('roulade followed by roulade does not warn (manifest.json "chain": true)', () => {
  // 0.5s is inside roulade's own 1.0s duration -- without the chaining
  // exception this would warn.
  const s = show({ events: [{ t: 0.0, do: 'roulade' }, { t: 0.5, do: 'roulade' }] });
  assert.deepEqual(warnings(s), []);
  assert.deepEqual(errors(s), []);
});

test('ground_pick duration depends on the drive mode active when it starts', () => {
  const walkOnly = show({ events: [{ t: 1.0, do: 'ground_pick' }, { t: 4.0, do: 'kick_right' }] });
  assert.equal(warnings(walkOnly).filter((w) => w.message.includes('execution of do=')).length, 0);

  const rollerFirst = show({
    events: [{ t: 0.0, mode: 'roller' }, { t: 1.0, do: 'ground_pick' }, { t: 4.0, do: 'kick_right' }],
  });
  assert.deepEqual(messages(warnings(rollerFirst)).filter((m) => m.includes('execution of do=')), [
    "do='kick_right' at t=4.0 begins 0.5s into the 3.5s execution of do='ground_pick' at t=1.0",
  ]);
});

test('sit_toggle has no confirmed duration, so it never occupies (only alpha_sitstand.onnx\'s ramp_s/unwind_s, no duration_s)', () => {
  const s = show({ events: [{ t: 0.0, do: 'sit_toggle' }, { t: 0.3, do: 'kick_left' }] });
  assert.deepEqual(warnings(s), []);
  assert.deepEqual(errors(s), []);
});

test('sit_toggle can still be the interrupting (later) skill', () => {
  const s = show({ events: [{ t: 0.0, do: 'ground_pick' }, { t: 0.5, do: 'sit_toggle' }] });
  const w = warnings(s).filter((i) => i.message.includes('execution of do='));
  assert.equal(w.length, 1);
  assert.match(w[0].message, /do='sit_toggle' at t=0\.5/);
  assert.match(w[0].message, /execution of do='ground_pick' at t=0\.0/);
});

test('skill occupancy and the 0.25s event-density rule are independent', () => {
  const s = show({ events: [{ t: 0.0, do: 'kick_left' }, { t: 0.1, do: 'kick_right' }] });
  assert.deepEqual(messages(errors(s)), ["event at t=0.1 is less than 0.25s after previous event at t=0.0"]);
});

test('meta.duration: required, finite, > 0', () => {
  const noDuration = show({});
  delete noDuration.meta.duration;
  assert.deepEqual(validate(noDuration), [{ severity: 'error', role: null, track: null, t: null, message: 'meta.duration is required' }]);
  assert.deepEqual(messages(errors(show({}, { duration: 0 }))), ['meta.duration=0.0 must be a finite number > 0']);
  assert.deepEqual(messages(errors(show({}, { duration: -3 }))), ['meta.duration=-3.0 must be a finite number > 0']);
  assert.deepEqual(validate(show({}, { duration: 0.5 })), []);
});

test('servo track: t >= 0, duration > 0, non-hold modes warn', () => {
  const doc = show({ servo: [{ t: 1, mode: 'laser_homing', duration: 0 }, { t: 2, duration: -1 }, { t: -3 }, { t: 4, mode: 'hold', duration: 1 }] });
  assert.deepEqual(validate(doc).map((i) => [i.severity, i.t, i.message]), [
    ['error', 1, 'servo duration=0.0 must be > 0'],
    ['warning', 1, "servo mode 'laser_homing' is not honored by v1 agents (only 'hold' has any effect)"],
    ['error', 2, 'servo duration=-1.0 must be > 0'],
    ['error', -3, 'servo t=-3.0 must be >= 0'],
  ]);
});

test('issue order matches Python: duration, then per role (sort, interp, limits, events, density, modes, mode-overlap, skill-overlap, servo)', () => {
  const tracks = {
    locomotion: [{ t: 1, vx: 0.5, interp: 'bogus' }, { t: 0.5 }],
    mouth: [{ t: 0, open: 2 }],
    events: [{ t: 0.4, mode: 'x' }, { t: 0.5, do: 'fly' }, { t: 1.0, do: 'ground_pick' }, { t: 1.3, do: 'kick_left' }],
    servo: [{ t: 0, mode: 'nope' }],
  };
  const perRole = [
    'locomotion keyframes are not sorted by t',
    "locomotion keyframe interp='bogus' is not one of ('step', 'linear', 'smooth')",
    `vx=0.5 exceeds limit of +/-${DEFAULT_LIMITS.max_abs_vx}`,
    'open=2.0 outside allowed range [0.0, 1.0]',
    "do='fly' is not a recognized skill (expected one of ('ground_pick', 'kick_left', 'kick_right', 'sit_toggle', 'roulade'))",
    'event at t=0.5 is less than 0.25s after previous event at t=0.4',
    "mode='x' is not a valid drive mode (expected one of ('walk', 'roller'))",
  ];
  // The invalid 'fly' do-event carries no known duration (skillDurationS
  // returns null for anything outside SKILL_DURATIONS_S), so it never
  // participates in the skill-occupancy check; ground_pick at t=1.0 does,
  // and kick_left at t=1.3 begins well inside its 2.8s window.
  const skillOverlap = "do='kick_left' at t=1.3 begins 2.5s into the 2.8s execution of do='ground_pick' at t=1.0";
  assert.deepEqual(messages(validate(show(tracks, { duration: 10 }))), [
    ...perRole,
    "mode event 'x' at t=0.4 overlaps nonzero locomotion within +/-0.5s",
    skillOverlap,
    "servo mode 'nope' is not honored by v1 agents (only 'hold' has any effect)",
  ]);
  // duration 0: the duration error comes first, and because the sampler zeroes
  // locomotion at t >= duration the mode/locomotion overlap warning cannot
  // fire (same as Python) -- but the skill-occupancy warning is unaffected:
  // it only looks at event times and drive mode, never at meta.duration.
  assert.deepEqual(messages(validate(show(tracks, { duration: 0 }))), [
    'meta.duration=0.0 must be a finite number > 0',
    ...perRole,
    skillOverlap,
    "servo mode 'nope' is not honored by v1 agents (only 'hold' has any effect)",
  ]);
});

test('custom limits object is honored', () => {
  const strict = { ...DEFAULT_LIMITS, max_abs_vx: 0.05 };
  assert.deepEqual(messages(validate(show({ locomotion: [{ t: 0, vx: 0.1 }] }), strict)), ['vx=0.1 exceeds limit of +/-0.05']);
});
