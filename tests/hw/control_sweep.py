# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""v0.1.4 mixer correctness sweep.

For every writable control: read it, set every possible value in turn,
read *all* controls back after each write, and flag any control other
than the one being tested (or a pre-declared, known-legitimate dependent)
that changed. Restores the full baseline after each control, and again
at the very end for safety.

This is exactly the kind of check that caught the v0.1.3 hardware
session's mixer restore gap (testing one switch at a time missed that
Phono's exclusivity had silently cleared a different switch) - this
script diffs the *entire* control set after every single write instead
of just the one control being touched.

Do not run this inside a non-hardware session: it writes real USB
control transfers to the device. Intended to be run by hand during a
supervised hardware session, same rules as tests/hw/mixer_test.py.
"""
import subprocess
import re
import sys
import json

CARD = "PhaseX64"

# Controls that may legitimately also change as a side effect of writing
# the key control - the mono-direct interlock (px_switch_put() /
# px_adc_source_put() in phasex64.c). Anything else that changes is
# flagged as unexpected. Not symmetric: e.g. turning the mono switchON
# can revert itself (that's just "the control we wrote changed", not a
# collateral effect on another control, so it isn't listed here).
EXPECTED_DEPENDENTS = {
    "In 1/2 Capture Source": {"Direct 1/2 Mono Playback Switch"},
    "Direct 1/2 Playback Switch": {"Direct 1/2 Mono Playback Switch"},
}


def run(*args):
    return subprocess.run(["amixer", "-c", CARD, *args],
                          capture_output=True, text=True, check=True).stdout


def parse_contents(text):
    """-> {name: {"numid": int, "type": str, "access": str, "value": str,
    "items": [str, ...] or None}}"""
    controls = {}
    numid = name = type_ = access = items = None
    for line in text.splitlines():
        m = re.match(r"numid=(\d+),iface=\S+,name='([^']*)'", line)
        if m:
            numid, name = int(m.group(1)), m.group(2)
            items = []
            continue
        m = re.search(r"type=(\w+),access=([^,]+)", line)
        if m:
            type_, access = m.group(1), m.group(2)
            continue
        m = re.match(r"\s*; Item #(\d+) '(.*)'", line)
        if m:
            items.append(m.group(2))
            continue
        m = re.match(r"\s*: values=(.*)", line)
        if m and name is not None:
            controls[name] = {
                "numid": numid, "type": type_, "access": access,
                "value": m.group(1).strip(),
                "items": items if items else None,
            }
            numid = name = type_ = access = items = None
    return controls


def possible_values(info):
    if info["type"] == "BOOLEAN":
        return ["off", "on"]
    if info["type"] == "ENUMERATED":
        return [str(i) for i in range(len(info["items"]))]
    return []


def set_control(name, value):
    run("-q", "cset", "name=" + name, value)


def restore(baseline):
    for name, info in baseline.items():
        if "w" in info["access"]:
            set_control(name, info["value"])


def diff(before, after, skip):
    changed = []
    for name, info in after.items():
        if name == skip:
            continue
        if before.get(name, {}).get("value") != info["value"]:
            changed.append(name)
    return changed


def main():
    baseline = parse_contents(run("contents"))
    print(f"baseline: {len(baseline)} controls")

    results = []
    unexpected_total = 0

    for name, info in baseline.items():
        if "w" not in info["access"]:
            continue
        values = possible_values(info)
        if not values:
            print(f"skip {name}: unknown type {info['type']}")
            continue

        expected = EXPECTED_DEPENDENTS.get(name, set())
        control_result = {"control": name, "original": info["value"], "steps": []}

        for value in values:
            pre = parse_contents(run("contents"))
            set_control(name, value)
            post = parse_contents(run("contents"))

            changed = diff(pre, post, skip=None)
            self_changed = name in changed
            if name in changed:
                changed.remove(name)
            unexpected = [c for c in changed if c not in expected]

            step = {
                "set_to": value,
                "readback": post[name]["value"],
                "self_changed": self_changed,
                "expected_dependents_changed": [c for c in changed if c in expected],
                "unexpected_changed": unexpected,
            }
            control_result["steps"].append(step)
            if unexpected:
                unexpected_total += len(unexpected)
                print(f"  UNEXPECTED: setting {name}={value} also changed "
                      f"{unexpected}")

        # restore the *entire* baseline, not just this control - a
        # dependent control (e.g. the mono switch) may have drifted
        # during the sweep and needs resetting too.
        restore(baseline)
        after_restore = parse_contents(run("contents"))
        restore_ok = all(after_restore[n]["value"] == v["value"]
                         for n, v in baseline.items() if "w" in v["access"])
        control_result["restore_ok"] = restore_ok
        if not restore_ok:
            print(f"  RESTORE FAILED after testing {name} - check baseline manually")

        results.append(control_result)
        print(f"{name}: {len(values)} values tested, "
              f"restore_ok={restore_ok}")

    # final full restore and verification, belt and braces
    restore(baseline)
    final = parse_contents(run("contents"))
    final_ok = all(final[n]["value"] == v["value"]
                   for n, v in baseline.items() if "w" in v["access"])

    print()
    print(f"controls tested: {len(results)}")
    print(f"unexpected collateral changes: {unexpected_total}")
    print(f"final baseline restore ok: {final_ok}")

    with open("data/control_sweep_results.json", "w") as f:
        json.dump({
            "baseline": baseline,
            "results": results,
            "unexpected_total": unexpected_total,
            "final_restore_ok": final_ok,
        }, f, indent=2)
    print("wrote data/control_sweep_results.json")

    if not final_ok or unexpected_total:
        sys.exit(1)


if __name__ == "__main__":
    main()
