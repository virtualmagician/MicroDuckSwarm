# tools/bake

The native **Create Preview** baker (`docs/viewer.md` "Create Preview (baked physics)"). Turns a `.duckshow` into a pose cache the existing kinematic viewer can replay, by driving a real MuJoCo simulation of each cast role against the shipped ONNX policies.

**Optional developer tool, not part of the stdlib-only rule.** See `requirements.txt`'s header and `docs/bake-format.md` "Why this is exempt from CLAUDE.md #1". Never imported by `python/duckshow`, `python/duck_agent`, or `python/mock_duck`; the whole repo works with this directory absent.

Full format/behavior documentation: **`docs/bake-format.md`** — the cache schema, the observation/action layout and exactly which parts are confirmed vs. inferred, what isn't simulated yet, and a measured timing run. This README is just setup + usage.

## Setup

Needs Python **3.12** specifically (see `requirements.txt`) and `assets/microduck/` populated (gitignored, user-supplied — `docs/bake-parts.md` §2; this tool never fetches it).

```bash
cd tools/bake
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

```bash
# bake a whole show
.venv/bin/python3 bake_show.py ../../shows/octet/octet.duckshow.json /path/to/octet.duckbake.json

# bake one role only, for fast iteration
.venv/bin/python3 bake_show.py ../../shows/octet/octet.duckshow.json /path/to/lead.duckbake.json --duck lead

# --duck is repeatable; --quiet suppresses per-role progress; --assets overrides
# the assets/microduck/ path (default: <repo>/assets/microduck)
```

The show is loaded and validated with the canonical `python/duckshow` parser (via `sys.path`, not duplicated); a show that fails validation is refused rather than baked around. Exit code is non-zero on any error, with a one-line message to stderr.

## Layout

```
tools/bake/
  requirements.txt   # pinned deps + why this venv needs Python 3.12
  bake_show.py        # CLI entry point
  bakelib/
    duckmodel.py       # MJCF load, timestep injection, STAND-keyframe constants
    bam_actuator.py     # ported BAM XL330 actuator model (replaces the stock <position> actuator)
    marks.py              # ports editor/duckshow-viewer.js's resolveMark/defaultMarkFor
    observation.py        # the 61-dim obs: 48 proprioception + 13 command
    policyset.py           # manifest.json + onnxruntime session + hashing
    sim.py                  # per-role physics loop, bake log, fall detection
    posecache.py             # cache assembly, cache-key hashing, JSON writer
```
