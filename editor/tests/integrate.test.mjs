// Dead reckoning: trunk-frame velocities (x forward, y left, heading CCW).
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { integrate } from '../duckshow-core.js';

function show(locomotion, extra = {}, duration = 2.0) {
  return {
    format: 'duckshow/1',
    meta: { duration },
    requires: { policies: [] },
    cast: [{ role: 'lead' }],
    tracks: { lead: { locomotion, ...extra } },
  };
}
const almost = (a, b, tol = 1e-9) => assert.ok(Math.abs(a - b) < tol, `${a} !~ ${b}`);

test('straight walk: vx=0.1 for 2 s -> x = 0.2, y = 0, heading 0', () => {
  const path = integrate(show([{ t: 0, vx: 0.1 }, { t: 2, vx: 0.1 }]), 'lead');
  assert.equal(path.length, 101); // t = 0, 0.02, ..., 2.0
  assert.deepEqual(path[0], { t: 0, x: 0, y: 0, heading: 0 });
  const end = path[path.length - 1];
  almost(end.t, 2.0);
  almost(end.x, 0.2);
  almost(end.y, 0);
  almost(end.heading, 0);
  almost(path[50].x, 0.1); // halfway
});

test('strafe: vy=0.1 for 2 s moves 0.2 to the left (+y in the world frame)', () => {
  const end = integrate(show([{ t: 0, vy: 0.1 }]), 'lead').at(-1);
  almost(end.x, 0);
  almost(end.y, 0.2);
});

test('pure turn: vyaw=1 for 2 s -> heading 2 rad, no displacement', () => {
  const end = integrate(show([{ t: 0, vyaw: 1.0 }, { t: 2, vyaw: 1.0 }]), 'lead').at(-1);
  almost(end.heading, 2.0);
  almost(end.x, 0);
  almost(end.y, 0);
});

test('walk after a quarter turn moves along +y', () => {
  // turn left at pi/2 rad/s for 1 s (step), then walk forward 1 s at 0.1 m/s
  const path = integrate(show([
    { t: 0, vyaw: Math.PI / 2, interp: 'step' },
    { t: 1, vx: 0.1, vyaw: 0, interp: 'step' },
  ]), 'lead');
  const end = path.at(-1);
  almost(end.heading, Math.PI / 2, 1e-9);
  almost(end.x, 0, 1e-9);
  almost(end.y, 0.1, 1e-9);
});

test('walk while turning traces an arc close to the analytic circle', () => {
  // v = 0.1 m/s, omega = 0.5 rad/s for 2 s: radius 0.2, angle 1 rad.
  const end = integrate(show([{ t: 0, vx: 0.1, vyaw: 0.5 }]), 'lead', 0.02).at(-1);
  const r = 0.2;
  almost(end.x, r * Math.sin(1.0), 1e-4);
  almost(end.y, r * (1 - Math.cos(1.0)), 1e-4);
  almost(end.heading, 1.0);
});

test('start mark offsets position and heading', () => {
  const end = integrate(show([{ t: 0, vx: 0.1 }]), 'lead', 0.02, { start: { x: 1, y: 2, heading: Math.PI } }).at(-1);
  almost(end.x, 0.8);
  almost(end.y, 2, 1e-9);
  almost(end.heading, Math.PI);
});

test('locomotion after meta.duration never moves the duck; tEnd can extend the path', () => {
  const doc = show([{ t: 0, vx: 0.1 }], {}, 1.0);
  const end = integrate(doc, 'lead', 0.02, { tEnd: 3.0 }).at(-1);
  almost(end.t, 3.0);
  almost(end.x, 0.1); // only the first second counts
});

test('servo hold window freezes locomotion', () => {
  const doc = show([{ t: 0, vx: 0.1 }], { servo: [{ t: 0.5, mode: 'hold', duration: 1.0 }] });
  const end = integrate(doc, 'lead').at(-1);
  almost(end.x, 0.1); // 2 s minus a 1 s hold
});

test('no locomotion track / no duration -> a single start point', () => {
  assert.deepEqual(integrate(show([]), 'lead'), [{ t: 0, x: 0, y: 0, heading: 0 }].concat(Array.from({ length: 100 }, (_, i) => ({ t: Math.min(2, (i + 1) * 0.02), x: 0, y: 0, heading: 0 }))));
  const doc = show([{ t: 0, vx: 0.1 }]);
  delete doc.meta.duration;
  assert.deepEqual(integrate(doc, 'lead'), [{ t: 0, x: 0, y: 0, heading: 0 }]);
});

test('non-multiple durations end exactly at tEnd', () => {
  const path = integrate(show([{ t: 0, vx: 0.1 }], {}, 0.05), 'lead', 0.02);
  assert.deepEqual(path.map((p) => Math.round(p.t * 1e6) / 1e6), [0, 0.02, 0.04, 0.05]);
  almost(path.at(-1).x, 0.005);
});
