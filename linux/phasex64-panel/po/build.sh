#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
# Build the compiled .mo files for local development/testing, so the panel
# can find a translation next to the script before it's packaged/installed
# (see phasex64-panel's _locale_search_dirs()). Not part of the PKGBUILD,
# which builds and installs its own copy under /usr/share/locale - this is
# only for running `phasex64-panel`/`test_panel.py` straight from the
# source tree. Output is gitignored; regenerate any time po/*.po changes.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

for po in *.po; do
	lang="${po%.po}"
	mkdir -p "../locale/$lang/LC_MESSAGES"
	msgfmt "$po" -o "../locale/$lang/LC_MESSAGES/phasex64-panel.mo"
	echo "built ../locale/$lang/LC_MESSAGES/phasex64-panel.mo"
done
