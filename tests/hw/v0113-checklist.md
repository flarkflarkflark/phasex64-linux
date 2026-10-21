# v0.1.13 manual test checklist (log-levels round)

What changed: `px_status_read()`'s "status read failed: bRequest=0x49
..." message moved from `dev_err_ratelimited` to `dev_dbg_ratelimited`
(see `analysis/NOTES.md`, "0x49 status-read stalls", and the v0.1.13
log-levels round). No other driver behaviour changed. This checklist
is for the user to run by hand - nothing here is run by the assistant.

Do every step in order. Record the actual result at each `->`, not just
pass/fail - some steps (B, D) are specifically about what *doesn't*
happen.

## A. Install + reload

```sh
rm -rf /tmp/px64-pkg && cp -r linux /tmp/px64-pkg
cd /tmp/px64-pkg
makepkg -si
systemctl --user stop pipewire.socket pipewire-pulse.socket pipewire pipewire-pulse wireplumber
sudo rmmod snd_usb_phasex64
sudo modprobe snd_usb_phasex64
cat /sys/module/snd_usb_phasex64/version   # expect 0.1.13
dkms status                                # expect snd-usb-phasex64/0.1.13, installed
```

## B. MIDI without any audio stream

Do this **before** restarting PipeWire, while nothing holds the device.

1. Find the card number and confirm no PCM substream is open:

   ```sh
   amidi -l                                           # find hw:N,0
   cat /proc/asound/cardN/pcm0p/sub0/status           # expect "closed"
   cat /proc/asound/cardN/pcm0c/sub0/status           # expect "closed"
   ```

2. Connect the DIN loopback cable, MIDI OUT -> MIDI IN.
3. Terminal 1 (dump input):

   ```sh
   amidi -p hw:N,0 -d
   ```

4. Terminal 2 (send):

   ```sh
   amidi -p hw:N,0 -S '90 3C 7F 80 3C 00'
   amidi -p hw:N,0 -S 'F0 7D 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F F7'
   ```

5. Expected if MIDI is stream-independent (per the v0.1.13 item #6
   report - no code change was made for this, it was already expected
   to work this way): the bytes appear in terminal 1, and
   `/proc/asound/cardN/midi0` Rx/Tx counters increase. Record the
   result either way - a clean pass here settles a previously-open
   question with no further driver work needed; a failure means the
   question needs to be reopened.
6. Ctrl+C terminal 1.

## C. Start PipeWire again

```sh
systemctl --user start pipewire.socket pipewire-pulse.socket wireplumber
```

## D. dmesg checks

```sh
sudo dmesg -C   # clear, so each check below only shows new lines
```

After each of the following, run `sudo dmesg | grep -i phasex64` and
confirm **no "status read failed" line appears**:
- right after the module load in step A;
- after restarting PipeWire (step C);
- after a REAPER start, then stop;
- after opening, then closing, the panel.

Then turn on dynamic debug and repeat once, to confirm the message
still exists and still fires at the same moments, just one level down:

```sh
echo 'module snd_usb_phasex64 +p' | sudo tee /sys/kernel/debug/dynamic_debug/control
systemctl --user restart pipewire.socket pipewire-pulse.socket wireplumber
sudo dmesg | grep -i phasex64   # "status read failed" lines SHOULD appear now
echo 'module snd_usb_phasex64 -p' | sudo tee /sys/kernel/debug/dynamic_debug/control
```

## E. Function check

- Playback: play something, confirm audio comes out.
- Capture: before recording, per CLAUDE.md's capture/rate rules -
  confirm `Digital Input Lock` is off and the rate is 48kHz (e.g.
  `rate_probe=1` or `amixer -c PhaseX64 cget` on `Digital Input Lock`).
  Record a short analog-loopback capture, confirm it looks sane.
- Live codec change: while REAPER is recording, change `IN 1/2 Capture
  Source` in the panel; confirm the stream doesn't glitch/drop and the
  new source takes effect.
- MIDI LEDs + monitor: open the panel, confirm IN/OUT LEDs blink with
  traffic and the MIDI monitor window shows it, same as the existing
  MIDI panel acceptance testing.

## F. Rollback to 0.1.12, if anything above is off

```sh
REPO="path/to/your/phasex64-checkout"
rm -rf /tmp/px64-rollback
git -C "$REPO" worktree add /tmp/px64-rollback main
cp -r /tmp/px64-rollback/linux /tmp/px64-pkg
git -C "$REPO" worktree remove /tmp/px64-rollback
```

This checks out `main` (0.1.12) into a separate, temporary worktree -
the repo's own current branch/working tree is untouched throughout.
Then:

```sh
cd /tmp/px64-pkg
makepkg -si
systemctl --user stop pipewire.socket pipewire-pulse.socket pipewire pipewire-pulse wireplumber
sudo rmmod snd_usb_phasex64
sudo modprobe snd_usb_phasex64
cat /sys/module/snd_usb_phasex64/version   # expect 0.1.12 again
systemctl --user start pipewire.socket pipewire-pulse.socket wireplumber
```

## Results (2026-10-20) — PASSED

**A.** Reload OK: `/sys/module/snd_usb_phasex64/version` = 0.1.13, `dkms
status` shows 0.1.13 installed. Note: the first `rmmod` attempt failed
with "in use" - an already-running panel instance with no visible
window, plus its own `alsactl monitor` child process, was holding
`controlC0`. Killing the panel process wasn't enough by itself; the
orphaned `alsactl monitor` child kept holding the device until it was
killed too, only then did `rmmod` succeed. Tracked as a backlog item
(see NOTES.md "Open items" and point 3 below) - not a v0.1.13 driver
issue, a pre-existing panel process-lifecycle gap.

**B.** MIDI without any audio stream (PipeWire fully stopped,
`pcm0p`/`pcm0c` both "closed", DIN loopback cable, `amidi -p hw:0,0
-d`/`-S`): **nothing received.** `midi0`'s Tx counter grew by only 3
bytes per `amidi -S` invocation regardless of payload size (9 -> 12
after sending an 18-byte SysEx message) - i.e. only the first USB-MIDI
framing packet's worth went out, Rx stayed at 0 throughout.

Repeated with *only* a capture stream running
(`arecord -D hw:0,0 -c 6 -f S32_LE -r 48000 -t raw /dev/null`, no
playback): Tx +18, Rx +18, the 18-byte SysEx came back intact.

**Conclusion: the device firmware only services MIDI while an audio
stream is active; a capture stream alone is enough.** This settles item
#6 differently from the code-only read in the step-1 report - the
driver's MIDI *code path* is independent of PCM state (confirmed
correct), but the *firmware* isn't. See NOTES.md "MIDI without an
audio stream" for the full writeup.

**C./D.** After the reload, after a PipeWire restart, after a REAPER
start/stop, after panel open/close, and during the arecord/amidi test
above: no "status read failed" lines in `dmesg | grep -i phasex64`.
With dynamic debug turned on and a PipeWire restart repeated, the debug
lines appear as expected - including one `err=-110` (ETIMEDOUT, ~1s =
`CTRL_TIMEOUT`) on the status read inside the PCM open path, about
1.05s after PipeWire closed both streams; `pcm open` still returned 0
and the stream was running again shortly after. See NOTES.md "0x49
status-read stalls" for this new observation and its effect (~1s extra
delay on a PipeWire restart, harmless).

Gotcha hit while running this: the message logs against the *USB
device* (`usb 3-2: status read failed ...`), not the ALSA card name, so
`dmesg | grep -i phasex64` alone does **not** find it - needed
`dmesg | grep -iE "status read|0x49"` instead. Documented in NOTES.md
and the README troubleshooting line.

**E.** Playback and simultaneous recording both worked (IN 3/4 =
`Loopback 1/2`, recording OUT 1/2's actual output); a live codec change
made during that recording ("Applied live" in the panel, no hang) took
effect with the recording continuing uninterrupted; MIDI LEDs and the
monitor window both worked correctly in the panel.

**Rollback:** not needed - nothing in A-E required falling back to
0.1.12.
