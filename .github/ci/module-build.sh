#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
#
# Build snd-usb-phasex64 against a distribution's own kernel headers, with
# W=1. Runs as root inside a distribution container (archlinux, debian,
# ubuntu, fedora); used by .github/workflows/build.yml. Installs the
# headers package, finds the kernel version it provides, builds the module
# and then dry-runs scripts/install-dkms.sh for that kernel. Never loads
# anything - a container can't.
#
#   docker run --rm -v "$PWD:/src:ro" debian:stable \
#     sh -c 'cp -a /src /w && /w/.github/ci/module-build.sh'
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=/dev/null
. /etc/os-release

echo "::group::install compiler and kernel headers ($PRETTY_NAME)"
case "$ID" in
arch)
	# The image ships without a local signing key; pacman's hooks need one.
	pacman-key --init
	pacman -Syu --noconfirm --needed base-devel linux-headers pahole dkms
	;;
debian)
	apt-get update
	DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
		build-essential linux-headers-amd64 dkms kmod
	;;
ubuntu)
	apt-get update
	DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
		build-essential linux-headers-generic dkms kmod
	;;
fedora)
	dnf -y install gcc make kernel-devel dwarves dkms kmod
	;;
*)
	echo "unsupported distribution: $ID" >&2
	exit 1
	;;
esac
echo "::endgroup::"

# The headers' kernel version: /usr/lib/modules/<ver>/build on Arch, Debian
# and Ubuntu; /usr/src/kernels/<ver> from Fedora's kernel-devel (the
# /lib/modules/<ver>/build link comes from kernel-core, not installed here).
mapfile -t kdirs < <(ls -d /usr/lib/modules/*/build /usr/src/kernels/* 2>/dev/null || true)
if [ ${#kdirs[@]} -ne 1 ]; then
	echo "expected exactly one kernel headers tree, found: ${kdirs[*]:-none}" >&2
	exit 1
fi
KDIR="$(readlink -f "${kdirs[0]}")"
case "${kdirs[0]}" in
*/build) KVER="$(basename "$(dirname "${kdirs[0]}")")" ;;
*) KVER="$(basename "${kdirs[0]}")" ;;
esac
if [ ! -e "/lib/modules/$KVER/build" ]; then
	mkdir -p "/lib/modules/$KVER"
	ln -s "$KDIR" "/lib/modules/$KVER/build"
fi
CC_VERSION="$(sed -n 's/^CONFIG_CC_VERSION_TEXT="\(.*\)"$/\1/p' "$KDIR/.config" 2>/dev/null || true)"
echo "kernel headers: $KVER ($KDIR)"
echo "kernel built with: ${CC_VERSION:-unknown}"
echo "compiler here: $(gcc --version | head -1)"

MOD="$ROOT/linux/snd-usb-phasex64"
LOG="$(mktemp)"
status=0
make -C "$KDIR" M="$MOD" W=1 modules 2>&1 | tee "$LOG" || status=$?
# Compiler diagnostics only (file:line:col: ...), not kbuild's own notes
# such as "the compiler differs from the one used to build the kernel".
DIAG='\.[ch]:[0-9]+:[0-9]+: (warning|error):'
warnings="$(grep -cE "${DIAG/(warning|error)/warning}" "$LOG" || true)"
errors="$(grep -cE "${DIAG/(warning|error)/error}" "$LOG" || true)"

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
	{
		echo "### $PRETTY_NAME - kernel $KVER"
		echo
		echo "- kernel built with: ${CC_VERSION:-unknown}"
		echo "- build: $([ "$status" = 0 ] && echo pass || echo "**FAIL** (make exit $status)")"
		echo "- W=1 warnings: $warnings, errors: $errors"
		if [ "$warnings" != 0 ] || [ "$errors" != 0 ]; then
			echo
			echo '```'
			grep -E "$DIAG" "$LOG" | sed "s#$ROOT/##" | head -40
			echo '```'
		fi
	} >>"$GITHUB_STEP_SUMMARY"
fi

if [ "$status" != 0 ]; then
	echo "module build failed for $KVER" >&2
	exit "$status"
fi
modinfo -F vermagic "$MOD/snd-usb-phasex64.ko"
modinfo -F version "$MOD/snd-usb-phasex64.ko"

echo "::group::scripts/install-dkms.sh --dry-run --kernel $KVER"
"$ROOT/scripts/install-dkms.sh" --dry-run --kernel "$KVER"
echo "::endgroup::"
