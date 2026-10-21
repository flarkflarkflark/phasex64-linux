# PHASE X64 USB – reverse engineering notes

## Device
- USB 0A4A:5B01 (vendor 0x0A4A = Ploytec / Musonik), bcdDevice 1.00, USB 2.0 high-speed, self powered
- Driver 2.9.34 (2011-11-28), generic Ploytec USB audio driver ("usb-audio.de")
- All interfaces are vendor specific (class 0xFF) -> `snd-usb-audio` cannot bind
- Codec(s): 2x Wolfson WM8776 (registry keys `WM8776_1`, `WM8776_2`)
- 4 in / 4 out, 44.1 / 48 / 88.2 / 96 kHz, MIDI in/out, S/PDIF coax out

## Hardware I/O (physical panels)

**Front**: Mic/Line XLR-combo jack (Ch.1) with its own gain knob and a 48V
LED; HiZ/Line 1/4" jack (Ch.2) with its own gain knob; headphone jack with
volume knob.

**Rear**: Power (9V DC barrel), USB-B, MIDI Out/In (5-pin DIN), Digital
(S/PDIF coax) Out/In, Outputs Ch.3/4 (RCA), Inserts (MIC/Line + HiZ/Line,
1/4" jacks), Outputs Ch.1/2 (1/4" jacks), Phono/Aux RCA inputs for both
Ch.1/2 and Ch.3/4.

**Side switches — hardware only, not USB/software controllable**:
`48V` off/on, `Ch.1` Line/Mic, `Ch.2` Line/HiZ, `Phones Ch.` 1/2 | 3/4,
`Ch.1/2` Aux/Phono, `Ch.3/4` Aux/Phono. These sit *upstream* of the WM8776
ADC mux: they pick which physical jack pair is live and how its preamp
stage is conditioned (phono input loading/gain vs. line-level Aux, mic vs.
line gain staging), but they do **not** provide the Windows RIAA curve and do
not by themselves route anything into the
ADC (consistent with the existing Windows/Linux RIAA match using the *full*
software curve against a switch that was on `Phono` throughout both - see
"Windows capture DSP: RIAA" below, and "Hardware `Ch.1/2 Aux/Phono` switch:
measured effect" for an inconclusive attempt to measure this directly) —
that's the separate, software-controlled ADC mux (`WM_ADC_MUX`,
see below), exposed as the `In 1/2 Capture Source` / `In 3/4 Capture
Source` ALSA enum controls since v0.1.4 (ADC-mux-selection-via-switches
was removed, see "ALSA control names" below). The two are cascaded and
independently settable: e.g. the hardware `Ch.1/2` switch set to `Aux`
while `In 1/2 Capture Source` is set to `Phono` gets the wrong physical
conditioning/source (and Windows would also enable its software RIAA), not an
error — software and hardware must be kept
in agreement by whoever is operating the device, since the driver has no
way to read the physical switch position back (no status bit for it
found so far, see "Control requests" below). This is exactly why these
are modelled as an enum rather than independent switches: the hardware
switch already makes the choice exclusive, so the software side should
too, instead of allowing a software state (e.g. two source bits both
"on") that can never correspond to anything the hardware can actually
do.

Capture channels 5/6 have no corresponding entry in the ADC mux at all
(no capture-source control exists for them) - consistent with them
being a fixed digital (S/PDIF) input path that bypasses the WM8776s
entirely, always listening to the rear Digital In jack, no source
selection needed.

## Interfaces / endpoints (full dump: `usb_descriptors.txt`)
Driver config: InChannels=6, OutChannels=4, 24 bit (registry `Parameters`).
PHASE X64 runs in the driver's "MIXED PROCESSING ISOC OUT and BULK IN" mode
(product list in FUN_f1012b90; pipe setup in the select-configuration function):

| Intf | Alt | EP   | Type     | MaxPkt | Interval | Role (from code)                          |
|------|-----|------|----------|--------|----------|-------------------------------------------|
| 0    | 1   | 0x02 | Isoc OUT | 156    | 1        | playback audio (pipe +0x870)              |
| 0    | 1   | 0x83 | Bulk IN  | 512    | -        | MIDI in (not an audio pipe)               |
| 0    | 1   | 0x04 | Bulk OUT | 512    | -        | MIDI out (not an audio pipe)              |
| 1    | 1   | 0x81 | Isoc IN  | 64     | 4        | playback feedback (pipe +0x858)           |
| 1    | 1   | 0x86 | Bulk IN  | 512    | 1        | capture audio (pipe +0x840)               |

### Playback (EP 0x02)
Plain PCM (the bit-interleave encoder is only used with a bulk OUT audio pipe, which this
device does not use). Sample conversion FUN_f1058ec0 = S32 -> S24_3LE, so the wire format is
**S24_3LE, 4 channels interleaved**, 12 bytes/frame; 156 = 13 frames = 12 @96k + 1 slack.
High-speed frame pattern (FUN_f101c540): rate/8000 frames per microframe, fractional
distribution (44.1k: 5/6), corrected by feedback from EP 0x81.

### Feedback (EP 0x81, every 1 ms) – FUN_f101d070
Byte 0 of each packet = number of frames the device consumed in that millisecond
(valid range rate/1000 - 2 .. +2). The driver applies one report per 8 output microframes.

### Stream start (mixed mode, FUN_f100f1e0 + FUN_f1005da0)
1. SET_CUR rate on EP 0x86, then on EP 0x02 (3 bytes LE)
2. read status 0x49, set bit4 (0x10) and bit5 (0x20, "small buffer" mode required by the
   6-channel decoder), write back if changed (driver waits 40 ms after a status change)
3. start feedback (0x81), capture (0x86) and playback (0x02) transfers

### MIDI
- Out (bulk, default FUN_f10027b0): 9-byte packets: bytes 0..3 MIDI data port A,
  bytes 4..7 port B, padding byte 0xFD; byte 8 = control byte (default 0xE0).
  Bytes per packet come from a rate table (~3 bytes/ms, MIDI wire speed).
- In (bulk, FUN_f1007990): MIDI bytes, 0xFD bytes are filler and must be dropped.
  `FUN_f1007990` strips 0xFD across the *entire* actual_length it's given (no
  stride/position logic, no byte-8 exclusion) - confirmed by reading the
  decomp loop directly (`for local_14 < param_2`, `param_2` = the raw read
  length).
- MIDI is gated by a host-side flag at decomp offset `+0x1ca8`
  (`FUN_f1002f00`/`FUN_f10027b0`/`FUN_f1002970` all check it), set only by
  `FUN_f1001c00` - called from the stream-start function (logs `"USB: start
  stream"`) - and cleared by `FUN_f1002370` at stream stop. Windows also
  keeps 5 MIDI-in read requests outstanding at once (`FUN_f1002f00` called
  5x at arm), not 1.

#### Linux implementation (snd-usb-phasex64, settled 2026-10-16)
Confirmed on hardware across `tests/hw/REPORT.md`'s "MIDI loopback
diagnosis" rounds 1-3 and the `midi_stress.py` run - full logs/evidence
there, summary here:
- **Hardware device-gate result, repeatedly confirmed (not a MIDI-
  availability API):** the 0.1.11.5 hardware tests below produced MIDI only
  while a PCM stream was active, matching the Windows gate above - and the
  reload test below re-confirmed it with a real external controller too,
  "gated live by stream state": MIDI stops the instant the stream stops, the
  device does not stay armed. **"Port open" and "data flowing" are two
  different things, and only the first is PCM-independent:** the ALSA
  rawmidi/seq software layer genuinely has no PCM dependency - confirmed by
  reading every `px_midi_in_open/close/trigger`/`px_midi_out_open/close/
  trigger` function in `linux/snd-usb-phasex64/phasex64.c` (lines 4301-4393):
  none of them reference `px->running` or any PCM state: `px_midi_in_open()`
  ([phasex64.c:4301](../linux/snd-usb-phasex64/phasex64.c#L4301)) submits its
  own dedicated MIDI URBs unconditionally - so PipeWire/WirePlumber's own
  rawmidi-keep-open behavior (`session.suspend-timeout-seconds=0`) really
  does keep the ALSA-seq *port* visible/subscribable regardless of PCM
  state. But that software-level fact says nothing about whether the
  *hardware* actually relays any bytes while idle - the repeated,
  hardware-confirmed gate above says it doesn't, and nothing in this driver
  overrides or works around that (no software gate exists to remove -
  the gate observed on the wire is the device's own firmware behavior,
  upstream of anything this driver's code controls). Treat MIDI as
  software-available-but-practically-silent whenever no PCM stream is
  running, not as "the bridge keeps it working." Two backlogged,
  design-only ideas recorded from the historical test setup, neither
  attempted:
  - A "light arm" that reproduces only part of the stream-start sequence
    (candidate: the `STATUS_STREAM` write alone) to wake the MIDI engine
    without a full PCM stream.
  - **"MIDI-only stream"** (2026-10-16, backlog, design only): since the
    gate is tied to streaming state rather than MIDI itself, the driver
    could keep the *capture* URB machinery running internally whenever
    `rawmidi` is open and no real PCM capture/playback substream is
    active - i.e. synthesize an internal capture stream (discarding its
    audio output) purely to keep the device's MIDI engine awake, started
    on rawmidi open and stopped on rawmidi close (or when a real PCM
    stream takes over). Would need to share state cleanly with the real
    PCM open/close/start/stop paths (two independent "keep streaming"
    reasons converging on the same URB lifecycle) rather than conflict
    with them - closer in shape to how `px_cap_watchdog_work`/
    `live_apply_work` already coordinate with `px->running`
    (`fix/watchdog-deadlock`, `design/v0.1.12`) than a one-off special
    case. Not scoped or attempted; noted here since it's a more complete
    answer than the light-arm idea (keeps MIDI working indefinitely, not
    just long enough for one `STATUS_STREAM` write), at the cost of
    running USB capture traffic the user didn't ask for.
- Each 9-byte IN frame is **8 data bytes + 1 trailing control byte**
  (observed `0x00` on idle hardware). The control byte is never forwarded,
  only logged - Windows does *not* exclude it (see above), so this is a
  Linux-only improvement past Windows parity, not a protocol port.
- A data byte (bit 7 clear) received before the first status byte since the
  MIDI-in substream was last *opened* is dropped (`px_midi_in_filter_frame()`,
  `linux/snd-usb-phasex64/midi_in_filter.h`) - orphaned data with no status
  is meaningless, and is exactly what idle/junk IN frames contain. This
  "seen a status byte" state must be reset once per **open**, not per
  `.trigger()` call: the rawmidi core calls `.trigger(up=1)` far more often
  than once per open (measured ~3x per delivered frame) - tying the reset to
  trigger instead broke running-status across packet boundaries.
- **5 MIDI-in read URBs** are kept outstanding at once, matching Windows;
  each resubmits only itself on completion. This measurably fixed a sporadic
  1-2 byte loss at the start of a burst that the single-URB version showed:
  `tests/hw/midi_stress.py` against `0.1.11.4` (5 URBs) - 100/100 exact
  matches (Note On/Off and SysEx, N=50 each), 0.0% loss.
- **Validated with a real external MIDI device** (2026-10-16, `0.1.11.5`):
  Akai MPK mini Plus, DIN MIDI Out -> PHASE X64 MIDI In. ShowMIDI showed
  identical events on `"PHASE X64 USB MIDI"` and on the MPK's own USB MIDI
  port (ch10 pad note-on + poly aftertouch, ch1 keys) - MIDI IN confirmed
  correct end-to-end with a real controller, not just the earlier
  self-loopback tests. Reload test re-confirmed the stream-gating behavior
  above with a real device too: with no PCM stream, nothing arrives; a 2s
  `arecord` lets MIDI through for exactly those 2 seconds and it stops the
  moment the stream stops - the device does not stay armed, it is gated
  live by stream state, matching the Windows gate's design (not just its
  presence at stream start). Unrelated hardware note for anyone repeating
  this test: the MPK mini Plus only forwards its keys to its DIN Out via
  its own second port (its "MIDI 2", e.g. `aconnect 32:0 32:1` on that
  controller) - a quirk of that controller, not this driver.

#### Panel MIDI monitor (2026-10-19)
- The panel treats PCM and MIDI as independent for *software* purposes: it
  never calls or interprets `Mixer.stream_active()` to decide whether either
  MIDI LED is enabled or whether its monitor may run - port availability and
  LED/monitor function genuinely don't depend on PCM state (see the hardware
  device-gate note above). This is not the same claim as "MIDI works with no
  stream" - it doesn't, per that same repeatedly-confirmed hardware gate - so
  the monitor window shows an informational hint ("MIDI only flows while an
  audio stream is active") whenever `stream_active()` reports no PCM stream,
  purely to set that expectation; nothing about the LEDs, the button, or
  whether the monitor can be opened is gated by it.
- IN/OUT activity comes only from the `Rx bytes` / `Tx bytes` counters in
  `/proc/asound/cardN/midiN`, read on the fast ~50ms "peaks" poll (moved there
  from the slow ~1s "state" poll on review - a plain /proc read, trivial next
  to the per-peak-name amixer subprocess calls that poll already makes; the
  LED hold needs sub-second sampling to read as a steady glow under
  continuous traffic like a MIDI clock, not a ~1Hz flicker). Counter
  increases light the corresponding LED briefly; unchanged counters mean only
  that no bytes moved during that interval, not that MIDI is inactive.
- MIDI IN messages come from `stdbuf -oL aseqdump`. The PHASE ALSA-seq address
  is rediscovered from `aseqdump -l` instead of being fixed to a client number,
  and the UI distinguishes a missing port (disconnected) from a missing tool or
  failed subscription (unavailable). OUT deliberately has activity indication
  only, not a message log.
- The worker queue is capped at 256 messages, Tk consumes at most 64 per UI tick
  in one batch, and the visible history retains the newest 200 lines. The owned
  `aseqdump` child receives SIGINT and is reaped when the port changes or the
  panel closes.
- Channel numbers are shown as 1-16 (aseqdump's own -R/--raw-view prints
  0-15). Clock/Active Sensing are filtered by default (a running clock alone
  is 48 messages/second) with a toggle to show them.

#### Panel MIDI monitor: hardware acceptance (2026-10-19, DIN loopback cable, REAPER via PipeWire Midi-Bridge)
- **LEDs independent of the monitor window, confirmed**: IN/OUT both blink
  with traffic while the window is closed. Cable removed: OUT stays lit
  (REAPER's own clock output, 48 msg/s, has nowhere to go but is still
  transmitted) while IN goes dark - confirms the two LEDs are driven by
  genuinely separate Tx/Rx counters, not one shared "any activity" signal.
- **Monitor window**: channel numbers read 1-16 correctly; Clock hidden by
  default, appears once the toggle is switched on; an 18-byte SysEx appeared
  as one message (not split across lines) - aseqdump does print it on one
  line, as expected, not something this panel needed to reassemble; the "OUT
  shows activity only" hint is visible.
- **Process lifecycle** (`pgrep -c aseqdump` sampled every 0.3s throughout):
  never more than 1 process at any sampled instant; repeated clicks on
  **MIDI…** while the window is already open never start a second one (same
  PID throughout); **CLOSE** -> 0; reopening -> exactly 1, a fresh process;
  closing the whole panel with the monitor still open -> 0. All of the
  lifecycle guarantees from the design held up on real hardware, not just
  in the subprocess-backed tests.
- **Found on this pass, fixed separately** (see the commit introducing this
  note): long lines (the SysEx dump) were truncated at the window edge
  instead of wrapping; the status bar's `MIDI:` field was reading the
  monitor *window's* connection state instead of the underlying port/device
  state, so it misleadingly read "closed" whenever the window itself was
  closed even though MIDI was working fine; ALSA system announce events
  (other clients' own port/client start/exit/subscribe traffic, source
  client 0) were leaking into the monitor's history.

#### Panel MIDI monitor: fix verification + OUT gotcha (2026-10-20)
- **Wrap fix verified on hardware**: the same 18-byte SysEx that used to
  show truncated ("... 0D") now appears in full, wrapped onto a
  continuation line ("0E 0F F7") rather than cut off.
- **Status bar verified independent of the monitor window**: `MIDI:
  available` shown with the monitor window closed.
- **Gotcha found: `aseqsend` to the PHASE port only reaches the hardware
  while some other client holds the rawmidi *output* open.** ALSA-seq
  `aseqsend` delivers to the sequencer port regardless of whether
  anything is reading from the far end of the rawmidi bridge that
  backs it - with no `Owner PID` under `Output 0` in
  `/proc/asound/cardN/midi0` (nothing has the rawmidi output device
  open), the bridge has nowhere to forward the event, the `Tx` counter
  doesn't move, and the message is silently dropped. This is ALSA
  sequencer/rawmidi-bridge behaviour, not a bug in this driver -
  nothing here controls whether the bridge has a reader. Observed:
  `Tx` read 24469 both before and after an `aseqsend` with no owner on
  `Output 0`; starting REAPER (which opens the device via PipeWire's
  Midi-Bridge) and repeating the identical `aseqsend` delivered it
  intact. Practical takeaway: an app needs to actually have the device
  open for *outgoing* ALSA-seq traffic to go anywhere, same as any
  other ALSA rawmidi output - this panel's own MIDI OUT LED is
  unaffected either way, since it only ever reports the `Tx` counter's
  own movement (or lack of it) accurately, whatever the cause.

### Capture (EP 0x86) – Ploytec bit-serial format (decoder FUN_f104f890, 6 ch)
One 512-byte bulk packet = 8 frames of 64 bytes:
- bytes  0..23: bit i (MSB first, i=23..0) -> byte bit0 = ch0, bit1 = ch2, bit2 = ch4
- bytes 24..31: padding
- bytes 32..55: same for ch1 (bit0), ch3 (bit1), ch5 (bit2)
- bytes 56..63: padding
Result is 24 bit per channel, driver shifts left by 8 (S32 container).

Independently confirmed against Ozzy (github.com/mischa85/Ozzy, MIT,
Copyright (C) 2024 Marcel Bierling): `ploytec_decode_frame_6ch` in
`common/devices/ploytec/ploytec_codec.c` uses the same byte ranges (0x00-0x17
and 0x20-0x37, 0x18-0x1F and 0x38-0x3F unused) and the same bit-to-channel
mapping (bit 0/1/2 of the first block = ch 1/3/5, of the second block =
ch 2/4/6, MSB first: src[0x00] -> bit 23). Only the output container differs
(Ozzy 3 bytes per sample, `px_decode_frame()` S32 shifted left by 8).

(For reference, the bulk-OUT encoder FUN_f1055b70, 4 ch: 48 bytes/frame, bytes 0..23 carry
ch0 in bit0 and ch2 in bit1 MSB first, bytes 24..47 carry ch1/ch3. Not used by this device.)

## Driver binaries (Windows driver 2.9.34)
Reverse-engineered from the official Windows driver package. The binaries,
and everything derived from them, were decompiled locally with Ghidra (not
distributed): references below such as `msnkphsu.sys.c:6395` or
`CPLPhaseX64USB.exe.c:19698` are line numbers in that local Ghidra output,
and `FUN_...` names are Ghidra's. Only the findings are published here - see
`analysis/README.md`.
- `msnkphsu.sys` – USB core (all protocol logic), x64
- `msnkphsa.sys` – PortCls WDM audio miniport (talks to msnkphsu, no USB)
- `msnkphsm.sys` – PortCls MIDI miniport

## Control requests (msnkphsu.sys)
`sub_25e50` = deviceRequest(dev, dir, type, recipient, bRequest, wValue, wIndex, buf, *len, ..., retries)
- type 0x40 = vendor, 0x20 = class; recipient 0 = device, 1 = interface, 2 = endpoint

| Dir | Type   | Rcpt | bRequest | wValue        | wIndex | Len | Meaning                                    |
|-----|--------|------|----------|---------------|--------|-----|--------------------------------------------|
| IN  | vendor | dev  | 0x49     | 0             | 0      | 1   | read status byte                           |
| OUT | vendor | dev  | 0x49     | status byte   | 0      | 0   | write status byte (see below)              |
| IN  | vendor | dev  | 0x56     | 0             | 0      | 3/5/15 | firmware/version info (other products)  |
| OUT | vendor | dev  | 0x41     | (reg<<9)\|(val&0x1FF) | 0x0102 / 0x0106 | 0 | WM8776 register write, codec 0 / codec 1 |
| OUT | class  | ep   | 0x01 SET_CUR | 0x0100    | 0x86, then 0x02 | 3 | set sample rate, 24-bit LE (FUN_f1025b10) |
| IN  | class  | ep   | 0x81 GET_CUR | 0x0100    | 0x86   | 3   | read back sample rate (FUN_f1025960)       |
| IN  | class  | ep   | 0x81 GET_CUR | 0x0100    | 0      | 3   | digital input rate; 192000 = not locked    |

**On our hardware (0a4a:5b01), `GET_CUR`/`SAMPLING_FREQ` is a dead
stub**, confirmed empirically (`rate_probe=1`, v0.1.10-pre5, see
`tests/hw/REPORT.md` "GET_CUR is a dead stub"): `wIndex=0`,
`wIndex=0x86` (`EP_CAPTURE`) and `wIndex=0x02` (`EP_PLAY`) all return
identical raw bytes `00 7d 00` (32000 decoded), at every stage of
stream start, at both 44100 and 48000 Hz, regardless of any preceding
`SET_CUR`. Never observed as `192000` (the documented "not locked"
sentinel for `wIndex=0`) either. Whatever this control does on the
decomp's source model(s), it does not return a live value on this
device/firmware - don't use it as a readback/verify mechanism here.

### Status byte (vendor 0x49) – FUN_f10131e0 / FUN_f1005a00
- bit0: mirrors settings field +8 ("USB1Mode" in registry), default 0
- bit1: 1 = internal clock (ClockSource 0), 0 = sync to digital input
- bit2: digital input present/locked (read only) -> driver measures S/PDIF rate via GET_CUR
- bit4: always set when streaming starts
- bit5: set for buffer setting <= 16 -> selects the bulk-in mode the 6 ch decoder needs
- driver writes 0x49 only when the value differs, then waits 40 ms
- bits 3, 6, 7: unknown, not yet exercised by anything we've observed

### Digital output source (`DigitalOutMode` / `DigitalOutSelector`) — inconclusive for this model
The generic driver's settings-persistence function (`FUN_f103a550` load /
`FUN_f103a5f0` save, around `msnkphsu.sys.c:25637-25669`) reads/writes
`DigitalOutMode` and `DigitalOutSelector` from the registry in the *same*
struct and the *same* calls as `ClockSource` and `USB1Mode` — the two
settings we already know map to status-byte bits 1 and 0 for this exact
device. That's suggestive that a digital-out source/mode concept exists
at the protocol level. Separately, a per-model UI capability table
(`msnkphsu.sys.c:~23150-23560`) builds dropdown labels like `"S/PDIF"`,
`"Optical"`, `"Monitor"`, `"Analog"` gated on specific VID:PID pairs —
but grepping that table for an exact `(param_2==0xa4a) && (param_3==0x5b01)`
match (our PHASE X64) found nothing; every "S/PDIF"/"Monitor" label match
there belongs to some *other* Ploytec-based product this shared generic
driver also supports. So: **we have not confirmed `DigitalOutMode`/
`DigitalOutSelector` do anything on 0A4A:5B01 specifically** - this may be
generic-driver scaffolding that's inert on this model, where the digital
output is simply hardwired to always mirror playback channels 1/2 with no
host-side configuration at all. Resolving this for real would need a USB
trace of the real Windows control panel (if it even exposes a matching
option for this model) toggling something and seeing a status byte / bit
3/6/7 or a new vendor request appear - not done. The Linux driver
currently sends nothing related to this and doesn't need to: confirmed by
hardware test (see `tests/hw/REPORT.md`, channel map v0.1.3) that
playback 1/2 reaches capture 5/6 via Digital Out -> (user's own coax
cable) -> Digital In with zero driver action, consistent with an
always-on fixed mirror.

### WM8776 codecs
Factory defaults written by the driver for 0A4A:5B01 (FUN_f10168d0):
codec0: R7=0x90, R21=0x01, R22=0x01 – codec1: R7=0x90, R21=0x04, R22=0x01
Current values on this unit (registry `WM8776_1/2`):
codec0: R7=0x90, R21=0x02, R22=0x01 – codec1: R7=0x90, R21=0xC0, R22=0x00

## Control panel -> hardware mapping (CPLPhaseX64USB.exe FUN_14004cee0)
The panel only uses driver generic request 0x26 (WM8776 get) / 0x27 (WM8776 set),
IOCTL 0x2200D0, payload {u32 cmd; u16 codec; u16 reg; u16 value}. Button n in script.txt
is index n-1 in the code.

| Button (script.txt) | Codec | Reg | Bit / value |
|---|---|---|---|
| BTN1 In 1/2 Mic-HiZ-Line | 0 | R21 | 0x01 |
| BTN2 In 1/2 Aux | 0 | R21 | 0x04 |
| BTN3 In 1/2 Phono (exclusive) | 0 | R21 | 0x02 |
| BTN4 Out 3/4 -> In 1/2 | 0 | R21 | 0x10 |
| BTN5 Sound module -> In 1/2 | 0 | R21 | 0x08 |
| (no input selected) | 0 | R21 | 0xC0 (both ADC channels muted) |
| BTN6 Direct 1/2 | 0 | R22 | 0x04 (bypass) |
| BTN7 Direct 1/2 mono (needs Mic + Direct) | 0 | R22 | 0x02 |
| BTN30 Playback 1/2 | 0 | R22 | 0x01 (DAC) |
| BTN27 Playback 1/2 mono | 0 | R7 | 0x90 off / 0xF0 on |
| BTN8 In 3/4 Aux | 1 | R21 | 0x04 |
| BTN9 In 3/4 Phono (exclusive) | 1 | R21 | 0x02 |
| BTN10 Out 1/2 -> In 3/4 | 1 | R21 | 0x10 |
| BTN11 Sound module -> In 3/4 | 1 | R21 | 0x08 |
| (no input selected) | 1 | R21 | 0xC0 |
| BTN12 Direct 3/4 | 1 | R22 | 0x04 |
| BTN13 Sound module -> Out 3/4 | 1 | R22 | 0x02 |
| BTN31 Playback 3/4 | 1 | R22 | 0x01 |
| BTN28 Playback 3/4 mono | 1 | R7 | 0x90 off / 0xF0 on |
| BTN14..19 latency | – | – | host buffer size only |
| BTN21..24 sample rate | – | – | indicator only |

## ALSA control names (snd-usb-phasex64)

v0.1.0-0.1.3 exposed R21 (WM_ADC_MUX) as 9 independent boolean switches
(5 for In 1/2, 4 for In 3/4), one per source bit, with phono-exclusivity
and all-zero-auto-mute enforced ad hoc in `px_switch_put()`. The Windows
panel groups these as a `CDlgGroupRadioButtonIterator` radio set
(`CPLPhaseX64USB.exe.c:19698-20010`) - i.e. this was never really 9
independent choices, it's a single-select source per pair that the
switch model made no structural guarantee of. v0.1.4 replaces them with
two `SNDRV_CTL_ELEM_TYPE_ENUMERATED` controls, matching the Windows UI
shape and the hardware (phono really does hardware-exclude the rest):

| Old (v0.1.0-0.1.3) | New (v0.1.4+) |
|---|---|
| `In 1/2 Mic-HiZ-Line Capture Switch` | `In 1/2 Capture Source` = `Mic-HiZ-Line` |
| `In 1/2 Phono Capture Switch` | `In 1/2 Capture Source` = `Phono` |
| `In 1/2 Aux Capture Switch` | `In 1/2 Capture Source` = `Aux` |
| `In 1/2 Loopback Out 3/4 Capture Switch` | `In 1/2 Capture Source` = `Loopback 3/4` |
| `In 1/2 Sound Module Capture Switch` | `In 1/2 Capture Source` = `Sound Module` |
| (all-zero register, reached via auto-mute) | `In 1/2 Capture Source` = `Mute` (now explicit, selectable) |
| `In 3/4 Phono Capture Switch` | `In 3/4 Capture Source` = `Phono` |
| `In 3/4 Aux Capture Switch` | `In 3/4 Capture Source` = `Aux` |
| `In 3/4 Loopback Out 1/2 Capture Switch` | `In 3/4 Capture Source` = `Loopback 1/2` |
| `In 3/4 Sound Module Capture Switch` | `In 3/4 Capture Source` = `Sound Module` |
| (all-zero register, reached via auto-mute) | `In 3/4 Capture Source` = `Mute` (now explicit, selectable) |

Unchanged (R22/R7 output routing is genuinely independent - several can
be on at once, e.g. Direct *and* PCM both feeding the same output - so
these stay booleans): `Direct 1/2 Playback Switch`, `Direct 1/2 Mono
Playback Switch`, `PCM 1/2 Playback Switch`, `PCM 1/2 Mono Playback
Switch`, `Direct 3/4 Playback Switch`, `Sound Module 3/4 Playback
Switch`, `PCM 3/4 Playback Switch`, `PCM 3/4 Mono Playback Switch`,
`Clock Source`, `Digital Input Lock`.

v0.1.10 adds `Digital Input Rate` (read-only integer, Hz): the device's
actual current rate via `GET_CUR(wIndex=0)`, for the panel to show
alongside `Digital Input Lock` - see `tests/hw/REPORT.md` "v0.1.10".

Old `/var/lib/alsa/asound.state` entries referencing the removed switch
names are now stale (harmless, alsactl just won't find them) - see
`linux/README.md`.

## Control panel (CPLPhaseX64USB.exe)
Skinned generic Ploytec panel, layout in `data/script.txt`, help text in `data/LANG_EN.TXT`.
Controls: input select (Mic/HiZ/Line, Aux, Phono) per pair, loopback (Out3/4->In1/2,
Out1/2->In3/4), DSP/sound module routing, Direct monitoring, mono, playback enable,
latency (6 steps), sample rate indicator. Settings stored under
`HKLM\SYSTEM\CurrentControlSet\Services\MUSONIK_PHASE_X64_USB\Parameters`.
Button mapping: see "Control panel -> hardware mapping" above.

## Linux driver (snd-usb-phasex64) — original crash, root cause

The initial port (v0.1.0) panicked with a NULL pointer dereference in
`px_fill_play_urb` the first time it was opened in REAPER (full duplex:
both playback and capture opened, one direction opened while the other
was already running — completely normal DAW behavior). Two independent
bugs combined to cause this:

1. **Race** (fixed in v0.1.1): `active` and `sub` were written by
   `trigger()`/`close()`/`hw_free()` without a lock the completion
   handlers' read of the same fields was synchronized against, so a
   completion could observe `active == true` with a `sub` that had
   already been nulled out from under it mid-close. Fixed with a
   per-stream spinlock covering both the writers and the completion
   handlers' reads, plus a `sync_stop` PCM op so `hw_free()`/`close()`
   can't free the runtime while a `snd_pcm_period_elapsed()` call for it
   is still in flight.
2. **Logic error** (fixed in v0.1.2, the more likely actual trigger for
   the original crash): `px_pcm_open()` only set `s->sub = sub` when
   `snd_pcm_hw_constraint_single()` returned exactly 0, but that function
   returns *positive* on success whenever it narrows the rate to a single
   value (per its own kernel doc comment) — which is exactly what happens
   every time the second direction is opened while the first is already
   running at a fixed rate. So `sub` silently never got set for whichever
   direction REAPER opened second, while `open()` still reported success;
   the following `trigger(START)` then set `active = true` regardless
   (it doesn't check `sub`), landing in exactly the `active == true,
   sub == NULL` state the completion handler dereferences. This is a
   deterministic logic error, not a race — it didn't need any particular
   timing to trigger, just "open direction B while direction A is
   running," which is the normal case.

Found during a supervised hardware test session (`tests/hw/REPORT.md`):
with bug 1 already fixed, bug 2 on its own produced a `WARN_ON_ONCE`
instead of a panic, confirming the defensive guards from the v0.1.1 fix
work as intended even against a bug they weren't specifically written
for.

## v0.1.4/v0.1.5 — capture stall, investigated and CLOSED unreproduced

Full evidence trail in `tests/hw/REPORT.md`. Summary: during v0.1.4
testing, every `arecord` attempt (standalone, no mixer interaction)
failed after ~550-650ms with `EIO`, `hw_ptr` stuck at 0, **zero** kernel
WARN/error output. 100% reproducible that session, never reproduced
since (including the exact original sequence repeated 10x). Four
specific hypotheses were tested directly against hardware and all four
disproven: (1) udev's `alsactl restore`→`init` fallback landing on
`Mute` for the new enum controls — mechanism confirmed real, but the
actual failing-load `amixer` dump shows normal values, and live
`alsactl init` didn't touch the controls; (2) muting both/either ADC
stopping capture data — disproven, all 4 mute combinations captured
cleanly; (3)/(4) the coax Digital-Out→Digital-In loopback plus a Clock
Source cycle wedging the capture engine, single-cycle and 10x-repeated —
both disproven. No fifth hypothesis invented. v0.1.5 adds permanent
`px_cap_complete()`/`px_status_update()` tracing and a 250ms no-data
watchdog (`dev_warn`, not dynamic-debug gated) for real evidence if it
recurs — no behavioral fix, no root-cause claim.

**Untested hypothesis 5**: the failing session followed a ~9h s2idle
suspend with v0.1.3 loaded (device powered) then an `rmmod`/`insmod`
cycle with no USB device reset; self-healed after several later
reloads. Plausible concrete mechanism: `px_resume()` never calls
`usb_clear_halt()` on any endpoint (neither does `px_probe()`) — a
stalled bulk IN endpoint (`0x86`) surviving a suspend or a reset-less
module reload would be indistinguishable from the observed symptom.
`px_resume()` also never checks `usb_set_interface()`'s return value,
unlike `px_probe()` (fatal there). Neither gap has been fixed — this is
a reviewed-but-unimplemented recommendation, not a confirmed cause.

## v0.1.5 — enum verification session: two new, unrelated, not-root-caused findings

Full evidence in `tests/hw/REPORT.md` ("v0.1.5 — enum verification,
digital-out, panel session"). Summary:

**Capture Source enum routing** confirmed correct and matches the hardware
mapping table above exactly (`Loopback 3/4`/`Loopback 1/2` are the only
items that route signal to ch1/2 or ch3/4 respectively; ch5/6 carry the
always-on digital mirror regardless of ADC mux state, as already known from
v0.1.3). **Decomp confirms** the old v0.1.0-v0.1.3 independent-switch bit
layout could genuinely sum non-phono ADC-mux source bits: the Windows
panel's own button handler (`FUN_14004cee0`) sums the four non-phono
buttons' independent checked-states into one register write with no
cross-exclusivity code between them, relying entirely on native radio-button
widget behavior (not application logic) to prevent that combination from
ever actually being sent.

**New finding A (not root-caused)**: every captured tone through the
digital S/PDIF mirror (ch5/6) or the internal chip-level `Loopback`
ADC-mux route comes back at the **wrong, non-sinusoidal frequency** (e.g.
1000Hz requested measures as a clean harmonic series peaking at 1105Hz) —
confirmed real via source-file sanity check, active-region-only reanalysis,
and full-spectrum harmonic analysis; confirmed independent of dual-tone
crosstalk and of which of the two paths carries it. Directly contradicts
the v0.1.3 channel-map test's exact-1000Hz result for what should be the
same signal path — **correction**: it doesn't actually contradict v0.1.3;
only the external-cable analog path (Clock/rate test) was ever rigorously
certified exact there, and this is the first time either the S/PDIF mirror
or the internal `Loopback` route has been pitch-checked at all.

**Finding A, follow-up (bit-pattern proof)**: replaced the sine with a
per-channel-tagged counter captured raw and diffed sample-by-sample.
Decisive on the S/PDIF mirror (ch5/6): routing exact, 97-98% correct
sequential stepping, ~1-2% showing a specific `+65`-then`-63` pair —
one extra duplicated `CAP_WIRE_FRAME` (64 bytes) inserted periodically
(~3.08ms between occurrences). Checked against this driver's own
32-frame-per-URB capture boundary (`CAP_URB_BYTES`/`CAP_WIRE_FRAME`): the
anomaly's position mod 32 is perfectly uniform, **ruling out a bug in
`px_cap_complete()`/`px_decode_frame()`** — points at either the device's
own internal S/PDIF tap or something on the playback side, not traced
further. The internal `Loopback` paths couldn't be bit-pattern-tested the
same way — both a per-sample ramp and a slow DC staircase failed to survive
the analog D/A->WM8776-mux->A/D round trip recognizably, almost certainly
because any real analog audio path is AC-coupled/anti-alias-filtered by
design, not because of a decode bug (the same uniformly-applied
`px_decode_frame()` is proven correct for both the S/PDIF path here and
the external-cable path in v0.1.3). Sine-based evidence remains the
operative evidence for those two paths.

**New finding B (not root-caused), corrected and expanded**: changing
`Clock Source` *or* reading `Digital Input Lock` while a PCM stream is open
**reliably fails every time**, persistently for as long as streaming
continues (not just right after start — checked at 1s/3s/6s/10s in, same
result) with a real kernel-level `EPIPE` (`status read failed: bRequest=0x49
... err=-32`) on the live `px_status_read()`. The `Digital Input Lock`
result was originally mis-reported as "succeeds" based only on `amixer
cget`'s exit code, which stays 0 even when the read actually failed —
checking stdout/stderr content corrected this. Every OUT-only control
(ADC source, boolean switches) tested fine under the same streaming load.
Handled safely by the driver (no state corruption, clean recovery, no
WARN/hang) but is a real user-facing bug. **Fix plan** (not implemented,
investigation only): for `Clock Source`, use the already-current
`px->last_status_byte` instead of a live read whenever `px->running` (it's
kept accurate by every successful write, including the one in
`px_start_streaming()`); for `Digital Input Lock`, cache the last
successfully *read* status byte separately and serve that while running,
since that control reports live hardware state with no write-side cache to
fall back on. Windows-driver research into how the original driver guards
this was inconclusive — found the generic multi-product status-update
function but couldn't trace the exact runtime clock-toggle IOCTL path
within reasonable effort.

**Also found, resolved**: the external RCA cable (`Out 3/4 -> Aux/Phono
In 3/4`) described in the v0.1.3 session showed no live loop during this
session — `Aux` on `In 3/4 Capture Source` showed no signal on ch3/4, only
the ch5/6 digital mirror. Cause confirmed afterward: the cable had been
borrowed for this session to wire up the coax Digital Out -> Digital In
loopback instead, and has since been reconnected. The `In 3/4 Aux` result
in `tests/hw/REPORT.md`'s enum-verification table is marked invalid there
for this reason — it reflects the cable's absence, not the control.

**Major correction, follow-up session**: the "~1.1x pitch shift" above was
a measurement artifact, not a real pitch shift. With the external cable
reconnected, it shows the exact same symptom under a naive single-FFT-peak
measurement that S/PDIF and the internal `Loopback` paths did — but a
direct, FFT-free zero-crossing check (median cycle length, across all of
external-cable ch3/ch4 and S/PDIF ch5/ch6) shows the *true* signal
frequency is correct on every one of them. What's real: ~39-40% of
individual cycles come out shortened/lengthened, with a strikingly
consistent secondary cluster at exactly 2/3 of the correct cycle length,
nearly identically on both the purely-analog external-cable path and the
purely-digital S/PDIF path — pointing at something upstream of both (the
playback/encode side, or a shared internal tap mechanism), not a per-path
decode bug. Sparse per-sample glitches (confirmed at ~1-2% via the
bit-pattern test) smear spectral energy across a harmonic comb; a naive
peak-pick grabs whichever harmonic is strongest for a given input
frequency, which is why different requested frequencies gave such
inconsistent-looking "measured" results throughout this document — not
because of a real, frequency-dependent pitch shift.

**Live capture-stall reproduction, same follow-up session (unexpected)**:
the exact symptom from the "CLOSED, unreproduced" section above recurred
live, for the first time since that investigation closed — `EIO` on
`arecord`, zero prior warning, after roughly a 2-hour idle-but-powered gap
with no suspend and no module reload. First real diagnostic evidence ever
captured for this symptom: the v0.1.5 watchdog fired three times
(`last urb status=0 actual_length=2048` — the last completion was
entirely normal, completions just stopped). New data point: `IN` status
reads (`Digital Input Lock`, `Clock Source`) succeeded normally *during*
the stall, unlike Finding B above where they fail 100% of the time under
genuine active streaming load — consistent with the capture bulk endpoint
having gone genuinely quiet (possibly stalled/halted at the USB level,
the same unconfirmed mechanism the `px_resume()`/`px_probe()` gap review
already named: neither path calls `usb_clear_halt()`). Not resolved, no
recovery attempted this session. Full detail in `tests/hw/REPORT.md`.

**Follow-up, same stall, before any `rmmod`/`insmod`**: playback confirmed
unaffected (`aplay` works cleanly) — specific to the capture endpoint.
The three captures immediately preceding the stall (before a final,
naturally-ending one) were explicitly killed mid-transfer via `SIGTERM`,
not stopped cleanly — a concrete candidate mechanism. A clean Clock
Source toggle with nothing open (no `EPIPE`) ruled out `px->running`
being stuck true; the next `arecord` still failed immediately even after
that toggle forced a fresh status write, meaning a *complete* normal
restart (fresh rate `SET_CUR`, fresh status byte) doesn't recover it —
points at the endpoint/URB level, not status-byte or rate state.

**v0.1.7 (not loaded)**: `usb_clear_halt()` on all three streaming
endpoints at every stream start (resets the data-toggle bit on both host
and device — the fix for a bulk IN pipe left silent after URBs were
killed mid-transfer). The capture watchdog now self-heals with
escalation instead of firing once: stage 0 clears halt + resubmits
capture URBs, stage 1 (250ms later, if still nothing) cycles `intf1`
through alt 0 -> alt 1 before resubmitting, stage 2 gives up and logs.
All under `px->mutex`, coordinated with `px_stop_streaming()` via the
same lock so a concurrent stop can't race an escalation step. Full
detail in `tests/hw/REPORT.md`.

**Fixed a bug in stage 1 before testing**: `intf1` carries feedback
(`0x81`) alongside capture (`0x86`), not playback (`0x02`, on `intf0`) -
the alt-cycle is interface-wide, so stage 1 was implicitly killing
feedback's URBs without resubmitting them. Now kills/resubmits feedback
right alongside capture.

**`tests/hw/kill_stress.sh`** (50x kill-mid-capture, random 50-900ms,
mostly `SIGTERM`/every 5th `SIGKILL`, then a normal capture must
succeed): **50/50 clean on both v0.1.5 and v0.1.7** - the kill-timing
pattern alone does not reproduce the stall. v0.1.7 also passed a 3s
capture / 3s playback / 10s full-duplex sanity check; all 309
`clear_halt` lines logged across both runs were successes, zero
`capture watchdog` firings (no stall occurred to trigger one).

**Leading hypothesis**: both real stalls this project has seen followed
long device inactivity (~9h suspend, hypothesis 5, never tested; this
session's ~2h idle-but-powered gap) - not kill-timing, which kill-stress
just ruled out as sufficient on its own. If the device's firmware drops
into an idle state after a long gap that needs a fresh `SET_INTERFACE`
to fully wake from for bulk transfers, that explains both occurrences
and why kill-stress (no idle gap between iterations) can't reproduce it.
Watchdog stage 1 already covers this mechanism if it holds up - not
confirmed, flagged as leading, not concluded.

**Idle-test result, directly testing the leading hypothesis above: no
stall.** ~2.5h of the device powered, enumerated, and completely
untouched (confirmed no PM suspend occurred - this tested idle-while-
powered specifically, not suspend/resume) did not reproduce it. A single
capture afterward succeeded on the first attempt, `exit=0`, full
non-truncated file size, zero `capture watchdog`/`stage` lines, only the
3 normal non-escalated `clear_halt` calls every stream start makes. Idle
time alone, like `kill_stress.sh`'s kill-timing alone, is not sufficient
to reproduce this on its own. The two real occurrences remain the only
two data points; hypothesis 5's original scenario (an actual
suspend/resume cycle) is still untested. Full detail in
`tests/hw/REPORT.md`.

**phasex64-panel** bidirectional sync (amixer->panel and panel->amixer, both
switches and enums) verified working against real hardware, plus the
`alsactl monitor`-based live auto-follow mechanism the GUI depends on.

**Automated REAPER test (v0.1.8)**: `tests/hw/reaper_test.lua` drives
REAPER through project setup, a 10s recording, a save, and 5x 2s
record/stop cycles. The correct `reaper.ini` ALSA config
(`linux_audio_mode=1`, not `0` or `3` as web research initially
suggested - caught by checking actual behavior, not trusted) was found
by testing, not guessing. With that config, every attempt hit a REAPER-
side instability: a continuous playback retrigger loop once audio is
active, and a hang on transport Stop specifically after an armed,
actually-recording track (not the retrigger loop itself, which also
happens during a clean pure-playback run). Ruled out buffer size/period
count and custom routing as causes. **The driver stayed fully healthy
throughout every attempt** - 0 watchdog firings, 0 `clear_halt`
failures, 0 WARN/BUG/Oops, clean teardown the instant the hung REAPER
process was killed. Looks like a REAPER-ALSA-backend issue specific to
this device, not a driver bug. Full detail in `tests/hw/REPORT.md`.

## v0.1.8-diag: wire-format investigation -> "capture dead, zero URBs" root cause found

Full evidence trail in `tests/hw/REPORT.md`. Summary of the chain that
led here: a `v0.1.8-diag1` build (hex-dumped the first raw capture URBs,
`CAP_URB_BYTES` temporarily 512) confirmed the 64-byte-frame/512-byte-
block wire layout matches the Windows decomp exactly, and that URB size
(512 vs 2048) does **not** affect the previously-measured ~1.42x
real-time capture pacing deficit. A tagged-counter delta histogram
(`dup_check.py`) then ruled out large-scale frame duplication (0%
`delta=0`) and, per the user's own correction of the initial hypothesis,
pointed at dropped frames instead (duplicates would lengthen cycles and
lower pitch; the measured symptom was correct pitch with shortened
cycles). A follow-up rate-sweep test (44100/48000/88200/96000 with the
diag1 build) never produced a timing ratio at all: every rate failed
immediately (~0.5-0.6s, `EIO`), with the v0.1.8 capture watchdog firing
through its full stage 0 -> stage 1 escalation for the first time with
real data, logging `last written status byte=0x00` - new, every prior
stall observation showed `0x32`/`0x36` here.

**Root cause, confirmed by the user's own fresh-replug reproduction**:
after a real replug (device status register back at its true power-up
default `0x16`), `0.1.8-diag1`'s start sequence logs the expected
`status update: old=0x16 new=0x36 ...`, runs its 3 `usb_clear_halt()`
calls, triggers, and **no capture URB ever completes** - the watchdog's
own escalation can't revive it, `arecord` fails in ~0.6s, 100%
reproducible. The same fresh `0x16` state with `v0.1.5` loaded instead
(no `clear_halt()` calls in its start path) streams fine (at the
already-known ~1.42x shortfall rate). Loading `0.1.8-diag1` again
*without* a replug (device already at `0x36`, so the status write is a
skipped no-op) also streams fine, even though all 3 `clear_halt()`
calls still run - ruling out `clear_halt()` in isolation.

**Diff against `v0.1.5` (commit `682ef29`, checked out in a separate worktree), requested to
find a timing/order difference - found none**: `px_status_write()`,
`px_status_update()` (read-modify-write, 40ms sleep after a real
change) and `px_start_streaming()`'s call order (settle -> `SET_CUR`
capture rate -> `SET_CUR` play rate -> status update -> submit URBs)
are byte-for-byte identical between `v0.1.5` and current `main`. Neither
version calls `usb_set_interface()` from a normal start path (only from
`px_probe()`, `px_resume()`, and watchdog stage 1). The Windows driver's
own decomp (`analysis/NOTES.md`'s existing "Stream start" section)
documents the exact same 3-step order and the same 40ms-after-a-change
rule, and a broad search of the decomp for any clear-halt/reset-pipe
equivalent (`ResetPipe`, `ClearFeature`, `SyncResetPipe`, etc.) found
nothing - the original driver never does this at all. **[Corrected by the
2026-10-14 Windows USB trace: the official driver does an `ABORT_PIPE` +
`SYNC_RESET_PIPE_AND_CLEAR_STALL` on EP 0x86 at every stream (re)start - the
URB-function equivalent of `usb_clear_halt()`. The decomp search missed it
because these are URB function codes, not named imports. See "Windows USB
trace" below.]** **The only
behavioral delta between a working fresh-replug start (`v0.1.5` or
Windows) and a broken one (`v0.1.8-diag1`) is the three `usb_clear_halt()`
calls `v0.1.7` added, issued immediately after the status write's 40ms
settle, right before submitting each endpoint's URBs.** Combined with
the user's A/B matrix above, the fatal ingredient is specifically
*`clear_halt()` issued shortly after a genuine status-register bit
transition on a freshly-powered device* - not the transition alone, and
not `clear_halt()` alone.

**What wrote `last written status byte=0x00`**: nothing explicit -
`px->last_status_byte` is zero at `kzalloc` time and is only ever
assigned by a successful `px_status_write()`. `px_probe()`'s own
`px_status_update(STATUS_INTERNAL_CLOCK, STATUS_INTERNAL_CLOCK)` is a
same-bit no-op (clearing then immediately re-setting one identical bit)
that computes `new == old` whenever that bit is already set - which the
user's own replug test confirms it always is, even at the fresh `0x16`
default - so probe's write is *always* skipped, and the cache starts
every device-fresh epoch at `0x00` until the first real stream start
writes `0x36`. Seeing `0x00` at the rate sweep's watchdog firing proves
that attempt was the first stream start since this device was last in a
genuinely fresh state - i.e. the rate sweep's first attempt silently
re-hit the exact fresh-replug scenario above, mid-session, with no
`rmmod`/`insmod` and no explicit replug noticed. **Not resolved**: what
put the device back into that fresh state is still unknown - candidates
are an actual bus-power drop/replug, a silent USB autosuspend/reset-
resume cycle (`px_suspend()`/`px_resume()` have no logging at all right
now, so either would be invisible), or an untracked manual reload.
`pcm open`/`trigger`/`close` dynamic-debug lines were also absent from
the log for this entire session for an unrelated reason (dynamic debug
for those specific lines was apparently not enabled, while the
unconditional hex-dump and watchdog `dev_warn`/`dev_err` lines still
printed) - worth re-enabling before the next long session.

**`v0.1.8-diag3`** (built, not loaded, `~/px64/snd-usb-phasex64.ko`,
`MODULE_VERSION("0.1.8-diag3")`): replaces diag1's hex dump with module
params so the fresh-replug matrix above can be tested without rebuilds:
`status_mode` (0=current `v0.1.8` behaviour, 1=exact `v0.1.5` behaviour -
no `clear_halt()` at all, 2=mode 0 plus an extra `status_settle_ms` delay
before `clear_halt()`), `status_settle_ms`, and `cap_urb_bytes` (replaces
the old `CAP_URB_BYTES` macro, validated to a non-zero multiple of 64
<= 8192). Also adds a once-a-second `pr_info()` capture summary
(completions, status tally, wire bytes/s, frames actually decoded into
the ALSA ring, out-of-order ring-slot completions) in place of the
hex dump. Test matrix and results pending in `tests/hw/REPORT.md`.

## "Capture dead, zero URBs" — root cause, fix, and a second throughput finding (diag3-diag7)

Full evidence trail in `tests/hw/REPORT.md`. The dead-start condition
turned out **intermittent**, not a deterministic function of the status
transition, `clear_halt()`, or autosuspend (each tested directly: same
params produced both dead and alive outcomes across repeated trials;
an autosuspend A/B test found a dead start with zero autosuspend ever
recorded). What *did* fix it, 5/5 across fresh replugs (p≈0.002 by
chance): proactively cycling `intf1` alt 0 -> alt 1 before the first
capture/feedback URB submission on every start, and unconditionally on
any rate change (44.1kHz had never once streamed successfully all
session until this fix - diag5's clear_halt()-free start path alone
wasn't enough, since the alt-cycle itself still needs to happen, with a
settle delay after `SET_CUR`, on a rate change specifically).

The capture watchdog (armed at `trigger(START)`, present since v0.1.5)
needed two more fixes along the way, found by direct code audit, not
guessing: (1) its liveness timestamp was being refreshed by *any*
non-shutdown-status completion, including zero-length/error ones, not
just genuine data - could let a run of errors masquerade as "healthy";
(2) several early-return paths let it silently stand down for the rest
of a stream with zero log trace. Both fixed: the timestamp now requires
`status==0 && actual_length>0`; the watchdog unconditionally
reschedules itself every tick, and only `cancel_delayed_work_sync()` in
`px_stop_streaming()` (process context) ever stops it.

**Second, independent finding, still OPEN** (build: diag7; the EIO part
with `arecord --buffer-time=2000000`): 96kHz capture showed a
real-time ratio of ~3.42x (vs ~1.6x at 44.1k/48k) - a clean, direct
confirmation of the original rate-sweep's fixed-throughput-cap
hypothesis (the same absolute ~1.87 MB/s wire rate / ~29232
frames-to-ALSA/s regardless of configured sample rate, i.e. 30.4% of
96000 nominal - a much larger shortfall than 44.1k/48k's ~39-61%,
not just a mild scaling-up of the same mild shortfall). At 96kHz with
a large `--buffer-time`, this produces a hard `EIO` after exactly
502ms - matching `1.1 x (buffer_size at the nominal rate)` to within
2ms - even though `hw_ptr` is confirmed, via live
`/proc/asound/.../status` polling 200ms apart, to be continuously
advancing (29920 fps) the whole time, never frozen. The 2ms timing
match is suggestive but not confirmed against ALSA/alsa-lib source;
kept as an open item in `tests/hw/REPORT.md`, not written up as a
settled "limitation".

v0.1.9 fix list (written here as a proposal; since built and accepted as
commit `9ccb276`, see `tests/hw/REPORT.md` "v0.1.10"): drop `clear_halt()` from the
normal start path; make the alt-cycle unconditional on every start and
every rate change with a settle delay; ship the hardened watchdog;
`usb_disable_autosuspend()` at probe as a cheap safeguard. The
96kHz/large-buffer-time EIO is explicitly NOT closed - needs further
investigation, not included in this fix list. `in_elapsed` was
re-audited this session and found already correct in committed v0.1.8
(an earlier assessment calling it a bug was itself wrong - no change
needed). `px_refresh_lock_cache()`'s double-call-site timing issue
remains outstanding from the v0.1.8 regression findings above.

## Windows USB trace (2026-10-14) — capture cap explained by an S/PDIF loopback cable

Full numbers in `tests/hw/REPORT.md` ("Windows USB trace"). Summary:

- With a coax S/PDIF loopback cable (digital out -> digital in) connected,
  the device powers up with status `0x16` (bit 2 = digital input locked) and
  `GET_CUR(wIndex 0)` returns 32000. The official Windows driver then runs
  everything at 32 kHz and restarts the stream ~1x per second; capture drops
  to ~0.75 MB/s and REAPER cannot record. Without the cable: status `0x12`,
  written `0x32`, `GET_CUR` returns the real rate, capture runs at exactly
  48000 fps (3.072 MB/s) at 48 kHz.
- All Linux "flat ~1.87 MB/s / ~29.2k fps" measurements were made with that
  cable connected (fresh status `0x16`, GET_CUR 32000). Leading hypothesis:
  the capture engine follows the S/PDIF input clock. To be verified on Linux
  without the cable before any code change.
- `GET_CUR(SAMPLING_FREQ)` is **not** a stub (correcting `tests/hw/REPORT.md`
  "GET_CUR ... dead stub"): it reports the active clock, which was the
  S/PDIF-locked 32 kHz.
- The official driver **does** clear the capture pipe at every stream
  (re)start: `ABORT_PIPE` + `SYNC_RESET_PIPE_AND_CLEAR_STALL` on 0x86, ~90-175
  ms after the status write. It writes the status byte even when unchanged,
  verifies with `GET_CUR(0x86)`, and never alt-cycles intf1.
- Wire details: bulk-IN 0x86 = 8 queued URBs of 131072 B; isoc OUT 0x02 = 40
  packets per URB, one per microframe (6 frames at 48k); feedback 0x81 = one
  1-packet URB per 1 ms frame, 3 bytes returned, all = frames per ms.
- ASIO buffer size (48 vs 1024 spls) and ASIO vs WDM make no difference on
  the wire.

**Confirmed on Linux (2026-10-14), cable removed** (full numbers in
`tests/hw/REPORT.md` "Confirmed without the S/PDIF cable"): with
`Digital Input Lock` off, `GET_CUR` tracks the requested rate exactly and
capture runs at real time (ratio ~1.0-1.02) at 44.1/48/88.2/96 kHz, with a
clean analog-loopback tone and zero errors - including 88.2k/96k, which
reliably crashed the capture engine every time the cable was connected.
**Every with-cable conclusion from this whole session is invalid**: the
flat ~1.8 MB/s throughput cap, the S/PDIF-mirror "64-frame"/`+65`/`-63`
periodic-duplication finding, "GET_CUR is a dead stub", and the 88.2k/96k
capture-engine crash. The `clear_halt`-is-harmful and alt-cycle-is-required
conclusions (v0.1.7/v0.1.9) were also reached with the cable connected and
are being re-tested now (`tests/hw/REPORT.md`, step 2) rather than assumed
either way. **Test rule going forward (see `CLAUDE.md`): never use an
S/PDIF digital loopback during capture/rate tests; use the analog RCA
loopback, and check `Digital Input Lock` + `GET_CUR` before every test.**

## Windows codec init, from USB trace (2026-10-14)

Source: `tests/hw/captures/asio_small.pcapng` (fresh plug-in, then REAPER
ASIO 44.1k -> 48k; panel state: In 1/2 = Phono, In 3/4 = Aux, Playback 1/2
and 3/4 on, Direct off, Mono off). All writes are vendor request `0x41`,
`wValue = (reg << 9) | value`, `wIndex = 0x0102` (codec 0) / `0x0106`
(codec 1), no data stage. The official driver sends exactly these six
writes, in this order, at **every** stream (re)start (4 times in
`asio_small`, ~0.5 ms apart, right after the status read and
`GET_CUR(wIndex 0)`, before `SET_CUR`):

| # | wIndex | wValue | Codec | Register | Value | Meaning |
|---|---|---|---|---|---|---|
| 1 | 0x0102 | 0x0e90 | 0 | R7  | 0x090 | DAC channel mix: stereo (0xF0 = mono) |
| 2 | 0x0102 | 0x2a02 | 0 | R21 | 0x002 | **ADC input mux 1/2: AIN2 = Phono** |
| 3 | 0x0102 | 0x2c01 | 0 | R22 | 0x001 | output mux 1/2: DAC only (playback on, Direct off) |
| 4 | 0x0106 | 0x0e90 | 1 | R7  | 0x090 | DAC channel mix: stereo |
| 5 | 0x0106 | 0x2a04 | 1 | R21 | 0x004 | **ADC input mux 3/4: AIN3 = Aux** |
| 6 | 0x0106 | 0x2c01 | 1 | R22 | 0x001 | output mux 3/4: DAC only |

No other WM8776 register is ever written (no reset, no interface/format,
ADC/DAC gain or attenuation registers) in any of the traces. The values are
the control panel's saved state (registry `WM8776_1/2`), not fixed
defaults:

- `asio_large`: no codec writes (the ASIO buffer change did not restart the
  stream).
- `wdm`: only single writes when a panel button changed, e.g. Direct 1/2 on
  = codec 0 R22 `0x005` (DAC + bypass), off again = `0x001`, each followed
  by the stream resync (`SET_CUR 0x86`, status write, ABORT/RESET 0x86/0x83).
- `digital_loop_32k`: the same 6-write burst on each of its ~1400 restarts,
  with the panel state of that moment (e.g. codec 1 R21 `0x0c0` = both ADC
  channels muted when no 3/4 input was selected, R22 `0x000`).

Comparison with `px_codec_init()` (v0.1.10, unchanged since v0.1.9): same registers, same order,
same encoding. Differences: (1) ours writes fixed factory defaults
(codec 0 R21 `0x01` = Mic/HiZ/Line, codec 1 R21 `0x04`, R22 `0x01` on both)
instead of the saved panel state, so after probe In 1/2 is Mic, not Phono,
until a mixer control or `alsactl restore` changes it; (2) ours writes once
at probe/resume, the official driver re-sends the set before every stream
start.

**Follow-up (2026-10-14, same day): difference (2) tested, not needed.**
A "thin Phono" symptom suggested our once-at-probe writes might be lost
across a stream stop/start. Built and power-cycle-tested a `codec_resync`
diagnostic that re-sends the registers before every start, matching
Windows exactly (`tests/hw/REPORT.md` "Codec resync: real bug, or a
measurement artifact?") - the registers were already provably correct
without it (full write log captured, zero errors, matches the Windows
target values exactly), and the apparent white-noise result was a
test-tooling bug in `pxtools.py` (see its module docstring), not a codec-state
problem. The remaining thin sound is explained by the RIAA finding below.
`codec_resync` was not shipped.

The v0.1.11 design revisits that last policy decision: resending six small
codec writes is cheap, matches Windows exactly and makes the cached shadow the
authoritative state again after any undocumented firmware reset. The proposal
therefore makes resync the default as robustness, not as the fix for the thin
sound. The thin sound has a separate, now-confirmed RIAA cause below.

## Windows capture DSP: RIAA

The missing phono equalization is software DSP in the official Windows USB
driver, not a WM8776 register or the ASIO DLL. In `msnkphsu.sys`,
`FUN_f100e590()` creates an RIAA filter only after the product check for
`0a4a:5b01` (decompiled locally with Ghidra, not distributed: `msnkphsu.sys.c:6395-6408`). The two channel-pair
checks are independent:

- channels 1/2: the per-pair `riaa_enabled` byte at object offset `0x33d` must
  be non-zero and input-selector field `+0x31c` must contain bit `0x2`;
- channels 3/4: the corresponding byte at `0x3b5` and selector field `+0x394`
  use the same bit `0x2` test.

Bit `0x2` is the Phono selection in WM8776 R21. Each successful check calls
`PtQuadPoleFilterData::createRIAA` (`FUN_f1059d90`) with that pair's state,
the current sample rate and channel count, and stores a separate processing
function pointer at `+0x2b8` or `+0x2c0`. This is direct evidence for
Phono-only, per-pair capture processing, including 3/4. Static inspection of
`msnkphs_x64.dll` found no RIAA strings, coefficient tables or equivalent
processing path; the ASIO DLL transports/control-dispatches audio while the
filter lives in `msnkphsu.sys`.

The constants embedded in `createRIAA` cover all four supported rates, but the
exact Windows filter could not be reconstructed reliably from the optimized
quad-pole/SSE state layout. We therefore do **not** claim the coefficients
below are Windows' original coefficients. They target the standard RIAA
playback curve with time constants 3180/318/75 us and are scaled so the digital
response is exactly 0 dB at 1 kHz.

The first proposal used a bilinear transform (BLT) prewarped at 1 kHz. Its
forced zero at Nyquist caused -13.52/-9.05 dB errors at 20 kHz for 44.1/48 kHz,
so the transform was re-evaluated before implementation. Maximum absolute
error on a 200001-point logarithmic 20 Hz--20 kHz grid was:

| rate | BLT, 1 kHz | best one-point BLT | per-break prewarp, z=-1 | matched-z, z=-1 | unconstrained fitted biquad | selected hybrid |
|---:|---:|---:|---:|---:|---:|---:|
| 44100 | 13.5152 | 7.8793 | 13.4706 | 13.5773 | 0.2350 | **0.3429** |
| 48000 | 9.0475 | 4.5212 | 9.0100 | 9.0999 | 0.1470 | **0.2230** |
| 88200 | 1.6545 | 0.6956 | 1.6435 | 1.6700 | 0.0087 | **0.0143** |
| 96000 | 1.3659 | 0.5750 | 1.3566 | 1.3789 | 0.0061 | **0.0100** |

Moving the global BLT prewarp cannot remove its Nyquist zero. Individually
prewarping each break frequency retains the same problem when the surplus zero
is placed at -1. A fully fitted real-root biquad has the smallest mathematical
maximum, but spends that improvement by moving 20 Hz by -0.235/-0.147 dB at
44.1/48 kHz. The selected one-biquad hybrid instead maps the three finite
analogue roots with matched-z and optimizes only the otherwise surplus digital
zero. It preserves the low band within 0.004 dB while reducing the whole-band
maximum to 0.343/0.223 dB. There is no extra runtime cost or extra state.

The difference equation is
`y[n] = b0*x[n] + b1*x[n-1] + b2*x[n-2] - a1*y[n-1] - a2*y[n-2]` (`a0 = 1`):

| rate | b0 | b1 | b2 | a1 | a2 |
|---:|---:|---:|---:|---:|---:|
| 44100 | 0.228910667793636 | -0.175340342942947 | -0.035213056747011 | -1.731979026020091 | 0.733832928312397 |
| 48000 | 0.214818305484235 | -0.168746850591770 | -0.030391314430797 | -1.750935179102223 | 0.752518919515998 |
| 88200 | 0.126855331060762 | -0.108156284027933 | -0.013756547019375 | -1.856141159796850 | 0.856640489535953 |
| 96000 | 0.117298884688064 | -0.100562058095814 | -0.012539193319164 | -1.867054403682534 | 0.867478483604059 |

| rate | b0 Q30 | b1 Q30 | b2 Q30 | a1 Q30 | a2 Q30 | pole radii |
|---:|---:|---:|---:|---:|---:|---:|
| 44100 | 245790958 | -188270260 | -37809732 | -1859698319 | 787947107 | 0.992894628, 0.739084398 |
| 48000 | 230659399 | -181190551 | -32632425 | -1880052333 | 808011037 | 0.993470051, 0.757465128 |
| 88200 | 136209875 | -116131926 | -14770980 | -1993016395 | 919810722 | 0.996440981, 0.859700179 |
| 96000 | 125948718 | -107977688 | -13463856 | -2004734401 | 931447929 | 0.996729678, 0.870324726 |

The response test is reproducible with `tests/hw/riaa/test_response.py`. At
20/50/100/500/1000/2000/10000/15000/20000 Hz,
digital-minus-analogue deviation is:

| rate | 20 Hz | 50 Hz | 100 Hz | 500 Hz | 1 kHz | 2 kHz | 10 kHz | 15 kHz | 20 kHz |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 44100 | +0.003404 | +0.003397 | +0.003372 | +0.002553 | 0.000000 | -0.010143 | -0.264946 | -0.318064 | +0.342947 |
| 48000 | +0.002300 | +0.002295 | +0.002278 | +0.001725 | 0.000000 | -0.006845 | -0.174889 | -0.205441 | +0.222989 |
| 88200 | +0.000166 | +0.000166 | +0.000165 | +0.000125 | 0.000000 | -0.000494 | -0.011795 | -0.012735 | +0.014310 |
| 96000 | +0.000117 | +0.000117 | +0.000116 | +0.000088 | 0.000000 | -0.000348 | -0.008272 | -0.008881 | +0.010007 |

Q30 rounding changes any coefficient by at most 4.46e-10. Its maximum added
response error is 0.0000014/0.0000017/0.0000032/0.0000049 dB respectively,
and every pole remains strictly inside the unit circle. The real A/B music
validation that established the audible cause predates this top-octave
improvement: using the original BLT, level-matched RIAA-filtered Linux versus
Windows differed by only
0.00-0.05 dB from the 31.7 Hz third-octave band through 1.6 kHz. The remaining
20-25 Hz residual was 2.2-2.6 dB. The user listened to
a local reference recording (`linux_riaa.wav`, not distributed) and confirmed it sounds like Windows. That closes
the thin-sound root cause: Windows applies this capture DSP; Linux v0.1.10
delivers the raw cartridge response.

### Linux v0.1.11 RIAA implementation (non-hardware validated)

The selected Q30 table is implemented directly after bit-serial capture
deserialization and before conversion to the ALSA S32 container. Each channel
uses Direct Form I with S24 input histories, saturated S24.Q8 feedback state,
a signed 64-bit accumulator and ties-away-from-zero rounding. The output and
feedback state are both clamped; wraparound is impossible. Channels 5/6 (the
digital mirror) are untouched.

`Phono RIAA 1/2 Capture Switch` and `Phono RIAA 3/4 Capture Switch` are
independent and default on. A pair is processed only when its WM8776 R21
source is exactly Phono, its switch is on and the read-only-at-load `riaa=1`
module parameter permits DSP. State resets on a fresh capture start, rate
selection, capture-source change and RIAA-switch change. The existing capture
spinlock serializes processing with every reset; controls keep the driver's
established `mutex -> cap.lock` order.

`tests/hw/riaa/test_kernel_model.py` verifies the literal C coefficient table,
bit-exact Q30/S24.Q8 results for every supported rate, saturation, raw bypass,
the deserialization refactor, independent pair controls and every reset path.
The module builds cleanly with `W=1` and reports version 0.1.11. No module was
loaded and no audio device was opened for this validation.

## Hardware `Ch.1/2 Aux/Phono` switch: measured effect (2026-10-16)

**Status: unconfirmed - do not treat the level/spectrum comparison below as
established**, though the segmentation itself (confirmed against the
operator's own record: the switch was toggled about every 10s, matching
what this analysis detected) is fine. Analysis-only session against a
pre-recorded hardware capture (a local recording, `switch_test.wav`, not distributed;
60s/6ch/48kHz/S32_LE) - no module load/unload, no device access. Setup:
software `In 1/2 Capture Source` fixed on `Aux` and driver RIAA off for the
whole recording (so nothing software-side could be contributing any EQ);
turntable on the `Ch.1/2` RCA jacks; the physical `Ch.1/2 Aux/Phono` switch
toggled by hand about every 10s, starting on `Phono`.

The one real problem: at least two of the segments this analysis labeled
`Aux (with signal)` are actually near the noise floor (confirmed by the
operator), not genuinely attenuated program material - comparing a
spectrum against a near-silent segment is meaningless, so the level/
spectrum comparison below, which depends on those segments, cannot be
trusted. Kept here, with this caveat, as a record of what was tried and why
it didn't work, rather than deleted - see "Conclusion" below for the
actual, independently-established answer (a direct hardware matrix the
operator measured, not derived from this recording at all), and "Optional
confirmation measurement" for a sweep-based cross-check.

### Segmentation

A naive moment-to-moment level comparison is unusable here - real program
material (music) swings several dB on its own well within what should be a
single switch state, producing many false "switch" candidates. A bass-energy-
to-broadband-energy ratio (20-150 Hz vs. full band, 1s window/0.25s hop, with
hysteresis) proved far more switch-specific, since it tracks hardware EQ state
rather than moment-to-moment loudness, and cleanly separated the recording
into 8 segments:

| span | duration | state |
|---|---|---|
| 0.00-10.00s | 10.00s | Phono |
| 10.00-21.75s | 11.75s | Aux (with signal) |
| 21.75-31.25s | 9.50s | **silent** (true digital floor, ~-71 dBFS RMS) |
| 31.25-41.25s | 10.00s | Phono |
| 41.25-42.00s | 0.75s | Aux (brief, too short for reliable stats) |
| 42.00-51.25s | 9.25s | **silent** |
| 51.25-52.75s | 1.50s | Phono (brief) |
| 52.75-60.00s | 7.25s | Aux (with signal) |

The ~10s cadence this table shows is confirmed correct against the
operator's own record of the test - not every ~5s, despite this document's
own earlier (wrong) assumption to the contrary. The one real problem:
**at least two of the segments labeled `Aux (with signal)` are actually
the noise floor** (confirmed by the operator), not attenuated program
material as assumed from the audio alone. Which specific segments is not
re-derived here, and it doesn't need to be - see "Conclusion" below for
why this no longer matters: the actual answer comes from a direct hardware
matrix, not from reinterpreting this recording.

### Level and spectrum: Phono vs. Aux (unreliable - see status note above)

Using the two usable segments per state (≥2s, 0.5s guard-banded at each
boundary): `Phono` = [0.00-10.00s, 31.25-41.25s], `Aux` = [10.00-21.75s,
52.75-60.00s].

- **Level**: Phono averages 8.1 dB louder RMS than Aux (pairwise diffs across
  all 2×2 combinations: +4.3 to +12.0 dB, mean +8.1 dB, std 2.9 dB - a small
  sample, n=2 per state, from one 60s recording).
- **Spectrum** (1/3-octave, 20 Hz-20 kHz): Phono minus Aux, by band - these
  are raw per-band dB differences (the ~+8 dB overall level difference above
  is folded in, not factored out), and the point is exactly that they are
  *not* uniformly ~+8 dB across bands, which a flat gain difference would
  produce - the bass band runs nearly 4x that, the mid band runs below it -

  | band | mean diff | range |
  |---|---:|---|
  | bass, 20-150 Hz | **+30.0 dB** | +9.2 to +46.9 dB (peaks ~40-100 Hz) |
  | mid, 150 Hz-2 kHz | +6.7 dB | +3.7 to +12.1 dB |
  | presence, 2-8 kHz | +15.6 dB | +8.4 to +22.4 dB |
  | top, 8-20 kHz | +15.4 dB | +7.0 to +21.1 dB |

  Checked for reproducibility, not just averaged: the two Phono/Aux pairs
  (0-10s vs. 10-21.75s, and 31.25-41.25s vs. 52.75-60s) are *different music*
  from the same record, not a repeat of the same passage, yet their
  per-band difference curves correlate at r=0.814 and show the same shape
  independently - the huge, consistent bass boost in both pairs (+33 to
  +49 dB in the 40-100 Hz bands, both times) is not a content coincidence.
  The presence/top-band boost is real in both pairs too but its *size* is
  less consistent (+25 to +34 dB in pair 1, +7 to +17 dB in pair 2) -
  reported with that caveat rather than averaged over it.

One caveat on the raw numbers: one Phono segment (31.25-41.25s) hits 0 dBFS -
genuine clipping, not a measurement artifact, consistent with RIAA off and a
turntable with a reasonably hot output feeding an already-boosted hardware
path. Peak figures for Phono are therefore a lower bound on the true analogue
peak, not the peak itself; RMS and the spectral shape (which this finding
rests on) are unaffected.

### Hardware matrix (2026-10-16, operator-measured - the actual answer)

Direct measurement, not derived from the recording above or from this
analysis at all: turntable on RCA `Ch.1/2`, read from the panel's In 1/2
peak meter plus listening, all four switch x software-source
combinations - **the stream was restarted after every software source
change**, since 0.1.11.5 has no live codec apply (a software `Capture
Source` write only takes hardware effect at the *next* stream start, see
"Live mixer-write protocol gap" below - changing it mid-stream and judging
the result without restarting gives a stale reading):

| hw switch | sw source (`In 1/2 Capture Source`) | result |
|---|---|---|
| `Phono` | `Phono` | full level, correct with driver RIAA on; full level but thin with it off |
| `Aux` | `Phono` | no signal |
| `Aux` | `Aux` | small signal (unamplified cartridge level) |
| `Phono` | `Aux` | no signal |

(An earlier pass at this same matrix, before restarting the stream on
every source change, read `Phono`/`Aux` as "full level, thin" instead of
"no signal" - a stale-state artifact of the live-apply gap, not the real
hardware behavior. Superseded by the table above.)

### Conclusion: what the switch does

- **The side switch routes the RCA `Ch.1/2` jacks to one of two separate
  destinations: the hardware phono preamp (`Phono` position) or a
  line-level `Aux` path (`Aux` position) - not the same node with a gain
  difference.** The software `Capture Source` selection (`Phono`/`Aux`)
  picks which of those two destinations the ADC actually listens to (see
  "Verified from code" below - these are genuinely different WM8776 mux
  pins). **The two must match, or nothing reaches the ADC at all** - both
  mismatched combinations in the matrix above (`Aux`/`Phono` and
  `Phono`/`Aux`) read as no signal, not just quiet, which only makes sense
  if each switch position feeds exactly one of the two pins, not both.
- **The hardware phono preamp is flat - roughly 40 dB of gain, no RIAA
  shaping.** `Phono`/`Phono` with the driver's RIAA DSP switched off still
  reads full level but thin; a hardware RIAA stage would still sound
  bass-correct there even with the software DSP off. Full RIAA shaping
  lives entirely in the driver (same as the Windows driver) - consistent
  with the existing Windows/Linux RIAA validation (switch was on `Phono`
  for every recording there, see "Windows capture DSP: RIAA" above;
  matching Windows with the *full* software curve and no partial/residual
  term only works if the hardware stage is flat).
- **Matching rule**: vinyl needs `Phono` + `Phono` (driver RIAA on, the
  normal default for that source). A line-level source connected to the
  same RCA jacks needs `Aux` + `Aux`.
- **Risk**: this is about a *matched* pair used for the wrong source, not
  a mismatch (a mismatch just gives silence, per the matrix above) - a
  line-level source left with *both* the hardware switch *and* software
  source on `Phono` gets ~40 dB of unwanted extra gain plus the RIAA bass
  boost, and will clip. Worth a clear warning somewhere a user would see
  it before plugging something in (see "Backlog" below).
- **Applies equally to `Ch.3/4`** (same switch/preamp circuit on that
  pair) - not separately measured, inferred from the shared design (see
  "Hardware I/O" above).

This session's earlier "partial, bass-only RIAA shelf" reading, and the
matrix's own first (stale-state) pass further up this section, are both
superseded by the table above and should not be treated as having
established anything about the switch's frequency response or routing.

### Verified from code: do the Phono/Aux control bytes differ?

Yes, confirmed by reading `linux/snd-usb-phasex64/phasex64.c`.
`px_adc_source_put()` writes WM8776 register R21 (`WM_ADC_MUX`) with
`ADC_PHONO = 0x02` for source `Phono`, `ADC_AUX = 0x04` for source `Aux`
(see the `ADC_MIC`/`ADC_PHONO`/`ADC_AUX`/... defines and
`px_adc_12_sources[]`) - genuinely different bit patterns, not the same
write with a different label. The driver's own long-standing comment on
this register already documents why that matters: "The ADC input mux
(`WM_ADC_MUX`) is a single-select source per channel pair, not a set of
independent bits" - this is a hardware mux choosing one of several
distinct physical WM8776 analog input pins, not a digital gain or EQ
control. In hardware terms, selecting `Phono` vs `Aux` in software picks
which codec input pin the ADC listens to.

That, combined with the corrected matrix above, is a clean, fully
consistent picture with no asymmetry left to explain: the switch
physically connects the RCA jacks to one of two destinations (the preamp
output, or a direct line-level path), each wired to exactly one of the
WM8776's two distinct mux pins, and software source selection picks which
pin the ADC listens to. Match them and signal flows; mismatch either way
and it doesn't, because the unselected pin simply isn't driven by
anything in that switch position. (An earlier draft of this section
inferred that `Phono` fed *both* pins, to explain a "full level, thin"
reading for the mismatched `Phono`/`Aux` case - that reading has since
been traced to the live-apply gap, not real hardware behavior, so that
more complicated theory is no longer needed.) Still, strictly, an
inference from the digital control bytes plus the operator's matrix, not
something the driver can directly see - it has no visibility into the
analog board itself, only into which mux bit it last wrote.

### Backlog: panel + README should state the matching rule

Not implemented here (docs-only session, no driver/panel change):

- **Panel**: a static hint near the `In 1/2` and `In 3/4` source choices,
  e.g. "Side switch `Ch.1/2` must match: Phono <-> Phono, Aux <-> Aux."
  Static only - the switch position can't be read back by software (no
  status bit found, see "Control requests" above), so the panel has no way
  to warn only when actually mismatched, just a standing reminder.
- **README** (`README.md` + `README.nl.md`): a hardware section stating
  the matching rule (vinyl = switch `Phono` + source `Phono`; line-level
  source on the same RCA jacks = switch `Aux` + source `Aux`) and the
  clipping warning: **not** a mismatch (that gives silence, see the
  matrix above) but a line-level source left with *both* switch `Phono`
  *and* source `Phono` - gets ~40 dB of unwanted extra gain plus RIAA,
  and will clip.

### Optional confirmation measurement (not run - prepared for the user to execute)

**Optional - the matrix above already answers the main question** (what
the switch does, and the matching rule). A sweep would only *quantify* the
preamp's gain and confirm its flatness numerically, not establish anything
new. Both captures use a **matched** switch/source pair this time (per the
corrected matrix, a mismatched pair gives no signal at all, so there's
nothing to measure there): `Phono`+`Phono` with the driver's RIAA DSP
switched off (so only the hardware stage can be responsible for any
shape), versus `Aux`+`Aux`.

- Capture 1: hardware switch **`Phono`**, software source **`Phono`**,
  `Phono RIAA 1/2 Capture Switch` **off**, sweep at **-60 dBFS** (the
  ~40 dB preamp gain is active - start low or it clips).
- Capture 2: hardware switch **`Aux`**, software source **`Aux`**, sweep
  at about **-20 dBFS** (no preamp gain this time, per the matrix's
  "small signal" - needs a hotter source to land at a usable level).

**Setup**: an analog RCA cable from `Outputs Ch.3/4` to the `Ch.1/2`
Phono/Aux RCA inputs (an actual cable this time, not the `Loopback 3/4`
software routing - that's a separate, WM8776-internal path, see "`Loopback
3/4` / `Loopback 1/2`" in `analysis/loopback-and-monitoring.md`, and would
bypass the `Ch.1/2 Aux/Phono` switch and the phono preamp stage entirely).
PipeWire stopped so `aplay`/`arecord` have the device directly.

**Important, per the matrix's own finding above**: `In 1/2 Capture Source`
only takes hardware effect at the *next stream start* (no live codec apply
in 0.1.11.5) - set it *before* each `arecord`/`aplay` pair below, with no
stream already running, not while one is active. The RIAA switch is a
software-only DSP toggle and *does* apply live, no restart needed for it
specifically.

```sh
cd /tmp && mkdir -p px64-sweep && cd px64-sweep

# log sweep, 20Hz-20kHz, 10s, 1s silence lead-in/out for a clean onset/decay
# and settle time; 4ch so it plays on Out 3/4 specifically. Two levels -
# -60dBFS for the Phono capture, -20dBFS for the Aux one.
for level in 60 20; do
  sox -n -r 48000 -b 24 -c 2 sweep_stereo_${level}.wav \
      synth 1 sine 0 vol 0 : synth 10 sine 20/20000 vol -${level}dB : synth 1 sine 0 vol 0
  sox -n -r 48000 -b 24 -c 2 silence34.wav synth 12 sine 0 vol 0
  sox -M silence34.wav sweep_stereo_${level}.wav -c 4 -b 24 sweep_out34_${level}.wav
done

systemctl --user stop pipewire pipewire-pulse wireplumber \
    pipewire.socket pipewire-pulse.socket

# --- switch on Phono, then: no stream running yet, so these take effect
# at the arecord/aplay below ---
amixer -c PhaseX64 cset name='In 1/2 Capture Source' Phono
amixer -c PhaseX64 cset name='Phono RIAA 1/2 Capture Switch' off
(arecord -D hw:PhaseX64 -f S32_LE -c 6 -r 48000 -d 14 -q capture_phono.wav) &
sleep 1
aplay -D hw:PhaseX64 -q sweep_out34_60.wav
wait

# --- switch on Aux, then: again, no stream running at this point ---
amixer -c PhaseX64 cset name='In 1/2 Capture Source' Aux
(arecord -D hw:PhaseX64 -f S32_LE -c 6 -r 48000 -d 14 -q capture_aux.wav) &
sleep 1
aplay -D hw:PhaseX64 -q sweep_out34_20.wav
wait

# sanity + clipping check - re-run the Phono side quieter (e.g. -72 in the
# "for level in" line above) if it clips; if either peak is near the noise
# floor instead (wrong switch position, or RIAA switch left on for the
# Phono capture), fix that and redo:
python3 -I -c "
import wave, numpy as np
for f in ('capture_phono.wav', 'capture_aux.wav'):
    with wave.open(f, 'rb') as w:
        d = np.frombuffer(w.readframes(w.getnframes()), dtype='<i4').reshape(-1, w.getnchannels())
    peak = np.abs(d[:, 0:2]).max() / 2**31
    print(f'{f}: In 1/2 peak = {20*np.log10(peak+1e-12):.1f} dBFS' +
          ('  *** CLIPPING, lower the sweep level and redo ***' if peak > 0.999 else ''))
"

amixer -c PhaseX64 cset name='Phono RIAA 1/2 Capture Switch' on
systemctl --user start pipewire.socket pipewire-pulse.socket pipewire pipewire-pulse wireplumber
```

**Analysis** (once both captures exist, clean): per-frequency magnitude of
In 1/2 across each sweep (e.g. a chirp-matched filter, or RMS in narrow
bands as the sweep passes through them), each normalized to its own
sweep's playback level (60 dB vs. 20 dB down), then `Phono - Aux` at each
frequency gives the preamp's own gain and frequency response directly -
no program-material ambiguity, no segmentation, no noise-floor
contamination, no mismatched-pair silence. Per the matrix above, the
expected result is a flat line at roughly the preamp's gain (~40 dB), not
a RIAA-shaped curve. A clearly frequency-dependent result here would
contradict the hardware matrix and be worth treating as the stronger
evidence, having controlled for everything the matrix (listening + meter,
not a sweep) didn't.

## Live mixer-write protocol gap (2026-10-15) - trace evidence, no fix yet

Codec/mixer controls (PCM, Direct, Mono, Capture Source) write correctly to
hardware but don't take audible/recorded effect while a stream is active -
only at the next stream start. Root cause is a protocol gap, not a driver
bug in the write path itself: Windows, on a live control-panel change while
streaming, follows the codec write with `SET_CUR`(same rate)+`GET_CUR`
verify+`STATUS` read/write+~90ms+pipe abort/reset on `EP_CAPTURE` (0x86)
*and* `EP_MIDI_IN` (0x83) - extracted as literal trace evidence from
`wdm.pcapng` (two clean Direct 1/2 toggles while streaming). Linux only does
the codec write. Full transaction table, locking/URB-ownership analysis, and
the (not yet built) experiment design are in `tests/hw/REPORT.md` "Live
mixer-write protocol investigation (2026-10-15)". Which element of the
Windows sequence is actually necessary is unconfirmed - treat as hypothesis,
not fact, until hardware-tested.

## `Loopback 3/4`/`Loopback 1/2` mechanism, hardware-confirmed (2026-10-19)

Settled which of two candidate mechanisms the `Loopback 3/4`/`Loopback
1/2` capture sources actually use: a copy of the *other input pair's*
own signal, or the *other output pair's* actual analog output (Direct +
PCM as mixed). Setup: `In 1/2 Capture Source = Loopback 3/4`, `In 3/4
Capture Source = Aux` with a synth playing into Aux (-13.9 dBFS). The
`PCM 3/4` switch itself stayed on throughout - only `Direct 3/4` was
toggled, and whether anything was actually playing through PCM -
comparing what `In 1/2` reads against what the synth is still doing on
`In 3/4`.

Result: **the other output pair's actual analog output** - confirmed by
`In 1/2` dropping to the analog noise floor (-84.7 dBFS) the moment Out
3/4 itself goes quiet (Direct off, PCM on but nothing playing), even
though the synth never stopped playing into In 3/4's own input the
whole time; and conversely, once real music actually played through
that same, still-on PCM path, In 1/2 followed it almost exactly (Out
3/4 at 0.0 dBFS, In 1/2 at -0.3 dBFS). Rules out "copy of the other
input" outright. Roughly 0.3-0.8 dB lower than the actual source across
every measurement in this session (an earlier, same-day null test -
sample-aligned, no filtering difference, just a ~0.3-0.75 dB level +
polarity correction - was consistent with this but couldn't distinguish
the two candidates on its own, since that test's setup happened to make
them predict the same result). Full methodology,
numbers, and what's still untested (the DSP route's own effect,
`Loopback 1/2` specifically) in `analysis/loopback-and-monitoring.md`,
"Loopback 3/4 / Loopback 1/2: a different mechanism".

## 0x49 status-read stalls (2026-10-19) - expected, harmless, noise-only

Investigated a day's worth of `status read failed: bRequest=0x49
wValue=0x0000 wIndex=0x0000 err=-32` lines in dmesg (25 lines) prompted by
their volume, not by any observed symptom. `0x49` is `REQ_STATUS`
(`snd-usb-phasex64/phasex64.c:51`); the one and only issuer is
`px_status_read()` (`phasex64.c:602-612`), a 1-byte IN-direction vendor
control read. `err=-32` is `-EPIPE`, i.e. a control-endpoint (EP0) STALL.
Per the USB 2.0 spec an EP0 stall self-clears on the next SETUP packet -
no `usb_clear_halt()`/`CLEAR_FEATURE` is needed and the driver issues
none; this matches `tests/hw/REPORT.md` "Finding B", which confirms the
device's control interface is immediately usable again right after each
failure, with no WARN, hang, or corruption.

**All six call sites, and how each one already guards or tolerates the
failure:**

1. `px_status_update()` (628-665) - generic switch/clock-put path,
   including the one call at probe (`px_probe()`, 4717-4720). Skips the
   live read entirely while `px->running`, using the cached
   `px->last_status_byte` instead; only reads live while not streaming.
2. `px_lock_get()` (3947-3984) - "Digital Input Lock" control `.get`.
   Same pattern: returns the cached `px->last_digital_lock` while
   running, only live-reads while not streaming.
3. `px_cap_watchdog_fn()` (~1766) - a deliberate diagnostic probe during
   an active-capture stall. Expected to fail while genuinely streaming;
   the failure is itself logged with its own context and doesn't block
   the watchdog's recovery action.
4. `px_start_streaming()` (~2100) - fresh stream start, wrapped in
   `px_ctrl_retry` (up to 3 retries, 20ms apart). Only call site where a
   failure can reach userspace, and only if all retries exhaust.
5. `px_live_apply_restart()` (2350) - live apply's own status read after
   killing URBs, wrapped in `px_ctrl_retry_stoppable`, same bounded-retry
   behaviour as #4.
6. PCM `.open` path (~3066, "not yet streaming" branch) - tolerates
   failure with no retry and no propagation at all; a failed read just
   leaves the full rate table in place, with `px_start_streaming()`'s own
   `GET_CUR` verify as the backstop.

**Today's evidence:** 25 lines in 8 clusters of 1-5 lines each, intra-
cluster gaps mostly ~0.19s (exact mechanism not pinned down - not needed
for this conclusion), inter-cluster gaps irregular (minutes to hours
apart). Clusters coincide with device (re)open activity: the first and
largest cluster follows a manual `rmmod`/`modprobe` module reload done as
part of today's install procedure (not a mere DKMS re-bind, correcting an
earlier read of this same data), the rest coincide with PipeWire/REAPER
stream start-stop and panel use throughout the day.

**Windows comparison:** `tests/hw/REPORT.md` "Windows USB trace" shows
the Windows driver issuing a live status read only at device plug-in; a
live control-panel change while already streaming goes straight to a
status **write**, never a read. Same cache-while-running strategy this
driver already uses for #1/#2 above. Supports treating the live-read
stall under active streaming traffic as a device/firmware-level
characteristic, not something either driver's own request sequencing
controls.

**Conclusion:** expected device behaviour, harmless. The message is
rate-limited (`dev_err_ratelimited()`, line 607); 25 lines across a whole
day of heavy open/close/REAPER activity is itself consistent with the
limiter suppressing nothing meaningful here.

**Follow-up, not done now (candidate for v0.1.13):** log at `dev_err`
only when `px_start_streaming()` exhausts its retries (the one path with
a real functional effect), and at `dev_dbg`/`dev_info_ratelimited` for
the remaining, already-tolerated/probe call sites - so a user reading
dmesg doesn't mistake routine, harmless noise for a bug.

**v0.1.13 update (2026-10-20), hardware-tested:** implemented the
follow-up above, minimally - `px_status_read()`'s own log
(`phasex64.c:607-610`) moved from `dev_err_ratelimited` to
`dev_dbg_ratelimited`; every caller's own, better-contextualized
logging (the retry macros, the watchdog's `dev_warn`, `px_start_
streaming()`'s `dev_err` on retry exhaustion) is untouched, so no real
error signal lost. Confirmed on hardware: no "status read failed" line
at error level after a reload, a PipeWire restart, REAPER start/stop,
or panel open/close; with dynamic debug enabled on `snd_usb_phasex64`
the same message reappears at the same moments, one level down. Full
checklist and results: `tests/hw/v0113-checklist.md`.

New observation from that same test session: an `err=-110` (`-ETIMEDOUT`,
~1s = `CTRL_TIMEOUT`) on the PCM-open-path status read (call site #6
above, `phasex64.c:~3066`), seen about 1.05s after PipeWire closed both
streams. `px_pcm_open()` still returned 0 (this call site tolerates the
failure by design, see #6 above) and the stream was running again
shortly after. Effect: roughly one extra second of delay on a PipeWire
restart, nothing else - harmless, consistent with the device's control
interface still settling right after a stream stop. Possible later
tweak, **not done now**: a shorter timeout for this specific read, or
skipping it outright for a short window right after a full stop.

**Debugging gotcha, hardware-confirmed:** this message logs against the
*USB device* (`usb 3-2: status read failed ...`), not the ALSA card
name - `dmesg | grep -i phasex64` alone does **not** find it. Grep for
`dmesg | grep -iE "status read|0x49"` instead. Added to the README
troubleshooting line (en+nl) next to the existing 0x49 note.

## MIDI without an audio stream (item #6, 2026-10-20) - hardware-tested

Investigated whether MIDI needs an active PCM stream to work, prompted
by PipeWire's permanent stream (`suspend-timeout 0`) having made this
untested in practice. Code-only read (done first, before hardware
testing): the driver's own MIDI path is **structurally independent** of
PCM state - `px_midi_in_open()`/`px_midi_in_close()`
(`phasex64.c:4301-4347`) and `px_midi_out_open()`/`close()`/`trigger()`
(`phasex64.c:4361-4387`) submit/kill their own URBs purely on the
rawmidi substream's own lifecycle, none of them reference `px->running`
or any PCM state; both USB interfaces go to altsetting 1 once at probe
and stay there through a PCM stop (`px_stop_streaming()`,
`phasex64.c:1858-1900`, touches only audio URBs/watchdog/live-apply
work, never an interface's altsetting); `px_resume()`
(`phasex64.c:4838-4862`) re-arms MIDI URBs independently of any PCM
precondition too.

**Hardware test (DIN loopback, PipeWire fully stopped, `pcm0p`/`pcm0c`
confirmed "closed"):**

| Condition | Sent | `midi0` Tx | `midi0` Rx | Received in terminal 1 |
|---|---|---|---|---|
| No audio stream at all | note on/off (6B) + 18B SysEx | +3 per `amidi -S` call (not +6/+18) | +0 | nothing |
| Capture stream only (`arecord`, no playback) | same | +18 | +18 | SysEx received intact |

**Conclusion: the driver's code path is independent of PCM state (as
the file:line evidence above shows), but the device's own firmware is
not - it only services the MIDI endpoints while an audio stream is
actually running, and a capture-only stream is enough.** This refines,
not reverses, the earlier code-only read: there's nothing to fix in the
driver's MIDI handling itself, the gating happens on the device side,
below anything Linux's driver controls.

The existing README/panel hint ("MIDI only flows while an audio stream
is active") stays correct as written; add "a capture stream alone is
enough" to it, since that wasn't previously confirmed.

**What a stream-independent MIDI path would require:** since the
limitation is in firmware, not in this driver's request sequencing, the
only way around it from Linux would be a driver-side silent keep-alive
audio stream (minimal-rate capture, discarded) kept running for as long
as the MIDI rawmidi device is open and no real PCM stream already is.
Not small: needs its own URB lifecycle independent of
`px_pcm_open()`/`close()`, interacting correctly with a real PCM stream
starting or stopping while the keep-alive is active, and the live-apply
machinery's various `px->running`/`px->urbs_live` assumptions would need
auditing against a "running but not the user's own stream" state they
don't currently have a concept of. **Low priority, not planned now** -
PipeWire's own `suspend-timeout 0` already keeps a real stream running
essentially all the time in normal use, so the gap is mostly
theoretical today.

## Open items (backlog, not scheduled)

- **RESOLVED (2026-10-21, `fix/panel-orphans`, merged as 4497c55):
  Panel process can survive with no visible window, and block
  `rmmod`.** Found 2026-10-20 while reloading for the v0.1.13 test: an
  already-running panel instance with no visible window, plus its own
  `alsactl monitor` child process, was still holding `controlC0` open.
  Killing the panel process alone didn't release it - the orphaned
  `alsactl monitor` child kept holding the device until it was killed
  separately too. See `tests/hw/v0113-checklist.md`, "Results", step A.
  Root cause: Python's default SIGTERM/SIGHUP disposition killed the
  panel without running its cleanup, and the children had nothing tying
  them to the parent. Fixed by SIGTERM/SIGINT/SIGHUP handlers routed
  into the normal close path, `setpriv --pdeathsig TERM` on both
  children (covers SIGKILL), and a single-instance lock. All six cases
  (window close, SIGTERM, SIGHUP, SIGKILL, second start, lock release)
  accepted on the real desktop on 2026-10-21.
  **Lesson:** Xvfb has no window manager. The "already running" dialog
  was transient-for a withdrawn owner; it passed the automated Xvfb test
  but never mapped under KWin (ICCCM: a transient of a withdrawn window
  stays unmapped), leaving the second process hung. Window-manager
  behaviour needs a real-desktop test, not just Xvfb.

### Later / v0.1.14 (optional)

- **PCM-open status-read timeout:** the `err=-110` on the PCM-open-path
  status read right after a full stop costs ~1s on a PipeWire restart
  (see "0x49 status-read stalls", v0.1.13 update). Candidates: a shorter
  timeout for that one read, or skip it for a short window after a stop.
- **Live mixer-write protocol gap:** Windows follows a live codec write
  with `SET_CUR`/`GET_CUR`/status/pipe reset (see "Live mixer-write
  protocol gap"). Live apply already works via the restart path, so this
  is for completeness/understanding, not a user-visible fix.
- **MIDI without an audio stream:** a cheap "light arm" hardware
  experiment (`STATUS_STREAM` write alone) first; a driver-side MIDI-only
  keep-alive stream only if that fails and it's still wanted (see "MIDI
  without an audio stream").

## Front inputs: Mic / Hi-Z / Line (2026-10-20) - hardware-measured

User-measured on hardware (panel IN 1/2 source = Mic-HiZ-Line; meters in
the panel and REAPER). All gain described here is analog, before the
ADC - not controllable by the driver or panel.

**Layout:**
- Front: Ch.1 = combo socket (XLR + 6.3mm jack) with "Mic/Line Gain" knob
  (0-11), +48V LED, Clip LED. Ch.2 = 6.3mm jack "HiZ/Line" with "HiZ/Line
  Gain" knob (0-11) and Clip LED. Phones jack + volume. Phono/Aux RCA for
  Ch.1/2 and Ch.3/4.
- Side switches: 48V Off/On; Ch.1 Line/Mic; Ch.2 Line/HiZ; Phones Ch.1/2 ↔
  3/4; Ch.1/2 Aux/Phono; Ch.3/4 Aux/Phono.
- Rear: "Inserts" MIC/Line and HiZ/Line (insert jacks for Ch.1/Ch.2 - not
  tested).

**Channel mapping and monitoring:**
- Ch.1 (mic) = capture channel 1 (L) of IN 1/2, mono. Ch.2 (Hi-Z) =
  channel 2 (R), mono.
- Direct on OUT 1/2: each channel on its own side (mic left, bass
  right).
- Direct Mono (only available with source Mic-HiZ-Line + Direct): SUM of
  Ch.1 + Ch.2 on both sides (mic and bass both heard in both ears).

**48V:** works (BM-800 condenser: weak residual signal without 48V, full
signal with 48V); +48V LED lights. Phantom is only on the XLR part of
Ch.1; the Ch.2 jack never carries 48V.

**Ch.1 switch selects the CONNECTOR, not just a gain stage:** "Mic" = XLR
part only; "Line" = jack part only. A jack plugged into the combo with
the switch on Mic gives no signal (only preamp noise: -85dBFS at gain 0,
-64.7dBFS at max - this is also the bare mic-preamp noise floor without
a mic).

**Gain ranges** (approximate, from peak/noise readings):

| Input | Switch | Source | Gain 0 | 12 o'clock | Max | Range | Notes |
|---|---|---|---|---|---|---|---|
| Ch.1 | Mic | BM-800 + 48V | -20dBFS (noise -64.7) | -7.8dBFS (noise -56.5) | 0dBFS + Clip LED (noise -39.3) | ≈25dB | Clip LED coincides with 0dBFS at the ADC |
| Ch.1 jack / Ch.2 | Line | phone, fixed volume | -19dBFS | -16dBFS | -5dBFS | ≈14dB | Log taper - most gain in the second half of the knob |
| Ch.2 | HiZ | phone, fixed volume | -30dBFS | -3.4dBFS ("quarter to") | clipping | ≥30dB | Steep; don't use HiZ for line sources |
| Ch.2 | HiZ | passive bass, gain 0, volume full | -25.8dBFS, R only | - | - | - | - |

**Recommendation: docs + panel tooltips only.** All gain and connector
selection happens on the unit itself (analog, pre-ADC); there's nothing
for the driver or panel to control or fix here. Documented in the README
(user-facing) and the panel's `ITEM_TOOLTIPS`/`direct_mono_tooltip` (the
L/R mapping and Direct Mono's sum behaviour, since those are relevant to
routing decisions the panel does make).
