#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
#
# Install the snd-usb-phasex64 driver with DKMS on any distribution:
# copies the module source to /usr/src/snd-usb-phasex64-<version>, then
# runs `dkms add`, `dkms build` and `dkms install`. DKMS rebuilds the
# module after every kernel update. Undo with uninstall-dkms.sh.
#
# On Arch Linux, prefer the package (linux/PKGBUILD or the AUR), which
# pacman can track and remove.
#
#   sudo scripts/install-dkms.sh             # install for the running kernel
#   scripts/install-dkms.sh --dry-run        # show what would happen
#   sudo scripts/install-dkms.sh --kernel 6.12.0-1-amd64
set -euo pipefail

MODULE=snd-usb-phasex64
# Written into the copied source directory so uninstall-dkms.sh only ever
# removes a /usr/src tree this script created, never one a package owns.
MARKER=.installed-by-install-dkms
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../linux/$MODULE" && pwd)"
FILES=(phasex64.c midi_in_filter.h Makefile dkms.conf)

DRY_RUN=0
KVER="$(uname -r)"

usage() {
	cat <<EOF
Usage: $(basename "$0") [--dry-run] [--kernel VERSION]

Installs $MODULE with DKMS for one kernel (default: the running kernel,
$KVER). DKMS then also builds it for kernels installed later.

  -n, --dry-run         check prerequisites and print the commands that
                        would run, without changing anything (no root needed)
  -k, --kernel VERSION  build and install for this kernel version
  -h, --help            show this help
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
	-n | --dry-run) DRY_RUN=1 ;;
	-k | --kernel)
		[ $# -ge 2 ] || { echo "error: $1 needs a kernel version" >&2; exit 2; }
		KVER="$2"
		shift
		;;
	--kernel=*) KVER="${1#*=}" ;;
	-h | --help) usage; exit 0 ;;
	*) echo "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
	esac
	shift
done

run() {
	echo "+ $*"
	[ "$DRY_RUN" = 1 ] || "$@"
}

VERSION="$(sed -n 's/^PACKAGE_VERSION="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$SRC_DIR/dkms.conf")"
[ -n "$VERSION" ] || { echo "error: no PACKAGE_VERSION in $SRC_DIR/dkms.conf" >&2; exit 1; }
DEST="/usr/src/$MODULE-$VERSION"
KDIR="/lib/modules/$KVER/build"

# Package manager family from /etc/os-release, for install hints only.
distro_family() {
	local ids=""
	if [ -r /etc/os-release ]; then
		# shellcheck source=/dev/null
		ids="$(. /etc/os-release && echo "${ID:-} ${ID_LIKE:-}")"
	fi
	for id in $ids; do
		case "$id" in
		debian | ubuntu) echo apt; return ;;
		fedora | rhel | centos) echo dnf; return ;;
		suse | opensuse*) echo zypper; return ;;
		arch) echo pacman; return ;;
		esac
	done
	echo unknown
}

# Distro package that provides the headers for $KVER.
headers_package() {
	case "$1" in
	apt) echo "linux-headers-$KVER" ;;
	dnf) echo "kernel-devel-$KVER" ;;
	zypper) echo "kernel-${KVER##*-}-devel" ;;
	pacman)
		case "$KVER" in
		*-lts) echo linux-lts-headers ;;
		*-zen*) echo linux-zen-headers ;;
		*-hardened*) echo linux-hardened-headers ;;
		*) echo linux-headers ;;
		esac
		;;
	esac
}

print_hints() {
	local family="$1"
	shift
	local -a pkgs=()
	local need pkg
	for need in "$@"; do
		case "$family:$need" in
		*:headers) pkg="$(headers_package "$family")" ;;
		apt:gcc | apt:make) pkg=build-essential ;;
		pacman:gcc | pacman:make) pkg=base-devel ;;
		*) pkg="$need" ;;
		esac
		# build-essential/base-devel cover both gcc and make: list them once.
		[[ " ${pkgs[*]} " == *" $pkg "* ]] || pkgs+=("$pkg")
	done
	case "$family" in
	apt) echo "  sudo apt install ${pkgs[*]}" ;;
	dnf)
		echo "  sudo dnf install ${pkgs[*]}"
		echo "  (RHEL, AlmaLinux, Rocky: dkms comes from EPEL)"
		;;
	zypper) echo "  sudo zypper install ${pkgs[*]}" ;;
	pacman)
		echo "  sudo pacman -S --needed ${pkgs[*]}"
		if [[ " $* " == *" headers "* ]]; then
			echo "  (use the headers package of your kernel: linux-headers,"
			echo "  linux-lts-headers, linux-zen-headers, linuxXYZ-headers on Manjaro, ...)"
		fi
		echo "  (or install the package instead: linux/PKGBUILD, see linux/README.md)"
		;;
	*) echo "  install: ${pkgs[*]} (kernel headers for $KVER)" ;;
	esac
	if [[ " $* " == *" headers "* ]]; then
		echo "  The headers must match the kernel exactly ($KVER). After a kernel"
		echo "  update, reboot into the new kernel first if the matching headers"
		echo "  package isn't available for the old one any more."
	fi
}

# --- prerequisites ---------------------------------------------------------

missing=()
command -v dkms >/dev/null || missing+=(dkms)
command -v make >/dev/null || missing+=(make)
if [ -f "$KDIR/Makefile" ]; then
	# A kernel built with clang (some distributions do this) needs clang to
	# build its modules too.
	if grep -qs '^CONFIG_CC_IS_CLANG=y' "$KDIR/.config" "$KDIR/include/config/auto.conf"; then
		command -v clang >/dev/null || missing+=(clang)
		command -v ld.lld >/dev/null || missing+=(lld)
	else
		command -v gcc >/dev/null || missing+=(gcc)
	fi
else
	command -v gcc >/dev/null || missing+=(gcc)
	missing+=(headers)
fi

echo "$MODULE $VERSION, kernel $KVER"
if [ ${#missing[@]} -gt 0 ]; then
	echo "Missing prerequisites: ${missing[*]}" >&2
	[[ " ${missing[*]} " == *" headers "* ]] && echo "  (no kernel headers at $KDIR)" >&2
	echo "Install them with:" >&2
	print_hints "$(distro_family)" "${missing[@]}" >&2
	exit 1
fi

if [ "$DRY_RUN" = 0 ] && [ "$(id -u)" -ne 0 ]; then
	echo "error: needs root - run it with sudo (or try --dry-run first)" >&2
	exit 1
fi

# --- install ---------------------------------------------------------------

# Versions DKMS already knows, from `dkms status` (formats: dkms 3.x
# "name/ver, kver, arch: state" / "name/ver: added"; dkms 2.x
# "name, ver, kver, arch: state").
registered_versions() {
	dkms status -m "$MODULE" 2>/dev/null |
		sed -nE "s#^$MODULE(/|, )([^,:]+).*#\\2#p" | sort -u
}

if [ -e "$DEST" ] && [ ! -e "$DEST/$MARKER" ]; then
	echo "error: $DEST exists but wasn't created by this script - it's probably" >&2
	echo "owned by a package (e.g. snd-usb-phasex64-dkms). Remove that package" >&2
	echo "first, or keep using it instead of this script." >&2
	exit 1
fi

for old in $(registered_versions); do
	[ "$old" = "$VERSION" ] && continue
	if [ ! -e "/usr/src/$MODULE-$old/$MARKER" ]; then
		echo "error: $MODULE $old is registered with DKMS by something other than" >&2
		echo "this script (a distribution package?). Remove that first." >&2
		exit 1
	fi
	echo "Replacing $MODULE $old"
	run dkms remove -m "$MODULE" -v "$old" --all
	run rm -rf "/usr/src/$MODULE-$old"
done

if registered_versions | grep -qx "$VERSION"; then
	# Re-running the script: start from a clean registration so the copied
	# source below is what gets built.
	run dkms remove -m "$MODULE" -v "$VERSION" --all
fi

run rm -rf "$DEST"
run install -d "$DEST"
for f in "${FILES[@]}"; do
	run install -m644 "$SRC_DIR/$f" "$DEST/$f"
done
run touch "$DEST/$MARKER"

run dkms add -m "$MODULE" -v "$VERSION"
run dkms build -m "$MODULE" -v "$VERSION" -k "$KVER"
run dkms install -m "$MODULE" -v "$VERSION" -k "$KVER"

echo
if [ "$DRY_RUN" = 1 ]; then
	echo "Dry run: nothing was changed."
else
	echo "Installed $MODULE $VERSION for $KVER."
	echo "Unplug and replug the PHASE X64 USB, or load the module with:"
	echo "  sudo modprobe $MODULE"
fi
if command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -q 'SecureBoot enabled'; then
	echo
	echo "Secure Boot is enabled: the module must be signed with a key the"
	echo "firmware trusts, or it won't load (\"Key was rejected by service\")."
	echo "See \"Secure Boot\" in linux/README.md."
fi
