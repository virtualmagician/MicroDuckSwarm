// panCamera(): right-drag panning of the stage camera (docs/viewer.md "Camera").
//
// Expected values are computed by hand from the contract, not read back from
// the function: perPx = 2 * distance * tan(fovY / 2) / viewportHeight, the
// target moves by -dx * perPx along the camera's right vector and +dy * perPx
// along its up vector. The house-preset cases below reproduce, to four
// decimals, what a 100 px drag measured in a real browser.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { CAMERA_PRESETS, DEG2RAD, PAN_MIN_EYE_HEIGHT_M, cameraEyePosition, mat4LookAt, panCamera } from '../viewer-gl.js';

const near = (a, b, eps = 1e-4) => assert.ok(Math.abs(a - b) < eps, `${a} !~ ${b}`);
const H = 500;
const house = CAMERA_PRESETS.house;
const perPx = (2 * house.distance * Math.tan(house.fovY / 2)) / H;

test('panCamera: a right-drag moves the house target along -x by dx * perPx', () => {
  // House: azimuth 0, eye on +z looking toward -z, so camera-right is +x and
  // dragging right moves the target the other way, so the scene follows.
  const out = panCamera(house, 100, 0, H);
  near(out.target[0], -100 * perPx);
  near(out.target[1], house.target[1]);
  near(out.target[2], 0);
  near(out.target[0], -0.5468, 1e-3); // the number the browser measured
});

test('panCamera: a down-drag moves along the camera up vector, tilted by the elevation', () => {
  // up' = (0, cos(el), -sin(el)) for the house camera at 15 degrees.
  const out = panCamera(house, 0, 100, H);
  const d = 100 * perPx;
  near(out.target[0], 0);
  near(out.target[1], house.target[1] + d * Math.cos(house.elevation));
  near(out.target[2], -d * Math.sin(house.elevation));
  near(out.target[1], 0.6881, 1e-3); // the number the browser measured
});

test('panCamera: leaves every other camera field untouched and never mutates its input', () => {
  const before = JSON.stringify(house);
  const out = panCamera(house, 37, -12, H);
  assert.equal(JSON.stringify(house), before);
  for (const k of ['azimuth', 'elevation', 'distance', 'fovY', 'up']) assert.deepEqual(out[k], house[k]);
});

test('panCamera: the reach clamp keeps the target within 12 m of the stage centre', () => {
  const out = panCamera(house, 1e6, 0, H);
  near(Math.hypot(out.target[0], out.target[2]), 12, 1e-9);
  const custom = panCamera(house, 1e6, 0, H, 3);
  near(Math.hypot(custom.target[0], custom.target[2]), 3, 1e-9);
});

test('panCamera: an upward drag stops when the EYE reaches the floor, not the target', () => {
  // The house up vector leans 15 degrees toward the viewer, so raising the
  // scene lowers the target. A clamp on the target at y=0 stopped that after
  // 30 px; the eye still had most of a metre above the floor.
  const out = panCamera(house, 0, -1e6, H);
  near(out.target[1], PAN_MIN_EYE_HEIGHT_M - house.distance * Math.sin(house.elevation));
  near(cameraEyePosition(out)[1], PAN_MIN_EYE_HEIGHT_M);
  assert.ok(out.target[1] < 0, 'the target may go below the floor while the eye stays above it');
});

test('panCamera: a modest upward drag in the house view is not clamped at all', () => {
  // 100 px up at a 500 px panel lowers the target by about 0.53 m, well
  // inside the 0.80 m the eye height allows.
  const out = panCamera(house, 0, -100, H);
  near(out.target[1], house.target[1] - 100 * perPx * Math.cos(house.elevation));
});

test('panCamera: the ceiling keeps the target at or below 3 m', () => {
  assert.equal(panCamera(house, 0, 1e6, H).target[1], 3);
});

test('panCamera: scales with the viewport so a drag covers the same screen distance at any size', () => {
  const small = panCamera(house, 100, 0, 250).target[0];
  const large = panCamera(house, 100, 0, 1000).target[0];
  near(small / large, 4);
});

test('panCamera: the top preset (up = z) pans in the floor plane and stays finite', () => {
  const top = CAMERA_PRESETS.top;
  const out = panCamera(top, 100, 100, H);
  assert.ok(out.target.every(Number.isFinite), out.target);
  assert.notEqual(out.target[0], 0, 'a horizontal drag must move x');
  assert.ok(out.target[2] > 0, 'a downward drag moves the target toward +z in the top view');
  assert.ok(Math.abs(out.target[1]) < 0.02, 'the near-vertical up vector has a 1% y component: a centimetre, not a clamp');
});

test('panCamera: looking straight down with up = y is degenerate and must still be finite', () => {
  // Unreachable from the editor (orbit clamps at 89 degrees; the top preset
  // carries up = z) but a legal camera for a pure function.
  const cam = { ...house, elevation: 90 * DEG2RAD, up: [0, 1, 0] };
  const out = panCamera(cam, 100, 100, H);
  assert.ok(out.target.every(Number.isFinite), out.target);
  assert.ok(Math.hypot(out.target[0] - cam.target[0], out.target[2] - cam.target[2]) > 0, 'the pan must move something');
});

test('panCamera: in the degenerate case the pan moves along the axes the view matrix uses', () => {
  // A right-drag moves the target along -right, where right is the view
  // matrix's own x row. If pan and mat4LookAt chose different fallback axes,
  // the scene would slide diagonally to the drag.
  const cam = { ...house, elevation: 90 * DEG2RAD, up: [0, 1, 0] };
  const view = mat4LookAt(cameraEyePosition(cam), cam.target, cam.up);
  const right = [view[0], view[4], view[8]];
  const out = panCamera(cam, 100, 0, H);
  const moved = [out.target[0] - cam.target[0], out.target[1] - cam.target[1], out.target[2] - cam.target[2]];
  const len = Math.hypot(...moved);
  assert.ok(len > 0);
  const dot = (moved[0] * right[0] + moved[1] * right[1] + moved[2] * right[2]) / len;
  near(dot, -1, 1e-6);
});
