// activePolicies(): which ONNX policy is driving each role right now.
//
// Exists because most of the shipped policies can never be named by an event
// label -- docs/duckshow-format.md lists alpha_walking.onnx / roller.onnx as
// "implicit in locomotion", so no .duckshow event ever spells them.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { activePolicies } from '../duckshow-core.js';

const show = (tracks, { duration = 30, cast = null } = {}) => ({
  format: 'duckshow/1',
  meta: { name: 'probe', duration },
  cast: (cast || Object.keys(tracks)).map((role) => ({ role })),
  tracks,
});

test('activePolicies: an idle cast is on the perpetual walk policy', () => {
  const doc = show({ a: {}, b: {} });
  assert.deepEqual(activePolicies(doc, 0), [
    { policy: 'alpha_walking.onnx', roles: ['a', 'b'] },
  ]);
});

test('activePolicies: roller mode swaps the perpetual policy, per role', () => {
  const doc = show({ a: {}, b: { events: [{ t: 1.0, mode: 'roller' }] } });
  assert.deepEqual(activePolicies(doc, 2.0), [
    { policy: 'alpha_walking.onnx', roles: ['a'] },
    { policy: 'roller.onnx', roles: ['b'] },
  ]);
  // ...and not before the mode event
  assert.deepEqual(activePolicies(doc, 0.5), [
    { policy: 'alpha_walking.onnx', roles: ['a', 'b'] },
  ]);
});

test('activePolicies: a skill in progress owns the duck for exactly its duration', () => {
  const doc = show({ a: { events: [{ t: 5.0, do: 'kick_left' }] } });
  assert.equal(activePolicies(doc, 4.9)[0].policy, 'alpha_walking.onnx');
  assert.equal(activePolicies(doc, 5.2)[0].policy, 'ball_kick_left.onnx');
  assert.equal(activePolicies(doc, 5.5)[0].policy, 'ball_kick_left.onnx', 'at the trailing edge');
  assert.equal(activePolicies(doc, 5.6)[0].policy, 'alpha_walking.onnx', 'released after the clip');
});

test('activePolicies: a roller-mode ground_pick reports the roller clip, for its longer duration', () => {
  const doc = show({ a: { events: [{ t: 1.0, mode: 'roller' }, { t: 5.0, do: 'ground_pick' }] } });
  assert.equal(activePolicies(doc, 5.5)[0].policy, 'roller_crouch.onnx');
  assert.equal(activePolicies(doc, 8.4)[0].policy, 'roller_crouch.onnx', '3.5 s, not 2.8');
  assert.equal(activePolicies(doc, 8.6)[0].policy, 'roller.onnx', 'back to the roller perpetual');
});

test('activePolicies: groups roles by policy and sorts both levels', () => {
  const doc = show({
    zeta: {}, alpha: {}, mid: { events: [{ t: 0.0, do: 'roulade' }] },
  });
  const at = activePolicies(doc, 0.5);
  assert.deepEqual(at.map((p) => p.policy), ['alpha_walking.onnx', 'roulade.onnx']);
  assert.deepEqual(at[0].roles, ['alpha', 'zeta'], 'roles sorted within a policy');
});

test('activePolicies: never claims alpha_stand, which nothing documents a switch to', () => {
  // The manifest marks it perpetual alongside alpha_walking, but robot.setMode
  // names only walk/roller -- there is no documented walk/stand switch, so
  // reporting it would be a guess presented as a readout. tools/bake makes the
  // same simplification and flags it.
  const doc = show({ a: { pose: [{ t: 0.0, z: 0.0, roll: 0, pitch: 0, active: true }] } });
  const names = activePolicies(doc, 1.0).map((p) => p.policy);
  assert.ok(!names.includes('alpha_stand.onnx'), names.join(','));
});

test('activePolicies: an empty cast returns an empty list, never throws', () => {
  assert.deepEqual(activePolicies(show({}, { cast: [] }), 0), []);
});
