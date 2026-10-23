#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
#
# Print the GitHub release notes for VERSION: its CHANGELOG.md section,
# then where to start with installing. Used by .github/workflows/release.yml.
#
#   .github/ci/release-notes.sh 0.9.1
set -euo pipefail

VERSION="${1:?usage: release-notes.sh VERSION}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="https://github.com/flarkflarkflark/phasex64-linux"
BASE="$REPO/releases/download/v$VERSION"
PKGREL="$(sed -n 's/^pkgrel=//p' "$ROOT/linux/PKGBUILD")"

# The section's body: everything after "## [VERSION]" up to the next "## [".
section="$(sed -n "/^## \[$VERSION\]/,/^## \[/p" "$ROOT/CHANGELOG.md" | sed '1d;$d' | sed '/./,$!d')"
if [ -z "$(tr -d '[:space:]' <<<"$section")" ]; then
	echo "error: CHANGELOG.md has no section for $VERSION" >&2
	exit 1
fi

cat <<EOF
$section

### Install

- **Arch Linux, Manjaro, EndeavourOS:** download the driver and the
  control panel package, check them, install them (pacman refuses
  unsigned packages given as a URL, so install the downloaded files):

  \`\`\`sh
  curl -L --remote-name-all $BASE/SHA256SUMS \\
    $BASE/snd-usb-phasex64-dkms-$VERSION-$PKGREL-any.pkg.tar.zst \\
    $BASE/phasex64-panel-$VERSION-$PKGREL-any.pkg.tar.zst
  sha256sum -c --ignore-missing SHA256SUMS
  sudo pacman -U snd-usb-phasex64-dkms-$VERSION-$PKGREL-any.pkg.tar.zst \\
    phasex64-panel-$VERSION-$PKGREL-any.pkg.tar.zst
  \`\`\`

  \`phasex64-wireplumber\` is optional; read "PipeWire / JACK" in the
  manual first.
- **Any other distribution:** download \`phasex64-linux-$VERSION.tar.gz\`,
  check it against \`SHA256SUMS\` and run \`scripts/install-dkms.sh\`.

Details, checksums and Secure Boot:
[Installation]($REPO#installation). DKMS builds the module on your
machine; no prebuilt kernel module is shipped.

Not affiliated with or endorsed by TerraTec, TerraSoniq, Musonik or
Ploytec; an experimental kernel module, provided without warranty
(GPL-2.0-only).

Built by loopback-coding: AI-assisted, human-directed, hardware-verified.
Every change went out, came back through real hardware, and was measured
before it counted.
EOF
