#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
# Rasterize icon.svg for local development/testing, so the panel has a PNG
# to load via root.iconphoto() when run straight from the source tree (see
# phasex64-panel's _find_icon_png()). Not part of the PKGBUILD, which
# rasterizes and installs its own copy into the hicolor icon theme - this
# is dev-only, same role as po/build.sh for translations. Output is
# gitignored; regenerate any time icon.svg changes. Requires rsvg-convert
# (librsvg).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

mkdir -p icons
for size in 16 24 32 48 64 128 256; do
	rsvg-convert -w "$size" -h "$size" icon.svg -o "icons/phasex64-panel-${size}.png"
	echo "built icons/phasex64-panel-${size}.png"
done
