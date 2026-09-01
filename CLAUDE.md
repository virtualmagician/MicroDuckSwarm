# MicroDuckSwarm — project conventions

Read `docs/architecture.md` first; the docs in `docs/` are the contracts between components. **Change the doc first, then the code.**

Hard rules:

1. **Python is stdlib-only** (3.10+). No pip dependencies, ever — the duck-agent must run on a stock Armbian image, and the tools must run on any Mac. Tests use `unittest`.
2. **Swift is zero-third-party-deps** (Swift 6, strict concurrency), Network.framework for UDP. Same discipline as StageWizard.
3. **Protocol fidelity:** `docs/robotd-api.md` mirrors `duck-ipc-proto` in pollen-robotics/microduck (currently api v16). Never invent method names — re-verify upstream, update the doc, then implement. All robotd JSON fields are snake_case.
4. **Show-file compatibility:** unknown JSON fields are ignored everywhere; breaking `.duckshow` changes bump the `duckshow/N` major and get a loader migration.
5. **Show-night invariants:** no multicast for must-arrive messages; commands idempotent by `cmd_id`; a duck never improvises to catch up (late > 2 s = sit out); panic always works from any state.
6. This repo is public. No venue details, client names, or credentials in code, comments, or fixtures.

Run before committing: `cd python && python3 -m unittest discover -s tests` and (if SwarmLink touched) `cd SwarmLink && swift build && swift test`.
