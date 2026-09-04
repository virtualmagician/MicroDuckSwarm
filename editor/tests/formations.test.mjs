// Formation helpers for setup mode (docs/authoring.md "4 · Setup mode").
// Pure geometry: role names in, {role: {x, y, heading}} out, no show document
// and no DOM. Show-space convention throughout: +x downstage, +y house-left,
// heading radians CCW with 0 facing downstage.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { formationMarks, FORMATION_KINDS, FORMATION_FACINGS } from '../duckshow-core.js';

const near = (a, b, eps = 1e-6) => Math.abs(a - b) < eps;

test('formationMarks: empty cast returns an empty object, never throws', () => {
  assert.deepEqual(formationMarks([], 'line'), {});
  assert.deepEqual(formationMarks(null, 'line'), {});
  assert.deepEqual(formationMarks(['a', null, '', 'b'], 'line', { spacing: 1 }).a.y, 0.5);
});

test('formationMarks: line is centred, so an even cast straddles the centre line', () => {
  const m = formationMarks(['a', 'b', 'c', 'd'], 'line', { spacing: 0.5 });
  assert.deepEqual(Object.keys(m), ['a', 'b', 'c', 'd']);
  assert.deepEqual([m.a.y, m.b.y, m.c.y, m.d.y], [0.75, 0.25, -0.25, -0.75]);
  // sums to zero: the formation's centre of mass is the origin
  assert.ok(near(m.a.y + m.b.y + m.c.y + m.d.y, 0));
  for (const r of Object.values(m)) assert.equal(r.x, 0);
});

test('formationMarks: an odd line puts one duck exactly on the centre line', () => {
  const m = formationMarks(['a', 'b', 'c'], 'line', { spacing: 0.4 });
  assert.equal(m.b.y, 0);
});

test('formationMarks: line honours x and a non-zero centre', () => {
  const m = formationMarks(['a', 'b'], 'line', { spacing: 1, x: 0.3, cy: 2 });
  assert.deepEqual([m.a.x, m.b.x], [0.3, 0.3]);
  assert.deepEqual([m.a.y, m.b.y], [2.5, 1.5]);
});

test('formationMarks: arc places every duck at the requested radius', () => {
  const m = formationMarks(['a', 'b', 'c', 'd'], 'arc', { radius: 1.5 });
  for (const r of Object.values(m)) assert.ok(near(Math.hypot(r.x, r.y), 1.5), `radius ${Math.hypot(r.x, r.y)}`);
});

test('formationMarks: a one-duck arc sits at the middle of the span, not an end', () => {
  const m = formationMarks(['solo'], 'arc', { radius: 2 });
  assert.ok(near(m.solo.x, 2));
  assert.ok(near(m.solo.y, 0));
});

test("formationMarks: facing 'centre' points every duck at the formation centre", () => {
  const m = formationMarks(['a', 'b', 'c'], 'arc', { radius: 1, facing: 'centre' });
  for (const r of Object.values(m)) {
    // walking one unit along the heading must reduce distance to the centre
    const moved = Math.hypot(r.x + Math.cos(r.heading) * 0.1, r.y + Math.sin(r.heading) * 0.1);
    assert.ok(moved < Math.hypot(r.x, r.y), 'heading should point inward');
  }
});

test("formationMarks: facing 'front' is heading 0, the default", () => {
  const a = formationMarks(['a', 'b'], 'arc', { radius: 1 });
  const b = formationMarks(['a', 'b'], 'arc', { radius: 1, facing: 'front' });
  assert.deepEqual(a, b);
  for (const r of Object.values(a)) assert.equal(r.heading, 0);
});

test("formationMarks: facing 'keep' preserves each role's existing heading", () => {
  const current = { a: { x: 9, y: 9, heading: 1.25 }, b: { x: 0, y: 0, heading: -0.5 } };
  const m = formationMarks(['a', 'b', 'c'], 'line', { facing: 'keep', current });
  assert.equal(m.a.heading, 1.25);
  assert.equal(m.b.heading, -0.5);
  assert.equal(m.c.heading, 0, 'a role with no current mark falls back to 0');
  assert.notEqual(m.a.x, 9, 'position is still rewritten; only heading is kept');
});

test('formationMarks: grid fills rows across y and stacks them upstage in -x', () => {
  const m = formationMarks(['a', 'b', 'c', 'd'], 'grid', { cols: 2, dx: 0.5, dy: 0.5 });
  // row 0 is downstage of row 1
  assert.ok(m.a.x > m.c.x, 'first row should be downstage of the second');
  assert.equal(m.a.x, m.b.x);
  assert.equal(m.c.x, m.d.x);
  assert.ok(m.a.y > m.b.y, '+y is house-left, filled left to right');
});

test('formationMarks: a short final grid row is re-centred, not left-aligned', () => {
  const m = formationMarks(['a', 'b', 'c', 'd', 'e'], 'grid', { cols: 2, dx: 0.4, dy: 0.4 });
  assert.equal(m.e.y, 0, 'the lone duck in the last row sits on the centre line');
});

test('formationMarks: grid defaults cols to a square-ish block', () => {
  const m = formationMarks(['a', 'b', 'c', 'd'], 'grid', {});
  const xs = new Set(Object.values(m).map((r) => r.x));
  assert.equal(xs.size, 2, '4 ducks should default to 2x2');
});

test('formationMarks: an unknown kind throws rather than silently producing a line', () => {
  assert.throws(() => formationMarks(['a'], 'circle'), /unknown formation/);
});

test('formationMarks: every advertised kind and facing actually works', () => {
  for (const kind of FORMATION_KINDS) {
    for (const facing of FORMATION_FACINGS) {
      const m = formationMarks(['a', 'b', 'c'], kind, { facing });
      assert.equal(Object.keys(m).length, 3, `${kind}/${facing}`);
      for (const r of Object.values(m)) {
        assert.ok(Number.isFinite(r.x) && Number.isFinite(r.y) && Number.isFinite(r.heading), `${kind}/${facing}`);
      }
    }
  }
});
