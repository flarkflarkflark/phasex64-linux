---
name: Hardware report
about: Report how the driver and panel work with your PHASE X64 USB and system
title: "Hardware report: <distro>, kernel <version>"
labels: ""
---

<!-- Thanks for testing. Reports from other machines are the most useful
     thing at this stage, including "everything works". -->

## System

- Distribution and version:
- Kernel (`uname -r`):
- PipeWire version (`pipewire --version`):
- WirePlumber version (`wireplumber --version`):
- Driver module version (`cat /sys/module/snd_usb_phasex64/version`):
- Panel version (`phasex64-panel --version`):
- Installed via: <!-- makepkg/pacman -U, built by hand, ... -->
- WirePlumber profile (`phasex64-wireplumber`) installed: yes / no
- Application(s) used: <!-- e.g. REAPER via pipewire-jack, Audacity, ... -->

## What works

<!-- e.g. playback, capture, phono with RIAA, MIDI in/out, live panel
     changes, buffer sizes you tried, sample rates you tried -->

## What doesn't work

<!-- What you did, what you expected, what happened instead. -->

## Kernel log

Relevant lines from:

```sh
sudo dmesg | grep -i -E "phasex64|status read|0x49"
```

```
<paste here>
```

<!-- An occasional "status read failed: bRequest=0x49 ... err=-32" at
     debug level is expected and harmless. -->
