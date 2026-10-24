# tests/hw - hardware test tools

Scripts and checklists for testing the driver on a real PHASE X64. Anything
here that opens the device (`aplay`/`arecord` on `hw:PhaseX64`, REAPER, the
`alsa` subcommand below, ...) is run by hand by the user, never by an
assistant session. Results go into `REPORT.md`.

## Latency measurement (`px_latency.py`)

Measures the round-trip delay the driver does not report to ALSA, to the
sample. It opens playback and capture on `hw:PhaseX64`, links them
(`snd_pcm_link`), starts both in the same trigger and plays 16 single-sample
impulses (-6 dBFS) on Out 1/2. Because both streams start at the same ALSA
instant, the position of an impulse in the capture minus its position in
the playback stream is exactly the delay hidden from ALSA. Two channels of
the same recording are compared:

- **ch 5** - the bit-exact digital tap of Out 1/2 (no cable): USB and
  firmware pipeline only.
- **ch 3** - In 3/4 on `Loopback 1/2`, i.e. Out 1/2's real analog output
  (DAC of codec 0 -> ADC of codec 1): the full analog round trip.
- **analog - tap** - the converters and the analog path (the WM8776
  datasheet gives a typical filter group delay of 22 samples for the ADC
  and 19 for the DAC).

Each run also prints the extra delay the driver reports
(`snd_pcm_avail_delay()` minus the ring buffer; 0/0 today) and, once the
device is idle again, the rate it reports (`Digital Input Rate`, a live
`GET_CUR` while not streaming) - a run is marked invalid if that does not
match the requested rate.

Requirements: `python3` with numpy, alsa-lib (`libasound.so.2`), `amixer`.
No other Python modules.

### What the script does and doesn't do

- It never writes a mixer control. It only reads them (`amixer cget`) and,
  if one is wrong, stops before opening anything and prints the exact
  `amixer cset` command for you to run.
- It stops if `Digital Input Lock` is on (live read - it first checks in
  `/proc/asound/PhaseX64/pcm0[pc]/sub0/status` that nothing is streaming).
- It stops at once with the owner's PID if anything holds the card. The
  PCMs are opened non-blocking, so it never hangs waiting for PipeWire.
- It closes both PCMs on every exit path, Ctrl-C, SIGTERM and SIGHUP
  included. A run with no capture data or playback space for 2 s is
  aborted as "stalled"; an xrun marks that run FAILED and the next one starts.

### Setup

- **S/PDIF coax loopback cable unplugged.** With it, capture follows the
  S/PDIF clock (`REPORT.md`, "Windows USB trace (2026-10-14)"); the script
  refuses to run while `Digital Input Lock` is on.
- 48 kHz for the main run, a 44.1 kHz run afterwards. (88.2 and 96 kHz are
  accepted too, but at small periods this Python loop may not keep up.)
- Mixer state - set it yourself, the script only checks:

  ```sh
  amixer -c PhaseX64 cset name='In 3/4 Capture Source' 'Loopback 1/2'
  amixer -c PhaseX64 cset name='Direct 1/2 Playback Switch' off
  amixer -c PhaseX64 cset name='PCM 1/2 Playback Switch' on
  amixer -c PhaseX64 cset name='Clock Source' 'Internal'
  ```

  (Patched Out 1/2 into In 3/4 with a cable instead? Pass `--cabled`.)
- **Speakers and headphones down**: every run plays 16 clicks at -6 dBFS
  on the Out 1/2 jacks.

### Run

The script writes its WAVs and logs into the current directory, so run it
from a scratch directory outside the checkout; set `PX` to your checkout's
`tests/hw/px_latency.py`.

```sh
PX=/path/to/phasex64-linux/tests/hw/px_latency.py
mkdir -p ~/px-latency && cd ~/px-latency

# offline check first - must print "selftest: PASS" (no device access)
python3 "$PX" selftest

# stop PipeWire, sockets included so nothing respawns it
systemctl --user stop pipewire.socket pipewire-pulse.socket pipewire pipewire-pulse wireplumber
cat /proc/asound/PhaseX64/pcm0p/sub0/status    # expect "closed"
cat /proc/asound/PhaseX64/pcm0c/sub0/status    # expect "closed"

# 48 kHz buffer sweep: 5 buffer sizes x 3 runs, captures saved (~1.5 min)
python3 "$PX" alsa --rate 48000 --buffers 64x2,64x3,128x2,256x2,512x2 --reps 3 --save r48 2>&1 | tee r48.log

# 44.1 kHz: 2 buffer sizes x 3 runs (~40 s)
python3 "$PX" alsa --rate 44100 --buffers 64x2,256x2 --reps 3 2>&1 | tee r44.log

# kernel messages from the runs (rate warnings, xruns, watchdog)
journalctl -k --since "-15 min" | grep -i phasex64 > kernel-phasex64.txt

# PipeWire back on
systemctl --user start pipewire.socket pipewire-pulse.socket pipewire pipewire-pulse wireplumber
```

Each run takes about 6 s (4.8 s of audio, plus open/prepare and the idle
rate readback); the whole block takes about 3 minutes.

### Reading the output

Per run: one offset line for ch 5 (tap) and one for ch 3 (analog), with
polarity, correlation and per-impulse spread; then the driver-reported
extra delay, `analog - tap` and the device rate check. A summary table
closes each invocation. What to look for:

- **tap** constant across buffer sizes and runs. If it changes from run to
  run, the start skew between the streams varies - that matters for
  latency compensation, so note it.
- **analog - tap** constant, expected around the datasheet's 41 samples
  plus a little.
- **spread** 0-1 everywhere. More means drift, an xrun or a dropout
  inside the run.
- **corr** above 0.3. Lower means no signal on that channel (wrong
  capture source, PCM 1/2 off, wrong `--analog-ch`).

### What to send back

- `r48.log` and `r44.log`, complete.
- `kernel-phasex64.txt` - only the `phasex64` lines, not the full kernel log.
- Anything you heard or saw that the logs don't show (clicks at the wrong
  moment, a run that seemed to hang).
- Keep the `r48-*.wav` captures (about 4 MB each); send them only if asked.
  Two runs at the same buffer size serve later as the baseline for a null
  test (`null` subcommand).

### Other subcommands (offline, no device access)

```sh
python3 "$PX" gen --channels 1 --out stim.wav   # mono stimulus, e.g. for REAPER
python3 "$PX" analyze take.wav --channels 1     # offsets in a recording rendered from t=0
python3 "$PX" null A.wav B.wav --ch 3           # null test of two saved captures
python3 "$PX" rpp project.rpp                   # REAPER's compensation error, saved project
python3 "$PX" selftest --keep DIR               # also keeps the synthetic WAVs in DIR
```

The selftest also runs the `alsa` subcommand's real I/O loop against a
simulated device (virtual time, the driver's 1 ms playback / 32-frame
capture shape): it proves the loop's logic and bookkeeping, not its
real-time behaviour on hardware.

For the REAPER check through pipewire-jack see `reaper-latency/README.md`.
