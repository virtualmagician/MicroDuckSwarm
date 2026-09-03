// Baked pose-cache parsing, validation, hash-mismatch detection, and
// frame sampling (docs/bake-format.md; docs/viewer.md Feature A "Create
// Preview"). No fixture .duckbake.json is committed to this repo (the
// real one lives outside it, produced by tools/bake) — every case here is
// built inline.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  BAKE_FORMAT, isBakeCache, validateBakeCache, sha256Hex, checkShowHashMatch,
  frameCountFor, poseAtTime, poseAllAtTime, bakeTrail, summarize,
} from '../bake-cache.js';

const POSE_FIELDS = ['x', 'y', 'heading', 'headYaw', 'headPitch', 'headRoll', 'neckPitch', 'bodyZ', 'bodyRoll', 'bodyPitch', 'mouthOpen', 'walkPhase'];

/** A minimal-but-complete duckbake/1 fixture: one role, `n` frames at `frameRate`, x walking at 1 unit/frame so interpolation is easy to check by hand. */
function fixtureCache({ role = 'lead', n = 5, frameRate = 50, showSha = 'deadbeef'.repeat(8) } = {}) {
  const poses = {};
  for (const field of POSE_FIELDS) {
    poses[field] = Array.from({ length: n }, (_, i) => (field === 'x' ? i : field === 'walkPhase' ? i * 0.1 : 0));
  }
  return {
    format: BAKE_FORMAT,
    cache_key: 'irrelevant-to-playback',
    show: { path: 'shows/fixture.duckshow.json', sha256: showSha, name: 'Fixture', duration: n / frameRate },
    frame_rate: frameRate,
    roles: [role],
    unsimulated_roles: [],
    fallen_roles: [],
    poses: { [role]: poses },
    log: [],
  };
}

test('isBakeCache: format tag only, no throw on garbage', () => {
  assert.equal(isBakeCache(fixtureCache()), true);
  assert.equal(isBakeCache({ format: 'duckbake/2' }), false);
  assert.equal(isBakeCache(null), false);
  assert.equal(isBakeCache('not an object'), false);
  assert.equal(isBakeCache(42), false);
});

test('validateBakeCache accepts a well-formed cache and returns it unchanged', () => {
  const cache = fixtureCache();
  assert.equal(validateBakeCache(cache), cache);
});

test('validateBakeCache rejects the wrong format tag', () => {
  const cache = fixtureCache();
  cache.format = 'duckbake/2';
  assert.throws(() => validateBakeCache(cache), /unrecognised format/);
});

test('validateBakeCache rejects a missing show.sha256', () => {
  const cache = fixtureCache();
  delete cache.show.sha256;
  assert.throws(() => validateBakeCache(cache), /show\.sha256/);
});

test('validateBakeCache rejects a missing/invalid frame_rate', () => {
  const a = fixtureCache(); delete a.frame_rate;
  assert.throws(() => validateBakeCache(a), /frame_rate/);
  const b = fixtureCache(); b.frame_rate = 0;
  assert.throws(() => validateBakeCache(b), /frame_rate/);
});

test('validateBakeCache rejects an empty or missing roles[]', () => {
  const a = fixtureCache(); a.roles = [];
  assert.throws(() => validateBakeCache(a), /roles/);
  const b = fixtureCache(); delete b.roles;
  assert.throws(() => validateBakeCache(b), /roles/);
});

test('validateBakeCache rejects a role with no matching poses entry', () => {
  const cache = fixtureCache({ role: 'lead' });
  cache.roles.push('ghost');
  assert.throws(() => validateBakeCache(cache), /poses\.ghost missing/);
});

test('validateBakeCache rejects a role missing one pose field', () => {
  const cache = fixtureCache();
  delete cache.poses.lead.bodyZ;
  assert.throws(() => validateBakeCache(cache), /poses\.lead\.bodyZ missing/);
});

test('validateBakeCache rejects mismatched array lengths within one role', () => {
  const cache = fixtureCache({ n: 5 });
  cache.poses.lead.y = [0, 0, 0]; // shorter than x
  assert.throws(() => validateBakeCache(cache), /length/);
});

test('validateBakeCache ignores unknown/extra fields (show-file-compatibility convention)', () => {
  const cache = fixtureCache();
  cache.bake = { engine: { python: '3.12.13' } };
  cache.someFutureField = 'whatever';
  assert.equal(validateBakeCache(cache), cache);
});

test('sha256Hex matches the standard test vector for "abc"', async () => {
  assert.equal(await sha256Hex('abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
});

test('sha256Hex matches the standard test vector for the empty string', async () => {
  assert.equal(await sha256Hex(''), 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
});

test('checkShowHashMatch: ok when the cache\'s recorded hash matches the loaded show text', async () => {
  const showText = '{"format":"duckshow/1","meta":{"duration":1}}';
  const hash = await sha256Hex(showText);
  const cache = fixtureCache({ showSha: hash });
  const result = await checkShowHashMatch(cache, showText);
  assert.equal(result.ok, true);
  assert.equal(result.actual, hash);
  assert.equal(result.expected, hash);
});

test('checkShowHashMatch: plainly reports a mismatch rather than throwing', async () => {
  const cache = fixtureCache({ showSha: 'not-the-real-hash' });
  const result = await checkShowHashMatch(cache, '{"different":"show"}');
  assert.equal(result.ok, false);
  assert.equal(result.expected, 'not-the-real-hash');
  assert.notEqual(result.actual, result.expected);
});

test('checkShowHashMatch: even a single-byte edit to the show text is caught', async () => {
  const showText = '{"format":"duckshow/1","meta":{"duration":1}}';
  const hash = await sha256Hex(showText);
  const cache = fixtureCache({ showSha: hash });
  const edited = showText.replace('1}}', '2}}'); // duration edited after loading/baking
  const result = await checkShowHashMatch(cache, edited);
  assert.equal(result.ok, false);
});

test('frameCountFor reports the recorded frame count, 0 for an unknown role', () => {
  const cache = fixtureCache({ n: 17 });
  assert.equal(frameCountFor(cache, 'lead'), 17);
  assert.equal(frameCountFor(cache, 'ghost'), 0);
});

test('poseAtTime: exact frame times return the recorded values untouched', () => {
  const cache = fixtureCache({ n: 5, frameRate: 50 }); // x = [0,1,2,3,4], one unit per frame (0.02s)
  const p0 = poseAtTime(cache, 'lead', 0);
  assert.equal(p0.role, 'lead');
  assert.equal(p0.x, 0);
  const p2 = poseAtTime(cache, 'lead', 2 / 50);
  assert.equal(p2.x, 2);
});

test('poseAtTime: interpolates linearly between adjacent recorded frames', () => {
  const cache = fixtureCache({ n: 5, frameRate: 50 });
  const half = poseAtTime(cache, 'lead', 1.5 / 50); // between frame 1 (x=1) and frame 2 (x=2)
  assert.ok(Math.abs(half.x - 1.5) < 1e-9);
  const quarter = poseAtTime(cache, 'lead', 1.25 / 50);
  assert.ok(Math.abs(quarter.x - 1.25) < 1e-9);
});

test('poseAtTime: holds flat before the first frame and after the last', () => {
  const cache = fixtureCache({ n: 5, frameRate: 50 });
  assert.equal(poseAtTime(cache, 'lead', -1).x, 0);
  assert.equal(poseAtTime(cache, 'lead', 1000).x, 4);
});

test('poseAtTime: every renderer-contract field is present on the returned pose', () => {
  const cache = fixtureCache();
  const pose = poseAtTime(cache, 'lead', 0);
  for (const field of POSE_FIELDS) assert.ok(field in pose, `missing ${field}`);
});

test('poseAtTime returns null for a role the cache never recorded', () => {
  const cache = fixtureCache({ role: 'lead' });
  assert.equal(poseAtTime(cache, 'ghost', 0), null);
});

test('poseAllAtTime returns one pose per cache role, in cache.roles order', () => {
  const cache = fixtureCache({ role: 'lead' });
  cache.roles.push('wing');
  cache.poses.wing = cache.poses.lead;
  const all = poseAllAtTime(cache, 0);
  assert.deepEqual(all.map((p) => p.role), ['lead', 'wing']);
});

test('bakeTrail: brightest point is last (current position), length capped at maxPoints', () => {
  const cache = fixtureCache({ n: 500, frameRate: 50 });
  const trail = bakeTrail(cache, 'lead', 10 /* seconds: well past the 10s of recorded data */, 50);
  // Same downsample-by-stride algorithm as duckshow-viewer.js's own
  // deriveTrail() (deliberately mirrored, see this module's doc comment):
  // the final point is always appended even when the stride overshoots
  // it, so the guarantee is "at most maxPoints+1", not an exact cap.
  assert.ok(trail.length <= 51);
  assert.ok(trail.length > 1);
  assert.equal(trail[trail.length - 1].brightness, 1);
  assert.ok(trail[0].brightness < trail[trail.length - 1].brightness);
});

test('bakeTrail: unknown role returns an empty trail, not a throw', () => {
  const cache = fixtureCache({ role: 'lead' });
  assert.deepEqual(bakeTrail(cache, 'ghost', 1), []);
});

test('summarize reports duration, unsimulated and fallen roles', () => {
  const cache = fixtureCache();
  cache.unsimulated_roles = ['wren'];
  cache.fallen_roles = ['reed'];
  const s = summarize(cache);
  assert.equal(s.roles, 1);
  assert.equal(s.duration, cache.show.duration);
  assert.deepEqual(s.unsimulatedRoles, ['wren']);
  assert.deepEqual(s.fallenRoles, ['reed']);
});
