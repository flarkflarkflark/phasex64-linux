#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
#
# Undo install-dkms.sh: `dkms remove` for every kernel, then delete the
# copied source in /usr/src/snd-usb-phasex64-<version>. Only touches
# versions install-dkms.sh installed - a version that came from a
# distribution package is left alone (remove that package instead).
#
#   sudo scripts/uninstall-dkms.sh           # remove every version it installed
#   scripts/uninstall-dkms.sh --dry-run      # show what would happen
#   sudo scripts/uninstall-dkms.sh --version 0.9.0
set -euo pipefail

MODULE=snd-usb-phasex64
MARKER=.installed-by-install-dkms

DRY_RUN=0
ONLY_VERSION=""

usage() {
	cat <<EOF
Usage: $(basename "$0") [--dry-run] [--version VERSION]

Removes $MODULE from DKMS (all kernels) and deletes its source from
/usr/src - only for versions installed by install-dkms.sh.

  -n, --dry-run          print the commands that would run, without
                         changing anything (no root needed)
  -v, --version VERSION  remove only this version (default: all)
  -h, --help             show this help
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
	-n | --dry-run) DRY_RUN=1 ;;
	-v | --version)
		[ $# -ge 2 ] || { echo "error: $1 needs a version" >&2; exit 2; }
		ONLY_VERSION="$2"
		shift
		;;
	--version=*) ONLY_VERSION="${1#*=}" ;;
	-h | --help) usage; exit 0 ;;
	*) echo "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
	esac
	shift
done

run() {
	echo "+ $*"
	[ "$DRY_RUN" = 1 ] || "$@"
}

if ! command -v dkms >/dev/null; then
	echo "error: dkms not found - nothing to uninstall through DKMS" >&2
	exit 1
fi

if [ "$DRY_RUN" = 0 ] && [ "$(id -u)" -ne 0 ]; then
	echo "error: needs root - run it with sudo (or try --dry-run first)" >&2
	exit 1
fi

# Same parsing as install-dkms.sh (dkms 2.x and 3.x status formats).
registered_versions() {
	dkms status -m "$MODULE" 2>/dev/null |
		sed -nE "s#^$MODULE(/|, )([^,:]+).*#\\2#p" | sort -u
}

# Versions to look at: registered with DKMS, or only left over in /usr/src
# (e.g. after an interrupted install).
mapfile -t versions < <(
	{
		registered_versions
		for d in "/usr/src/$MODULE"-*/; do
			[ -e "$d$MARKER" ] && basename "$d" | sed "s/^$MODULE-//"
		done
	} | sort -u
)
if [ -n "$ONLY_VERSION" ]; then
	versions=("$ONLY_VERSION")
fi

found=0
for v in "${versions[@]}"; do
	[ -n "$v" ] || continue
	src="/usr/src/$MODULE-$v"
	if [ ! -e "$src/$MARKER" ]; then
		if registered_versions | grep -qx "$v"; then
			echo "Skipping $MODULE $v: not installed by install-dkms.sh" \
				"(a distribution package?). Remove that package instead." >&2
		elif [ -n "$ONLY_VERSION" ]; then
			echo "$MODULE $v is not installed." >&2
		fi
		continue
	fi
	found=1
	echo "Removing $MODULE $v"
	if registered_versions | grep -qx "$v"; then
		run dkms remove -m "$MODULE" -v "$v" --all
	fi
	run rm -rf "$src"
done

if [ "$found" = 0 ]; then
	echo "Nothing to remove."
	exit 0
fi

echo
if [ "$DRY_RUN" = 1 ]; then
	echo "Dry run: nothing was changed."
else
	echo "Done. A module that is still loaded stays loaded until you unplug"
	echo "the PHASE X64 USB and run 'sudo modprobe -r $MODULE', or reboot."
fi
