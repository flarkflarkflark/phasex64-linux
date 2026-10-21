#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
# Developer build of snd-usb-phasex64: copies the source to ~/px64 and builds
# there with W=1, failing on any compiler warning. Keeps build output out of
# the repo and works from any checkout path (kbuild's M= can't handle spaces).
# A plain `make -C linux/snd-usb-phasex64` also works from a path without
# spaces.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/snd-usb-phasex64" && pwd)"
BUILD_DIR="$HOME/px64"
KDIR="/lib/modules/$(uname -r)/build"

rsync -a --delete "$SRC_DIR/" "$BUILD_DIR/"

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

if ! make -C "$KDIR" M="$BUILD_DIR" W=1 modules 2>&1 | tee "$LOG"; then
	echo "build failed" >&2
	exit 1
fi

if grep -qiE 'warning:|error:' "$LOG"; then
	echo "build produced warnings or errors, see above" >&2
	exit 1
fi

KO="$BUILD_DIR/snd-usb-phasex64.ko"
echo "module: $KO"
echo "version: $(modinfo -F version "$KO")"
