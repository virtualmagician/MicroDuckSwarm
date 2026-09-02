// Sampler parity with python/duckshow/sampler.py — the numbers here are the
// ones python/tests/test_sampler.py asserts.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { createSampler, smoothstep, parseShow, normalizeShow } from '../duckshow-core.js';

const DEMO = fileURLToPath(new URL('../../shows/demo/demo.duckshow.json', import.meta.url));

function show(tracks, { role = 'lead', duration = 10.0 } = {}) {
  return {
    format: 'duckshow/1',
    meta: { duration },
    requires: { policies: [] },
    cast: [{ role }],
    tracks: { [role]: tracks },
  };
}

const almost = (a, b, msg) => assert.ok(Math.abs(a - b) < 1e-7, msg ?? `${a} !~ ${b}`);

test('linear: midpoint and quarter point', () => {
  let s = createSampler(show({ locomotion: [{ t: 0.0, vx: 0.0, interp: 'linear' }, { t: 2.0, vx: 1.0 }] }), 'lead');
  almost(s.at(1.0).locomotion.vx, 0.5);
  s = createSampler(show({ locomotion: [{ t: 0.0, vx: 0.0, interp: 'linear' }, { t: 4.0, vx: 2.0 }] }), 'lead');
  almost(s.at(1.0).locomotion.vx, 0.5);
});

test('linear is the default interp', () => {
  const s = createSampler(show({ mouth: [{ t: 0.0, open: 0.0 }, { t: 2.0, open: 1.0 }] }), 'lead');
  almost(s.at(0.5).mouth.open, 0.25);
});

test('step holds the first value until the next keyframe', () => {
  const s = createSampler(show({ locomotion: [{ t: 0.0, vx: 0.0, interp: 'step' }, { t: 2.0, vx: 1.0 }] }), 'lead');
  almost(s.at(0.0).locomotion.vx, 0.0);
  almost(s.at(1.0).locomotion.vx, 0.0);
  almost(s.at(1.999).locomotion.vx, 0.0);
  almost(s.at(2.0).locomotion.vx, 1.0);
});

test('smooth: midpoint 0.5, quarter point is smoothstep(0.25) = 0.15625, endpoints exact', () => {
  let s = createSampler(show({ head: [{ t: 0.0, head_pitch: 0.0, interp: 'smooth' }, { t: 2.0, head_pitch: 1.0 }] }), 'lead');
  almost(s.at(1.0).head.head_pitch, smoothstep(0.5));
  almost(s.at(1.0).head.head_pitch, 0.5);
  s = createSampler(show({ head: [{ t: 0.0, head_pitch: 0.0, interp: 'smooth' }, { t: 4.0, head_pitch: 1.0 }] }), 'lead');
  almost(s.at(1.0).head.head_pitch, smoothstep(0.25));
  almost(s.at(1.0).head.head_pitch, 0.15625); // 3*0.25^2 - 2*0.25^3, pinned literal
  assert.ok(Math.abs(s.at(1.0).head.head_pitch - 0.25) > 1e-3, 'would be 0.25 if linear');
  s = createSampler(show({ head: [{ t: 0.0, head_pitch: 0.3, interp: 'smooth' }, { t: 2.0, head_pitch: 0.9 }] }), 'lead');
  almost(s.at(0.0).head.head_pitch, 0.3);
  almost(s.at(2.0).head.head_pitch, 0.9);
});

test('unrecognized interp falls back to linear (sampler never throws)', () => {
  const s = createSampler(show({ mouth: [{ t: 0.0, open: 0.0, interp: 'bogus' }, { t: 2.0, open: 1.0 }] }), 'lead');
  almost(s.at(1.0).mouth.open, 0.5);
});

test('hold before the first and after the last keyframe', () => {
  let s = createSampler(show({ mouth: [{ t: 5.0, open: 0.7 }] }), 'lead');
  almost(s.at(0.0).mouth.open, 0.7);
  almost(s.at(4.999).mouth.open, 0.7);
  s = createSampler(show({ mouth: [{ t: 1.0, open: 0.2 }, { t: 2.0, open: 0.9 }] }, { duration: 100.0 }), 'lead');
  almost(s.at(2.0).mouth.open, 0.9);
  almost(s.at(50.0).mouth.open, 0.9);
});

test('locomotion is zeroed at and after meta.duration, never before', () => {
  const s = createSampler(show({ locomotion: [{ t: 0.0, vx: 0.2 }, { t: 1.0, vx: 0.2 }] }, { duration: 5.0 }), 'lead');
  almost(s.at(4.999).locomotion.vx, 0.2);
  assert.deepEqual(s.at(5.0).locomotion, { vx: 0, vy: 0, vyaw: 0 });
  assert.deepEqual(s.at(10.0).locomotion, { vx: 0, vy: 0, vyaw: 0 });
});

test('without meta.duration locomotion is held, not zeroed', () => {
  const doc = show({ locomotion: [{ t: 0.0, vx: 0.2 }] });
  delete doc.meta.duration;
  const s = createSampler(doc, 'lead');
  almost(s.at(1000).locomotion.vx, 0.2);
});

test('missing tracks yield null (the duck defaults rule)', () => {
  const f = createSampler(show({ mouth: [{ t: 0.0, open: 0.5 }] }), 'lead').at(0.0);
  assert.equal(f.locomotion, null);
  assert.equal(f.head, null);
  assert.equal(f.pose, null);
  assert.notEqual(f.mouth, null);
  const g = createSampler(show({}), 'nobody').at(0.0);
  assert.deepEqual(g, { t: 0, locomotion: null, head: null, pose: null, mouth: null });
});

test('pose.active always steps, even with smooth interp on the same keyframe', () => {
  const s = createSampler(show({
    pose: [{ t: 0.0, z: 0.0, active: true, interp: 'smooth' }, { t: 2.0, z: 1.0, active: false }],
  }), 'lead');
  assert.equal(s.at(0.0).pose.active, true);
  assert.equal(s.at(1.0).pose.active, true);
  assert.equal(s.at(1.999).pose.active, true);
  assert.equal(s.at(2.0).pose.active, false);
  almost(s.at(1.0).pose.z, 0.5);
});

test('sparse keyframes default like Python (0.0 / false / linear)', () => {
  const s = createSampler(show({ head: [{ t: 0, head_yaw: 0.3 }], pose: [{ t: 0, z: -0.01 }] }), 'lead');
  assert.deepEqual(s.at(0).head, { neck_pitch: 0, head_pitch: 0, head_yaw: 0.3, head_roll: 0 });
  assert.deepEqual(s.at(0).pose, { z: -0.01, roll: 0, pitch: 0, active: false });
});

const withEvents = () => show({ events: [{ t: 1.0, sound: 'chirp' }, { t: 2.0, do: 'kick_left' }, { t: 2.0, sound: 'peck' }] });

test('eventsBetween: (t0, t1] tick-edge semantics', () => {
  const s = createSampler(withEvents(), 'lead');
  let fired = s.eventsBetween(0.5, 1.0);
  assert.equal(fired.length, 1);
  assert.equal(fired[0].t, 1.0);
  assert.deepEqual(s.eventsBetween(1.0, 1.0), []);
  fired = s.eventsBetween(1.0, 2.0);
  assert.equal(fired.length, 2);
  assert.deepEqual(new Set(fired.map((e) => e.t)), new Set([2.0]));
  assert.deepEqual(s.eventsBetween(1.5, 3.0).map((e) => e.t), [2.0, 2.0]); // late join skips t=1.0
});

test('eventsBetween sorts by t and keeps document indices', () => {
  const s = createSampler(show({ events: [{ t: 2.0, sound: 'chirp' }, { t: 1.0, sound: 'coo' }] }), 'lead');
  const fired = s.eventsBetween(0, 3);
  assert.deepEqual(fired.map((e) => [e.t, e.index]), [[1.0, 1], [2.0, 0]]);
});

test('modeAt: latest mode event with t <= given time', () => {
  let s = createSampler(show({ events: [{ t: 5.0, mode: 'roller' }] }), 'lead');
  assert.equal(s.modeAt(0.0), null);
  assert.equal(s.modeAt(4.999), null);
  s = createSampler(show({ events: [{ t: 5.0, mode: 'roller' }, { t: 10.0, mode: 'legs' }] }), 'lead');
  assert.equal(s.modeAt(5.0), 'roller');
  assert.equal(s.modeAt(7.0), 'roller');
  assert.equal(s.modeAt(10.0), 'legs');
  assert.equal(s.modeAt(100.0), 'legs');
  s = createSampler(show({ events: [{ t: 1.0, sound: 'chirp' }, { t: 2.0, mode: 'roller' }] }), 'lead');
  assert.equal(s.modeAt(1.5), null);
  assert.equal(s.modeAt(2.0), 'roller');
});

test('servoAt: [t, t+duration) window; without duration until the next entry', () => {
  let s = createSampler(show({ servo: [{ t: 5.0, mode: 'hold', duration: 2.0 }] }), 'lead');
  assert.equal(s.servoAt(4.999), null);
  assert.notEqual(s.servoAt(5.0), null);
  assert.notEqual(s.servoAt(6.999), null);
  assert.equal(s.servoAt(7.0), null);
  s = createSampler(show({ servo: [{ t: 1.0 }, { t: 3.0, mode: 'laser_homing', duration: 1.0 }] }), 'lead');
  assert.equal(s.servoAt(2.0).mode, 'hold'); // default mode
  assert.equal(s.servoAt(3.5).mode, 'laser_homing');
  assert.equal(s.servoAt(4.0), null);
});

test('demo show samples across the whole duration without error, at 50 Hz', () => {
  const doc = parseShow(readFileSync(DEMO, 'utf8'));
  const norm = normalizeShow(doc);
  for (const role of norm.roleNames()) {
    const s = createSampler(doc, role);
    for (let i = 0; i * 0.02 <= norm.meta.duration + 1.0; i++) s.at(i * 0.02);
  }
  // A couple of pinned demo values (lead locomotion: linear 0 -> 0.1 over 3.0..3.5, step at 3.5).
  const lead = createSampler(doc, 'lead');
  almost(lead.at(3.25).locomotion.vx, 0.05);
  almost(lead.at(4.5).locomotion.vx, 0.1);
  almost(lead.at(5.75).locomotion.vx, 0.05);
  assert.deepEqual(lead.at(20.0).locomotion, { vx: 0, vy: 0, vyaw: 0 });
});
