// Every edit operation: correct result, input never mutated.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  addKeyframe, moveKeyframe, deleteKeyframe, setInterp, cycleInterp, keyframeIndexAt,
  addEvent, updateEvent, setEventAction, deleteEvent,
  setMeta, addRole, removeRole, renameRole, setMark, newShow, validate,
} from '../duckshow-core.js';

function base() {
  return {
    format: 'duckshow/1',
    meta: { name: 'Base', duration: 10.0, music: { file: 'a.wav', bpm: 120, beat_offset: 0 } },
    requires: { policies: [] },
    cast: [{ role: 'lead', notes: 'front' }, { role: 'wing' }],
    tracks: {
      lead: {
        locomotion: [{ t: 0.0, vx: 0.0 }, { t: 2.0, vx: 0.1, interp: 'smooth' }, { t: 4.0, vx: 0.0 }],
        events: [{ t: 1.0, sound: 'chirp' }, { t: 3.0, do: 'kick_left' }],
      },
      wing: {},
    },
    editor: { marks: { lead: { x: 1, y: 0, heading: 0 } } },
  };
}

/** Run op on a snapshot and assert the original was not touched. */
function pure(op) {
  const doc = base();
  const frozen = JSON.stringify(doc);
  const out = op(doc);
  assert.equal(JSON.stringify(doc), frozen, 'input show was mutated');
  assert.notEqual(out, doc);
  return out;
}

test('addKeyframe inserts in t order and creates missing track lists', () => {
  let out = pure((d) => addKeyframe(d, 'lead', 'locomotion', { t: 1.0, vx: 0.05 }));
  assert.deepEqual(out.tracks.lead.locomotion.map((k) => k.t), [0, 1, 2, 4]);
  assert.equal(keyframeIndexAt(out, 'lead', 'locomotion', 1.0), 1);
  out = pure((d) => addKeyframe(d, 'wing', 'head', { t: 0.5, head_yaw: 0.2, interp: 'step' }));
  assert.deepEqual(out.tracks.wing.head, [{ t: 0.5, head_yaw: 0.2, interp: 'step' }]);
  out = pure((d) => addKeyframe(d, 'lead', 'locomotion', { t: 9.0, vx: 0.01 }));
  assert.equal(out.tracks.lead.locomotion.at(-1).t, 9.0);
});

test('addKeyframe at an existing t merges into that keyframe', () => {
  const out = pure((d) => addKeyframe(d, 'lead', 'locomotion', { t: 2.0, vy: 0.1 }));
  assert.equal(out.tracks.lead.locomotion.length, 3);
  assert.deepEqual(out.tracks.lead.locomotion[1], { t: 2.0, vx: 0.1, interp: 'smooth', vy: 0.1 });
});

test('addKeyframe rejects bad input', () => {
  assert.throws(() => addKeyframe(base(), 'ghost', 'locomotion', { t: 0 }), RangeError);
  assert.throws(() => addKeyframe(base(), 'lead', 'events', { t: 0 }), RangeError);
  assert.throws(() => addKeyframe(base(), 'lead', 'locomotion', { t: -1 }), RangeError);
  assert.throws(() => addKeyframe(base(), 'lead', 'locomotion', { t: NaN }), RangeError);
});

test('moveKeyframe changes time and values, re-sorts, clamps t at 0', () => {
  let out = pure((d) => moveKeyframe(d, 'lead', 'locomotion', 1, { t: 5.0, vx: 0.2 }));
  assert.deepEqual(out.tracks.lead.locomotion.map((k) => k.t), [0, 4, 5]);
  assert.deepEqual(out.tracks.lead.locomotion[2], { t: 5.0, vx: 0.2, interp: 'smooth' });
  assert.equal(keyframeIndexAt(out, 'lead', 'locomotion', 5.0), 2);
  // A negative time clamps to 0; keyframe 0 already owns t=0, so that is not a collision.
  out = pure((d) => moveKeyframe(d, 'lead', 'locomotion', 0, { t: -3, vx: 0.02 }));
  assert.deepEqual(out.tracks.lead.locomotion.map((k) => k.t), [0, 2, 4]);
  assert.equal(out.tracks.lead.locomotion[0].vx, 0.02);
});

test('moveKeyframe refuses a time collision (show returned unchanged)', () => {
  const doc = base();
  const same = moveKeyframe(doc, 'lead', 'locomotion', 2, { t: 2.0 });
  assert.equal(same, doc);
  const clamped = moveKeyframe(doc, 'lead', 'locomotion', 2, { t: -3 }); // clamps to 0, which is taken
  assert.equal(clamped, doc);
});

test('moveKeyframe value-only edit keeps position; undefined deletes a field', () => {
  const out = pure((d) => moveKeyframe(d, 'lead', 'locomotion', 1, { vx: 0.25, interp: undefined }));
  assert.deepEqual(out.tracks.lead.locomotion[1], { t: 2.0, vx: 0.25 });
  assert.throws(() => moveKeyframe(base(), 'lead', 'locomotion', 7, { vx: 0 }), RangeError);
});

test('deleteKeyframe', () => {
  const out = pure((d) => deleteKeyframe(d, 'lead', 'locomotion', 1));
  assert.deepEqual(out.tracks.lead.locomotion.map((k) => k.t), [0, 4]);
  assert.throws(() => deleteKeyframe(base(), 'lead', 'locomotion', 3), RangeError);
});

test('setInterp and cycleInterp', () => {
  const out = pure((d) => setInterp(d, 'lead', 'locomotion', 0, 'step'));
  assert.equal(out.tracks.lead.locomotion[0].interp, 'step');
  assert.throws(() => setInterp(base(), 'lead', 'locomotion', 0, 'bezier'), RangeError);
  assert.equal(cycleInterp('linear'), 'smooth');
  assert.equal(cycleInterp('smooth'), 'step');
  assert.equal(cycleInterp('step'), 'linear');
  assert.equal(cycleInterp(undefined), 'smooth'); // default is linear
  assert.equal(cycleInterp('bogus'), 'linear');
});

test('addEvent inserts in t order (after equal times)', () => {
  let out = pure((d) => addEvent(d, 'lead', { t: 2.0, mode: 'roller' }));
  assert.deepEqual(out.tracks.lead.events.map((e) => e.t), [1, 2, 3]);
  out = pure((d) => addEvent(d, 'lead', { t: 1.0, sound: 'coo' }));
  assert.deepEqual(out.tracks.lead.events.map((e) => e.sound ?? e.do), ['chirp', 'coo', 'kick_left']);
  out = pure((d) => addEvent(d, 'wing', { t: 0.0, do: 'roulade' }));
  assert.deepEqual(out.tracks.wing.events, [{ t: 0.0, do: 'roulade' }]);
  assert.throws(() => addEvent(base(), 'lead', { t: -1, sound: 'coo' }), RangeError);
  assert.throws(() => addEvent(base(), 'ghost', { t: 0, sound: 'coo' }), RangeError);
});

test('updateEvent merges, keeps position, deletes undefined keys', () => {
  let out = pure((d) => updateEvent(d, 'lead', 0, { t: 5.0, hold: 1.5 }));
  assert.deepEqual(out.tracks.lead.events[0], { t: 5.0, sound: 'chirp', hold: 1.5 });
  out = pure((d) => updateEvent(d, 'lead', 0, { sound: undefined, do: 'sit_toggle' }));
  assert.deepEqual(out.tracks.lead.events[0], { t: 1.0, do: 'sit_toggle' });
  assert.throws(() => updateEvent(base(), 'lead', 0, { t: -1 }), RangeError);
  assert.throws(() => updateEvent(base(), 'lead', 9, { t: 1 }), RangeError);
});

test('setEventAction leaves exactly one action key (hold only with sound)', () => {
  let out = pure((d) => updateEvent(d, 'lead', 0, { hold: 2.0 }));
  out = setEventAction(out, 'lead', 0, 'do', 'kick_right');
  assert.deepEqual(out.tracks.lead.events[0], { t: 1.0, do: 'kick_right' });
  out = setEventAction(out, 'lead', 0, 'mode', 'roller');
  assert.deepEqual(out.tracks.lead.events[0], { t: 1.0, mode: 'roller' });
  out = updateEvent(out, 'lead', 0, { hold: 1 });
  out = setEventAction(out, 'lead', 0, 'sound', 'wheee');
  assert.deepEqual(out.tracks.lead.events[0], { t: 1.0, hold: 1, sound: 'wheee' });
  assert.throws(() => setEventAction(base(), 'lead', 0, 'dance', 'x'), RangeError);
});

test('deleteEvent', () => {
  const out = pure((d) => deleteEvent(d, 'lead', 1));
  assert.deepEqual(out.tracks.lead.events, [{ t: 1.0, sound: 'chirp' }]);
  assert.throws(() => deleteEvent(base(), 'wing', 0), RangeError);
});

test('setMeta merges shallowly, music one level deeper, undefined deletes, null clears music', () => {
  let out = pure((d) => setMeta(d, { name: 'New', duration: 12, author: 'me', music: { bpm: 100 } }));
  assert.deepEqual(out.meta, { name: 'New', duration: 12, music: { file: 'a.wav', bpm: 100, beat_offset: 0 }, author: 'me' });
  out = pure((d) => setMeta(d, { author: undefined, music: { file: undefined } }));
  assert.deepEqual(out.meta, { name: 'Base', duration: 10.0, music: { bpm: 120, beat_offset: 0 } });
  out = pure((d) => setMeta(d, { music: null }));
  assert.equal(out.meta.music, null);
  const noMeta = { format: 'duckshow/1', cast: [], tracks: {} };
  assert.deepEqual(setMeta(noMeta, { duration: 3, music: { bpm: 90 } }).meta, { duration: 3, music: { bpm: 90 } });
});

test('addRole appends cast + tracks entry; removeRole drops cast, tracks and mark', () => {
  let out = pure((d) => addRole(d, 'tail', 'upstage'));
  assert.deepEqual(out.cast.at(-1), { role: 'tail', notes: 'upstage' });
  assert.deepEqual(out.tracks.tail, {});
  assert.deepEqual(validate(out), []);
  assert.throws(() => addRole(base(), 'lead'), RangeError);
  assert.throws(() => addRole(base(), ''), RangeError);
  out = pure((d) => removeRole(d, 'lead'));
  assert.deepEqual(out.cast, [{ role: 'wing' }]);
  assert.equal('lead' in out.tracks, false);
  assert.deepEqual(out.editor.marks, {});
  assert.deepEqual(validate(out), []);
});

test('renameRole rewrites cast (in place), tracks key and editor.marks key atomically', () => {
  let out = pure((d) => renameRole(d, 'lead', 'front'));
  assert.deepEqual(out.cast, [{ role: 'front', notes: 'front' }, { role: 'wing' }]); // same index; 'wing' untouched
  assert.equal('lead' in out.tracks, false);
  assert.deepEqual(out.tracks.front.locomotion, base().tracks.lead.locomotion);
  assert.deepEqual(out.tracks.front.events, base().tracks.lead.events);
  assert.equal('lead' in out.editor.marks, false);
  assert.deepEqual(out.editor.marks.front, { x: 1, y: 0, heading: 0 });
  assert.deepEqual(validate(out), []);

  // a role with no editor.marks entry (only 'lead' has one in the fixture) renames cleanly too
  out = pure((d) => renameRole(d, 'wing', 'flank'));
  assert.deepEqual(out.cast, [{ role: 'lead', notes: 'front' }, { role: 'flank' }]); // 'lead' untouched, order preserved
  assert.deepEqual(out.tracks.flank, {});
  assert.equal('wing' in out.editor.marks, false);
  assert.equal('flank' in out.editor.marks, false);
});

test('renameRole keeps cast ARRAY order even when the new name would sort differently — a rename never re-sorts the cast', () => {
  const doc = {
    format: 'duckshow/1', meta: { duration: 5 }, requires: { policies: [] },
    cast: [{ role: 'a' }, { role: 'b' }, { role: 'c' }],
    tracks: { a: {}, b: {}, c: {} },
  };
  const out = renameRole(doc, 'b', 'zzz'); // alphabetically 'zzz' sorts after 'c'; array order must not follow
  assert.deepEqual(out.cast.map((c) => c.role), ['a', 'zzz', 'c']);
});

test('renameRole trims surrounding whitespace off the new name', () => {
  const out = pure((d) => renameRole(d, 'wing', '  flank  '));
  assert.deepEqual(out.cast[1], { role: 'flank' });
});

test('renameRole rejects empty, whitespace-only, an unknown role, or a name that collides with another role — show left untouched', () => {
  const doc = base();
  const frozen = JSON.stringify(doc);
  assert.throws(() => renameRole(doc, 'lead', ''), RangeError);
  assert.throws(() => renameRole(doc, 'lead', '   '), RangeError);
  assert.throws(() => renameRole(doc, 'lead', 'wing'), RangeError); // collides with another role
  assert.throws(() => renameRole(doc, 'ghost', 'x'), RangeError); // 'from' not in the cast
  assert.equal(JSON.stringify(doc), frozen, 'show mutated by a rejected rename');
});

test('renameRole to its own (trimmed) name is a no-op, not an error', () => {
  const doc = base();
  const out = renameRole(doc, 'lead', '  lead  ');
  assert.notEqual(out, doc); // still a fresh object, per the "-> new show" contract
  assert.deepEqual(out, doc);
});

test('setMark writes editor.marks[role] with numeric fields only', () => {
  const out = pure((d) => setMark(d, 'wing', { x: '0.5', y: 2, heading: undefined }));
  assert.deepEqual(out.editor.marks.wing, { x: 0.5, y: 2, heading: 0 });
  assert.deepEqual(out.editor.marks.lead, { x: 1, y: 0, heading: 0 });
});

test('newShow is a valid, empty document', () => {
  const doc = newShow({ name: 'Fresh', duration: 16, bpm: 100, beatOffset: 0.25, roles: ['a', 'b'], created: '2026-09-02' });
  assert.deepEqual(doc, {
    format: 'duckshow/1',
    meta: { name: 'Fresh', created: '2026-09-02', duration: 16, music: { file: null, bpm: 100, beat_offset: 0.25 } },
    requires: { policies: [] },
    cast: [{ role: 'a' }, { role: 'b' }],
    tracks: { a: {}, b: {} },
  });
  assert.deepEqual(validate(doc), []);
  assert.equal(newShow({ bpm: 0 }).meta.music, undefined);
});
