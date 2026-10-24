# REAPER latency check (pipewire-jack)

Does REAPER, running on PipeWire through `pipewire-jack`, place a recording
where it belongs? The raw-ALSA measurement (`../REPORT.md`, "Round-trip
latency, raw ALSA (2026-10-24)") found ~4.55 ms (USB pipeline) + ~52.9
samples (converters) of round-trip delay that the driver does not report.
This kit records the same impulse stimulus on two tracks in one take:

- **in3** - In 3/4 on `Loopback 1/2`: Out 1/2's analog output (USB +
  converters);
- **in5** - capture 5, the bit-exact digital tap of Out 1/2 (USB only).

Then `px_latency.py rpp` reads, from the saved project, where REAPER put
each recorded item, and prints the remaining error in samples. 0 = REAPER
compensated exactly; positive = the recording sits late.

Done once per PipeWire quantum: 64, 256 and 1024. All by hand - nothing
in this directory opens the device by itself.

## Files

- `latency.rpp` - project template: track `stim` (the stimulus at
  position 0, hardware out 1/2, no master send), tracks `in3` and `in5`
  (record armed, mono inputs 3 and 5, monitoring off, no master send -
  in3 must never reach Out 1/2, which it is recording). Records 24-bit WAV
  into `recorded/`.
- The stimulus `stim.wav` is not committed (the repo ignores `*.wav`); it
  is generated, deterministically, with `px_latency.py gen --channels 1`
  (mono, 48 kHz, 4.6 s, ~650 KB).

## Steps

### 1. Prepare (once)

```sh
PX=/path/to/phasex64-linux/tests/hw/px_latency.py       # your checkout
KIT=/path/to/phasex64-linux/tests/hw/reaper-latency
mkdir -p ~/px-latency/reaper && cd ~/px-latency/reaper
for q in 64 256 1024; do
    mkdir -p q$q && cp "$KIT/latency.rpp" q$q/
    python3 "$PX" gen --channels 1 --out q$q/stim.wav
done

amixer -c PhaseX64 cget name='Digital Input Lock'           # must be off (coax unplugged)
amixer -c PhaseX64 cget name='Direct 1/2 Playback Switch'   # note it, to restore later
amixer -c PhaseX64 cset name='In 3/4 Capture Source' 'Loopback 1/2'
amixer -c PhaseX64 cset name='Direct 1/2 Playback Switch' off
amixer -c PhaseX64 cset name='PCM 1/2 Playback Switch' on
```

PipeWire stays running. With a stream active, these mixer changes apply
through the driver's live restart (a ~0.3 s dropout) - do them before
starting REAPER. **Speakers and headphones down:** the stimulus is 16 clicks
at -6 dBFS on the Out 1/2 jacks.

In REAPER, once: *Preferences -> Audio -> Device*: JACK. *Preferences ->
Audio -> Recording*: "Use audio driver reported latency" **on**, output
and input manual offset **0**. (These are global preferences, not part of
the project file.)

### 2. Per quantum - 64, then 256, then 1024

```sh
Q=64                                                 # then 256, then 1024
pw-metadata -n settings 0 clock.force-quantum $Q
pw-dump > ~/px-latency/reaper/q$Q/pw-dump.json       # what PipeWire reports, for comparison
```

1. Start REAPER fresh (quit it between quanta - it takes the JACK
   latencies when it opens the device) and open `q$Q/latency.rpp`.
2. **First time only, check the routing** - the one thing this kit cannot
   pin down for you is how REAPER's input numbers map under
   pipewire-jack. REAPER's "Input 3" is REAPER's own third JACK input
   port; which PHASE X64 channel feeds it depends on the connections. Check
   them in a terminal:

   ```sh
   pw-link -l | grep -A3 -i reaper
   ```

   Expected: `REAPER:in3` <- the PHASE X64 pro input's third channel (port
   name like `capture_AUX2`), `REAPER:in5` <- its fifth (`capture_AUX4`),
   `REAPER:out1`/`out2` -> the pro output's first two (`playback_AUX0`,
   `playback_AUX1`). Anything else (another card first, or no links):
   connect them that way in qpwgraph, or pick the inputs in the tracks'
   record-input menus. Also check in REAPER: track `stim`'s routing window
   shows a hardware output to 1/2 (if not, add it there) and no master
   send; `in3`/`in5` show record input "Input 3"/"Input 5" (mono).
3. Press **Home** (cursor to 0), **Ctrl+R** (record), wait until the play
   cursor is past the end of the stim item (4.6 s), **Space** (stop), keep
   the take, **Ctrl+S** (save), quit REAPER.
4. Read the result:

   ```sh
   python3 "$PX" rpp ~/px-latency/reaper/q$Q/latency.rpp | tee ~/px-latency/reaper/q$Q/rpp.txt
   ```

### 3. Restore

```sh
pw-metadata -n settings 0 clock.force-quantum 0
amixer -c PhaseX64 cset name='In 3/4 Capture Source' 'Aux'
amixer -c PhaseX64 cset name='Direct 1/2 Playback Switch' on    # only if it was on before
```

About 15 minutes in total.

## What to send back

- `q64/rpp.txt`, `q256/rpp.txt`, `q1024/rpp.txt` - per track the raw
  offset in the recorded file, REAPER's compensation and the **remaining
  error** (that last number is the result).
- `q64/pw-dump.json`, `q256/pw-dump.json`, `q1024/pw-dump.json`.
- What `pw-link -l | grep -A3 -i reaper` showed, if anything had to be
  reconnected.

Reading it: in5's error is the USB part REAPER did not compensate, in3 -
in5 the converter part (expected ~52.9). By the PipeWire 1.6.10 source
neither part is reported anywhere, so the expectation is in5 ~= 220 +- 13
and in3 ~= 273 at every quantum (`../REPORT.md`, same section, last
paragraph). in5 ~= 0 and in3 ~= 53 would mean something compensates the
USB part after all.

## Why the saved project and not a stem render

REAPER writes each recorded item's position and start offset (`POSITION`,
`SOFFS`) into the saved project, next to the path of the raw recorded file.
The raw file's offset against the stimulus, minus the shift REAPER applied,
is REAPER's error, with no render settings involved. A stem render gets the
same number but depends on several dialog settings (source "selected
tracks (stems)", bounds from 0, mono, no normalisation or dither, no tail),
any one of which can silently shift or alter the output. It is still a
useful cross-check if you want one:

*File -> Render*: Source "Selected tracks (stems)" with `in3` and `in5`
selected, Bounds "Entire project", Directory `stems`, File name `$track`,
WAV 24 bit, 48000 Hz, Mono. Then:

```sh
python3 "$PX" analyze ~/px-latency/reaper/q$Q/stems/in3.wav --channels 1
python3 "$PX" analyze ~/px-latency/reaper/q$Q/stems/in5.wav --channels 1
```

The `offset` printed for each should equal the matching "remaining error"
from `rpp`.
