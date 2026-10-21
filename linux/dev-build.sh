#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
# Build snd-usb-phasex64 outside the repo, because kbuild's M= can't handle
# the space in this repo's path ("PHASE X64"). Never writes build output
# back into the repo.
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
