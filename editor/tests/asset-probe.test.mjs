// Asset-presence detection (docs/viewer.md Feature B: real meshes when
// available, silent primitive-duck fallback when not). Exercised with
// fake fetch-shaped probes — no network, no real assets/microduck/
// directory needed (it is gitignored and not present in CI).
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { detectAssetsAvailable, detectAllAvailable } from '../asset-probe.js';

const okResponse = async () => ({ ok: true, status: 200 });
const notFound = async () => ({ ok: false, status: 404 });
const networkFailure = async () => { throw new TypeError('Failed to fetch'); }; // real fetch() rejection shape
const syncThrow = () => { throw new Error('probe itself threw synchronously'); };

test('detectAssetsAvailable: true only for an ok:true response', async () => {
  assert.equal(await detectAssetsAvailable(okResponse), true);
});

test('detectAssetsAvailable: false for a 404 (ok:false), never throws', async () => {
  assert.equal(await detectAssetsAvailable(notFound), false);
});

test('detectAssetsAvailable: false for a rejected probe (network/CORS failure), never throws', async () => {
  assert.equal(await detectAssetsAvailable(networkFailure), false);
});

test('detectAssetsAvailable: false for a probe that throws synchronously, never throws', async () => {
  assert.equal(await detectAssetsAvailable(syncThrow), false);
});

test('detectAssetsAvailable: false for a probe resolving to null/undefined', async () => {
  assert.equal(await detectAssetsAvailable(async () => null), false);
  assert.equal(await detectAssetsAvailable(async () => undefined), false);
});

test('detectAssetsAvailable: false for a truthy-but-not-ok response (ok missing, ok: "yes", etc.)', async () => {
  assert.equal(await detectAssetsAvailable(async () => ({})), false);
  assert.equal(await detectAssetsAvailable(async () => ({ ok: 'yes' })), false); // must be === true, not merely truthy
});

test('detectAllAvailable: ok when every probe succeeds', async () => {
  const result = await detectAllAvailable([okResponse, okResponse, okResponse]);
  assert.equal(result.ok, true);
  assert.equal(result.okCount, 3);
  assert.equal(result.total, 3);
  assert.deepEqual(result.results, [true, true, true]);
});

test('detectAllAvailable: not ok when any probe fails, but every probe still runs (no short-circuit)', async () => {
  const result = await detectAllAvailable([okResponse, notFound, networkFailure, okResponse]);
  assert.equal(result.ok, false);
  assert.equal(result.okCount, 2);
  assert.equal(result.total, 4);
  assert.deepEqual(result.results, [true, false, false, true]);
});

test('detectAllAvailable: empty probe list is vacuously ok', async () => {
  const result = await detectAllAvailable([]);
  assert.equal(result.ok, true);
  assert.equal(result.total, 0);
});
