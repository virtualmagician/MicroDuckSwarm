// Round-trip preservation: unknown fields anywhere, key order, and the
// top-level "editor" block survive parse -> edit -> serialize.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import {
  parseShow, serializeShow, normalizeShow, validate, addKeyframe, moveKeyframe, addEvent, setMeta, setMark, getMark, getMarks, removeRole,
  DuckShowFormatError,
} from '../duckshow-core.js';

const DEMO = fileURLToPath(new URL('../../shows/demo/demo.duckshow.json', import.meta.url));

const WITH_UNKNOWNS = `{
  "format": "duckshow/1",
  "x_top_level": {"nested": [1, 2, {"deep": true}]},
  "meta": {"name": "Unknowns", "duration": 4.0, "x_meta": "keep", "music": {"bpm": 90, "x_music": 1}},
  "requires": {"policies": [{"name": "p", "mode": "roller", "file": "p.onnx", "sha256": "abc", "slot": "walk", "x_policy": 7}], "x_requires": null},
  "cast": [{"role": "lead", "notes": "n", "x_cast": [true]}],
  "tracks": {
    "lead": {
      "x_track_level": "keep",
      "locomotion": [{"t": 0.0, "vx": 0.1, "x_kf": {"tag": "a"}}, {"t": 2.0, "vx": 0.0, "interp": "smooth"}],
      "events": [{"t": 1.0, "sound": "chirp", "x_event": "keep"}],
      "servo": [{"t": 3.0, "mode": "hold", "duration": 0.5, "x_servo": 1}]
    }
  },
  "editor": {"marks": {"lead": {"x": 0.5, "y": -0.25, "heading": 1.5707963}}, "x_editor_other": "keep"}
}
`;

test('demo show round-trips to an identical document', () => {
  const text = readFileSync(DEMO, 'utf8');
  const doc = parseShow(text);
  const again = parseShow(serializeShow(doc));
  assert.deepEqual(again, JSON.parse(text));
  assert.deepEqual(Object.keys(again), Object.keys(JSON.parse(text))); // key order preserved
});

test('unknown fields at every level survive parse -> serialize', () => {
  const doc = parseShow(WITH_UNKNOWNS);
  assert.deepEqual(JSON.parse(serializeShow(doc)), JSON.parse(WITH_UNKNOWNS));
  assert.deepEqual(validate(doc), []); // unknown fields are ignored, not flagged
});

test('unknown fields survive edit operations on neighbouring data', () => {
  let doc = parseShow(WITH_UNKNOWNS);
  doc = addKeyframe(doc, 'lead', 'locomotion', { t: 1.0, vx: 0.05 });
  doc = moveKeyframe(doc, 'lead', 'locomotion', 0, { vx: 0.2 });
  doc = addEvent(doc, 'lead', { t: 2.0, do: 'kick_left' });
  doc = setMeta(doc, { name: 'Renamed', music: { bpm: 100 } });
  const out = JSON.parse(serializeShow(doc));
  assert.deepEqual(out.x_top_level, { nested: [1, 2, { deep: true }] });
  assert.equal(out.meta.x_meta, 'keep');
  assert.equal(out.meta.music.x_music, 1);
  assert.equal(out.meta.music.bpm, 100);
  assert.equal(out.meta.name, 'Renamed');
  assert.equal(out.requires.policies[0].x_policy, 7);
  assert.equal('x_requires' in out.requires, true);
  assert.deepEqual(out.cast[0].x_cast, [true]);
  assert.equal(out.tracks.lead.x_track_level, 'keep');
  assert.deepEqual(out.tracks.lead.locomotion[0], { t: 0.0, vx: 0.2, x_kf: { tag: 'a' } }); // edited value, unknown kept
  assert.equal(out.tracks.lead.locomotion[1].t, 1.0);
  assert.equal(out.tracks.lead.events[0].x_event, 'keep');
  assert.equal(out.tracks.lead.servo[0].x_servo, 1);
  assert.equal(out.editor.x_editor_other, 'keep');
});

test('the "editor" block: marks are read, written, ignored by validation', () => {
  let doc = parseShow(WITH_UNKNOWNS);
  assert.deepEqual(getMark(doc, 'lead'), { x: 0.5, y: -0.25, heading: 1.5707963 });
  assert.deepEqual(getMark(doc, 'nobody'), { x: 0, y: 0, heading: 0 });
  doc = setMark(doc, 'lead', { x: 1, y: 2, heading: 0.5 });
  const out = JSON.parse(serializeShow(doc));
  assert.deepEqual(out.editor, { marks: { lead: { x: 1, y: 2, heading: 0.5 } }, x_editor_other: 'keep' });
  assert.deepEqual(getMarks(doc), { lead: { x: 1, y: 2, heading: 0.5 } });
  assert.deepEqual(validate(doc), []);
  // setMark on a document without an editor block creates it; removeRole drops the mark.
  let bare = parseShow(readFileSync(DEMO, 'utf8'));
  bare = setMark(bare, 'wing', { x: -0.5, y: 0, heading: 3.1 });
  assert.deepEqual(JSON.parse(serializeShow(bare)).editor, { marks: { wing: { x: -0.5, y: 0, heading: 3.1 } } });
  bare = removeRole(bare, 'wing');
  assert.deepEqual(JSON.parse(serializeShow(bare)).editor, { marks: {} });
});

test('serializeShow ends with a newline and refuses non-finite numbers', () => {
  const doc = parseShow(WITH_UNKNOWNS);
  assert.ok(serializeShow(doc).endsWith('}\n'));
  doc.meta.duration = Infinity;
  assert.throws(() => serializeShow(doc), DuckShowFormatError);
});

test('parseShow rejects what the Python loader rejects', () => {
  const cases = [
    ['not json', /invalid JSON/],
    ['[]', /must be a JSON object/],
    ['{"meta":{"duration":1}}', /missing or non-string top-level 'format'/],
    ['{"format":"duckshow/2","meta":{"duration":1}}', /unsupported duckshow format major version 2/],
    ['{"format":"quack/1"}', /unrecognized 'format' field/],
    ['{"format":"duckshow/1","meta":[]}', /expected 'meta' to be an object, got list/],
    ['{"format":"duckshow/1","meta":{"duration":"soon"}}', /could not convert string to float/],
    ['{"format":"duckshow/1","meta":{"duration":1},"cast":[{"role":5}]}', /expected 'cast\[\].role' to be a string, got int/],
    ['{"format":"duckshow/1","meta":{"duration":1},"tracks":{"lead":{"locomotion":[{"vx":0.1}]}}}', /malformed document: 't'/],
    ['{"format":"duckshow/1","meta":{"duration":1},"tracks":{"lead":{"locomotion":{"t":0}}}}', /expected 'locomotion' to be a list, got dict/],
    ['{"format":"duckshow/1","meta":{"duration":1},"tracks":{"lead":[]}}', /expected "tracks\['lead'\]" to be an object, got list/],
    ['{"format":"duckshow/1","meta":{"duration":1},"requires":{"policies":[{"name":"m","mode":"m"}]}}', /requires.policies\[\].file/],
    ['{"format":"duckshow/1","meta":{"duration":1},"tracks":{"lead":{"pose":[{"t":0,"z":null}]}}}', /float\(\) argument must be a string or a real number, not 'NoneType'/],
  ];
  for (const [text, re] of cases) {
    assert.throws(() => parseShow(text), (err) => err instanceof DuckShowFormatError && re.test(err.message), `${text} -> ${re}`);
  }
});

test('minimal document loads with Python defaults', () => {
  const norm = normalizeShow(parseShow('{"format":"duckshow/1","meta":{"duration":5.0},"cast":[{"role":"lead"}],"tracks":{"lead":{}}}'));
  assert.equal(norm.meta.name, null);
  assert.equal(norm.meta.music, null);
  assert.equal(norm.meta.duration, 5.0);
  assert.deepEqual(norm.requires.policies, []);
  assert.deepEqual(norm.roleNames(), ['lead']);
  assert.deepEqual(norm.tracksFor('lead'), { locomotion: [], head: [], pose: [], mouth: [], events: [], servo: [] });
  const music = normalizeShow(parseShow('{"format":"duckshow/1","meta":{"duration":5,"music":{"file":"x.wav","bpm":100}}}')).meta.music;
  assert.deepEqual(music, { file: 'x.wav', bpm: 100, beat_offset: 0 });
});
