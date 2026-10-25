# REAPER latency check (pipewire-jack)

Does REAPER, running on PipeWire through `pipewire-jack`, place a recording
where it belongs? The raw-ALSA measurement (`../REPORT.md`, "Round-trip
latency, raw ALSA (2026-10-24)") found ~4.55 ms (USB pipeline) + ~52.9
samples (converters) of round-trip delay that the driver does not report.
This kit records the same impulse stimulus on two tracks in one take:

- **in3** - In 3/4 on `Loopback 1/2`: Out 1/2's analog output (USB +
  converters);
- **in5** - capture 5, the bit-exact digital tap of Out 1/2 (USB only).

Then `px_latency.py rpp` reads, from the saved project, the recorded
take of each track and prints what is left over after REAPER's own latency
compensation, in samples. 0 = REAPER compensated exactly; positive = the
recording sits late. (REAPER 7.81 applies its compensation inside the
recorded file and leaves the item at position 0 / start offset 0, so this
is the raw offset of the take against the stimulus.)

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
   latencies when it opens the device) and open `q$Q/latency.rpp`. REAPER
   reopens the *last* project: check that the path in the status line
   (top right) says `q$Q/recorded`, and open the right project if not.
   The status line also shows the latencies JACK reports, `NNNspls
   ~x/yms`: at quantum 64, 256, 1024 they were 64, 256, 1024 samples each
   way.
2. **Check the routing.** REAPER's `out1`..`out4` auto-connect to the
   *default sink* - on a machine with several audio devices that is not
   the PHASE X64, and the recording is then silent. Look:

   ```sh
   pw-link -l | grep -A1 -E '^REAPER:(out|in[35])$'
   ```

   Expected: `REAPER:in3` <- the PHASE X64 pro input's third channel
   (`capture_AUX2`), `REAPER:in5` <- its fifth (`capture_AUX4`) - that
   part was right here without any change. If `REAPER:out1`/`out2` go
   elsewhere, move them (state-changing, run it yourself; `P` is the
   PHASE X64's `alsa_output...pro-output-0` node name from `pw-link -o`):

   ```sh
   A=alsa_output.<other device>.pro-output-0
   P=alsa_output.<PHASE X64>.pro-output-0
   for i in 1 2 3 4; do pw-link -d REAPER:out$i $A:playback_AUX$((i-1)); done
   pw-link REAPER:out1 $P:playback_AUX0
   pw-link REAPER:out2 $P:playback_AUX1
   ```

   **Redo this after anything that restarts REAPER's audio** (opening
   another project does not, but changing a Preferences > Audio setting
   does, and so does restarting REAPER). Also check in REAPER: track
   `stim`'s routing window shows one hardware output 1/2 and no master
   send; `in3`/`in5` take Input 3 / Input 5 (mono).
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

- `q64/rpp.txt`, `q256/rpp.txt`, `q1024/rpp.txt` - per track the offset
  of the recorded take and the **remaining error** (the last number is
  the result).
- `q64/pw-dump.json`, `q256/pw-dump.json`, `q1024/pw-dump.json`.
- What `pw-link -l | grep -A3 -i reaper` showed, if anything had to be
  reconnected.

Reading it: in5's error is the USB part REAPER did not compensate, in3 -
in5 the converter part (~52.8). Result of the first run (2026-10-25) and
what to expect: `../REPORT.md`, "REAPER through pipewire-jack".

### With the `latency.internal.*` values installed

If the node properties from `linux/wireplumber/51-phasex64-pro-audio.conf`
are active (check with `pw-dump`: both PHASE X64 nodes carry
`latency.internal.rate` / `.ns`; REAPER's status line then shows a larger
latency than the bare quantum, e.g. `~2.6/5.7ms` at quantum 64), the
expectation changes: **in3 about 0, in5 about -54** - the digital tap
does not pass the converters, whose ~53 samples are now compensated.
Results of the first run: `../REPORT.md`, "F2 values re-measured". A
user-level copy of the conf is *added* to the system one (WirePlumber
merges fragments from all locations, a same-named file does not replace
the other), which is harmless for these values.

### Optional: the same take without compensation

To see how much REAPER takes off: switch *Preferences > Audio > Recording >
"Use audio driver reported latency"* off, **then redo the `pw-link` step
above** (the preference change reconnects the outputs), record again,
switch it back on. The new `recorded/*.wav` files can be read with
`px_latency.py analyze FILE --channels 1`. At quantum 64 that gave 354 vs
255 samples (one take each; REAPER takes about the reported 128 off).

## Why the saved project and not a stem render

The saved project names the raw recorded file of each take, and REAPER's
compensation is already inside that file (item `POSITION` and `SOFFS`
stay 0), so the file's offset against the stimulus is REAPER's remaining
error, with no render settings involved. A stem render gets the same
number but depends on several dialog settings (source "selected
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
