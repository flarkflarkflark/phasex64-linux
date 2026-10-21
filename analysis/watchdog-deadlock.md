# v0.1.11.1: watchdog teardown deadlock

2026-10-16. Step 1 only. Branch `fix/watchdog-deadlock`, directly from main
`9f1482f`. Minimal driver fix, offline tests, W=1 build; hardware tests pending.
No module operation, sudo, device access, service change or push was performed.
DKMS installation and v0.1.12 implementation wait for separate approval.

## Confirmed defect in v0.1.11

Original `px_stop_streaming()` at main line 1024 (comments omitted):

```c
static void px_stop_streaming(struct px_dev *px)
{
	if (!px->running)
		return;
	px->running = false;
	px_kill_audio_urbs(px);
	cancel_delayed_work_sync(&px->cap_watchdog_work);
	px->stop_jiffies = jiffies;
}
```

Its callers hold `px->mutex`. For example `px_pcm_close()` at main line 1417
contains, in this order (unrelated statements omitted):

```c
mutex_lock(&px->mutex);
/* mark this substream inactive/unprepared */
if (!px->play.prepared && !px->cap.prepared)
	px_stop_streaming(px);
mutex_unlock(&px->mutex);
```

Original `px_cap_watchdog_fn()` at main line 863 contains these relevant
statements, in order (other statements omitted):

```c
schedule_delayed_work(&px->cap_watchdog_work, msecs_to_jiffies(CAP_WD_CHECK_MS));
/* read active and last genuine completion under cap.lock */
if (!active)
	return;
if (stall_ms < CAP_WD_STALL_MS)
	return;
mutex_lock(&px->mutex);
/* recheck active/disconnected, perform recovery, unlock */
```

Yes: this is blocking `mutex_lock`, not trylock. If the watchdog has passed its
initial checks, then teardown acquires the mutex first, teardown waits for the
watchdog to finish while the watchdog waits for teardown to release the mutex.
Clearing `active` does not fix this: the watchdog's second check is behind the
blocking acquisition. This is a reachable lock-order deadlock, not merely a
slow USB request, although it was not deliberately reproduced on hardware here.

## Minimal fix and proof of the removed wait cycle

The watchdog now uses:

```c
if (!mutex_trylock(&px->mutex))
	return; /* next tick was queued above; stop can drain us */
```

Also reject recovery if `!px->running` after acquisition, in addition to the
existing inactive/disconnected guards. Keep the unconditional 50 ms self-requeue,
recovery sequence, attempt limits and synchronous stop cancellation unchanged.
Do not add a new stopping state or refactor lifecycle in this hotfix.

There are only two mutex acquisition orders:

1. **Stop owns the mutex first.** The watchdog never waits for it. It either
   exits at an earlier check or fails trylock and returns. Stop can finish
   `cancel_delayed_work_sync()` and remove the pending next tick. Stop holds no
   callback spinlock while cancelling, so the watchdog's short `cap.lock` read
   cannot depend on a lock held by that cancellation.
2. **Watchdog owns it first.** Stop waits *before* entering the stop helper, not
   inside synchronous cancellation. Recovery executes and releases the mutex;
   it does not call stop or wait for the cancelling thread. Once stop acquires
   the mutex, any later watchdog tick is case 1. A recovery already in progress
   may still delay teardown by finite USB operations/timeouts; it is not the
   circular mutex/work wait.

If a mixer operation owns the mutex, the watchdog also returns and tries on its
next scheduled tick. Recovery may be deferred by contention; it is not disabled.
The added running check excludes recovery after transport has stopped even if
an active flag is stale. No ALSA position, RIAA or codec behavior changes.

The cancellation assumption is the documented
[cancel_delayed_work_sync contract](https://docs.kernel.org/core-api/workqueue.html#c.cancel_delayed_work_sync):
it drains executing work and self-requeueing, provided independent producers
cannot race teardown. The only other producer here is capture trigger START;
normal PCM lifecycle serializes START versus its STOP/free, start checks running,
and card disconnect prevents new user activity. No new producer is introduced.
The watchdog has no blocking device-mutex acquisition after this patch.

### Call-path audit

Locations refer to the fixed source `linux/snd-usb-phasex64/phasex64.c`.

| Path | Location | Why this wait cycle cannot occur |
|---|---|---|
| Transport stop | `px_stop_streaming`, 1027 | Holds mutex, cancels worker which now never waits on it |
| PCM trigger STOP/PAUSE/SUSPEND | `px_pcm_trigger`, 1513 | Atomic context: clears active, only non-sync cancellation; no wait on worker |
| PCM sync_stop | `px_pcm_sync_stop`, 1611 | Waits for period notifications, not this work; completions do not acquire px->mutex |
| Close | `px_pcm_close`, 1419 | Makes this stream inactive, calls common stop if last prepared direction |
| hw_free | `px_pcm_hw_free`, 1463 | Same last-direction stop; peer-active case does not synchronously cancel this work |
| hw_params/rate change | `px_pcm_hw_params`, 1444 | Common stop under mutex when no prepared peer |
| Failed prepare/start | prepare 1490, start fail label | Common stop under prepare's mutex; no worker self-cancel |
| Suspend | `px_suspend`, 2665 | PCM suspension precedes common stop under mutex |
| Disconnect | `px_disconnect`, 2633 | Card disconnected, then common stop; extra sync cancellation at 2652 is outside mutex |

This proves removal of **this mutex/work wait cycle** on all listed paths. It
does not claim that arbitrary USB-controller failures, scheduler starvation or
unrelated ALSA bugs can never hang. Hardware stop/close, suspend and unplug
validation remain necessary; do not label source/model evidence as hardware PASS.

## Verification performed

- `python3 tests/hw/test_watchdog_deadlock.py`: **6/6 PASS**. Source guards cover
  trylock, requeue ordering, running guard, cancellation and all stop callers.
  A deliberately small finite wait-graph model explores both lock acquisition
  orders: old variant 13 states / one deadlocked terminal state; fixed variant
  13 states / zero deadlocked states. USB operations are assumed to finish;
  this is not a model of the entire kernel or a real stall-injection test.
- `python3 tests/hw/test_codec_resync.py`: **4/4 PASS**.
- `bash linux/dev-build.sh`: **W=1 PASS**, no warnings/errors, kernel
  `7.2.4-arch1-2`. No install/load was done.
- `modinfo -F version ~/px64/snd-usb-phasex64.ko`: **0.1.11.1**.
  srcversion `5E43BA15858D4C4EF6C40BC`.
  SHA-256 `2200ef0d1157531a87faba6028caed237a28198a71dca9f47858eb6f46f3147b`.
- MODULE_VERSION, dkms.conf and PKGBUILD agree on 0.1.11.1. This is source/build
  metadata only, not a claim about the installed DKMS module; that is step 2.

The build script replaced the old build directory's generated artifacts after a
full backup to a local backup directory. The previous
`0.1.11+live-resync4` module and source remain recoverable there. The running
module was untouched. This main-based hotfix has **no live_resync parameter**;
do not pass diagnostic parameters to it. Live mixer support is still step 3.

## User-run load and 20-cycle test (not executed by the agent)

Save/close REAPER and the panel first. Never use the S/PDIF loopback. Keep audio
services off. Inspect openers/refcount; do not force removal if busy:

```bash
systemctl --user stop pipewire.socket pipewire-pulse.socket pipewire pipewire-pulse wireplumber
fuser -v /dev/snd/*
cat /sys/module/snd_usb_phasex64/refcnt
```

Only with no PHASE X64 openers and refcount 0, replace the loaded diagnostic:

```bash
if sudo rmmod snd_usb_phasex64; then
    sudo insmod ~/px64/snd-usb-phasex64.ko dyndbg=+p
fi
cat /sys/module/snd_usb_phasex64/version
```

Require version **0.1.11.1** before testing. In a separate terminal, observe the
kernel log (do not clear it):

```bash
sudo journalctl -kf -o short-monotonic
```

Check Clock Source = Internal. Each cycle below checks live Digital Input Lock
off and GET_CUR = 48000 *before* opening capture. If the rate is different, stop
and report it; do not bypass the check or introduce a digital loopback. The
`Digital Input Rate` control performs GET_CUR while idle; while running it only
returns a cache, so a running read alone is not verification.

```bash
amixer -c PhaseX64 cget name='Clock Source'
(
    set -euo pipefail
    px64_test_dir=$(mktemp -d "$HOME/px64-logs/watchdog-0.1.11.1-XXXXXX")
    printf 'Logs: %s\n' "$px64_test_dir"
    for px64_iteration in {1..20}; do
        amixer -c PhaseX64 cget name='Digital Input Lock' > "$px64_test_dir/lock-$px64_iteration.txt"
        amixer -c PhaseX64 cget name='Digital Input Rate' > "$px64_test_dir/rate-$px64_iteration.txt"
        if ! rg -q ': values=off$' "$px64_test_dir/lock-$px64_iteration.txt" ||
           ! rg -q ': values=48000$' "$px64_test_dir/rate-$px64_iteration.txt"; then
            printf 'STOP: lock/rate check failed on cycle %s; inspect logs.\n' "$px64_iteration"
            exit 1
        fi
        if timeout --signal=INT --kill-after=3s 10s \
            arecord -q -D hw:PhaseX64 -f S32_LE -r 48000 -c 6 \
            -t raw -d 2 /dev/null 2> "$px64_test_dir/capture-$px64_iteration.log"; then
            printf '%s/20 exited normally\n' "$px64_iteration"
        else
            px64_rc=$?
            printf 'STOP: cycle %s returned %s; retain logs.\n' "$px64_iteration" "$px64_rc"
            exit 1
        fi
    done
    cat /sys/module/snd_usb_phasex64/refcnt
)
```

Every capture trigger arms the 50 ms watchdog; two seconds is long enough for
many ticks. Healthy ticks need not escalate or print watchdog errors. Require
20 normal exits, no blocked-task/lockdep/Oops reports, no leftover PCM openers,
and module refcount back to 0. A timeout is a failure, not a successful close.
`timeout` cannot rescue a task stuck in an uninterruptible kernel wait; do not
keep launching tests or force-unload if a process remains stuck.

### Close during a stall

There is **no safe deterministic forced-stall hook** in the main-based driver,
and none is added to this minimal fix. Muting the analog input or SIGSTOPing
arecord is not a device stall. Do not use S/PDIF, USB fault injection or a new
altsetting experiment to manufacture one. The old deadlock is exercised only
by the offline model here, not claimed reproduced physically.

If a known/reproducible stall occurs with the normal setup, repeat a capture
with a wall-clock timeout (after the same idle lock/rate checks):

```bash
timeout --signal=INT --kill-after=3s 5s \
    arecord -D hw:PhaseX64 -f S32_LE -r 48000 -c 6 -t raw /dev/null
cat /sys/module/snd_usb_phasex64/refcnt
```

Observe the watchdog warning in the separate log terminal; Ctrl-C while recovery
is active exercises close immediately, or the five-second SIGINT exercises it
automatically. Exit 124 is expected for an ordinary timeout of this unbounded
capture, unlike the finite 20-cycle test. Exit 137, a leftover task or a kernel
blocked-task report is a failure. Save the kernel log and elapsed close time.
If no genuine stall is observed, mark this case **not exercised**, not PASS.
Suspend/disconnect races likewise need explicit user-run hardware coverage;
ordinary 20-cycle capture does not test those paths physically.

Report step-1 hardware results and wait for approval before DKMS installation
or any work on v0.1.12. No design-branch rebase is performed in this step.
