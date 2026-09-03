#!/usr/bin/env python3
"""tools/bake/bake_show.py -- the native "Create Preview" baker.

Bakes a .duckshow into a pose cache the existing kinematic viewer can
replay (docs/viewer.md "Create Preview (baked physics)"). See
docs/bake-format.md for the cache format and every fidelity gap and
inference this baker makes; see requirements.txt for why this whole
directory is exempt from CLAUDE.md #1's stdlib-only rule.

    cd tools/bake
    .venv/bin/python3 bake_show.py <show.duckshow.json> <out.duckbake.json> [--duck ROLE]

Requires assets/microduck/ populated (docs/bake-parts.md §2) and this
directory's own venv (see requirements.txt) -- never required to author
or run a show; the editor and duck-agent never import anything here.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "python"))  # python/duckshow is stdlib-only; safe to import.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import duckshow  # noqa: E402  (path insert must come first)

from bakelib import duckmodel, policyset, posecache, sim  # noqa: E402


def _eprint(*a, **k):
    print(*a, file=sys.stderr, **k)
    sys.stderr.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("show", type=Path, help="path to a .duckshow.json file")
    parser.add_argument("out", type=Path, help="path to write the pose cache (.duckbake.json) to")
    parser.add_argument("--duck", metavar="ROLE", action="append", default=None,
                         help="bake only this role (repeatable). Default: every role in cast.")
    parser.add_argument("--assets", type=Path, default=REPO_ROOT / "assets" / "microduck",
                         help="path to the gitignored, user-supplied assets/microduck/ directory")
    parser.add_argument("--quiet", action="store_true", help="suppress per-role progress output")
    args = parser.parse_args(argv)

    if not args.show.exists():
        _eprint(f"error: show file not found: {args.show}")
        return 1

    show_bytes = args.show.read_bytes()
    try:
        show_doc = __import__("json").loads(show_bytes.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- CLI top-level, report and exit
        _eprint(f"error: {args.show} is not valid JSON: {exc}")
        return 1

    try:
        show = duckshow.loads_show(show_bytes.decode("utf-8"))
    except duckshow.DuckShowFormatError as exc:
        _eprint(f"error: {args.show} failed to load: {exc}")
        return 1

    issues = duckshow.validate(show)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        _eprint(f"error: {args.show} fails validation ({len(errors)} error(s)) -- fix the show before baking:")
        for i in errors[:20]:
            _eprint(f"  - {i}")
        return 1
    warnings = [i for i in issues if i.severity != "error"]
    if warnings and not args.quiet:
        _eprint(f"note: {len(warnings)} validation warning(s) in {args.show.name} (not fatal):")
        for i in warnings[:10]:
            _eprint(f"  - {i}")

    duration = show.meta.duration
    if not duration or duration <= 0:
        _eprint(f"error: {args.show} has no valid meta.duration")
        return 1

    all_roles = show.role_names()
    if args.duck:
        missing = [r for r in args.duck if r not in all_roles]
        if missing:
            _eprint(f"error: --duck role(s) not in cast: {missing}; cast is {all_roles}")
            return 1
        roles = args.duck
    else:
        roles = all_roles

    if not args.quiet:
        _eprint(f"loading assets/microduck/ from {args.assets} ...")
    t_load0 = time.time()
    try:
        duck = duckmodel.load_duck_model(args.assets / "mjcf")
        policies = policyset.load_policy_set(args.assets / "policies")
    except (FileNotFoundError, policyset.PolicyManifestError, ValueError) as exc:
        _eprint(f"error: {exc}")
        return 1
    t_load1 = time.time()
    if not args.quiet:
        _eprint(f"model + policy load: {t_load1 - t_load0:.2f}s "
                 f"(mujoco model nq={duck.model.nq} nu={duck.model.nu}, "
                 f"locomotion policy = {policyset.LOCOMOTION_POLICY_FILE})")

    results = {}
    t_bake0 = time.time()
    for idx, role in enumerate(roles):
        sampler = duckshow.Sampler(show, role)
        t_role0 = time.time()

        last_pct = [-1]

        def progress(role_name, k, n, _last_pct=last_pct):
            if args.quiet:
                return
            pct = (k * 100) // n
            if pct != _last_pct[0] and pct % 10 == 0:
                _last_pct[0] = pct
                _eprint(f"  [{role_name}] {pct:3d}% ({k}/{n} frames)")

        result = sim.simulate_role(
            duck, policies, show_doc, sampler, role,
            role_index=all_roles.index(role), role_total=len(all_roles),
            duration=duration, progress=progress,
        )
        t_role1 = time.time()
        results[role] = result
        if not args.quiet:
            n_fell = sum(1 for e in result.log if e.kind == "fell")
            n_skill = sum(1 for e in result.log if e.kind == "skill_unsimulated")
            status = "OK" if result.simulated else "NOT SIMULATED"
            _eprint(f"  [{role}] done in {t_role1 - t_role0:.2f}s -- {status}, "
                     f"{result.frame_count} frames, {n_skill} skill event(s) logged unsimulated"
                     + (f", FELL at t={next(e.t for e in result.log if e.kind == 'fell'):.2f}s" if n_fell else ""))

    t_bake1 = time.time()
    wall_clock_s = t_bake1 - t_bake0

    cache = posecache.build_cache(
        show_path=args.show, show_bytes=show_bytes, show_doc=show_doc,
        policies=policies, results=results, duration=duration,
        wall_clock_s=wall_clock_s, roles_requested=roles,
    )
    posecache.write_cache(cache, args.out)

    total_frames = sum(r.frame_count for r in results.values())
    out_size = args.out.stat().st_size
    _eprint(
        f"\nbaked {len(roles)} role(s), {total_frames} total frames, "
        f"physics wall-clock {wall_clock_s:.2f}s "
        f"({total_frames / wall_clock_s:.0f} frames/s)"
        if wall_clock_s > 0 else ""
    )
    _eprint(f"wrote {args.out} ({out_size / 1024:.1f} KiB)")
    if cache["unsimulated_roles"]:
        _eprint(f"NOT simulated (see log): {cache['unsimulated_roles']}")
    if cache["fallen_roles"]:
        _eprint(f"fell during bake: {cache['fallen_roles']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
