// duckset-core.js: the .duckset/1 contract as the editor sees it.
//
// The rules here are the same rules as python/duckset/validator.py, which is
// canonical. Where a check needs the referenced show files, python reads them
// off disk and this reads a `{path: {name, duration, roles}}` index the page
// has already fetched, so the two can disagree only about what each can see,
// never about what the rule is.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  DEFAULT_END,
  PLAY_LEAD_S,
  addEntry,
  endOf,
  entryLabel,
  formatClock,
  moveEntry,
  moveEntryById,
  newSetlist,
  nextEntryId,
  parseSetlist,
  removeEntry,
  serializeSetlist,
  setEntryEnd,
  setEntryLabel,
  totalRuntime,
  validateSetlist,
} from '../duckset-core.js';

const OCTET = '/shows/octet/octet.duckshow.json';
const DEMO = '/shows/demo/demo.duckshow.json';

const setOf = (...entries) => ({ format: 'duckset/1', meta: { name: 'probe' }, entries });
const errorsOf = (issues) => issues.filter((i) => i.severity === 'error');

test('parseSetlist: a minimal document, with hold as the default end', () => {
  const { doc, error } = parseSetlist(JSON.stringify(setOf({ id: 'a', show: OCTET })));
  assert.equal(error, undefined);
  assert.equal(doc.meta.name, 'probe');
  assert.equal(endOf(doc.entries[0]), 'hold');
  assert.equal(DEFAULT_END, 'hold');
});

test('parseSetlist: unknown fields survive a round-trip untouched', () => {
  const original = { ...setOf({ id: 'a', show: OCTET, colour: 'red' }), future_block: { x: 1 } };
  const { doc } = parseSetlist(JSON.stringify(original));
  const back = JSON.parse(serializeSetlist(doc));
  assert.deepEqual(back.future_block, { x: 1 });
  assert.equal(back.entries[0].colour, 'red');
});

test('parseSetlist: refuses a newer major rather than dropping its fields', () => {
  const { error } = parseSetlist('{"format":"duckset/2","meta":{"name":"x"},"entries":[]}');
  assert.match(error, /duckset\/1/);
});

test('parseSetlist: names the field that is wrong', () => {
  const cases = [
    ['{', /valid JSON/],
    ['[]', /JSON object/],
    ['{"meta":{"name":"x"}}', /format/],
    ['{"format":"setlist/1","meta":{"name":"x"}}', /format/],
    ['{"format":"duckset/1","meta":{}}', /meta\.name/],
    ['{"format":"duckset/1","meta":{"name":"x"},"entries":{}}', /entries/],
    ['{"format":"duckset/1","meta":{"name":"x"},"entries":[{"show":"a"}]}', /id/],
    ['{"format":"duckset/1","meta":{"name":"x"},"entries":[{"id":"a"}]}', /show/],
  ];
  for (const [text, pattern] of cases) {
    const { error } = parseSetlist(text);
    assert.match(error || '', pattern, text);
  }
});

test('parseSetlist: an unknown end behaviour loads and becomes a validator error', () => {
  // Not a parse failure: a setlist written by a newer editor must still open,
  // with the problem shown against the block that has it.
  const { doc, error } = parseSetlist(JSON.stringify(setOf({ id: 'a', show: OCTET, end: 'fade' })));
  assert.equal(error, undefined);
  const errors = errorsOf(validateSetlist(doc));
  assert.equal(errors.length, 1);
  assert.equal(errors[0].entry, 'a');
  assert.match(errors[0].message, /fade/);
});

test('newSetlist: an empty set is a valid document', () => {
  const doc = newSetlist('opening');
  assert.deepEqual(doc.entries, []);
  assert.deepEqual(errorsOf(validateSetlist(doc)), []);
});

test('nextEntryId: derived from the show filename, deduplicated', () => {
  assert.equal(nextEntryId([], OCTET), 'octet');
  assert.equal(nextEntryId([{ id: 'octet' }], OCTET), 'octet-2');
  assert.equal(nextEntryId([{ id: 'octet' }, { id: 'octet-2' }], OCTET), 'octet-3');
});

test('addEntry: appends by default and inserts at an index when asked', () => {
  let doc = addEntry(newSetlist(), OCTET);
  doc = addEntry(doc, DEMO);
  assert.deepEqual(doc.entries.map((e) => e.id), ['octet', 'demo']);
  doc = addEntry(doc, OCTET, { at: 0 });
  assert.deepEqual(doc.entries.map((e) => e.id), ['octet-2', 'octet', 'demo']);
});

test('addEntry: never mutates the document it was given', () => {
  const before = newSetlist();
  const after = addEntry(before, OCTET);
  assert.equal(before.entries.length, 0);
  assert.equal(after.entries.length, 1);
});

test('moveEntry: lands the entry AT the destination index', () => {
  const doc = setOf({ id: 'a', show: OCTET }, { id: 'b', show: OCTET }, { id: 'c', show: OCTET });
  assert.deepEqual(moveEntry(doc, 0, 2).entries.map((e) => e.id), ['b', 'c', 'a']);
  assert.deepEqual(moveEntry(doc, 2, 0).entries.map((e) => e.id), ['c', 'a', 'b']);
  assert.deepEqual(moveEntry(doc, 1, 1).entries.map((e) => e.id), ['a', 'b', 'c']);
});

test('moveEntry: out-of-range indices clamp instead of dropping an entry', () => {
  // A drag can end past either end of the strip; losing a block to that would
  // be the worst possible response.
  const doc = setOf({ id: 'a', show: OCTET }, { id: 'b', show: OCTET });
  assert.deepEqual(moveEntry(doc, 0, 99).entries.map((e) => e.id), ['b', 'a']);
  assert.deepEqual(moveEntry(doc, 1, -4).entries.map((e) => e.id), ['b', 'a']);
  assert.deepEqual(moveEntry(newSetlist(), 0, 1).entries, []);
});

test('moveEntryById: an unknown id changes nothing', () => {
  const doc = setOf({ id: 'a', show: OCTET });
  assert.equal(moveEntryById(doc, 'nope', 0), doc);
});

test('setEntryEnd / removeEntry: touch only the named entry', () => {
  let doc = setOf({ id: 'a', show: OCTET }, { id: 'b', show: DEMO });
  doc = setEntryEnd(doc, 'b', 'loop');
  assert.equal(endOf(doc.entries[0]), 'hold');
  assert.equal(endOf(doc.entries[1]), 'loop');
  assert.deepEqual(removeEntry(doc, 'a').entries.map((e) => e.id), ['b']);
});

test('setEntryLabel: an emptied label is removed, not saved as ""', () => {
  // "" would round-trip as a blank block rather than falling back to the
  // show's own name.
  let doc = setEntryLabel(setOf({ id: 'a', show: OCTET }), 'a', '  vamp  ');
  assert.equal(doc.entries[0].label, 'vamp');
  doc = setEntryLabel(doc, 'a', '   ');
  assert.equal('label' in doc.entries[0], false);
});

test('entryLabel: label, then the show name, then the filename', () => {
  assert.equal(entryLabel({ id: 'a', show: OCTET, label: 'vamp' }, { name: 'Octet' }), 'vamp');
  assert.equal(entryLabel({ id: 'a', show: OCTET }, { name: 'Octet' }), 'Octet');
  assert.equal(entryLabel({ id: 'a', show: OCTET }, null), 'octet');
});

test('validateSetlist: duplicate ids are an error, the same show twice is not', () => {
  // A reprise is legitimate, which is exactly why the id is not the show.
  const dup = errorsOf(validateSetlist(setOf({ id: 'a', show: OCTET }, { id: 'a', show: DEMO })));
  assert.equal(dup.length, 1);
  assert.match(dup[0].message, /duplicate/);
  assert.deepEqual(errorsOf(validateSetlist(setOf({ id: 'open', show: OCTET }, { id: 'reprise', show: OCTET }))), []);
});

test('validateSetlist: a show path must be a .duckshow.json', () => {
  const errors = errorsOf(validateSetlist(setOf({ id: 'a', show: '/shows/octet/notes.txt' })));
  assert.equal(errors.length, 1);
  assert.match(errors[0].message, /\.duckshow\.json/);
});

test('validateSetlist: a trailing continue is a warning, not an error', () => {
  const issues = validateSetlist(setOf({ id: 'a', show: OCTET }, { id: 'b', show: OCTET, end: 'continue' }));
  assert.deepEqual(errorsOf(issues), []);
  const warnings = issues.filter((i) => /continue/.test(i.message));
  assert.equal(warnings.length, 1);
  assert.equal(warnings[0].entry, 'b');
});

test('validateSetlist: a continue that is not last says nothing', () => {
  const issues = validateSetlist(setOf({ id: 'a', show: OCTET, end: 'continue' }, { id: 'b', show: OCTET }));
  assert.deepEqual(issues.filter((i) => /continue/.test(i.message)), []);
});

test('validateSetlist: without an index, nothing is claimed about the files', () => {
  const issues = validateSetlist(setOf({ id: 'a', show: '/shows/nope/nope.duckshow.json' }));
  assert.deepEqual(issues, []);
});

test('validateSetlist: with an index, a missing show is a warning', () => {
  const issues = validateSetlist(setOf({ id: 'a', show: '/shows/nope/nope.duckshow.json' }), { [OCTET]: { roles: [] } });
  assert.deepEqual(errorsOf(issues), []);
  assert.match(issues[0].message, /not found/);
});

test('validateSetlist: a cast change between entries is a warning', () => {
  const index = { [OCTET]: { roles: ['lead', 'wing'] }, [DEMO]: { roles: ['lead'] } };
  const issues = validateSetlist(setOf({ id: 'a', show: OCTET }, { id: 'b', show: DEMO }), index);
  const cast = issues.filter((i) => /cast changes/.test(i.message));
  assert.equal(cast.length, 1);
  assert.equal(cast[0].entry, 'b');
  assert.match(cast[0].message, /drops wing/);
});

test('validateSetlist: the same cast twice says nothing', () => {
  const index = { [OCTET]: { roles: ['lead', 'wing'] } };
  const issues = validateSetlist(setOf({ id: 'a', show: OCTET }, { id: 'b', show: OCTET }), index);
  assert.deepEqual(issues.filter((i) => /cast changes/.test(i.message)), []);
});

test('totalRuntime: counts what it knows and says how much it does not', () => {
  // A duration this page has not fetched must never be treated as zero: the
  // readout would understate the set rather than admit it is incomplete.
  const doc = setOf({ id: 'a', show: OCTET }, { id: 'b', show: DEMO }, { id: 'c', show: '/shows/x/x.duckshow.json' });
  const total = totalRuntime(doc, { [OCTET]: 30, [DEMO]: 20 });
  assert.equal(total.known, 50);
  assert.equal(total.unknown, 1);
  assert.equal(total.leadIn, 2 * PLAY_LEAD_S);
});

test('totalRuntime: a loop counts once, and is reported separately', () => {
  // Its real length is not in the file: it runs until the operator advances.
  const doc = setOf({ id: 'a', show: OCTET, end: 'loop' });
  const total = totalRuntime(doc, { [OCTET]: 30 });
  assert.equal(total.known, 30);
  assert.equal(total.loops, 1);
  assert.equal(total.leadIn, 0, 'a single entry has no transition');
});

test('formatClock: minutes and seconds, and a readable nothing', () => {
  assert.equal(formatClock(0), '0:00');
  assert.equal(formatClock(9.6), '0:10');
  assert.equal(formatClock(125), '2:05');
  assert.equal(formatClock(NaN), '--:--');
  assert.equal(formatClock(-1), '--:--');
});
