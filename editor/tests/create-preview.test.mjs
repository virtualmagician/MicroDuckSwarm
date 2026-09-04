// "Create Preview" button decision logic (docs/viewer.md "Create Preview
// (baked physics)") — capability handling, the job-status state machine,
// and show-path shape checking, all exercised without a browser or a real
// scripts/editor_server.py. No network, no subprocess.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  normalizeShowPath, createPreviewState,
  BAKE_JOB_STATUSES, isTerminalBakeStatus,
  formatBakeProgress, formatBakeSummary, formatBakeError, bakeLogEntries,
} from '../create-preview.js';
import { summarize } from '../bake-cache.js';

// ---------------------------------------------------------------------------
// normalizeShowPath
// ---------------------------------------------------------------------------
test('normalizeShowPath: adds a leading slash to a bare repo-relative path', () => {
  assert.equal(normalizeShowPath('shows/octet/octet.duckshow.json'), '/shows/octet/octet.duckshow.json');
});

test('normalizeShowPath: leaves an already-slashed path alone', () => {
  assert.equal(normalizeShowPath('/shows/octet/octet.duckshow.json'), '/shows/octet/octet.duckshow.json');
});

test('normalizeShowPath: trims surrounding whitespace', () => {
  assert.equal(normalizeShowPath('  /shows/demo/demo.duckshow.json  '), '/shows/demo/demo.duckshow.json');
});

test('normalizeShowPath: null for anything not ending in .duckshow.json', () => {
  assert.equal(normalizeShowPath('/shows/octet/octet.duckbake.json'), null);
  assert.equal(normalizeShowPath('/shows/octet/'), null);
  assert.equal(normalizeShowPath('/README.md'), null);
});

test('normalizeShowPath: null for an absolute URL (not a repo-relative path)', () => {
  assert.equal(normalizeShowPath('http://evil.example/shows/x.duckshow.json'), null);
  assert.equal(normalizeShowPath('https://example.com/x.duckshow.json'), null);
});

test('normalizeShowPath: null for empty/blank/non-string input', () => {
  assert.equal(normalizeShowPath(''), null);
  assert.equal(normalizeShowPath('   '), null);
  assert.equal(normalizeShowPath(null), null);
  assert.equal(normalizeShowPath(undefined), null);
  assert.equal(normalizeShowPath(42), null);
});

// ---------------------------------------------------------------------------
// createPreviewState
// ---------------------------------------------------------------------------
const AVAILABLE_CAPS = { available: true, reason: null, venv_python: true, bake_script: true, assets: true, shows: [] };

test('createPreviewState: enabled when capabilities, path and cleanliness all line up', () => {
  const s = createPreviewState({ capabilities: AVAILABLE_CAPS, showPath: '/shows/octet/octet.duckshow.json', dirty: false });
  assert.equal(s.enabled, true);
  assert.equal(s.reason, null);
});

test('createPreviewState: disabled with a reason when capabilities never loaded (file:// or plain http.server)', () => {
  const s = createPreviewState({ capabilities: null, showPath: '/shows/demo/demo.duckshow.json', dirty: false });
  assert.equal(s.enabled, false);
  assert.match(s.reason, /file:\/\/|http\.server|edit\.sh/);
});

test('createPreviewState: disabled with the server\'s own reason when capabilities.available is false', () => {
  const caps = { available: false, reason: 'assets/microduck/ not populated', venv_python: true, bake_script: true, assets: false, shows: [] };
  const s = createPreviewState({ capabilities: caps, showPath: '/shows/demo/demo.duckshow.json', dirty: false });
  assert.equal(s.enabled, false);
  assert.equal(s.reason, 'assets/microduck/ not populated');
});

test('createPreviewState: falls back to a generic reason if the server omits one', () => {
  const caps = { available: false, reason: null, venv_python: false, bake_script: true, assets: true, shows: [] };
  const s = createPreviewState({ capabilities: caps, showPath: '/shows/demo/demo.duckshow.json', dirty: false });
  assert.equal(s.enabled, false);
  assert.ok(s.reason);
});

test('createPreviewState: disabled with a reason when the loaded show has no known server path', () => {
  const s = createPreviewState({ capabilities: AVAILABLE_CAPS, showPath: null, dirty: false });
  assert.equal(s.enabled, false);
  assert.match(s.reason, /path/i);
});

test('createPreviewState: unsaved edits no longer block the button', () => {
  // Regression guard for the opposite of the old rule. Requiring a saved file
  // was unworkable: a browser cannot write the show back (Save downloads a
  // copy), so every edit disabled Create Preview until the author manually
  // moved a download over the original. The editor now POSTs the document
  // itself, so a dirty show bakes fine and the cache is hash-checked against
  // exactly the bytes that were baked.
  const caps = { available: true, reason: null, shows: [] };
  const s = createPreviewState({ capabilities: caps, showPath: '/shows/octet/octet.duckshow.json', dirty: true });
  assert.equal(s.enabled, true);
  assert.equal(s.reason, null);
});

test('createPreviewState: dirty does not change the answer either way', () => {
  const caps = { available: true, reason: null, shows: [] };
  const path = '/shows/octet/octet.duckshow.json';
  assert.deepEqual(
    createPreviewState({ capabilities: caps, showPath: path, dirty: true }),
    createPreviewState({ capabilities: caps, showPath: path, dirty: false }),
  );
});

test('createPreviewState: capabilities/path checks take priority over the dirty check (most specific problem first is not required, but a reason is always present)', () => {
  const s = createPreviewState({ capabilities: null, showPath: null, dirty: true });
  assert.equal(s.enabled, false);
  assert.ok(s.reason);
});

// ---------------------------------------------------------------------------
// Job-status state machine
// ---------------------------------------------------------------------------
test('BAKE_JOB_STATUSES: the three states scripts/editor_server.py can report', () => {
  assert.deepEqual(BAKE_JOB_STATUSES, ['running', 'done', 'error']);
});

test('isTerminalBakeStatus: only done/error are terminal', () => {
  assert.equal(isTerminalBakeStatus('running'), false);
  assert.equal(isTerminalBakeStatus('done'), true);
  assert.equal(isTerminalBakeStatus('error'), true);
  assert.equal(isTerminalBakeStatus('nonsense'), false);
});

test('formatBakeProgress: generic message before the first progress line arrives', () => {
  assert.equal(formatBakeProgress({ status: 'running', progress: null }), 'baking…');
  assert.equal(formatBakeProgress(null), 'baking…');
});

test('formatBakeProgress: names the role, its position in the cast, and percent', () => {
  const job = { status: 'running', progress: { role: 'lead', role_index: 3, role_total: 8, pct: 40 } };
  assert.equal(formatBakeProgress(job), 'baking lead (3/8) 40%…');
});

test('formatBakeProgress: degrades gracefully without a known cast size', () => {
  const job = { status: 'running', progress: { role: 'lead', role_index: 1, role_total: null, pct: 0 } };
  assert.equal(formatBakeProgress(job), 'baking lead 0%…');
});

test('formatBakeSummary: accepts the server job.summary shape (snake_case)', () => {
  // scripts/editor_server.py's _summarize_cache() spells these the way the
  // on-disk duckbake/1 cache does.
  const summary = { roles: 8, duration: 64, unsimulated_roles: ['lead'], fallen_roles: [] };
  assert.equal(formatBakeSummary(summary), '8 roles, 64s · 1 unsimulated');
});

test('formatBakeSummary: accepts bake-cache.js summarize() output (camelCase), for real', () => {
  // Regression: this used to be asserted against a hand-written snake_case
  // literal, so it never actually exercised the Create Preview path -- which
  // passes summarize()'s camelCase object. Both note fields silently read as
  // undefined there, so a bake with an unsimulated role or a fallen duck
  // reported a clean "N roles, Ns". Drive the REAL producer here so the two
  // shapes can never drift apart again unnoticed.
  const cache = {
    roles: ['lead', 'echo', 'drift', 'spark', 'reed', 'wren', 'sable', 'flare'],
    show: { duration: 64 },
    unsimulated_roles: ['lead'],
    fallen_roles: ['echo'],
  };
  const summary = summarize(cache);
  assert.deepEqual(Object.keys(summary).sort(), ['duration', 'fallenRoles', 'roles', 'unsimulatedRoles']);
  assert.equal(formatBakeSummary(summary), '8 roles, 64s · 1 unsimulated · 1 fell');
});

test('formatBakeSummary: both producers render an identical line for the same cache', () => {
  // The docstring's actual promise: "the two paths read identically".
  const cache = {
    roles: ['a', 'b'], show: { duration: 16 },
    unsimulated_roles: ['a'], fallen_roles: [],
  };
  const fromPlayBaked = formatBakeSummary(summarize(cache));
  const fromCreatePreview = formatBakeSummary({
    roles: cache.roles.length, duration: cache.show.duration,
    unsimulated_roles: cache.unsimulated_roles, fallen_roles: cache.fallen_roles,
  });
  assert.equal(fromPlayBaked, fromCreatePreview);
});

test('formatBakeSummary: no notes clause when nothing unsimulated or fallen', () => {
  const summary = { roles: 2, duration: 16, unsimulated_roles: [], fallen_roles: [] };
  assert.equal(formatBakeSummary(summary), '2 roles, 16s');
});

test('formatBakeSummary: reports falls too, and both together', () => {
  const summary = { roles: 8, duration: 64, unsimulated_roles: ['a', 'b'], fallen_roles: ['c'] };
  assert.equal(formatBakeSummary(summary), '8 roles, 64s · 2 unsimulated · 1 fell');
});

test('formatBakeSummary: empty string for no summary yet', () => {
  assert.equal(formatBakeSummary(null), '');
});

test('formatBakeError: leads with the job\'s own error, appends the baker\'s stderr tail', () => {
  const job = { error: 'bake_show.py exited 1', log_tail: ['error: shows/x.duckshow.json fails validation (2 error(s))', '  - vx over limit'] };
  const msg = formatBakeError(job);
  assert.match(msg, /^bake_show\.py exited 1/);
  assert.match(msg, /fails validation/);
  assert.match(msg, /vx over limit/);
});

test('formatBakeError: falls back to a generic message with no job/log', () => {
  assert.equal(formatBakeError(null), 'bake failed');
  assert.equal(formatBakeError({ error: null, log_tail: [] }), 'bake failed');
});

test('bakeLogEntries: caps entries and reports how many were left out', () => {
  const log = Array.from({ length: 15 }, (_, i) => ({ role: 'lead', t: i, kind: 'skill_unsimulated', detail: `event ${i}` }));
  const { entries, more } = bakeLogEntries({ log }, 12);
  assert.equal(entries.length, 12);
  assert.equal(more, 3);
  assert.equal(entries[0].detail, 'event 0');
});

test('bakeLogEntries: no cap needed, no "more"', () => {
  const log = [{ role: 'lead', t: 1, kind: 'fell', detail: 'trunk dropped' }];
  const { entries, more } = bakeLogEntries({ log });
  assert.equal(entries.length, 1);
  assert.equal(more, 0);
});

test('bakeLogEntries: empty/missing log never throws', () => {
  assert.deepEqual(bakeLogEntries(null), { entries: [], more: 0 });
  assert.deepEqual(bakeLogEntries({}), { entries: [], more: 0 });
  assert.deepEqual(bakeLogEntries({ log: [] }), { entries: [], more: 0 });
});
