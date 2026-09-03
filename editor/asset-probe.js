// asset-probe.js — pure "is assets/microduck/ usable" logic (docs/viewer.md
// "Assets are supplied, never vendored" / Feature B "real meshes when
// available"). No fetch call lives here: `probe` is an injected async
// function so this stays importable and testable with no network and no
// browser (editor/tests/asset-probe.test.mjs exercises it with fake
// resolved/rejected probes standing in for a real `fetch`).
//
// The one contract this file exists to guarantee: absence, a 404, a CORS
// failure, a file:// origin refusing fetch() outright, or any other
// network hiccup must *never* throw past this point and must never be
// reported as a console error — assets/microduck/ being missing is the
// expected, silent, default state for a fresh clone (CLAUDE.md licensing
// rule; docs/viewer.md: "Absent -> ... the kinematic viewer ... carries on
// exactly as before").

/**
 * True if `probe()` resolves to a response whose `.ok` is true; false for
 * every other outcome — a non-ok response (404 etc.), a rejected promise
 * (network/CORS failure), or `probe` throwing synchronously. Never throws.
 */
export async function detectAssetsAvailable(probe) {
  try {
    const res = await probe();
    return Boolean(res && res.ok === true);
  } catch (_) {
    return false;
  }
}

/**
 * Run several probes (e.g. one per required file) and report whether
 * *every* one succeeded, without short-circuiting the others — useful for
 * a single clear status line ("3 of 43 meshes missing") instead of
 * failing opaquely on the first 404. Never throws; a probe that rejects
 * or throws counts as that entry failing, not as the whole batch failing.
 */
export async function detectAllAvailable(probes) {
  const results = await Promise.all(probes.map((p) => detectAssetsAvailable(p)));
  return { ok: results.every(Boolean), okCount: results.filter(Boolean).length, total: results.length, results };
}
