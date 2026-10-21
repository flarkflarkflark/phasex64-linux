# v0.1.12 phase 3a hardware validation checklist

**Status (2026-10-17): scenario (a) as originally written below caused a
real kernel hang on first hardware attempt (hard power-off required).
Root cause, fix, and a narrower, safer replacement first step are in
`analysis/v0.1.12-plan.md` section 12 - read that before touching this
checklist again.** The fix (bounded+killable `sync_stop`, live-apply
cancellation moved earlier in the teardown sequence, a pre-resubmit
stream-state re-check, and a second review round covering lock-ordering,
worst-case timing, diagnostics and an `in_elapsed` audit - section 12
"Review round 2") is committed; this exact checklist has not been
re-validated against it yet. Do not re-run scenario (a) as
REAPER-plus-two-rapid-clicks until section 12's narrower replacement
sequence (arecord/aplay, amixer instead of the panel, one click before
two) has passed first.

**Before any step below**: run a second terminal with
`journalctl -kf > ~/px64-test.log` (or `sudo dmesg -w > ~/px64-test.log`)
for the whole session, so kernel logs survive a hard power-off. **Note**:
neither this logging command nor `arecord -d N`'s own duration limit nor
an external `timeout` wrapper protects against a hang - a process stuck
in a `TASK_UNINTERRUPTIBLE` (D-state) kernel wait cannot run any
userspace code, including its own timers, until that wait returns. The
only actual protection is the driver's own bounded/killable wait inside
`px_pcm_sync_stop()` (section 12). Logging separately just means a hang,
if one still happens, leaves evidence instead of needing to be
reconstructed from memory after a hard power-off.

For `design/v0.1.12`, build `0.1.12-pre1` (`analysis/v0.1.12-plan.md`
section 11: retry/fallback/XRUN around the live codec apply from sections
1-10; section 12: the hang this scenario caused, and the fix). Guided
session - the user runs every command that touches the module or the
device; the assistant never does. `tests/hw/test_live_apply.py` automates
the parts that are safe to automate (reading dmesg/sysfs); it is run by
the user, same as everything else here.

**Capture/rate test rules (`CLAUDE.md`) apply throughout**: Digital Input
Lock must be `off` and the live rate must be `48000` before *every* step
below, and no S/PDIF loopback cable for this session. Run this before each
lettered scenario (a-e), not just once at the start:

```sh
python3 tests/hw/test_live_apply.py preflight
```

Do not proceed on a step if this doesn't say `Preflight OK.`

## Step 0: load the build

```sh
modinfo ~/px64/snd-usb-phasex64.ko | grep ^version      # expect 0.1.12-pre1
systemctl --user stop pipewire pipewire-pulse wireplumber \
    pipewire.socket pipewire-pulse.socket
sudo rmmod snd-usb-phasex64
# dyndbg=+p - same convention as the earlier capture/rate sessions
# (tests/hw/REPORT.md "Step 2 results", dyndbg=+p rate_probe=1) - enables
# the dev_dbg() staged/committed traces this checklist's gap/coalescing
# measurements depend on.
sudo insmod ~/px64/snd-usb-phasex64.ko dyndbg=+p
modinfo snd-usb-phasex64 | grep ^version                # expect 0.1.12-pre1 again, now loaded
dmesg | tail -20                                        # or: journalctl -k -n 20 --no-pager
```

Report back: does `dmesg` show the probe/claim lines cleanly, no warnings?
Then PipeWire back on (`systemctl --user start ...`), confirm the card
shows up normally, and REAPER (or your player of choice) can open it ALSA-
direct (PipeWire off again) for the scenarios below, which need a running
stream.

## (a) Functional: live apply within ~0.5s, no stream restart

With a capture+playback stream running (REAPER, ALSA `hw:PhaseX64`,
PipeWire stopped) - for each toggle below, note the time you made it, then
after a few seconds run `python3 tests/hw/test_live_apply.py gaps --since
"2 minutes ago"` and read off the gap for that generation:

1. **In 1/2 source `Phono` <-> `Aux`**, hardware switch left on `Phono`
   throughout (per `analysis/NOTES.md` "Hardware matrix": this makes `Aux`
   a mismatch, i.e. silence - that silence *is* the proof the codec change
   actually landed, not just a side effect). Toggle a few times, listen/
   watch the panel meter: does it go silent switching to `Aux` and come
   back switching to `Phono`, *without* REAPER's transport stopping or the
   stream needing to be reopened?
2. **Out 1/2 Direct/PCM.** Toggle, confirm playback routing audibly
   changes without a stream restart.
3. **Out 3/4 PCM.** Same.

Record: did every toggle's gap (from `gaps`) come in under 500ms? Any
`*** OVER 0.5s ***` lines, or any `staged but never committed` lines that
aren't explained by a later scenario (fault injection, coalescing)?

## (b) Coalescing: 10 fast clicks -> at most 2 restarts

Still streaming. Pick one control (e.g. In 1/2 source) and flip it back
and forth 10 times as fast as you can - a tight loop is fine too:

```sh
for i in $(seq 1 10); do
  amixer -c PhaseX64 cset name='In 1/2 Capture Source' $(( i % 2 == 0 ? 1 : 2 ))
done
sleep 2
python3 tests/hw/test_live_apply.py coalesce --since "2 minutes ago"
```

Expect "OK (<=2 commits)" - design doc section 3's coalescing fence should
collapse the burst into at most the one already-running attempt plus one
more for whatever the state was when it finished, not 10 separate restarts.

## (c) Fault injection: `live_fail_step=1..5`

Stream running, In 1/2 source as the toggle. One step at a time, not all
five in a loop - inspect dmesg after each before moving to the next:

```sh
STEP=1   # repeat this whole block with STEP=2, 3, 4, 5 in turn
python3 tests/hw/test_live_apply.py fault $STEP
amixer -c PhaseX64 cset name='In 1/2 Capture Source' 2   # Aux, or whatever it isn't currently
sleep 3   # 3 attempts worst case ~170ms settle + backoff 100+300ms each, well under 3s
python3 tests/hw/test_live_apply.py scan --since "1 minute ago"
dmesg | tail -40   # or: journalctl -k -n 40 --no-pager
```

Per `analysis/v0.1.12-plan.md` section 11's own scenario table, expect:

| step | expected dmesg pattern |
|---|---|
| 1 (status read) | 3x "attempt N/3 failed at status read (fault injection)", then "live apply: codec change not applied after 3 attempts, stream restored..." (fallback succeeded - steps 1-4 don't touch the codec-free fallback) |
| 2 (codec write) | same shape, "failed at codec write (fault injection)" |
| 3 (SET_CUR rate) | same shape, "failed at SET_CUR rate (fault injection)" |
| 4 (status write) | same shape, "failed at status write (fault injection)" |
| 5 (URB resubmit) | 3x "failed at URB resubmit (fault injection)", then the *fallback's own* resubmit also fails ("live apply fallback: fault injection - forcing resubmit to fail" / "resubmit failed"), then "live apply: fallback also failed... forcing an XRUN" - the DAW should show an underrun/dropout here and need to re-prepare, not just show silence |

After step 5 specifically: does the stream actually recover (REAPER
re-preparing/restarting capture+playback on its own, or needing a manual
stop/start)? That's the expected, correct behavior for this step, not a
failure - record what you observe either way.

`scan` must come back clean (no hang/oops/BUG) after every step. Finish
with:

```sh
python3 tests/hw/test_live_apply.py fault 0   # disable fault injection
```

## (d) Stop/close during a retry: must finish within ~2s

A self-contained stream (not REAPER this time, so `time` can wrap an exact
stop) - run the capture in the background, force retries, toggle, then
immediately kill the capture process while the retry is still in flight:

```sh
arecord -D hw:PhaseX64 -f S32_LE -c 6 -r 48000 -d 30 -q /tmp/d-scenario.wav &
REC=$!
sleep 1
python3 tests/hw/test_live_apply.py fault 1   # force retries (status read fails every attempt)
amixer -c PhaseX64 cset name='In 1/2 Capture Source' 2   # Aux, or whatever it isn't currently
time kill $REC && wait $REC 2>/dev/null
python3 tests/hw/test_live_apply.py fault 0
python3 tests/hw/test_live_apply.py scan --since "1 minute ago"
```

Record the `time` output. **Corrected expectation** (plan doc section 12
"Review round 2", item 2 - the previous "~2s" estimate here only counted
the settle sleep and backoff, not real control-transfer timeouts): with
`fault 1` forcing the status-read step to fail every attempt via fault
injection (not a real USB timeout), each attempt fails fast and this
should still finish in a couple of seconds. But the *true* worst case,
if a step ever fails via a genuine `-ETIMEDOUT` instead of fault
injection, is bounded at ~92 seconds (3 retried attempts plus fallback,
every control transfer timing out for real) - still finite, not a hang,
just much slower than "under 2s". Record the actual `time` output either
way; a result anywhere up to roughly a minute is consistent with the fix
working correctly, not evidence of a new problem. `scan` must still come
back clean.

## (e) Watchdog: still capture-only, no false triggers during live apply

No fault injection this time (`fault 0`, confirmed). Stream running, do a
handful of ordinary (non-failing) toggles as in (a), then:

```sh
python3 tests/hw/test_live_apply.py scan --since "5 minutes ago"
journalctl -k --since "5 minutes ago" --no-pager | grep -i "capture watchdog"
```

Two things worth knowing going in, from reading the code (not yet
hardware-confirmed): `px_live_apply_restart()`'s kill-settle-resubmit
sequence (`live_settle_ms` default 170ms, plus several control transfers)
can plausibly run long enough to cross the watchdog's own stall threshold
(`CAP_WD_STALL_MS` = 150ms) during a *normal, successful* live apply - so
a **stray "capture watchdog: no px_cap_complete()..." warning log line
coinciding with a live-apply toggle is not necessarily a bug**, just the
watchdog noticing the deliberate gap. What *would* be a real problem: the
watchdog actually taking its own recovery action concurrently (look for
"cycling intf1 alt 0 -> alt 1 + resubmitting capture and feedback URBs" in
the same window) - `mutex_trylock()` should prevent that (the watchdog
skips its tick silently if `px_live_apply_work()` holds the mutex), but
that's exactly what this scenario is here to confirm, not assume. Record
which (if either) you see.

## Clean-up

```sh
sudo rmmod snd-usb-phasex64
sudo modprobe snd-usb-phasex64        # back to the installed 0.1.11.5 DKMS module, no dyndbg
modinfo snd-usb-phasex64 | grep ^version   # expect 0.1.11.5
systemctl --user start pipewire.socket pipewire-pulse.socket pipewire pipewire-pulse wireplumber
```

## Reporting back

Per scenario: pass/fail, gap times (a), commit count (b), dmesg excerpts
for each fault step (c), the `time` output (d), and whatever `scan`/
watchdog-grep found (e). Paste `dmesg`/`journalctl -k` excerpts rather
than summarizing them from memory - exact wording matters here (which
step failed, which generation, exact ms).
