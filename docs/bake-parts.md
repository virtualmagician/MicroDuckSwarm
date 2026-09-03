# Baked-physics preview — parts, setup, and feasibility

This is the acquisition and implementation plan for **Create Preview** (`docs/viewer.md`, "Create Preview (baked physics)"; `docs/architecture.md` M4+). Nothing here is implemented yet — this document exists so that work can start later without repeating the research that went into it.

It consolidates three independent research passes (referred to below as **Strand 1**, **Strand 2**, **Strand 3**) run the same day against `pollen-robotics/microduck_rl`, `pollen-robotics/microduck`, and the reference implementation `pollen-robotics/microduck-simulator` (an existing Hugging Face Space that already does an in-browser MuJoCo-WASM + onnxruntime-web bake, just built with npm/CDN tooling this repo can't use directly). Where the strands agree, that is stated plainly. **Where they disagree, or where only one of them could confirm something, that is called out explicitly rather than smoothed over** — this repo's own hard rules (protocol fidelity, no invented facts) apply to research the same as to code.

Nothing in this document authorizes fetching or committing any third-party asset. It describes what a person would do, on their own machine, if they choose to.

---

## 1. Parts list

### 1a. Geometry — ⚠ CC BY-SA-NC, user-supplied only, never vendored

All of the following live in `pollen-robotics/microduck_rl`, under `src/mjlab_microduck/robot/microduck/`. This is exactly the material `docs/viewer.md` §"Assets are supplied, never vendored" already governs: it goes in the gitignored `assets/microduck/` directory (already present in `.gitignore`), fetched by whoever runs the tool, never committed to this repo.

| Artifact | Size | Needed for | Status |
|---|---|---|---|
| `robot_walk.xml` + `robot_walk_backlash.xml` | 32,017 B / 34,104 B | Legged walk-only model (trunk/head contacts stripped — cheap falls) | Confirmed present; license inferred |
| `robot_allcollisions.xml` + `_backlash` | 32,898 B / 34,985 B | Full-collision legged model — needed for stand/sit/ground-pick/kick/roulade, i.e. every `do` skill event and the `pose.z` crouch | Confirmed present; license inferred |
| `robot_allcollisions_rollers.xml` + `_backlash` | 35,688 B / 37,775 B | Standalone roller-skate model (own passive wheel joints/bodies, does **not** `<include>` the leg files) — for `mode: "roller"` | Confirmed present; license inferred |
| `scene.xml`, `scene_walk.xml`, `scene_backlash.xml`, `scene_walk_backlash.xml`, `scene_rollers.xml`, `scene_ball.xml` | 1.2–2.6 KB each | Floor + light + STAND/SIT/FOLD keyframes wrapping a robot file | Confirmed present; license inferred |
| `additional.xml`, `joints_properties.xml`, `sensors.xml` | 611 B / 1,780 B / 357 B | Injected into every export (actuator/sensor definitions) | Confirmed present; license inferred |
| `ball.xml` | 706 B | 70 mm/15 g prop for the `kick_left`/`kick_right` skills | Confirmed present; license inferred |
| `config_mjcf_*.json` (6 files) | ~1.8–2.2 KB each | `onshape-to-robot` export recipes (CAD source URL, `max_stl_size: 1.0` MB, sed post-processing) — reference only, not needed at bake time | Confirmed present; license inferred |
| 47 STL meshes, `assets/` subdir | 26,080,648 B (~26.08 MB) | All visual/collision geometry | Confirmed present; license is the one explicit statement in the README (below) |
| 47 matching `.part` sidecars | ~18 KB total | `onshape-to-robot` metadata, not geometry — safe to skip | Confirmed present |

**Explicitly excluded:** `src/mjlab_microduck/robot/xl330_test_bench/` (its own MJCF + 11 STL, 774,024 B) is a servo calibration test-bench rig, not the duck. Don't fetch it.

**License, quoted verbatim** (`microduck_rl/README.md`, License section, Strand 1):
> This project is licensed under the Apache 2.0 License. See the LICENSE file for details.
> Hardware design files are licensed under Creative Commons BY-SA-NC.

That is the *entire* licensing text in the repo — no per-directory `LICENSE`/`NOTICE`, no SPDX headers in the XML/JSON files fetched, no explicit path enumeration. **All three strands independently converge on the same conservative reading** — everything in the table above (MJCF, scene wrappers, export configs, STL, `.part`) is CAD-derived or a direct export of the physical assembly, so it falls under "hardware design files" = CC BY-SA-NC — but **all three also flag this as their best-supported inference, not a directly quotable per-file rule.** Treat it exactly as `docs/viewer.md` already does: non-commercial, user-supplied, never vendored, never relabeled by this MIT repo.

Two duplicate-mesh pairs share git blob SHAs under two filenames each (e.g. `left_upper_leg.stl` / `upper_leg_left.stl`) — the export pipeline writes onshape-native and semantic names for the same geometry. Harmless; don't worry about it.

### 1b. Trained ONNX policies — Apache-2.0

Nine policies live in `pollen-robotics/microduck`, directory `policies/` (not in `microduck_rl` — that repo has only the exporter script, no weights).

| File | Size | Boot behavior in the reference Space |
|---|---|---|
| `alpha_walking.onnx` | 793,705 B | eager |
| `alpha_stand.onnx` | 793,705 B | eager |
| `alpha_sitstand.onnx` | 793,695 B | eager |
| `alpha_ground_pick.onnx` | 793,685 B | eager |
| `ball_kick_left.onnx` | 793,685 B | eager |
| `ball_kick_right.onnx` | 793,685 B | eager |
| `roller.onnx` | 793,685 B | lazy (roller mode) |
| `roller_crouch.onnx` | 793,685 B | lazy (roller mode) |
| `roulade.onnx` | 793,685 B | eager |
| `policies/README.md` | 3,547 B | — provenance + contract notes |

All ≈ 793.7 KB, `obs[1,61] -> actions[1,14]` (confirmed independently by `docs/robotd-api.md`'s own `robot.modelApi`/policy-slot description — real robotd validates policies against this exact shape). **The observation normalizer is baked into the ONNX graph** — `scripts/export.py` traces `actor(normalizer(obs))`, so a consumer does not reimplement normalization (Strand 1).

**Provenance caveat, quoted from `policies/README.md`:** these specific files were *"Copied from `apirrone/microduck_runtime` at commit `5f3b314`"* (roulade at a different commit). That source repo returned HTTP 404 when Strand 1 checked it — private, renamed, or deleted, unconfirmed which. It is not possible from this repo's research to verify these exact binaries were produced by `microduck_rl`'s current training code.

**Licensing — flagged disagreement between strands.** Strand 1 and Strand 2 each independently confirmed Apache-2.0 for these files two ways: the `pollen-robotics/microduck` repo's GitHub-reported license *and* the standalone Hugging Face model `pollen-robotics/microduck-policies`, tagged `license: apache-2.0`. **Strand 3's own parts list, working from README-level access only, marked this same fact `"confirmed": false`.** Two independently-sourced confirmations against one unconfirmed flag — treat Apache-2.0 as the working assumption, but this is exactly the kind of thing worth a five-minute re-check (`https://api.github.com/repos/pollen-robotics/microduck` → `license.spdx_id`) before it matters for redistribution.

### 1c. Browser-path runtime (only if the browser bake is built — see §3)

| Artifact | Source | License | Size | Status |
|---|---|---|---|---|
| `@mujoco/mujoco@3.11.0`, single-thread build | npm / jsDelivr; upstream `google-deepmind/mujoco`, `wasm/` dir | Apache-2.0 | `mujoco.js` 292 KB + `mujoco.wasm` 10.12 MB ≈ 10.4 MB | Confirmed (npm registry + jsDelivr data API) |
| `onnxruntime-web@1.27.0` | npm / jsDelivr; upstream Microsoft `onnxruntime` | MIT | `ort.min.mjs` 360 KB + wasm sidecar 13.48 MB ≈ 13.8 MB | Confirmed size via jsDelivr; **the exact single-threaded-only build size was not independently isolated** — the reference Space loads the file literally named `ort-wasm-simd-threaded.wasm` even with `numThreads=1` set at runtime, so this is the number that applies whether or not a from-scratch build re-uses that exact file |
| The 9 ONNX policies (§1b) | same files | Apache-2.0 | ≈ 5.56 MB (7 eager) + ≈ 1.59 MB (2 lazy) | Confirmed |
| `microduck.glb` (decimated visual rig, doubles as the WASM VFS mesh source) | reference Space's own asset copy — **not** the same file as `microduck_rl`'s STLs | CC BY-SA-NC (derived hardware geometry) | 1.28 MB | Confirmed to exist; still gated by the never-vendor rule same as §1a |
| `kinematics.json` / `kinematics_rollers.json` | reference Space's own asset copy | CC BY-SA-NC (derived) | 26.6 KB / 29.6 KB | Confirmed to exist |

Cold-boot one-time download for the legs variant: **≈ 31 MB total**, cacheable by the browser after first load. Only 9 of 38 mesh references in `robot_allcollisions.xml` are actually used by collision geoms, and all 9 already ship inside the visual GLB — so, for that specific asset pipeline, zero extra mesh bytes beyond the GLB. That optimization is specific to how the reference Space packages meshes (GLB reused for both render and physics); a from-scratch implementation against raw STL would not get it for free.

### 1d. Native-path runtime (only if the native bake is built — see §3)

| Artifact | Source | License | Size | Status |
|---|---|---|---|---|
| `mujoco` (official Python bindings) | PyPI, prebuilt wheels incl. Apple Silicon | Apache-2.0 | prebuilt wheel, tens of MB | Confirmed to exist and be installable |
| `onnxruntime` | PyPI | MIT | prebuilt wheel, tens of MB | Confirmed to exist and be installable |
| `numpy` | PyPI | BSD-3-Clause | small | Not independently checked by any of the three research strands — included here because a native bake driver needs array math; this is well-established public fact, not research output |

None of these are stdlib. See §3 for the CLAUDE.md tension this creates and §4 for what remains unresolved about it.

---

## 2. Setup: populating `assets/microduck/`

**This section is instructions for a person to run themselves, later, on their own machine — not something this repo, this document, or any automated tool in it should do on its own.** `assets/microduck/` is already in `.gitignore`.

### Proposed layout

No code in this repo reads this directory yet, so this layout is a **proposal**, not a contract — pick it (or change it) when the bake driver is actually written, and update this doc if it changes:

```
assets/microduck/
  mjcf/
    robot_walk.xml
    robot_walk_backlash.xml
    robot_allcollisions.xml
    robot_allcollisions_backlash.xml
    robot_allcollisions_rollers.xml
    robot_allcollisions_rollers_backlash.xml
    scene.xml  scene_walk.xml  scene_backlash.xml  scene_walk_backlash.xml
    scene_rollers.xml  scene_ball.xml  ball.xml
    additional.xml  joints_properties.xml  sensors.xml
    assets/              ← the 47 STL files (this is the MJCF compiler's meshdir="assets",
                            so keeping this exact relative name lets the XML files load
                            unmodified — do not rename it)
  policies/
    alpha_walking.onnx  alpha_stand.onnx  alpha_sitstand.onnx  alpha_ground_pick.onnx
    ball_kick_left.onnx  ball_kick_right.onnx  roller.onnx  roller_crouch.onnx
    roulade.onnx
    README.md
```

### Fetch commands

Uses `git sparse-checkout` with `--filter=blob:none` (partial clone — pulls only the requested subtree's blobs, not the whole repo's history or the `xl330_test_bench` files). `git` is preinstalled on macOS, so this doesn't strain the "any Mac" requirement.

```bash
mkdir -p assets/microduck
cd assets/microduck

# --- 1. Geometry (CC BY-SA-NC — see §1a; personal/reference use, not for
#         redistribution, until Pollen confirms otherwise) ---
git clone --filter=blob:none --sparse --depth 1 \
  https://github.com/pollen-robotics/microduck_rl.git .src-rl
git -C .src-rl sparse-checkout set src/mjlab_microduck/robot/microduck
mv .src-rl/src/mjlab_microduck/robot/microduck mjcf
rm -rf .src-rl

# --- 2. Trained policies (Apache-2.0 — see §1b) ---
git clone --filter=blob:none --sparse --depth 1 \
  https://github.com/pollen-robotics/microduck.git .src-policies
git -C .src-policies sparse-checkout set policies
mv .src-policies/policies policies
rm -rf .src-policies

cd ../..
```

If `git sparse-checkout` isn't available (very old git), the 9 policy files are small enough to fetch individually instead:

```bash
base="https://raw.githubusercontent.com/pollen-robotics/microduck/main/policies"
for f in alpha_walking alpha_stand alpha_sitstand alpha_ground_pick \
         ball_kick_left ball_kick_right roller roller_crouch roulade; do
  curl -fsSL -o "assets/microduck/policies/${f}.onnx" "${base}/${f}.onnx"
done
curl -fsSL -o assets/microduck/policies/README.md "${base}/README.md"
```

### Verify the fetch

Sanity-check against the confirmed sizes in §1a/§1b — a short read or a bad partial fetch will show up as a wrong file count or a wildly different total:

```bash
find assets/microduck/mjcf/assets -name '*.stl' | wc -l    # expect 47
du -sh assets/microduck/mjcf/assets                          # expect ~26 MB
du -sh assets/microduck/policies                             # expect ~7.1 MB
shasum -a 256 assets/microduck/policies/*.onnx > /tmp/policy-hashes.txt
```

The per-file sha256 output is also exactly the value `docs/duckshow-format.md`'s `requires.policies[].sha256` field expects, and a natural candidate for the "policy versions" half of the bake cache key `docs/viewer.md` already specifies (`"a cache keyed by show hash and policy versions"`) — hash the actual bytes in `assets/microduck/policies/` rather than trusting a version string, since these are exactly the files whose provenance (§1b) couldn't be fully traced.

### What's still missing after this

Populating `assets/microduck/` gets you the model and the trained weights. It does **not** get you a bake driver — nothing in either source repo runs the MuJoCo step loop against these files in a way this repo can import (see §3). That code has to be written.

---

## 3. Feasibility and recommendation

### 3.1 What's actually confirmed about the simulation contract

Two facts, each confirmed **independently by two strands reading two different sources** — the more solid category of finding here:

- **Control rate: 50 Hz.** `microduck_rl`'s README states policies are *"trained here at 50 Hz"* (Strand 1); the reference Space's own `constants.js` computes `CTRL_DT = TIMESTEP × DECIMATION` (Strand 2).
- **Physics timestep 0.005 s (200 Hz), decimation 4.** Strand 1 read this directly out of the external `mjlab` framework's `SimulationCfg` default (`mujocolab/mjlab`, `src/mjlab/tasks/velocity/velocity_env_cfg.py`), which `microduck_rl`'s own task configs never override. Strand 2 read the *same numbers* independently, out of the reference Space's `constants.js` and its injected `<option timestep="0.005">`. Neither `robot_walk.xml` nor `robot_allcollisions*.xml` carries a MuJoCo `<option>` element of its own (both strands confirm this by direct file read) — **whoever writes the bake driver has to inject the timestep**, it is not already in the exported MJCF.

**Disagreement to flag:** Strand 3's own feasibility arithmetic used **500 Hz** physics ("32,000 steps at 500 Hz over 64 s"), which its own Unknowns section labels as coming from *"the task's own framing"* — i.e. an assumption, not something it verified. That is 2.5× the confirmed 200 Hz figure above. §3.2 recomputes using the confirmed number; Strand 3's original headline numbers are shown alongside for transparency, not silently replaced.

Also confirmed and worth calling out because it's easy to miss: the trained **action space is 14-DOF and excludes the mouth**. `docs/robotd-api.md`'s `JOINT_NAMES` lists 15 (left leg ×5, neck/head/**mouth** ×5, right leg ×5); the RL action space is *"0–4 left leg (hip_yaw, hip_roll, hip_pitch, knee, ankle), 5–8 neck/head (neck_pitch, head_pitch, head_yaw, head_roll), 9–13 right leg"* — 5+4+5 = 14, no mouth term anywhere. **A physics bake cannot make the beak move** — `mouthOpen` has to keep passing straight through from the `.duckshow` mouth track to the renderer exactly as the kinematic path already does it, physics or not.

### 3.2 Bake time, recomputed against the confirmed numbers

Worked example: `shows/octet/octet.duckshow.json` — 8 ducks, 64 s duration (a real fixture already in this repo, not a hypothetical).

Total physics steps for the whole show = 8 ducks × 64 s × 200 Hz = **102,400 steps** (Strand 3's own number, 256,000, used the unconfirmed 500 Hz and is 2.5× too high).

Total policy inferences = 8 × 64 × 50 Hz = **25,600 forward passes** (this one doesn't change — both the 500 Hz and 200 Hz assumptions agree on the 50 Hz control rate).

**Native MuJoCo, single core:** Strand 3's cited native step-rate range is 30,000–75,000 steps/sec/core, itself an extrapolation from MuJoCo's own published 27-DOF reference-humanoid benchmarks (~30k steps/sec measured at one cited figure, ~75k steps/sec implied by a separate community re-run) rather than a duck-specific measurement — flagged in Strand 3's own sourcing, not upgraded here. 102,400 ÷ 30k–75k = **1.4–3.4 s** of single-core physics compute for the whole show (down from Strand 3's original 3.4–8.5 s, purely from the timestep correction). ONNX inference native is not separately benchmarked by any strand for this exact model; a 61→14 MLP at ~793 KB is well within "sub-millisecond per call" territory for native `onnxruntime` (this is *this document's* estimate, not a strand finding — flagged as such), adding roughly a few seconds of single-core CPU time across the whole show.

**Parallelized 8-way** (one OS process per duck — ducks never interact physically, so this is embarrassingly parallel, same shape the doc's Web Worker plan already assumes): low single digits of seconds wall-clock for physics + inference combined, plus Python/`mujoco`/`onnxruntime` import overhead per cold process (typically sub-2s per process; not independently measured for this stack by any strand — flagged as an estimate).

**Browser, MuJoCo-WASM + onnxruntime-web, one Worker per duck:** applying Strand 3's cited 2–5× WASM-vs-native slowdown (itself a general-purpose figure, not MuJoCo-WASM-specific — no one has published a benchmark for this exact workload, confirmed by Strand 3's own search of `google-deepmind/mujoco`'s and `zalo/mujoco_wasm`'s READMEs) to the corrected 1.4–3.4 s native figure gives ≈ 2.8–17 s of single-core WASM compute, ÷ 8 parallel Workers ≈ 0.35–2.1 s wall-clock physics, plus Strand 1's own WASM-inference estimate (~13–26 s total CPU across the show at a pessimistic 0.5–1 ms/call, ÷ 8 ≈ 1.6–3.3 s wall-clock), plus a **per-Worker WASM instantiate tax** Strand 3 estimated at "a few hundred ms to ~1–2 s," paid once if Workers stay warm across repeated "Create Preview" presses and every time otherwise, plus the **≈ 31 MB one-time download** from §1c (cacheable after first load, not paid every bake).

**Bottom line: both paths land in low-single-digit-to-low-double-digit seconds of wall-clock time for an 8-duck, 64 s show** — both are comfortably faster than the show itself, so "does it bake fast enough to be useful" is not the deciding question. **Every throughput number above is an extrapolation from measurements of a different workload, not a direct benchmark of this one** — Strand 1 and Strand 3 both say so explicitly about the native/WASM step rates, and no strand benchmarked onnxruntime-web or native `onnxruntime` against this exact 61→14 model shape. Treat this whole subsection as "very likely the right order of magnitude," and run an actual timed spike before committing either architecture to a schedule.

### 3.3 Recommendation: build the native bake first

Strand 3, the strand that actually weighed this tradeoff head-on, recommends a native Python (or Rust) helper over the in-browser WASM path, and this document agrees, for the same three reasons:

1. **Speed margin is larger for native**, per §3.2 — no WASM tax, no per-Worker instantiate cost, no ~31 MB fetch, no JS↔WASM marshalling per ONNX call. It is very likely the faster of the two, possibly by enough that the difference matters at a venue on bad WiFi (the first-run 31 MB download; the pip wheels only need to be installed once, offline-capable after that).
2. **Fidelity is cheaper to improve natively.** The real, specific gap here (not the generic sim-to-real one `docs/viewer.md` already names) is that training used the **BAM actuator model** — a voltage-control-law model of the Dynamixel XL330 (back-EMF, Coulomb/Stribeck/load-dependent friction, domain-randomized battery voltage 6.5–8.2 V, voltage sag, command delay) — not classic MuJoCo's stock `<position>`/`<general>` actuator primitives. Re-stepping the exported MJCF in **plain** MuJoCo (WASM or native, doesn't matter) drives the policy against a physical plant it wasn't quite trained for. This is a training-engine-vs-bake-engine gap sitting *in front of* the ordinary sim-to-real gap, and it applies identically to both paths — porting `microduck_rl`'s actuator code (`src/mjlab_microduck/actuator/friction_dr_bam.py`, Python) is straightforwardly cheaper in a native Python bake than reimplementing that voltage-control-law math in JavaScript.
3. **Integration cost is lower.** `docs/viewer.md` §"Pose in, pixels out" already commits the renderer to a plain pose-array contract. A native helper just needs to emit that array as JSON, matching the format the editor's `<input type=file>` Open flow already knows how to load — no Worker orchestration, no multi-MB WASM binaries anywhere near the editor's load path, no COOP/COEP question at all.

**Where browser wins, honestly:** ergonomics. "One button in the tab already open" beats "leave the browser, run a CLI, come back and load a file." This repo already has a pattern that closes most of that gap without going all the way to in-browser WASM: `SwarmLink`'s `swarmctl serve` already exposes local capability to a browser-hosted UI over HTTP (`docs/osc-facade.md`). A small local companion process that "Create Preview" POSTs to, which runs the native bake and hands back the pose-cache file, keeps the one-button UX while doing the actual physics natively — more plumbing than either pure option, but it is the version that gets native speed/fidelity *and* the workflow the doc's "one button" framing wants. Worth designing that way from the start rather than bolting it on later.

**A real, unresolved tension this creates:** `CLAUDE.md`'s hard rule #1 is unconditional — *"Python is stdlib-only (3.10+). No pip dependencies, ever."* A native bake helper needs `mujoco`, `onnxruntime`, and `numpy` (§1d), all real pip dependencies. `docs/viewer.md` already carves out an equivalent exemption for the *JavaScript* preview module ("That rule stands for the editor. The preview is a separate, optional module"), and the same shape of carve-out — a script that's never imported by `duck_agent`/`python/duckshow`/`python/mock_duck`/`showmaster.py`, gitignored venv, documented one-time install, never touching the on-duck or show-night path — would fit a native bake helper well. But `CLAUDE.md`'s current wording doesn't say that; it says *never*. **This document does not resolve that tension** — that's a `CLAUDE.md` edit, and this document was scoped to leave `CLAUDE.md` and everything else alone. Whoever picks up implementation should raise it explicitly (with Marco) before writing `pip install` into any script, rather than deciding it by omission. Until it's resolved, a native bake helper should probably live in a directory structurally separate from `python/` (e.g. a new top-level `bake/` or `tools/bake/`, not `python/tools/`) so it's never ambiguous which parts of the tree are stdlib-only.

### 3.4 What this means for `docs/viewer.md`

If the native-first recommendation is adopted, `docs/viewer.md`'s "Create Preview" section would need updating in three places (not done here — that section is outside this document's mandate to touch; flagging exactly what would change so it's a small, deliberate edit later):

- The pipeline diagram (`.duckshow ──kinematic sampler──▶ intents ──▶ [MuJoCo WASM + onnxruntime-web, per duck] ──▶ baked pose cache ──▶ the same renderer`) currently names the browser stack specifically; it would need to describe a native step (possibly fronted by the local-companion-process pattern from §3.3) instead, or as well.
- The **Dependencies** paragraph currently frames the exemption as being from "the no-CDN/no-build rule the editor holds" — true for the JS module, but doesn't cover a Python helper needing pip packages, which is a different rule (`CLAUDE.md` #1, not the editor's own no-CDN rule). See the `CLAUDE.md` tension in §3.3.
- The **Honesty** paragraph should get one added line about actuator-model fidelity (§3.3 point 2) — it currently only names generic sim-to-real gaps and floor fidelity; the BAM-vs-stock-actuator gap is more specific and applies before hardware ever enters the picture.

---

## 4. Open questions

Things no strand could confirm without hardware, without running the actual code, or without information nobody has yet. Consolidated and deduplicated from all three strands' own Unknowns sections, plus a few this document's cross-referencing surfaced on top.

**Licensing / provenance**
- Whether "hardware design files" in `microduck_rl`'s license clause is meant file-by-file to include the MJCF XML (not just STL/CAD) is not stated anywhere fetched — all three strands read it the same conservative way, but none of them found a sentence that actually enumerates paths.
- The ONNX policy weights' Apache-2.0 status: confirmed two ways by Strand 1 and Strand 2, flagged unconfirmed by Strand 3's own parts list. Worth a direct re-check (see §1b) before it matters for redistribution.
- `apirrone/microduck_runtime`, the repo `policies/README.md` names as the actual source of the shipped `.onnx` files, returns HTTP 404. Private, renamed, or deleted — nobody could tell from outside. Means the shipped policies' relationship to `microduck_rl`'s current training code as published is not fully traceable.
- Whether Pollen would grant explicit permission for this kind of reuse is a question for Pollen, not for research against their public repos. `docs/viewer.md` already suggests asking; still unsent as of this document.

**Simulation fidelity**
- Whether the BAM actuator model is expressed as a stock MuJoCo `<actuator>` element in the exported MJCF, an engine plugin, or purely external Python/Warp-side torque math layered on top of a simple motor actuator — this determines exactly how much porting work closing the fidelity gap in §3.3 actually takes, and none of the three strands opened `microduck_rl`'s actuator source file to check (Strand 3 read only the README's description).
- The exact 48-dim proprioception breakdown (3 gyro + 3 gravity + 42 joint values) is well supported by `policies/README.md`'s description of the legacy 51-D format, but the internal 3-way split of the "42 joint values" per servo (position/velocity/previous-action, the standard convention, versus something else) is Strand 1's inference from standard practice, not a literal quote.
- **New gap this document surfaces, not found by any strand directly:** `docs/duckshow-format.md`'s `pose` track carries only `z`/`roll`/`pitch` (3 values), but the RL command vector's `body_pose` slot is 6-dimensional. The `head` track's 4 values (`neck_pitch`, `head_pitch`, `head_yaw`, `head_roll`) line up count-for-count with the command vector's 4-dim `head_pose` slot, which is suggestive but not confirmed to be the same representation. Whichever bake path gets built will need to read `mjlab`'s actual command-vector construction code (none of the three strands did) to know what the other 3 `body_pose` values should be — plausibly `x`/`y`/`yaw`, left at zero if not separately commanded, but that's this document's guess, not a finding.
- Whether NVIDIA Warp's CPU fallback device is practical on Apple Silicon — relevant only if someone later wants to close the training-engine gap completely by baking through the actual training engine (mjlab/Warp) instead of classic MuJoCo. Nobody checked.

**Engineering unknowns**
- Whether `@mujoco/mujoco` actually runs under Node (relevant only to the browser path, or to a Node-based variant of it) — Strand 2 read the package as a standard ESM/Emscripten module with nothing browser-only visible, but did not run it. A short spike was recommended by Strand 2 itself and not done in this research pass.
- No step-rate or inference-latency number in this document (or either research strand) was actually measured against this specific model — see the callout at the end of §3.2. A short timed spike (native `mujoco`+`onnxruntime` stepping the exported MJCF against one policy for a few thousand steps) would replace most of §3.2's ranges with real numbers cheaply, and should happen before committing to either architecture.
- Cross-machine determinism: MuJoCo is deterministic given a fixed build/platform/thread-count with sensor noise off, single-threaded stepping avoids the main source of nondeterminism — but a bake produced on one machine/browser is not guaranteed byte-identical to the same show baked elsewhere (different JS engines can emit different native code from the same WASM; different CPUs handle FMA/SIMD slightly differently; a contact-rich biped is exactly the kind of chaotic system where early floating-point differences visibly diverge by the end of a show). `docs/viewer.md`'s current "cache keyed by show hash and policy versions" framing is necessary but not sufficient for *cross-machine* reproducibility — worth either fingerprinting the cache key by engine+OS/arch, or documenting explicitly that a bake is reproducible per-machine, not a portable "golden" artifact.
