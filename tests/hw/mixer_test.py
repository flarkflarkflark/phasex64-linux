# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Test 5: mixer. For each writable boolean switch: read, toggle, read
back, restore. Clock Source is deliberately excluded (ask first);
Digital Input Lock is read-only so there's nothing to toggle."""
import subprocess
import re
import json

CARD = "PhaseX64"
EXCLUDE_NUMIDS = {18}  # Clock Source - ask before touching

SWITCHES = [
    (1, "In 1/2 Mic-HiZ-Line Capture Switch"),
    (2, "In 1/2 Aux Capture Switch"),
    (3, "In 1/2 Phono Capture Switch"),
    (4, "In 1/2 Loopback Out 3/4 Capture Switch"),
    (5, "In 1/2 Sound Module Capture Switch"),
    (6, "Direct 1/2 Playback Switch"),
    (7, "Direct 1/2 Mono Playback Switch"),
    (8, "PCM 1/2 Playback Switch"),
    (9, "PCM 1/2 Mono Playback Switch"),
    (10, "In 3/4 Aux Capture Switch"),
    (11, "In 3/4 Phono Capture Switch"),
    (12, "In 3/4 Loopback Out 1/2 Capture Switch"),
    (13, "In 3/4 Sound Module Capture Switch"),
    (14, "Direct 3/4 Playback Switch"),
    (15, "Sound Module 3/4 Playback Switch"),
    (16, "PCM 3/4 Playback Switch"),
    (17, "PCM 3/4 Mono Playback Switch"),
]


def cget(numid):
    out = subprocess.run(["amixer", "-c", CARD, "cget", f"numid={numid}"],
                          capture_output=True, text=True, check=True).stdout
    m = re.search(r"values=(\w+)", out.split("\n")[-2] if out.strip() else out)
    return m.group(1) if m else None


def cset(numid, value):
    return subprocess.run(["amixer", "-c", CARD, "cset", f"numid={numid}", value],
                           capture_output=True, text=True)


results = []
all_ok = True
for numid, name in SWITCHES:
    if numid in EXCLUDE_NUMIDS:
        continue
    before = cget(numid)
    toggled = "off" if before == "on" else "on"
    cset(numid, toggled)
    after_toggle = cget(numid)
    cset(numid, before)
    after_restore = cget(numid)

    ok = (after_toggle == toggled) and (after_restore == before)
    all_ok &= ok
    results.append({
        "numid": numid,
        "name": name,
        "before": before,
        "toggled_to": toggled,
        "read_after_toggle": after_toggle,
        "read_after_restore": after_restore,
        "ok": ok,
    })
    status = "OK" if ok else "MISMATCH"
    print(f"numid={numid:2d} {name:45s} {before:>3s} -> {toggled:>3s} "
          f"(read {after_toggle:>3s}) -> restored {after_restore:>3s}  [{status}]")

print()
print("Clock Source (numid=18): excluded, not touched - ask before testing")
print("Digital Input Lock (numid=19): read-only, nothing to toggle")
print()
print("all switches OK:" , all_ok)

with open("data/mixer_test_results.json", "w") as f:
    json.dump({"switches": results, "all_ok": all_ok}, f, indent=2)
print("wrote data/mixer_test_results.json")
