# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Offline unit test for midi_stress.py's `amidi -l` detection logic.

No hardware, no subprocess calls to amidi/arecord - just feeds literal
`amidi -l` output strings (including the exact output that broke the
original substring check: the port name is "PHASE X64 USB MIDI", not
the ALSA card id "PhaseX64" that -p/hw:PhaseX64,... uses) through
amidi_l_mentions_phasex64() and checks the result.

Run with:
    python3 tests/unit/test_midi_stress_parse.py
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_MIDI_STRESS_PATH = os.path.join(_HERE, "..", "hw", "midi_stress.py")

_spec = importlib.util.spec_from_file_location("midi_stress", _MIDI_STRESS_PATH)
midi_stress = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(midi_stress)

failures = 0


def check(name, output, expect):
    got = midi_stress.amidi_l_mentions_phasex64(output)
    if got != expect:
        global failures
        failures += 1
        print(f"FAIL {name}: got {got}, expected {expect} for output={output!r}")
    else:
        print(f"ok   {name}")


# The exact output reported against a live system: `amidi -l` names the
# MIDI port ("PHASE X64 USB MIDI"), never the ALSA card id ("PhaseX64")
# - this is the case that broke the original `"PhaseX64" not in out` check.
check("real amidi -l output (port name, not card id)",
      "Dir Device    Name\nIO  hw:0,0    PHASE X64 USB MIDI\n",
      True)

check("bare line, no header", "IO  hw:0,0    PHASE X64 USB MIDI", True)

check("different case", "io  hw:2,0    phase x64 usb midi", True)

check("other device present, not this one",
      "Dir Device    Name\nIO  hw:1,0    Some Other MIDI Device\n",
      False)

check("empty output (no devices / amidi not found)", "", False)

check("multiple devices, phasex64 among them",
      "Dir Device    Name\n"
      "IO  hw:0,0    Some Other MIDI Device\n"
      "IO  hw:1,0    PHASE X64 USB MIDI\n",
      True)

if failures:
    print(f"\n{failures} test(s) FAILED")
    sys.exit(1)
print("\nall tests passed")
