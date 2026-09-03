#!/usr/bin/env python3
"""Verify the e2e demo: parse two mock-duck intent logs and assert that both
ducks received their choreography with tight cross-duck timing.

Usage: verify_e2e.py <lead-log.jsonl> <wing-log.jsonl>
Exit 0 when every check passes; prints a report either way.

Beyond presence/count checks, this also samples actual params values at
known points on shows/demo/demo.duckshow.json's curves, checks the
achieved streaming rate, checks that roles with no locomotion/mouth/pose
track never emit those methods, and checks the end-of-show ordering
(zero robot.move immediately before robot.stop). These are pinned to
that specific demo show; if the show file changes, these numbers need
updating alongside it.
"""
import json
import sys

# Tolerances are for localhost mock runs; real-hardware budgets live in docs.
CROSS_DUCK_TOL_S = 0.10
RELATIVE_TOL_S = 0.15
EVENT_TIME_TOL_S = 0.06

# Every tolerance below is quoted for a REFERENCE tick period and then scaled
# by the rate actually achieved in this run (see tolerance_scale). A sparse
# stream genuinely cannot resolve a curve or an event time as finely as a
# dense one, so a fixed tolerance silently encodes the speed of whichever
# machine it was calibrated on. Observed: ~26 ms per tick on a dev Mac,
# ~62 ms on a GitHub macOS runner, and 108 ms on a bad day for the same
# runner running the same code. That is a 4x spread with no code change
# behind it, which is why these scale rather than sit still.
REFERENCE_TICK_S = 0.026

# Curve-sample tolerances: generous relative to the actual smoothstep
# deviation at these points (a few ms of arrival jitter around an exact
# or near-exact keyframe moves the sampled value by well under 1e-3 in
# practice -- see scripts/verify_e2e.py's own e2e run for the numbers
# these were calibrated against), but tight enough that a sampler
# regression (wrong curve, wrong keyframe, wrong role) still fails them.
HEAD_ANGLE_TOL = 0.03
POSE_Z_TOL = 0.005
VX_TOL = 0.02
# The demo show's mouth gape is a smoothstep 0 -> 1 over 9.0-9.3 s, then back
# to 0 by 9.8 s. We sample that curve on the agent's tick grid, so the closest
# sample to the apex is off by up to half a tick period, and the ascending leg
# is the steep one. Worked out against real tick gaps:
#
#   gap  26 ms (dev Mac)     -> best nearest sample 0.998
#   gap  62 ms (CI average)  -> 0.970
#   gap  94 ms (CI p95)      -> 0.934
#   gap 120 ms               -> 0.896
#
# So anything at or above ~0.97 is unreachable on a loaded runner and would
# fail for reasons that have nothing to do with the choreography. 0.85 still
# fails loudly on the regressions that matter: a mouth track that never plays
# reads 0.0, and a mis-scaled one reads about half.
MOUTH_OPEN_MIN_AT_PEAK = 0.85

# A sample must land within this many seconds of the target show_time to
# count as "the sample near that point" -- generous enough to tolerate a
# sparse/degraded stream, tight enough that a genuinely missing stream
# (nothing nearby at all) is correctly treated as no sample.
SAMPLE_WINDOW_S = 0.5

# Rate floor: the tick loop is nominally 50 Hz (docs/architecture.md), but
# a threaded Python process on a shared/non-RT machine measurably runs
# below that: ~38 Hz on a quiet dev Mac, ~16 Hz on a GitHub macos-latest
# runner with two mock ducks, two agents and swarmctl all sharing the VM.
# This floor only exists to trip on a real regression (a loop that
# silently degraded to a few Hz), never on runner speed. Agent tick
# throughput on the real RK3566 is an M1 measurement item.
# This is a "did the loop die" check, not a performance benchmark. The tick
# rate the agent achieves is a property of the machine, and the functional
# checks below already scale themselves to it. Only a loop that has
# essentially stopped should trip this.
MIN_AVG_HZ = 5.0
MAX_P95_GAP_MS = 400.0

CONTINUOUS_METHODS = {"robot.head", "robot.move", "robot.pose", "robot.mouth"}


def load_log(path):
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def events_of(entries):
    """Map discrete choreography calls to {(method, name): rx_wall_first}."""
    out = {}
    for e in entries:
        msg = e.get("msg", {})
        method = msg.get("method")
        params = msg.get("params", {}) or {}
        key = None
        if method == "robot.do":
            key = ("do", params.get("skill"))
        elif method == "robot.sound" and params.get("hold") is not True:
            key = ("sound", params.get("tag"))
        elif method == "robot.sound":
            key = ("sound+hold", params.get("tag"))
        if key is not None and key not in out:
            out[key] = e["rx_wall"]
    return out


def count_notifications(entries, method):
    return sum(1 for e in entries if e.get("msg", {}).get("method") == method)


def anchor_show_start(entries):
    """First rx_wall of any continuous-track notification -- this duck
    process's estimate of show_time == 0.0. Used to translate every later
    rx_wall into an approximate show_time so we can sample the actual
    choreography curve, not just count notifications.
    """
    for e in entries:
        if e.get("msg", {}).get("method") in CONTINUOUS_METHODS:
            return e["rx_wall"]
    return None


def sample_near(entries, method, target_show_time, t0, window_s=SAMPLE_WINDOW_S):
    """The params of whichever `method` notification's estimated show_time
    is closest to target_show_time, or None if nothing is within window_s.
    """
    if t0 is None:
        return None
    best_params, best_diff = None, None
    for e in entries:
        msg = e.get("msg", {})
        if msg.get("method") != method:
            continue
        show_time = e["rx_wall"] - t0
        diff = abs(show_time - target_show_time)
        if diff > window_s:
            continue
        if best_diff is None or diff < best_diff:
            best_diff, best_params = diff, msg.get("params")
    return best_params


def interpolate_at(entries, method, field, target_show_time, t0, window_s=SAMPLE_WINDOW_S):
    """Linearly interpolate `field` to exactly `target_show_time`, using the
    two samples that bracket it.

    Picking the single nearest sample (the old sample_near) makes the value
    depend on the achieved tick rate: at ~16 Hz on a loaded CI runner the
    nearest sample can be 30 ms away, and on a steep part of a curve that is
    a large value error with no defect behind it. Interpolating removes the
    tick-rate sensitivity, so the check measures the choreography rather
    than the machine.
    """
    if t0 is None:
        return None
    before = after = None  # (show_time, value)
    for e in entries:
        msg = e.get("msg", {})
        if msg.get("method") != method:
            continue
        params = msg.get("params") or {}
        if field not in params:
            continue
        st = e["rx_wall"] - t0
        if abs(st - target_show_time) > window_s:
            continue
        pt = (st, params[field])
        if st <= target_show_time and (before is None or st > before[0]):
            before = pt
        if st >= target_show_time and (after is None or st < after[0]):
            after = pt
    if before and after:
        (t_a, v_a), (t_b, v_b) = before, after
        if t_b - t_a < 1e-9:
            return v_a
        f = (target_show_time - t_a) / (t_b - t_a)
        return v_a + (v_b - v_a) * f
    if before:
        return before[1]
    if after:
        return after[1]
    return None


def peak_between(entries, method, field, t_from, t_to, t0):
    """The maximum of `field` over a show-time window.

    A peak is a peak: asking for the value at the exact instant of a maximum
    only works if a sample happens to land there, which is a coin flip at low
    tick rates. The choreography's claim is that the mouth fully opens during
    the gape, so that is what gets checked.
    """
    if t0 is None:
        return None
    best = None
    for e in entries:
        msg = e.get("msg", {})
        if msg.get("method") != method:
            continue
        params = msg.get("params") or {}
        if field not in params:
            continue
        st = e["rx_wall"] - t0
        if t_from <= st <= t_to and (best is None or params[field] > best):
            best = params[field]
    return best


def rate_stats(entries, method):
    """(avg_hz, p95_gap_ms, count) for a continuous-notification stream,
    or None if there are fewer than 2 samples to measure a rate from.
    """
    times = sorted(e["rx_wall"] for e in entries if e.get("msg", {}).get("method") == method)
    if len(times) < 2:
        return None
    span = times[-1] - times[0]
    avg_hz = (len(times) - 1) / span if span > 0 else 0.0
    gaps_ms = sorted((times[i + 1] - times[i]) * 1000.0 for i in range(len(times) - 1))
    p95_gap_ms = gaps_ms[int(len(gaps_ms) * 0.95)]
    return avg_hz, p95_gap_ms, len(times)


def tolerance_scale(entries, method="robot.head"):
    """How much looser every timing/value tolerance must be for this run.

    1.0 when the stream ran at the reference rate or better; larger in
    proportion to how much sparser it actually was. Returns 1.0 when the
    rate cannot be measured, which keeps a broken stream strict rather
    than accidentally excusing it.
    """
    rs = rate_stats(entries, method)
    if not rs or rs[0] <= 0:
        return 1.0
    achieved_period = 1.0 / rs[0]
    return max(1.0, achieved_period / REFERENCE_TICK_S)


def last_move_before_last_stop(entries):
    """The params of the most recent robot.move strictly before the last
    robot.stop, or None if there's no robot.stop or no preceding move.
    """
    stop_idx = None
    for i, e in enumerate(entries):
        if e.get("msg", {}).get("method") == "robot.stop":
            stop_idx = i
    if stop_idx is None:
        return None
    for e in reversed(entries[:stop_idx]):
        if e.get("msg", {}).get("method") == "robot.move":
            return e["msg"].get("params")
    return None


def _is_zero_move(params, tol=VX_TOL):
    if not params:
        return False
    return all(abs(params.get(k, 0.0)) <= tol for k in ("vx", "vy", "vyaw"))


def main():
    lead_log, wing_log = sys.argv[1], sys.argv[2]
    lead = load_log(lead_log)
    wing = load_log(wing_log)
    lead_ev = events_of(lead)
    wing_ev = events_of(wing)
    t0_lead = anchor_show_start(lead)
    t0_wing = anchor_show_start(wing)
    # Widen every tolerance in proportion to how sparse this run's stream
    # actually was, so the checks measure the choreography and not the
    # machine that happened to run it.
    scale_lead = tolerance_scale(lead)
    scale_wing = tolerance_scale(wing)
    head_tol_lead = HEAD_ANGLE_TOL * scale_lead
    head_tol_wing = HEAD_ANGLE_TOL * scale_wing
    pose_tol_wing = POSE_Z_TOL * scale_wing
    event_tol_lead = EVENT_TIME_TOL_S * scale_lead
    print(f"tick rate scaling: lead x{scale_lead:.2f}, wing x{scale_wing:.2f} "
          f"(1.00 = reference {REFERENCE_TICK_S * 1000:.0f} ms/tick)")
    failures = []

    def check(cond, ok_msg, fail_msg):
        print(("  ok    " if cond else "  FAIL  ") + (ok_msg if cond else fail_msg))
        if not cond:
            failures.append(fail_msg)

    print("lead (duck-01):")
    check(count_notifications(lead, "robot.head") > 100,
          f"head stream present ({count_notifications(lead, 'robot.head')} notifications)",
          "head stream missing or too sparse")
    check(count_notifications(lead, "robot.move") > 50,
          f"locomotion stream present ({count_notifications(lead, 'robot.move')} notifications)",
          "locomotion stream missing or too sparse")
    check(count_notifications(lead, "robot.mouth") > 10,
          "mouth stream present", "mouth stream missing")
    for key in [("sound", "chirp"), ("sound", "greet"), ("do", "kick_left"), ("do", "sit_toggle")]:
        check(key in lead_ev, f"event {key} fired", f"event {key} missing")
    check(count_notifications(lead, "robot.stop") >= 1,
          "robot.stop at show end", "no robot.stop at show end")

    print("lead curve values (sampled near known points on the demo show):")
    hp = interpolate_at(lead, "robot.head", "head_pitch", 1.0, t0_lead)
    check(hp is not None and abs(hp - (-0.30)) <= head_tol_lead,
          f"head_pitch@1.0s = {hp if hp is None else round(hp, 4)} (target -0.30, interpolated)",
          f"head_pitch@1.0s = {'no sample' if hp is None else round(hp, 4)} (target -0.30 +/-{head_tol_lead:.3f}, interpolated)")
    hy = interpolate_at(lead, "robot.head", "head_yaw", 6.0, t0_lead)
    check(hy is not None and abs(hy - 0.6) <= head_tol_lead,
          f"head_yaw@6.0s = {hy if hy is None else round(hy, 4)} (target 0.6, interpolated)",
          f"head_yaw@6.0s = {'no sample' if hy is None else round(hy, 4)} (target 0.6 +/-{head_tol_lead:.3f}, interpolated)")
    mo = peak_between(lead, "robot.mouth", "open", 9.0, 9.8, t0_lead)
    check(mo is not None and mo >= MOUTH_OPEN_MIN_AT_PEAK,
          f"mouth peak over 9.0-9.8s = {mo if mo is None else round(mo, 4)} (target >= {MOUTH_OPEN_MIN_AT_PEAK})",
          f"mouth peak over 9.0-9.8s = {'no sample' if mo is None else round(mo, 4)} (target >= {MOUTH_OPEN_MIN_AT_PEAK})")

    if t0_lead is not None:
        vx_bad = []
        for e in lead:
            if e.get("msg", {}).get("method") != "robot.move":
                continue
            st = e["rx_wall"] - t0_lead
            vx = e["msg"].get("params", {}).get("vx")
            if 3.6 <= st <= 5.4 and (vx is None or abs(vx - 0.1) > VX_TOL):
                vx_bad.append((round(st, 3), vx))
            elif st >= 6.1 and (vx is None or abs(vx - 0.0) > VX_TOL):
                vx_bad.append((round(st, 3), vx))
        check(not vx_bad,
              "locomotion vx matches the demo show's walk segment (0.1 in [3.6,5.4]s, 0.0 after 6.1s)",
              f"locomotion vx wrong at {len(vx_bad)} sample(s), e.g. {vx_bad[:3]}")

    rs = rate_stats(lead, "robot.head")
    check(rs is not None and rs[0] >= MIN_AVG_HZ and rs[1] <= MAX_P95_GAP_MS,
          f"head stream rate ok (avg {rs[0]:.1f} Hz, p95 gap {rs[1]:.0f} ms)" if rs else "head stream rate ok",
          f"head stream rate too slow/gappy (avg {rs[0]:.1f} Hz, p95 gap {rs[1]:.0f} ms, want >= {MIN_AVG_HZ} Hz and <= {MAX_P95_GAP_MS} ms)" if rs else "head stream has fewer than 2 samples; cannot measure rate")

    lead_last_move = last_move_before_last_stop(lead)
    check(lead.__len__() > 0 and lead[-1].get("msg", {}).get("method") == "robot.stop" and _is_zero_move(lead_last_move),
          "playback ends with a zeroed robot.move then robot.stop",
          f"end-of-show ordering wrong: last record method="
          f"{lead[-1].get('msg', {}).get('method') if lead else None!r}, last robot.move before it={lead_last_move}")

    check(count_notifications(lead, "robot.pose") == 0,
          "no robot.pose sent for lead (no pose track on this role)",
          f"unexpected robot.pose notifications for lead ({count_notifications(lead, 'robot.pose')}) -- omitted tracks must emit nothing")

    print("wing (duck-02):")
    check(count_notifications(wing, "robot.head") > 100,
          f"head stream present ({count_notifications(wing, 'robot.head')} notifications)",
          "head stream missing or too sparse")
    check(count_notifications(wing, "robot.pose") > 10,
          "pose stream present", "pose stream missing")
    for key in [("sound", "coo"), ("do", "kick_right"), ("do", "sit_toggle")]:
        check(key in wing_ev, f"event {key} fired", f"event {key} missing")

    print("wing curve values (sampled near known points on the demo show):")
    pz = interpolate_at(wing, "robot.pose", "z", 10.5, t0_wing)
    pz_active = sample_near(wing, "robot.pose", 10.5, t0_wing)
    active_ok = pz_active is not None and pz_active.get("active") is True
    check(pz is not None and abs(pz - (-0.03)) <= pose_tol_wing and active_ok,
          f"pose z@10.5s = {pz if pz is None else round(pz, 5)}, active={pz_active.get('active') if pz_active else None} "
          f"(target -0.03, True; z interpolated)",
          f"pose z@10.5s = {'no sample' if pz is None else round(pz, 5)}, active={pz_active.get('active') if pz_active else None} "
          f"(target -0.03 +/-{pose_tol_wing:.4f}, active=True)")
    pa = sample_near(wing, "robot.pose", 11.6, t0_wing)
    check(pa is not None and pa.get("active") is False,
          "pose active@~11.6s = False",
          f"pose active@~11.6s = {pa.get('active') if pa else 'no sample'} (target False)")

    rs_w = rate_stats(wing, "robot.head")
    check(rs_w is not None and rs_w[0] >= MIN_AVG_HZ and rs_w[1] <= MAX_P95_GAP_MS,
          f"head stream rate ok (avg {rs_w[0]:.1f} Hz, p95 gap {rs_w[1]:.0f} ms)" if rs_w else "head stream rate ok",
          f"head stream rate too slow/gappy (avg {rs_w[0]:.1f} Hz, p95 gap {rs_w[1]:.0f} ms, want >= {MIN_AVG_HZ} Hz and <= {MAX_P95_GAP_MS} ms)" if rs_w else "head stream has fewer than 2 samples; cannot measure rate")

    wing_last_move = last_move_before_last_stop(wing)
    check(wing.__len__() > 0 and wing[-1].get("msg", {}).get("method") == "robot.stop" and _is_zero_move(wing_last_move),
          "playback ends with a zeroed robot.move (or none) then robot.stop",
          f"end-of-show ordering wrong: last record method="
          f"{wing[-1].get('msg', {}).get('method') if wing else None!r}, last robot.move before it={wing_last_move}")

    check(count_notifications(wing, "robot.move") <= 1,
          "no locomotion stream for wing beyond the end-of-show zero move (no locomotion track on this role)",
          f"unexpected robot.move notifications for wing ({count_notifications(wing, 'robot.move')}) -- omitted tracks must emit nothing")
    check(count_notifications(wing, "robot.mouth") == 0,
          "no robot.mouth sent for wing (no mouth track on this role)",
          f"unexpected robot.mouth notifications for wing ({count_notifications(wing, 'robot.mouth')}) -- omitted tracks must emit nothing")

    print("sync:")
    key = ("do", "sit_toggle")
    if key in lead_ev and key in wing_ev:
        delta = abs(lead_ev[key] - wing_ev[key])
        check(delta <= CROSS_DUCK_TOL_S,
              f"cross-duck sit_toggle delta {delta * 1000:.1f} ms (tol {CROSS_DUCK_TOL_S * 1000:.0f} ms)",
              f"cross-duck sit_toggle delta {delta * 1000:.1f} ms exceeds tolerance")
        kick = ("do", "kick_left")
        if kick in lead_ev:
            rel = lead_ev[key] - lead_ev[kick]
            check(abs(rel - 4.0) <= RELATIVE_TOL_S,
                  f"lead kick→sit spacing {rel:.3f} s (target 4.0 s)",
                  f"lead kick→sit spacing {rel:.3f} s off target 4.0 s")

    chirp_key = ("sound", "chirp")
    coo_key = ("sound", "coo")
    if chirp_key in lead_ev and t0_lead is not None:
        chirp_show_time = lead_ev[chirp_key] - t0_lead
        check(abs(chirp_show_time - 4.0) <= event_tol_lead,
              f"lead chirp fired at show_time~{chirp_show_time:.3f}s (target 4.0s)",
              f"lead chirp fired at show_time~{chirp_show_time:.3f}s, off target 4.0s +/-{event_tol_lead:.3f}s")
    if chirp_key in lead_ev and coo_key in wing_ev:
        spacing = wing_ev[coo_key] - lead_ev[chirp_key]
        check(abs(spacing - 0.5) <= max(event_tol_lead, EVENT_TIME_TOL_S * scale_wing),
              f"cross-duck chirp→coo spacing {spacing:.3f} s (target 0.5 s)",
              f"cross-duck chirp→coo spacing {spacing:.3f} s off target 0.5 s +/-{max(event_tol_lead, EVENT_TIME_TOL_S * scale_wing):.3f}s")

    if failures:
        print(f"\ne2e FAILED: {len(failures)} check(s) failed")
        return 1
    print("\ne2e PASSED: both ducks performed in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
