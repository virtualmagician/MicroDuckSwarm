// ribbonVertices(): the intent path as a flat strip on the floor
// (docs/viewer.md "Intent curves, and the drift diff").
//
// Layout under test is the renderer's own: 16 bytes per vertex, position xyz
// then brightness, two vertices per path point, TRIANGLE_STRIP order (left,
// right, left, right, ...). Show space is {x forward, y left}; render space
// is (y, height, x).
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { PATH_RIBBON_HALF_WIDTH_M, ribbonVertices } from '../viewer-gl.js';

const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} !~ ${b}`);
const vertex = (data, i) => ({ x: data[i * 4 + 2], y: data[i * 4 + 0], h: data[i * 4 + 1], b: data[i * 4 + 3] });

test('ribbonVertices: two vertices per point, offset perpendicular to the path by the half width', () => {
  // Straight along +x (show forward): the perpendicular is +y (show left).
  const data = ribbonVertices([{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 2, y: 0 }], 0.01, 0.002, 0.5);
  assert.equal(data.length, 3 * 2 * 4);
  const left = vertex(data, 0), right = vertex(data, 1);
  near(left.x, 0); near(left.y, 0.01);
  near(right.x, 0); near(right.y, -0.01);
  near(left.h, 0.002); near(left.b, 0.5);
  const lastLeft = vertex(data, 4);
  near(lastLeft.x, 2); near(lastLeft.y, 0.01, 1e-9);
});

test('ribbonVertices: the offset turns with the path', () => {
  // A path heading +y (show left): the perpendicular is -x.
  const data = ribbonVertices([{ x: 0, y: 0 }, { x: 0, y: 1 }], 0.01);
  const left = vertex(data, 0), right = vertex(data, 1);
  near(left.x, -0.01); near(left.y, 0);
  near(right.x, 0.01); near(right.y, 0);
  assert.equal(PATH_RIBBON_HALF_WIDTH_M, 0.01, 'the default half width is 1 cm');
});

test('ribbonVertices: a duplicate point mid-path reuses the previous direction rather than a zero normal', () => {
  const data = ribbonVertices([{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 0 }, { x: 2, y: 0 }], 0.01);
  assert.equal(data.length, 4 * 2 * 4);
  assert.ok([...data].every(Number.isFinite), 'no NaN from a zero-length segment');
  const dup = vertex(data, 4); // the repeated point's left vertex
  near(dup.x, 1); near(dup.y, 0.01);
});

test('ribbonVertices: leading duplicates look ahead for the first real direction', () => {
  const data = ribbonVertices([{ x: 0, y: 0 }, { x: 0, y: 0 }, { x: 1, y: 0 }], 0.01);
  assert.ok([...data].every(Number.isFinite));
  near(vertex(data, 0).y, 0.01);
});

test('ribbonVertices: fewer than two distinct points yields nothing to draw', () => {
  assert.equal(ribbonVertices([], 0.01).length, 0);
  assert.equal(ribbonVertices([{ x: 1, y: 1 }], 0.01).length, 0);
  assert.equal(ribbonVertices([{ x: 1, y: 1 }, { x: 1, y: 1 }, { x: 1, y: 1 }], 0.01).length, 0);
  assert.equal(ribbonVertices(null, 0.01).length, 0);
});

test('ribbonVertices: missing coordinates read as 0, the way the old line builder treated them', () => {
  const data = ribbonVertices([{}, { x: 1 }], 0.01);
  assert.equal(data.length, 2 * 2 * 4);
  assert.ok([...data].every(Number.isFinite));
});
