# Capture channels 5/6, the Loopback sources, and "record what you hear"

Read-only research, driver unchanged. Written to answer three things: what
channels 5/6 actually are, what the existing `Loopback 3/4`/`Loopback 1/2`
capture sources are (a different mechanism, easily confused with the
first), and how a user can actually record "what you hear" in REAPER and
PipeWire.

**2026-10-16 update**: the original version of this document hypothesized
that channels 5/6 were a *physical* S/PDIF mirror requiring the user's own
Digital Out -> Digital In coax loopback cable, and explicitly flagged that
hypothesis as untested. It's now hardware-tested (see "Hardware test
results" below) and the hypothesis was **wrong on that specific point**:
5/6 needs no cable at all. The routing/content findings (playback 1/2,
intact, modulo the one periodic-duplicate-frame artifact) hold up and are
sharpened by the new tests; the *mechanism* section below is rewritten
accordingly, and the old cable-dependent guidance in "Recording what you
hear" is corrected to match.

## What capture channels 5/6 are

**Answer: an always-on, internal digital tap of the PCM/DSP stream feeding
Out 1/2 specifically - not a physical S/PDIF loopback, no cable needed,
bit-exact, and independent of the Out 1/2 output switches.** Confirmed by
direct hardware test (below); no driver change implied, since the driver
already exposes these channels and sends nothing related to this path.

Hardware-confirmed, 2026-10-16 (full methodology and data in "Hardware
test results"):

- **No cable required.** With the Digital Out -> Digital In coax cable
  physically disconnected (confirmed by eye) and `Digital Input Lock`
  reading `off` throughout, a 1 kHz/-12 dBFS tone played on Out 1/2
  appears on capture 5/6 at exactly -12.0 dBFS. This overturns the
  original hypothesis, which had assumed the cable was required but never
  actually tested the no-cable case.
- **Bit-exact.** Sample-for-sample comparison of the captured 5/6 data
  against the exact file sent to `aplay` (after sample-accurate alignment)
  gives a gain ratio of exactly `256.000000` - precisely the 24-to-32-bit
  left-shift ALSA uses to represent the device's native 24-bit samples in
  a 32-bit capture format - with **0 LSB** maximum difference and 0.00 LSB
  RMS difference across the comparison window. This is a lossless digital
  copy of the PCM data, not a re-digitized analog signal.
- **Out 1/2 specifically, not the output mix.** The same tone played on
  Out 3/4 instead produces **exact digital silence** on 5/6 (literal
  `-inf` dBFS, not just "quiet"). 5/6 does not mirror Out 3/4 and does not
  mirror "the mix" - only Out 1/2.
- **Independent of the Out 1/2 output switches.** With `PCM 1/2 Playback
  Switch` turned **off** (so Out 1/2's analog jack is muted/silent), the
  same tone still appears on 5/6 unchanged - same level, same timing. 5/6
  taps the PCM/DSP data itself, upstream of the WM8776 output mux
  (`WM_OUT_MUX`/R22) that the Direct/PCM/Mono switches control. Practical
  consequence: 5/6 reflects what's being *sent* to Out 1/2, not
  necessarily what's currently audible at the physical jack - worth
  stating plainly in any user-facing description (see "Recording what you
  hear" below). `Direct 1/2 Playback Switch` wasn't independently tested
  (no live analog source on hand for that sub-test this session), but
  given PCM itself doesn't gate 5/6, it's a safe inference that Direct
  doesn't either - 5/6 is positioned before all of the output-stage
  switches, not selectively gated by one of them.
- **Tracks playback level exactly.** A -24 dBFS source reads -24.0 dBFS on
  5/6 - no fixed analog gain stage, no clipping/headroom surprises.
- **Works at other sample rates.** Repeated at 44.1 kHz (vs. 48 kHz):
  same bit-exact, same-level result.

One thing this session did *not* test: whether an external S/PDIF source
on the physical `Digital In` jack also reaches 5/6 (e.g. mixed in, or
switched to, alongside the internal Out 1/2 tap). What's established is
that 5/6 does not *require* the jack/cable - whether it additionally
*responds* to it when something real is connected is untested and lower
priority, since the primary use case ("record what you hear" on Out 1/2)
doesn't depend on the answer either way.

## `Loopback 3/4` / `Loopback 1/2`: a different mechanism

These are two of the selectable items on the `In 1/2 Capture Source` /
`In 3/4 Capture Source` ALSA enum controls (`analysis/NOTES.md`, "ALSA
control names") - **not** related to channels 5/6 or the S/PDIF jack at
all:

- `In 1/2 Capture Source = Loopback 3/4` routes **Out 3/4** into the
  **In 1/2** ADC mux (WM8776 codec 0, register R21, bit `0x10` - see
  "Control panel -> hardware mapping", `BTN4 Out 3/4 -> In 1/2`).
- `In 3/4 Capture Source = Loopback 1/2` routes **Out 1/2** into the
  **In 3/4** ADC mux (codec 1, R21, bit `0x10`, `BTN10 Out 1/2 -> In 3/4`).

This is an **analog, chip-internal** route inside the WM8776 - no S/PDIF,
no coax cable, no digital mirror, and it lands on capture channels 1-4 (via
the normal ADC mux path), not 5/6. It exists specifically so one output
pair's analog signal can be captured through the *other* pair's input
without an external patch cable. `analysis/NOTES.md` "v0.1.5" pitch-checked
both this path and the S/PDIF mirror together (the "~1.1x pitch shift"
turned out to be a measurement artifact affecting both, traced to
something upstream of either path - not evidence they're the same
mechanism, just that whatever glitch exists affects both playback-derived
capture paths similarly).

### Hardware-confirmed (2026-10-19): it's the analog output stage, not a copy of the other input

Two readings of "routes Out 3/4 into the In 1/2 ADC mux" were both
consistent with the vendor's own naming until this test: (1) a copy of
In 3/4's own input signal (bypassing the output stage entirely), or (2)
the actual analog signal driving the Out 3/4 jack (Direct + PCM as
mixed/routed). An earlier same-day null test (In 1/2 = Loopback 3/4,
In 3/4 = Aux with a synth playing, Out 3/4 with both Direct and PCM
switched on but nothing actually playing through PCM) found the two
tracks null completely after a ~0.3-0.75 dB level correction and
polarity inversion - sample-aligned, no filtering difference - but
couldn't distinguish (1) from (2): with Direct on and nothing coming
through PCM, Out 3/4's actual output *equals* In 3/4's own signal
either way.

Disambiguating test, same session - the PCM switch itself stays on
throughout all three steps below; only whether Direct is on and
whether anything is actually *playing* through PCM changes, with the
same Aux synth still playing into In 3/4 (-13.9 dBFS) the whole time
and In 1/2 still set to Loopback 3/4:

| Step | Out 3/4 state | In 1/2 (Loopback 3/4) reads |
|---|---|---|
| A | Direct on, PCM on but nothing playing | -14.7 dBFS (follows the synth via Direct) |
| B | Direct off, PCM on but nothing playing | -84.7 dBFS (analog noise floor) - **synth keeps playing on In 3/4 the whole time** |
| C | Direct off, PCM on and actually playing music from REAPER (Out 3/4 at 0.0 dBFS) | -0.3 dBFS - follows the PCM audio instead |

In 1/2 drops to the noise floor at step B, exactly when Out 3/4 itself
goes quiet, even though In 3/4's own input signal (the synth) never
stopped - rules out (1) outright. Step C is the positive confirmation:
once real audio actually flows through that same, still-on PCM path,
In 1/2 follows it almost exactly (0.0 to -0.3 dBFS). **Confirmed:
candidate (2), the analog output stage.** Roughly 0.3-0.8 dB lower
than the actual source across these measurements (consistent with the
earlier null test's own ~0.3-0.75 dB) - likely the Direct/output
mixing stage's own small analog gain, not independently broken down
further.

**Not tested**: the DSP/Sound Module route's own effect on the loopback
signal, and `Loopback 1/2` (`In 3/4 Capture Source`, mirroring Out 1/2
into In 3/4) specifically - presumed, by the identical vendor naming
(`BTN10 Out 1/2 -> In 3/4`) and mechanism, to behave the same way as
`Loopback 3/4` just confirmed, but not independently hardware-tested.
The panel's own live indicator (`linux/phasex64-panel`'s
`LOOPBACK_STATUS_TOOLTIP`) states this plainly rather than asserting it
as confirmed.

## A closed analog feedback loop (hardware-confirmed, 2026-10-19)

Distinct from the software-monitoring feedback loop in "Recording what
you hear" below (a DAW routing a Loopback input's track back to the
output it's tapping) - this one is entirely internal to the card, no
software involved. Both Loopback sources now being confirmed as
genuine output taps (previous section) means selecting **both** at
once, with Direct **on for both outputs**, closes a loop purely
through the WM8776's own analog routing:

```
In 1/2 --Direct--> Out 1/2 --Loopback 1/2--> In 3/4
In 3/4 --Direct--> Out 3/4 --Loopback 3/4--> In 1/2
```

i.e. each output's Direct-monitored signal feeds back into the input
that the *other* output's Loopback source taps, which itself feeds the
first output's Direct path - a closed ring.

**Measured**: with this exact combination active (`In 1/2 Capture
Source = Loopback 3/4`, `In 3/4 Capture Source = Loopback 1/2`,
`Direct 1/2 Playback Switch = on`, `Direct 3/4 Playback Switch = on`),
both inputs read about **-68.7 dBFS**, versus the ~-85 dBFS analog
noise floor measured with the loop open (same session, same "Hardware-
confirmed" test above). At unity gain this is still only noise, well
below any audible or damaging level - but it is a real, closed loop
with gain somewhere around unity-ish in this condition, not nothing;
raising gain anywhere in that path (the WM8776's own analog stages, or
anything downstream) risks it ringing or oscillating rather than
staying at a fixed low level.

**Avoiding it**: break the loop at any one of its four conditions -
turn Direct off on at least one of the two outputs, or use only one
Loopback source at a time (leave the other input on Aux/Phono/Sound
Module/Mute instead of the other Loopback item). The panel
(`linux/phasex64-panel`'s `feedback_loop_active()`) detects exactly
this combination live and switches both Loopback indicators to an
amber warning state while it holds, reverting the instant any one
condition changes - see `FEEDBACK_LOOP_TEXT`/`FEEDBACK_LOOP_TOOLTIP`.

**Not independently tested**: whether the DSP/Sound Module route can
close an equivalent loop (it wasn't part of this measurement), and
whether a *partial* version of this loop (e.g. only one Direct switch
on, or only one Loopback source selected) shows any measurable rise
above the noise floor - the four-condition combination above is the
only one measured, and the only one the panel currently detects.

## Recording "what you hear"

Two genuinely different situations, because the answer depends on whether
PipeWire or REAPER owns the PHASE X64 directly.

### If PipeWire has the device (the normal case)

**Use PipeWire's own sink monitor - no hardware loopback, no cable, no
clock side effects.** Every PipeWire sink exposes a `.monitor` source
automatically; recording from it captures exactly what that sink is
playing, in software, with no round-trip through the device at all:

```sh
pactl list sources short | grep monitor      # find "...PhaseX64...monitor"
```

Point REAPER's (or any recorder's) input at that monitor source like any
other input device. This is the standard Linux "stereo mix" equivalent,
it's what most users mean by "record what you hear," and it sidesteps
everything above entirely - no S/PDIF cable, no `Loopback 3/4` routing, no
interaction with `Digital Input Lock`/`Clock Source`.

### If REAPER has the device directly (ASIO-style exclusive `hw:` access)

PipeWire never sees the stream in this mode, so its monitor source isn't
available. Three options, in order of preference:

1. **Capture channels 5/6**, if Out 1/2 is the output you want to record.
   No cable, no clock side effects, no extra routing to set up -
   channels 5/6 are simply always present in the device's normal 6-channel
   capture stream (In 1/2, In 3/4, 5/6) and always reflect Out 1/2's
   PCM/DSP content, bit-exact (see "What capture channels 5/6 are" above).
   In REAPER: add input channels 5/6 to a track, arm, record. This is now
   the simplest and most robust option for this specific case, and
   supersedes the hardware-loopback-cable advice this document used to
   give. **Caveat**: 5/6 reflects what's being *sent* to Out 1/2, not
   necessarily what's currently *audible* there - it's unaffected by
   `PCM 1/2 Playback Switch`, so it keeps capturing PCM content even if
   that output is muted. If you've explicitly silenced Out 1/2 and expect
   silence in the recording, check that switch, not just what channels
   5/6 "should" be doing.
2. **A bus/send inside REAPER itself** - route the output tracks (also)
   into a submix bus and record that bus. Zero hardware interaction,
   works identically regardless of audio backend and of which output pair
   you're recording (Out 1/2 or Out 3/4), and is how most DAW users
   already do this for a reason unrelated to this device. Prefer this
   over option 1 when you need Out 3/4, since 5/6 is Out-1/2-only.
3. **`Loopback 3/4`/`Loopback 1/2`** (above), only if something outside
   REAPER's own mixer genuinely needs to see the signal (e.g. feeding a
   second, separate recording device or app that can't share REAPER's
   internal bus, and can't use capture 5/6 either) - analog, no cable, no
   clock effects, but uses up that input pair's normal capture role.

## Hardware test results (2026-10-16)

Setup: PipeWire fully stopped (`pipewire`/`pipewire-pulse`/`wireplumber`
services and sockets), raw `arecord`/`aplay` against `hw:PhaseX64`, test
tones generated with `sox` (24-bit/48kHz 4-channel WAVs, silence on the
untested output pair, a 1s silent lead-in before the tone for onset
detection), 6-channel `arecord -f S32_LE -c 6` capturing In 1/2, In 3/4,
and 5/6 simultaneously. Analysis in Python/numpy: per-pair level and
dominant-frequency via FFT, onset via envelope threshold, and sample-exact
alignment (coarse via onset detection, refined via a short local
cross-correlation anchored at the attack transient - a long steady-tone
correlation window turned out to be unreliable, phase-locking to the
wrong region, since a pure sinusoid's correlation is strong at *any* lag
with enough periodic overlap; the fix was a tight local search around an
independently-detected onset, not periodic matching).

| Test | Result |
|---|---|
| Cable physically disconnected (confirmed by eye) + 1kHz/-12dBFS tone on Out 1/2 | 5/6: -12.0 dBFS, 1000 Hz, `Digital Input Lock` off throughout |
| Sample-exact bit comparison, 5/6 vs. the sent file | gain ratio exactly 256.0 (24-to-32-bit shift), 0 LSB max diff, 0.00 LSB RMS diff |
| Same tone, on Out 3/4 instead | 5/6: exact digital silence (`-inf` dBFS) - confirms Out-1/2-only, not the mix |
| Same tone on Out 1/2, `PCM 1/2 Playback Switch` off | 5/6: unchanged (-12.0 dBFS, same timing) - independent of the output switch |
| -24 dBFS tone on Out 1/2 | 5/6: -24.0 dBFS - tracks playback level exactly |
| Same test at 44.1 kHz instead of 48 kHz | 5/6: -12.0 dBFS, bit-exact - works at both rates |

Not tested this session (lower priority, don't affect the "record what you
hear" use case): `Direct 1/2 Playback Switch`'s effect specifically (no
live analog source on hand), and whether an external, non-self S/PDIF
source on `Digital In` also reaches 5/6.

No driver change was made or is implied by any of this - measurement only.
Scripts and raw captures are session-local (not checked in); the
methodology above is reproducible with the commands shown.

## Proposed exposure (not implemented - needs a go-ahead)

Channels 5/6 are already captured today (they're part of the device's
fixed 6-channel capture format) - this is about making their purpose
discoverable, not adding new functionality:

- **ALSA channel naming.** ALSA capture channel maps are per-stream, not
  nameable per pair the way mixer controls are, so there's no control to
  rename - but `snd_pcm_chmap` could label them, e.g.
  `NA, NA, NA, NA, "Out 1/2 Monitor L", "Out 1/2 Monitor R"` instead of
  generic channel numbers, so a channel-aware host (REAPER's input list
  included) shows a readable label instead of "channel 5"/"channel 6".
  Needs checking what `chmap` support this driver currently has, if any.
- **Panel indicator.** A small label under/near the Out 1/2 peak meter
  card, e.g. "Also on capture 5/6" with a tooltip explaining the bit-exact,
  switch-independent relationship (including the mute caveat above) - low
  effort, directly addresses "I didn't know this existed" for users who'd
  otherwise reach for a physical cable or REAPER bus trick unnecessarily.
- **README**: the "Recording what you hear" section above, carried into
  `linux/README.md`/`README.nl.md` once this document's content is
  reviewed - a `tests/hw/` audience document isn't somewhere a typical
  user would find this.

No driver change needed for any of this - `chmap` labeling and the panel
indicator are both read-only/cosmetic additions to code that already
exists.

### Addendum (2026-10-19): PipeWire/WirePlumber port names hit the same wall

While wiring up `linux/wireplumber/51-phasex64-pro-audio.conf` (see
`linux/README.md`, "PipeWire / JACK"): the "Pro Audio" profile's ports
are generic by channel index (`capture_AUX0`..`AUX5`,
`playback_AUX0`..`AUX3`), same unlabeled-5/6 problem as above, one
layer further out. A WirePlumber `monitor.alsa.rules` `update-props`
rule cannot rename individual ports - port names come from the
ALSA-Card-Profile profile-set definition itself (the same `paths`/port
list ACP reads to build the "Pro Audio" profile), not from anything a
device/node-level WirePlumber rule can override. Fixing this at the
PipeWire layer would mean a custom ACP profile-set for this device, a
separate, heavier piece of work than a drop-in `.conf.d` snippet - out
of scope for that task, folded into this backlog item instead since
it's the same underlying problem (5/6, or AUX4/AUX5 one layer out,
having no discoverable meaning) rather than a new one.
