#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""v0.1.12 phase 3a hardware test helpers (analysis/v0.1.12-plan.md section 11).

Not run by the assistant - the user runs this against real hardware, after
loading the 0.1.12-pre1 build themselves (see the companion checklist,
v0112-live-apply-checklist.md, for the full guided session). This script
only reads dmesg/sysfs and does small, local computations on that output -
it never touches the device, loads/unloads the module, or runs amixer/
arecord/aplay itself, so it needs no special authorization to exist, only
to be *run*, which is the user's call.

Subcommands:
  preflight         Capture/rate test rules check (CLAUDE.md): Digital
                     Input Lock must be off and the live rate must be
                     48000 before every test in this session.
  gaps [--since T]  Parse dmesg for matched "staged gen=N" / "committed
                     gen=N" dev_dbg trace pairs (needs dyndbg enabled for
                     phasex64.c - see the checklist) and print the
                     stage-to-commit gap per toggle.
  coalesce [--since T]
                     Count distinct "committed gen=" lines since T - for
                     the 10-fast-clicks coalescing scenario, expect at
                     most 2.
  scan [--since T]   Scan dmesg for hang/oops/BUG patterns since T - for
                     every scenario, expect none.
  fault N            Set live_fail_step=N (0-5) via the module's sysfs
                     parameter. N=0 disables fault injection.
"""
import argparse
import re
import subprocess
import sys

FAULT_STEP_PARAM = "/sys/module/snd_usb_phasex64/parameters/live_fail_step"
LOCK_CONTROL = "Digital Input Lock"
RATE_CONTROL = "Digital Input Rate"
EXPECTED_RATE = 48000

HANG_PATTERNS = (
    "BUG:", "Oops", "RIP:", "Call Trace", "WARNING: CPU:",
    "INFO: task", "blocked for more than", "watchdog: BUG",
    "general protection fault", "kernel NULL pointer dereference",
)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def journal_since(since):
    cmd = ["journalctl", "-k", "--no-pager"]
    if since:
        cmd += ["--since", since]
    return run(cmd).stdout.splitlines()


def cmd_preflight(_args):
    lock = run(["amixer", "-c", "PhaseX64", "cget", "name=" + LOCK_CONTROL]).stdout
    rate = run(["amixer", "-c", "PhaseX64", "cget", "name=" + RATE_CONTROL]).stdout
    lock_off = ": values=off" in lock
    rate_ok = f": values={EXPECTED_RATE}" in rate
    print(f"Digital Input Lock: {'off (OK)' if lock_off else 'NOT off - FIX BEFORE TESTING'}")
    print(f"Digital Input Rate: {'48000 (OK)' if rate_ok else 'NOT 48000 - FIX BEFORE TESTING'}")
    if not (lock_off and rate_ok):
        print("\nRefusing to say PASS - do not proceed with a capture/rate-"
              "sensitive step until both read OK. No S/PDIF loopback cable "
              "for this session either (see CLAUDE.md capture/rate rules).")
        return 1
    print("\nPreflight OK.")
    return 0


def parse_gen_events(lines):
    staged, committed = {}, {}
    for line in lines:
        if "live apply: staged gen=" in line:
            m = re.search(r"staged gen=(\d+)", line)
            if m:
                staged[int(m.group(1))] = line
        elif "live apply: committed gen=" in line:
            m = re.search(r"committed gen=(\d+)", line)
            if m:
                committed[int(m.group(1))] = line
    return staged, committed


def journal_timestamp_seconds(line):
    # journalctl -o short (default) prints "Mon DD HH:MM:SS host ...";
    # ask journalctl for the same lines with -o short-precise instead for
    # sub-second precision when computing gaps - this parses that format.
    m = re.match(r"^\S+ +\d+ (\d{2}):(\d{2}):(\d{2})\.(\d+)", line)
    if not m:
        return None
    h, mi, s, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + float("0." + frac)


def cmd_gaps(args):
    cmd = ["journalctl", "-k", "--no-pager", "-o", "short-precise"]
    if args.since:
        cmd += ["--since", args.since]
    lines = run(cmd).stdout.splitlines()
    staged, committed = parse_gen_events(lines)
    if not staged and not committed:
        print("No 'live apply: staged/committed' lines found. Enable dyndbg "
              "for this file first (see the checklist) and make at least "
              "one live control change.")
        return 1
    gens = sorted(set(staged) | set(committed))
    any_bad = False
    for gen in gens:
        s_line = staged.get(gen)
        c_line = committed.get(gen)
        if not s_line:
            print(f"gen={gen}: committed with no staged line seen (outside --since window?)")
            continue
        if not c_line:
            print(f"gen={gen}: staged but never committed (coalesced into a later gen, "
                  f"a fault, or device gone - not necessarily wrong, check the scenario)")
            continue
        t0, t1 = journal_timestamp_seconds(s_line), journal_timestamp_seconds(c_line)
        if t0 is None or t1 is None:
            print(f"gen={gen}: could not parse timestamps, inspect manually:\n  {s_line}\n  {c_line}")
            continue
        gap_ms = (t1 - t0) * 1000
        flag = "" if gap_ms <= 500 else "  *** OVER 0.5s ***"
        if gap_ms > 500:
            any_bad = True
        print(f"gen={gen}: gap={gap_ms:.0f} ms{flag}")
    return 1 if any_bad else 0


def cmd_coalesce(args):
    lines = journal_since(args.since)
    commits = [l for l in lines if "live apply: committed gen=" in l]
    print(f"{len(commits)} commit(s) since {args.since or 'boot'}:")
    for l in commits:
        print(f"  {l}")
    if len(commits) > 2:
        print(f"\n*** {len(commits)} commits for a 10-click burst - coalescing"
              " did not reduce this to <=2 as expected ***")
        return 1
    print("\nOK (<=2 commits).")
    return 0


def cmd_scan(args):
    lines = journal_since(args.since)
    hits = [l for l in lines if any(p in l for p in HANG_PATTERNS)]
    if hits:
        print(f"*** {len(hits)} suspicious line(s) found: ***")
        for l in hits:
            print(f"  {l}")
        return 1
    print(f"Clean - no hang/oops/BUG patterns since {args.since or 'boot'}.")
    return 0


def cmd_fault(args):
    if not 0 <= args.step <= 5:
        print("step must be 0-5 (0 disables fault injection)")
        return 1
    try:
        with open(FAULT_STEP_PARAM, "w", encoding="ascii") as f:
            f.write(str(args.step))
    except OSError as exc:
        print(f"Could not write {FAULT_STEP_PARAM}: {exc}\n"
              "Is the 0.1.12-pre1 build actually loaded? (pre-0.1.12 "
              "modules don't have this parameter.)")
        return 1
    print(f"live_fail_step = {args.step}"
          f"{' (disabled)' if args.step == 0 else ''}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preflight")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("gaps")
    p.add_argument("--since", default=None, help='journalctl --since value, e.g. "5 minutes ago"')
    p.set_defaults(func=cmd_gaps)

    p = sub.add_parser("coalesce")
    p.add_argument("--since", default=None)
    p.set_defaults(func=cmd_coalesce)

    p = sub.add_parser("scan")
    p.add_argument("--since", default=None)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("fault")
    p.add_argument("step", type=int)
    p.set_defaults(func=cmd_fault)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
