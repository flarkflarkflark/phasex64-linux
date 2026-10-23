# AUR packaging

`PKGBUILD` and `.SRCINFO` here are what goes into the AUR repository
`phasex64`. They build from the release tarball asset
(`releases/download/v<pkgver>/phasex64-linux-<pkgver>.tar.gz`, built by
`.github/workflows/release.yml` and listed in the release's
`SHA256SUMS`), not from a checkout;
`linux/PKGBUILD` is the checkout variant. Both produce the same split
packages from the same files.

**One package base, three packages.** `pkgbase=phasex64` with
`snd-usb-phasex64-dkms`, `phasex64-panel` and `phasex64-wireplumber`,
as in `linux/PKGBUILD`, rather than three separate AUR packages:

- one version, one tarball and one checksum per release, so the driver
  and the panel can't drift apart on the AUR;
- AUR helpers handle split packages: `paru -S snd-usb-phasex64-dkms
  phasex64-panel` builds the base once and installs only those two, so
  `phasex64-wireplumber` (which changes WirePlumber's behaviour for the
  whole machine) stays opt-in;
- the driver package keeps the `-dkms` suffix the AUR uses for DKMS
  modules, and each package is still searchable by its own name.

The cost is that building one package builds all three - a few seconds
here (translations and icons).

**Release checklist:**

1. Bump `pkgver` (and reset `pkgrel=1`) in both PKGBUILDs once the tag
   is pushed; packaging-only changes bump `pkgrel` in both.
2. Update `sha256sums` here once the release is published: `makepkg -g`
   (downloads the tarball asset); it must equal the tarball's line in the
   release's `SHA256SUMS`.
3. Build with `makepkg -s` in a clean directory, check with
   `namcap PKGBUILD *.pkg.tar.zst`.
4. `makepkg --printsrcinfo > .SRCINFO`.
5. Copy `PKGBUILD` and `.SRCINFO` into the AUR git repository
   (`ssh://aur@aur.archlinux.org/phasex64.git`), commit, push.

Expected namcap output: `Split PKGBUILD needs additional makedepends
['wireplumber']` (namcap wants runtime dependencies of split packages
installed at build time; not needed here), `Referenced library 'python3'`
(the `env python3` shebang, provided by `python`), and "Dependency
included, but may not be needed" for `tk`, `alsa-utils`,
`gtk-update-icon-cache`, `dkms` and `wireplumber`, which namcap can't see
being used by a script or a config file.
