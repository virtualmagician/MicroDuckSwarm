#!/usr/bin/env python3
"""Verify the e2e demo: parse two mock-duck intent logs and assert that both
ducks received their choreography with tight cross-duck timing.

Usage: verify_e2e.py <lead-log.jsonl> <wing-log.jsonl>
Exit 0 when every check passes; prints a report either way.
"""
import json
import sys

# Tolerances are for localhost mock runs; real-hardware budgets live in docs.
CROSS_DUCK_TOL_S = 0.10
RELATIVE_TOL_S = 0.15


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


def main():
    lead_log, wing_log = sys.argv[1], sys.argv[2]
    lead = load_log(lead_log)
    wing = load_log(wing_log)
    lead_ev = events_of(lead)
    wing_ev = events_of(wing)
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

    print("wing (duck-02):")
    check(count_notifications(wing, "robot.head") > 100,
          f"head stream present ({count_notifications(wing, 'robot.head')} notifications)",
          "head stream missing or too sparse")
    check(count_notifications(wing, "robot.pose") > 10,
          "pose stream present", "pose stream missing")
    for key in [("sound", "coo"), ("do", "kick_right"), ("do", "sit_toggle")]:
        check(key in wing_ev, f"event {key} fired", f"event {key} missing")

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

    if failures:
        print(f"\ne2e FAILED: {len(failures)} check(s) failed")
        return 1
    print("\ne2e PASSED: both ducks performed in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
