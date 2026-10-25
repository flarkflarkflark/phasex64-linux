# snd-usb-phasex64 hardware test session — HALTED at Test 1

Session date: 2026-10-11. Module already loaded by the user (fixed version,
commit `9490eff`). Stopped per instructions at the first crash-like symptom.

References in this report to the Windows "decomp", `msnkphsu.sys.c` or
`CPLPhaseX64USB.exe.c` (and `FUN_...` names) are to the official Windows
driver, decompiled locally with Ghidra (not distributed) - see
`analysis/README.md`.

## Status

| Test | Status |
|---|---|
| 1. Channel map | **Halted — hit a kernel WARNING, see below** |
| 2. Clock/rate | Not run |
| 3. Glitch detection | Not run |
| 4. Stress (open/close) | Not run |
| 5. Mixer | Not run |

## What happened

Test 1 opens capture first (and triggers it), then opens/plays/closes
playback once per channel. On the very first playback open — i.e. the
first time playback is opened *while capture is already running* — the
kernel logged:

```
WARNING: phasex64.c:246 at px_fill_play_urb+0x1f8/0x240 [snd_usb_phasex64], CPU#14: swapper/14/0
!s->sub
```

followed by a full stack trace through `px_play_complete` → URB
completion softirq. **This is not a panic** — it's the `WARN_ON_ONCE(!s->sub)`
guard added in the earlier fix, firing exactly as designed: it turned what
would otherwise be the same NULL-pointer dereference the original driver
panicked on into a harmless warning (playback silently produces silence
for that stream instead of crashing). The system stayed fully responsive
throughout and afterward — module still loaded, device still enumerates,
`/proc/asound/PhaseX64/pcm0p,0c/sub0/hw_params` both cleanly back to
`closed`, no hang, no further warnings on the next three channels (because
`WARN_ON_ONCE` only ever fires once per boot, not because the bug stopped
happening — see root cause).

Visible symptom at the user level: every `aplay` call failed immediately
with `write error: Input/output error`, so **no actual audio was
transmitted on any of the 4 channels** — the "channel map" data captured
(`data/channel_map_results.json`) is just the analog noise floor
(~-96 to -98 dBFS on inputs 1-4, exactly 0 on inputs 5/6) and does not
reflect anything about the real channel routing. That test needs to be
re-run once this is fixed.

## Root cause (new, pre-existing bug — not introduced by the earlier lock fix)

`px_pcm_open()` (`phasex64.c`, around the `mutex_lock(&px->mutex)` block):

```c
mutex_lock(&px->mutex);
if (px->running)
	err = snd_pcm_hw_constraint_single(rt, SNDRV_PCM_HW_PARAM_RATE,
					   px->rate);
if (!err)
	s->sub = sub;
mutex_unlock(&px->mutex);
```

Per the kernel header doc comment for `snd_pcm_hw_constraint_single()`
(`include/sound/pcm.h`):

> Return: Positive if the value is changed, zero if it's not changed, or a
> negative error code.

So a **positive** return means success (the constraint was applied and it
changed something) — only a **negative** return is failure. `if (!err)`
is wrong: it treats any non-zero return, including the common positive
"changed" case, as failure. Whenever `px->running` is already true (i.e.
whenever one direction is opened while the other is already streaming —
completely normal full-duplex use, confirmed in the kernel log as
`err=1` on every playback open in this run), `s->sub` is silently never
set, while `px_pcm_open()` still returns the positive `err` up to ALSA
core — which treats any non-negative return as success, since it only
checks `err < 0`. The open "succeeds", `prepare()` and `trigger(START)`
run normally (`trigger` doesn't look at `sub`, only sets `active`), and
the stream ends up exactly in the `active == true && sub == NULL` state
that used to panic the machine — now caught by `WARN_ON_ONCE` instead.

This is independent of the race-condition fix from the earlier session
(that fix closed the *timing* window between trigger/close and the
completion handler; this bug is a plain logic error in the open() success
check, reachable on every full-duplex open, deterministically, no race
needed). It's plausible this was a **second, independent contributor** to
the original REAPER crash, alongside the race already fixed, since REAPER
routinely opens both directions with one already running.

Minimal fix (not applied — editing driver source is forbidden for this
test session): change the check to `if (err >= 0) s->sub = sub;`, or
simpler, treat only negative as failure throughout that function.

## Fix applied (follow-up, outside the hardware test session)

`px_pcm_open()` now only treats a negative return from
`snd_pcm_hw_constraint_single()` as failure, normalizes positive "changed"
returns to 0, and assigns `s->sub = sub` under `s->lock` (consistent with
`close()`). `px_pcm_trigger()`'s START/PAUSE_RELEASE case now also checks
`s->sub == sub` under the same lock before setting `active = true`,
refusing with `-EIO` + `dev_err()` otherwise — defense in depth so this
class of bug can never again silently produce `active == true` with a
wrong or NULL `sub`. Released as v0.1.2. Full audit of every other
`err`-returning call in the file (all confirmed to already use the
correct 0/negative convention, so no other fix was needed) is in the
`phasex64: fix sub never set when opening second direction during duplex`
commit message.

**This bug is the most likely root cause of the original REAPER panic**:
REAPER opens both directions, and whichever direction it opens *second*
hits exactly this path (`px->running` already true from the first
direction) — `sub` silently never gets set for it, then `trigger(START)`
sets `active = true` regardless, and the first URB completion for that
direction dereferences a NULL `sub`. The race-condition fix from the
earlier session (per-stream locking, `sync_stop`) didn't cause this and
doesn't fix it either — it's what turned this *specific* manifestation
from a panic into a `WARN_ON_ONCE`, which is how it was caught here
without taking the machine down. The two bugs are independent: the lock
fix closes a *timing* window; this fix closes a *deterministic* logic
error. Both needed fixing for full-duplex use to be safe.

This hardware test session should resume from Test 1 once the fixed
module (v0.1.2) is loaded.

## Kernel log of the original crash (2026-10-11) — every non-`dev_dbg` line

```
okt 11 01:10:22 <host> kernel: ------------[ cut here ]------------
okt 11 01:10:22 <host> kernel: !s->sub
okt 11 01:10:22 <host> kernel: WARNING: phasex64.c:246 at px_fill_play_urb+0x1f8/0x240 [snd_usb_phasex64], CPU#14: swapper/14/0
okt 11 01:10:22 <host> kernel: Modules linked in: snd_seq_midi snd_seq_midi_event snd_usb_phasex64(OE) snd_rawmidi ntfs3 ccm rfcomm snd_seq_dummy snd_hrtimer snd_seq snd_seq_device algif_hash algif_skcipher af_alg bnep vfat fat snd_sof_amd_acp70 snd_sof_amd_acp63 snd_sof_amd_vangogh snd_sof_amd_rembrandt snd_sof_amd_renoir snd_sof_amd_acp snd_sof_pci snd_sof_xtensa_dsp snd_sof snd_sof_utils snd_pci_acp7x snd_pci_ps snd_soc_acpi_amd_match snd_amd_sdw_acpi amd_atl snd_hda_codec_alc269 soundwire_amd soundwire_generic_allocation iwlmvm snd_hda_codec_realtek_lib intel_rapl_msr soundwire_bus intel_rapl_common snd_hda_scodec_component snd_soc_sdca snd_hda_codec_nvhdmi snd_hda_codec_atihdmi snd_hda_codec_generic snd_hda_codec_hdmi mac80211 snd_soc_core snd_compress ac97_bus snd_hda_intel snd_pcm_dmaengine kvm_amd ptp snd_acp_pci snd_hda_codec snd_amd_acpi_mach pps_core snd_acp_legacy_common ee1004 hid_multitouch wl(POE) <vendor_module> libarc4 snd_pci_acp6x snd_hda_core btusb kvm r8169 snd_intel_dspcfg snd_pci_acp5x btmtk btrtl snd_intel_sdw_acpi
okt 11 01:10:22 <host> kernel:  snd_rn_pci_acp3x irqbypass realtek snd_hwdep sp5100_tco ucsi_acpi btbcm <vendor_module> snd_acp_config phy_package aesni_intel iwlwifi typec_ucsi btintel snd_pcm snd_soc_acpi gf128mul i2c_piix4 mdio_devres roles bluetooth rapl wmi_bmof firmware_attributes_class k10temp typec i2c_smbus snd_timer snd_pci_acp3x cfg80211 libphy i2c_hid_acpi snd thunderbolt i2c_hid mdio_bus amd_pmc <vendor_module> soundcore ccp mousedev joydev mac_hid dm_mod ntsync crypto_user i2c_dev pkcs8_key_parser amdgpu drm_buddy amdxcp i2c_algo_bit drm_exec drm_panel_backlight_quirks gpu_sched nvme drm_suballoc_helper nvme_core serio_raw drm_display_helper nvme_keyring cec nvme_auth nvidia_drm(OE) drm_ttm_helper ttm <vendor_module> <vendor_module> sparse_keymap nvidia_uvm(OE) nvidia_modeset(OE) rfkill video wmi nvidia(OE) aead
okt 11 01:10:22 <host> kernel: CPU: 14 UID: 0 PID: 0 Comm: swapper/14 Tainted: P        W  OE       7.2.4-arch1-2 #1 PREEMPT(full)  ceab5b7eb60a89a58d616500ee9fef4aad8789ba
okt 11 01:10:22 <host> kernel: Tainted: [P]=PROPRIETARY_MODULE, [W]=WARN, [O]=OOT_MODULE, [E]=UNSIGNED_MODULE
okt 11 01:10:22 <host> kernel: Hardware name: <AMD Ryzen 7 5800H laptop, AMD Renoir/Cezanne xHCI (USB 3.1); model/BIOS redacted>
okt 11 01:10:22 <host> kernel: RIP: 0010:px_fill_play_urb+0x1f8/0x240 [snd_usb_phasex64]
okt 11 01:10:22 <host> kernel: Code: 08 48 8b 3c 24 e8 18 48 ba cc 48 89 d8 48 8b 6c 24 18 48 8b 5c 24 10 4c 8b 74 24 30 4c 8b 7c 24 38 48 83 c4 40 e9 93 79 70 cb <0f> 0b 43 8d 04 7f 31 f6 31 db 8d 14 85 00 00 00 00 e8 02 4f b9 cc
okt 11 01:10:22 <host> kernel: RSP: 0018:ffffcf2ac039ce20 EFLAGS: 00010046
okt 11 01:10:22 <host> kernel: RAX: 0000000000000000 RBX: 0000000000000240 RCX: ffff8b8818d1db40
okt 11 01:10:22 <host> kernel: RDX: 0000000000000001 RSI: 0000000000000000 RDI: ffff8b86cf343800
okt 11 01:10:22 <host> kernel: RBP: ffff8b8818d1da00 R08: 0000000000000020 R09: ffffffff927a4a00
okt 11 01:10:22 <host> kernel: R10: ffff8b86426310d0 R11: 0000000000000000 R12: 0000000000000000
okt 11 01:10:22 <host> kernel: R13: 0000000000000000 R14: ffff8b86ed0c7650 R15: 0000000000000030
okt 11 01:10:22 <host> kernel: FS:  0000000000000000(0000) GS:ffff8b8d7c0ad000(0000) knlGS:0000000000000000
okt 11 01:10:22 <host> kernel: CS:  0010 DS: 0000 ES: 0000 CR0: 0000000080050033
okt 11 01:10:22 <host> kernel: CR2: 0000338c03034ffc CR3: 000000019e5bb000 CR4: 0000000000f50ef0
okt 11 01:10:22 <host> kernel: PKRU: 55555554
okt 11 01:10:22 <host> kernel: Call Trace:
okt 11 01:10:22 <host> kernel:  <IRQ>
okt 11 01:10:22 <host> kernel:  ? srso_alias_return_thunk+0x5/0xfbef5
okt 11 01:10:22 <host> kernel:  ? led_trigger_blink_oneshot+0x88/0xc0
okt 11 01:10:22 <host> kernel:  px_play_complete+0x4a/0xd0 [snd_usb_phasex64 8666afae94cffcd3f5b7fb476035a4fad7246ff8]
okt 11 01:10:22 <host> kernel:  __usb_hcd_giveback_urb+0xa0/0x120
okt 11 01:10:22 <host> kernel:  usb_giveback_urb_bh+0xc0/0x140
okt 11 01:10:22 <host> kernel:  process_one_work+0x19f/0x370
okt 11 01:10:22 <host> kernel:  bh_worker+0x1d2/0x1f0
okt 11 01:10:22 <host> kernel:  ? sched_clock_cpu+0xf/0x1f0
okt 11 01:10:22 <host> kernel:  tasklet_hi_action+0x13/0x30
okt 11 01:10:22 <host> kernel:  handle_softirqs+0xe8/0x2a0
okt 11 01:10:22 <host> kernel:  __irq_exit_rcu+0xb0/0x100
okt 11 01:10:22 <host> kernel:  common_interrupt+0x85/0xa0
okt 11 01:10:22 <host> kernel:  </IRQ>
okt 11 01:10:22 <host> kernel:  <TASK>
okt 11 01:10:22 <host> kernel:  asm_common_interrupt+0x26/0x40
okt 11 01:10:22 <host> kernel: RIP: 0010:cpuidle_enter_state+0xbb/0x400
okt 11 01:10:22 <host> kernel: Code: 00 00 e8 68 b3 e8 fe e8 23 ee ff ff 48 89 c5 0f 1f 44 00 00 31 ff e8 54 3e e6 fe 45 84 ff 0f 85 48 01 00 00 fb 0f 1f 44 00 00 <45> 85 f6 0f 88 9f 01 00 00 44 89 f1 48 2b 2c 24 48 6b d1 68 48 89
okt 11 01:10:22 <host> kernel: RSP: 0018:ffffcf2ac021fe78 EFLAGS: 00000246
okt 11 01:10:22 <host> kernel: RAX: ffff8b8d7c0ad000 RBX: 0000000000000002 RCX: 0000000000000000
okt 11 01:10:22 <host> kernel: RDX: 000010018d09fe95 RSI: fffffffcbbc71cf9 RDI: 0000000000000000
okt 11 01:10:22 <host> kernel: RBP: 000010018d09fe95 R08: ffff8b8d7c0ad000 R09: ffffffff91c19480
okt 11 01:10:22 <host> kernel: R10: 0000000000000232 R11: ffffffffffffffff R12: ffff8b86452a0800
okt 11 01:10:22 <host> kernel: R13: ffffffff91c19480 R14: 0000000000000002 R15: 0000000000000000
okt 11 01:10:22 <host> kernel:  ? cpuidle_enter_state+0xac/0x400
okt 11 01:10:22 <host> kernel:  cpuidle_enter+0x31/0x50
okt 11 01:10:22 <host> kernel:  do_idle+0x1cc/0x2e0
okt 11 01:10:22 <host> kernel:  cpu_startup_entry+0x29/0x30
okt 11 01:10:22 <host> kernel:  start_secondary+0x119/0x150
okt 11 01:10:22 <host> kernel:  common_startup_64+0x13e/0x151
okt 11 01:10:22 <host> kernel:  </TASK>
okt 11 01:10:22 <host> kernel: ---[ end trace 0000000000000000 ]---
```

## Not yet produced (blocked on resuming the session)

- Channel map table (output n → input m) — invalid data from this run, see above.
- Clock/rate table (44100/48000/88200/96000).
- Glitch detection count/timestamps.
- Stress test (200x/200x/50x open-close) result.
- `amixer -c PhaseX64 contents` listing.

## Next steps (your call)

1. Fix `px_pcm_open()`'s success check in a normal (non-hardware-test) session, rebuild, reload, then resume this test plan from Test 1.
2. Or: continue hardware testing now, avoiding the trigger condition (open playback only when capture is *not* already running, and vice versa) — this would mask the bug rather than exercise real full-duplex use, so channel-map/glitch results would be less representative of actual DAW use (which always has both directions open).
3. PipeWire is currently still stopped (I haven't restarted it, in case you want to poke at the device manually first).

---

# v0.1.3 — full test session, completed

Session resumed and completed in full: all 5 tests ran, none halted. Module
verified as v0.1.3 by `/sys/module/snd_usb_phasex64/{version,srcversion}`
before starting (first load attempt was still the 0.1.2 binary — caught
and corrected before any test ran).

## Hardware I/O (physical panels)

Full writeup in `analysis/NOTES.md`; summary relevant to interpreting the
tests below:

- **Front**: Phono/Aux RCA inputs for Ch.1/2 and Ch.3/4, Mic/Line
  XLR-combo (Ch.1, own gain + 48V LED), HiZ/Line jack (Ch.2, own gain),
  headphone jack + volume.
- **Rear**: Power (9V DC), USB-B, MIDI Out/In, Digital (S/PDIF coax)
  Out/In, Outputs Ch.3/4 (RCA), Inserts (MIC/Line + HiZ/Line), Outputs
  Ch.1/2 (jacks). (Corrected 2026-10-21: the Phono/Aux RCA inputs were
  listed on the rear here; they are on the front.)
- **Side switches, hardware-only** (no USB/ALSA control exists for any of
  these): `48V`, `Ch.1 Line/Mic`, `Ch.2 Line/HiZ`, `Phones Ch. 1/2|3/4`,
  `Ch.1/2 Aux/Phono`, `Ch.3/4 Aux/Phono`. These sit upstream of the ADC
  mux and must be kept in agreement with whichever ADC-mux source the
  mixer controls select (see "Open issues" below) — the driver has no way
  to read them back.
- **This session's loopback cabling, for reference**: outputs 3/4 → RCA →
  Phono/Aux inputs 3/4 (switch on Aux), **and** a coax S/PDIF cable from
  Digital Out to Digital In — the latter wasn't in the original setup
  notes and is what explains the channel-map result below.

## Logging caveat — read this before trusting any "0 WARNs" claim below

**`dev_dbg` tracing was off for this entire session.** Dynamic debug
settings don't persist across a module reload, and re-enabling them
requires `sudo`, which wasn't run until after all 5 tests were done. So
none of the `pcm open/close/prepare/trigger` trace lines exist for tests
1-5 — only unconditional `WARN_ON_ONCE`/`dev_err`/`dev_warn_ratelimited`
output would have appeared, since those aren't dynamic-debug gated.

Separately, and more seriously: my own background `journalctl -k -f`
capture into `kernel-v0.1.3.log` turned out to be **worthless** for this
session. `flark` is not in the `systemd-journal` group, and
`journalctl -k --since "15 minutes ago"` silently returned
`-- No entries --` despite real activity in that window — a one-shot,
time-ranged query fails the same way a live `-f` follow does, silently
rather than with an error. `kernel-v0.1.3.log` has 0 lines, and that fact
by itself proves nothing either way.

**The actual verification is the user's `sudo dmesg` capture**, filtered
on `phasex|1-2:|cut here|WARNING|retry`, covering boot counter 19915
(v0.1.3 load) through 22107 (end of session, dynamic debug re-enabled
right at the end). Between 19915 and 22107 — all of tests 1 through 5 —
there are **zero matching lines**: zero `WARN_ON_ONCE`, zero `dev_err`
(ctrl transfer failures), zero `dev_warn_ratelimited` (ctrl retries), zero
`resubmit failed`. The 22107 block (after dynamic debug was back on) shows
a duplex open with `Playback err=0` and no WARN, confirming the `sub` fix
is doing its job live. The `-110`/`-32` errors and both `WARN_ON_ONCE`
hits from the previous session's log are from v0.1.1, before any of the
px_pcm_open/settle-delay/retry fixes — not from this session.

So: **0 WARNs, 0 ctrl retries, 0 ctrl errors, 0 resubmit failures,
confirmed by `sudo dmesg`**, for the whole v0.1.3 session. The "stop at
the first WARN/oops/hang" instruction was never triggered.

## 1. Channel map

No `aplay`/`arecord` I/O errors on any of the 4 playback channels (the
v0.1.1/v0.1.2 bug this was blocked on last time). RMS/dominant frequency
per input, playback ch1-4 at -12 dBFS / 1 kHz, 48000 Hz, noise floor
around -94 to -97 dBFS on unconnected inputs:

| Output ch | → Input ch | RMS (dBFS) | Dominant freq |
|---|---|---|---|
| 1 | **5** | -16.67 | 1000.0 Hz |
| 2 | **6** | -16.67 | 1000.0 Hz |
| 3 | **3** | -20.18 | 1000.0 Hz |
| 4 | **4** | -20.09 | 1000.0 Hz |

(All other inputs per output: noise floor, -93.6 to -96.7 dBFS, no tone.)

**Resolved**: output ch1→input5 and ch2→input6 is an external S/PDIF
loopback — confirmed a coax cable connects Digital Out to Digital In on
the rear panel (see "Hardware I/O" below). Playback channels 1/2 are
mirrored to Digital Out and loop straight back to capture 5/6; the signal
being *louder* than the analog 3/4 path (-16.67 dBFS vs -20.09/-20.18
dBFS) is exactly what a clean digital link with near-zero loss vs. a real
RCA cable's attenuation would look like. No driver action is involved —
see the "Digital output source" research in `analysis/NOTES.md` for why:
we could not confirm this device exposes any host-configurable digital-out
source select at all, so the simplest explanation (an always-on fixed
mirror of playback 1/2) stands.

Outputs 3/4 → inputs 3/4 matches the stated analog RCA loopback exactly,
with a small (~-73 dB relative) crosstalk bleed between the two pairs,
typical for an RCA stereo pair.

## 2. Clock / rate

All 4 native rates, ch3 loopback, measured via FFT with parabolic
interpolation:

| Rate | Measured freq | RMS | Within ±0.5 Hz? | `hw_params` during playback |
|---|---|---|---|---|
| 44100 | 1000.000 Hz | -19.82 dBFS | ✅ | `rate: 44100 (44100/1)` |
| 48000 | 1000.000 Hz | -19.82 dBFS | ✅ | `rate: 48000 (48000/1)` |
| 88200 | 1000.000 Hz | -19.83 dBFS | ✅ | `rate: 88200 (88200/1)` |
| 96000 | 1000.000 Hz | -19.83 dBFS | ✅ | `rate: 96000 (96000/1)` |

Exact to the measurement's resolution at every rate. `/proc/asound/PhaseX64/pcm0p/sub0/hw_params`
correctly reported the live rate (and matching `period_size`/`buffer_size`)
during each run, confirmed by reading it mid-playback, not just after.

## 3. Glitch detection (5 min, full duplex, 48000, 997 Hz on ch3)

**My first detector implementation was broken** — a per-block phase-
unwrapping approach flagged 27,100 of 30,100 blocks (90%) even on a
mathematically perfect, synthetically-generated sine with zero glitches,
which is how I caught it before trusting the result against the real
recording. Replaced it with a global least-squares amplitude/phase fit
over the whole recording, then residual RMS per 10ms block (outlier =
median + 10×MAD) — verified against the same synthetic perfect-sine test
first (residual ~9 orders of magnitude below signal, as expected for a
clean sine).

Re-running that corrected detector against the *already-captured*
recording (no need to redo the 5-minute hardware run):

- First pass (0.5s edge trim): 107 flagged blocks, but **all** of them
  fell in the first 1s or last 0.5s of the window — onset/offset
  transients the global fit can't model (it assumes constant amplitude
  for the whole 300s), not real glitches. Confirmed by checking the
  timestamp distribution: zero flagged blocks between 1.0s and 299.5s.
- Second pass (1.5s edge trim): **2 flagged blocks**, at 128.57s and
  145.34s. Both are marginal — residual RMS only ~3% over threshold, with
  the overall residual floor already sitting at -78.8 dB below the
  signal. A real dropped/inserted sample or audible dropout would spike
  the residual far more dramatically than a 3% threshold crossing. I'm
  reporting these as detected rather than suppressing them, but I don't
  read them as confirmed defects.
- `aplay -v` / `arecord -v`: **0 xrun/overrun/underrun lines** in either
  stream's output.
- `sudo dmesg` for this window: 0 WARN, 0 retry, 0 error lines (per the
  logging section above).

## 4. Stress (the old crash pattern)

The exact scenario that produced the v0.1.1 WARN (`px_fill_play_urb`
dereferencing a NULL `sub` because the second direction's `open()` never
set it while the first was already running). Checked `sudo dmesg` after
each of the three sub-stages before proceeding to the next, per the
"stop at the first WARN" rule:

| Stage | Cycles | Errors | Hangs | Continuous stream died early? |
|---|---|---|---|---|
| (a) continuous 4ch playback + capture open/close | 200 | 0 | — | No |
| (b) continuous 6ch capture + playback open/close | 200 | 0 | — | No |
| (c) both directions opened/closed simultaneously | 50 | 0 | 0 | — |

450 open/close cycles total, zero `aplay`/`arecord` errors, zero process
hangs needing a forced kill, zero WARNs in `sudo dmesg`, device left
enumerated and idle (`hw_params` closed on both PCMs) afterward.

## 5. Mixer

`amixer -c PhaseX64 contents`: 19 controls — 17 read/write boolean
switches, 1 read/write enum (`Clock Source`), 1 read-only boolean
(`Digital Input Lock`).

Read/toggle/read-back/restore cycle run on all 17 writable switches
(`Clock Source` held back separately, see below):

| numid | Name | Before | Toggled to | Read after toggle | Restored |
|---|---|---|---|---|---|
| 1 | In 1/2 Mic-HiZ-Line Capture Switch | on | off | off | on |
| 2 | In 1/2 Aux Capture Switch | off | on | on | off |
| 3 | In 1/2 Phono Capture Switch | off | on | on | off |
| 4 | In 1/2 Loopback Out 3/4 Capture Switch | off | on | on | off |
| 5 | In 1/2 Sound Module Capture Switch | off | on | on | off |
| 6 | Direct 1/2 Playback Switch | off | on | on | off |
| 7 | Direct 1/2 Mono Playback Switch | off | on | **off** | off |
| 8 | PCM 1/2 Playback Switch | on | off | off | on |
| 9 | PCM 1/2 Mono Playback Switch | off | on | on | off |
| 10 | In 3/4 Aux Capture Switch | on | off | off | on |
| 11 | In 3/4 Phono Capture Switch | off | on | on | off |
| 12 | In 3/4 Loopback Out 1/2 Capture Switch | off | on | on | off |
| 13 | In 3/4 Sound Module Capture Switch | off | on | on | off |
| 14 | Direct 3/4 Playback Switch | off | on | on | off |
| 15 | Sound Module 3/4 Playback Switch | off | on | on | off |
| 16 | PCM 3/4 Playback Switch | on | off | off | on |
| 17 | PCM 3/4 Mono Playback Switch | off | on | on | off |

numid=7 reading back `off` after being set `on` is **not a bug** —
`px_switch_put()` deliberately clears the mono-direct bit when its
prerequisites (mic input selected + direct monitoring on) aren't met, and
`Direct 1/2 Playback Switch` (numid=6) was off at that moment. Documented
in the driver source and `analysis/NOTES.md` ("needs Mic + Direct").

**A second, more serious interaction, which I caused and then had to
fix**: `In 1/2 Phono Capture Switch` (3) and `In 3/4 Phono Capture Switch`
(11) are hardware-exclusive within their shared ADC-mux register ("phono
excludes the others"). Testing them one switch at a time, each one's own
toggle-then-restore read back correctly — but restoring Phono to `off`
from a state where it was the *only* bit set triggered the driver's
auto-mute path (`if (!(val & 0x1f)) val = WM_ADC_MUTE;`), which silently
also cleared `In 1/2 Mic-HiZ-Line Capture Switch` (1) and `In 3/4 Aux
Capture Switch` (10) as collateral damage. My script's per-switch restore
check only verified the bit it had just touched, so it reported "OK" for
switches 1 and 10 even though they'd already been correctly restored
*then silently undone later* by switch 3/11's test. Caught by diffing the
full 19-control state against the original baseline afterward (not by
the per-switch script output), and corrected numid 1 and 10 back to `on`.
Final state verified to match the original baseline on all 19 controls.

**Clock Source** (held back from the generic loop, tested separately with
explicit confirmation first):
- Read `0` (Internal). No PCM stream open (`hw_params` closed on both).
- Switched to `1` (Digital Input): command returned immediately (well
  under an 8s timeout, no hang), read back confirmed `1`. `Digital Input
  Lock` correctly read `off` (nothing connected). Waited 5s, device
  stayed responsive (`arecord -l` worked, both PCMs still closed).
- Restored to `0` (Internal): returned immediately, read back confirmed.
- Recovery proof: 5s loopback on ch3 @ 48000 measured **1000.000 Hz**,
  -19.82 dBFS, both `aplay`/`arecord` returned 0 — clock fully recovered.

## Open issues found during mixer testing, for v0.1.4 (not fixed here)

1. **`px_switch_put()` changes other controls' values without
   `snd_ctl_notify()` for them.** The ADC-mux interlock (phono-exclusive)
   and the auto-mute-when-empty path both silently change bits belonging
   to *other* `snd_kcontrol`s than the one `put()` was called for, but
   only the control that was actually written gets a change
   notification. Any listener that caches control state (`alsamixer`,
   the `phasex64-panel` GUI, PipeWire's device state) will show stale
   values for the collaterally-changed switches until it happens to
   re-read them for some other reason. This is exactly the kind of bug
   that made my own mixer test's per-switch "OK" checks misleading — a
   real control surface would have the same blind spot.
2. **Mutually-exclusive input selections should probably be one ALSA enum
   control, not independent boolean switches — re-checked against the
   physical hardware and the Windows panel, recommendation holds.** The
   current `In 1/2 {Mic-HiZ-Line, Aux, Phono, Loopback, Sound Module}
   Capture Switch` set models what's actually a single-select ADC-mux
   input as five separate booleans with hidden exclusivity rules enforced
   ad hoc in `px_switch_put()` (phono excludes the others; nothing stops
   two *non*-phono bits like Aux and Loopback both being set at once,
   which this test session didn't check and the current switch model
   doesn't prevent). Checked the Windows panel decomp for how it actually
   presented these: `CPLPhaseX64USB.exe.c` uses a
   `CDlgGroupRadioButtonIterator` class (`:19698-20010`) for grouping
   buttons, consistent with these having been presented as a
   mutually-exclusive radio group in the UI, not independent checkboxes —
   supports the enum recommendation.

   One nuance the hardware photos surfaced that the enum design needs to
   account for: **Aux/Phono/Mic/Line/HiZ are *also* real physical
   switches** (`Ch.1/2 Aux/Phono`, `Ch.3/4 Aux/Phono`, `Ch.1 Line/Mic`,
   `Ch.2 Line/HiZ` — see "Hardware I/O" above), separate from and
   upstream of this software ADC-mux control. So the mixer's
   `{...} Capture Switch` controls are *not* selecting which physical
   jack pair is live — they're selecting which already-hardware-gated
   source feeds the ADC. An ALSA enum here is still the right shape
   (matches the Windows radio-group UI, makes the exclusivity structural
   instead of ad hoc), but its items should probably be documented as
   "requires the matching physical switch position" rather than treated
   as fully self-contained — e.g. selecting the `Phono` enum item while
   the physical `Ch.1/2` switch is set to `Aux` would select the correct
   ADC-mux bit but read a signal that was never RIAA-corrected. The
   driver has no way to read the physical switch position back to warn
   about this.

---

# v0.1.4/v0.1.5 — capture stall investigation (CLOSED, unreproduced)

## Symptom

During the v0.1.4 session (mixer-refactor testing), capture broke
completely: every `arecord` attempt — standalone, no mixer interaction
involved — failed with `read error: Input/output error` (`EIO`) after
~550-650ms, well short of the requested duration. Live-polled
`/proc/asound/card3/pcm0c/sub0/status` showed `state: RUNNING` with
**`hw_ptr` stuck at `0`** for the entire failure window, then `closed`.
Playback was unaffected throughout (`aplay` always completed cleanly,
exact duration, exit 0). 100% reproducible across 4+ attempts in that
one session. `sudo dmesg` for the whole window (v0.1.3 load through the
failures) showed **zero** `WARN`/`dev_err`/`resubmit failed` lines —
nothing from the driver indicated anything was wrong.

The failure has **not recurred** since — not in the user's own A/B/A
bisection (v0.1.3 OK, v0.1.4 failed, v0.1.3 OK, v0.1.4 OK again), not in
any test run in this or later sessions, including the exact original
failing sequence (`control_sweep.py` → `enum_verify.py`, full duplex,
playback tones running during capture) repeated **10 times in a row
(~12 minutes, 110 capture attempts)** with dynamic debug and the new
tracing active. Zero failures.

## Four hypotheses, tested, all disproven

Each was a concrete, falsifiable claim, tested directly against hardware
rather than argued from code reading alone:

1. **udev's `alsactl restore` falls back to `alsactl init` for the new
   enum controls (no entry in `/var/lib/alsa/asound.state` yet), and the
   generic `/usr/share/alsa/init/default` script lands on `Mute`.**
   Confirmed the *mechanism* is real: `/usr/lib/udev/rules.d/90-alsa-restore.rules`
   does run `alsactl --export restore $CARD` on hotplug; `man alsactl`
   confirms restore-falls-back-to-init-on-mismatch has been the default
   since alsa-utils 1.0.18; `/var/lib/alsa/asound.state`'s `PhaseX64`
   section genuinely has no entries for `In 1/2/3/4 Capture Source` (old
   switch-era dump only); live `sudo alsactl init PhaseX64` output matched
   `/usr/share/alsa/init/00main`'s generic-fallback path exactly
   (`"Found hardware..."` / `"Hardware is initialized using a generic
   method"`). **But**: the one failing-load `amixer contents` dump
   (`amix-A-015.txt`) actually in hand shows **normal, non-Mute values**
   (`In 1/2 Capture Source: 0` = Mic-HiZ-Line, `In 3/4: 1` = Aux,
   `Clock Source: 0` = Internal) — disproven by the most direct evidence
   available. Confirmed again live: `sudo alsactl init PhaseX64` did not
   change either control at all.
2. **Muting both ADC sources stops the device from sending capture data
   altogether.** Tested all 4 combinations (both muted, `In 1/2` only,
   `In 3/4` only, neither) — every combination captured cleanly, full
   rate, zero errors. Content was correctly near-silent (-76 to -89 dBFS)
   but the *data stream* was completely healthy in every case. Disproven.
3. **Clock Source → Digital Input (with the coax Digital Out → Digital In
   loopback) wedges the capture engine; switching back to Internal
   doesn't fully recover it**, single cycle. Tested: cycled Clock Source
   to Digital Input, waited 3s, back to Internal — status byte changes
   logged correctly (`0x36→0x34`, `0x30→0x32`), `Digital Input Lock`
   flipped off/on exactly as expected, capture afterward was clean.
   Disproven for a single cycle.
4. **Same, but via the exact original rapid/repeated session pattern**
   (`control_sweep.py`, which itself cycles Clock Source, immediately
   followed by `enum_verify.py`'s 11 rapid cset+capture cycles). Run
   once: clean. Looped 10x (~12 min, 110 capture attempts): zero
   failures, device left healthy, mixer at baseline throughout.
   Disproven.

No fifth hypothesis was invented to replace these. The investigation is
closed on the basis of: real, reproducible symptom; four specific,
falsifiable explanations tested directly against hardware; all four
disproven; no root cause identified. See the conversation history for
the full step-by-step evidence trail (kernel log excerpts, `amixer`
dumps, `pahole` struct-layout comparison between v0.1.3/v0.1.4 that ruled
out a struct-layout-shift theory too, before these four were formulated).

## Hypothesis 5 (untested) — for whoever picks this up next

The failing session followed roughly a 9-hour s2idle suspend with
**v0.1.3** loaded and the device powered, then a module `rmmod`/`insmod`
cycle **without a USB device reset** (no physical unplug/replug, no
`usb_reset_device()` — only the driver was reloaded against the same,
never-reset USB device). All captures failed in that session; it
self-healed after several subsequent module reloads. This hasn't been
tested at all — flagging it with a concrete test plan rather than
testing it under a time-boxed investigation that was already closed:

**Test plan**: load the module, suspend the system (s2idle, device
powered throughout, several hours if practical — matching the original
timing isn't verified to matter, but match it if convenient), resume,
attempt capture without any `rmmod`/`insmod` or physical reset in
between. If that alone doesn't reproduce it, add the `rmmod`/`insmod`
step (module reload with the USB device left powered and *not* reset)
on top, matching the original session's exact sequence. Watch
`/proc/asound/card3/pcm0c/sub0/status` live (`hw_ptr` stuck at 0 is the
signature) and the new v0.1.5 tracing/watchdog (below) for real
`urb->status`/`actual_length` evidence if it does reproduce.

See "`px_resume()` vs `px_probe()` reinit gap review" below for a
concrete mechanism that could plausibly connect this hypothesis to the
observed symptom — reasoning, not verified evidence.

## v0.1.5: tracing + no-data watchdog (no behavioral fix, no root-cause claim)

Added for whenever this recurs, since the one thing missing throughout
this entire investigation was real evidence *from the actual failing
moment* — all instrumentation below postdates the only session that ever
reproduced it:

- `dev_dbg_ratelimited()` in `px_cap_complete()`: `urb->status`,
  `urb->actual_length`, decoded `frames`, `active`/`running`, on every
  single completion (before the early-return, so failures aren't
  invisible). `dev_dbg()` of old/new status-byte values in
  `px_status_update()` whenever it actually writes.
- **New capture watchdog**: if a capture stream has been `RUNNING` for
  more than 250ms with **zero** `px_cap_complete()` invocations, emits
  one `dev_warn()` (not dynamic-debug gated — always visible) with the
  last URB's `status`/`actual_length`, the last-written status byte, a
  fresh live read of the current status byte (and what it says about
  `Digital Input Lock`), `px->running`, and `px->clock_internal`. Armed
  on capture `trigger(START)`, cancelled on `trigger(STOP)`, reset per
  stream start. Implemented as a `delayed_work` (process context) rather
  than a check in the playback completion path — deliberately, because
  every reproduction attempt in this entire investigation was a *plain
  `arecord` with no `aplay` running*; tying the check to playback
  completions would never fire for exactly the scenario it exists to
  catch. Reuses the existing `px->cap.lock` and `px->mutex` for all its
  bookkeeping rather than adding any new lock.

## `px_resume()` vs `px_probe()` — reinit gap review (not changed)

Checked what a fresh `probe()` initializes that `resume()` does not, for
the 5 categories asked about:

| Category | `px_probe()` | `px_resume()` | Gap? |
|---|---|---|---|
| USB interface altsetting | `usb_set_interface(0,1)` + `(1,1)`, **checked**, fatal (`goto fail`) on error | Same two calls | **Gap**: return values are never checked — if altset selection silently fails post-resume, resume proceeds as if it succeeded |
| Endpoint halt/clear | Never calls `usb_clear_halt()` on any endpoint | Same (never) | **Gap, but symmetric** — neither path ever clears a stall. Directly relevant to hypothesis 5: a bulk IN (`0x86`) endpoint left stalled from before a suspend, or from a module reload that never resets the USB device, would survive *either* path unchanged, and a stalled bulk endpoint not completing is indistinguishable from the exact "zero completions, zero errors" signature chased in this investigation |
| Codec registers (WM8776 R7/ADC_MUX/OUT_MUX × 2) | Writes **factory defaults** unconditionally (`px_codec_init()`) | Writes back the **cached shadow** values (preserves user's pre-suspend settings) | Not a gap — intentionally different behavior, same register set covered in both |
| Status byte | Sets only `STATUS_INTERNAL_CLOCK`, forced on | Sets only `STATUS_INTERNAL_CLOCK`, from `px->clock_internal` | Not a gap — same scope (neither sets `STATUS_STREAM`/`STATUS_SMALL_BUFFER`; both correctly defer that to the next `prepare()`) |
| Rate | Never calls `px_set_rate_ep()` | Never calls `px_set_rate_ep()` | Not a gap — rate is exclusively set inside `px_start_streaming()` (triggered by `prepare()`) in both the first-ever-use and post-resume case; our `hw.info` doesn't advertise `SNDRV_PCM_INFO_RESUME`, so ALSA core always requires userspace to re-`prepare()` after a resume, which correctly goes through the full start-streaming path including rate |

The settle-delay logic from v0.1.3 (`px_start_streaming()`'s check
against `px->stop_jiffies`) already generalizes correctly to the
post-resume case for free, since `px_suspend()` calls
`px_stop_streaming()`, which sets it — no separate resume-side handling
needed there.

**Not changed** — `usb_clear_halt()` on the capture/playback/feedback
endpoints during `resume()` (and arguably `probe()`, for the "module
reloaded without a USB reset" case in hypothesis 5) is the concrete,
nameable fix this review points at, plus checking `usb_set_interface()`'s
return values in `resume()`. Left as a recommendation, not implemented —
no root cause is confirmed, and resume wasn't where this investigation's
testing budget was spent.

---

# v0.1.5 — enum verification, digital-out, panel session

Module confirmed loaded (`/sys/module/snd_usb_phasex64/version` = `0.1.5`),
card 3 enumerated, `sudo dmesg -w | tee -a ~/px-live.log` confirmed live
before starting. Dynamic debug was **not** re-enabled this session (needs
`sudo`, not requested) — the `cap complete`/`status update` dev_dbg traces
are absent from this session's log for that reason, same caveat as the
v0.1.3 session. Everything below comes from unconditional `dev_err_ratelimited`
output and userspace (`amixer`/`aplay`/`arecord`) results.

Two new findings turned up while running the three requested tests, neither
related to the v0.1.4 capture-stall investigation above (that stays closed,
unreproduced). Both are reported with full repro evidence and are **not**
root-caused — flagging for whoever picks them up next, same discipline as
the rest of this file.

## 1. Capture Source enum verification

`enum_verify.py` run against v0.1.5: dual-tone file (ch1/2=440Hz,
ch3/4=1000Hz, -12dBFS), each `In 1/2`/`In 3/4 Capture Source` item selected
in turn (other pair held at `Mute`), all 6 capture channels recorded.

| Item (In 1/2) | Signal seen on | Item (In 3/4) | Signal seen on |
|---|---|---|---|
| Mic-HiZ-Line | ch5,ch6 only | — | — |
| Phono | ch5,ch6 only | Phono | ch5,ch6 only |
| Aux | ch5,ch6 only | Aux | **ch5,ch6 only — INVALID, see below** |
| Loopback 3/4 | **ch1,ch2** + ch5,ch6 | Loopback 1/2 | **ch3,ch4** + ch5,ch6 |
| Sound Module | ch5,ch6 only | Sound Module | ch5,ch6 only |
| Mute | ch5,ch6 only | Mute | ch5,ch6 only |

**Correction (added after this session): the `In 3/4 Aux` row above is
invalid.** The external RCA cable (`Out 3/4 -> Aux/Phono In 3/4`, switch on
`Aux`) that this result depends on had been unplugged for this entire
session — borrowed to wire up the coax Digital Out -> Digital In loopback
instead — and was reconnected afterward. "No signal" for `In 3/4 Aux` in
this table reflects the cable being absent, not the control failing to
route anything. Every other row is unaffected: `In 1/2 Aux`/`Phono` depend
on a different, still-unconnected path (front jacks); `In 3/4 Phono` would
show no signal either way since the physical `Ch.3/4 Aux/Phono` switch was
on `Aux`, not `Phono`, regardless of the cable. See "Cabling update" below.

Routing identity is otherwise unambiguous and matches the hardware mapping
table in `analysis/NOTES.md` exactly: `Loopback 3/4` (`Out 3/4 -> In 1/2`)
is the only `In 1/2` item that puts a signal on ch1/2, and only while that
item is selected; same for `Loopback 1/2` on ch3/4. The other items show
silence on ch1-4 in this test setup because nothing was physically cabled
into the front Mic/HiZ/Line jack, nothing played on the internal Sound
Module (DSP) route, and (per the correction above) nothing was cabled into
the front Aux/Phono input for ch3/4 either, at the time.
ch5/ch6 show *something* on every single item, including `Mute` —
confirming (again) that the digital S/PDIF mirror is completely
independent of the ADC mux, exactly as found in the v0.1.3 channel-map
test.

**But the measured pitch on every one of those "something" hits is wrong**
— see finding A below. The routing/identity table above is trustworthy;
the frequency numbers `enum_verify.py` printed (`tone@309Hz?` etc.) are not,
and that file's `data/enum_verify_results.json` is gitignored rather than
committed for that reason (same policy as the other transient run data).

### Could non-phono sources be summed in the old (v0.1.0-v0.1.3) bit layout?

Yes — confirmed from the Windows panel decomp, not just inferred from the
register bit layout. `CPLPhaseX64USB.exe.c` button handler `FUN_14004cee0`
(`:71980`), for the `In 1/2` button group: on every click, it independently
reads each of buttons 0 (Mic), 1 (Aux), 3 (Loopback), 4 (Sound Module) via
four separate `GetCheck`-style calls, and literally **sums their bit values**
(`1`, `4`, `0x10`, `8`) into one register value before writing it to
`WM_ADC_MUX` (`FUN_14004c870(param_1,1,0x15,uVar5)` at the end of the
function) — there is no code anywhere in this function that clears bit 1
when bit 3 is set, or vice versa, for any pair among {Mic, Aux, Loopback,
Sound Module}. The **only** hand-coded exclusivity in this function is for
Phono (bit 2): selecting it explicitly clears the other four's cached
states, and selecting any of the other four explicitly clears Phono. So the
Windows application code's own defense against a summed non-phono value is
nothing — it relies entirely on the four buttons being native Win32
auto-exclusive radio controls (`CDlgGroupRadioButtonIterator` at `:19698` is
generic MFC dialog-navigation plumbing, not application-level exclusivity
logic) to guarantee the UI itself never produces a state where two of them
read as checked at once. The v0.1.0-v0.1.3 Linux driver modeled these as
nine *independent* ALSA boolean switches with no such widget-level guarantee
(already flagged as open issue #2 above) — so `amixer` could set, say, both
`In 1/2 Aux Capture Switch` and `In 1/2 Sound Module Capture Switch` on at
once, producing `WM_ADC_MUX = 0x04 | 0x08 = 0x0C`, a value the original
Windows software's own write path could construct in principle but never
actually sends. Whether the WM8776-side mux hardware would then genuinely
mix both analog sources, or just pick one, or produce garbage, is a
hardware question the decomp can't answer (the original software never
exercises it) — not tested here either, since the current v0.1.4+ enum
control structurally can't send a multi-bit value any more.

## 2. Digital-out source investigation

### Finding A: wrong pitch on every digital-mirror / internal-loopback capture (NEW, not root-caused)

While building the table above, every channel that showed a "tone" showed
the **wrong frequency** — not close, not a rounding error. Isolated with a
single clean tone (no dual-tone interaction — tested and ruled out
explicitly, see below):

| Requested (ch1/2, solo) | Measured on ch5 (digital mirror) |
|---|---|
| 300 Hz | 426.52 Hz |
| 440 Hz | 309.25-309.33 Hz |
| 500 Hz | 394.6 Hz |
| 700 Hz | 678.9 Hz |
| 1000 Hz | 1105.41-1105.47 Hz |

Not a fixed ratio, not a fixed offset. Confirmed real (not a test-harness
artifact) by multiple independent checks:
- The *source* WAV file itself reads back as exactly the requested
  frequency before ever touching the hardware (`pxtools.analyze_channel` on
  the un-played file: 1000.0 Hz exactly).
- Measuring only the active (non-silent) portion of the capture, trimmed
  0.05s on each edge to exclude ramp transients, gives the same wrong
  frequency as measuring the whole buffer — not a windowing/leakage
  artifact from a short tone inside a longer silent recording.
- Full spectrum of the 1000Hz-requested capture shows a clean series of
  **odd harmonics spaced ~316.4 Hz apart** (156.7, 472.6, 789.0, **1105.4**,
  1421.9, 1738.3 Hz) — i.e. the captured waveform is a real, specific,
  reproducible non-sinusoid, not noise and not an FFT peak-picking error.
  1105.4 Hz is simply the strongest of that harmonic series, not an
  isolated artifact.
- **Ruled out dual-tone crosstalk**: a single tone on ch1/2 alone (ch3/4
  silent the whole time) still measures wrong on ch5 — same for ch3/4 alone
  via the `Loopback 1/2` path into ch1/2. So this is not an interaction
  between the two simultaneously-played pairs in the original dual-tone
  test file.
- **Ruled out "only stereo pairs with both L/R channels active" as the
  trigger**: ch1 alone (ch2 silent) still measures wrong on ch5. Ch2 alone
  (ch1 silent) produces nothing at all on ch5 — i.e. **the digital mirror
  only carries ch1, not ch2** (ch6 wasn't separately confirmed to carry ch2
  this session; worth checking next time).
- Confirmed on **two independent signal paths**: the always-on digital
  S/PDIF mirror (ch1/2 -> Digital Out -> coax -> Digital In -> ch5/6, no ADC
  mux involved) *and* the internal chip-level `Loopback 1/2`/`Loopback 3/4`
  ADC-mux route (`Out 1/2 -> In 3/4` via the WM8776, no S/PDIF involved) both
  show the same kind of distorted, non-sinusoidal result for the same
  nominal source frequency — strongly suggesting the distortion is common
  to both paths' shared origin (how the driver/hardware internally taps
  channels 1/2's and 3/4's own output), not specific to either downstream
  path.
- **Corrected framing (does not actually contradict v0.1.3)**: v0.1.3's only
  *rigorously certified* exact-frequency result ("Exact to the measurement's
  resolution at every rate", with an explicit ±0.5Hz tolerance check) is the
  "Clock/rate" test, and that test used the **external, physical RCA cable**
  loopback (Out3/4 jacks -> Aux/Phono In3/4 jacks) — a different path from
  either one implicated here. The "Channel map" test's "1000.0 Hz" figure for
  output ch1 -> input 5 (the digital mirror) was a side observation from a
  test whose actual purpose was identifying *routing* (which output reaches
  which input), not a pitch-accuracy certification — it carries none of the
  "exact to resolution"/tolerance-checked language the rate test does. So
  there is exactly one channel/path in this project's whole history with a
  hardware-confirmed, tolerance-checked exact-frequency result: the external
  analog cable into ch3/4. Today's finding doesn't contradict that — it's the
  first time either the digital S/PDIF mirror or the internal `Loopback`
  ADC-mux route has been pitch-checked at all.

Not root-caused. No fix attempted (out of scope for this session, and this
needs its own investigation the same way the capture stall did).

### Finding B: Clock Source control reliably fails (EPIPE) while a stream is active (NEW, not root-caused)

Discovered incidentally while testing whether any control changes what
Digital Out carries. Isolated to a precise, 100%-reproducible repro:

```
start arecord + aplay (both actively streaming)
amixer -c PhaseX64 cset name='Clock Source' <any other value>
  -> "amixer: Control sysdefault:3 element write error: Broken pipe"
```

6/6 reproductions across both directions (Internal->Digital Input and
Digital Input->Internal), every time a stream was active when the control
write was attempted. Kernel log line each time:

```
usb 1-2: status read failed: bRequest=0x49 wValue=0x0000 wIndex=0x0000 err=-32
```

`err=-32` is `-EPIPE` (endpoint stall). `px_clock_put()` goes through
`px_status_update()`, which does a **read**-modify-write of the status byte
(`px_status_read()` first, to avoid clobbering unrelated bits, then
`px_status_write()`) — the read is what's failing, and there's no retry on
this path (unlike the "bounded ctrl retry in stream start" logic added
elsewhere per the `e480463` commit). On failure, `px_clock_put()` correctly
leaves `px->clock_internal` unchanged (no state corruption — confirmed:
`Clock Source` reads back the pre-attempt value every time, exactly as the
code should do), and the device's control interface is immediately usable
again afterward (confirmed: a plain `cset` right after each failure
succeeds normally). No WARN, no hang, no corruption — just a reliably
failing user-facing operation.

**Isolated to control-reads specifically, not control transfers in
general**, by testing the other control paths while streaming:

| Control change while streaming | USB request pattern | Result |
|---|---|---|
| `Clock Source` (`px_status_update`: IN read, then OUT write) | vendor IN 0x49 then OUT 0x49 | **Fails every time** |
| `Digital Input Lock` (`px_lock_get`: IN read only, no write at all) | vendor IN 0x49 | **Fails every time** (see correction below) |
| `In 1/2 Capture Source` (`px_codec_write`: OUT only) | vendor OUT 0x41 | Succeeds |
| `PCM 3/4 Playback Switch` (`px_switch_put`: cached read + OUT write) | vendor OUT 0x41 | Succeeds |

**Correction to the original version of this finding**: `Digital Input
Lock` was first reported as unaffected ("30/30 succeeded") based on
`amixer cget`'s exit code alone. That was wrong — `amixer cget` exits `0`
even when the underlying read fails; on failure it prints the control's
static metadata (name/type/range) but silently omits the `: values=...`
line and puts the real error only on stderr (`amixer: Control sysdefault:3
element read error: Broken pipe`). Checking stdout content and stderr
together shows `Digital Input Lock` fails **every single time** while
streaming too, exactly like `Clock Source` — confirming it's specifically
every live `IN`-direction `px_status_read()`, not something particular to
the read-modify-write sequence `Clock Source` happens to use.

Also corrected: this is not a transient "right after start" condition.
Repeating the `Clock Source` toggle at 1s, 3s, 6s, and 10s into an
already-running stream fails identically every time — the failure
persists for as long as the stream is active, it does not settle out.

So it's specifically the `IN`-direction vendor control request
(`bRequest=0x49`, read status byte) that stalls while isochronous (playback)
+ bulk (capture) + isochronous (feedback) transfers are concurrently active
— every write-only control path tested succeeds fine under the same
streaming load, for as long as streaming continues. This is a **real,
user-facing reliability bug**: switching Clock Source, or just checking
Digital Input Lock, while music is playing or being recorded — both
entirely normal things to want to do — fail every time with "Broken pipe"
on this hardware.

Likely relevant to anyone revisiting this device's firmware-level USB
behavior generally, since it's the first time this project has issued an
`IN` vendor control request concurrently with active streaming (every
previous mixer/control test in this file, including the original Clock
Source hypothesis testing above, was done with both PCMs closed). **Not**
claiming this explains the closed v0.1.4 capture-stall investigation above
— that symptom was zero completions with zero kernel errors, this is a
clean, immediate, always-logged `EPIPE` with a different request entirely —
but it's the same general territory (status-byte IN reads under load) and
worth keeping in mind.

See "Clock Source / status-read fix plan" below for the proposed fix
(not implemented this session — investigation only, per instruction).

### What does Digital Out actually carry, and does any control change it?

Confirmed this session: Digital Out continues to carry a fixed, always-on
signal (present regardless of ADC mux state, matches v0.1.3's conclusion)
derived from playback channel 1 (channel 2 untested this session — see
Finding A). Toggling `PCM 1/2 Playback Switch` off then back on mid-stream
did not need isolating further given time spent on Findings A/B; not
re-verified this session beyond the pre-existing v0.1.3 "always-on mirror"
conclusion, which nothing here contradicts. Given Finding A, "clean mirror"
from the v0.1.3 writeup should be read as "mirror, content pitch-correctness
now in question" rather than "verified clean" — the RMS/loudness comparison
that supported "clean digital link" in v0.1.3 didn't check pitch against a
non-1kHz-elsewhere-confirmed reference the way this session did.

No control tested this session changed *whether* Digital Out is active or
*what it's a mirror of* (still looks hardwired to ch1, as before). Clock
Source cannot be meaningfully tested for this while a stream is open, per
Finding B. `DigitalOutMode`/`DigitalOutSelector` remain unconfirmed to do
anything on this model (per `analysis/NOTES.md`) — nothing new this session
changes that conclusion; still the most likely explanation is that Digital
Out is simply hardwired with no host-side source selection on this specific
model, independent of Finding A's pitch bug.

## 3. phasex64-panel on real hardware

Driven programmatically against the real `Mixer`/`Panel` classes (import the
module, real `amixer`-backed `Mixer(card)`, a real `Tk` root on the
session's live `DISPLAY`) rather than by hand-clicking, so each direction
could be asserted exactly rather than eyeballed:

| Direction | Test | Result |
|---|---|---|
| amixer -> panel | External `cset` on a boolean switch (`Direct 3/4 Playback Switch`), then `panel.refresh()` | Panel state matched the new value |
| amixer -> panel | External `cset` on an enum (`In 3/4 Capture Source`), then `panel.refresh()` | Panel's enum var matched the new item name |
| panel -> amixer | `panel.click()` on a switch button (`Direct 1/2 Playback Switch`) | amixer value flipped, and flipped back on a second click |
| panel -> amixer | `panel.click()` on an enum button (`In 3/4 Capture Source` = Phono) | amixer enum index matched exactly |

All four passed. Separately confirmed the **live auto-follow path** the
running GUI actually depends on (`Mixer.start_change_monitor()`, which
shells out to `alsactl monitor hw:PhaseX64` in a background thread and calls
`refresh()` on any output line) by running `alsactl monitor hw:PhaseX64`
directly and toggling a control externally: it printed a `VALUE` line
immediately on each change, confirming the plumbing the panel relies on for
passive updates (not just the manual `refresh()` calls used in the
assertions above) is live and working on this hardware/ALSA version.

## Kernel log — every non-dev_dbg line this session

Dynamic debug was off (not re-enabled), so no `cap complete`/`status
update` trace lines exist for this session; the lines below are the
complete set of anything else logged, all from Finding B above:

```
usb 1-2: status read failed: bRequest=0x49 wValue=0x0000 wIndex=0x0000 err=-32
usb 1-2: status read failed: bRequest=0x49 wValue=0x0000 wIndex=0x0000 err=-32
usb 1-2: status read failed: bRequest=0x49 wValue=0x0000 wIndex=0x0000 err=-32
usb 1-2: status read failed: bRequest=0x49 wValue=0x0000 wIndex=0x0000 err=-32
usb 1-2: status read failed: bRequest=0x49 wValue=0x0000 wIndex=0x0000 err=-32
usb 1-2: status read failed: bRequest=0x49 wValue=0x0000 wIndex=0x0000 err=-32
usb 1-2: status read failed: bRequest=0x49 wValue=0x0000 wIndex=0x0000 err=-32
```

Zero `WARN`, zero panics, zero resubmit failures, zero other errors. The
device recovered cleanly after every one of these (confirmed: card stayed
enumerated, both PCMs showed `closed` afterward, a follow-up `cset`
succeeded normally each time) — this is Finding B, not a crash or a hang.

---

# v0.1.5 — follow-up: bit-pattern proof for Finding A, fix plan for Finding B

Investigation only, per instruction — no driver code changed this session.
Continues directly from the session above; same module (v0.1.5), same
baseline, mixer state restored to baseline throughout and at the end.

## Kernel log accounting (correcting an undercount)

The "7 lines" figure in the session above was accurate for everything up to
that point, but this follow-up investigation generated substantially more
`status read failed` lines while re-verifying and extending Finding B, none
of which had been reported yet. Full accounting, every line attributed to a
specific script:

| Timestamp(s) | Count | Source |
|---|---|---|
| 86433 | 1 | Finding B's original discovery (digital-out control test) |
| 86571/86578/86584 | 3 | Clock Source 3-attempt repro loop (~6.24s/iteration — the loop's own fixed wall time: fresh `arecord`+`aplay`+1s delay+wait+sleep) |
| 86634/86650/86655 | 3 | Further isolation (read-vs-write-only controls) — **all 7 above = the original report, unchanged** |
| 92293.x | 10 visible + "20 callbacks suppressed" (30 total) | `Digital Input Lock` read loop, 30x at 50ms — confirms it fails every time too |
| 92344/92350/92357 | 3 | Clock Source repro loop, re-run to confirm still reproducible |
| 92398/92402/92409/92420 | 4 | Delay sweep (1s/3s/6s/10s into an active stream) — proves the failure persists, it's not transient |
| 92514.x | 10 visible | stdout/stderr-checked re-check of the `Digital Input Lock` finding |

The ~54ms-spaced 10-line bursts are the kernel's default `dev_err_ratelimited`
cap (burst=10 per 5s) truncating a tight loop of back-to-back `amixer cget`
calls — not a driver-side retry loop. The ~6.24s cadence is simply that
loop's own fixed per-iteration wall time. Confirmed **not** the capture
watchdog (`grep -c "capture watchdog" ~/px-live.log` = 0 for the whole
session) and **not** `alsactl monitor`/the panel (that ran much earlier,
pre-commit, no time overlap with any of these lines). Every line here is
directly attributable to a script run in this investigation.

## Finding A: bit-pattern proof

Sine waves can only show *that* the frequency comes out wrong, not *how*.
Replaced the tone with a per-channel-tagged counter (`(tag << N) | sample
sequence number`, captured as raw `S32_LE` and compared word-for-word
against what was sent) on the three paths that don't need new cabling:
`Loopback 3/4` into capture ch1/2, `Loopback 1/2` into capture ch3/4, and
the digital S/PDIF mirror into capture ch5/6.

**Two different pattern designs were needed**, because the two kinds of
path being tested have fundamentally different constraints:

- A per-sample ramp (every sample ±1 from the last) has energy all the way
  to Nyquist — any analog reconstruction/anti-imaging filter will blur it
  regardless of whether there's a driver bug, so it's the wrong tool for
  the two internal `Loopback` paths (genuinely analog: DAC -> WM8776 ADC
  mux -> ADC). It *is* the right tool for the S/PDIF mirror, since that's a
  bit-exact digital tap with no analog filtering in the way.
- A slow staircase (step held for 1ms, well inside the passband the device
  already handles cleanly at 1kHz sine) was tried next for the analog
  paths, but also failed to come through recognizably — almost certainly
  because any real analog audio path is AC-coupled (DC-blocked) by design,
  and a staircase is mostly sustained DC between edges. This is a limit of
  testing an analog path with a digital bit-pattern, not evidence of a
  decode bug — **sine waves remain the right instrument for the two
  internal `Loopback` paths**, and Finding A's existing harmonic-spectrum
  result is the best available evidence for them.

### S/PDIF mirror (ch5/6): clean, decisive result

> **Invalid (2026-10-14): measured with the S/PDIF loopback cable
> connected** (this finding's own capture path *is* that cable: digital
> out -> digital in). The "64-frame"/`+65`/`-63` periodic duplication
> below is pacing noise from the capture clock following the locked
> ~32kHz S/PDIF input, not a genuine wire-level duplication bug. Needs
> re-measurement with the cable removed (and therefore without a live
> S/PDIF mirror to tap - a different test setup) before the underlying
> "is there a real duplication" question is reopened.

Routing is exact — capture ch5 shows **only** tag 1, ch6 shows **only**
tag 2, zero cross-contamination. Sample-to-sample index stepping:

| Delta | Share |
|---|---|
| +1 (correct) | 97.7-98.3% |
| +65 | 0.9-1.5% |
| -63 | 0.4-0.8% |

`+65` immediately followed by `-63` nets to `+2` over two samples — the
same net progress two correct `+1`s would make — but with exactly one
extra, duplicated sample's worth of data (`CAP_WIRE_FRAME` = 64 bytes = one
captured frame) inserted in between. This is a **periodic single-frame
duplication**, not noise, not a byte swap, not a fixed bit-shift: a tiny
fraction of wire frames get processed (or transmitted) twice.

**Checked whether this aligns with our own URB handling** (`CAP_URB_BYTES`
= 2048 = 32 frames/URB): the anomaly's sample position mod 32 is perfectly
uniform (40-59 hits in every one of the 32 possible slots, no clustering at
slot 0 or 31). **This rules out a bug in `px_cap_complete()`'s per-URB loop
or `px_decode_frame()`** — if either mis-handled a URB boundary, the
anomaly would cluster at a specific offset within the 32-frame URB, not
spread uniformly across all of them.

The duplications arrive in closely-spaced pairs (3-5 samples apart) with
roughly ~148 samples (~3.08ms) between pairs — a periodicity that doesn't
match any constant in this driver (`PLAY_PACKETS`=8, `FB_URBS`=4,
`CAP_URBS`=8, 1ms feedback interval, etc., checked and none line up with
~3.08ms). Combined with the uniform mod-32 result, the evidence points
**away from this driver's capture-side code** and toward either a periodic
hiccup in the device's own internal tap/mux circuitry for the S/PDIF
mirror specifically, or a periodic effect on the *playback* side not yet
checked against this exact timebase. Not traced further than this —
flagging the next step rather than guessing at it.

### Internal `Loopback` paths (ch1/2, ch3/4): methodologically inconclusive by bit-pattern; sine evidence stands

Neither the ramp nor the staircase pattern survived recognizably through
either `Loopback 3/4`->ch1/2 or `Loopback 1/2`->ch3/4 — captured values
centered near zero/noise-floor for both designs, consistent with the
AC-coupling/anti-aliasing explanation above rather than a decode bug (the
same `px_decode_frame()` that's proven correct here for the S/PDIF path,
and proven correct in the v0.1.3 session for the external-cable analog
path, is applied identically to every channel — nothing about its code
singles out ch1-4 vs ch5/6). The existing sine-based result (clean harmonic
series, ~1.1x apparent pitch on both analog-mux-routed paths) remains the
operative evidence for these two paths; this session did not independently
confirm the same byte-level mechanism for them, only the same qualitative
symptom.

### Ch1/2 via a real analog input, and the external-cable control — blocked on cabling at the time

Two parts of the original request needed physical cabling changes this
session couldn't make: (1) the external-cable control (`Out 3/4 -> RCA ->
Aux/Phono In 3/4`) and (2) `Out 1/2 L -> front HiZ/Line jack` to check the
ch1/2 slot via a real analog input. **Update, after this session**: the
`Out 3/4 -> Aux/Phono In 3/4` cable was only unplugged for this session (it
had been borrowed to wire up the coax Digital Out -> Digital In loopback
instead) and has since been reconnected, switch on `Aux`. (2) still needs
its own, separate cabling and remains open.

### `enum_verify.py` Aux sweep, re-run

With the cable reconnected: `Aux` on `In 3/4 Capture Source` now correctly
shows signal on capture ch3/ch4 (plus the usual ch5/ch6 digital mirror) —
confirmed, routing was never broken, only the cable was absent before.

### S/PDIF repeat-period rate sweep (44100/48000/96000) — blocked, not completed

Requested to tell whether the duplicated-frame period scales with sample
rate (clock domain) or stays ~constant in time (packet/feedback timing).
Not completed — the live capture stall below (found while running this
exact test) made it impossible to get clean captures at any of the three
rates. Deferred until capture is working again.

## Finding A, major correction: the "~1.1x pitch shift" was a measurement artifact

With the cable reconnected, (1) above was run — and the external-cable
path, the one path with a hardware-confirmed exact result from v0.1.3,
**also** shows the same "wrong frequency" symptom under the original
single-FFT-peak measurement (997Hz requested measured as 1101.10Hz). That
result does **not** mean the external cable is now also broken the same
way as S/PDIF — it means the measurement method was never trustworthy to
begin with, and this is the test that exposed it.

**Exact-duplicate-frame check (this session's actual request, "expect
zero repeated frames")**: confirmed zero. Scanning for literal,
bit-identical consecutive capture words (excluding samples near a
peak/trough, where a real signal can legitimately repeat a quantized value
by chance) found **zero** exact duplicates on either cap ch3 or ch4,
across the whole 5-second capture. This part of the expectation holds.

**But a direct, FFT-free check reveals a different, real anomaly that the
naive measurement was conflating with "wrong pitch"**: zero-crossing period
analysis (with light smoothing to reject spurious noise-crossings) shows
the *median* cycle length correctly matches the requested frequency on
**every channel tested on every path** — external cable ch3/ch4 and S/PDIF
ch5/ch6 alike, all four resolving to the correct ~48-sample (1000Hz-ish)
period at the mode. The true signal frequency was right all along. What's
actually happening: roughly 39-40% of individual cycles come out
*shortened or lengthened* rather than the correct length, with a
strikingly consistent secondary cluster at exactly 2/3 of the correct
period (32 samples instead of 48) on **all four channels on both paths** —
not just S/PDIF.

| Path | Channel | Median cycle (correct) | Cycles matching correct length | 2nd-largest cluster |
|---|---|---|---|---|
| External cable (Aux) | ch3 | 48 (1000.0Hz) | 61% | 32 samples, ~11% |
| External cable (Aux) | ch4 | 48 (1000.0Hz) | 60% | 32 samples, ~11% |
| S/PDIF mirror | ch5 | 48 (1000.0Hz) | 61% | 32 samples, ~10% |
| S/PDIF mirror | ch6 | 48 (1000.0Hz) | 61% | 32 samples, ~11% |

This near-identical distribution across a purely-analog path (DAC -> real
RCA cable -> WM8776 ADC) and a purely-digital path (bit-exact S/PDIF tap)
is strong evidence the actual glitch is **upstream of both** — most likely
on the playback/encode side, or in how the device internally taps its own
output for *either* monitoring path — not specific to the ADC mux or the
S/PDIF receiver. This also retroactively explains why the original
single-FFT-peak measurements (freq sweep, enum table, the earlier "1105Hz"
figures throughout this document) looked so inconsistent across different
requested frequencies: sparse per-sample glitches smear spectral energy
across a harmonic comb, and a naive peak-pick grabs whichever harmonic
happens to be strongest for that particular input frequency — not a real,
frequency-dependent pitch shift. **The underlying playback pitch is
correct; what's actually wrong is sparse (roughly 1-2% of samples, per the
bit-pattern measurement) duplicate/dropped-sample-style glitches that
corrupt any naive single-tone frequency measurement.** The S/PDIF-specific
`+65`/`-63` finding (clean duplicate-frame insertion, ruled out as a
per-URB boundary bug) still stands as the one path where the *mechanism*
behind these glitches was pinned down precisely; the external-cable
result shows the same-sized effect without matching it to the identical
byte-level mechanism (no exact duplicates found there — consistent with
drops rather than duplicate insertions on this path, not yet confirmed
further).

## Finding B: Clock Source / status-read fix plan

### Windows driver research: inconclusive

Searched the decomp for how the original driver guards a clock-source
change against concurrent streaming. Found `FUN_f10131e0` (status-byte
read-modify-write, gated behind a long chain of VID/PID capability checks
suggesting it's shared across several Ploytec-based products, not specific
to this device) and `FUN_f1005a00` (a *different* function, setting the
small-buffer status bit for stream start — not the clock toggle). Could
not locate a clean, clearly-identified runtime IOCTL handler for the
control panel's live Clock Source toggle within reasonable effort — this
decompiled driver is large, heavily shared across multiple products, and
tracing the exact dispatch path for one specific UI action would be its
own multi-hour effort. Reporting this as inconclusive rather than guessing.
If the Windows driver avoids this failure, the mechanism wasn't found here.

### Proposed fix (not implemented — report only, per instruction)

Two different controls, two different fixes, because they need genuinely
different data:

**`Clock Source` (`px_clock_put`/`px_status_update`)**: the read-before-write
exists only to avoid clobbering unrelated status bits (`STATUS_STREAM`,
`STATUS_SMALL_BUFFER`, bits 3/6/7) that this driver doesn't track anywhere
else. But `px->last_status_byte` is already updated on every successful
`px_status_write()` — including the one inside `px_start_streaming()` that
ORs in `STATUS_STREAM|STATUS_SMALL_BUFFER|clock bit` when a stream starts.
So **whenever `px->running` is true, `px->last_status_byte` already holds
an accurate, current copy of the full status byte** — there's no need to
re-read it live. Proposed: in `px_status_update()`, use `px->last_status_byte`
as `old` instead of calling `px_status_read()` when `px->running` is true;
only do the live read when not running (the first-ever change, before any
stream has started, where the cache might not reflect the hardware's true
power-on state). This avoids the failing `IN` transfer entirely in the
case that currently fails 100% of the time, while keeping a real read for
the case that needs one.

**`Digital Input Lock` (`px_lock_get`)**: this is fundamentally different —
it reports a live, read-only hardware status bit (whether the S/PDIF
receiver currently sees a valid signal) that the driver never writes and
has no cached write-side value for. It cannot be served from
`last_status_byte` the way Clock Source can. Proposed: cache the *last
successfully read* status byte separately (a new field, distinct from
`last_status_byte` which is write-side only) on every successful
`px_status_read()` anywhere in the driver (including the capture
watchdog's own diagnostic read), and have `px_lock_get()` return that
cached value instead of attempting a live read when `px->running` is true
— falling back to a live read when not running, same as above. This keeps
the control responsive (a slightly stale "last known" answer) instead of
failing outright, which matters for anything that polls it periodically
(the panel GUI's 1s refresh, `alsamixer`, etc.) while audio is playing —
exactly the normal case.

Both changes are read-path-only, bounded to `px_clock_put()`/`px_status_update()`
and `px_lock_get()`, and don't touch `px_status_write()` or any locking —
low risk, but not implemented this session per the investigate-first
instruction.

## Task 3 follow-up: does `Aux` on In 3/4 show the external-cable signal?

Re-checked explicitly: every sweep this session (`enum_verify.py`'s
original run and the digital-out tests) showed `Aux` on `In 3/4 Capture
Source` with **zero** signal on capture ch3/4 — only the always-on ch5/6
digital mirror. At the time this was reported as the external RCA cable
no longer providing a live loop, cause unconfirmed.

**Resolved, after this session**: the cable (`Out 3/4 -> RCA -> Aux/Phono
In 3/4`, switch on `Aux`) had been unplugged for this entire session —
borrowed to wire up the coax Digital Out -> Digital In loopback instead —
and has since been reconnected, confirmed by whoever was running the
hardware. **The `In 3/4 Aux` result in the enum-verification table above
(section "1. Capture Source enum verification") is therefore invalid** —
it reflects the cable's absence, not the control. Both cables (`Out 3/4 ->
Aux/Phono In 3/4` and the coax `Digital Out -> Digital In`) are confirmed
live again as of the cabling update. Everything in this document that
depended on the coax loopback specifically (Finding A's S/PDIF results,
the enum table's ch5/ch6 columns, Finding B) is unaffected — that cable
was never the one in question. Re-running `enum_verify.py` and the
external-cable control sub-test from Finding A are both unblocked now,
just not yet done.

---

# v0.1.5 — LIVE capture-stall reproduction (unexpected, mid-session)

**This is the symptom from the "CLOSED, unreproduced" v0.1.4 investigation
above, happening live, for the first time since that investigation closed.**
Found incidentally while running the next round of requested tests, not
deliberately reproduced. Stopped all further hardware testing immediately
on confirming it; no recovery attempted (no `rmmod`/`insmod`, nothing else
— out of scope for this session regardless). Reporting the evidence, not
reopening or re-closing the investigation's status — that's a call for
whoever picks this up next.

## What happened

Picking testing back up after roughly a 2-hour gap with the device open,
powered, and otherwise idle (no PCM activity — that gap was spent on
decomp research and writing up the previous findings), the very first new
`arecord` attempt failed:

```
arecord: pcm_read:2285: read error: Input/output error
```

Exactly the original symptom: `EIO`, no prior indication anything was
wrong. Tried twice more (a second rate, then a plain capture-only retry
with no playback at all) — **all three failed identically**. The device
remained enumerated throughout (`lsusb`/`/proc/asound/cards` both normal),
both PCMs read back `closed` after each attempt (clean teardown, no hang),
and `amixer -c PhaseX64 contents` read back every control at its correct
baseline value with no errors — this is a clean, repeatable `EIO`, not a
hang or a crash.

## First real evidence, ever, for this symptom — the v0.1.5 watchdog fired

Three times, once per attempt, each reading:

```
usb 1-2: capture watchdog: no px_cap_complete() in >250ms - last urb
status=0 actual_length=2048, last written status byte=0x32, current
status byte=0x36 (read err=0), running=1 clock_internal=1
digital_input_lock=on
```

- `last urb status=0 actual_length=2048` — the *last* URB that did
  complete was entirely normal (no error, a full, non-truncated 2048-byte
  transfer). Completions didn't degrade or error out, they just **stopped**
  — matching the original "zero completions, zero errors" description
  exactly, now with the watchdog's confirmation that it genuinely stopped
  rather than never having started.
- `current status byte=0x36` vs `last written=0x32`: **not an
  inconsistency** — the only bit that differs is `STATUS_DIGITAL_LOCK`
  (`0x04`, read-only, hardware-reported), and `digital_input_lock=on`
  confirms that's exactly what it is. The device's live status read
  succeeded and is self-consistent.
- `clock_internal=1`, `running=1` — both as expected, no corruption of
  driver-side state.

## New data point: control-plane reads succeeded *while this was happening*

`amixer -c PhaseX64 cget name='Digital Input Lock'` and `cget name='Clock
Source'` were both run *during* this stall (capture `running=1`, per the
watchdog) and **both succeeded cleanly** — no `EPIPE`, no "Broken pipe".
This is notable against Finding B above, where every `IN` status read
failed 100% of the time while a stream was genuinely transferring data.
The simplest reading: with the bulk capture endpoint producing **zero**
traffic (that's the whole symptom), there's no contending bulk/isoc load
for the control pipe to lose arbitration against — consistent with
Finding B's mechanism being specifically about *active* USB traffic
contention, not merely `px->running` being true. Also consistent, without
being able to confirm it from here (no `sudo`, no `usbfs` access to check
endpoint halt state directly): the capture bulk endpoint (`0x86`) going
genuinely quiet — stalled/halted at the USB level — would produce exactly
this signature, and is exactly the mechanism the existing `px_resume()` vs
`px_probe()` gap review already named as a plausible, unverified cause
(**neither path ever calls `usb_clear_halt()`**) — still not confirmed,
flagging the connection rather than claiming it.

## Status at the point testing stopped

Capture still failing as of the last check (3/3 attempts, including a
plain capture-only retry with no playback). Not confirmed whether this
self-heals the way the original session's did ("self-healed after several
subsequent module reloads") — no reload attempted, out of scope. Device
otherwise fully healthy: enumerated, idle, every control readable and at
its correct baseline value, no WARN, no hang, no corruption.

**This directly bears on hypothesis 5** from the original investigation
(long idle period with the device powered, then resumed activity) — this
session's gap was ~2 hours of idle-but-open, not a 9-hour suspend, and no
`rmmod`/`insmod` cycle happened in between, which makes this an even
simpler trigger than hypothesis 5 proposed, if it holds up. Not claiming
it's confirmed from a single occurrence — flagging it as the first actual
evidence this symptom is still alive and reproducible under much more
mundane conditions than originally thought.

## Live stall, continued: two non-invasive checks, then v0.1.7

Before any `rmmod`/`insmod`, four more checks against the still-stalled
device, none of them touching code:

**1. Does playback work right now?** Yes — `aplay` of a short tone
completed cleanly, `exit=0`, no errors. **Confirmed specific to the
capture endpoint**; playback is completely unaffected by whatever's
wedged.

**2. How did the last capture before the stall end — normal or killed?**
Can't be answered from the kernel log directly: dynamic debug was never
re-enabled after the v0.1.5 module load, so there are zero `pcm
open`/`trigger`/`close` trace lines anywhere in this session. Reconstructed
from this session's own script history instead: the capture immediately
before the ~2h gap ended naturally (`arecord`'s own duration timer
expired). But the **three before that** — the last three iterations of
the delay-sweep script used to confirm Finding B's EPIPE persists over
time — were explicitly killed via `Popen.terminate()` (`SIGTERM`) mid-transfer,
with only a 3-second grace period before the script gave up waiting and
moved on without confirming clean exit. A real, concrete candidate
mechanism for how the capture bulk endpoint could have been left in a bad
state — not just a hypothesis.

**3. Is `px->running` stuck true with nothing open?** Both PCMs read
`closed`, no stray processes. `px->running` itself has no sysfs/procfs
exposure and the only unconditional trace that prints it is inside the
watchdog (which only fires on a new capture start) — couldn't check it
directly without `sudo`-gated dynamic debug. Resolved indirectly instead:

**4. Least-invasive recovery test.** With nothing open, toggled `Clock
Source` (forces a real status-byte write) — **succeeded cleanly in both
directions**, `exit=0`, no `EPIPE`. Since Finding B established that this
exact read-modify-write reliably fails specifically when `px->running` is
true, under genuine load, a clean success here is strong evidence
`running` was correctly `false` — **not** stuck true. Then: `arecord` 3s —
**still failed immediately** (`EIO`, 44-byte file, header only), with a
4th watchdog line, byte-for-byte identical to the first three.

**Net result: the "running stuck true, skipped re-send" hypothesis is
ruled out.** `running` was false, the next `prepare()` went through the
*entire* normal start sequence (fresh `SET_CUR` rate on both endpoints,
fresh status-byte write with `STATUS_STREAM` re-asserted) — and it still
produced zero completions. Whatever's wedged survives a complete, correct
re-initialization of everything this driver controls over the control
pipe, which points at the endpoint/URB level rather than status-byte or
rate-negotiation state. Per instruction, stopped here rather than
`rmmod`/`insmod`-ing — that recovery is the user's call, not taken this
session.

### v0.1.7: usb_clear_halt() at stream start + self-healing watchdog escalation

Two changes, investigation/code only — **not loaded this session**.

**`usb_clear_halt()` on all three streaming endpoints at every
`px_start_streaming()`** (capture `0x86`, feedback `0x81`, playback
`0x02`), right before each one's URBs are (re)submitted. This resets the
data-toggle bit on both host and device — the classic fix for a bulk IN
pipe that silently stops delivering after its URBs were killed
mid-transfer without a clean stop, which is exactly what check 2 above
found as a real, specific candidate (three `SIGTERM`-killed captures
right before the stall). Isochronous endpoints (feedback, playback) don't
really have a toggle/halt concept the same way bulk does, but it's
harmless to try there too, for symmetry — non-fatal either way, logged at
`dev_dbg`/`dev_warn_ratelimited`.

**The capture watchdog is now self-healing, escalating every 250ms**
instead of firing once and only reporting:

- Stage 0 (first fire, no completions in >250ms): kill capture URBs,
  `usb_clear_halt()` the capture endpoint, resubmit.
- Stage 1 (still nothing 250ms later): the heavier `usb_set_interface()`
  alt 0 -> alt 1 cycle on `intf1` (capture + feedback's interface), then
  kill/resubmit capture URBs again.
- Stage 2 (still nothing): give up, log the full diagnostic as before.

Every stage happens entirely under `px->mutex` — the same lock
`px_stop_streaming()` (reached from `trigger(STOP)`'s eventual
`close()`/`hw_free()`, suspend, and disconnect) is always called under —
so a concurrent stop can't race a watchdog escalation step; one
completes before the other starts. `cap.active` is re-checked immediately
after acquiring the mutex (in addition to the existing lock-free
pre-check) to close the small window where a stop could have landed
between the pre-check and actually acquiring the lock — if so, the
watchdog bails without touching any URB, since `px_stop_streaming()`
already correctly tore them down.

Also added, per this session's finding 2 above: every watchdog log line
now includes a derived hint — `"status read succeeded while streaming =
device not streaming"` — whenever the diagnostic status read inside the
watchdog succeeds, since Finding B established that exact read reliably
fails under genuine streaming load; a clean success there is itself
evidence the device doesn't consider itself actively streaming at that
moment, consistent with tonight's finding 4.

`MODULE_VERSION`/`PACKAGE_VERSION`/`pkgver` bumped to 0.1.7. Built clean,
`W=1`, no warnings. **Not loaded** — build/commit only, per instruction.
The device remains in its stalled state from the live reproduction above,
preserved for whenever recovery is decided on.

## v0.1.7 stage-1 fix, kill-stress testing, and a leading hypothesis

`intf1` carries feedback (`0x81`) alongside capture (`0x86`), not
playback (`0x02`, which is on `intf0`) — confirmed from the file's own
header comment. Stage 1's `usb_set_interface()` alt-cycle is
interface-wide, so it was implicitly invalidating feedback's URBs too
without resubmitting them, independent of whether playback itself was in
use (feedback is always submitted alongside capture in
`px_start_streaming()`). Fixed: stage 1 now kills, resets
(`fb_head`/`fb_count`), and resubmits feedback URBs right alongside
capture's, around the interface cycle.

A second, separate build-only step: `tests/hw/kill_stress.sh` was added —
50 iterations of killing an `arecord` mid-capture at a random 50-900ms
point (`SIGTERM`, every 5th iteration `SIGKILL`) with no clean stop, then
checking whether a normal capture right after still succeeds. This
directly tests the "last 3 captures were `SIGTERM`-killed mid-transfer"
candidate mechanism found while investigating the live stall above.

**Result on a freshly-reloaded v0.1.5 (no fix): 50/50 clean — the kill
trigger alone does not reproduce the stall.** Every kill (40 `SIGTERM`,
10 `SIGKILL`) at every tested delay was followed by a successful normal
capture; `capture watchdog`/`clear_halt` lines: 0/0 (expected — neither
exists in v0.1.5).

**Result on v0.1.7 (with the fix): also 50/50 clean**, plus a sanity
check (3s capture, 3s playback, 10s full duplex — all `exit=0`).
`capture watchdog` lines: 0 (no stall ever occurred, so it never had
reason to fire). `clear_halt` lines: 300 during the kill-stress run + 9
during the sanity check = 309 total, **all logged "ok"**, zero failures —
every one of the ~103 stream starts across both runs cleared halt
successfully on all three endpoints as a normal, no-op part of starting,
confirming the new unconditional `usb_clear_halt()` call in
`px_start_streaming()` doesn't interfere with completely healthy
operation.

**Leading hypothesis, given both pieces of evidence together**: the
kill-mid-capture pattern alone isn't suffient to reproduce this — 100/100
clean kills across both module versions — but **both of the two real
stalls this project has ever seen followed long device inactivity**: the
original v0.1.4 session's suspected trigger (~9h s2idle suspend,
hypothesis 5, never tested) and this session's live reproduction (~2h
idle-but-powered, no suspend, no reload). The common factor isn't
kill-timing, it's **elapsed idle time with the device powered and
enumerated but not actively streaming**. If the device's own firmware
drops into some internal idle/power state after a long enough gap and
doesn't fully wake back up for a bulk transfer without a fresh
`SET_INTERFACE`, that would explain both real occurrences and the
kill-stress test's inability to reproduce it (kill-stress has no idle
gap between iterations — each one starts within ~1-2s of the last).
Watchdog stage 1 (the `usb_set_interface()` alt 0 -> alt 1 cycle on
`intf1`) already covers exactly this mechanism, if it holds up. Not
confirmed — the two real occurrences are still only two data points, and
no one has yet tested "long idle, then capture" directly (hypothesis 5's
own test plan, still open). Flagging as the leading hypothesis, not a
conclusion.

## v0.1.7 idle test

Directly testing the leading hypothesis above: the device (v0.1.7 loaded,
confirmed) is left completely untouched — no `aplay`/`arecord`/`amixer`,
no panel, nothing accessing card 3 — for at least 2 hours, then a plain
capture attempt.

**Time zero:**

- Wall clock: `2026-10-12T00:00:30Z` (`2026-10-12 02:00:30 CEST`)
- Last kernel timestamp in `~/px-live.log`: `[106648.607775]` (`usb 1-2:
  pcm close: Capture` — the tail end of the v0.1.7 sanity-check duplex
  test from the prior step)

Next capture attempt should happen no earlier than kernel timestamp
`~108648` (106648 + 7200s) / wall clock `2026-10-12T02:00:30Z` or later.

### Result: no stall

Ended at wall clock `2026-10-12T02:34:27Z` / kernel timestamp
`~116242` — roughly 2.5 hours idle, device powered and enumerated the
whole time, completely untouched (no `aplay`/`arecord`/`amixer`/panel).

1. `grep -i "PM: suspend"` since time zero: **0 matches** — the laptop
   did not sleep during the idle period. This tests "idle while powered"
   specifically, not suspend/resume (hypothesis 5's original, untested
   scenario, which remains untested).
2. Single capture (`arecord -D hw:PhaseX64 -f S32_LE -c 6 -r 48000 -d 3`):
   **`exit=0`**, file size `3456044` bytes — exactly
   `48000 * 6 * 4 * 3 + 44` (full, non-truncated 3s capture).
3. Grep since time zero, verbatim:
   - `"capture watchdog"`: no matches.
   - `"clear_halt"` (failures only): no matches.
   - `"stage"`: no matches.
   - `"alt"`: only as a substring of "clear_**halt**" —
     ```
     [116242.879258] usb 1-2: usb_clear_halt(feedback) ok
     [116242.879582] usb 1-2: usb_clear_halt(capture) ok
     [116242.879755] usb 1-2: usb_clear_halt(playback) ok
     ```
     the 3 normal, unconditional calls `px_start_streaming()` always
     makes — not watchdog-triggered, no escalation involved. (For
     context, not one of the 4 requested patterns: `pcm open: Capture
     err=0` -> `pcm trigger: Capture cmd=1` -> 10 logged + "4455
     callbacks suppressed" ratelimited `cap complete` lines -> clean
     `pcm close: Capture` — confirms the stream genuinely ran to
     completion, not just opened.)

| Question | Answer |
|---|---|
| Stall occurred? | No |
| Escalation step that recovered it? | None needed — succeeded unescalated |
| `clear_halt` activity | 3 calls, all the normal non-watchdog ones, all "ok" |
| PM suspend during the window? | No |

**~2.5 hours of idle-but-powered, un-suspended inactivity did not
reproduce the stall.** Combined with `kill_stress.sh`'s 100/100 clean
kills (v0.1.5 + v0.1.7), two candidate mechanisms have now each been
tested in isolation and neither reproduces it alone. The two real
occurrences (this project's ~9h-suspend-session inference and this
session's live ~2h-idle reproduction) remain the only two data points;
whatever combination of factors actually caused them hasn't been
isolated by either single-variable test so far. Hypothesis 5's original
scenario — an actual suspend/resume cycle, not just idle-while-powered —
is still untested.

## v0.1.8 regression: kill_stress.sh + sanity check

Same two tests re-run against v0.1.8 (generation-counter + re-arm-recheck
fixes from the race review), as a regression check.

**`kill_stress.sh` (50 iterations): 50/50 clean, no failures** — identical
result to the v0.1.5/v0.1.7 runs. **Sanity check (3s capture, 3s playback,
10s full duplex): all `exit=0`.** `capture watchdog` lines: 0. `clear_halt`
lines: 0 — but this 0 does **not** mean `usb_clear_halt()` wasn't called;
dynamic debug was off for this run (reset by the reload to v0.1.8, as
usual — confirmed via zero `pcm open`/`trigger`/`close` lines either,
which are equally dynamic-debug-gated), so the normal-path `clear_halt`
success message (`dev_dbg`) had nothing to print to. No failures means
the unconditional, non-gated failure path (`dev_warn_ratelimited`) had
nothing to report either — consistent with, but not independent
confirmation beyond, the v0.1.7 run's 309-line all-"ok" result.

### Important caveat on the idle test's interpretation

**The v0.1.7 idle test above cannot distinguish "no stall would have
happened anyway" from "the unconditional `usb_clear_halt()` at stream
start prevented one."** v0.1.7 (and v0.1.8) call `usb_clear_halt()` on
all three endpoints on *every* `px_start_streaming()` — including the
idle test's single post-idle capture — not only from the watchdog's
escalation. If a ~2h idle period really does leave the capture endpoint
in a state that needs a fresh `SET_INTERFACE` or halt-clear to recover
(the leading hypothesis above), then v0.1.7/v0.1.8's own fix may simply
have silently fixed it before the watchdog ever got a chance to notice
anything was wrong — the capture would still show up as "succeeded on
the first attempt, zero watchdog lines," indistinguishable from "idle
alone was always harmless." The idle test as run cannot tell these
apart. v0.1.5 (no `usb_clear_halt()` at all) would be the correct module
to re-run this specific idle test against to actually distinguish them —
not done here.

### Status: investigation parked

No further active investigation planned at this time. The watchdog's
logging (stage transitions, `clear_halt` outcomes, the "status read
succeeded while streaming" hint) is now in place and will identify the
mechanism directly if the stall recurs in normal use, without needing
another dedicated reproduction session.

## Automated REAPER test (v0.1.8)

Full automation: `tests/hw/reaper_test.lua` (ReaScript) launched via
`reaper -nosplash -new tests/hw/reaper_test.lua`, driving REAPER through
project setup, a 10s recording, a save, and 5x 2s record/stop cycles.
`~/.config/REAPER/reaper.ini` was backed up first (`reaper.ini.bak_20261012_054816`)
and restored exactly afterward.

### reaper.ini ALSA keys — verified empirically, not guessed

Researched first (REAPER Linux documentation, a real working dotfile
example pairing `linux_audio_mode=0` with populated `alsa_indev`/
`alsa_outdev`), then **checked against actual behavior** rather than
trusted outright — which caught the research being wrong for this
install:

| `linux_audio_mode` | Result |
|---|---|
| `0` (web-evidenced guess) | REAPER tried JACK (`jack server is not running` in stdout), never touched ALSA — **0 kernel log lines**, 0 channels visible |
| `3` (this install's own prior value) | Also 0 kernel log lines, 0 channels — not ALSA either on this REAPER version |
| `1` | **6 input + 4 output channels visible**, real driver activity (`pcm open`, `usb_clear_halt(...) ok` x3, `cap complete`) — confirmed correct |

`alsa_indev=hw:PhaseX64` / `alsa_outdev=hw:PhaseX64` (matching the ALSA
card ID, same as every `aplay`/`arecord` invocation all session) were
correct once paired with the right mode. Final verified config: mode=1,
`alsa_indev`/`alsa_outdev=hw:PhaseX64`, `linux_audio_srate=48000`,
`linux_audio_bsize=512`, `linux_audio_bufs=2`, `linux_audio_nch_in=6`,
`linux_audio_nch_out=4` (full device channel count, so track routing can
select specific pairs — not restricted to 2).

### `reaper_test.lua`: ReaScript API choices, verified before use

Each of these was checked against REAPER API documentation/real examples
rather than assumed, per instruction:
- `I_RECINPUT = 1024 + n`: bit 1024 flags a stereo pair, `n` = 0-based
  index of its first channel. Hardware inputs 3/4 -> `n=2` -> `1026`.
  Confirmed via the script's own readback logging matching, and via the
  saved project's `REC` line.
- Hardware-output sends: `CreateTrackSend(track, nil)` (nil destination
  = hardware output, not a track-to-track send), then
  `SetTrackSendInfo_Value(track, 1, sendidx, "I_DSTCHAN", 2)` — category
  `1` specifically means hardware output for get/set-send-info functions
  (confirmed via REAPER API search results, consistent across two
  independent queries; one unofficial third-party MCP wrapper's example
  used category `2`, inconsistent with the official docs and not used
  here).
- Transport action IDs `1007`/`1013`/`1016` (Play/Record/Stop),
  `40023` (New project) — confirmed via a working code example.
- `RECORD_PATH` project key, `Main_SaveProjectEx`/`RecursiveCreateDirectory`
  signatures — confirmed via REAPER API search results.

### Result: REAPER-side instability found, not a driver bug

**No clean, complete run of `reaper_test.lua` was achieved** with a
genuinely-open audio device. With the correct ALSA config, every
attempt hit the same wall: a continuous low-level playback
retrigger loop (`pcm trigger: Playback cmd=1` -> `cmd=0` -> `pcm
prepare` every ~150-240ms) starts once a stream is active, and calling
transport Stop (`Main_OnCommand 1016`) after an **armed, actually-recording**
track hangs REAPER's main/script thread indefinitely (confirmed stuck
for a full 150s test) while the retrigger loop keeps running
in the background.

Isolated precisely before giving up:
- **Not a buffer-size issue**: identical hang at `bsize=512/bufs=2`,
  `bufs=3` (the "known-good" reference value), and `bsize=2048/bufs=3`.
- **Not specific to the custom hw-3/4 routing**: a minimal test using
  only default channel routing (hw out/in 1/2, no custom sends)
  reproduces the identical retrigger pattern and the identical hang.
- **The retrigger loop itself isn't what hangs REAPER**: a pure-playback
  test (`Main_OnCommand 1007`, no armed recording track at all) shows
  the *same* retrigger pattern — because REAPER always opens both
  directions regardless of whether anything is recording — and
  completed cleanly (`exit=0`) in 5s. The hang is specifically tied to
  calling Stop after a real, armed recording.
- **The driver stayed completely healthy throughout every single
  attempt**: 0 `capture watchdog` firings (capture completions never
  actually stopped flowing, even during the chaos), 0 `clear_halt`
  failures (all succeeded), 0 kernel `WARNING`/`BUG`/`Oops` lines, and a
  clean, immediate teardown (`pcm hw_free`/`close` on both directions)
  the moment the hung REAPER process was finally killed by the external
  `timeout`. A plain `arecord` sanity check immediately after confirmed
  the device fully healthy and unaffected.
- The one real-audio recording obtained (hw out 3/4 -> hw in 3/4, the
  external RCA loopback) showed **pure noise floor** (~-96dBFS, no 1kHz
  content), consistent with the retrigger instability preventing any
  coherent audio from actually transferring, not a mixer/cabling
  problem — `In 3/4 Capture Source` and `Digital Input Lock` were both
  confirmed correctly set at the time.

This looks like a genuine REAPER-ALSA-backend instability specific to
this device (or this REAPER version/build combination), not a driver
bug — the kernel side never faulted, never warned, and recovered
instantly once REAPER itself was gone. Not root-caused further; flagging
for whoever revisits REAPER automation with this device. The two
non-dev_dbg kernel log line categories across the whole session: `status
read failed` x10 (`err=-32` x7, the already-known Finding B EPIPE under
load; `err=-110`/ETIMEDOUT x3, new, all during final device teardown as
REAPER was being killed — a benign teardown race, not a new stall
signature). Zero `capture watchdog`, zero `clear_halt` failures, zero
`WARNING`/`BUG`/`Oops`.

| Check | Result |
|---|---|
| Correct ALSA config found | Yes — `linux_audio_mode=1` (not 0 or 3), verified empirically |
| `reaper_test.lua` completed cleanly | **No** — hangs on Stop after a real recording, every attempt |
| Driver health during all attempts | Healthy — 0 WARN/BUG/Oops, 0 watchdog firings, 0 `clear_halt` failures, clean teardown every time |
| Recorded audio content | Noise floor only — consistent with the REAPER-side instability, not a mixer/cabling issue |
| `reaper.ini` restored | Yes, confirmed byte-identical to the backup |

`tests/hw/reaper_test.lua` is committed as the intended automation for
whenever this REAPER-side issue is resolved or worked around (e.g. not
calling Stop via the same action, or investigating REAPER's own audio
thread further) — not deleted, since the ALSA config and ReaScript API
usage in it are now verified correct.

## "Capture dead, zero URBs" root cause and fix — diag3 through diag7

Interactive session, module built but never loaded by the assistant;
every `rmmod`/replug/`insmod` done by the user, every `arecord`/analysis
step done by the assistant. Starting point: the v0.1.8-diag1 wire-format
investigation (previous section) had found the ~1.42-1.65x capture
pacing deficit was real and rate-independent, then stumbled onto a
second, separate, far more serious bug live during a rate sweep: capture
sometimes never starts at all (0 URB completions, `EIO` in ~0.6s).

**Diff against v0.1.5 (commit `682ef29`) and the Windows decomp**: the
status-byte write order/timing (settle, `SET_CUR` rate x2,
read-modify-write with the 40ms delay) is byte-for-byte identical
between v0.1.5 and `main`, and matches the Windows driver's own
documented sequence - no `clear_halt()`/reset-pipe equivalent found
anywhere in the Windows decomp. The user's own fresh-replug reproduction
(real hardware, outside this session) showed: v0.1.5 streams fine from a
genuinely fresh `0x16` status byte; v0.1.8 with a *skipped* status write
(byte already `0x36`) also streams fine; v0.1.8 with a *real* `0x16`→
`0x36` transition is 100% dead. This pointed at v0.1.7's `clear_halt()`
calls, issued right after a genuine status transition, as the suspect.

**`last written status byte=0x00`** (seen during the original rate-sweep
stall) is explained by code, not a device reset: `px->last_status_byte`
is zero at `kzalloc` time and only set by a successful
`px_status_write()`; `px_probe()`'s own status update
(`STATUS_INTERNAL_CLOCK, STATUS_INTERNAL_CLOCK`) is a same-bit no-op
that always computes `new == old` (that bit is already set even at a
fresh `0x16`), so it never actually writes - the cache sits at `0x00`
until the first real stream start. Seeing `0x00` just means that start
was the first since the device was last genuinely fresh; what put the
device back into that state mid-session was never conclusively
identified (candidates: real replug, silent autosuspend/resume, an
untracked reload) - `px_suspend()`/`px_resume()` had zero logging at the
time, since fixed (see diag5 below).

### diag3 — module params, no behavior change yet

`status_mode` (0=current, 1=no `clear_halt()` at all like v0.1.5,
2=mode 0 + `status_settle_ms` delay before `clear_halt()`),
`status_settle_ms`, `cap_urb_bytes` (replacing the `CAP_URB_BYTES`
macro), plus a once-a-second `pr_info()` capture summary (completions,
status tally, wire bytes/s, frames pushed to ALSA, out-of-order
ring-slot completions) replacing diag1's hex dump.

**Row A** (`status_mode=1`, fresh replug): dead, 0 completions - directly
contradicted the clear_halt hypothesis. **Row B** (same load, no
replug, x2): both clean, ~913-914 completions/s, ~1.869 MB/s wire,
~29232 frames/s to ALSA (~61% of 48000) - the known pacing deficit,
confirmed independent of `clear_halt()`.

### diag4 — `status_at_probe`, `cap_wd_ms`

**T1** (`status_mode=1 cap_wd_ms=0`, watchdog fully off, x3 same load):
all 3 dead, 0 completions every time - with no alt-cycle ever possible,
the device never streams no matter how many retries. **T2** (same
params, default watchdog, fresh replug, x3): all 3 **succeeded**, zero
watchdog firings in any of them. T2's params are identical to the
earlier Row A, which was dead - same params, opposite outcome. **The
dead-start condition is intermittent, not a deterministic function of
`status_mode`/`cap_wd_ms`/a fresh transition alone.**

Also found and fixed during this phase: autosuspend was investigated as
a hidden variable (the device is unbound, hence unprotected by the
driver's `supports_autosuspend=0`, during the blacklist gap between
replug and `insmod`) via a P-auto (3 cycles, `power/control=auto`) vs
P-on (3 cycles, `power/control=on`) experiment, logging
`runtime_suspended_time` before/after each. **Result: P-auto 3/3 dead,
P-on 1/3 dead with zero autosuspend ever recorded** - autosuspend is not
proven as a cause (P-on-1's dead start had `runtime_suspended_time=0`
throughout), though `no_autosuspend=1` (default on,
`usb_disable_autosuspend()` at probe) was kept in diag5+ as a cheap
safeguard regardless.

### diag5 — `start_alt_cycle`, continuous watchdog (first version), `no_autosuspend`

Hypothesis H: the device's streaming engine starts on an `intf1` alt
0→1 cycle issued *after* the status bits are set, and keeps running
after a stream stops; the next start works because the *previous*
watchdog stage-1 alt-cycle already woke it. `start_alt_cycle=1`
proactively does that cycle in the normal start path, after the status
write, before submitting capture/feedback URBs.

**5 fresh-replug cycles, `status_mode=1 start_alt_cycle=1`: 5/5 alive**
(two needed one watchdog-driven alt-cycle recovery attempt, the other
three were clean from trigger). p≈0.002 against the day's ~70% dead-start
rate by chance - `start_alt_cycle` is the fix for the fresh-start dead
condition. Also ran 2 more recordings clean, then a rate-change probe
(48k→44.1k) that failed: **44.1kHz had never once streamed successfully
anywhere in this entire session's log** - 2 good completions (leftover
FIFO) then silence, watchdog attempt 1/3 fired too late (504ms, vs
`arecord`'s own ~554ms give-up) to save it. The watchdog design at this
point stood down forever after the *first-ever* completion, which this
session's "continuous" rewrite (checking elapsed-since-last-real-data
instead of a monotonic counter) was built specifically to fix - and it
did catch this later-than-trigger stall, just too slowly.

### diag6 — `rate_settle_ms`, mandatory alt-cycle on rate change, faster watchdog

Code audit (no hardware) confirmed `start_alt_cycle` ran, in the right
order (after both `SET_CUR` calls and the status write), but with
*zero* settle delay before it when the status byte didn't transition
(44.1kHz's byte was already `0x36`, write skipped, so the mode-1
settle-on-transition logic never applied). Windows decomp: the
`SMALL_BUFFER` status bit is set at stream start from a buffer/period
frame-count field set alongside the rate as one atomic struct update
(`FUN_f1011140`) - no rate-dependent override found. diag6 adds
`rate_settle_ms` (200ms default) and makes the alt-cycle **mandatory on
any rate change**, regardless of `start_alt_cycle`; the watchdog now
checks every fixed `CAP_WD_CHECK_MS`=50ms with `cap_wd_ms` (default
150) as the stall threshold, instead of the check interval.

**48k→44.1k→48k→96k→48k, 5s apart: 44.1kHz succeeded for the first time
ever** (first-start stall at 48k #1 recovered in 150ms via 1 watchdog
attempt - much faster than diag5's 504ms). **96kHz failed** (`EIO`,
0.725s) with a brand new, different symptom: the watchdog **never
fired at all** despite ~490ms of genuine silence past the 150ms
threshold - not explainable by stale gen/gave-up state from earlier
cycles (verified clean). Flagged for audit rather than guessed at.

### diag7 — watchdog audit + hardening, decomp re-check, final rate/duplex matrix

Audit of every arm/cancel/requeue/gen site, against three candidate
mechanisms:
- **(a)** rate-change path bumping `cap_wd_gen` after arming: not
  found - `cap_wd_gen` is only ever incremented once, in
  `trigger(START)`, before scheduling.
- **(b)** liveness timestamp updated by zero-length/error completions:
  **confirmed, real bug** - `cap_last_complete_jiffies` was updated for
  *any* non-shutdown-status completion, not just genuine
  `status==0 && actual_length>0` ones. Fixed. (Checked by hand against
  the actual 96kHz log first: ruled out as *that specific* incident's
  cause, since the log showed genuinely zero completions in the silent
  window, hidden or not - but a real latent bug regardless.)
- **(c)** requeue only happens under a condition untrue on this path:
  structurally plausible (`schedule_delayed_work()` is a no-op if
  already pending; the old non-sync `cancel_delayed_work()` at
  `trigger(STOP)` plus several early-returns-without-rescheduling could
  silently end the chain with zero log trace) but not confirmed as the
  96kHz mechanism specifically. Closed anyway: the watchdog now
  unconditionally reschedules itself first thing, every tick; the only
  thing that ever stops it is `cancel_delayed_work_sync()` in
  `px_stop_streaming()` (process context, confirmed safe at every call
  site: `hw_free`/`close`/`hw_params`/failed-start/`suspend`/
  `disconnect`). Added a ratelimited `pr_info` diagnostic tap (gen,
  stall_ms, attempt) per tick.

Decomp re-check: the capture bit-decoder is selected by **channel count
only** (a 4-entry table: 6/8/10/12ch), never by rate - the same decoder
handles every rate on this 6-channel device, and no rate-conditional
branch exists near the `SMALL_BUFFER` computation either.

**Final matrix, `status_mode=1 start_alt_cycle=1`, diag7:**

| recording | result | real time | notes |
|---|---|---|---|
| 48k #1 | alive | 17.7s | 1 watchdog attempt, recovered |
| 44.1k | alive | 15.3s | clean |
| 48k #2 | alive | 16.7s | clean |
| 96k | alive | 34.2s | clean, 0 watchdog firings |
| 48k #3 | alive | 17.7s | clean |
| 48k (buffer-time=2s) | alive | 16.5s | clean |
| 44.1k (buffer-time=2s) | alive | 15.9s | clean |
| 48k (buffer-time=2s) | alive | 16.7s | clean |
| 96k (buffer-time=2s) | **dead** | 1.7s | see below - OPEN, not a stall |
| duplex (48k, arecord+aplay) | alive | 10.0s nominal | 0 watchdog, 0 WARN/BUG |

**96kHz's real-time ratio (34.2s/10s ≈ 3.42x) is a clean, direct
confirmation of the original fixed-throughput-cap hypothesis**: the
per-second summary showed the exact same ~1.868-1.870 MB/s wire
bytes/s and ~29216-29248 frames/s pushed to ALSA as every 44.1k/48k
measurement all session - rate-independent, as suspected from the very
first rate-sweep. At 96k this fixed absolute rate is a much larger
fraction short of nominal (~30% of 96000 vs ~61% of 48000), so the
real-time inflation roughly doubles.

**96k+buffer-time=2s failure (build: diag7, `arecord --buffer-time=2000000`)
— OPEN, not a stall, not yet fully explained.** A ~30-39% shortfall (44.1k/48k) still advances `hw_ptr`
steadily and never trips a failure; 96k's shortfall is more severe
(~70% short, i.e. actual throughput is under a third of nominal - see
below) and does. Numbers from a fresh, precisely-sampled run:

- `hw_ptr` trajectory (`/proc/asound/card3/pcm0c/sub0/status`, 200ms
  apart): `+0.4s hw_ptr=5600` -> `+0.6s hw_ptr=11584` (both `appl_ptr=0`
  - `arecord`'s `read()` never returned any data at all before giving
  up). Delta: (11584-5600)/0.2s = **29920 frames/s**, continuously
  advancing, never frozen.
- Diagnostic tap confirms the same the whole window: `stall_ms` stayed
  at 1-2ms on every 50ms tick (real completions never stopped
  arriving).
- `hw_params`: `period_size=21845`, `buffer_size=43690` frames - the
  requested 2s buffer-time was clamped by `buffer_bytes_max=1MB` to
  ≈0.455s.
- Completions/wire-bytes/frames-to-ALSA for this exact short (502ms)
  run didn't live long enough for a 1-second summary, but the earlier
  *successful* plain 96k run's summaries give the same-regime numbers:
  completions≈913-914/s, wire_bytes≈1.868-1.870 MB/s,
  frames_pushed≈29216-29248/s - **30.4% of the 96000 nominal rate**,
  consistent with the live `hw_ptr` delta above.
- Trigger-to-giveup was **502.4ms**. `1.1 x (buffer_size at the nominal
  96000 rate)` = 1.1 x (43690/96000s) = **500.6ms** - a 2ms match. This
  suggests ALSA's internal cutoff may be a fixed multiple of the
  *nominal-rate* buffer-fill time, independent of whether `hw_ptr` is
  actually moving - but this is an inference from one close numerical
  match, not confirmed against ALSA/alsa-lib source, and is being kept
  open rather than written up as settled. Not patched in diag7; no
  code change proposed for this specific symptom pending further
  confirmation.

### Proposed for v0.1.9 (built afterwards as `9ccb276`, accepted - see "v0.1.10" below)

> **Validity pending re-test (2026-10-14):** items 1 and 2 below - "clear_halt
> is harmful" and "the alt-cycle is required for dead starts" - were both
> concluded with the S/PDIF digital loopback cable connected (fresh status
> `0x16`, digital input locked, capture clock following the ~32kHz S/PDIF
> input). See "Windows USB trace" and "Confirmed without the S/PDIF cable"
> near the end of this file. Not yet re-tested without the cable (that is
> step 2, in progress) - treat both as open again until they are.

1. Drop `clear_halt()` from the normal start path entirely (match
   v0.1.5/Windows); keep it only as the watchdog's own stage-0-style
   recovery action if reintroduced there.
2. Always cycle `intf1` alt 0→1 before the first capture/feedback URB
   submission on every start, and again on any rate change, with a
   settle delay after `SET_CUR`/the status write (`rate_settle_ms`-style,
   ~200ms).
3. Ship the diag7-hardened watchdog as the real implementation: liveness
   timestamp gated on genuine data only, unconditional self-requeue,
   `cancel_delayed_work_sync()` in `px_stop_streaming()` as the sole stop
   mechanism, ~50ms check cadence, ~150-250ms stall threshold, capped at
   3 recovery attempts per stall episode before giving up and going quiet
   until data resumes.
4. `usb_disable_autosuspend()` at probe (cheap safeguard, not proven as
   *the* cause but correct regardless).
5. 96kHz + large `--buffer-time` EIO (see numbers above) stays an
   **open item**, not a documented limitation - not fixed by this
   release, needs more investigation before it's characterized either
   way.
6. `in_elapsed` was re-checked this session and found already correct in
   committed v0.1.8 (`atomic_inc()` already outside the per-frame decode
   loop) - an earlier assessment calling it a pending bug was wrong; no
   change needed. `px_refresh_lock_cache()`'s double-call-site/live-read
   timing issue (flagged in the v0.1.8 regression section above) is
   still outstanding and still planned for v0.1.9.
7. Bump `MODULE_VERSION` to `"0.1.9"`, build `W=1`, commit, `git push
   origin main`. Not built this session - diag3 through diag7 are all
   uncommitted, throwaway diagnostic builds per usual project convention.

# v0.1.10 — capture throughput shortfall investigation

v0.1.9 was accepted (3x fresh replug, 3x in a row, rate changes, duplex,
60s - all clean). Next question: a 60s capture nominally takes ~95.8s
real time. This section covers the throughput investigation; it is not
yet resolved.

## Ruled out, with no effect on throughput

- Host-side URB buffering: `cap_urbs` (8-64), `cap_urb_bytes` (512-16384),
  `fb_urbs` (4-32) - no change at any setting.
- An alternate "large buffer" Windows DMA decoder for 6-channel capture:
  `chooseDMADecoder` requires `SMALL_BUFFER` for channel counts 2-16; no
  large-buffer variant exists for our channel count.
- Removing the extra playback `SET_CUR` (`skip_play_rate_setcur`, matching
  the Windows decomp's capture-only start path) - no change.
- Fully replicating the open-source Ozzy driver's richer init handshake
  (github.com/mischa85/Ozzy, MIT; `ozzy_handshake=1` - firmware read,
  status read, boot-rate read, `SET_CUR` x2, verify read, 50ms sleep,
  status re-read, confirm-write, 500ms sleep, `clear_halt`) - ran clean,
  zero watchdog firings, but steady-state throughput identical to
  baseline (~882-914 completions/s, ~1.80-1.87 MB/s, ~28200-29250 fps).
- `setcur_repeat` (N alternating capture/play `SET_CUR` pairs, matching
  the official Windows driver's 5-7x pattern) at N=6 - also identical to
  baseline within run-to-run noise (96.1-96.7% `dup_check` +1 fraction,
  same band as the existing 96.6-98.3% baseline, which already contains
  the same `+7`/`-71`-class deltas). The combination of
  `ozzy_handshake=1` + `setcur_repeat=6` was not tested separately - both
  variants alone already land in the same band as baseline, so the
  combination was judged not worth the extra hardware cycle.

## False alarm: "inverted output-switch polarity"

Mid-session, by-ear testing suggested `Direct 1/2 Playback Switch` (and
by extension the other `px_switches[]` entries) might be active-low
against the driver's straightforward get/put logic, which would also
explain the analog loopback test's persistent failure. A clean,
isolated amixer-only A/B test (one switch at a time, panel closed, no
module reload in between) gave an unambiguous result: `on` -> audible,
`off` (with PCM and Mono also off) -> silent - exactly the driver's
existing, non-inverted behavior. **No polarity bug; no code change.**
The original report was very likely a stale-panel artifact: the GUI
checkbox doesn't re-poll after a module reload, and this session did
several reloads (stuck `alsactl monitor` processes from the panel) that
reset the codec registers to `px_codec_init()`'s defaults underneath an
unrefreshed display. Follow-up (separate project, `phasex64-panel`):
the panel should re-read mixer state on open and after a device
reload/reconnect.

Side observation while isolating this: L/R correlation on a live Phono
capture was 0.51, with comparable L/R RMS - genuinely stereo, not a
mono-summing bug in the Direct monitor path. The contemporary guess that the
thin sound came from the turntable/preamp or hardware Aux/Phono switch was
later disproved. See "Thin Phono sound: Windows capture RIAA DSP" below.

With the switches cleared of suspicion, the loopback retest confirmed
the tone reaches In 3/4 (output routing is fine) but still shows the
same ~1269Hz-instead-of-1000Hz distortion and now also a mid-capture
dropout - both symptoms of this same throughput shortfall (at ~61% of
real-time, a 25s tone file run against a nominal 22s capture window
runs out of signal before the capture buffer finishes filling), not a
new bug.

## GET_CUR(SAMPLING_FREQ) is a dead stub on this device

> **Correction (2026-10-14, Windows USB trace):** this conclusion is wrong.
> During every Linux session a coax S/PDIF loopback cable (digital out ->
> digital in) was connected. With that cable the device reports status bit 2
> ("digital input locked", fresh power-up `0x16`) and `GET_CUR` returns the
> rate of the digital input, 32000 Hz - in the Windows trace too, where the
> official driver then adopts 32000 Hz. Without the cable the fresh status is
> `0x12` and `GET_CUR` returns the real active rate (44100 before any
> `SET_CUR`, then 48000 after `SET_CUR 48000`). `GET_CUR` works; it was
> reporting the S/PDIF-locked clock. See "Windows USB trace" at the end.

`ozzy_handshake`'s rate-verify step reliably warned `device reports
32000 Hz instead of 48000 Hz` on every run, which looked like it could
be *the* lead: a capture clock that never actually reaches 48kHz would
explain the throughput shortfall, the wrong tone pitch, and why no URB
tuning helped. `NOTES.md`'s existing control-request table (from the
generic `msnkphsu.sys` decomp) documents the Windows driver querying
`GET_CUR` at `wIndex=0x86` (the real capture endpoint) for "read back
sample rate", separately from `wIndex=0` ("digital input rate; 192000 =
not locked") - i.e. the official driver never reads back at `wIndex=0`
the way our `ozzy_handshake`/`px_set_rate_repeated` verify step (and
Ozzy's own `hs_read_rate`) do.

Built `rate_probe=1` (v0.1.10-pre5): dumps `GET_CUR` raw bytes + decoded
value at `wIndex=0`, `wIndex=EP_CAPTURE` (0x86), and `wIndex=EP_PLAY`
(0x02), at 6 stages of stream start (pre-`SET_CUR`, post-capture-
`SET_CUR`, post-play-`SET_CUR`, post-status-update, post-alt-cycle,
post-URB-submit-while-running), logged via `dev_info` (always visible,
no `dyndbg` needed). Tested at 44100 and 48000 Hz, fresh replug, normal
(non-`ozzy_handshake`) path:

**Every single read - all 3 `wIndex` targets, all 6 stages, both
rates, over 50+ reads - returned identical raw bytes `00 7d 00` (32000
decoded).** Querying the real endpoint instead of `wIndex=0` (the fix
the Windows-driver evidence suggested) made no difference whatsoever.
Perfect invariance across every variable tested is the signature of a
fixed/boot-time stub value, not a live clock readback - this device's
firmware does not implement a working `GET_CUR` for this control. It
also doesn't match `NOTES.md`'s documented `wIndex=0` "digital input
rate; 192000 = not locked" semantics either (we see 32000, never
192000, regardless of lock state) - so either this specific model/
firmware revision (0a4a:5b01) behaves differently from whatever
model(s) the generic decomp was captured from, or this control was
never wired up on this hardware at all.

**Conclusion: the "32000 Hz" warning is not evidence of a stuck device
clock - it's evidence that this GET_CUR can't be used to check the
clock.** `ozzy_handshake`'s verify-mismatch warning should be read as
inconclusive, not as confirmation of a real rate problem. This lead is
closed; `rate_probe` is left in the tree as a diagnostic in case it's
useful later, but the wIndex angle itself is not pursued further.

## The real signature: a flat ~1.8 MB/s cap, not a wrong clock

> **Invalid (2026-10-14): measured with the S/PDIF loopback cable
> connected.** The flat cap is simply ~32000 fps x 64 B - the device
> following the locked digital input clock, not a device-firmware pacing
> limit. See "Confirmed without the S/PDIF cable" at the end: without the
> cable, throughput tracks the requested rate exactly at 44.1/48/88.2/96k.

`rate_sweep_timing.sh` (10s nominal capture at each rate, same
`rate_probe=1` load):

| rate | real time | ratio (real/nominal) | wire throughput |
|---|---|---|---|
| 44100 Hz | 15.35s | 1.535 | 1.84 MB/s |
| 48000 Hz | 17.03s | 1.703 | 1.80 MB/s |

The ratio *grows* with the requested rate, but the absolute wire byte
rate stays flat (~1.8 MB/s either way). A proportionally-wrong clock
(e.g. a device really running at a fixed fraction of whatever rate was
requested) would show a *constant ratio* across rates, not a constant
absolute byte rate - so this is a fixed throughput ceiling, independent
of the sample rate, not a clock-rate error. It's also consistent with
the entire session's URB-tuning results: nothing that only changes
buffer counts/sizes can lift a hard ceiling. The capture endpoint (EP
0x86) is bulk, 512-byte packets (`lsusb -v`), not interrupt, so there's
no Ozzy-style bInterval/microframe arithmetic that would explain a cap
this low from the USB transport side - the ceiling looks like it
originates in the device's own firmware pacing of the bulk-IN
endpoint, not the USB bus. Not chased further this session; parked for
the Windows/REAPER USB trace (see below).

## New, separate bug: 88.2 kHz / 96 kHz break the capture engine

> **Invalid (2026-10-14): measured with the S/PDIF loopback cable
> connected.** Re-tested without the cable (see "Confirmed without the
> S/PDIF cable" at the end): both rates now run cleanly at full real-time
> throughput with zero errors. Whatever caused the `-32` endpoint stall
> here was specific to the cable-locked 32kHz-vs-88.2k/96k-requested
> mismatch, not a standalone 88.2k/96k bug in this driver.

(Build/setting: v0.1.10-pre5 with `rate_probe=1`, `rate_sweep_timing.sh`,
10 s nominal `arecord` per rate, default buffer-time. Not the same run as
the diag7 96k+buffer-time=2s EIO above, which streamed at ~29.9k fps for
502 ms before EIO; here both rates die after ~1 s with endpoint stalls.)

Discovered incidentally during the rate sweep. Both rates fail almost
immediately (`arecord: pcm_read: read error: Input/output error`,
~1-1.1s real time instead of the nominal 10s). Log sequence (`dmesg`):
`usb_set_interface failed: -32` (EPIPE, endpoint stall) during the capture
watchdog's recovery attempt, all feedback/capture URB resubmits
failing with `-2` (ENOENT), a `status read failed... err=-32`, and the
watchdog exhausting all 3 recovery attempts and giving up. The device
self-recovers on the next 48kHz stream open (no replug needed) - not
fatal, but 88.2k/96k are effectively non-functional right now. Not
investigated further this session; `.rates` in `px_play_hw`/`px_cap_hw`
still advertises `SNDRV_PCM_RATE_88200 | SNDRV_PCM_RATE_96000` - worth
reconsidering whether to advertise rates that currently fail this
badly, but no change made pending root-causing it.

## Next step: USB trace under Windows 11 + REAPER (ASIO)

Every host-side and protocol-sequence hypothesis tested so far (URB
counts/sizes, handshake replication, SET_CUR repetition, GET_CUR
wIndex) has come back negative or inconclusive. The only untried source
of ground truth is a real USB capture of the device working correctly
(Windows 11, official driver, REAPER via ASIO) to compare against what
our driver actually sends. `tests/hw/pcap_analyze.py` (tshark-based) is
now in the tree - see its own header for capabilities, and the results
below. No further
guessing at the pacing mechanism until that trace exists.

## Windows USB trace (2026-10-14): the capture cap is the S/PDIF loopback cable

Setup: the same AMD Ryzen 7 5800H laptop (Renoir/Cezanne xHCI), Windows 11 25H2, official driver 2.9.34,
REAPER 7.81, USBPcap 1.5.4 on both root hubs (card on USBPcap2, bus 2
device 2), tshark 4.6.8, analysed with `tests/hw/pcap_analyze.py`. Input
1/2 = Phono (turntable), output 1/2 = playback. The `.pcapng`/`.wav` files
stay local in `tests/hw/captures/` (gitignored).

| Trace | Situation | 0x86 capture | 0x02 playback | feedback 0x81 | status writes |
|---|---|---|---|---|---|
| `digital_loop_32k` | S/PDIF out->in cable connected, plug-in + REAPER ASIO | 0.749 MB/s, 11.7k fps, 4317 cancelled URBs | 13.1k fps, 4 frames/uframe | bytes 28/32/60 | `0x36` x700 |
| `asio_small` | cable removed, fresh plug-in, ASIO 48 spls, 44.1k -> 48k | 2.88 MB/s @44.1k, 3.08 MB/s @48k | 5/6 frames/uframe | `2c 2c 2c` / `30 30 30` | `0x32` x4 |
| `asio_large` | ASIO 1024 spls, 48k | **3.072 MB/s = 48000 fps** | 47988 fps, 72 B = 6 frames/uframe | `30 30 30` (48) | none (no restart) |
| `wdm` | REAPER WaveOut/DirectSound, 48k | 3.066 MB/s = 47.9k fps | 47.9k fps | 48 | `0x32` x2 (codec change) |

### What the cable does (`digital_loop_32k`)

Fresh status `0x16` (bit 2 = digital input locked), `GET_CUR(wIndex 0)` =
32000 -> the official driver sets `SET_CUR 32000` on 0x86/0x02 and writes
`0x36`; it then restarts the stream about once per second for the whole
13 minutes (700 x SELECT_CONFIGURATION, SET_CUR, status write, ABORT_PIPE +
SYNC_RESET_PIPE_AND_CLEAR_STALL on 0x86), capture runs at ~0.75 MB/s and
REAPER can neither record nor monitor; the control panel lights no sample
rate. Removing the cable fixed Windows immediately (fresh status `0x12`,
panel shows 44.1, REAPER records/monitors).

**Every Linux measurement of the "flat ~1.87 MB/s / ~29.2k fps cap" was
made with this cable connected** (fresh status was always `0x16`, GET_CUR
always 32000). Leading hypothesis, not yet verified on Linux: with bit 2 set
the device's capture engine follows the S/PDIF input clock (32 kHz from its
own S/PDIF output), so capture is paced near 32k fps whatever rate
`SET_CUR` asks for. Playback is paced by feedback and still looked real-time.

### Answers

1. **Bulk-IN 0x86 under Windows:** 3.072 MB/s at 48 kHz (exactly 48000 x 64
   B/frame, `asio_large`), ~2.82-2.88 MB/s at 44.1 kHz. Windows queues 8 bulk
   URBs of 131072 bytes (2048 frames, one completes every ~42.7 ms).
2. **Start sequence (official driver, fresh plug-in, no cable):**

   | t | event |
   |---|---|
   | 0 | STATUS read -> `0x12` |
   | +1.4 ms | `GET_CUR(wIndex 0)` -> 44100 |
   | +0.5 ms | WM8776 writes R7, R21, R22 for codec 0 and 1 (panel state) |
   | +8.9 ms | `SET_CUR 0x86`, `SET_CUR 0x02`, `SET_CUR 0x86` (3 bytes LE) |
   | +0.3 ms | `GET_CUR(0x86)` -> verify |
   | +0.4 ms | STATUS read, STATUS write `0x32` (written even when unchanged) |
   | +88..175 ms | `ABORT_PIPE 0x86`, +1 ms `SYNC_RESET_PIPE_AND_CLEAR_STALL 0x86` |
   | +0.2 ms | submit 0x86, 0x81, 0x02, 0x83 |

   Restarts without a rate change send only `SET_CUR 0x86`. No
   SET_INTERFACE / alt cycle at stream start: both interfaces are selected
   at alt 1 via SELECT_CONFIGURATION when the device starts.
   Ours (`phasex64.c` v0.1.9): settle, `SET_CUR 0x86`, `SET_CUR 0x02`,
   status read-modify-write (`|0x30`, only written on change), rate-change
   settle, intf1 alt 0 -> 1, submit 0x81, 0x86, 0x02. Differences: no
   `GET_CUR` verify, no unconditional status write, no clear-halt on 0x86,
   an extra alt cycle, different submit order. Status values match
   (`0x32` without cable).
3. **Isoc OUT 0x02 during capture:** always running, also when only
   recording: 40 packets per URB (5 ms), one packet per microframe (~8000/s),
   72 B = 6 frames at 48k, 60 B = 5 frames at 44.1k with feedback-driven
   corrections. Feedback 0x81: 1-packet URBs (64 B requested, 3 B returned),
   one per 1 ms frame, ~20 queued; the three bytes are frames per ms (byte 0
   is what we use). The trace cannot show whether capture is *paced* by the
   isoc OUT stream, since both always run together - same as in our driver.
4. **ASIO small vs large vs WDM:** identical on the wire (same URB sizes,
   rates and feedback). Changing the ASIO buffer did not even restart the
   hardware stream. A control-panel change (Direct 1/2 = R22) makes the
   driver resync: `SET_CUR 0x86`, status write `0x32`, ABORT/RESET on 0x86 and
   0x83. In REAPER only ASIO worked; WaveOut/DirectSound (44.1k/48k) recorded
   nothing although the USB stream ran at full rate - a Windows-side WDM
   problem of the 2011 driver, not relevant for Linux.

WAV check (`--wav`): `asio_small.wav` 22.21 s and `asio_large.wav` 21.14 s
(recording stopped by hand after the time selection), 48 kHz 24-bit stereo,
no sample jumps, clean music signal. Pitch could not be checked exactly with
a record; the wire rate (48000 fps) shows the capture ran in real time.

### Proposed driver change (not built)

1. **First a test, no code:** v0.1.9 on Linux with the S/PDIF loopback
   cable removed and a fresh replug (status must read `0x12`). Expect 3.072
   MB/s on 0x86 at 48k and a real-time ratio of 1.0. Also re-run 88.2/96 kHz
   and the dead-start matrix, which were all measured with the cable in.
2. **Detect the condition:** log the status byte at every start; if bit 2
   is set, read `GET_CUR(wIndex 0)` and `dev_warn()` that capture follows the
   digital input at N Hz. Optionally refuse a `hw_params` rate that differs
   from the locked digital rate while "Clock Source" is internal (the
   official driver silently adopts the digital rate instead).
3. **Match the Windows start sequence** (behind a module param first):
   `SET_CUR 0x86` (+ `0x02`, `0x86` on a rate change), `GET_CUR(0x86)` verify,
   unconditional status write `|0x30`, ~90 ms wait, `usb_clear_halt()` on
   0x86 right before submitting, submit order 0x86, 0x81, 0x02, 0x83, no
   intf1 alt cycle. The earlier "clear_halt kills capture" and "alt cycle
   is required" findings were both made with the cable in (status `0x16` ->
   `0x36`), so they need re-testing before either is kept or dropped.
4. Optional: bulk URB geometry like Windows (8 x 131072 B). Not expected to
   matter - URB tuning never changed throughput.

## Confirmed without the S/PDIF cable (2026-10-14): step 1 of the re-test plan

Hypothesis above confirmed on Linux. Setup: v0.1.10-pre5, `rate_probe=1`,
default params otherwise (fresh boot, `insmod` required loading
`snd_rawmidi` first - a missing dependency, not loaded automatically by
`insmod`, unrelated to this investigation), S/PDIF loopback cable removed,
analog RCA loopback (out 3/4 -> Aux in 3/4) left connected. Before testing:
`Digital Input Lock` = off, `Clock Source` = Internal (both confirmed via
`amixer`, live reads).

12s nominal `arecord` + 1kHz tone via the analog loopback, per rate:

| rate | real time | ratio | `GET_CUR` | wire throughput | fps | tone (FFT / zero-cross) | errors |
|---|---|---|---|---|---|---|---|
| 44100 Hz | 12.224s | 1.019 | 44100 Hz | 2.82 MB/s | 44083-44115 | 1000.0 / 1002.3 Hz | none |
| 48000 Hz | 12.017s | 1.001 | 48000 Hz | 3.07 MB/s | 47984-48015 | 1000.0 / 1000.0 Hz | none |
| 88200 Hz | 12.238s | 1.020 | 88200 Hz | 5.64 MB/s | 88167-88199 | 1000.0 / 1002.3 Hz | none (previously -32 EPIPE + watchdog giveup) |
| 96000 Hz | 12.279s | 1.023 | 96000 Hz | 6.14 MB/s | 95968-96000 | 1000.0 / 1000.0 Hz | none |

Every rate now runs at real-time (ratio ~1.0-1.02, not the ~1.5-1.7x
inflation measured all session with the cable in), `GET_CUR` tracks the
requested rate exactly (not stuck at 32000), wire throughput matches
nominal rate x 64B/frame to within measurement noise, the loopback tone
is clean with zero dropouts at every rate, and 88.2k/96k - which reliably
crashed the capture engine every time the cable was connected - now run
flawlessly. This closes the entire "capture throughput shortfall"
investigation that ran across this whole session (and, per the "Windows
USB trace" section above, invalidates every cable-connected measurement
of it): there was never a driver bug here, only a test setup that
silently re-clocked the device.

Step 2 (in progress): re-test the clear_halt/alt-cycle conclusions above
without the cable, and whether a dead start at a fresh replug still needs
the alt-cycle workaround.

## Step 2 results: neither the alt-cycle nor clear_halt is needed/harmful without the cable

Three configurations, `v0.1.10-pre6`, cable removed, `dyndbg=+p rate_probe=1`:

| config | fresh-replug trials | result |
|---|---|---|
| `start_alt_cycle=0` (no clear_halt either) | 3/3 | alive, 0 watchdog firings, full real-time |
| `start_alt_cycle=1` (default), `clear_halt_delay_ms=90` | 1/1 | alive, clear_halt succeeds, `GET_CUR` correct right after |
| `start_alt_cycle=0`, `clear_halt_delay_ms=90` (Windows-equivalent) | 2/2 | alive, 0 watchdog firings, full real-time |

Both v0.1.9-era conclusions - "clear_halt is harmful" and "the alt-cycle is
required for dead starts" - were artifacts of the cable, not real findings.
Without it, the actual Windows-equivalent sequence (no alt-cycle,
`clear_halt` ~90ms after the status write) runs exactly as cleanly as the
v0.1.9 alt-cycle approach, across 6 total fresh-replug trials with zero
dead starts and zero watchdog firings either way.

## v0.1.10

Decision: ship the Windows-equivalent sequence as the new default, add
proper clock-mismatch handling, and clean out every throwaway diagnostic
param from this investigation (`fb_urbs`, `skip_status_update`,
`skip_play_rate_setcur`, `ozzy_handshake`, `setcur_repeat`, `rate_probe`,
`clear_halt_delay_ms`) - all removed. `cap_urb_bytes` predates this
investigation (already a param in committed v0.1.9) and stays.

**1. Default start sequence** (`px_start_streaming()`): status read,
`SET_CUR` 0x86/0x02/0x86, `GET_CUR` verify, unconditional status write
(written even if unchanged, matching Windows), a settle delay,
`usb_clear_halt()` on `EP_CAPTURE`, URB submit order 0x86/0x81/0x02. No
alt-cycle. `start_alt_cycle=1` (module param, default 0) reverts to the
v0.1.9 sequence (alt-cycle, no clear_halt) as a fallback for a different
unit/firmware; the capture watchdog's own alt-cycle recovery action is
unchanged and always available regardless of this setting.

**2. Clock handling.** Two layers:
- `px_pcm_open()`: if the digital input is already locked when a stream
  opens (not yet running), constrain `SNDRV_PCM_HW_PARAM_RATE` to the one
  live rate `GET_CUR` reports - whether this is a deliberate digital-clock
  setup or an unexpected lock while internal clock is selected (`dev_warn`
  in the latter case only, since that one is the surprising one). This
  steers userspace to request a rate that will actually work, rather than
  negotiating one that can't.
- `px_start_streaming()`: after the `SET_CUR`/`GET_CUR` verify, if the
  digital input is locked and the live rate still doesn't match what was
  requested, refuse to start (`-EINVAL`) with a `dev_warn` naming the real
  rate - defense in depth for the case where the lock engages in the
  window between `open()` and here. No silent capture at the wrong speed
  either way.
- New read-only controls for the panel: `Digital Input Rate` (the measured
  `GET_CUR` value in Hz) alongside the existing `Digital Input Lock`.

**3. Cleanup confirmed**: `W=1` build, zero warnings. `MODULE_VERSION`
`"0.1.10"`. Net change to `phasex64.c`: +220/-326 lines (diagnostic code
removed outweighs the new clock-handling feature).

### Acceptance tests (2026-10-14, cable removed, `dyndbg=+p`, default params)

| test | result |
|---|---|
| 5x fresh replug, first recording each (10s @ 48k) | alive every time: 10.148-10.191s real, 0 WARN/BUG/watchdog, ~3.07 MB/s |
| 44100 Hz, 12s + tone | 12.351s real, `GET_CUR` matched, 0 WARN/BUG/watchdog |
| 48000 Hz, 12s + tone | 12.383s real, `GET_CUR` matched, 0 WARN/BUG/watchdog |
| 88200 Hz, 12s + tone | 12.401s real, `GET_CUR` matched, 0 WARN/BUG/watchdog |
| 96000 Hz, 12s + tone | 12.445s real, `GET_CUR` matched, 0 WARN/BUG/watchdog |
| rate changes 48->44.1->96->48, no replug | all 4 transitions clean, 0 WARN/BUG/watchdog, 0 `GET_CUR` mismatches |
| duplex (aplay+arecord), 10s | 10.146s real, 0 WARN/BUG/watchdog |
| one 60s recording | 60.161s real, 59/59 per-second summaries ~3.07 MB/s (no degradation), 0 WARN/BUG/watchdog |

All real-time ratios 1.0-1.04 across every rate and every trial - no
throughput shortfall anywhere, confirming the "Confirmed without the
S/PDIF cable" finding holds under the full v0.1.10 build, not just the
diagnostic pre-releases.

**Real-use-case test**: 30s capture of a record playing on Phono In 1/2
(`In 1/2 Capture Source = Phono`, 48k). 30.154s real (real-time), L rms
156.4M / R rms 135.9M (genuine music dynamics), L/R correlation 0.60
(stereo, not collapsed), spectrum balance normal for music (energy
rising 20-200/200-2k/2k-8k Hz, falling 8k-20k), zero dropouts (max 1
consecutive identical sample, zero deltas exceeding 20x the local RMS
envelope), 0 WARN/BUG/watchdog. Played back through Out 1/2 for a
listening check.

**Open, unrelated to the driver**: the Out 3/4 -> Aux In 3/4 RCA
loopback (used for the analog 1kHz tone test earlier this session) was
found to not actually be delivering the tone to capture at all, with or
without the cable connected, with either `v0.1.10` or the known-good
`v0.1.10-pre5` binary (identical noise-floor amplitude and ~0
correlation in both cases) - ruling out the driver entirely. This is a
hardware/cabling issue (wrong jack, swapped pair, or similar) on the
test bench, not a regression and not investigated further here. The
analog tone test itself stays unverified pending that hardware check;
every other acceptance item above does not depend on it.

> **Update (2026-10-14, later the same day): this conclusion was wrong,
> and so was the cable hypothesis.** Root cause found: a test-tooling
> bug in `tests/hw/pxtools.py`. Capture data is 24-bit samples
> left-shifted by 8 in the 32-bit container (`value << 8`, see
> `phasex64.c`'s capture decode comment); every listen-back script this
> session fed those raw, un-shifted values straight into
> `write_wav(..., bits=24)`, which packs its input as if already
> correctly scaled - taking the wrong 24 bits of each sample produces
> exactly this kind of loud, high-frequency-dominated noise. The actual
> captured audio was correct the entire time; only the playback-for-
> listening step was broken. Confirmed by re-converting a captured file
> correctly (right-shift by 8 first) and listening again: clean music,
> no noise, on both the Out 3/4 loopback and a live Phono recording (see
> "Codec resync: real bug, or a measurement artifact?" below for the
> fuller story). `pxtools.py` now documents this and provides
> `capture_to_24bit()`. There is no hardware fault on the Out 3/4
> loopback to chase.
>
> **Every "white noise" listening test in this session's write-up used
> `aplay -D hw:...` with a 32-bit WAV that needed the shift above and
> never got it - treat all of them as unreliable, not just the ones
> called out individually.** `pxtools.py` now also has `play_file()`,
> which plays through `plughw:PhaseX64` instead of `hw:`: `plughw`'s
> format-conversion plugin does the standard 32-to-24-bit conversion
> (take the upper 24 bits) automatically, which happens to exactly match
> this device's wire format, so a raw captured file can be played
> directly with no manual bit-shifting step to get wrong. Use
> `play_file()` for every listening check from here on, not a hand-rolled
> `aplay -D hw:...` call.

## Codec resync: real bug, or a measurement artifact? (2026-10-14)

Follow-up session after v0.1.10 shipped: a "thin"-sounding Phono
recording led to `analysis/NOTES.md` "Windows codec init, from USB
trace" (the official driver re-sends all 6 WM8776 registers from the
saved panel state before every stream start; `px_codec_init()` only
writes factory defaults once, at probe). Built `0.1.11-pre1`
(uncommitted): `codec_resync=1` module param re-sends `R7`/`R21`/`R22`
for both codecs from the cached mixer state at the start of every
stream, matching Windows' placement and order exactly; also added
permanent-style logging of every codec write (wIndex/wValue, decoded
codec/reg/value, result) at the single `px_codec_write()` choke point,
so every init write, mixer put, interlock write and resync write is
visible.

**First A/B (invalid)**: `codec_resync=0` vs `=1` without a power cycle
between runs. Important correction mid-session: the WM8776 keeps its
register state for as long as the card has USB power - rmmod/insmod
alone does not reset it. A bad write from an earlier build or test in
this same powered-up session can silently persist into a later "clean"
build's test, making that build look broken too. This exact thing
happened: the v0.1.10 acceptance session had already left the codec in
some state before this A/B began, so neither side of the first A/B was
a true baseline.

**Clean re-test**: full USB power cycle (unplug 10s, replug) before each
run, `dyndbg=+p`, full codec-write log captured.

**Run 1** (`codec_resync=0`, i.e. plain v0.1.10 behaviour): after a
fresh power cycle, `In 1/2 Capture Source` set to `Phono` via `amixer`.
Complete write log for the whole run:

```
codec write: wIndex=0x0102 wValue=0x0e90 -> codec=0 reg=7  val=0x090 err=0   (probe default)
codec write: wIndex=0x0102 wValue=0x2a01 -> codec=0 reg=21 val=0x001 err=0   (probe default, Mic)
codec write: wIndex=0x0102 wValue=0x2c01 -> codec=0 reg=22 val=0x001 err=0   (probe default)
codec write: wIndex=0x0106 wValue=0x0e90 -> codec=1 reg=7  val=0x090 err=0   (probe default)
codec write: wIndex=0x0106 wValue=0x2a04 -> codec=1 reg=21 val=0x004 err=0   (probe default, Aux)
codec write: wIndex=0x0106 wValue=0x2c01 -> codec=1 reg=22 val=0x001 err=0   (probe default)
codec write: wIndex=0x0102 wValue=0x2a02 -> codec=0 reg=21 val=0x002 err=0   (amixer: Phono)
```

Nothing else for the rest of the run (prepare, trigger, 30s of capture):
zero further writes, zero errors, no EPIPE, nothing during streaming.
Final state (codec0 R7=0x090, R21=0x002 Phono, R22=0x001; codec1
R7=0x090, R21=0x004 Aux, R22=0x001) matches the Windows trace's target
values for this panel configuration exactly.

A first listen of this capture, converted for playback with the
(then-undiscovered) `pxtools` bug above, sounded like pure white noise -
which looked like a dramatic confirmation that something was still
broken even with provably-correct registers. **It wasn't**: converting
the same capture correctly (right-shift by 8) and listening again gave
clean, correct music. The codec write log was right all along; the
listening test was lying.

**Conclusion: no evidence `codec_resync` fixes a real bug.**
`px_codec_init()`-at-probe-only (current v0.1.10 behaviour) was already
producing correct audio once measured correctly. At this point in the
investigation, `codec_resync` was not being proposed for v0.1.11 and
`0.1.11-pre1` stayed uncommitted. Matching
Windows' always-resync behavior is defensible as cheap extra robustness
(e.g. against some future, still-hypothetical case where the device
*does* lose this state), but there is no measured problem it solves
today, so there is nothing to recommend shipping it for right now.
`Run 2` (`codec_resync=1`) was not completed, since Run 1 already
settled the question.

The later v0.1.11 design nevertheless proposes `codec_resync=1` by default as
cheap Windows-equivalent robustness. It is explicitly not the thin-sound fix.

**v0.1.11 implementation decision:** the default is now `codec_resync=1`, with
`codec_resync=0` retained as an escape hatch. This does not change the finding
above: resync fixes no demonstrated thin-sound or mixer bug. The source audit
does establish that it cannot restore stale defaults over a user selection:
all mixer/source puts and `px_start_streaming()` serialize on `px->mutex`, and
the resync writes the current `px->codec[][]` shadow rather than factory
constants. A put completed before start is included; a put blocked by start
runs afterwards and wins. The six writes are after the initial status read and
before SET_CUR, in Windows order, with bounded control-transfer retries.
`tests/hw/test_codec_resync.py` locks these source invariants down without
opening the device.

## Thin Phono sound: Windows capture RIAA DSP (2026-10-14)

Root cause is now confirmed. The official Windows USB driver
`msnkphsu.sys`, not the ASIO DLL or the WM8776, creates a per-pair RIAA filter
for product `0a4a:5b01` when the pair's `riaa_enabled` flag is set and its
input-selector bit `0x2` (Phono) is active. Linux v0.1.10 correctly selects the
same physical/codec Phono input, but returns the cartridge signal raw. That is
why Linux sounded thin while Windows did not.

A standard 3180/318/75 us RIAA playback biquad, bilinear-transformed with a
1 kHz prewarp and level-matched at 1 kHz, validates against the Windows
recording to 0.00-0.05 dB from the 31.7 Hz band through 1.6 kHz. Only the
20-25 Hz bands retain a 2.2-2.6 dB difference. The user listened to the
filtered Linux result and confirmed that it sounds like Windows. Full reverse
engineering evidence, coefficients and the analogue-response test are in
`analysis/NOTES.md` ("Windows capture DSP: RIAA") and
`tests/hw/riaa/`.

### Listening-test rule: do not send a 32-bit capture WAV to `hw:`

Every earlier listening check that ran `aplay -D hw:...` directly on a
32-bit capture WAV is unreliable. Capture samples are S24 values left-shifted
by 8 in an S32 container; the PHASE X64 playback endpoint consumes packed
`S24_3LE`. Direct reinterpretation takes the wrong three bytes and sounds like
white noise. From now on use `tests/hw/pxtools.py`'s `play_file()`
(`plughw:PhaseX64`) or convert with SoX before playback. Never hand a
non-`S24_3LE` file directly to `aplay -D hw:PhaseX64`. A repository search
found no remaining hardware-test script that does this; the only `hw:`
occurrences are this historical warning and `pxtools.py`'s own documentation.

## Live mixer-write protocol investigation (2026-10-15)

**Symptom** (reproducible, v0.1.11): codec/mixer controls (PCM output
enable, Direct monitor, Mono, Capture Source) take effect in the driver's
shadow state and the USB codec write succeeds immediately, but the
*hardware's* audible/recorded behavior does not change while a stream is
active (`px->running`). The new state only becomes effective at the next
stream start, where `codec_resync` (default on since v0.1.11) re-sends it
and the device then adopts it correctly. Controls changed before a stream
starts work correctly and reproducibly - this is specific to a *live*
change while streaming.

**Ruled out**: not an ALSA control bug, not a stale shadow value, not
`codec_resync` overwriting a live change (it only runs at stream start,
and re-sends the *current* shadow, which already reflects the live put),
not a failed/deferred USB submission (the control write returns success
synchronously). Confirmed by reading `px_switch_put()`/`px_adc_source_put()`
(`phasex64.c`): both take `px->mutex`, compute the new register value,
and call `px_codec_write()` unconditionally when the value changed -
nothing checks `px->running`, nothing defers, nothing else runs.

### Windows live Direct-toggle sequence (trace evidence, not inferred)

Extracted from the existing `wdm.pcapng` capture (already in the repo's
analysis material, bus 2 device 2, 48 kHz, REAPER/WDM streaming
continuously). The capture contains exactly two live "Direct 1/2" button
clicks while streaming, isolated with no unrelated UI actions in between
(13 control transfers in the whole 280s trace). First toggle in full:

| t | bmRequestType | bRequest | wValue | wIndex | payload | meaning |
|---|---|---|---|---|---|---|
| +0.0 ms | 0x40 | 0x41 (`REQ_CODEC`) | 0x2c05 | 0x0102 | - | CODEC0 write R22=0x005 (the mixer change itself) |
| +1.4 ms | 0x22 | 0x01 (`SET_CUR`) | 0x0100 | 0x0086 | 80bb00 | SET_CUR rate -> EP_CAPTURE, still 48000 (unchanged) |
| +1.7 ms | 0xa2 | 0x81 (`GET_CUR`) | 0x0100 | 0x0086 | 80bb00 | GET_CUR rate <- EP_CAPTURE, verify -> 48000 |
| +2.0 ms | 0xc0 | 0x49 (`REQ_STATUS`) | 0x0000 | 0x0000 | 32 | STATUS read -> 0x32 |
| +2.4 ms | 0x40 | 0x49 (`REQ_STATUS`) | 0x0032 | 0x0000 | - | STATUS write 0x32 (same value, written anyway) |
| +90.7 ms | - | - | - | - | - | ABORT_PIPE 0x86 |
| +91.7 ms | - | - | - | - | - | SYNC_RESET_PIPE_AND_CLEAR_STALL 0x86 |
| +92.6 ms | - | - | - | - | - | ABORT_PIPE 0x83 |
| +92.6 ms | - | - | - | - | - | SYNC_RESET_PIPE_AND_CLEAR_STALL 0x83 |

The second toggle (R22=0x001, 1.18s later) is a byte-identical sequence,
including the ~88-90ms gap before the pipe operations. `ABORT_PIPE`/
`SYNC_RESET_PIPE_AND_CLEAR_STALL` are USBPcap's names for the underlying
`IOCTL_INTERNAL_USB_SUBMIT_URB` pipe-reset request pair, not literal wire
transfers - `pcap_analyze.py` reports them as pseudo-events from the
capture, same as it already did for the stream-start sequence documented
under "Windows USB trace" earlier in this file.

### Linux divergence

Current Linux mixer puts perform only the `REQ_CODEC` write (step 1
above). None of steps 2-9 happen. This matches the symptom exactly: the
register write lands, but whatever the pipe-reset sequence actually does
on the device side never happens, so the device keeps operating on its
prior state until the next full stream-start resync.

### Important interpretation constraint

**The trace proves only that Windows performs this complete sequence
when Direct 1/2 changes while streaming - it does not prove which part
of it is the actual latch/commit operation.** In particular:

- `ABORT_PIPE` is not automatically equivalent to Linux `usb_clear_halt()`.
  Windows first aborts active transfers on the pipe, then resets/clears
  it. `usb_clear_halt()` alone only clears a halt/stall condition via a
  `CLEAR_FEATURE(ENDPOINT_HALT)` control transfer - it does not cancel
  in-flight URBs. A faithful Linux equivalent of `ABORT_PIPE` is closer to
  `usb_kill_urb()` (synchronously cancels and waits for each outstanding
  URB on that endpoint) than to `usb_clear_halt()`.
- Whether `SET_CUR`/`GET_CUR`, the `STATUS` read/write, the pipe
  abort+reset, their exact order, or some combination is the actual
  necessary element is not established by this trace. Any one of the
  ranked hypotheses below could be sufficient on its own, or the whole
  sequence could be required together.

**Ranked hypotheses (not facts)**:
1. Capture endpoint abort/reset/resubmit (0x86) is required to latch the
   codec state - most consistent with "USB pipe state machine needs a
   kick" being a common class of embedded-device behavior.
2. `SET_CUR`/`GET_CUR` causes or participates in the latch (re-asserting
   the already-current rate might itself trigger a device-side resync,
   independent of the pipe reset).
3. `STATUS` read/write acts as a commit/strobe (plausible given this
   exact read-modify-rewrite pattern is also central to the stream-start
   sequence documented earlier in this file).
4. The complete ordering is required (no single element is sufficient
   alone; the device expects the full ritual).
5. The MIDI endpoint (0x83) reset is incidental - possibly just "reset
   every currently-open pipe" bookkeeping on Windows' side, not something
   the capture-side latch actually depends on.

### Current Linux URB ownership/locking (relevant to designing the experiment)

Read, not modified, before any experiment design:

- Capture (`cap_urb[CAP_URBS]`) and feedback (`fb_urb[FB_URBS]`) URBs
  share intf1 and are always submitted/killed together (see the capture
  watchdog's existing recovery path, `phasex64.c` ~line 981-1006): on a
  stall, it calls `usb_kill_urb()` on every capture and feedback URB,
  cycles `intf1` alt 0 -> 1, resets `fb_head`/`fb_count`, and resubmits
  both sets. This is the existing, hardware-tested Linux precedent for
  "abort + reset + resubmit" on this pipe - via an alt-setting cycle, not
  `usb_clear_halt()`.
- The v0.1.10+ default *start* sequence instead uses `usb_clear_halt()`
  on `EP_CAPTURE` (no alt-cycle) - a second, different existing precedent
  for "the reset step," also hardware-validated, but for a cold start,
  not a live mid-stream change. Which (if either) is the right analog for
  a live-update latch is exactly what the experiment needs to determine.
- `px_cap_complete()` (the capture URB completion handler) runs in
  interrupt/softirq context under `s->lock` (a spinlock), completely
  separate from `px->mutex` (process context, used by mixer puts,
  `px_start_streaming()`/`px_stop_streaming()`, and the watchdog's
  recovery work). Mixer puts already run in process context under
  `px->mutex`, the same context `px_start_streaming()` uses - so calling
  blocking operations (`usb_kill_urb()`, `usb_clear_halt()`, a
  `SET_CUR`/`GET_CUR`/`STATUS` round-trip) directly from a mixer put is
  not blocked by any existing locking constraint.
- EP 0x83 (MIDI in) is a *separate* URB (`midi_in_urb`) with its own
  lifecycle, independent of the capture/feedback arrays: submitted in
  `px_midi_in_open()`, killed in `px_midi_in_close()`/disconnect, gated
  by `px->midi_in_opened`, and guarded by a different spinlock
  (`px->midi_lock`) for the `midi_in`/`midi_out` substream pointers. A
  faithful replication of Windows' 0x83 reset would need to touch this
  URB specifically, conditional on MIDI actually being open - a
  different ownership domain than the capture-side reset, not something
  that falls out of the capture-path changes for free.

### Next experiment (not built)

Design intent, to be implemented and tested only after this is reviewed:

- Behind a new diagnostic module param, default off (0 = current,
  unaffected behavior) - no change to validated v0.1.11 behavior unless
  explicitly enabled.
- Direct 1/2 only initially (`px_switch_put()`'s path for that one
  switch), not all mixer controls, to keep the first test minimal and
  isolated.
- Reproduce the full Windows ordering as faithfully as practical for the
  first test: `SET_CUR` current rate -> `EP_CAPTURE`, `GET_CUR` verify,
  `STATUS` read, `STATUS` write (same value, unconditional), ~90ms
  settle, then abort+reset+resubmit on `EP_CAPTURE` (and, if MIDI is
  open, the same treatment for `EP_MIDI_IN`) - establish a working
  baseline with the complete sequence before isolating individual
  elements.
- Only after the full-sequence experiment is confirmed to fix the
  symptom on hardware should individual protocol elements be removed one
  at a time, to find the minimal necessary subset (testing hypotheses
  1-5 above against each other).
- No production/default behavior change until hardware evidence exists
  that a specific sequence actually works.

## MIDI loopback diagnosis (2026-10-16)

Symptom: `amidi -l` shows the port (`hw:0,0 "PHASE X64 USB MIDI"`), but a
physical loopback test (5-pin DIN cable, MIDI Out -> MIDI In; `amidi -d`
listening, `amidi -S '90 3C 7F 80 3C 00'` sending) showed no bytes
arriving.

### Instrumented build (`0.1.11.1-midi-dbg1`)

Added `dev_dbg()` at every MIDI-out submit (bytes + `usb_submit_urb()` rc)
and every MIDI-in completion (status, actual_length, first bytes before
filler stripping) - no behavioral change, diagnostic-only. `dyndbg`
scoped to just `px_midi_send`/`px_midi_out_complete`/`px_midi_in_complete`
to avoid the module's other (unrelated) `dev_dbg` noise.

**Run A - no PCM stream, loopback cable connected:**

```
[14083.192106] usb 3-2: midi out: submit 3 bytes 903c7ffdfdfdfdfde0 rc=0
[14083.192137] usb 3-2: midi out: complete status=0 actual_length=9
[14083.193160] usb 3-2: midi out: submit 3 bytes 803c00fdfdfdfdfde0 rc=0
[14083.193172] usb 3-2: midi out: complete status=0 actual_length=9
[14083.194184] usb 3-2: midi out: nothing to transmit (n=0)
[14087.190507] usb 3-2: midi in: complete status=-2 actual_length=0 first=
```

OUT transfers succeed at the USB level (`status=0`) in both directions,
but the MIDI-in bulk URB never completes with real data for the whole
5s window - the only completion is the forced `-ENOENT` cancel when
`amidi -d` timed out and closed the port.

**Run B - `arecord -D hw:PhaseX64 -f S32_LE -r 48000 -c 6` running:**

```
[14089.232179] usb 3-2: midi in: complete status=0 actual_length=9 first=0000fdfdfdfdfdfd00
[14089.232214] usb 3-2: midi in: complete status=0 actual_length=9 first=0000fdfdfdfdfdfd00
[14090.233567] usb 3-2: midi out: submit 3 bytes 903c7ffdfdfdfdfde0 rc=0
[14090.233581] usb 3-2: midi out: complete status=0 actual_length=9
[14090.234589] usb 3-2: midi out: submit 3 bytes 803c00fdfdfdfdfde0 rc=0
[14090.234600] usb 3-2: midi out: complete status=-2 actual_length=9
[14090.235261] usb 3-2: midi in: complete status=0 actual_length=9 first=903cfdfdfdfdfdfd00
[14090.236260] usb 3-2: midi in: complete status=0 actual_length=9 first=7f803cfdfdfdfdfd00
[14090.237260] usb 3-2: midi in: complete status=0 actual_length=9 first=00fdfdfdfdfdfdfd00
[14095.237447] usb 3-2: midi in: complete status=-2 actual_length=0 first=
```

Stripping 0xFD filler from the last three IN frames and concatenating:
`90 3C` + `7F 80 3C` + `00` = `90 3C 7F 80 3C 00` - exactly the six bytes
sent via `amidi -S`. The loopback cable genuinely completes the circuit;
MIDI works correctly once a PCM stream is running.

The OUT-side "Note Off" completion shows `status=-2` (`-ENOENT`) despite
`actual_length=9`: `amidi -S` closes the port immediately after queuing
both bytes, racing `px_midi_out_close()`'s `usb_kill_urb()` against the
1ms pacing timer. Benign - the echoed data confirms the byte still
reached the wire - not investigated further.

### Conclusion 1: MIDI requires an active PCM stream

Matches the Windows decomp (`FUN_f1001c00`/`FUN_f1002370`, gating on the
`+0x1ca8` flag - see `analysis/NOTES.md` "MIDI") - not a Linux-vs-Windows
gap, a real hardware/firmware constraint, now confirmed on this unit by
direct measurement rather than inferred from decomp alone. Decision: keep
as documented behavior for now, not a driver defect. A "light arm" that
reproduces only part of the stream-start sequence (candidate: the
`STATUS_STREAM` status-byte write alone) to wake the MIDI engine without
a full PCM stream is on the backlog as a separate experiment - not
attempted here.

### Conclusion 2: byte 8 of every IN frame leaks into the data stream

Every IN frame above has `00` in byte 8 (even the two idle frames before
anything was sent), and the pre-fix `px_midi_in_complete()` only stripped
literal `0xFD` - so byte 8 was never excluded, and it was forwarded to
`snd_rawmidi_receive()` as if it were real data on every single frame.
Reconstructing exactly what the pre-fix code forwarded for Run B (not
what was sent): `90 3C 00 7F 80 3C 00 00 00` - extra zero bytes inserted
after `90 3C` and three more at the end, none of which were sent.

Checked against the decomp: `FUN_f1007990` strips `0xFD` across the
*whole* `actual_length` it's given, with no stride/position logic and no
byte-8 exclusion - i.e. **Windows does not treat byte 8 specially
either**, so if the real firmware's byte 8 is consistently non-`0xFD`
idle padding, Windows carries the identical leak. The fix below (ignore
byte 8 entirely, treat the IN buffer as a stream of 9-byte frames) is
therefore a Linux-only improvement past Windows parity, not a protocol
port - justified directly by this capture, not by the decomp.

### Fix (`fix/midi`, `0.1.11.2`)

`px_midi_in_complete()` now walks `actual_length` in `MIDI_PACKET` (9)
byte strides instead of filtering the whole buffer in one pass. Per
frame: strip `0xFD` from bytes 0..7 only, log byte 8 via `dev_dbg()` but
never forward it, and - new - drop any data byte (bit 7 clear) seen
before the first status byte since the MIDI-in substream was last armed
(`px_midi_in_trigger(sub, 1)`, which now also resets the
"seen a status byte yet" flag). A data byte with no preceding status is
meaningless on the wire; it's exactly what both idle frames in Run B
contained. Hand-verified against the Run B capture above: the two idle
frames now filter to zero bytes each (dropped), and the three data
frames filter to `90 3C`, `7F 80 3C`, `00` - concatenated, exactly the
original `90 3C 7F 80 3C 00`, with every stray zero gone.

No change to the OUT side - Run A/B already showed OUT transfers
succeeding and round-tripping correctly; the corruption was IN-side only.

Build: `W=1`, zero warnings. `MODULE_VERSION` `"0.1.11.2"`.

### Not done here (backlog)

- "Light arm" without a full PCM stream (see Conclusion 1).
- Multiple MIDI-in read URBs in flight (Windows keeps 5 queued via
  `FUN_f1002f00` x5 at arm; Linux keeps 1, relying on immediate
  resubmission from the completion handler). Not needed to explain
  either symptom above and not changed in this fix.

## MIDI loopback diagnosis, round 2 (2026-10-16): the 0.1.11.2 fix had a real bug, and a second, separate symptom surfaced

Re-tested `0.1.11.2` with the same Run C (Note On/Off) and a new Run D
(SysEx `F0 7D 01 02 03 04 05 06 07 08 F7`, chosen to span multiple
9-byte OUT packets). Two different problems showed up, not one.

**Run C, `0.1.11.2`:**

```
[...] midi in: complete status=0 actual_length=9 first=903cfdfdfdfdfdfd00
[...] midi in: complete status=0 actual_length=9 first=7f803cfdfdfdfdfd00
[...] midi in: complete status=0 actual_length=9 first=00fdfdfdfdfdfdfd00
```

`amidi -d` only read `90 3C` and `80 3C` (4 bytes) - `7F` and the final
`00` were dropped, even though the byte-8 fix from round 1 was otherwise
working (no stray zeros). The dropped bytes are each the *first* data
byte of their packet that isn't itself a status byte; `90` (frame 1's
first byte, a real status byte) survived. That pattern only happens if
`midi_in_seen_status` is being reset to false *before each packet*, not
once per open - and indeed, `0.1.11.2`'s reset lived in
`px_midi_in_trigger(sub, 1)`. The rawmidi core calls `.trigger(up=1)`
far more often than once per open - confirmed by adding a `dev_dbg()` to
`px_midi_in_trigger()` in `0.1.11.3` for the next capture - which broke
the running-status carry-over this filter depends on.

**Run D, `0.1.11.2`:**

```
[...] midi out: submit 3 bytes f07d01fdfdfdfdfde0 rc=0
[...] midi out: complete status=0 actual_length=9
[...] midi out: submit 3 bytes 020304fdfdfdfdfde0 rc=0
[...] midi out: complete status=0 actual_length=9
[...] midi out: submit 3 bytes 050607fdfdfdfdfde0 rc=0
[...] midi out: complete status=0 actual_length=9
[...] midi in: complete status=0 actual_length=9 first=010203fdfdfdfdfd00
[...] midi out: submit 2 bytes 08f7fdfdfdfdfdfde0 rc=0
[...] midi out: complete status=-2 actual_length=9
[...] midi in: complete status=0 actual_length=9 first=040506fdfdfdfdfd00
[...] midi in: complete status=0 actual_length=9 first=0708f7fdfdfdfdfd00
```

`amidi -d` only read `F7` (1 byte) - but note the **first IN completion's
raw `first=` dump is already `01 02 03 ...`, not `F0 7D 01 ...`.** `F0`
and `7D` never appear in any IN frame at all, before any software
filtering runs. That's bytes lost at the USB/hardware level, upstream of
`px_midi_in_complete()` entirely - a genuinely different phenomenon from
the Run C bug, and the junk-drop filter (seeing `01 02 03 04 05 06 07 08`
with no status byte ever preceding them, since the real status byte `F0`
simply isn't there) correctly, but unhelpfully, drops all of it until
`F7` (which has bit 7 set, so it's accepted as if it were a fresh status
byte). Fixing the Run C reset-timing bug does **not** fix this - it's not
a software filtering bug. Candidate explanation, not confirmed: Run D
started after a ~7s idle gap since Run C's last activity (`14834.386` to
`14841.567`), so the device/UART receiver may need a brief settle period
after idle during which the first couple of bytes of a new burst are
lost. Left as an open item - not fixed, not root-caused further here.

### Fix (`0.1.11.3`)

Moved the `midi_in_seen_status` reset from `px_midi_in_trigger(sub, 1)`
to `px_midi_in_open()` - the one place guaranteed to run exactly once
per logical arm. `px_midi_in_trigger()` now only sets `px->midi_in`
(gating whether a frame's filtered bytes get forwarded at all) and logs
`up` via `dev_dbg()` so the next capture can directly confirm how often
the core actually calls it. No change to the filtering algorithm itself
from `0.1.11.2` (byte-8 exclusion, FD-strip, drop-until-status) - only
to *when* its running-status memory gets cleared.

The filter logic (`px_midi_in_filter_frame()`, extracted to
`linux/snd-usb-phasex64/midi_in_filter.h` - kept free of kernel headers
specifically so it's unit-testable) was pulled out and exercised in
`tests/unit/test_midi_in_filter.c`: idle junk at start, a note split
across 3 packets (the literal Run C bytes), a SysEx split across 4
packets, running status across a frame boundary, junk-then-real-data
within one frame, and a data-only frame with status already seen (the
case the Run D hardware loss resembles in isolation - confirming the
*filter* has no problem with it; the bytes in Run D never reached the
filter at all). All 6 cases pass:

```
$ gcc -Wall -Wextra -std=c99 -o /tmp/test_midi_in_filter tests/unit/test_midi_in_filter.c && /tmp/test_midi_in_filter
ok   idle junk at start, nothing forwarded (0 bytes)
ok   note on/off split across 3 packets (Run C) (6 bytes)
ok   sysex split across 4 packets (11 bytes)
ok   running status across a frame boundary (5 bytes)
ok   junk then real message in one frame (3 bytes)
ok   data-only frame with status already seen (2 bytes)

all tests passed
```

These tests cover the pure filter function only, with the caller
(the test) managing `seen_status` correctly - they do not exercise
`px_midi_in_trigger()`/`px_midi_in_open()` themselves, which need the
full rawmidi/URB stack. The round-1 bug lived in *when* the integration
code reset the state, not in the filter function, so this suite
wouldn't by itself have caught that regression - worth keeping in mind
before trusting unit-test coverage alone on the integration side.

Build: `W=1`, zero warnings. `MODULE_VERSION` `"0.1.11.3"`.

### Still open after this round

- Run D's missing `F0 7D` (hardware/USB-level byte loss after an idle
  gap) - not explained, not fixed.
- The "light arm without a full PCM stream" idea from round 1 - still
  backlogged.
- Multiple MIDI-in read URBs in flight (Windows keeps 5) - still not
  needed to explain anything seen so far, still not changed.

## MIDI loopback diagnosis, round 3 (2026-10-16): leading-byte loss confirmed as real, upstream of the driver; 5 IN URBs added; stress test written

`0.1.11.3` re-tested: Note On/Off (Run E) now round-trips exactly every
time, confirming the round-2 reset-timing fix. `px_midi_in_trigger()`'s
new `dev_dbg()` also settled the open question from round 2 directly -
the rawmidi core calls `.trigger(up=1)` 3 times per delivered frame in
this testing, nowhere near "once per open":

```
[...] midi in: complete status=0 actual_length=9 first=903cfdfdfdfdfdfd00
[...] midi in: packet control byte=0x00
[...] midi in: trigger up=1
[...] midi in: trigger up=1
[...] midi in: trigger up=1
```

SysEx (Run F1/F2, same 11-byte message, back to back) was sporadic: F1
lost the first two bytes again (`first=010203...`, no `F0`/`7D` in any
IN frame), F2 round-tripped the exact same message perfectly
(`first=f07dfdfdfdfdfdfd00` as the very first IN frame). A third SysEx
with different content (Run G, `F0 43 10 4C 00 00 7E 00 F7`) also
round-tripped perfectly. So the loss isn't message-content-specific and
isn't the "idle gap" theory from round 1 either - F1 and F2 were sent
back to back with no idle gap between them, and only one of the two
lost bytes.

**Isolating OUT vs IN for Run F1** (the question asked going into this
round): the OUT submit for `F0 7D 01` completed clean -
`midi out: submit 3 bytes f07d01fdfdfdfdfde0 rc=0` immediately followed
by `midi out: complete status=0 actual_length=9` - the full 9-byte USB
packet (which carries `F0 7D 01` in its first 3 bytes) was acknowledged
by the device at the USB level. The very next IN completion's raw dump
is already `first=010203fdfdfdfdfd00` - `F0` and `7D` are not in it, or
in either of the two IN frames after it. So the loss happens strictly
*after* the device has already ACKed the USB OUT transfer and *before*
those bytes are echoed back through the physical loop - i.e. inside the
device's own TX-to-wire and/or wire-to-RX path, not in anything either
driver's USB layer does. Not something host-side instrumentation can see
further into, and not fixed here - left open, now with much better
evidence (OUT definitely succeeded at the USB level; IN definitely never
saw those two bytes at all) than round 1's idle-gap guess, which this
round's back-to-back F1/F2 pair rules out.

### 5 MIDI-in read URBs (`0.1.11.4`)

Implemented to match the Windows driver's own concurrency (decomp
`FUN_f1002f00` x5 at arm, see `analysis/NOTES.md` "MIDI") rather than
Linux's single always-resubmitted URB, on the chance that having only
one read outstanding at a time was itself contributing to the loss
(a gap between one completion being processed and its resubmission
reaching the device could miss a byte that arrives in that window).
`midi_in_urb` is now `struct urb *midi_in_urb[MIDI_IN_URBS]` (`= 5`),
folded into the existing `px_free_urbs()`/`px_alloc_urbs()` array-loop
pattern already used for `play_urb`/`fb_urb`/`cap_urb`. `px_midi_in_open()`
submits all 5 (rolling back and returning the error if any submission
fails partway through), `px_midi_in_close()`/`px_disconnect()`/
`px_suspend()` kill all 5, `px_resume()` resubmits all 5 if the port was
open. `px_midi_in_complete()` itself needed no change - it already
operates on whichever `urb` it was called with and resubmits only that
one, which is exactly "each resubmits itself"; the shared
`seen_status`/`px->midi_lock` were already shared state regardless of
which URB completed. This has **not yet been tested on hardware** - that's
what the stress test below is for.

Build: `W=1`, zero warnings. `MODULE_VERSION` `"0.1.11.4"`.

### Stress test (`tests/hw/midi_stress.py`)

Sends the Note On/Off message and the SysEx message N times each
(default 50) over the real loopback cable with a PCM stream running
(started and settled before the first send, same as every manual test
above), opening a fresh `amidi -r` receiver per iteration (raw binary
capture, not the human-formatted `-d` output, for an exact byte
comparison) and comparing the received bytes to what was sent. Reports
exact-match count and loss rate per message type, writes full
per-iteration detail (sent/received hex, match bool) to
`tests/hw/data/midi_stress_results.json`, exits nonzero if any iteration
didn't match exactly. Not run as part of this change - intended to be
run once against `0.1.11.3` as a baseline and once against `0.1.11.4`,
to get an actual loss-rate number instead of three manual samples (one
lost, two clean) for comparing the two.

## MIDI loopback diagnosis - result (2026-10-16)

`tests/hw/midi_stress.py` run against `0.1.11.4` (5 MIDI-in URBs): Note
On/Off 50/50 exact matches, SysEx 50/50 exact matches, 0.0% loss both
message types. The 5-URB change (round 3) measurably fixed the sporadic
leading-byte loss that round 3's manual captures showed about 1/3 of the
time with the single-URB version. `0.1.11.3`'s loss rate wasn't captured
numerically (round 3 only had 3 manual samples, 1 of 3 lost bytes) -
`midi_stress.py` exists precisely so a future regression gets a real
number instead of "seemed sporadic" again.

Diagnostic dyndbg cleanup: dropped the `px_midi_send()` early-bailout
logs (busy/no-substream/disconnected, empty FIFO), the per-packet
"control byte" log (its value is already visible in the main `complete
... first=` dump), and `px_midi_in_trigger()`'s call-frequency log (its
one question - how often does `.trigger()` actually fire - is answered
and recorded above; keeping it logging every call forever isn't useful
going forward). Kept: `px_midi_send()`'s submit log (bytes + rc),
`px_midi_out_complete()`'s status log, `px_midi_in_complete()`'s main
completion log (status/actual_length/first bytes) and its "not a
multiple of 9" defensive warning - these have ongoing diagnostic value
for any future MIDI issue, not just this investigation.

Summary of where this leaves the driver, for anyone who hasn't read
rounds 1-3 above: MIDI works, but only while a PCM stream is running
(same constraint as the Windows driver - see `analysis/NOTES.md`
"MIDI"); the byte-8/running-status IN parsing and the 5 outstanding
read URBs are both implemented and hardware-confirmed; one separate,
rare byte-loss mechanism (traced to between the device ACKing a USB OUT
transfer and the bytes echoing back on IN) remains unexplained and
un-fixed, not reproduced since the 5-URB change but not confirmed gone
either.

## Round-trip latency, raw ALSA (2026-10-24)

First run of `tests/hw/px_latency.py` (`alsa` subcommand, commit
`5ac51b1`), run by hand. Setup: driver 0.9.1, S/PDIF coax cable
unplugged (`Digital Input Lock` = off, live read before the run), `Clock
Source` = Internal, `In 3/4 Capture Source` = `Loopback 1/2`, `Direct 1/2`
off, `PCM 1/2` on, PipeWire stopped (sockets included). Each run opens
playback + capture on `hw:PhaseX64`, links them (`snd_pcm_link`), starts
both in one trigger and plays 16 single-sample impulses (-6 dBFS) on Out
1/2; the offset is where an impulse lands in the capture minus where it
was in the playback stream, i.e. the delay the driver's pointer does not
account for. Channels: ch 5 = bit-exact digital tap of Out 1/2 (USB and
firmware pipeline only), ch 3 = Out 1/2's analog output via `Loopback
1/2` (adds DAC + ADC). Every valid run passed the idle rate readback
(`Digital Input Rate` = requested rate) and had a per-impulse spread of 0
(all 16 impulses at the same offset - no drift or dropout inside a run).

**Measured** (samples; `tap` = ch 5, `analog` = ch 3):

| rate | buffer | run | tap | analog | analog - tap | driver-reported extra delay |
|---|---|---|---|---|---|---|
| 48000 | 64x2 | 1 | - | - | - | capture xrun, run invalid |
| 48000 | 64x2 | 2 | 218 | 270.95 | 52.95 | 0 / 0 |
| 48000 | 64x2 | 3 | 216 | 268.95 | 52.95 | 0 / 0 |
| 48000 | 64x3 | 1 | 226 | 278.95 | 52.95 | 0 / 0 |
| 48000 | 64x3 | 2 | 220 | 272.94 | 52.94 | 0 / 0 |
| 48000 | 64x3 | 3 | 226 | 278.94 | 52.94 | 0 / 0 |
| 48000 | 128x2 | 1 | 234 | 286.94 | 52.94 | 0 / 0 |
| 48000 | 128x2 | 2 | 214 | 266.93 | 52.93 | 0 / 0 |
| 48000 | 128x2 | 3 | 228 | 280.93 | 52.93 | 0 / 0 |
| 48000 | 256x2 | 1 | 224 | 276.92 | 52.92 | 0 / 0 |
| 48000 | 256x2 | 2 | 222 | 274.92 | 52.92 | 0 / 0 |
| 48000 | 256x2 | 3 | 210 | 262.91 | 52.91 | 0 / 0 |
| 48000 | 512x2 | 1 | 214 | 266.91 | 52.91 | 0 / 0 |
| 48000 | 512x2 | 2 | 208 | 260.90 | 52.90 | 0 / 0 |
| 48000 | 512x2 | 3 | 214 | 266.90 | 52.90 | 0 / 0 |
| 44100 | 64x2 | 1, 2 | - | - | - | playback xrun, runs invalid |
| 44100 | 64x2 | 3 | 188 | 240.87 | 52.87 | 0 / 0 |
| 44100 | 256x2 | 1 | 224 | 276.86 | 52.86 | 0 / 0 |
| 44100 | 256x2 | 2 | 210 | 262.85 | 52.85 | 0 / 0 |
| 44100 | 256x2 | 3 | 181 | 233.84 | 52.84 | 0 / 0 |

Summary: 48 kHz (14 valid runs) tap 208-234, mean 219.6 (4.33-4.88 ms,
mean 4.57 ms), analog - tap 52.90-52.95; 44.1 kHz (4 valid runs) tap
181-224, mean 200.8 (4.10-5.08 ms, mean 4.55 ms), analog - tap
52.84-52.87. The driver reports 0 extra delay in both directions
(`runtime->delay` is never set, see `analysis/NOTES.md`).

**Split into two parts.**

- *Sample-based, ~52.9 samples (measured):* analog - tap is the same in
  samples at 44.1 and 48 kHz (52.86 vs 52.93), and constant to 0.05
  samples across every buffer size and run. It is therefore set by the
  converters' clocked filters, not by USB timing. *Inference:* the WM8776
  datasheet gives typical digital-filter group delays of 22 (ADC) + 19
  (DAC) = 41 samples (Table 17); the other ~12 samples are plausibly the
  sigma-delta modulators, analog filtering and the I2S framing around the
  codecs, but this measurement cannot attribute them.
- *Time-based, ~4.55 ms (measured):* the tap offset is the same in time at
  both rates (mean 4.57 vs 4.55 ms), not in samples (219.6 vs 200.8).
  *Inference:* this is the USB pipeline - the driver keeps `PLAY_URBS` = 4
  playback URBs of 1 ms in flight and advances its pointer when frames are
  copied into an URB, not when they are played (3-4 ms), plus 32-frame
  capture bulk URBs and whatever FIFO the device firmware keeps. Which
  share of the 4.55 ms belongs to playback and which to capture is not
  measurable with a round trip.
- Neither part depends on the ALSA buffer size (64x2 ... 512x2): it is
  latency below the driver's ALSA pointer, invisible to every client.

**Run-to-run skew (measured):** the tap offset varies per stream start -
standard deviation 7.4 samples at 48 kHz (range 26 samples = 0.54 ms),
19.8 at 44.1 kHz (range 43 samples = 0.97 ms, 4 runs only) - while being
exactly constant within a run. *Inference:* the phase between the
playback URB schedule and the capture bulk stream is set anew at each
transport start. Under PipeWire with `session.suspend-timeout-seconds = 0`
the device starts once per session, so the skew is fixed for a session
but differs between sessions; a fixed latency compensation can therefore
only be right to about +-13 samples at 48 kHz.

**xrun verdict: the measurement script, not the driver (inference, with
these measured facts behind it).** The kernel log for the whole run
window has no line from the driver or the USB core - only the 8 plug-in
lines from before the runs (no rate warning, no watchdog, no URB error).
That alone does not clear the driver, since an ALSA xrun is detected by
the ALSA core and the driver logs nothing for it. The xruns occurred only
at the smallest buffer, 64x2 = 128 frames (2.67 ms at 48 kHz, 2.90 ms at
44.1 kHz); all 15 runs at 64x3 or larger were clean. The two 44.1 kHz
underruns happened in the first 0.3 s (the run's 0.3 s status line never
printed); the 48 kHz capture overrun happened later. The `5ac51b1` loop
refilled playback with exactly as many frames as it had just read from
capture, and capture data arrives later than playback is consumed (32-frame
bulk URBs plus the device's own lag), so the playback fill sank to the
buffer size minus that lag minus the read granularity minus the 1 ms
consumption step - almost nothing at 128 frames. The capture overrun is
consistent with a host-side pause of more than 2.67 ms (Python garbage
collection, or the one status print plus `/proc` read the loop did at the
0.3 s mark). The loop was rewritten afterwards (`11913ca`): playback is
topped up to the full buffer on every pass, independent of capture; the
loop polls at 1 ms, reads straight into a preallocated buffer, and prints
and garbage-collects nothing until the run is over. That loop passes the
selftest against a simulated device with the driver's 1 ms / 32-frame
shape at 64x2, but a simulator has no real-time behaviour - **the 64x2
runs still need one hardware re-run to confirm.**

Side observation (measured, not investigated): the analog channel's peak
level fell steadily over the session, -7.4 dBFS in the first valid run
to -8.2 dBFS in the last 48 kHz run and -8.5 to -8.7 dBFS at 44.1 kHz,
while the digital tap stayed at -6.0 dBFS. The analog - tap delay did not
change with it (52.95 -> 52.90). Small analog drift (e.g. warm-up) or the
44.1 kHz filters; irrelevant to the latency result.

**Through PipeWire / REAPER (inference, written before the REAPER
measurement - confirmed by "REAPER through pipewire-jack" below).** By the PipeWire
1.6.10 source, an ALSA node's port advertises 1 quantum
(`spa/plugins/alsa/alsa-pcm-sink.c:910-913`, `alsa-pcm-source.c:856-859`)
plus its headroom as `min_rate`/`max_rate` (`alsa-pcm.c:2111-2156`; for a
BATCH device under timer scheduling the headroom gains one ALSA period,
:2131-2132, and capture gets at least 32 frames, :2136-2137), and
pipewire-jack reports exactly `quantum x nframes + rate + ns` to JACK
clients (`pipewire-jack/src/pipewire-jack.c:6733-6771`). Both terms are
real buffering PipeWire itself keeps in the ALSA ring. Nothing in that
path knows about the ~4.55 ms + 52.9 samples measured here, and the ALSA
plugin never calls `snd_pcm_delay()`. So by the source, REAPER on
pipewire-jack should be ~270 samples late at 48 kHz, at every quantum -
not the ~54 samples measured by hand earlier, which matches analog - tap
alone. That points to the hand measurement having had the digital
pipeline as its reference, or to REAPER compensating something beyond
the JACK port latencies. `tests/hw/reaper-latency/` records In 3 and In 5
in the same take to settle it: in5's error isolates the USB part, in3 -
in5 the converter part.

## REAPER through pipewire-jack (2026-10-25)

Run by hand with `tests/hw/reaper-latency/` (project template + README),
REAPER 7.81 on JACK (pipewire-jack), PipeWire 1.6.10, driver 0.9.1, 48
kHz, PipeWire left running with `clock.force-quantum` set to 64, 256 and
1024 in turn. Mixer as in the raw-ALSA run (`In 3/4 Capture Source` =
`Loopback 1/2`, `Direct 1/2` off, `PCM 1/2` on, `Digital Input Lock` off),
REAPER's *Use audio driver reported latency* on, manual offsets 0. One
take of the 16-impulse stimulus per quantum: track `stim` (stimulus,
hardware out 1/2), `in3` (REAPER input 3 = `capture_AUX2`, the analog
loopback) and `in5` (input 5 = `capture_AUX4`, the digital tap). The ALSA
period of the PHASE X64 followed the quantum (64 / 256 / 1024; the buffer was 192
frames at quantum 64; read from `/proc/asound`). `px_latency.py rpp`
reads the remaining error from the saved project; every take had all 16
impulses at one constant offset.

**Measured** (samples; what is left over after REAPER's compensation -
positive = the recording sits late):

| quantum | REAPER's reported latency (status line) | in5 (digital, USB only) | in3 (analog) | in3 - in5 |
|---|---|---|---|---|
| 64 | 64 in / 64 out (~1.3 ms) | 255.00 | 307.76 | 52.76 |
| 256 | 256 / 256 (~5.3 ms) | 217.00 | 269.75 | 52.75 |
| 1024 | 1024 / 1024 (~21 ms) | 236.00 | 288.73 | 52.73 |

- The reported latency is exactly one quantum each way, as the PipeWire
  source says (`alsa-pcm.c:2111-2156`, `pipewire-jack.c:6733-6771`; see
  above). REAPER also showed `64spls ~1.3/2.0ms` while its outputs were
  still auto-connected to another card (the default sink): the output
  figure follows the connected device.
- **The error does not grow with the quantum.** in5 217-255, a spread of
  38 samples (0.8 ms) with no trend; the raw-ALSA runs at 48 kHz had
  208-234 (14 runs, mean 219.6). in3 - in5 is 52.7-52.8 at every
  quantum, equal to the raw-ALSA converter part (52.9). So REAPER on
  pipewire-jack compensates what PipeWire reports (one quantum each way)
  and nothing of the ~4.5 ms USB part nor the ~53 samples of the codecs.
- The 54 samples of the earlier hand measurement therefore equal the
  converter part alone (inference: that measurement had a reference that
  already contained the USB delay; not checked).
- Not explained: the quantum-64 take is 255, 21 above the highest raw-ALSA
  run (234). One take only; the per-start skew (sd 7.4 samples, 14 runs)
  makes 255 unlikely but not impossible. Not tested whether it repeats.

**REAPER puts its compensation inside the recorded file** (measured, q64):
the same take with *Use audio driver reported latency* off lies at 354
(first eight impulses) / 360 (last eight) for in5 and 407 / 413 for in3,
against 255 / 307.76 with it on. The saved project has `POSITION 0` and
`SOFFS 0` in both cases and the file length equals the item length, so the
shift is not stored as an item offset. The difference, ~99 samples, is
close to the 128 samples REAPER reports (64 in + 64 out); the remaining
~29 is within the per-start skew (inference; one take each). The
uncompensated take has a +6-sample step between its 8th and 9th impulse
(~2.3-2.5 s), identical on in5 and in3 - so in the capture/playback chain
and not in the converters. Seen once, not investigated; it did not occur
in the compensated q64, q256 and q1024 takes.

**Procedure findings** (for whoever repeats this):
- REAPER's `out1`..`out4` auto-connect to the *default sink*; here that
  was another USB interface, so the first thing to check is
  `pw-link -l`. A recording with silent in3/in5 means the stimulus played
  elsewhere. The inputs were right without changes.
- Changing a *Preferences > Audio* setting restarts REAPER's JACK client
  and with it the connections: one take was lost that way (silent, kept
  out of the results). Opening another project does not.
- REAPER reopens the last project; the path in its status line tells
  which one is open.
- The kernel log had no line from the driver or the USB core for any of
  the runs.

**Consequence for the PipeWire node properties (inference):** what
`latency.internal.*` would have to add is the remaining error: ~4.6-4.9
ms (the raw-ALSA mean 219.6 samples = 4.57 ms; the three REAPER takes mean
236 samples = 4.92 ms) in total over playback and capture, plus ~52.8
samples for the converters. The split between playback and capture cannot
be measured with a round trip. A fixed value is only right to about +-13
samples because of the per-start skew.

Still open: how the driver reports delay to ALSA (it reports none, so raw
ALSA clients have the whole ~270 samples unreported), a repeat of the
quantum-64 take, 44.1 kHz through REAPER, and the +6-sample step.
