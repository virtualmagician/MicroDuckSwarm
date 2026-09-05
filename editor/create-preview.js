// create-preview.js — pure decision logic for the editor's "Create Preview"
// button (docs/viewer.md "Create Preview (baked physics)"). No DOM, no
// fetch: duckshow-editor.html owns talking to scripts/editor_server.py's
// /api/capabilities and /api/bake endpoints and wires the results through
// the functions here, so the actual decisions (is the button usable right
// now, what does a job's progress read as, what belongs in the bake-log
// panel) are covered by editor/tests/create-preview.test.mjs without a
// browser — the same split bake-cache.js and asset-probe.js already use
// for this file's siblings.

/**
 * Normalize a show path to the repo-root-relative, leading-slash form the
 * server API and the editor's own `?show=` query param both use (e.g.
 * "/shows/octet/octet.duckshow.json"). Returns null for anything that
 * isn't plausibly that shape — an absolute http(s) URL, an empty string,
 * a path not ending in .duckshow.json — so a caller can treat null as
 * "no known server path" exactly like never having one (opened via the
 * file picker or drag & drop, where the browser's File API never exposes
 * a path a server request could use). This is a client-side shape check
 * only; scripts/editor_server.py independently re-validates and resolves
 * the path server-side before touching the filesystem — this function
 * grants no trust, it just avoids sending an obviously-useless request.
 */
export function normalizeShowPath(raw) {
  if (typeof raw !== 'string') return null;
  let path = raw.trim();
  if (!path) return null;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(path)) return null; // absolute URL, not a repo-relative path
  if (!path.startsWith('/')) path = `/${path}`;
  if (!path.endsWith('.duckshow.json')) return null;
  return path;
}

/**
 * Whether the Create Preview button should be enabled right now, and the
 * one-line reason it isn't when it's not — the button must never just sit
 * there dead with no explanation (docs/viewer.md).
 *
 * `capabilities` — the last GET /api/capabilities response, or null if it
 * hasn't loaded yet or the request failed outright (the page opened from
 * file://, or served by plain `python3 -m http.server`, which has no
 * /api routes at all — both look like a fetch failure, not a JSON body).
 * `showPath` — normalizeShowPath() of the currently loaded show's known
 * server path, or null.
 * `dirty` — true once the loaded show has been edited since it was last read
 * from disk (mirrors duckshow-editor.html's `state.showText === null`).
 * Accepted for callers that still report it, but it no longer gates anything:
 * the editor bakes the document it is holding rather than the file on disk.
 * See docs/viewer.md "Create Preview" for why that replaced refusing.
 */
export function createPreviewState({ capabilities, showPath, dirty }) {
  if (!capabilities) {
    return { enabled: false, reason: 'no bake server — open this page via ./scripts/edit.sh, not file:// or a plain http.server' };
  }
  if (!capabilities.available) {
    return { enabled: false, reason: capabilities.reason || 'baking is not available on this machine' };
  }
  if (!showPath) {
    return { enabled: false, reason: "this show wasn't opened from the repo (picked via Open… or dragged in) — load it via Load demo, ?show=, or scripts/edit.sh so the server knows its path" };
  }
  // No dirty gate. This used to refuse unsaved edits outright, because a bake
  // read the show from disk and an edited document would produce a cache whose
  // hash could never be checked against what was on screen. The editor now
  // POSTs the document itself (show_text) and pins state.showText to those
  // same bytes, so the cache is verified against exactly what was baked. The
  // old rule was unworkable in practice anyway: the browser cannot write the
  // file back, so every edit disabled the button until the author moved a
  // download over the original. `dirty` is still accepted and still reported
  // by callers; it simply no longer blocks.
  return { enabled: true, reason: null };
}

// ---------------------------------------------------------------------------
// Job-status state machine — GET /api/bake/<job id> returns one of these
// three states; everything else here is read-only projections of that JSON
// for display, not state transitions of their own.
// ---------------------------------------------------------------------------
export const BAKE_JOB_STATUSES = Object.freeze(['running', 'done', 'error']);

/** True once a job has reached a state that will never change again — the poll loop's own stop condition. */
export function isTerminalBakeStatus(status) {
  return status === 'done' || status === 'error';
}

/** One-line "which role, how far" progress text for a running job (docs/viewer.md: "show real progress"). */
export function formatBakeProgress(job) {
  const p = job && job.progress;
  if (!p || !p.role) return 'baking…';
  const of = p.role_total ? ` (${p.role_index}/${p.role_total})` : '';
  const pct = typeof p.pct === 'number' ? ` ${p.pct}%` : '';
  return `baking ${p.role}${of}${pct}…`;
}

/** One-line summary for a finished job — deliberately the same phrasing bake-cache.js's summarize() produces for a "Play Baked…"-opened cache, so the two paths read identically. */
export function formatBakeSummary(summary) {
  if (!summary) return '';
  const notes = [];
  // Two producers feed this, and they spell these two fields differently:
  // scripts/editor_server.py's _summarize_cache() ships snake_case in
  // job.summary (matching the on-disk duckbake/1 field names), while
  // bake-cache.js's summarize() returns camelCase for an already-parsed
  // cache. The Create Preview path passes the camelCase one; reading only
  // snake_case here meant `un`/`fell` were always 0 on that path, so a bake
  // containing an unsimulated role (roller mode) or a duck that fell over
  // reported a clean "8 roles, 64s" -- silently, and only on the path most
  // people use. Accept either spelling rather than making one caller convert.
  const unList = summary.unsimulated_roles ?? summary.unsimulatedRoles;
  const fellList = summary.fallen_roles ?? summary.fallenRoles;
  const un = Array.isArray(unList) ? unList.length : 0;
  const fell = Array.isArray(fellList) ? fellList.length : 0;
  const heldList = summary.held_roles ?? summary.heldRoles;
  const held = Array.isArray(heldList) ? heldList.length : 0;
  if (un) notes.push(`${un} unsimulated`);
  if (held) notes.push(`${held} partly held`);
  if (fell) notes.push(`${fell} fell`);
  return `${summary.roles} roles, ${summary.duration}s${notes.length ? ` · ${notes.join(' · ')}` : ''}`;
}

/** The message shown for a failed job — always the baker's own stderr (its log_tail), never a generic "bake failed" (docs/viewer.md: "Failure must be legible"). */
export function formatBakeError(job) {
  if (!job) return 'bake failed';
  const head = job.error || 'bake failed';
  const tail = Array.isArray(job.log_tail) ? job.log_tail.filter(Boolean) : [];
  if (!tail.length) return head;
  return `${head}\n${tail.join('\n')}`;
}

/**
 * Bake-log entries for the small panel surfaced after a bake (docs/viewer.md:
 * "the most useful output ... must not be buried") — capped so a show with
 * many logged skill/fall events still reads as a brief list, with a plain
 * count of anything past the cap rather than silently dropping it.
 */
export function bakeLogEntries(summary, maxEntries = 12) {
  const log = summary && Array.isArray(summary.log) ? summary.log : [];
  const entries = log.slice(0, maxEntries);
  return { entries, more: Math.max(0, log.length - entries.length) };
}
