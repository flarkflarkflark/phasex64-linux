#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
#
# Build the release assets for the current commit into OUTDIR:
#
#   phasex64-linux-<ver>.tar.gz          git archive of the commit
#   snd-usb-phasex64-dkms-<ver>-<rel>-any.pkg.tar.zst
#   phasex64-panel-<ver>-<rel>-any.pkg.tar.zst
#   phasex64-wireplumber-<ver>-<rel>-any.pkg.tar.zst
#   SHA256SUMS
#
# The Arch packages are built with makepkg from linux/PKGBUILD inside the
# extracted tarball, so they contain exactly what the tarball contains. All
# three are arch=any: the driver package ships the module *source* and DKMS
# builds it on the user's machine. A prebuilt kernel module in any asset
# fails the build.
#
# Used by .github/workflows/release.yml in an archlinux container; locally:
#
#   docker run --rm -v "$PWD:/src:ro" archlinux:latest sh -c \
#     'pacman -Syu --noconfirm --needed git base-devel gettext librsvg &&
#      git clone -q /src /w && /w/.github/ci/release-assets.sh /w/dist'
#
# Needs git, makepkg, bsdtar, msgfmt and rsvg-convert. As root it builds
# through a throwaway "builder" user (makepkg refuses to run as root).
set -euo pipefail

OUT="${1:?usage: release-assets.sh OUTDIR [TAG]}"
TAG="${2:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

fail() { echo "error: $*" >&2; exit 1; }

# --- one version everywhere ------------------------------------------------

v_module="$(sed -n 's/^MODULE_VERSION("\(.*\)");$/\1/p' linux/snd-usb-phasex64/phasex64.c)"
v_dkms="$(sed -n 's/^PACKAGE_VERSION="\(.*\)"$/\1/p' linux/snd-usb-phasex64/dkms.conf)"
v_pkgbuild="$(sed -n 's/^pkgver=//p' linux/PKGBUILD)"
v_panel="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' linux/phasex64-panel/phasex64-panel)"
pkgrel="$(sed -n 's/^pkgrel=//p' linux/PKGBUILD)"
VERSION="$v_module"

printf '%-28s %s\n' "MODULE_VERSION (phasex64.c)" "${v_module:-<missing>}" \
	"PACKAGE_VERSION (dkms.conf)" "${v_dkms:-<missing>}" \
	"pkgver (linux/PKGBUILD)" "${v_pkgbuild:-<missing>}" \
	"VERSION (phasex64-panel)" "${v_panel:-<missing>}"
[ -n "$TAG" ] && printf '%-28s %s\n' "tag" "$TAG"

[ -n "$VERSION" ] || fail "no MODULE_VERSION found"
for v in "$v_dkms" "$v_pkgbuild" "$v_panel"; do
	[ "$v" = "$VERSION" ] || fail "version mismatch (see above)"
done
if [ -n "$TAG" ]; then
	[ "${TAG#v}" = "$VERSION" ] || fail "tag $TAG doesn't match version $VERSION"
	[ "$(git rev-parse HEAD)" = "$(git rev-parse "$TAG^{commit}")" ] ||
		fail "HEAD is not the commit tagged $TAG"
fi
grep -q "^## \[$VERSION\]" CHANGELOG.md || fail "CHANGELOG.md has no section for $VERSION"
[ -z "$(git status --porcelain --untracked-files=no)" ] ||
	echo "warning: uncommitted changes are not part of the assets (git archive uses HEAD)" >&2
echo "version $VERSION, pkgrel $pkgrel: ok"

# --- source tarball ----------------------------------------------------------

NAME="phasex64-linux-$VERSION"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
TARBALL="$OUT/$NAME.tar.gz"
git archive --format=tar.gz --prefix="$NAME/" -o "$TARBALL" HEAD

# Same file list as the commit (files only; tar also lists directories).
diff <(git ls-tree -r --name-only HEAD | sed "s#^#$NAME/#" | sort) \
	<(bsdtar -tf "$TARBALL" | grep -v '/$' | sort) ||
	fail "tarball file list differs from the commit"
echo "tarball: $(basename "$TARBALL"), $(bsdtar -tf "$TARBALL" | grep -vc '/$') files"

# --- Arch packages, built from the extracted tarball -------------------------

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
bsdtar -xf "$TARBALL" -C "$BUILD"
mkdir "$BUILD/pkgs"
if [ "$(id -u)" = 0 ]; then
	id builder >/dev/null 2>&1 || useradd --create-home builder
	chown -R builder: "$BUILD"
	(cd "$BUILD/$NAME/linux" && runuser -u builder -- env PKGDEST="$BUILD/pkgs" makepkg --noconfirm)
else
	(cd "$BUILD/$NAME/linux" && PKGDEST="$BUILD/pkgs" makepkg --noconfirm)
fi

PACKAGES=(snd-usb-phasex64-dkms phasex64-panel phasex64-wireplumber)
[ "$(find "$BUILD/pkgs" -name '*.pkg.tar.zst' | wc -l)" = "${#PACKAGES[@]}" ] ||
	fail "expected exactly ${#PACKAGES[@]} packages: $(ls "$BUILD/pkgs")"

# Files each package must install.
expected_files() {
	case "$1" in
	snd-usb-phasex64-dkms)
		printf 'usr/src/snd-usb-phasex64-%s/%s\n' "$VERSION" phasex64.c "$VERSION" midi_in_filter.h \
			"$VERSION" Makefile "$VERSION" dkms.conf
		;;
	phasex64-panel)
		printf '%s\n' usr/bin/phasex64-panel usr/share/applications/phasex64-panel.desktop \
			usr/share/locale/nl/LC_MESSAGES/phasex64-panel.mo \
			usr/share/icons/hicolor/scalable/apps/phasex64-panel.svg \
			usr/share/icons/hicolor/48x48/apps/phasex64-panel.png
		;;
	phasex64-wireplumber)
		echo usr/share/wireplumber/wireplumber.conf.d/51-phasex64-pro-audio.conf
		;;
	esac
}

for p in "${PACKAGES[@]}"; do
	file="$BUILD/pkgs/$p-$VERSION-$pkgrel-any.pkg.tar.zst"
	[ -f "$file" ] || fail "missing package $(basename "$file")"
	info="$(bsdtar -xOf "$file" .PKGINFO)"
	grep -qx 'arch = any' <<<"$info" || fail "$p: arch is not 'any'"
	grep -qx "pkgver = $VERSION-$pkgrel" <<<"$info" || fail "$p: wrong pkgver"
	listing="$(bsdtar -tf "$file")"
	while read -r f; do
		grep -qx "$f" <<<"$listing" || fail "$p: doesn't install $f"
	done < <(expected_files "$p")
	cp "$file" "$OUT/"
	echo "package: $(basename "$file") (arch any, $(grep -vc -e '/$' -e '^\.' <<<"$listing") files)"
done
dkms_conf="$(bsdtar -xOf "$OUT/snd-usb-phasex64-dkms-$VERSION-$pkgrel-any.pkg.tar.zst" \
	"usr/src/snd-usb-phasex64-$VERSION/dkms.conf")"
grep -qx "PACKAGE_VERSION=\"$VERSION\"" <<<"$dkms_conf" || fail "packaged dkms.conf has the wrong version"

# --- never a prebuilt kernel module -------------------------------------------

for f in "$OUT"/*.tar.gz "$OUT"/*.pkg.tar.zst; do
	if bsdtar -tf "$f" | grep -E '\.ko(\.(gz|xz|zst))?$'; then
		fail "$(basename "$f") contains a kernel module binary"
	fi
done
echo "no prebuilt kernel module in any asset: ok"

# --- checksums -----------------------------------------------------------------

(cd "$OUT" && sha256sum -- *.tar.gz *.pkg.tar.zst >SHA256SUMS && sha256sum -c --quiet SHA256SUMS)
echo
cat "$OUT/SHA256SUMS"
ls -l "$OUT"
