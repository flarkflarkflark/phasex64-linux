# PHASE X64 USB on Linux

Linux support for the TerraTec / TerraSoniq PHASE X64 USB (USB ID `0a4a:5b01`).
The protocol was reverse-engineered from Windows driver 2.9.34; see
`../analysis/NOTES.md`.

- `snd-usb-phasex64/`: ALSA kernel driver (playback, capture, MIDI, mixer)
- `phasex64-panel/`: control panel, the Linux equivalent of `CPLPhaseX64USB.exe`

A Dutch translation of this file is available at `README.nl.md`; this file
is the source of truth.

**Versions:** the driver, the panel and the packages share one version
number. It jumped from 0.1.13 to 0.9.0 for the first public release (the
panel was separately numbered 0.2.0 before that); the "Since v0.1.12" /
"Since v0.1.13" notes below refer to those earlier driver versions.

## What the driver provides

| ALSA | Content |
|---|---|
| PCM playback | 4 channels, S24_3LE, 44.1 / 48 / 88.2 / 96 kHz |
| PCM capture | 6 channels, S32_LE (24 bit), same rates |
| MIDI | 1 in, 1 out |
| Mixer | every button from the Windows panel, plus "Clock Source" and "Digital Input Lock" |

Playback and capture always run at the same sample rate (one clock in the
device).

MIDI only works while a PCM stream (playback or capture) is active - e.g.
with REAPER open and a stream running, or `arecord`/`aplay` against the
card - the same constraint as the original Windows driver, and confirmed
live on real hardware: MIDI stops the moment the stream stops, it doesn't
stay armed (see `../analysis/NOTES.md`, "MIDI").

## Installing on Arch Linux

```sh
sudo pacman -S --needed base-devel dkms linux-headers   # or linux-zen-headers etc.
cd linux
makepkg -si
```

This builds the driver via DKMS, including after every kernel update. Then
unplug and replug the card, or:

```sh
sudo modprobe snd-usb-phasex64
```

Building by hand without the package also works:

```sh
cd linux/snd-usb-phasex64
make
sudo insmod snd-usb-phasex64.ko
```

## Control panel

The original skin isn't shipped with this project. Import it once from the
Windows driver package (for example from a Windows partition):

```sh
phasex64-panel --import-skin /path/to/MUSONIK_PHASE_X64_USB_X64_2.9.34
phasex64-panel
```

Without a skin (`phasex64-panel --plain`) you get plain buttons with the
same functions. Every button is also a regular ALSA control
(`alsamixer -c PhaseX64`) and is saved with `alsactl store`. Windows'
latency buttons don't exist here: set the buffer size in PipeWire, JACK or
your DAW instead.

The panel UI is multilingual via gettext; it follows the system locale
(`LANG`/`LC_MESSAGES`/`LC_ALL`, which desktop environments such as Plasma
set). See `phasex64-panel/po/README.md` for the translation workflow -
Dutch (`nl`) is the first translation.

**Note when upgrading to v0.1.4**: the five separate "In 1/2 ... Capture
Switch" controls and the four "In 3/4 ... Capture Switch" controls were
replaced by two enum controls, `In 1/2 Capture Source` and `In 3/4 Capture
Source` (see `analysis/NOTES.md`, "ALSA control names"). An old
`/var/lib/alsa/asound.state` with the old names no longer matches those
controls - harmless, but run `sudo alsactl store` again after first loading
v0.1.4 so the new names get saved.

**Since v0.1.12**: panel changes (codec routing, RIAA, source selection)
apply live while a stream is running - playback/capture keep going, with
no audible gap in the stream's own timeline (measured drift under 3ms
per change, not accumulating). The change itself takes effect across a
brief (~0.3s) internal reconfiguration during which audio is silent; one
click or transient that happens to land exactly in that window can be
lost. Before v0.1.12, a change made while streaming only took effect at
the next stream start - the panel still shows that older behavior's text
when loaded against a driver older than v0.1.12.

## MIDI monitor

Two IN/OUT activity LEDs sit next to the status bar's `MIDI:` field,
always on - they light briefly on real traffic (driven only by the
ALSA rawmidi `Rx`/`Tx` byte counters in `/proc/asound/cardN/midiN`,
read on the normal poll) regardless of whether the monitor window
below is open, and regardless of PCM stream state. The status bar's
`MIDI:` field itself shows `available`/`unavailable` - whether the
rawmidi *device* is present - not the monitor window's own connection
state (that only ever shows inside the window itself, top right).

A **MIDI…** button next to **CLOCK…**/**ABOUT** opens a themed window
showing incoming MIDI messages as text. Only once that window is open
does the panel resolve the PHASE ALSA-seq port by name (never a fixed
client number - re-resolved every time, so card/client renumbering and
unplug/replug don't need a restart) and run `aseqdump` against it;
closing the window stops that `aseqdump` cleanly (`SIGINT`, not a
plain kill, so it flushes before exiting) - nothing runs in the
background while the window is closed.

**IN only, not OUT**: the window shows incoming messages as text, but
outgoing messages (anything your DAW or another app sends *to* the
device) can only be shown as activity (the OUT LED), never as message
content - ALSA's sequencer has no passive way to observe data written
into a port by another client, only what the port itself emits to its
own subscribers. This is a structural limitation, not something a
future version of this panel can lift.

**Needs an active stream to actually do anything**: the LEDs, the
button, and the monitor window all work regardless of PCM stream
state - but real MIDI bytes only flow while a stream is active. This
is the hardware's own behavior (repeatedly confirmed, including with a
real external controller - see `../analysis/NOTES.md`, "MIDI"), not
something this driver enforces or could remove: with no stream
running, the monitor window will sit quietly with nothing arriving,
and its own hint text says so - that's expected, not a sign anything
is broken. See "What the driver provides" above for the same
constraint from the driver's side.

**Troubleshooting OUT**: sending to the PHASE ALSA-seq port directly
(e.g. `aseqsend`) only reaches the hardware while some other
application already has the device's rawmidi *output* open (REAPER
via PipeWire's Midi-Bridge, `aplay`, etc. - check `Owner PID` under
`Output 0` in `/proc/asound/cardN/midi0`). With no owner, the message
is silently dropped and the Tx counter/OUT LED simply won't move -
this is ALSA sequencer/rawmidi-bridge behaviour, not something this
driver or panel controls either way.

Clock and Active Sensing messages are filtered out of the monitor by
default (a steady 120 BPM clock alone is 48 messages/second - mostly
noise for a human reading the log) with a toggle to show them. Other
ALSA clients' own connect/disconnect traffic (e.g. "Port subscribed")
is filtered separately, also off by default, via its own "Show system
events" toggle - a different kind of noise from Clock/Active Sensing,
worth showing independently of it. Long lines (e.g. a SysEx dump) wrap
rather than getting cut off at the window edge; history is capped and
the display updates in batches, not once per incoming
message.

## First test

```sh
dmesg | grep -i phase                      # driver loaded, no errors?
aplay -l; arecord -l                       # card visible?
speaker-test -D hw:PhaseX64 -c 4 -F S24_3LE -r 48000 -t sine
arecord -D hw:PhaseX64 -c 6 -f S32_LE -r 48000 -d 5 test.wav
```

Then just select the "PHASE X64 USB" card in PipeWire/REAPER.

## PipeWire / JACK

By default PipeWire/WirePlumber may pick a consumer stereo profile for
this card (hiding most of its channels) rather than "Pro Audio", and
REAPER's own exclusive ALSA access then means stopping PipeWire first.
`linux/wireplumber/51-phasex64-pro-audio.conf` avoids both: it pins the
card to the "Pro Audio" profile and its two nodes (4ch playback, 6ch
capture) to the native 48kHz rate with no resampling or channel
mixing, so REAPER can use the device **through** PipeWire (via
`pipewire-jack`) at the same time as ordinary desktop audio, without
ever needing PipeWire stopped.

**Install (test first, user-level)**:

```sh
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
cp linux/wireplumber/51-phasex64-pro-audio.conf ~/.config/wireplumber/wireplumber.conf.d/
systemctl --user restart wireplumber
```

**Verify**:

```sh
wpctl status                 # Active Profile: pro-audio under the card;
                              # default sink/source (*) should be PHASE X64
pw-dump | grep -i phase -A3 -B3 | grep -i 'audio.rate\|audio.format'
```

Expect `audio.rate = 48000` with no other rate offered, and
`audio.format` left as whatever ACP already negotiates per node
(`S24LE` on playback, `S32LE` on capture - this config deliberately
doesn't pin format, see the `.conf` file's own comment: the two nodes
need different values, and ACP already gets it right on its own).

**One-time volume fix**: the capture node can come up at a software
volume below unity (observed 70%) the *first* time WirePlumber ever
sees it - this device has no hardware capture gain control at all
(`amixer -c PhaseX64` has no volume control to restore from), and
nothing in this config can pin a node's volume at creation time. Not
bit-exact at 70% - fix it once:

```sh
pactl set-sink-volume alsa_output.usb-MUSONIK_GmbH_MUSONIK_GmbH_PHASE_X64_USB_no_serial_number-00.pro-output-0 100%
pactl set-source-volume alsa_input.usb-MUSONIK_GmbH_MUSONIK_GmbH_PHASE_X64_USB_no_serial_number-00.pro-input-0 100%
```

WirePlumber persists this (`~/.local/state/wireplumber/stream-properties`)
and restores it on every later restart/reconnect - a one-time fix per
machine, not a recurring step.

Once confirmed, install it system-wide for every user on the machine
via the optional `phasex64-wireplumber` package (`makepkg -si` from
this `linux/` directory, same as the driver/panel - see "Installing on
Arch Linux" above):

```sh
sudo pacman -S phasex64-wireplumber
```

This is a separate, optional package rather than part of
`phasex64-panel` - see the PKGBUILD's own comment on
`package_phasex64-wireplumber()` for why (it changes session-manager
behaviour for the whole machine, not just whoever wants the GUI
mixer). It installs the identical file to
`/usr/share/wireplumber/wireplumber.conf.d/51-phasex64-pro-audio.conf`
- **remove your user-level test copy first** (the uninstall command
under "Rollback" below, just the `rm`, not the `systemctl restart`) so
WirePlumber doesn't load the same rule twice from two places; then
`systemctl --user restart wireplumber` once, after the package is
installed.

**Port names**: the "Pro Audio" profile's ports are named generically
by channel index (`playback_AUX0`..`AUX3`, `capture_AUX0`..`AUX5`),
matching this driver's own channel order (playback: Out 1/2, Out 3/4;
capture: In 1/2, In 3/4, Out 1/2 tap - see "Recording what you hear"
below). Renaming them isn't possible from this config file - port
names come from the ALSA-Card-Profile profile-set itself, not a
WirePlumber rule - same underlying problem as capture channels 5/6
having no discoverable label in plain ALSA, one layer further out; see
`analysis/loopback-and-monitoring.md`, "Proposed exposure", where this
is tracked.

**Desktop audio**: a plain stereo app (browser, media player) still
lands on Out 1/2 with correct L/R - `channelmix.disable`/
`resample.disable` above only pin the *node's* own boundary with the
hardware (nothing left to convert, since the format/rate already match
natively); routing an ordinary 2-channel client stream onto the first
two of this node's four channels is a separate stage of PipeWire's own
default stream-to-sink mixing, unaffected by those two properties.

**Bit-exactness check**: play a known 24-bit file through PipeWire to
Out 1/2, record the card's own capture node, and compare capture
channels 5/6 (the bit-exact Out 1/2 tap, same one described in
"Recording what you hear" below) against the source file - any
difference means PipeWire converted something on the way out, and the
rate/format pins above need re-checking. **Confirmed** (2026-10-19,
1kHz/-12dBFS tone, `pw-play`/`pw-record` with `--channel-map
AUX0,AUX1,AUX2,AUX3,AUX4,AUX5`): 140000/140000 samples on both
channels bit-for-bit identical to the source file, zero difference -
*once the sink's own volume is at exactly 100%* (see next paragraph;
the first attempt at this test, at the sink's then-current 95%,
reproduced that exact volume as a uniform linear scale factor with a
sub-1-LSB residual - not a PipeWire conversion artifact, just the
ordinary software volume control still being, correctly, in the
signal path).

**Sink/source volume must stay at 100% for bit-exact use**: the
PHASE X64 sink's own software volume (`wpctl`/`pactl`/the desktop
volume control) scales *everything* sent to Out 1/2 - desktop audio,
REAPER/JACK playback, all of it - and the source's software volume
scales everything captured the same way. This is ordinary, expected
PipeWire behaviour (see "One-time volume fix" above) and not something
this config disables, since desktop users still need a usable volume
control - but it means bit-exact recording/playback through PipeWire
requires both set to exactly 100% first:

```sh
pactl set-sink-volume alsa_output.usb-MUSONIK_GmbH_MUSONIK_GmbH_PHASE_X64_USB_no_serial_number-00.pro-output-0 100%
pactl set-source-volume alsa_input.usb-MUSONIK_GmbH_MUSONIK_GmbH_PHASE_X64_USB_no_serial_number-00.pro-input-0 100%
```

**REAPER via JACK**: needs `pipewire-jack` (`sudo pacman -S
pipewire-jack`, not installed by default). Point REAPER's audio system
at JACK; pipewire-jack bridges it into the same PipeWire graph as
everything else. Latency/quantum is a **graph-wide** PipeWire setting,
not specific to this device - set it with
`pw-metadata -n settings 0 clock.force-quantum <256|1024|0>` (`0`
clears the override) rather than editing this file. **Confirmed**
(2026-10-19): REAPER via `pipewire-jack` at quantum 1024 and 256,
6ch/4ch, recording vinyl on In 1/2 continuously, desktop audio alongside
still working, no glitches.

**Live apply while streaming through PipeWire**: a panel change's
~0.3s restart gap (see "Since v0.1.12" above) happens underneath
PipeWire the same way it would under direct ALSA access - PipeWire
rides through it using the same timeline-fill mechanism, without
dropping the node. **Confirmed** (2026-10-19): live panel toggles on
In 1/2 during a REAPER/JACK recording at both quantum 1024 and 256 -
recording stayed continuous, no node drop, no glitch.

**Suspend**: by default WirePlumber closes an idle ALSA device after
5s (`session.suspend-timeout-seconds`). The config above disables that
for this card (`= 0`) so MIDI - which this driver only carries while a
PCM stream is open, see above - keeps working at all times rather than
only right after audio last played. Trade-off: the device's own
hardware (clock, DAC/ADC) then never powers down on its own. Set the
value back to `5`, or remove that one line, to restore the default
idle-close behaviour instead.

**Rollback**: removing the file and restarting wireplumber undoes the
*suspend* behaviour immediately - without it, the nodes suspend when
idle again (the panel shows "Stream: idle", empty meters, "MIDI:
inactive" until a stream starts, instead of always-on) - **but not the
profile** (confirmed 2026-10-19): WirePlumber remembers the last
profile you actually used, in its own state
(`~/.local/state/wireplumber/default-profile`), independently of
whether this config file exists. If `pro-audio` was ever active, it
stays active after removing the file, because WirePlumber restores
*that*, not whatever ACP would have auto-picked from scratch. A full
revert to today's behaviour needs both steps:

```sh
rm ~/.config/wireplumber/wireplumber.conf.d/51-phasex64-pro-audio.conf
systemctl --user restart wireplumber
pactl set-card-profile alsa_card.usb-MUSONIK_GmbH_MUSONIK_GmbH_PHASE_X64_USB_no_serial_number-00 \
  output:multichannel-output+input:multichannel-input
```

(or pick a profile from Plasma's/your desktop's own sound settings
instead of the `pactl` command - same effect). Skip that last step if
you want to keep using `pro-audio` without the rest of this config
(suspend, rate/format pins) - the profile and this file are
independent, which is exactly why removing the file alone doesn't
revert it.

## Capture channel layout

The device always captures 6 channels in one stream:

| channels | content |
|---|---|
| 1/2 | In 1/2 - whatever `In 1/2 Capture Source` selects |
| 3/4 | In 3/4 - whatever `In 3/4 Capture Source` selects |
| 5/6 | Out 1/2 tap - always-on, digital, bit-exact copy of the PCM/DSP data sent to Out 1/2, before any output mixing (see "Recording what you hear" below) |

Two of `In 1/2`/`In 3/4 Capture Source`'s selectable items, `Loopback
3/4` and `Loopback 1/2`, are a **different mechanism** from capture
5/6 above, easily confused with it:

| | mechanism | lands on | includes Direct |
|---|---|---|---|
| Capture 5/6 | digital, bit-exact tap of the PCM/DSP data sent to Out 1/2 | channels 5/6, always | no - before output mixing |
| `Loopback 3/4`/`Loopback 1/2` | analog, the *other* output pair's own analog output | channels 1-4, whichever pair selects it | yes |

Set `In 1/2 Capture Source = Loopback 3/4` and channels 1/2 capture Out
3/4's actual analog output (hardware-confirmed, 2026-10-19 - see
`analysis/loopback-and-monitoring.md` for the full test); `In 3/4
Capture Source = Loopback 1/2` is the mirror for Out 1/2 into In 3/4
(presumed the same way by the identical vendor naming, not
independently tested). Both run roughly 0.3-0.8 dB lower than the
actual output and drop to the analog noise floor (~-85 dBFS) when that
output is silent - not a digital copy.

**Feedback loop warning (software monitoring)**: monitoring a track
whose input is a Loopback source back to that same output is a
feedback loop - disable that track's input monitoring. The panel
shows a live reminder in that input's own area whenever a Loopback
source is actually selected.

**Feedback loop warning (analog, hardware-confirmed 2026-10-19)**: a
*different*, purely analog loop exists if you select **both**
Loopback sources **and** turn Direct on for **both** outputs at the
same time - In 1/2 -> Direct -> Out 1/2 -> `Loopback 1/2` -> In 3/4 ->
Direct -> Out 3/4 -> `Loopback 3/4` -> In 1/2, closed entirely inside
the card, no DAW/software monitoring involved at all. Measured: both
inputs rise to about -68.7 dBFS (vs. the ~-85 dBFS floor with the loop
open) - only noise at unity gain, but it can ring/oscillate if gain
goes up anywhere in that loop. Avoid it by turning off Direct on at
least one of the two outputs, or by using only one Loopback source at
a time. The panel switches both live indicators to an amber warning
the moment all four conditions are met, and back the moment any one
of them isn't. See `analysis/loopback-and-monitoring.md` for the full
measurement.

**In PipeWire**, ports are named generically by channel index
(`capture_AUX0`..`AUX5`, `playback_AUX0`..`AUX3` - see "PipeWire /
JACK" above), not by this layout, and the WirePlumber config in this
repo has no way to rename them - the same underlying limitation as
capture 5/6 having no ALSA control of its own, tracked in
`analysis/loopback-and-monitoring.md`, "Proposed exposure".

## Front inputs (Mic / Hi-Z / Line)

With `In 1/2 Capture Source = Mic-HiZ-Line`: Ch.1 is the front combo
socket (XLR + 6.3mm jack, with its own gain knob and +48V) and lands on
capture channel 1 (left); Ch.2 is the front Hi-Z/Line jack (own gain
knob) and lands on channel 2 (right) - both mono. The side switches pick
Line/Mic for Ch.1 and Line/HiZ for Ch.2.

`Direct` on Out 1/2 keeps them separate (mic left, Hi-Z/Line right);
`Direct Mono` (only selectable with this source, and only while Direct
is on) sums both channels to both sides instead.

**48V is only on the Ch.1 XLR pin** - never on the Ch.2 jack, and never
on Ch.1's own jack half. The Ch.1 switch actually selects which
*connector* is live, not just a gain stage: with the switch on Mic, a
jack plugged into the combo gets nothing (only preamp noise) - switch it
to Line instead.

All gain (and 48V) is set on the unit itself, before the ADC - neither
the driver nor the panel can read or control it. The Clip LED lighting
up corresponds to 0dBFS at the ADC. Typical settings: condenser mic ->
48V + Mic switch, gain low and work up; passive bass/guitar -> HiZ;
synth or other line-level gear -> Line. Measured gain ranges and raw
numbers are in `analysis/NOTES.md`, "Front inputs: Mic / Hi-Z / Line".

## Recording what you hear

If PipeWire has the device (the normal case), record from the card's own
PipeWire monitor source - no special setup:

```sh
pactl list sources short | grep monitor      # find "...PhaseX64...monitor"
```

If REAPER (or another app) has the device directly instead, and it's
**Out 1/2** you want to record: capture channels 5/6 are always present in
the normal 6-channel capture stream and are a bit-exact digital copy of
whatever's being sent to Out 1/2 - no cable, no extra routing. In REAPER,
add input channels 5/6 to a track, arm, record. Note that 5/6 reflects
what's *sent* to Out 1/2, not necessarily what's currently audible there -
it keeps capturing even if `PCM 1/2 Playback Switch` is off. For Out 3/4,
or for exclusive-access setups where a hardware loopback is specifically
needed, see `../analysis/loopback-and-monitoring.md`.

## Persisting settings

Routing/source switches, RIAA, and Clock Source are restored automatically:
`alsa-restore.service` runs `alsactl restore` when the device appears
(after the driver's own startup defaults), and runs `alsactl store`
automatically on a clean shutdown or reboot - no manual step needed in
the normal case. Live/read-only state - Digital Input Lock, Digital Input
Rate, and the four peak meters - is never restored; it always reflects
the hardware directly.

If the machine crashes or loses power before a clean shutdown, whatever
you changed since the last store is lost. Run `sudo alsactl store` after
changing settings if you want to be sure they survive that case.

The panel's presets (Presets menu) are separate: named, on-demand
configurations you save and load yourself, not a boot-time mechanism -
they don't replace `alsactl store`/`restore`.

## If something doesn't work

Send the output of `dmesg` after plugging in and after a playback or
capture attempt.

An occasional `status read failed: bRequest=0x49 ... err=-32` in `dmesg`
is expected and harmless - see `../analysis/NOTES.md`, "0x49 status-read
stalls". Since v0.1.13 this message no longer appears at error level
during normal use; it's only visible with dynamic debug enabled on
`snd_usb_phasex64`. It logs against the USB device, not the ALSA card
name, so `dmesg | grep -i phasex64` alone won't find it - grep for
`dmesg | grep -iE "status read|0x49"` instead.

## Licence

The driver, the control panel and everything else in this repository are
licensed under the GNU General Public License, version 2 only
(`GPL-2.0-only`) - see `../LICENSE`.
