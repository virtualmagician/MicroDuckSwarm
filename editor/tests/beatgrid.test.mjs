// Beat grid helpers and the Python-compatible number/string formatting the
// validator messages depend on.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { beatTimes, beatPeriod, showBeatTimes, snap, pyFloatStr, pyReprStr, pyRepr, trackFields, DEFAULT_LIMITS } from '../duckshow-core.js';

const round6 = (xs) => xs.map((x) => Math.round(x * 1e6) / 1e6);

test('beatTimes at 120 bpm: every 0.5 s within [0, duration]', () => {
  assert.deepEqual(beatTimes(120, 0, 2), [0, 0.5, 1, 1.5, 2]);
  assert.deepEqual(beatTimes(120, 0.25, 2), [0.25, 0.75, 1.25, 1.75]);
  assert.deepEqual(beatTimes(120, 0, 2, 2), [0, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]);
});

test('beatTimes includes beats before the first downbeat when beat_offset > period', () => {
  assert.deepEqual(beatTimes(60, 1.5, 3), [0.5, 1.5, 2.5]);
  assert.deepEqual(beatTimes(60, -0.25, 2), [0.75, 1.75]);
});

test('beatTimes is empty for bad input and free of float noise', () => {
  assert.deepEqual(beatTimes(0, 0, 10), []);
  assert.deepEqual(beatTimes(120, 0, -1), []);
  assert.deepEqual(beatTimes(NaN, 0, 10), []);
  assert.deepEqual(beatTimes(120, 0, Infinity), []);
  assert.deepEqual(beatTimes(100, 0, 3), round6(beatTimes(100, 0, 3))); // 0.6 * k stays clean
  assert.equal(beatPeriod(120), 0.5);
  assert.ok(Number.isNaN(beatPeriod(0)));
});

test('showBeatTimes reads meta.music', () => {
  const doc = { format: 'duckshow/1', meta: { duration: 1, music: { bpm: 240, beat_offset: 0.1 } }, cast: [], tracks: {} };
  assert.deepEqual(showBeatTimes(doc), [0.1, 0.35, 0.6, 0.85]);
  assert.deepEqual(showBeatTimes(doc, 2), [0.1, 0.225, 0.35, 0.475, 0.6, 0.725, 0.85, 0.975]);
  assert.deepEqual(showBeatTimes({ format: 'duckshow/1', meta: { duration: 1 }, cast: [], tracks: {} }), []);
});

test('snap picks the nearest grid time, ties go down, empty grid is identity', () => {
  const grid = beatTimes(120, 0, 2);
  assert.equal(snap(0.7, grid), 0.5);
  assert.equal(snap(0.8, grid), 1.0);
  assert.equal(snap(0.75, grid), 0.5);
  assert.equal(snap(-1, grid), 0);
  assert.equal(snap(9, grid), 2);
  assert.equal(snap(0.7, []), 0.7);
  assert.equal(snap(0.7, null), 0.7);
});

test('pyFloatStr prints floats exactly like CPython str(float)', () => {
  const cases = [
    [0, '0.0'], [-0, '-0.0'], [1, '1.0'], [2, '2.0'], [0.5, '0.5'], [1.1, '1.1'], [100, '100.0'], [-3, '-3.0'],
    [0.25, '0.25'], [1.2, '1.2'], [0.05, '0.05'], [12345.678, '12345.678'], [0.0001, '0.0001'],
    [1e-5, '1e-05'], [1.5e-7, '1.5e-07'], [1e16, '1e+16'], [1.2345678901234568e17, '1.2345678901234568e+17'],
    [9999999999999998, '9999999999999998.0'], [0.30000000000000004, '0.30000000000000004'],
    [Infinity, 'inf'], [-Infinity, '-inf'], [NaN, 'nan'], [-1.2000000001, '-1.2000000001'],
  ];
  for (const [x, s] of cases) assert.equal(pyFloatStr(x), s, `pyFloatStr(${x})`);
});

test('pyReprStr / pyRepr follow CPython repr()', () => {
  assert.equal(pyReprStr('lead'), "'lead'");
  assert.equal(pyReprStr("it's"), '"it\'s"');
  assert.equal(pyReprStr('say "hi"'), "'say \"hi\"'");
  assert.equal(pyReprStr('both \' and "'), "'both \\' and \"'");
  assert.equal(pyReprStr('a\\b\n\t\x01'), "'a\\\\b\\n\\t\\x01'");
  assert.equal(pyRepr(null), 'None');
  assert.equal(pyRepr(true), 'True');
  assert.equal(pyRepr(5), '5');
  assert.equal(pyRepr(5.5), '5.5');
  assert.equal(pyRepr(['do', 'sound']), "['do', 'sound']");
  assert.equal(pyRepr({ a: 1 }), "{'a': 1}");
});

test('trackFields derives editing ranges from the limits table', () => {
  const f = trackFields(DEFAULT_LIMITS);
  // Derived from DEFAULT_LIMITS rather than literals: the point of this test
  // is that trackFields READS the table, so hard-coding the table's current
  // values here just makes it break on every retune without testing more.
  const L = DEFAULT_LIMITS;
  assert.deepEqual(f.locomotion.map((x) => [x.name, x.min, x.max]), [
    ['vx', -L.max_abs_vx, L.max_abs_vx],
    ['vy', -L.max_abs_vy, L.max_abs_vy],
    ['vyaw', -L.max_abs_vyaw, L.max_abs_vyaw],
  ]);
  assert.deepEqual(f.head.map((x) => x.name), ['neck_pitch', 'head_pitch', 'head_yaw', 'head_roll']);
  assert.equal(f.head[0].max, L.max_abs_head_angle);
  assert.deepEqual(f.pose.map((x) => x.name), ['z', 'roll', 'pitch', 'active']);
  assert.equal(f.pose[3].bool, true);
  assert.deepEqual(f.mouth, [{ name: 'open', min: 0, max: 1, unit: '' }]);
});
