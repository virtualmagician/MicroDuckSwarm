// duckset-core.js — pure ES module, no DOM, no dependencies.
//
// MODULE_API: bump this whenever this module's *exported surface* changes.
// setlist.html asserts it at boot and refuses to start on a mismatch, for the
// reason spelled out at the top of duckshow-core.js: separately-fetched ES
// modules with no bundler and no content hashes can be paired with a stale
// cache entry, and no response header can fix a cache entry that is never
// revalidated.
//
// The second implementation of the .duckset/1 contract
// (docs/setlist-format.md), alongside python/duckset (canonical). Rules,
// severities and message text match python/duckset/validator.py for the
// checks that do not need the filesystem; the disk-backed warnings there are
// answered here from a show index the page has already fetched.

export const MODULE_API = 1;

export const SETLIST_FORMAT = 'duckset/1';
export const END_BEHAVIOURS = ['hold', 'loop', 'continue'];
export const DEFAULT_END = 'hold';
export const SHOW_SUFFIX = '.duckshow.json';

/** What each end behaviour does, for the picker's help text. */
export const END_DESCRIPTIONS = {
  hold: 'Stop and wait for an operator cue. The ducks end the show locally and stand.',
  loop: 'Play this show again from the top, until the operator advances.',
  continue: 'Load the next entry and play it. Costs a load fan-out plus the play lead.',
};

// ---------------------------------------------------------------------------
// documents
// ---------------------------------------------------------------------------

export function newSetlist(name = 'untitled set') {
  return { format: SETLIST_FORMAT, meta: { name }, entries: [] };
}

/** Parse text or an already-parsed object. Returns {doc} or {error}. */
export function parseSetlist(input) {
  let doc = input;
  if (typeof input === 'string') {
    try {
      doc = JSON.parse(input);
    } catch (err) {
      return { error: `not valid JSON: ${err.message}` };
    }
  }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) {
    return { error: 'expected a JSON object' };
  }
  const fmt = doc.format;
  if (typeof fmt !== 'string') return { error: 'missing "format": expected "duckset/1"' };
  const match = /^duckset\/(\d+)$/.exec(fmt);
  if (!match) return { error: `unrecognised format ${JSON.stringify(fmt)}: expected "duckset/N"` };
  if (Number(match[1]) !== 1) {
    // Equality, not "<= supported": a newer major means fields this build
    // would silently drop on the next save.
    return { error: `unsupported format major ${match[1]}: this build reads duckset/1` };
  }
  if (typeof doc.meta?.name !== 'string') return { error: 'meta.name must be a string' };
  if (doc.entries !== undefined && !Array.isArray(doc.entries)) {
    return { error: 'entries must be a list' };
  }
  for (const [i, raw] of (doc.entries || []).entries()) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return { error: `entries[${i}] is not an object` };
    if (typeof raw.id !== 'string') return { error: `entries[${i}].id must be a string` };
    if (typeof raw.show !== 'string') return { error: `entries[${i}].show must be a string` };
    if (raw.end !== undefined && typeof raw.end !== 'string') return { error: `entries[${i}].end must be a string` };
  }
  // Unknown fields survive untouched: the document handled from here IS the
  // parsed JSON, edited by the pure functions below.
  return { doc: { ...doc, entries: doc.entries ? [...doc.entries] : [] } };
}

export function serializeSetlist(doc) {
  return `${JSON.stringify(doc, null, 2)}\n`;
}

export function endOf(entry) {
  return typeof entry?.end === 'string' ? entry.end : DEFAULT_END;
}

// ---------------------------------------------------------------------------
// edit operations — every one returns a new document, none mutate
// ---------------------------------------------------------------------------

/** A unique entry id derived from the show's filename, so the JSON stays
 *  readable when someone opens it in an editor. `octet`, `octet-2`, ... */
export function nextEntryId(entries, showPath) {
  const base = (showPath.split('/').pop() || 'entry').replace(/\.duckshow\.json$/, '') || 'entry';
  const taken = new Set((entries || []).map((e) => e.id));
  if (!taken.has(base)) return base;
  for (let n = 2; ; n += 1) {
    const candidate = `${base}-${n}`;
    if (!taken.has(candidate)) return candidate;
  }
}

export function addEntry(doc, showPath, { end = DEFAULT_END, at = null } = {}) {
  const entry = { id: nextEntryId(doc.entries, showPath), show: showPath, end };
  const entries = [...doc.entries];
  entries.splice(at === null ? entries.length : clampIndex(at, entries.length + 1), 0, entry);
  return { ...doc, entries };
}

export function removeEntry(doc, id) {
  return { ...doc, entries: doc.entries.filter((e) => e.id !== id) };
}

export function setEntryEnd(doc, id, end) {
  return { ...doc, entries: doc.entries.map((e) => (e.id === id ? { ...e, end } : e)) };
}

export function setEntryLabel(doc, id, label) {
  const clean = typeof label === 'string' ? label.trim() : '';
  return {
    ...doc,
    entries: doc.entries.map((e) => {
      if (e.id !== id) return e;
      const next = { ...e };
      // An empty label means "use the show's own name", which is absence,
      // not an empty string: a saved "" would round-trip as a blank block.
      if (clean) next.label = clean;
      else delete next.label;
      return next;
    }),
  };
}

function clampIndex(i, length) {
  return Math.max(0, Math.min(length - 1, Math.trunc(i)));
}

/** Move the entry at `from` so it lands at index `to` in the resulting list. */
export function moveEntry(doc, from, to) {
  const entries = [...doc.entries];
  if (!entries.length) return doc;
  const src = clampIndex(from, entries.length);
  const dst = clampIndex(to, entries.length);
  if (src === dst) return doc;
  const [moved] = entries.splice(src, 1);
  entries.splice(dst, 0, moved);
  return { ...doc, entries };
}

export function moveEntryById(doc, id, to) {
  const from = doc.entries.findIndex((e) => e.id === id);
  return from < 0 ? doc : moveEntry(doc, from, to);
}

// ---------------------------------------------------------------------------
// readouts
// ---------------------------------------------------------------------------

/** docs/setlist-format.md: the default play lead, which every transition
 *  between entries costs at minimum. Mirrors DEFAULT_LEAD_MS in
 *  python/tools/showmaster.py. */
export const PLAY_LEAD_S = 1.5;

/**
 * Total runtime, from a `{showPath: durationSeconds}` index.
 *
 * `known` is the sum over entries whose duration this page has; `unknown`
 * counts the rest, so the UI can say "at least N" instead of a number that
 * silently treats a missing show as zero. A `loop` entry is counted once: it
 * runs until the operator advances, so its real length is not in the file.
 * `leadIn` is the transition cost the set cannot avoid.
 */
export function totalRuntime(doc, durations = {}) {
  let known = 0;
  let unknown = 0;
  let loops = 0;
  for (const entry of doc.entries || []) {
    const d = durations[entry.show];
    if (typeof d === 'number' && Number.isFinite(d) && d > 0) known += d;
    else unknown += 1;
    if (endOf(entry) === 'loop') loops += 1;
  }
  const transitions = Math.max(0, (doc.entries || []).length - 1);
  return { known, unknown, loops, leadIn: transitions * PLAY_LEAD_S };
}

export function formatClock(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '--:--';
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function entryLabel(entry, showMeta = null) {
  if (entry?.label) return entry.label;
  if (showMeta?.name) return showMeta.name;
  return (entry?.show || '').split('/').pop()?.replace(/\.duckshow\.json$/, '') || entry?.id || '(entry)';
}

// ---------------------------------------------------------------------------
// validation — mirrors python/duckset/validator.py
// ---------------------------------------------------------------------------

/**
 * @param doc    a parsed setlist
 * @param index  optional `{showPath: {name, duration, roles: [...]}}`. What
 *               python/duckset answers from disk, this answers from whatever
 *               the page has already fetched. Omit it and only the checks a
 *               document can make about itself run.
 */
export function validateSetlist(doc, index = null) {
  const issues = [];
  const error = (entry, message) => issues.push({ severity: 'error', entry, message });
  const warning = (entry, message) => issues.push({ severity: 'warning', entry, message });

  if (!String(doc?.meta?.name || '').trim()) error(null, 'meta.name is empty');

  const seen = new Map();
  (doc.entries || []).forEach((entry, i) => {
    const where = entry.id || `entries[${i}]`;
    if (!String(entry.id || '').trim()) error(where, 'entry id is empty');
    else if (seen.has(entry.id)) {
      error(where, `duplicate entry id ${JSON.stringify(entry.id)} (first used at entries[${seen.get(entry.id)}]); `
        + 'ids must be unique so reordering and operator cues stay unambiguous');
    } else seen.set(entry.id, i);

    if (!END_BEHAVIOURS.includes(endOf(entry))) {
      error(where, `unknown end behaviour ${JSON.stringify(endOf(entry))}: expected one of ${END_BEHAVIOURS.join(', ')}`);
    }

    const show = String(entry.show || '').trim();
    if (!show) { error(where, 'show path is empty'); return; }
    if (!show.endsWith(SHOW_SUFFIX)) {
      error(where, `show path must end in ${SHOW_SUFFIX}, got ${JSON.stringify(entry.show)}`);
      return;
    }
    if (index && !index[show]) warning(where, `show file not found on this machine: ${show}`);
  });

  const entries = doc.entries || [];
  if (entries.length && endOf(entries[entries.length - 1]) === 'continue') {
    warning(entries[entries.length - 1].id,
      "the last entry ends in 'continue' with nothing after it, so it holds");
  }

  if (index) issues.push(...castChangeWarnings(entries, index));
  return issues;
}

function castChangeWarnings(entries, index) {
  const out = [];
  let previousRoles = null;
  let previousId = null;
  for (const entry of entries) {
    const meta = index[entry.show];
    if (!meta || !Array.isArray(meta.roles)) { previousRoles = null; continue; }
    const roles = meta.roles;
    if (previousRoles) {
      const added = roles.filter((r) => !previousRoles.includes(r)).sort();
      const removed = previousRoles.filter((r) => !roles.includes(r)).sort();
      if (added.length || removed.length) {
        const parts = [];
        if (added.length) parts.push(`adds ${added.join(', ')}`);
        if (removed.length) parts.push(`drops ${removed.join(', ')}`);
        out.push({
          severity: 'warning',
          entry: entry.id,
          message: `cast changes from entry ${JSON.stringify(previousId)}: ${parts.join(', ')}. `
            + 'The operator has to know which ducks the next entry needs.',
        });
      }
    }
    previousRoles = roles;
    previousId = entry.id;
  }
  return out;
}
