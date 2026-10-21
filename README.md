# phasex64-linux

Linux driver and control panel for the PHASE X64 USB audio interface.

![Licence: GPL-2.0-only](https://img.shields.io/badge/licence-GPL--2.0--only-blue)
![Status: public beta](https://img.shields.io/badge/status-public%20beta-orange)

Nederlandse versie: [README.nl.md](README.nl.md)

## What it is

The PHASE X64 USB has no class-compliant USB audio mode, so Linux's
standard `snd-usb-audio` driver can't use it. This project provides:

- **`snd-usb-phasex64`** - an ALSA kernel driver, built and kept up to
  date across kernel updates with DKMS.
- **`phasex64-panel`** - a control panel (Python/Tk) for routing, capture
  sources, phono RIAA, meters, MIDI activity and PipeWire's buffer size.
  It doesn't depend on any particular desktop environment.
- **`phasex64-wireplumber`** (optional) - a WirePlumber profile that
  keeps the card on PipeWire's "Pro Audio" profile with native format
  and sample rate, for use with JACK applications such as REAPER.

Also known as / search terms: TerraTec PHASE X64 USB, TerraSoniq PHASE
X64, Musonik PHASE X64, Ploytec chipset, TerraTec Phase X64 Linux
driver. These names are used only to describe which hardware this
works with.

## Status

**v0.9.0, public beta.** Tested on one unit, on one machine.

What works:

- Playback, 4 channels (Out 1/2 and Out 3/4), 24 bit.
- Capture, 6 channels: In 1/2, In 3/4, and channels 5/6, a bit-exact
  digital copy of what is sent to Out 1/2 (for recording what you hear).
- Phono inputs with the RIAA equalisation applied in the driver, as the
  Windows driver does.
- Codec and routing changes while a stream is running (applied live,
  with a ~0.3 s gap that keeps the stream's timeline continuous).
- MIDI in and out (5-pin DIN).
- PipeWire, and JACK applications via `pipewire-jack`; bit-exact
  through PipeWire when the card's sink and source volume are at 100 %.
- Buffer sizes down to 64 samples (1.3 ms at 48 kHz).
- Sample rates 44.1, 48, 88.2 and 96 kHz (playback and capture always
  share one rate).
- Clock source: internal, or locked to the S/PDIF input.

Tested environment: Arch Linux, kernel 7.2.x, PipeWire 1.6.x,
WirePlumber 0.5.x, REAPER 7.82 via `pipewire-jack`.

## Screenshots

All screenshots are from the panel's demo mode (no device connected,
no vendor artwork).

![Main window with the "Record vinyl" preset: In 1/2 on Phono with RIAA, meters running](docs/screenshots/main-window.png)

*Main window, "Record vinyl" preset loaded.*

![Both inputs set to a Loopback source with Direct on both outputs; the panel warns about the analog feedback loop](docs/screenshots/loopback-feedback-warning.png)

*Loopback sources with the analog feedback-loop warning.*

![MIDI IN monitor window listing note, controller, pitch bend, program change and SysEx messages](docs/screenshots/midi-monitor.png)

*MIDI IN monitor (example messages).*

![BUFFER control set to 128 samples, showing 2.7 ms](docs/screenshots/buffer-control.png)

*Buffer control: PipeWire's buffer size, with the latency at the current
sample rate.*

## Hardware

A USB 2.0 interface released around 2010-2011 under the TerraSoniq
name by Musonik, which had distributed TerraTec's Producer range
(including the PHASE series) and continued it under its own brand; the
vendor's own text presented it as the successor of the PHASE 26 USB
([KVR Audio forum thread, 2011](https://www.kvraudio.com/forum/viewtopic.php?t=309484)).
It is no longer sold
([Musikhaus Korn product listing](https://www.musikhaus-korn.de/en/terrasoniq-phase-x64-usb/pd/36185)).
Listed features: 24 bit / 96 kHz, a mic/line combo input with 48 V
phantom power, a Hi-Z instrument input, two inserts, phono/aux inputs,
coaxial S/PDIF in and out, a headphone output, MIDI in and out, and
power from USB or a separate supply (same listing;
[Audiofanzine product page and user review](https://en.audiofanzine.com/usb-interface/terratec/terrasoniq-phase-x64-usb/)).

Inside, as seen from the driver: a Ploytec USB controller (USB ID
`0a4a:5b01`) and two Wolfson WM8776 codecs.

Connections on the unit used for this project:

- **Front:** Ch.1 combo socket (XLR + 6.3 mm jack) with gain knob, +48 V
  and clip LEDs; Ch.2 Hi-Z/Line jack with gain knob and clip LED;
  headphone jack with volume knob.
- **Rear:** power (9 V DC), USB-B, MIDI out/in, S/PDIF (coaxial) out/in,
  Out 3/4 (RCA), inserts for Ch.1 and Ch.2, Out 1/2 (6.3 mm jacks),
  phono/aux RCA inputs for Ch.1/2 and Ch.3/4.
- **Side switches:** 48 V on/off, Ch.1 Line/Mic, Ch.2 Line/Hi-Z, headphone
  source 1/2 or 3/4, Ch.1/2 Aux/Phono, Ch.3/4 Aux/Phono. These are
  hardware-only; the driver can't read or set them.

### Measured with this driver

- **Front inputs:** Ch.1 arrives on the left channel of In 1/2, Ch.2 on
  the right, both mono. The Ch.1 switch selects the connector: Mic uses
  the XLR part, Line the jack part. 48 V is only on the XLR pin. Gain is
  analog, set with the knobs on the unit; the clip LED lights at 0 dBFS
  at the converter. Mic gain covers about 25 dB, Line about 14 dB, Hi-Z
  at least 30 dB. Details:
  [Front inputs](linux/README.md#front-inputs-mic--hi-z--line).
- **Direct monitoring:** Direct keeps Ch.1 left and Ch.2 right; Direct
  Mono sums both to both sides.
- **Two kinds of loopback:** capture 5/6 is a digital copy of the PCM
  sent to Out 1/2, before any Direct monitoring is mixed in. The
  `Loopback 3/4` / `Loopback 1/2` capture sources record the *other*
  output pair's analog output, including Direct. Details:
  [Capture channel layout](linux/README.md#capture-channel-layout).
- **Buffer sizes:** 64, 128, 256 and 1024 samples run stable with
  playback and recording at the same time; a live change can cause one
  short click.
- **Latency:** at 64 samples, an analog loopback recording came back
  about 54 samples (≈1.1 ms) later than REAPER's own compensation
  accounts for. This is a first, approximate measurement. Details:
  [Buffer size and latency](linux/README.md#buffer-size-and-latency).
- **MIDI needs an audio stream:** the device firmware only passes MIDI
  while an audio stream is running (a capture stream alone is enough).
  The driver's MIDI code doesn't depend on it; the device does.

## Quick start (Arch Linux / Manjaro)

```sh
sudo pacman -S --needed base-devel dkms linux-headers git   # use the headers for your kernel
git clone https://github.com/flarkflarkflark/phasex64-linux.git
cd phasex64-linux/linux
makepkg -s
sudo pacman -U snd-usb-phasex64-dkms-*.pkg.tar.zst phasex64-panel-*.pkg.tar.zst
```

Install the headers that match your kernel (for example
`linux-zen-headers`, or `linuxXYZ-headers` on Manjaro). DKMS then builds
the module, also after every kernel update.

Then unplug and replug the card, or load the module directly:

```sh
sudo modprobe snd-usb-phasex64
cat /sys/module/snd_usb_phasex64/version   # 0.9.0
```

Start the panel from the application menu ("PHASE X64 USB") or with
`phasex64-panel`.

Optional, for JACK/REAPER use: the WirePlumber profile. Read
[PipeWire / JACK](linux/README.md#pipewire--jack) first - it changes
WirePlumber's behaviour for the whole machine.

```sh
sudo pacman -U phasex64-wireplumber-*.pkg.tar.zst
systemctl --user restart wireplumber
```

Other distributions: packages follow in a later release. Until then the
module builds by hand on any distribution with kernel headers (see
[Installing on Arch Linux](linux/README.md#installing-on-arch-linux),
"Building by hand"), and the panel runs straight from the checkout as
`linux/phasex64-panel/phasex64-panel` (needs Python 3 with Tk, and
alsa-utils). The full manual is [linux/README.md](linux/README.md).

## Known limitations

- **MIDI only while audio streams.** See above; with the WirePlumber
  profile the stream stays open, so MIDI keeps working.
- **Side switches must match.** The Aux/Phono switch for Ch.1/2 and
  Ch.3/4 has to match the capture source chosen in the panel; the driver
  can't read the switches.
- **A locked S/PDIF input sets the clock.** When the S/PDIF input is
  locked, the device follows its clock even with the internal clock
  selected. The panel shows the lock state.
- **S/PDIF audio** (recording from or playing to S/PDIF) hasn't been
  characterised yet.
- **Gain and 48 V are analog,** on the unit itself; no software control.
- **`status read failed: bRequest=0x49`** in `dmesg` (debug level only)
  is expected and harmless - see
  [If something doesn't work](linux/README.md#if-something-doesnt-work).
- **The original Windows panel skin is optional** and has to be imported
  from your own copy of the vendor driver package; it isn't included
  here.

## Disclaimer

This project is not affiliated with or endorsed by TerraTec, TerraSoniq,
Musonik or Ploytec. All trademarks belong to their owners. The driver
was developed by independent reverse engineering for interoperability;
no vendor code or firmware is included. It is an experimental kernel
module and comes with no warranty (see the GPL-2.0 licence).

## Acknowledgements

- The ALSA and Linux kernel developers.
- The original engineers of the PHASE X64 and its firmware.
- Marcel Bierling, for the [Ozzy](https://github.com/mischa85/Ozzy)
  driver for Ploytec interfaces (MIT), an early reference for the
  protocol.
- The users and reviewers whose published reviews and discussions
  informed the hardware overview:
  [Audiofanzine](https://en.audiofanzine.com/usb-interface/terratec/terrasoniq-phase-x64-usb/user_reviews/),
  the [KVR Audio forum thread](https://www.kvraudio.com/forum/viewtopic.php?t=309484).
- AI agents that contributed:
  - Claude by Anthropic (Opus and Sonnet) - analysis, driver and panel
    implementation, reviews.
  - OpenAI Codex - the first implementation of the MIDI monitor.
- Project lead: Flark / flarkAUDIO (GitHub
  [flarkflarkflark](https://github.com/flarkflarkflark)) - direction and
  all hardware testing.

Built by loopback-coding: AI-assisted, human-directed, hardware-verified.
Every change went out, came back through real hardware, and was measured
before it counted.

## Licence

GPL-2.0-only - see [LICENSE](LICENSE). The reverse-engineering notes are
in [analysis/](analysis/README.md).
