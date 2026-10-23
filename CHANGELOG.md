# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The driver, the panel and the packages share one version number.

## [Unreleased]

## [0.9.1] - 2026-10-23

Packaging, installation and documentation release. **No driver or panel
changes** apart from the version number.

### Added

- Release artifacts on every GitHub release: the source tarball, the
  three Arch Linux packages (`snd-usb-phasex64-dkms`, `phasex64-panel`,
  `phasex64-wireplumber`, all `arch=any` - DKMS builds the module on your
  machine; no prebuilt kernel module is ever shipped) and `SHA256SUMS`.
  Built by `.github/workflows/release.yml`.
- `scripts/install-dkms.sh` and `scripts/uninstall-dkms.sh`: install the
  driver with DKMS on any distribution, with a `--dry-run` mode,
  prerequisite checks and package hints for apt, dnf, zypper and pacman.
- CI on every push and pull request: the module is built with `W=1`
  against the kernel headers of Ubuntu 22.04 (5.15), Ubuntu 24.04 (6.8),
  Debian 13 (6.12), Arch Linux (7.2) and Fedora (7.2); plus the panel
  tests under Xvfb, translations, the C unit test, the RIAA models and
  shellcheck.
- AUR packaging prepared (`aur/PKGBUILD`, one package base `phasex64`);
  not published on the AUR yet.
- Documentation: installation for other distributions, control panel
  dependencies per distribution, Secure Boot (MOK enrolment), the front
  ground terminal for turntables, the "Run" LED, a second tested
  machine.
- Photos of the hardware (front, rear, side switches) in the README,
  CC BY 4.0.
- This changelog.

## [0.9.0] - 2026-10-21

First public release (beta).

### Added

- `snd-usb-phasex64`, an ALSA kernel driver built with DKMS:
  - playback, 4 channels (Out 1/2 and Out 3/4), 24 bit;
  - capture, 6 channels: In 1/2, In 3/4, and 5/6 as a bit-exact digital
    copy of what is sent to Out 1/2;
  - phono inputs with RIAA equalisation in the driver;
  - codec and routing changes applied live while a stream runs;
  - MIDI in and out (5-pin DIN; the device only passes MIDI while an
    audio stream runs);
  - 44.1, 48, 88.2 and 96 kHz; internal clock or locked to S/PDIF in;
  - every control of the Windows panel as an ALSA control.
- `phasex64-panel`, a control panel (Python/Tk) for routing, capture
  sources, phono RIAA, meters, MIDI activity and PipeWire's buffer size;
  English and Dutch.
- `phasex64-wireplumber`, an optional WirePlumber profile that keeps the
  card on PipeWire's "Pro Audio" profile for JACK applications.
- Works with PipeWire and JACK applications via `pipewire-jack`;
  bit-exact through PipeWire at 100 % volume; buffer sizes down to 64
  samples.
- Arch Linux packaging (`linux/PKGBUILD`).

[Unreleased]: https://github.com/flarkflarkflark/phasex64-linux/compare/v0.9.1...HEAD
[0.9.1]: https://github.com/flarkflarkflark/phasex64-linux/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/flarkflarkflark/phasex64-linux/releases/tag/v0.9.0
