# Peak meter controls — design (not implemented)

The panel (`linux/phasex64-panel/phasex64-panel`) already has a complete UI
and backend for stereo sample-peak meters: `PEAK_NAMES` (`"In 1/2 Peak"`,
`"In 3/4 Peak"`, `"Out 1/2 Peak"`, `"Out 3/4 Peak"`), `Mixer.peaks()`/
`parse_peak()`/`peak_db()`, the `PeakMeter` widget, and `PanelIO`'s dedicated
50ms "peaks" poll that's paused whenever the window isn't visible (see
`tests/hw/REPORT.md`-adjacent panel tests, `test_hidden_and_obscured_windows_do_not_poll_peaks`).
None of these controls exist in the driver yet — `Mixer.contents()` never
requests them, `self.available` never contains them on real hardware, and the
panel correctly falls back to "needs driver ≥ 0.1.12" (`PeakMeter.draw()`).
This document is the driver-side design to close that gap. **Nothing here is
implemented** — no module change accompanies this document.

## Controls to add

Four new read-only ALSA controls, one `snd_kcontrol_new` each (or one
`.get`/`.info` pair parameterized over a 4-entry table — implementor's
choice, not specified here):

| Name | Source channels |
|---|---|
| `In 1/2 Peak` | capture ch 0, 1 |
| `In 3/4 Peak` | capture ch 2, 3 |
| `Out 1/2 Peak` | playback ch 0, 1 |
| `Out 3/4 Peak` | playback ch 2, 3 |

Each is a 2-element `SNDRV_CTL_ELEM_TYPE_INTEGER` array (L, R), range
`0..PEAK_MAX` (`0x7fffff`, matching the panel's `PEAK_MAX` constant and
`parse_peak()`'s range check), `.access = SNDRV_CTL_ELEM_ACCESS_READ |
SNDRV_CTL_ELEM_ACCESS_VOLATILE`, no `.put`. This mirrors the existing
`px_rate_measured_ctl`/`px_lock_ctl` pattern (`phasex64.c:1989-2057`) exactly
— those are the closest existing precedent for a read-only, live-value
control, just with `uinfo->count = 2` instead of `1` and no `px->mutex`/live
USB round-trip involved (peaks are driver-local state, not a device
round-trip).

Capture channels 4/5 (digital in) get no peak control — `PEAK_NAMES` only
covers the four analog pairs, matching the existing ADC mux design where
5/6 has no source-selection control either (`analysis/NOTES.md`, "Hardware
I/O").

## Reset-on-read semantics

The panel already assumes this exact semantic — see
`Mixer.contents()`'s comment ("Never use `contents`: it would consume
reset-on-read peak controls during slow status refreshes") and
`_read_names()`'s ("discovery/status never reads or resets any peak
element"). `.get` must therefore read-and-clear: return the max magnitude
seen since the *previous* `.get` call on that control, then reset the
tracked max back to 0. This makes the panel's 50ms peak-poll interval
(`PanelIO`, `next_peak = now + .05`) the meter's integration window — each
read shows "loudest sample in roughly the last 50ms," a standard peak-meter
behavior. Only the panel is expected to poll these controls in practice
(`amixer -c PhaseX64 cget name='In 1/2 Peak'` would also reset them, same
as any other reader — document this in the control's help text / the
README's control table, not silently).

## Where to compute each pair

**Capture (`In 1/2`/`In 3/4`)**: `px_decode_frame()` (`phasex64.c:698`)
already produces `s32 samples[CAP_CHANNELS]` per frame (8 frames per
512-byte bulk packet), called from `px_cap_complete()`'s per-frame loop
(`phasex64.c:~784`). Samples are already sign-extended 24-bit values shifted
left by 8 (see the capture-decode comment) — `abs(samples[ch]) >> 8` gives
the 24-bit magnitude to compare against a running per-channel max.
Accumulate the per-URB max for channels 0-3 in **local variables, no
locking**, across the frame loop (the existing loop already touches all 8
frames unlocked before taking `s->lock` once at the end for the ALSA period
bookkeeping — same shape applies here), then merge into the shared peak
state with a single locked compare-and-update after the frame loop, not per
frame/sample. Reuse `px->cap.lock` for this merge (already taken once per
`px_cap_complete()` call for the existing elapsed/pointer bookkeeping) rather
than adding a new spinlock, to avoid one more lock acquisition per
completion.

**Playback (`Out 1/2`/`Out 3/4`)**: the function at `phasex64.c:460`
(`px_play_copy()`, called from `px_fill_play_urb()`) `memcpy()`s raw
S24_3LE frames straight from `rt->dma_area` into the URB buffer —
currently no per-sample touch at all. Peak tracking here means walking the
copied region's S24LE triplets for channels 0-3 (4-channel interleave,
`PLAY_FRAME_BYTES` = 12 bytes/frame) immediately after each `memcpy()`
chunk, same local-max-then-single-locked-merge shape as capture, under
`px->play.lock` (already held for the whole `px_play_copy()` call via
`px_fill_play_urb()`). On silence (`s->active` false, the `silence:` label
at line 490), peaks should read 0 — matches the panel's `peak_db(0) == -60`
floor.

**"PCM peaks only" is a hard constraint, not a simplification to revisit
later**: Direct and DSP (Sound Module) routing happen entirely inside the
WM8776 codecs / device firmware, mixed in the analog or digital domain
*after* the point where PCM samples leave the driver — the driver has no
visibility into that signal at all, on any OS. The panel's existing copy
("PCM peaks only · excludes Direct/DSP", `build_plain()`) is already
correct and must stay; there is no driver-side change that could make
`Out 1/2 Peak` reflect Direct/DSP-routed audio, short of the hardware
exposing a post-mix tap it doesn't have.

## Idle / not-running behavior

While `px->running` is false (no stream), or while a direction has no
active substream (`px->play.active`/`px->cap`'s equivalent), both channels
of the relevant control(s) should read `0, 0` rather than stale data from a
previous session — reset the tracked max (not just read-and-clear on
`.get`) at the same points `px_start_streaming()`/`px_stop_streaming()`
already reset other per-stream state, so a fresh stream doesn't inherit a
leftover peak from before it started (same principle already applied to
RIAA filter state on stream start, `analysis/v0.1.11-plan.md` section 1).

## Locking summary

No new spinlocks needed — reuse `px->cap.lock` and `px->play.lock`, each
already taken once per relevant completion/fill call. The expensive part
(scanning every sample for a max) stays lock-free; only the final
compare-and-merge-into-shared-state is locked, and that's a handful of
integer compares, not a loop over the whole buffer again.

## Version

The panel's `PeakMeter.draw()` already hardcodes `"needs driver ≥ 0.1.12"`
— target `v0.1.12` for this feature to match, rather than picking a
different version number that would leave that string wrong either way.
