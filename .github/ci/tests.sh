#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
#
# Every test that runs without a PHASE X64 USB: Python syntax, the panel
# (GUI tests under Xvfb), translations, the C MIDI-filter unit test, the
# MIDI stress parser, the RIAA models and shellcheck. Used by
# .github/workflows/build.yml; runs the same way locally from any checkout:
#
#   .github/ci/tests.sh
#
# Needs python3 with tkinter, numpy and scipy, xvfb-run, gcc, xgettext and
# the shellcheck tool. Never talks to ALSA: the panel tests replace the
# mixer with FakeMixer and fail on any real amixer/alsactl call.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# Keep __pycache__ out of the checkout.
export PYTHONPYCACHEPREFIX="$TMP/pycache"

step() { printf '\n=== %s\n' "$*"; }

step "py_compile"
mapfile -t pyfiles < <(git ls-files '*.py')
python3 -m py_compile linux/phasex64-panel/phasex64-panel "${pyfiles[@]}"
echo "ok (${#pyfiles[@]} files + phasex64-panel)"

step "panel tests (Xvfb)"
(cd linux/phasex64-panel && PX64_GUI_TESTS=1 xvfb-run -a python3 test_panel.py)

step "i18n"
(cd linux/phasex64-panel && python3 test_i18n.py)

step "C unit test: MIDI-in filter"
gcc -Wall -Wextra -Werror -std=c99 -o "$TMP/test_midi_in_filter" tests/unit/test_midi_in_filter.c
"$TMP/test_midi_in_filter"

step "MIDI stress parser"
python3 tests/unit/test_midi_stress_parse.py

step "RIAA models"
python3 tests/hw/riaa/test_response.py
python3 tests/hw/riaa/test_kernel_model.py

step "shellcheck"
# tests/hw/*.sh are hardware test scripts (need the device); not covered here.
shellcheck scripts/*.sh linux/dev-build.sh linux/phasex64-panel/build-icons.sh \
	linux/phasex64-panel/po/build.sh .github/ci/*.sh
echo ok

step "all tests passed"
