// SPDX-License-Identifier: GPL-2.0-only
/*
 * ALSA driver for the TerraTec / TerraSoniq PHASE X64 USB (Ploytec based)
 *
 * USB 0a4a:5b01, high speed only. The device is vendor specific:
 *
 *  intf 0 alt 1: EP 0x02 isoc OUT  playback, S24_3LE, 4 channels
 *                EP 0x83 bulk IN   MIDI in (0xfd = filler)
 *                EP 0x04 bulk OUT  MIDI out (9 byte packets)
 *  intf 1 alt 1: EP 0x81 isoc IN   playback feedback, byte 0 = frames per ms
 *                EP 0x86 bulk IN   capture, Ploytec bit-serial format, 6 channels
 *
 *  vendor request 0x49 reads/writes a status byte, vendor request 0x41 writes
 *  a register of one of the two WM8776 codecs, the sample rate is set with a
 *  UAC1 style SET_CUR(SAMPLING_FREQ) to the endpoints 0x86 and 0x02.
 *
 * The mixer controls mirror the buttons of the Windows control panel, which
 * only manipulates WM8776 registers R7, R21 (ADC mux) and R22 (output mux).
 *
 * Copyright (C) 2026 flarkflarkflark
 */

#include <linux/module.h>
#include <linux/bitops.h>
#include <linux/usb.h>
#include <linux/slab.h>
#include <linux/delay.h>
#include <linux/hrtimer.h>
#include <linux/jiffies.h>
#include <linux/wait.h>
#include <linux/atomic.h>
#include <linux/workqueue.h>
#include <linux/version.h>
#include <linux/math64.h>
#include <linux/sched.h>
#include <sound/core.h>
#include <sound/pcm.h>
#include <sound/pcm_params.h>
#include <sound/control.h>
#include <sound/rawmidi.h>
#include <sound/initval.h>

#define PX_VID			0x0a4a
#define PX_PID			0x5b01

#define EP_PLAY			0x02
#define EP_FEEDBACK		0x81
#define EP_CAPTURE		0x86
#define EP_MIDI_IN		0x83
#define EP_MIDI_OUT		0x04

#define REQ_CODEC		0x41
#define REQ_STATUS		0x49
#define UAC_SET_CUR		0x01
#define UAC_SAMPLING_FREQ	0x0100

#define STATUS_INTERNAL_CLOCK	0x02
#define STATUS_DIGITAL_LOCK	0x04
#define STATUS_STREAM		0x10
#define STATUS_SMALL_BUFFER	0x20

#define PLAY_CHANNELS		4
#define PLAY_FRAME_BYTES	(PLAY_CHANNELS * 3)
#define PLAY_URBS		4
#define PLAY_PACKETS		8	/* one millisecond per URB */
#define PLAY_MAX_PACKET		156

#define FB_URBS			4
#define FB_PACKET		64
#define FB_FIFO			16

#define CAP_CHANNELS		6
#define CAP_FRAME_BYTES		(CAP_CHANNELS * 4)
#define CAP_WIRE_FRAME		64	/* bytes per frame on the wire */
#define CAP_URBS		8
#define CAP_URB_BYTES_DEFAULT	2048

#define RIAA_COEF_SHIFT		30
#define RIAA_STATE_SHIFT	8
#define S24_MIN			(-(1 << 23))
#define S24_MAX			((1 << 23) - 1)
#define RIAA_STATE_MIN		((s32)(S24_MIN * (1 << RIAA_STATE_SHIFT)))
#define RIAA_STATE_MAX		((s32)(S24_MAX * (1 << RIAA_STATE_SHIFT)))

#define MIDI_PACKET		9
#define MIDI_BYTES_PER_MS	3
#define MIDI_IN_BYTES		512
#define MIDI_FILLER		0xfd
#define MIDI_CONTROL		0xe0
#define MIDI_IN_URBS		5	/* Windows keeps 5 queued (FUN_f1002f00 x5 at arm) */

#include "midi_in_filter.h"
#if MIDI_PACKET != PX_MIDI_PACKET || MIDI_FILLER != PX_MIDI_FILLER
#error "midi_in_filter.h's PX_MIDI_PACKET/PX_MIDI_FILLER must match MIDI_PACKET/MIDI_FILLER above"
#endif

#define CODEC_REGS		24
#define WM_R7			7
#define WM_ADC_MUX		21
#define WM_OUT_MUX		22
#define WM_ADC_MUTE		0xc0
#define WM_R7_STEREO		0x90
#define WM_R7_MONO_BITS		0x60
#define ADC_MIC			0x01
#define ADC_PHONO		0x02
#define ADC_AUX			0x04
#define ADC_SOUND_MODULE	0x08
#define ADC_LOOPBACK		0x10
#define OUT_MONO		0x02
#define OUT_DIRECT		0x04

#define CTRL_TIMEOUT		1000
#define CTRL_RETRY_DELAY_MS	20
#define CTRL_EPIPE_RETRIES	3
#define CTRL_ETIMEDOUT_RETRIES	1
#define PX_SETTLE_MS		50
#define CAP_WD_CHECK_MS		50	/* watchdog tick interval */
#define CAP_WD_STALL_MS		150	/* escalate once a stall exceeds this */
#define CAP_WD_MAX_ATTEMPTS	3
#define RATE_SETTLE_MS		200	/* delay after SET_CUR on a rate change,
					 * before the alt-cycle + URB submission
					 */
#define CLEAR_HALT_DELAY_MS	90	/* delay after the status write before
					 * usb_clear_halt() on EP_CAPTURE, matching
					 * the Windows driver's
					 * ABORT_PIPE+SYNC_RESET_PIPE_AND_CLEAR_STALL
					 * timing (~90-175ms) - see tests/hw/REPORT.md
					 * "Windows USB trace"
					 */
#define LIVE_APPLY_RETRY_MS	10	/* retry delay when the live-apply
					 * worker loses the px->mutex trylock -
					 * see px_live_apply_work()
					 */
#define LIVE_APPLY_DEBOUNCE_MS	30	/* addendum (2026-10-17, hardware hang
					 * review): delay used instead of 0 when
					 * scheduling live_apply_work from an
					 * accepted write or a mid-restart
					 * requeue. schedule_delayed_work()'s own
					 * "already pending, no-op" coalescing
					 * only protects a burst that arrives
					 * while a previous schedule is still
					 * *queued* (delay not yet elapsed) - at
					 * delay=0 that window is however long it
					 * takes the workqueue to dequeue and
					 * start running, often well under 1ms,
					 * far shorter than realistic back-to-back
					 * user actions. A short, fixed delay
					 * here costs nothing user-visible (it is
					 * well inside this feature's whole point
					 * of applying "fast enough to not notice"
					 * - sub-100ms) and makes coalescing
					 * actually effective for rapid changes
					 * instead of only for ones that happen
					 * to land inside a sub-millisecond
					 * window.
					 */
#define LIVE_APPLY_MAX_RETRIES	2	/* addendum (2026-10-16, v0.1.12-plan.md
					 * section 11): retries of a failing
					 * px_live_apply_restart() before
					 * falling back - see
					 * px_live_apply_work()
					 */
#define SYNC_STOP_TIMEOUT_MS	1000	/* addendum (2026-10-17, hardware hang
					 * fix): bound on px_pcm_sync_stop()'s
					 * wait_event_timeout(), see that
					 * function's comment. Generous relative
					 * to a live-apply sequence's own worst
					 * case (~1-2s) despite sync_stop now
					 * cancelling that work first - this is
					 * a backstop for whatever this timeout
					 * doesn't anticipate, not the primary
					 * defense.
					 */

static int index[SNDRV_CARDS] = SNDRV_DEFAULT_IDX;
static char *id[SNDRV_CARDS] = SNDRV_DEFAULT_STR;
static bool enable[SNDRV_CARDS] = SNDRV_DEFAULT_ENABLE_PNP;

module_param_array(index, int, NULL, 0444);
MODULE_PARM_DESC(index, "Index value for the PHASE X64 USB.");
module_param_array(id, charp, NULL, 0444);
MODULE_PARM_DESC(id, "ID string for the PHASE X64 USB.");
module_param_array(enable, bool, NULL, 0444);
MODULE_PARM_DESC(enable, "Enable the PHASE X64 USB.");

static unsigned int cap_urb_bytes = CAP_URB_BYTES_DEFAULT;
module_param(cap_urb_bytes, uint, 0444);
MODULE_PARM_DESC(cap_urb_bytes, "capture bulk URB size in bytes, must be a "
	"multiple of 64 (the wire frame size); clamped to "
	"[64, 8192] and to a multiple of 64 if invalid, default 2048");

static bool riaa = true;
module_param(riaa, bool, 0444);
MODULE_PARM_DESC(riaa, "enable Phono RIAA capture equalization; default 1");

static bool codec_resync = true;
module_param(codec_resync, bool, 0444);
MODULE_PARM_DESC(codec_resync, "re-send cached codec mixer/source state before "
		 "each stream start; default 1");

/*
 * v0.1.10 default start sequence matches the official Windows driver
 * (see tests/hw/REPORT.md "Windows USB trace" / "Confirmed without the
 * S/PDIF cable"): SET_CUR 0x86/0x02/0x86, GET_CUR verify, unconditional
 * status write, a CLEAR_HALT_DELAY_MS settle, usb_clear_halt() on
 * EP_CAPTURE, no alt-cycle. The older v0.1.9 alt-cycle-based start (kept
 * for fallback) cycles intf1 alt 0->1 instead of clear_halt; the capture
 * watchdog's own alt-cycle recovery action is separate from this and
 * always runs regardless of this setting.
 */
static int start_alt_cycle;
module_param(start_alt_cycle, int, 0444);
MODULE_PARM_DESC(start_alt_cycle, "use the older v0.1.9 start sequence "
	"(cycle intf1 alt 0->1, no clear_halt) instead of the v0.1.10 "
	"default (clear_halt on EP_CAPTURE, no alt-cycle); default 0 = "
	"v0.1.10 default");

/*
 * v0.1.12 live codec apply (analysis/v0.1.12-plan.md, phase 3a only - no
 * timeline bridge, see px_live_apply_work()): settle time before the
 * codec register replay during a live (while-streaming) full restart.
 * 170ms is the design doc's conservative candidate, the only value
 * validated with the mode-4 diagnostic so far (section 8) - no sweep has
 * been run to find a shorter production default. Runtime-adjustable
 * (0644, unlike every other param above) specifically so that sweep can
 * happen without a reload, per the design doc's own test plan.
 */
static unsigned int live_settle_ms = 170;
module_param(live_settle_ms, uint, 0644);
MODULE_PARM_DESC(live_settle_ms, "settle time (ms) before the codec replay "
	"in a live control-change restart while streaming; clamped to "
	"[0, 500]; default 170 (UNVALIDATED - see analysis/v0.1.12-plan.md "
	"section 8, no sweep has been run)");

/*
 * Addendum (2026-10-16, v0.1.12-plan.md section 11): debug/test aid, not a
 * production knob. Forces one named step of px_live_apply_restart() to fail
 * deterministically, without touching the device, so the retry/fallback/XRUN
 * paths below can be exercised on demand instead of waiting for a real
 * transient failure. Step 5 (URB resubmit) is also checked inside
 * px_live_apply_fallback()'s own resubmit loop, so a single value drives the
 * "fallback also fails" -> XRUN scenario end to end; steps 1-4 only touch
 * the codec-replaying main restart, so the codec-free fallback is unaffected
 * by them and succeeds instead, exercising the "fallback restores playback"
 * scenario. 0 = disabled (default).
 */
static unsigned int live_fail_step;
module_param(live_fail_step, uint, 0644);
MODULE_PARM_DESC(live_fail_step, "debug/test: force step N of "
	"px_live_apply_restart() to fail without touching the device; "
	"1=status read, 2=codec write, 3=SET_CUR rate, 4=status write, "
	"5=URB resubmit (also checked in the fallback path); 0 = disabled "
	"(default)");

struct px_stream {
	struct snd_pcm_substream *sub;
	bool prepared;
	bool active;
	unsigned int hwptr;
	unsigned int period_pos;
	spinlock_t lock;	/* protects active, sub against completion handlers */
	atomic_t in_elapsed;	/* completions currently inside/about to call
				 * snd_pcm_period_elapsed() for this stream
				 */
	wait_queue_head_t elapsed_wq;
	/*
	 * Addendum (2026-10-17, review round 5): lifetime inc/dec totals for
	 * in_elapsed - cheap, never reset, so (inc_total - dec_total) is
	 * always re-derivable as a cross-check against the live in_elapsed
	 * value in any trace line, and a growing gap between them across
	 * multiple trace lines pinpoints which event leaked an increment
	 * without its matching decrement, the way the review round 5 bug
	 * (px_fill_play_urb()'s return value discarded at three resubmit
	 * call sites) could only be found by reading code, not from a single
	 * snapshot. Logged in sync_stop's diag, and in every "staged"/
	 * "committed" live-apply line (struct px_dev, below).
	 */
	atomic_t in_elapsed_inc_total;
	atomic_t in_elapsed_dec_total;
};

struct px_riaa_coeff {
	unsigned int rate;
	s32 b0, b1, b2, a1, a2;
};

struct px_riaa_state {
	s32 x1, x2;		/* signed S24 */
	s32 y1, y2;		/* saturated S24.Q8 */
};

struct px_dev {
	struct usb_device *udev;
	struct usb_interface *intf0;
	struct usb_interface *intf1;
	struct snd_card *card;
	struct snd_pcm *pcm;
	bool disconnected;

	struct mutex mutex;	/* streaming state, codec shadow, status */
	spinlock_t lock;	/* pcm positions, feedback fifo */

	unsigned int rate;
	bool running;
	unsigned long stop_jiffies;	/* last px_stop_streaming(), for settle delay */
	struct px_stream play;
	struct px_stream cap;

	/*
	 * Capture watchdog: armed on capture trigger(START), cancelled (via
	 * cancel_delayed_work_sync() in px_stop_streaming()) on a normal
	 * stop. Reschedules itself unconditionally every CAP_WD_CHECK_MS as
	 * long as it's armed, measuring elapsed time since
	 * cap_last_complete_jiffies (updated only by genuine
	 * status==0 && actual_length>0 completions - see px_cap_complete())
	 * rather than a monotonic completion counter, so a stall starting
	 * any time after a healthy start is still caught. On a stall
	 * (no real data for >= CAP_WD_STALL_MS): kill and resubmit
	 * capture+feedback URBs around an intf1 alt 0 -> alt 1 cycle, up to
	 * CAP_WD_MAX_ATTEMPTS times for the current stall episode, then
	 * goes quiet until data resumes on its own (which resets the
	 * attempt counter). last_cap_urb_* and last_status_byte are cached
	 * outside the watchdog so it never has to guess at stale data - see
	 * px_cap_watchdog_fn().
	 */
	struct delayed_work cap_watchdog_work;
	atomic_t cap_complete_count;
	/*
	 * cap_wd_gen is incremented (atomic_inc) by trigger(START) only -
	 * it never writes cap_wd_attempt/cap_wd_gave_up directly, which
	 * used to race against the watchdog's own state transitions when a
	 * stream stopped and a new one started while an old stall episode
	 * was still mid-recovery (see tests/hw/REPORT.md). cap_wd_stage_gen,
	 * cap_wd_attempt and cap_wd_gave_up are all owned exclusively by
	 * the watchdog, under px->mutex: at entry, if cap_wd_stage_gen
	 * doesn't match a fresh atomic_read() of cap_wd_gen, the stored
	 * state belongs to a superseded stream (or this is simply the
	 * first-ever firing for the current one) and is reset fresh for
	 * the current generation - this only resets stale state, it never
	 * skips acting on (or rescheduling for) the current stream.
	 */
	atomic_t cap_wd_gen;
	unsigned int cap_wd_stage_gen;
	unsigned long cap_last_complete_jiffies;
	int cap_wd_attempt;
	bool cap_wd_gave_up;
	/*
	 * TEMPORARY per-second capture summary (v0.1.10-pre1, see
	 * tests/hw/REPORT.md - the capture throughput shortfall
	 * investigation): dev_dbg once a second while capture is running,
	 * real measured completions/wire-bytes/frames-to-ALSA, not derived
	 * from nominal rate x real time. Protected by px->cap.lock (already
	 * taken by px_cap_complete() for the decode section).
	 */
	unsigned long cap_sum_jiffies;
	unsigned int cap_sum_completions;
	unsigned long cap_sum_bytes;
	unsigned int cap_sum_frames;
	int last_cap_urb_status;
	unsigned int last_cap_urb_actual_length;
	u8 last_status_byte;

	/* Digital Input Lock cache: live px_status_read() reliably fails
	 * while a stream is active (control-pipe contention with active
	 * bulk/isoc transfers - see tests/hw/REPORT.md, "Finding B").
	 * px_lock_get() serves this instead of a live read while px->running,
	 * and otherwise does its own live read + cache update + notify
	 * lazily, on demand - there is no proactive refresh at stream stop
	 * (that used to risk a control transfer right after URBs were just
	 * killed, a moment already documented elsewhere as prone to
	 * -EPIPE/-ETIMEDOUT).
	 */
	bool last_digital_lock;
	struct snd_kcontrol *lock_kctl;	/* for snd_ctl_notify() when
					 * px_lock_get() finds a change
					 */

	u8 fb_fifo[FB_FIFO];
	unsigned int fb_head;
	unsigned int fb_count;
	unsigned int rate_acc;

	struct urb *play_urb[PLAY_URBS];
	struct urb *fb_urb[FB_URBS];
	struct urb *cap_urb[CAP_URBS];

	u16 codec[2][CODEC_REGS];
	bool clock_internal;
	bool riaa_switch[2];
	bool riaa_active[2];
	const struct px_riaa_coeff *riaa_coeff;
	struct px_riaa_state riaa_state[4];
	/* "In 1/2 Peak"/"In 3/4 Peak"/"Out 1/2 Peak"/"Out 3/4 Peak" - max
	 * |sample| since the last read (reset-on-read, see px_peak_get()).
	 * peak_cap[] is post-RIAA (px_cap_update_peaks() runs after
	 * px_riaa_process_frame()) and protected by cap.lock; peak_play[]
	 * is protected by play.lock. See analysis/peak-controls-design.md.
	 */
	s32 peak_cap[4];
	s32 peak_play[PLAY_CHANNELS];
	struct snd_kcontrol *mono_12_kctl;	/* for snd_ctl_notify() when the
						 * mono-direct interlock clears
						 * it from another control's put()
						 */

	/*
	 * v0.1.12 live codec apply, phase 3a only (analysis/v0.1.12-plan.md
	 * sections 3-5; no section 6 timeline bridge - a live toggle while
	 * streaming causes an audible ~300ms gap/jump, not a seamlessly
	 * bridged one). Hardware-tested since v0.1.12, see
	 * analysis/v0.1.12-plan.md section 17.
	 *
	 * desired_codec is the full 2x24 register snapshot controls should
	 * eventually reach - NOT the same thing as `codec[][]` above, which
	 * is "last known applied to hardware". px_codec_write() keeps them
	 * equal on every direct write (idle-path puts, init, the worker's
	 * own replay); only a live-path put() running concurrently with an
	 * in-flight restart can make them differ, and only until the next
	 * worker pass. desired_lock protects desired_codec/desired_gen;
	 * never nested with px->mutex or any stream lock (put() takes it
	 * alone, briefly, with no sleep - see px_switch_put()/
	 * px_adc_source_put()).
	 */
	spinlock_t desired_lock;
	u16 desired_codec[2][CODEC_REGS];
	unsigned int desired_gen;	/* bumped on every accepted live-path put() */
	unsigned int live_applied_gen;	/* last desired_gen committed to hardware */
	struct delayed_work live_apply_work;
	/*
	 * Addendum (2026-10-17, lock-ordering review): true exactly when
	 * capture+feedback+playback URBs are believed submitted and alive.
	 * Before v0.1.12 this was always implied by px->running (only
	 * px_start_streaming()/px_stop_streaming() ever touched URBs, 1:1
	 * with running); the live-apply path broke that correspondence by
	 * adding a third place URBs get killed/resubmitted without touching
	 * running. px_start_streaming()'s own entry guard now checks this
	 * too, not running alone - see that function. Protected by
	 * desired_lock, not px->mutex: px_pcm_sync_stop()'s force-teardown
	 * path needs to clear it without the lock-ordering question a
	 * px->mutex acquisition from that function would otherwise raise
	 * (moot after cancel_delayed_work_sync() returns there, but
	 * desired_lock avoids needing to reason about that at all - the
	 * same reason desired_gen/live_applied_gen already use it instead
	 * of px->mutex for a live put()).
	 */
	bool urbs_live;
	/*
	 * Addendum (2026-10-17, review round 3): refcount, not a bool -
	 * px_pcm_sync_stop()/px_stop_streaming() each inc/dec this around
	 * their own cancel_delayed_work_sync(&live_apply_work) call, so
	 * overlapping calls (e.g. capture and playback substreams not
	 * explicitly linked) still leave it nonzero until every one of them
	 * has finished, rather than one clearing it while another is still
	 * relying on it. px_live_apply_restart()/fallback() check this
	 * before every sleep, control transfer and retry and abort with
	 * -ESHUTDOWN as soon as it's nonzero, bounding cancel's wait to
	 * whichever single operation was already in flight (see
	 * px_ctrl_retry_stoppable() and analysis/v0.1.12-plan.md section 12
	 * "Review round 3" for the worst-case latency this gives).
	 */
	atomic_t live_apply_stopping;
	/*
	 * Addendum (2026-10-17, review round 3): set only by
	 * px_live_apply_give_up(), when both the restart and the codec-free
	 * fallback have failed and no audio URBs are outstanding (both
	 * failure paths kill them first). px_pcm_pointer() reports
	 * SNDRV_PCM_POS_XRUN while this is set, which is the ALSA-core-
	 * sanctioned way for a .pointer op to signal XRUN without the driver
	 * calling back into the PCM core itself (snd_pcm_update_hw_ptr0()
	 * detects the sentinel and does the rest) - replaces the removed
	 * px_live_apply_xrun()'s direct snd_pcm_stop_xrun() call, which was a
	 * lock-ordering risk (see px_live_apply_give_up()'s own comment), not
	 * a weaker version of it. Cleared only by px_start_streaming() on a
	 * fresh successful start - "the next prepare/start" is the one
	 * recovery path out of this state. px_cap_watchdog_fn() also checks
	 * this, so it doesn't resubmit capture/feedback URBs behind this
	 * state's back. Protected by desired_lock, same reasoning as
	 * urbs_live above.
	 */
	bool give_up;
	/*
	 * Addendum (2026-10-17, v0.1.12 phase 3b, timeline fill): fills the
	 * ~340ms gap between a live-apply restart's px_kill_audio_urbs() and
	 * its resubmit with fabricated frames at the nominal rate (capture:
	 * silence; playback: discard, just advance position) so a REAPER
	 * recording's position/length stays continuous across a toggle
	 * instead of losing ~0.3s per toggle. HRTIMER_MODE_REL_SOFT: the
	 * callback (px_fill_timer_fn()) runs in softirq context, same class
	 * of context the real URB completion handlers already run in - it
	 * may take cap.lock/play.lock (one at a time, never nested) but must
	 * never do anything that can sleep (no mutex, no msleep, no
	 * GFP_KERNEL allocation). fill_active is checked by the callback
	 * before re-arming (hrtimer_forward_now() + HRTIMER_RESTART) and is
	 * cleared by every stopper before calling hrtimer_cancel() - see
	 * px_fill_start()/px_fill_stop()'s own comments for the full
	 * start/stop/handover contract.
	 */
	struct hrtimer fill_timer;
	atomic_t fill_active;
	/*
	 * Addendum (2026-10-18, phase 3b diagnostic pass): per-restart
	 * counters for px_fill_timer_fn()'s own accounting, reset in
	 * px_fill_start() and logged in px_fill_stop(). fill_overrun_extra
	 * is the sum, across every tick, of (overruns - 1) whenever
	 * hrtimer_forward_now() reports more than one period elapsed since
	 * the last tick - the softirq fired late, and without this, exactly
	 * that many periods' worth of frames would never get fabricated.
	 * Cheap (a handful of plain adds per tick, already under no lock),
	 * kept permanently rather than stripped after the diagnostic pass:
	 * a nonzero value here is itself useful signal for any future
	 * timing investigation on this path.
	 */
	unsigned int fill_ticks;
	unsigned int fill_overrun_extra;
	unsigned int fill_cap_frames;
	unsigned int fill_play_frames;
	ktime_t fill_start_ns;
	/*
	 * Addendum (2026-10-19, review round 4): frames of the timer's own
	 * *playback* advance to withhold up front, set to the upcoming
	 * restart's known playback prefill size (PLAY_URBS * nominal
	 * ms-frames - see px_live_apply_restart()'s own resubmit loop)
	 * at px_fill_start(), decremented by each tick's frame count until
	 * it reaches 0 - only then does px_fill_timer_fn() actually call
	 * px_fill_playback() for that (and later) ticks. Capture is
	 * unaffected and fills normally from the first tick - it has no
	 * prefill step to reserve room for. This is what makes the
	 * handover catch-up in px_play_complete() safe to apply as a
	 * plain forward-only addition: by reserving the prefill's own size
	 * up front, the timer's playback total ends up *short* of
	 * wall-clock by roughly the prefill's size instead of matching it,
	 * so there is nothing left to subtract backward once the prefill
	 * and catch-up both add their own contributions. Never moves
	 * play.hwptr backward anywhere - unlike an earlier (reverted)
	 * attempt at this same fix, which rewound hwptr directly and
	 * corrupted the ALSA core's own position tracking whenever that
	 * crossed a buffer wrap (see analysis/v0.1.12-plan.md section 14
	 * "review round 4" for what that looked like on hardware).
	 */
	unsigned int play_fill_reserve;
	/*
	 * Addendum (2026-10-18, phase 3b diagnostic pass): one-shot gap
	 * measurement from px_kill_audio_urbs() (in px_live_apply_restart())
	 * to the first real completion after the matching resubmit -
	 * diag_*_pending is set right before resubmitting and cleared (with
	 * a dev_dbg of the gap) by whichever of px_cap_complete()/
	 * px_play_complete() sees it set first. Not reset per-tick like the
	 * counters above - this is a single measurement per restart, not a
	 * running total.
	 */
	ktime_t restart_kill_ns;
	/*
	 * Addendum (2026-10-19, review): the playback prefill
	 * (px_fill_play_urb(), called on each of PLAY_URBS at resubmit to
	 * populate their initial content) advances play.hwptr/period_pos by
	 * its own frame count - exactly PLAY_URBS * (nominal ms frames),
	 * confirmed on hardware at exactly 192 frames (4.0ms at 48kHz) on
	 * every single restart, zero variance - on top of whatever the fill
	 * timer and its handover catch-up already advanced it by. That is a
	 * real double-count: the catch-up's own "expected vs fabricated"
	 * calculation (px_fill_catchup_frames()) had no way to know about
	 * this, since it runs later (at the first real completion) and only
	 * knew about fill_play_frames (the timer's own contribution). Set
	 * here, in px_live_apply_restart()/fallback(), right after computing
	 * it; consumed (subtracted) in px_play_complete()'s own catch-up
	 * call, never used for capture (capture URBs are submitted empty -
	 * no equivalent prefill advance exists on that side).
	 */
	unsigned int play_prefill_frames;
	/*
	 * Addendum (2026-10-19, review item 2): px_live_apply_restart()'s
	 * PLAY_URBS resubmit loop can fail partway through (a real
	 * usb_submit_urb() failure, not reachable via the existing
	 * live_fail_step=5 fault injection, which fails before this loop
	 * ever starts) - the URBs submitted before the failing one already
	 * had px_fill_play_urb() advance play.hwptr/period_pos for them, a
	 * phantom advance never actually played (the fail: path kills them
	 * again before they transmit). Set to that partial amount (if any)
	 * by px_live_apply_restart()'s own fail: path; consumed - subtracted
	 * from the fresh reserve, then cleared - by the very next
	 * px_fill_start() call, which is always the next retry attempt's own
	 * (never a fallback()/give_up() consequence: those end the sequence
	 * rather than retry, so there is deliberately no carry logic on
	 * fallback()'s own fail: path - see its comment).
	 */
	unsigned int play_fill_carry;
	bool diag_cap_pending;
	bool diag_play_pending;

	struct snd_rawmidi_substream *midi_in;
	struct snd_rawmidi_substream *midi_out;
	struct urb *midi_in_urb[MIDI_IN_URBS];
	struct urb *midi_out_urb;
	spinlock_t midi_lock;
	bool midi_in_opened;
	int midi_in_seen_status;	/* 0/1, type matches px_midi_in_filter_frame()'s int* */
	bool midi_out_busy;
	struct hrtimer midi_timer;
};

/* ---------------------------------------------------------------------- */
/* control requests */

static int px_status_read(struct px_dev *px, u8 *status)
{
	int err = usb_control_msg_recv(px->udev, 0, REQ_STATUS,
				    USB_DIR_IN | USB_TYPE_VENDOR | USB_RECIP_DEVICE,
				    0, 0, status, 1, CTRL_TIMEOUT, GFP_KERNEL);
	if (err < 0)
		dev_dbg_ratelimited(&px->udev->dev,
			"status read failed: bRequest=0x%02x wValue=0x%04x wIndex=0x%04x err=%d\n",
			REQ_STATUS, 0, 0, err);
	return err;
}

static int px_status_write(struct px_dev *px, u8 status)
{
	int err = usb_control_msg_send(px->udev, 0, REQ_STATUS,
				    USB_DIR_OUT | USB_TYPE_VENDOR | USB_RECIP_DEVICE,
				    status, 0, NULL, 0, CTRL_TIMEOUT, GFP_KERNEL);
	if (err < 0)
		dev_err_ratelimited(&px->udev->dev,
			"status write failed: bRequest=0x%02x wValue=0x%04x wIndex=0x%04x err=%d\n",
			REQ_STATUS, status, 0, err);
	else
		px->last_status_byte = status;
	return err;
}

/*
 * read-modify-write the status byte, the device needs 40 ms after a change.
 *
 * While px->running, skip the live read: it reliably fails (-EPIPE) while
 * audio is actively streaming - control-pipe contention with active bulk
 * (capture) + isochronous (playback/feedback) transfers, confirmed via
 * tests/hw/REPORT.md "Finding B" (every IN-direction status read failed
 * 100% of the time under load; every OUT-direction control write, this
 * function's own px_status_write() included, succeeded). px->
 * last_status_byte is already kept current by every successful write,
 * including the one px_start_streaming() does on every stream start, so
 * it's a safe, accurate substitute for the live read in exactly the case
 * that would otherwise fail. Only this function's own read is skipped;
 * the write below still happens as a genuine USB transfer either way.
 */
static int px_status_update(struct px_dev *px, u8 clear, u8 set)
{
	u8 old, new;
	int err;

	if (px->running) {
		old = px->last_status_byte;
	} else {
		err = px_status_read(px, &old);
		if (err < 0)
			return err;
	}
	new = (old & ~clear) | set;
	if (new == old)
		return 0;
	dev_dbg(&px->udev->dev, "status update: old=0x%02x new=0x%02x clear=0x%02x set=0x%02x\n",
		old, new, clear, set);
	err = px_status_write(px, new);
	if (err < 0)
		return err;
	msleep(40);
	return 0;
}

static int px_codec_write(struct px_dev *px, int codec, int reg, u16 val)
{
	unsigned long flags;
	int err;

	err = usb_control_msg_send(px->udev, 0, REQ_CODEC,
				   USB_DIR_OUT | USB_TYPE_VENDOR | USB_RECIP_DEVICE,
				   (reg << 9) | (val & 0x1ff),
				   codec ? 0x0106 : 0x0102,
				   NULL, 0, CTRL_TIMEOUT, GFP_KERNEL);
	if (err < 0)
		return err;
	px->codec[codec][reg] = val & 0x1ff;
	/* Keep desired_codec mirroring the applied shadow on every direct
	 * write (idle-path puts, px_codec_init(), px_codec_resync_shadow(),
	 * and the live-apply worker's own replay) - see the struct px_dev
	 * comment and px_live_apply_work(). desired_lock, not px->mutex:
	 * a live-path put() can be staging a different register under only
	 * that lock while this runs.
	 */
	spin_lock_irqsave(&px->desired_lock, flags);
	px->desired_codec[codec][reg] = val & 0x1ff;
	spin_unlock_irqrestore(&px->desired_lock, flags);
	return 0;
}

static int px_set_rate_ep(struct px_dev *px, u8 ep, unsigned int rate)
{
	u8 data[3] = { rate, rate >> 8, rate >> 16 };
	int err;

	err = usb_control_msg_send(px->udev, 0, UAC_SET_CUR,
				    USB_DIR_OUT | USB_TYPE_CLASS | USB_RECIP_ENDPOINT,
				    UAC_SAMPLING_FREQ, ep, data, sizeof(data),
				    CTRL_TIMEOUT, GFP_KERNEL);
	if (err < 0)
		dev_err_ratelimited(&px->udev->dev,
			"SET_CUR rate failed: bRequest=0x%02x wValue=0x%04x wIndex=0x%02x rate=%u err=%d\n",
			UAC_SET_CUR, UAC_SAMPLING_FREQ, ep, rate, err);
	return err;
}

/*
 * px_get_rate_ep() - GET_CUR(SAMPLING_FREQ) from wIndex @ep, raw bytes and
 * decoded value both returned. wIndex=0 reports the device's actual
 * current rate regardless of source (internal after our own SET_CUR, or
 * the external digital-input rate while locked - see tests/hw/REPORT.md
 * "Windows USB trace" / "Confirmed without the S/PDIF cable"): used both
 * as the post-SET_CUR verify and as the live value behind the "Digital
 * Input Rate" control below.
 */
static int px_get_rate_ep(struct px_dev *px, u8 ep, unsigned int *rate, u8 *raw)
{
	u8 buf[3];
	int err;

	err = usb_control_msg_recv(px->udev, 0, UAC_SET_CUR | 0x80,
				    USB_DIR_IN | USB_TYPE_CLASS | USB_RECIP_ENDPOINT,
				    UAC_SAMPLING_FREQ, ep, buf, sizeof(buf),
				    CTRL_TIMEOUT, GFP_KERNEL);
	if (err < 0)
		return err;
	if (raw)
		memcpy(raw, buf, 3);
	*rate = buf[0] | (buf[1] << 8) | (buf[2] << 16);
	return 0;
}

/* ---------------------------------------------------------------------- */
/* streaming */

static unsigned int px_next_ms_frames(struct px_dev *px)
{
	unsigned int n;

	if (px->fb_count) {
		n = px->fb_fifo[(px->fb_head + FB_FIFO - px->fb_count) % FB_FIFO];
		px->fb_count--;
		return n;
	}
	px->rate_acc += px->rate;
	n = px->rate_acc / 1000;
	px->rate_acc %= 1000;
	return n;
}

/*
 * Addendum (2026-10-17, v0.1.12 phase 3b): the fill timer's own per-tick
 * frame count, used in place of px_next_ms_frames() - deliberately does
 * NOT consume px->fb_fifo/fb_count (the feedback path): there is no real
 * feedback data arriving during the gap (the feedback URBs are dead too),
 * so draining whatever stale entries were already queued would just be
 * using leftover measurements for a time they no longer describe. Shares
 * px->rate_acc with px_next_ms_frames() on purpose - advancing the same
 * accumulator means the fractional carry is exactly where the real
 * per-URB stepping left off once URBs are resubmitted, no jump, no
 * discontinuity at handover.
 */
static unsigned int px_fill_ms_frames(struct px_dev *px)
{
	unsigned int n;

	px->rate_acc += px->rate;
	n = px->rate_acc / 1000;
	px->rate_acc %= 1000;
	return n;
}

/* Track playback sample-peak magnitude for "Out 1/2 Peak"/"Out 3/4 Peak"
 * - "PCM peaks only", by construction: this reads the S24_3LE bytes just
 * written into the URB from the ALSA ring buffer, before the device ever
 * sees them, so it can never reflect Direct/DSP routing (mixed in the
 * codec/firmware, invisible to the driver either way). Caller
 * (px_play_copy()) holds px->play.lock, so a plain compare-and-store is
 * enough. See analysis/peak-controls-design.md.
 */
static void px_play_update_peaks(struct px_dev *px, const u8 *src, unsigned int frames)
{
	s32 local[PLAY_CHANNELS] = { 0 };
	unsigned int i;
	int ch;

	for (i = 0; i < frames; i++) {
		for (ch = 0; ch < PLAY_CHANNELS; ch++) {
			const u8 *p = src + i * PLAY_FRAME_BYTES + ch * 3;
			s32 sample = sign_extend32(p[0] | (p[1] << 8) | (p[2] << 16), 23);
			s32 mag = sample < 0 ? -sample : sample;

			if (mag > S24_MAX)
				mag = S24_MAX;	/* abs(S24_MIN) overflows S24_MAX by 1 */
			if (mag > local[ch])
				local[ch] = mag;
		}
	}
	for (ch = 0; ch < PLAY_CHANNELS; ch++)
		if (local[ch] > px->peak_play[ch])
			px->peak_play[ch] = local[ch];
}

/* copy frames out of the ring buffer, returns true when a period elapsed.
 * Caller holds px->play.lock. A NULL substream with active still true would
 * mean close()/hw_free() raced with this completion; that must not happen
 * now that active and sub are cleared together under the same lock, but the
 * WARN_ON_ONCE catches it as silence instead of a NULL deref if it ever does.
 */
static bool px_play_copy(struct px_dev *px, u8 *dst, unsigned int frames)
{
	struct px_stream *s = &px->play;
	struct snd_pcm_runtime *rt;
	const u8 *peak_src = dst;
	unsigned int total_frames = frames;
	unsigned int n;

	if (!s->active)
		goto silence;
	if (WARN_ON_ONCE(!s->sub))
		goto silence;
	rt = s->sub->runtime;
	if (WARN_ON_ONCE(!rt))
		goto silence;

	s->period_pos += frames;
	while (frames) {
		n = min_t(unsigned int, frames, rt->buffer_size - s->hwptr);
		memcpy(dst, rt->dma_area + s->hwptr * PLAY_FRAME_BYTES,
		       n * PLAY_FRAME_BYTES);
		dst += n * PLAY_FRAME_BYTES;
		frames -= n;
		s->hwptr += n;
		if (s->hwptr >= rt->buffer_size)
			s->hwptr = 0;
	}
	px_play_update_peaks(px, peak_src, total_frames);
	if (s->period_pos >= rt->period_size) {
		s->period_pos %= rt->period_size;
		return true;
	}
	return false;

silence:
	memset(dst, 0, frames * PLAY_FRAME_BYTES);
	return false;
}

/* returns the substream when a period elapsed */
static struct snd_pcm_substream *px_fill_play_urb(struct px_dev *px,
						  struct urb *urb)
{
	struct snd_pcm_substream *elapsed = NULL;
	unsigned int ms_frames, frames, offset = 0;
	unsigned long flags;
	int i;

	spin_lock_irqsave(&px->lock, flags);
	ms_frames = px_next_ms_frames(px);
	spin_unlock_irqrestore(&px->lock, flags);

	for (i = 0; i < PLAY_PACKETS; i++) {
		frames = ms_frames / PLAY_PACKETS +
			 (i < ms_frames % PLAY_PACKETS ? 1 : 0);
		urb->iso_frame_desc[i].offset = offset;
		urb->iso_frame_desc[i].length = frames * PLAY_FRAME_BYTES;
		offset += frames * PLAY_FRAME_BYTES;
	}
	urb->number_of_packets = PLAY_PACKETS;
	urb->transfer_buffer_length = offset;

	spin_lock_irqsave(&px->play.lock, flags);
	if (px_play_copy(px, urb->transfer_buffer, offset / PLAY_FRAME_BYTES)) {
		elapsed = px->play.sub;
		/*
		 * Counted while still holding play.lock, i.e. in the same
		 * critical section that read active/sub: sync_stop() can
		 * then safely wait for this count to hit zero before
		 * letting hw_free()/close() free the runtime out from under
		 * the snd_pcm_period_elapsed() call below.
		 */
		atomic_inc(&px->play.in_elapsed);
		atomic_inc(&px->play.in_elapsed_inc_total);
	}
	spin_unlock_irqrestore(&px->play.lock, flags);
	return elapsed;
}

/*
 * Addendum (2026-10-17, review round 5): factored out of px_play_complete()
 * below - bug found on real hardware with REAPER (small periods, mmap):
 * px_start_streaming()/px_live_apply_restart()/px_live_apply_fallback() all
 * call px_fill_play_urb() to pre-fill each playback URB before its initial
 * post-restart submission, and all three *discarded its return value* -
 * silently dropping the snd_pcm_period_elapsed() call and the matching
 * atomic_dec_and_test()/wake_up() whenever that prefill happened to cross a
 * period boundary. That left play.in_elapsed stuck at 1 with no waiter ever
 * able to bring it back to 0 - exactly the "increment without a decrement"
 * sync_stop's bounded/killable wait (review round 1) then had to catch via
 * its timeout, ~100s later at the next close. Unlike a fresh
 * px_pcm_prepare() (which zeroes period_pos before calling
 * px_start_streaming()), a live-apply restart/fallback never resets
 * period_pos - it carries over from before the kill, so a resubmit's
 * prefill can immediately cross a period boundary if period_pos was
 * already close to period_size when the kill happened - far likelier with
 * small periods (REAPER) than with aplay's typically larger ones, which is
 * why aplay-based testing never reproduced this. Safe to call
 * snd_pcm_period_elapsed() from any of those three: all run in process
 * context (px->mutex held), same guarantee the completion handler's own
 * interrupt-context call already relies on - the function is documented
 * safe from either.
 */
static void px_play_notify_elapsed(struct px_dev *px, struct snd_pcm_substream *elapsed)
{
	if (!elapsed)
		return;
	snd_pcm_period_elapsed(elapsed);
	atomic_inc(&px->play.in_elapsed_dec_total);
	if (atomic_dec_and_test(&px->play.in_elapsed))
		wake_up(&px->play.elapsed_wq);
}

/* Forward declarations - px_fill_capture()/px_fill_playback() are defined
 * further down (near the rest of the fill-timer machinery), but
 * px_cap_complete()/px_play_complete() below need to call them for the
 * handover catch-up correction (review/phase-3b item 2, 2026-10-18 -
 * see px_fill_catchup_frames()'s own comment).
 */
static void px_fill_capture(struct px_dev *px, unsigned int frames);
static void px_fill_playback(struct px_dev *px, unsigned int frames);

/*
 * Addendum (2026-10-18, phase 3b accuracy fix): the gap between
 * px_fill_stop() (called right before the real resubmit, to hand over
 * cleanly - see that function's own comment) and the first real
 * completion actually arriving was previously uncompensated entirely -
 * measured on real hardware at ~1.2-2.0ms per restart, every time,
 * never caught up by anything afterward (confirmed by instrumenting one
 * restart: px->restart_kill_ns to "first real completion" minus the
 * fill timer's own covered duration leaves exactly this gap, to within
 * the fill timer's own ~24us of accounting slop). This function
 * computes the shortfall - expected frames from wall-clock time elapsed
 * since the kill, minus what the fill timer actually fabricated for
 * this stream - so the caller can fabricate exactly that many more
 * frames immediately before processing the first real completion's own
 * data, closing the gap instead of permanently losing it. Returns 0
 * (nothing to do) on any call that isn't the first real completion
 * after a restart, or if the fill timer's own count already met or
 * exceeded wall-clock (possible by a frame or two from rounding - never
 * run backwards).
 */
static unsigned int px_fill_catchup_frames(struct px_dev *px, unsigned int fabricated_frames)
{
	u64 elapsed_ns, expected_frames;

	elapsed_ns = ktime_to_ns(ktime_sub(ktime_get(), px->restart_kill_ns));
	expected_frames = div_u64(elapsed_ns * px->rate, NSEC_PER_SEC);
	if (expected_frames <= fabricated_frames)
		return 0;
	return (unsigned int)(expected_frames - fabricated_frames);
}

static void px_play_complete(struct urb *urb)
{
	struct px_dev *px = urb->context;
	struct snd_pcm_substream *elapsed;

	if (urb->status == -ENOENT || urb->status == -ECONNRESET ||
	    urb->status == -ESHUTDOWN || urb->status == -ENODEV ||
	    !px->running)
		return;

	/* Addendum (2026-10-18, phase 3b accuracy fix): one-shot handover
	 * catch-up, checked before anything else touches playback state for
	 * this completion - see px->diag_play_pending's own comment and
	 * px_fill_catchup_frames()'s. Fabricates the measured shortfall via
	 * the same px_fill_playback() the timer itself uses (advance
	 * hwptr/period_pos, no data copy), so the real data this completion
	 * is about to write starts from the caught-up position, not the
	 * stale one.
	 *
	 * Addendum (2026-10-19, review - superseding two earlier attempts):
	 * fill_play_frames + play_prefill_frames, not fill_play_frames
	 * alone - the upcoming prefill (px_fill_play_urb(), run for each
	 * resubmitted play URB, *before* this completion fires) already
	 * advanced play.hwptr/period_pos by play_prefill_frames, measured on
	 * hardware at exactly PLAY_URBS * nominal-ms-frames (192 frames,
	 * 4.0ms at 48kHz) every single restart - a real double-count against
	 * this catch-up if not included. Two things make this safe to
	 * compute with a plain, clamped-at-zero subtraction here, unlike the
	 * first attempt at this exact fix: (1) px_fill_timer_fn() now
	 * reserves exactly play_prefill_frames of its own advance up front
	 * (px->play_fill_reserve - see that field's own comment), so
	 * fill_play_frames by the time this runs is already *short* of
	 * wall-clock by roughly the prefill's own size, not matching it -
	 * the clamp essentially never fires anymore, verified on hardware
	 * (see analysis/v0.1.12-plan.md section 14 "review round 4"); (2)
	 * even if it did, clamping to zero here is always safe (just means
	 * "nothing to add"), unlike the second attempt's signed version,
	 * which needed to subtract from hwptr directly - and hwptr is
	 * reported straight to the ALSA core via .pointer(), which requires
	 * monotonic, forward-only reporting. Moving it backward (confirmed
	 * on hardware) corrupts the core's own position tracking whenever it
	 * crosses a buffer wrap, surfacing as a phantom echo of an earlier
	 * click roughly one buffer-length late - a real regression, caught
	 * before trusting that approach. This version never moves hwptr
	 * backward anywhere. Capture has no equivalent term: capture URBs
	 * are submitted empty, nothing pre-advances cap.hwptr at resubmit.
	 */
	if (px->diag_play_pending) {
		unsigned int shortfall = px_fill_catchup_frames(px,
			px->fill_play_frames + px->play_prefill_frames);

		px->diag_play_pending = false;
		if (shortfall)
			px_fill_playback(px, shortfall);
		dev_dbg(&px->udev->dev,
			"live apply: first real play completion %lldus after kill, "
			"catchup=%u frames, hwptr=%u\n",
			ktime_to_us(ktime_sub(ktime_get(), px->restart_kill_ns)),
			shortfall, px->play.hwptr);
	}
	elapsed = px_fill_play_urb(px, urb);
	if (usb_submit_urb(urb, GFP_ATOMIC) < 0)
		dev_err_ratelimited(&px->udev->dev, "playback resubmit failed\n");
	px_play_notify_elapsed(px, elapsed);
}

static void px_fb_complete(struct urb *urb)
{
	struct px_dev *px = urb->context;
	unsigned int nominal, v;
	unsigned long flags;

	if (urb->status == -ENOENT || urb->status == -ECONNRESET ||
	    urb->status == -ESHUTDOWN || urb->status == -ENODEV ||
	    !px->running)
		return;

	if (urb->iso_frame_desc[0].status == 0 &&
	    urb->iso_frame_desc[0].actual_length >= 1) {
		v = ((u8 *)urb->transfer_buffer)[0];
		nominal = px->rate / 1000;
		if (v + 2 >= nominal && v <= nominal + 3) {
			spin_lock_irqsave(&px->lock, flags);
			px->fb_fifo[px->fb_head] = v;
			px->fb_head = (px->fb_head + 1) % FB_FIFO;
			if (px->fb_count < FB_FIFO / 2)
				px->fb_count++;
			spin_unlock_irqrestore(&px->lock, flags);
		}
	}

	if (usb_submit_urb(urb, GFP_ATOMIC) < 0)
		dev_err_ratelimited(&px->udev->dev, "feedback resubmit failed\n");
}

/* Offline-designed matched-z/minimax-extra-zero RIAA biquads, normalized to
 * 0 dB at 1 kHz. See analysis/NOTES.md and tests/hw/riaa/.
 */
static const struct px_riaa_coeff px_riaa_coeffs[] = {
	{ 44100, 245790958, -188270260, -37809732, -1859698319, 787947107 },
	{ 48000, 230659399, -181190551, -32632425, -1880052333, 808011037 },
	{ 88200, 136209875, -116131926, -14770980, -1993016395, 919810722 },
	{ 96000, 125948718, -107977688, -13463856, -2004734401, 931447929 },
};

static s64 px_round_shift(s64 value, unsigned int shift)
{
	s64 half = (s64)1 << (shift - 1);

	if (value >= 0)
		return (value + half) >> shift;
	return -((-value + half) >> shift);
}

static s32 px_s24_clamp(s64 value)
{
	if (value > S24_MAX)
		return S24_MAX;
	if (value < S24_MIN)
		return S24_MIN;
	return (s32)value;
}

static void px_riaa_reset_pair(struct px_dev *px, unsigned int pair)
{
	memset(&px->riaa_state[pair * 2], 0,
	       2 * sizeof(px->riaa_state[0]));
}

static void px_riaa_reset(struct px_dev *px)
{
	memset(px->riaa_state, 0, sizeof(px->riaa_state));
}

/* Called with px->mutex and px->cap.lock held, except during probe before any
 * stream exists. The completion reads only riaa_active[] under cap.lock.
 */
static void px_riaa_update_pair(struct px_dev *px, unsigned int pair)
{
	u16 source = px->codec[pair][WM_ADC_MUX] & ~WM_ADC_MUTE;

	px->riaa_active[pair] = riaa && px->riaa_switch[pair] &&
				 source == ADC_PHONO;
}

static int px_riaa_set_rate(struct px_dev *px, unsigned int rate)
{
	unsigned long flags;
	int i;

	for (i = 0; i < ARRAY_SIZE(px_riaa_coeffs); i++)
		if (px_riaa_coeffs[i].rate == rate) {
			spin_lock_irqsave(&px->cap.lock, flags);
			px->riaa_coeff = &px_riaa_coeffs[i];
			px_riaa_update_pair(px, 0);
			px_riaa_update_pair(px, 1);
			px_riaa_reset(px);
			spin_unlock_irqrestore(&px->cap.lock, flags);
			return 0;
		}
	return -EINVAL;
}

static s32 px_riaa_sample(struct px_riaa_state *state,
			  const struct px_riaa_coeff *coeff, s32 sample)
{
	s64 accumulator, y;
	s32 output;

	/* Numerator products start in S24.Q30 and are promoted to S24.Q38;
	 * feedback products already use the S24.Q8 state and Q30 coefficients.
	 * The conservative worst case is 74.13% of signed-s64 range at 96 kHz.
	 */
	accumulator = ((s64)coeff->b0 * sample +
		       (s64)coeff->b1 * state->x1 +
		       (s64)coeff->b2 * state->x2) *
		      (1 << RIAA_STATE_SHIFT);
	accumulator -= (s64)coeff->a1 * state->y1 +
		       (s64)coeff->a2 * state->y2;
	y = px_round_shift(accumulator, RIAA_COEF_SHIFT);
	if (y > RIAA_STATE_MAX)
		y = RIAA_STATE_MAX;
	else if (y < RIAA_STATE_MIN)
		y = RIAA_STATE_MIN;

	output = px_s24_clamp(px_round_shift(y, RIAA_STATE_SHIFT));
	state->x2 = state->x1;
	state->x1 = sample;
	state->y2 = state->y1;
	state->y1 = (s32)y;
	return output;
}

static void px_riaa_process_frame(struct px_dev *px, s32 *samples)
{
	int pair, channel;

	if (!px->riaa_coeff)
		return;
	for (pair = 0; pair < 2; pair++) {
		if (!px->riaa_active[pair])
			continue;
		for (channel = pair * 2; channel < pair * 2 + 2; channel++)
			samples[channel] = px_riaa_sample(&px->riaa_state[channel],
						       px->riaa_coeff,
						       samples[channel]);
	}
}

/* Track capture sample-peak magnitude for "In 1/2 Peak"/"In 3/4 Peak",
 * post-RIAA (caller runs this after px_riaa_process_frame()) - the
 * meter reflects what RIAA actually hands to the PCM buffer, not the
 * raw phono signal. Channels 4/5 (digital in) have no peak control, so
 * only 0-3 are tracked. Caller (px_cap_complete()) already holds
 * px->cap.lock for the whole decode loop this runs in, so a plain
 * compare-and-store is enough - no extra locking, see
 * analysis/peak-controls-design.md.
 */
static void px_cap_update_peaks(struct px_dev *px, const s32 *samples)
{
	int ch;
	s32 mag;

	for (ch = 0; ch < 4; ch++) {
		mag = samples[ch] < 0 ? -samples[ch] : samples[ch];
		if (mag > S24_MAX)
			mag = S24_MAX;	/* abs(S24_MIN) overflows S24_MAX by 1 */
		if (mag > px->peak_cap[ch])
			px->peak_cap[ch] = mag;
	}
}

/* one 64 byte wire frame -> 6 signed S24 samples */
static void px_decode_frame(const u8 *src, s32 *samples)
{
	u32 v[CAP_CHANNELS] = { 0 };
	int i;

	for (i = 0; i < 24; i++) {
		u8 a = src[i], b = src[32 + i];

		v[0] = (v[0] << 1) | (a & 1);
		v[2] = (v[2] << 1) | ((a >> 1) & 1);
		v[4] = (v[4] << 1) | ((a >> 2) & 1);
		v[1] = (v[1] << 1) | (b & 1);
		v[3] = (v[3] << 1) | ((b >> 1) & 1);
		v[5] = (v[5] << 1) | ((b >> 2) & 1);
	}
	for (i = 0; i < CAP_CHANNELS; i++)
		samples[i] = sign_extend32(v[i], 23);
}

/*
 * Addendum (2026-10-17, v0.1.12 phase 3b): factored out of
 * px_cap_complete() below, mirroring px_play_notify_elapsed() - shared
 * with the fill timer's own px_fill_capture() so both the real completion
 * path and the gap-filling path pair every increment with exactly one
 * snd_pcm_period_elapsed() + decrement, the same discipline review round
 * 5's bug was found missing on the playback side.
 */
static void px_cap_notify_elapsed(struct px_stream *s, struct snd_pcm_substream *elapsed)
{
	if (!elapsed)
		return;
	snd_pcm_period_elapsed(elapsed);
	atomic_inc(&s->in_elapsed_dec_total);
	if (atomic_dec_and_test(&s->in_elapsed))
		wake_up(&s->elapsed_wq);
}

static void px_cap_complete(struct urb *urb)
{
	struct px_dev *px = urb->context;
	struct px_stream *s = &px->cap;
	struct snd_pcm_substream *elapsed = NULL;
	struct snd_pcm_runtime *rt;
	const u8 *src = urb->transfer_buffer;
	unsigned int frames = urb->actual_length / CAP_WIRE_FRAME;
	unsigned int nframes = frames;
	unsigned long flags;
	bool do_print = false;
	unsigned int p_completions = 0, p_frames = 0;
	unsigned long p_bytes = 0, p_elapsed_ms = 0;

	/* capture is bulk, not isochronous - there's no iso_frame_desc[]
	 * array to inspect per-packet here, unlike playback/feedback.
	 * status/actual_length is the whole picture for a bulk completion.
	 */
	dev_dbg_ratelimited(&px->udev->dev,
		"cap complete: status=%d actual_length=%u frames=%u active=%d running=%d\n",
		urb->status, urb->actual_length, frames, s->active, px->running);

	if (urb->status == -ENOENT || urb->status == -ECONNRESET ||
	    urb->status == -ESHUTDOWN || urb->status == -ENODEV ||
	    !px->running)
		return;

	/* Addendum (2026-10-18, phase 3b accuracy fix): one-shot handover
	 * catch-up - see px_play_complete()'s identical fix and
	 * px_fill_catchup_frames()'s own comment.
	 */
	if (px->diag_cap_pending) {
		unsigned int shortfall = px_fill_catchup_frames(px, px->fill_cap_frames);

		px->diag_cap_pending = false;
		if (shortfall)
			px_fill_capture(px, shortfall);
		dev_dbg(&px->udev->dev,
			"live apply: first real cap completion %lldus after kill, "
			"catchup=%u frames, hwptr=%u\n",
			ktime_to_us(ktime_sub(ktime_get(), px->restart_kill_ns)),
			shortfall, s->hwptr);
	}

	spin_lock_irqsave(&s->lock, flags);
	/* watchdog bookkeeping: reuses s->lock rather than adding a new one.
	 * Counts every completion that reaches here (i.e. wasn't a shutdown
	 * status and the stream was running) as proof the capture engine is
	 * alive, regardless of urb->status==0 - see px_cap_watchdog_fn().
	 */
	px->last_cap_urb_status = urb->status;
	px->last_cap_urb_actual_length = urb->actual_length;
	atomic_inc(&px->cap_complete_count);
	/*
	 * Only genuine data counts as proof the stream is alive right now -
	 * a zero-length or error-status completion must not quietly
	 * convince the watchdog that data is flowing (see tests/hw/REPORT.md).
	 */
	if (urb->status == 0 && urb->actual_length > 0) {
		px->cap_last_complete_jiffies = jiffies;
		px->cap_wd_attempt = 0;
		px->cap_wd_gave_up = false;
	}

	/*
	 * TEMPORARY per-second capture summary (v0.1.10-pre1, see
	 * tests/hw/REPORT.md): tally this completion, then once a second
	 * snapshot and reset the counters so the actual dev_dbg() can
	 * happen below without holding this lock.
	 */
	if (px->cap_sum_jiffies == 0)
		px->cap_sum_jiffies = jiffies;
	px->cap_sum_completions++;
	px->cap_sum_bytes += urb->actual_length;

	if (s->active && urb->status == 0 && !WARN_ON_ONCE(!s->sub)) {
		rt = s->sub->runtime;
		if (!WARN_ON_ONCE(!rt)) {
			s32 samples[CAP_CHANNELS];
			__le32 *dst;
			int channel;

			px->cap_sum_frames += nframes;
			while (frames--) {
				px_decode_frame(src, samples);
				px_riaa_process_frame(px, samples);
				px_cap_update_peaks(px, samples);
				dst = (__le32 *)(rt->dma_area +
						 s->hwptr * CAP_FRAME_BYTES);
				for (channel = 0; channel < CAP_CHANNELS; channel++)
					dst[channel] = cpu_to_le32((u32)samples[channel] << 8);
				src += CAP_WIRE_FRAME;
				if (++s->hwptr >= rt->buffer_size)
					s->hwptr = 0;
				if (++s->period_pos >= rt->period_size) {
					s->period_pos = 0;
					elapsed = s->sub;
				}
			}
			/* once per completion, not once per period crossed */
			if (elapsed) {
				atomic_inc(&s->in_elapsed);
				atomic_inc(&s->in_elapsed_inc_total);
			}
		}
	}

	if (time_after(jiffies, px->cap_sum_jiffies + HZ)) {
		do_print = true;
		p_elapsed_ms = jiffies_to_msecs(jiffies - px->cap_sum_jiffies);
		p_completions = px->cap_sum_completions;
		p_bytes = px->cap_sum_bytes;
		p_frames = px->cap_sum_frames;
		px->cap_sum_jiffies = jiffies;
		px->cap_sum_completions = 0;
		px->cap_sum_bytes = 0;
		px->cap_sum_frames = 0;
	}
	spin_unlock_irqrestore(&s->lock, flags);

	if (do_print)
		dev_dbg(&px->udev->dev,
			"cap summary: %lums completions=%u wire_bytes=%lu (%lu B/s) frames_to_alsa=%u (%lu fps)\n",
			p_elapsed_ms, p_completions, p_bytes,
			p_elapsed_ms ? p_bytes * 1000 / p_elapsed_ms : 0,
			p_frames, p_elapsed_ms ? (unsigned long)p_frames * 1000 / p_elapsed_ms : 0);

	if (usb_submit_urb(urb, GFP_ATOMIC) < 0)
		dev_err_ratelimited(&px->udev->dev, "capture resubmit failed\n");
	px_cap_notify_elapsed(s, elapsed);
}

/*
 * Addendum (2026-10-17, v0.1.12 phase 3b, timeline fill): one tick of the
 * fill timer for capture - writes silence (all CAP_CHANNELS, zero) into
 * the ring buffer and advances hwptr/period_pos exactly like
 * px_cap_complete() does for real data, except: no px_decode_frame(), no
 * px_riaa_process_frame(), no px_cap_update_peaks() - silence bypasses
 * RIAA entirely (never touches px->riaa_state, so the filter picks back
 * up from wherever real samples last left it once the gap ends, not from
 * a zero-input transient) and never writes to the peak arrays (a ~300ms
 * gap just leaves the last real peak showing, same as the existing peak
 * hold behavior already does for other reasons). Runs in softirq context
 * (the timer's own) - takes cap.lock only, releases it before calling
 * px_cap_notify_elapsed() (which may call snd_pcm_period_elapsed() -
 * documented safe from interrupt/softirq context, same guarantee the
 * real completion handler already relies on).
 */
static void px_fill_capture(struct px_dev *px, unsigned int frames)
{
	struct px_stream *s = &px->cap;
	struct snd_pcm_substream *elapsed = NULL;
	struct snd_pcm_runtime *rt;
	unsigned long flags;

	spin_lock_irqsave(&s->lock, flags);
	if (s->active && !WARN_ON_ONCE(!s->sub)) {
		rt = s->sub->runtime;
		if (!WARN_ON_ONCE(!rt)) {
			unsigned int remaining = frames;
			unsigned int n;

			while (remaining) {
				n = min_t(unsigned int, remaining, rt->buffer_size - s->hwptr);
				memset(rt->dma_area + s->hwptr * CAP_FRAME_BYTES, 0,
				       n * CAP_FRAME_BYTES);
				s->hwptr += n;
				if (s->hwptr >= rt->buffer_size)
					s->hwptr = 0;
				remaining -= n;
			}
			s->period_pos += frames;
			if (s->period_pos >= rt->period_size) {
				s->period_pos %= rt->period_size;
				elapsed = s->sub;
			}
			if (elapsed) {
				atomic_inc(&s->in_elapsed);
				atomic_inc(&s->in_elapsed_inc_total);
			}
		}
	}
	spin_unlock_irqrestore(&s->lock, flags);
	px_cap_notify_elapsed(s, elapsed);
}

/*
 * Addendum (2026-10-17, v0.1.12 phase 3b): the playback side of the same
 * tick - no data copy at all (app data in the ring buffer is simply
 * discarded for this stretch, per the design doc's own accepted
 * tradeoff: the device really did miss ~0.3s of real audio during the
 * gap), just fast-forward hwptr/period_pos so the application's sense of
 * elapsed time doesn't drift. No px_play_update_peaks() either, same
 * "resets nothing, shows garbage to nobody" reasoning as the capture
 * side.
 */
static void px_fill_playback(struct px_dev *px, unsigned int frames)
{
	struct px_stream *s = &px->play;
	struct snd_pcm_substream *elapsed = NULL;
	struct snd_pcm_runtime *rt;
	unsigned long flags;

	spin_lock_irqsave(&s->lock, flags);
	if (s->active && !WARN_ON_ONCE(!s->sub)) {
		rt = s->sub->runtime;
		if (!WARN_ON_ONCE(!rt)) {
			s->hwptr += frames;
			while (s->hwptr >= rt->buffer_size)
				s->hwptr -= rt->buffer_size;
			s->period_pos += frames;
			if (s->period_pos >= rt->period_size) {
				s->period_pos %= rt->period_size;
				elapsed = s->sub;
			}
			if (elapsed) {
				atomic_inc(&s->in_elapsed);
				atomic_inc(&s->in_elapsed_inc_total);
			}
		}
	}
	spin_unlock_irqrestore(&s->lock, flags);
	px_play_notify_elapsed(px, elapsed);
}

/*
 * Addendum (2026-10-19, review round 4): px_fill_timer_fn()'s own wrapper
 * around px_fill_playback() above - consumes px->play_fill_reserve first
 * (see that field's own comment), only calling through for whatever
 * `frames` remains once the reserve is exhausted. Returns the frame
 * count actually passed to px_fill_playback() (0 while still inside the
 * reserve), which is what the caller should add to its own
 * fill_play_frames running total - not the nominal `frames` argument -
 * so that total correctly reflects play.hwptr's own real advance.
 */
static unsigned int px_fill_playback_reserved(struct px_dev *px, unsigned int frames)
{
	unsigned int play_frames;

	if (px->play_fill_reserve >= frames) {
		px->play_fill_reserve -= frames;
		return 0;
	}
	play_frames = frames - px->play_fill_reserve;
	px->play_fill_reserve = 0;
	px_fill_playback(px, play_frames);
	return play_frames;
}

/*
 * Addendum (2026-10-17, v0.1.12 phase 3b): HRTIMER_MODE_REL_SOFT, so this
 * runs in softirq context, not hardirq - may take cap.lock/play.lock
 * (sequentially, never nested together) but must never do anything that
 * can sleep: no mutex, no msleep(), no GFP_KERNEL allocation. Checks
 * fill_active *before* doing any work and again before re-arming -
 * px_fill_stop() clears it first, then calls hrtimer_cancel(), so a
 * callback already past the first check but not yet re-armed still sees
 * it false at the second check and returns HRTIMER_NORESTART instead of
 * scheduling another tick for hrtimer_cancel() to have to wait out.
 */
static enum hrtimer_restart px_fill_timer_fn(struct hrtimer *timer)
{
	struct px_dev *px = container_of(timer, struct px_dev, fill_timer);
	unsigned int frames;
	u64 overruns;

	if (!atomic_read(&px->fill_active))
		return HRTIMER_NORESTART;

	frames = px_fill_ms_frames(px);
	px_fill_capture(px, frames);
	px->fill_play_frames += px_fill_playback_reserved(px, frames);
	px->fill_ticks++;
	px->fill_cap_frames += frames;

	if (!atomic_read(&px->fill_active))
		return HRTIMER_NORESTART;
	/*
	 * Addendum (2026-10-18, phase 3b diagnostic pass): hrtimer_forward_now()
	 * returns the number of periods that have actually elapsed since
	 * this timer's last expiry, not always 1 - if the softirq is ever
	 * scheduled late enough to skip one or more 1ms periods, the return
	 * value reflects that, and this callback must account for every
	 * skipped period's worth of frames, not just the one tick it's
	 * currently running. fill_overrun_extra tallies exactly how often
	 * and by how much this happens, for px_fill_stop()'s own log line.
	 */
	overruns = hrtimer_forward_now(timer, ms_to_ktime(1));
	if (overruns > 1) {
		unsigned int extra = overruns - 1;
		unsigned int extra_frames = 0;
		unsigned int i;

		/* One px_fill_ms_frames() call per missed period, not a
		 * single call multiplied by `extra` - px->rate_acc's
		 * fractional carry (e.g. 44100/1000 isn't a whole number)
		 * must advance once per missed ms, same as if each tick
		 * had actually fired on time.
		 */
		for (i = 0; i < extra; i++)
			extra_frames += px_fill_ms_frames(px);
		px_fill_capture(px, extra_frames);
		px->fill_play_frames += px_fill_playback_reserved(px, extra_frames);
		px->fill_cap_frames += extra_frames;
		px->fill_overrun_extra += extra;
	}
	return HRTIMER_RESTART;
}

/*
 * Addendum (2026-10-17, v0.1.12 phase 3b): called from
 * px_live_apply_restart() right after it kills the audio URBs.
 * Idempotent across retries - px_live_apply_restart() calls this at the
 * top of every attempt, including retries, but only the first actually
 * arms the timer (atomic_xchg() makes the rest no-ops); the timer then
 * keeps running continuously across retries without needing to be
 * restarted between them, since nothing about the gap changes from the
 * timer's perspective just because one attempt failed and another is
 * starting.
 */
static void px_fill_start(struct px_dev *px)
{
	if (atomic_xchg(&px->fill_active, 1))
		return;
	px->fill_ticks = 0;
	px->fill_overrun_extra = 0;
	px->fill_cap_frames = 0;
	px->fill_play_frames = 0;
	px->play_prefill_frames = 0;
	/* See px->play_fill_reserve's own comment. PLAY_URBS * (rate/1000)
	 * matches exactly what px_live_apply_restart()'s/fallback()'s own
	 * resubmit loop will advance play.hwptr by when it prefills those
	 * same PLAY_URBS - confirmed exact on hardware (zero variance
	 * across 20 restarts), since the feedback FIFO (the other input to
	 * px_next_ms_frames(), used by that exact same prefill call) is
	 * always empty at that point - the feedback URBs were just killed
	 * and resubmitted alongside it, with no time for any real feedback
	 * data to have arrived yet.
	 */
	px->play_fill_reserve = PLAY_URBS * (px->rate / 1000);
	/* Addendum (2026-10-19, review item 2): absorb any carry left by a
	 * previous attempt's own partial PLAY_URBS prefill before it failed
	 * - see px->play_fill_carry's own comment. Clamped at zero: the
	 * carry is bounded by PLAY_URBS * (rate/1000) itself (at most that
	 * many frames can ever have been prefilled before a failure), so
	 * this never goes negative, but clamp defensively rather than rely
	 * on that bound staying true.
	 */
	if (px->play_fill_carry) {
		unsigned int carry = px->play_fill_carry;

		px->play_fill_carry = 0;
		px->play_fill_reserve = px->play_fill_reserve > carry ?
			px->play_fill_reserve - carry : 0;
	}
	px->fill_start_ns = ktime_get();
	hrtimer_start(&px->fill_timer, ms_to_ktime(1), HRTIMER_MODE_REL_SOFT);
}

/*
 * Addendum (2026-10-17, v0.1.12 phase 3b): called from every path that
 * needs the fill timer stopped - the handover point in
 * px_live_apply_restart()/px_live_apply_fallback() (right before their
 * own real resubmit loops, so there is no window where the timer and a
 * freshly-submitted real URB could both be advancing the same position),
 * px_live_apply_give_up() (so the core's own SNDRV_PCM_POS_XRUN handling
 * isn't papered over by a timer that keeps seamlessly advancing
 * position), and px_pcm_sync_stop()/px_stop_streaming()/px_disconnect()
 * as belt-and-braces teardown. Clearing fill_active *before*
 * hrtimer_cancel() (not after) is what lets a callback that's already
 * running finish cleanly without re-arming - see px_fill_timer_fn()'s own
 * comment. hrtimer_cancel() is safe to call unconditionally here
 * (idempotent, returns immediately if the timer isn't active) - this
 * function does not need its own "already stopped" guard. Always called
 * from process context (never from inside the callback itself, which
 * would deadlock) - px_live_apply_restart()/fallback()/give_up() run in
 * the workqueue, sync_stop/stop_streaming/disconnect in the calling
 * process's own context.
 */
static void px_fill_stop(struct px_dev *px)
{
	/* Addendum (2026-10-19, review): log only on the call that actually
	 * transitions fill_active from 1 to 0 - gating on px->fill_ticks
	 * instead (an earlier version of this function did) is wrong:
	 * fill_ticks is only *reset* by px_fill_start(), never cleared by a
	 * stop, so every subsequent belt-and-braces px_fill_stop() call on
	 * an already-stopped timer would re-log the *same stale* counters
	 * and a bogus elapsed time (ktime_get() minus a fill_start_ns from
	 * whatever the last real restart was, however long ago) - confirmed
	 * on hardware: a real instance logged "ticks=307 over 700230786us"
	 * at a stream close nearly 12 minutes after the restart that
	 * actually produced those 307 ticks, which had already stopped
	 * cleanly at its own handover. Not a stuck timer, not a bug beyond
	 * this misleading log line - but worth getting right before this
	 * instrumentation either becomes permanent dev_dbg or gets removed
	 * (review round 3, item 3).
	 */
	bool was_active = atomic_xchg(&px->fill_active, 0);

	hrtimer_cancel(&px->fill_timer);
	if (was_active)
		dev_dbg(&px->udev->dev,
			"fill timer: %u ticks over %lldus, overrun_extra=%u, "
			"cap_frames=%u play_frames=%u\n",
			px->fill_ticks, ktime_to_us(ktime_sub(ktime_get(), px->fill_start_ns)),
			px->fill_overrun_extra, px->fill_cap_frames, px->fill_play_frames);
}

/*
 * Continuous capture watchdog. Unconditionally reschedules itself FIRST,
 * before doing anything else, every single invocation, for as long as
 * it's armed - the only thing that ever stops the chain is
 * cancel_delayed_work_sync() in px_stop_streaming() (process context:
 * hw_free/close/hw_params/a failed start/suspend/disconnect), never a
 * decision made in here. Gen checks (cap_wd_stage_gen vs cap_wd_gen)
 * only reset stale attempt/gave_up state left over from a superseded
 * stream - they never skip rescheduling or acting on the *current*
 * stream (see tests/hw/REPORT.md for the audit that established this;
 * an earlier design's conditional self-requeue could silently die for
 * the rest of a stream via any of several early-return paths, with no
 * log trace at all).
 *
 * Measures elapsed time since cap_last_complete_jiffies (updated only
 * by genuine status==0 && actual_length>0 completions - see
 * px_cap_complete()), not a monotonic completion counter, so a stall
 * starting any time after a healthy start is still caught. On a
 * genuine stall (no real data for >= CAP_WD_STALL_MS): kill and
 * resubmit capture+feedback URBs around an intf1 alt 0 -> alt 1 cycle
 * (the action that revives a dead capture engine - see
 * px_start_streaming()), up to CAP_WD_MAX_ATTEMPTS for this stall
 * episode. If the last attempt still doesn't help, log once and go
 * quiet (no more recovery attempts or log spam) until real data flows
 * again on its own, which immediately resets the attempt counter in
 * px_cap_complete() - so a later, separate stall still gets its own
 * fresh attempts.
 *
 * delayed_work (process context) so all of the above can block; not a
 * check in the playback completion path, because capture routinely runs
 * with no playback active at all.
 *
 * The escalation action itself still happens under px->mutex, which
 * px_stop_streaming() is also always called under - that's what keeps
 * this from resubmitting URBs on a stream that's concurrently
 * stopping. Never wait for that mutex here: stop synchronously cancels
 * this work while holding it. A busy mutex defers recovery to the next
 * already-scheduled tick instead of deadlocking teardown.
 */
static void px_cap_watchdog_fn(struct work_struct *work)
{
	struct px_dev *px = container_of(to_delayed_work(work), struct px_dev,
					 cap_watchdog_work);
	unsigned long flags;
	int urb_status, i;
	unsigned int actual_length;
	unsigned int cur_gen;
	bool active;
	unsigned long last_complete, stall_ms;
	u8 last_written, status = 0;
	int err, serr;
	const char *hint;

	/* Unconditional self-requeue, first thing, every tick - see the
	 * comment above.
	 */
	schedule_delayed_work(&px->cap_watchdog_work, msecs_to_jiffies(CAP_WD_CHECK_MS));

	spin_lock_irqsave(&px->cap.lock, flags);
	active = px->cap.active;
	last_complete = px->cap_last_complete_jiffies;
	urb_status = px->last_cap_urb_status;
	actual_length = px->last_cap_urb_actual_length;
	spin_unlock_irqrestore(&px->cap.lock, flags);

	cur_gen = atomic_read(&px->cap_wd_gen);
	stall_ms = jiffies_to_msecs(jiffies - last_complete);

	if (!active)
		return;		/* nothing to act on this tick - already
				 * rescheduled above, chain stays alive */

	if (stall_ms < CAP_WD_STALL_MS)
		return;		/* healthy (or not stalled long enough yet) */

	if (!mutex_trylock(&px->mutex))
		return;		/* next tick was queued above; stop can drain us */

	spin_lock_irqsave(&px->cap.lock, flags);
	active = px->cap.active;
	spin_unlock_irqrestore(&px->cap.lock, flags);
	if (!active || !px->running || px->disconnected) {
		mutex_unlock(&px->mutex);
		return;
	}
	/* Addendum (2026-10-17, review round 3): give_up means live-apply
	 * has already killed the audio URBs and handed recovery to the PCM
	 * core's own XRUN handling (px_pcm_pointer()) - this watchdog's own
	 * independent "cycle the interface and resubmit" recovery below
	 * must not race that by resubmitting behind its back. Cleared only
	 * by the next successful px_start_streaming().
	 */
	spin_lock_irqsave(&px->desired_lock, flags);
	if (px->give_up) {
		spin_unlock_irqrestore(&px->desired_lock, flags);
		mutex_unlock(&px->mutex);
		return;
	}
	spin_unlock_irqrestore(&px->desired_lock, flags);

	/*
	 * cap_wd_attempt/cap_wd_gave_up only mean anything for the stream
	 * they were last updated for - if trigger(START) has since bumped
	 * cap_wd_gen (a new stream started, possibly while an old stall was
	 * still mid-recovery), stale state must not be trusted; restart
	 * fresh for the current generation. This only resets state, it
	 * never skips acting on the current stream.
	 */
	if (px->cap_wd_stage_gen != cur_gen) {
		px->cap_wd_stage_gen = cur_gen;
		px->cap_wd_attempt = 0;
		px->cap_wd_gave_up = false;
	}

	if (px->cap_wd_gave_up) {
		/* already gave up on this stall episode - stay quiet (the
		 * unconditional reschedule above already keeps the chain
		 * alive to notice if data resumes on its own).
		 */
		mutex_unlock(&px->mutex);
		return;
	}

	last_written = px->last_status_byte;
	err = px_status_read(px, &status);
	/* a live read succeeding here is itself a diagnostic data point:
	 * Finding B (tests/hw/REPORT.md) established that this exact read
	 * reliably fails while the device is genuinely streaming, under
	 * real USB load - if it just succeeded, that's evidence the device
	 * considers itself idle, not actively streaming, right now.
	 */
	hint = err == 0 ?
		" [status read succeeded while streaming = device not streaming]" : "";

	px->cap_wd_attempt++;
	if (px->cap_wd_attempt > CAP_WD_MAX_ATTEMPTS) {
		dev_warn(&px->udev->dev,
			"capture watchdog: still no px_cap_complete() in %lums after "
			"%d alt-cycle attempts - giving up for now. last urb status=%d "
			"actual_length=%u, last written status byte=0x%02x, current "
			"status byte=0x%02x (read err=%d), running=%d clock_internal=%d "
			"digital_input_lock=%s%s\n",
			stall_ms, CAP_WD_MAX_ATTEMPTS, urb_status, actual_length,
			last_written, status, err, px->running, px->clock_internal,
			err < 0 ? "unknown" : (status & STATUS_DIGITAL_LOCK) ? "on" : "off",
			hint);
		px->cap_wd_gave_up = true;
	} else {
		dev_warn(&px->udev->dev,
			"capture watchdog: no px_cap_complete() in %lums (attempt %d/%d) - "
			"last urb status=%d actual_length=%u, last written status "
			"byte=0x%02x, current status byte=0x%02x (read err=%d), "
			"running=%d clock_internal=%d digital_input_lock=%s%s - "
			"cycling intf1 alt 0 -> alt 1 + resubmitting capture and "
			"feedback URBs\n",
			stall_ms, px->cap_wd_attempt, CAP_WD_MAX_ATTEMPTS, urb_status,
			actual_length, last_written, status, err, px->running,
			px->clock_internal,
			err < 0 ? "unknown" : (status & STATUS_DIGITAL_LOCK) ? "on" : "off",
			hint);
		/*
		 * intf1 carries EP 0x81 (feedback) as well as EP 0x86
		 * (capture) - see the file header comment. An altsetting
		 * change is interface-wide, so it implicitly invalidates
		 * feedback's URBs too, not just capture's; feedback must be
		 * killed and resubmitted right alongside capture here, or
		 * playback's rate correction (px_next_ms_frames()) silently
		 * loses its feedback input for the rest of the stream.
		 * Feedback is always active whenever capture is (both are
		 * submitted together in px_start_streaming()), independent
		 * of whether playback itself is in use.
		 */
		for (i = 0; i < CAP_URBS; i++)
			usb_kill_urb(px->cap_urb[i]);
		for (i = 0; i < FB_URBS; i++)
			usb_kill_urb(px->fb_urb[i]);
		serr = usb_set_interface(px->udev, 1, 0);
		if (!serr)
			serr = usb_set_interface(px->udev, 1, 1);
		if (serr < 0)
			dev_warn_ratelimited(&px->udev->dev,
				"capture watchdog: usb_set_interface failed: %d\n", serr);
		spin_lock_irqsave(&px->lock, flags);
		px->fb_head = 0;
		px->fb_count = 0;
		spin_unlock_irqrestore(&px->lock, flags);
		for (i = 0; i < FB_URBS; i++) {
			serr = usb_submit_urb(px->fb_urb[i], GFP_KERNEL);
			if (serr < 0)
				dev_warn_ratelimited(&px->udev->dev,
					"capture watchdog: feedback resubmit failed: %d\n", serr);
		}
		for (i = 0; i < CAP_URBS; i++) {
			serr = usb_submit_urb(px->cap_urb[i], GFP_KERNEL);
			if (serr < 0)
				dev_warn_ratelimited(&px->udev->dev,
					"capture watchdog: capture resubmit failed: %d\n", serr);
		}
	}

	mutex_unlock(&px->mutex);
}

static void px_kill_audio_urbs(struct px_dev *px)
{
	int i;

	for (i = 0; i < PLAY_URBS; i++)
		usb_kill_urb(px->play_urb[i]);
	for (i = 0; i < FB_URBS; i++)
		usb_kill_urb(px->fb_urb[i]);
	for (i = 0; i < CAP_URBS; i++)
		usb_kill_urb(px->cap_urb[i]);
}

static void px_stop_streaming(struct px_dev *px)
{
	unsigned long flags;

	if (!px->running)
		return;
	px->running = false;
	/* v0.1.12 phase 3b: belt-and-braces - stop the filler before
	 * anything else. Idempotent (safe even if it was never running).
	 */
	px_fill_stop(px);
	px_kill_audio_urbs(px);
	spin_lock_irqsave(&px->desired_lock, flags);
	px->urbs_live = false;
	/* A full stop/close is itself a way out of give_up (there's no
	 * stream left for it to describe), even though the only place that
	 * actually matters for recovery is px_start_streaming()'s own clear.
	 */
	px->give_up = false;
	spin_unlock_irqrestore(&px->desired_lock, flags);
	/*
	 * The one and only place that ever stops the watchdog's
	 * unconditional self-requeue chain (px_cap_watchdog_fn() never
	 * decides this on its own - see its comment). The watchdog uses
	 * mutex_trylock(), never waits for px->mutex, and holds no spinlock
	 * while returning, so cancelling it while holding px->mutex cannot
	 * form a mutex/work-completion wait cycle. Callers are in process
	 * context (hw_free/close/hw_params, failed start, suspend, disconnect),
	 * not atomic trigger(STOP), which only uses cancel_delayed_work().
	 */
	cancel_delayed_work_sync(&px->cap_watchdog_work);
	/* Same reasoning, same safety: px_live_apply_work() also uses
	 * mutex_trylock(), never blocks on px->mutex, and holds no spinlock
	 * while returning (see its own comment) - hardware-tested since
	 * v0.1.12 (analysis/v0.1.12-plan.md section 17). live_apply_stopping (review round 3):
	 * bounds this cancel's wait to whichever single operation the
	 * worker was already in when it notices, instead of its full
	 * worst-case sequence - see px->live_apply_stopping's own comment.
	 */
	atomic_inc(&px->live_apply_stopping);
	cancel_delayed_work_sync(&px->live_apply_work);
	atomic_dec(&px->live_apply_stopping);
	px->stop_jiffies = jiffies;
}

/*
 * Updates the Digital Input Lock cache from an already-read status byte
 * and notifies listeners if it changed. Lazy and called from exactly
 * one place - px_lock_get()'s own live read while !px->running - rather
 * than proactively from a stream stop: a stop is not a good moment for
 * an extra control transfer (right after URBs were just killed, a
 * point already documented elsewhere as prone to -EPIPE/-ETIMEDOUT),
 * and it would be redundant anyway, since the next get() does its own
 * fresh read regardless.
 */
static void px_update_lock_cache(struct px_dev *px, u8 status)
{
	bool locked = !!(status & STATUS_DIGITAL_LOCK);

	if (locked != px->last_digital_lock) {
		px->last_digital_lock = locked;
		if (px->lock_kctl)
			snd_ctl_notify(px->card, SNDRV_CTL_EVENT_MASK_VALUE,
				       &px->lock_kctl->id);
	}
}

/*
 * Right after a very recent stream stop, the device intermittently stalls
 * (-EPIPE) or doesn't answer in time (-ETIMEDOUT) on the first control
 * transfer of the next start sequence - seen in practice as a few failed
 * prepare() calls in a row before one succeeds (see tests/hw/REPORT.md).
 * px_status_read()/px_set_rate_ep() already log the specifics via
 * dev_err_ratelimited(); retry those two transient errors a bounded few
 * times here instead of failing prepare() outright. -EPIPE (control stall)
 * gets up to CTRL_EPIPE_RETRIES attempts at CTRL_RETRY_DELAY_MS apart;
 * -ETIMEDOUT gets only CTRL_ETIMEDOUT_RETRIES, since each attempt already
 * costs a full CTRL_TIMEOUT. Any other error, or running out of retries,
 * is returned as-is. Each retry is logged via dev_warn_ratelimited() with
 * which transfer it was, so the stress test can show whether the settle
 * delay below makes these retries unnecessary in practice.
 */
#define px_ctrl_retry(px, what, call) ({				\
	int __e = (call);						\
	int __i = 0, __max;						\
	while (__e == -EPIPE || __e == -ETIMEDOUT) {			\
		__max = (__e == -EPIPE) ? CTRL_EPIPE_RETRIES :		\
					   CTRL_ETIMEDOUT_RETRIES;	\
		if (__i >= __max)					\
			break;						\
		__i++;							\
		dev_warn_ratelimited(&(px)->udev->dev,			\
			"%s: retry %d/%d after %d\n",			\
			(what), __i, __max, __e);			\
		msleep(CTRL_RETRY_DELAY_MS);				\
		__e = (call);						\
	}								\
	__e;								\
})

/*
 * Addendum (2026-10-17, review round 3): same retry shape as
 * px_ctrl_retry() above, but checks px->live_apply_stopping before each
 * *retry* (not the first attempt - that one's result is what decides
 * whether to retry at all) and bails with -ESHUTDOWN instead. Only the
 * live-apply path uses this, not px_start_streaming()'s own use of plain
 * px_ctrl_retry() - the two have different cancellation needs. Bounds
 * how long a single call can keep this function from noticing a stop
 * request to "whichever one attempt was already in flight when the flag
 * was set" (at most CTRL_TIMEOUT, since that's the longest a single
 * usb_control_msg* call can block), not the full remaining retry budget.
 */
#define px_ctrl_retry_stoppable(px, what, call) ({			\
	int __e = (call);						\
	int __i = 0, __max;						\
	while (__e == -EPIPE || __e == -ETIMEDOUT) {			\
		if (atomic_read(&(px)->live_apply_stopping)) {		\
			__e = -ESHUTDOWN;				\
			break;						\
		}							\
		__max = (__e == -EPIPE) ? CTRL_EPIPE_RETRIES :		\
					   CTRL_ETIMEDOUT_RETRIES;	\
		if (__i >= __max)					\
			break;						\
		__i++;							\
		dev_warn_ratelimited(&(px)->udev->dev,			\
			"%s: retry %d/%d after %d\n",			\
			(what), __i, __max, __e);			\
		msleep(CTRL_RETRY_DELAY_MS);				\
		__e = (call);						\
	}								\
	__e;								\
})

static int px_codec_resync_shadow(struct px_dev *px)
{
	static const u8 regs[] = { WM_R7, WM_ADC_MUX, WM_OUT_MUX };
	u16 snapshot[2][CODEC_REGS];
	unsigned long flags;
	unsigned int gen;
	int c, i, err;

	/*
	 * Addendum (2026-10-17, review round 4): resync from desired_codec,
	 * not codec - replaces the earlier "reschedule live_apply_work from
	 * sync_stop" fix (removed; it could race the application's own
	 * trigger(START), since px_start_streaming() runs synchronously
	 * inside prepare(), before trigger(START) can possibly follow, while
	 * a *rescheduled* work item fires some time after - a window where
	 * it could kill freshly-submitted URBs out from under an
	 * already-active stream). Live-path puts only ever stage into
	 * desired_codec while running (px_switch_put()/px_adc_source_put()),
	 * so if a live-apply attempt was aborted or never got the chance to
	 * run (px_pcm_sync_stop()'s own forced teardown, or
	 * px_live_apply_check_stopping() aborting mid-sequence - review
	 * round 3) before the stream stopped, desired_codec can be ahead of
	 * codec at the next start. Reading desired_codec here instead picks
	 * that up directly, as part of the normal start path, with no extra
	 * work item and no race window: by the time this returns,
	 * live_applied_gen is caught up, under the same px->mutex
	 * (px_pcm_prepare()'s own hold, same as always - this function's own
	 * pre-existing "caller holds px->mutex" contract, below) that
	 * already keeps this from racing a concurrent idle-path put(). When
	 * nothing is pending, desired_codec already equals codec (every
	 * px_codec_write() call - live-apply's own replay included - mirrors
	 * both), so this is not a behavior change in the common case.
	 *
	 * Caller holds px->mutex. Mixer puts use the same mutex (while
	 * stopped) or desired_lock alone (while running - not relevant here,
	 * since this only ever runs while stopped, before px->running is set
	 * true below), so this snapshot is coherent: a put before this call
	 * is included, and a put waiting behind it runs afterwards and wins.
	 */
	spin_lock_irqsave(&px->desired_lock, flags);
	memcpy(snapshot, px->desired_codec, sizeof(snapshot));
	gen = px->desired_gen;
	spin_unlock_irqrestore(&px->desired_lock, flags);

	for (c = 0; c < 2; c++)
		for (i = 0; i < ARRAY_SIZE(regs); i++) {
			err = px_ctrl_retry(px, "codec resync",
					    px_codec_write(px, c, regs[i],
							   snapshot[c][regs[i]]));
			if (err < 0)
				return err;
		}

	spin_lock_irqsave(&px->desired_lock, flags);
	px->live_applied_gen = gen;
	spin_unlock_irqrestore(&px->desired_lock, flags);
	return 0;
}

static int px_start_streaming(struct px_dev *px, unsigned int rate)
{
	int err, i, serr;
	unsigned int since_stop_ms, live_rate;
	bool rate_changed, urbs_live;
	unsigned long flags;
	u8 status;

	if (px->running) {
		/* Addendum (2026-10-17, lock-ordering review): running alone
		 * used to be sufficient here - before v0.1.12 it was, since
		 * only this function and px_stop_streaming() ever touched
		 * URBs, 1:1 with running. The live-apply path (and
		 * px_pcm_sync_stop()'s own forced teardown on a stuck wait)
		 * can now kill URBs without running ever going false - if
		 * that happened, fall through and actually resubmit instead
		 * of trusting a running flag that no longer implies live
		 * URBs. See px->urbs_live's own comment.
		 */
		spin_lock_irqsave(&px->desired_lock, flags);
		urbs_live = px->urbs_live;
		spin_unlock_irqrestore(&px->desired_lock, flags);
		if (urbs_live)
			return px->rate == rate ? 0 : -EBUSY;
		dev_warn(&px->udev->dev,
			"start: running but URBs are not live (previous live-apply "
			"or sync_stop teardown) - redoing the full start sequence\n");
	}
	if (px->disconnected)
		return -ENODEV;
	err = px_riaa_set_rate(px, rate);
	if (err < 0)
		return err;

	/* px->rate holds the last configured rate even while stopped
	 * (px_stop_streaming() never clears it) - px->rate==0 only at the
	 * very first-ever start, which isn't a "change" from anything.
	 */
	rate_changed = px->rate != 0 && px->rate != rate;

	/*
	 * Give the device a brief settle time after a very recent stop
	 * before touching it again - called under px->mutex only (no
	 * spinlock held here), so msleep() is safe.
	 */
	since_stop_ms = jiffies_to_msecs(jiffies - px->stop_jiffies);
	if (since_stop_ms < PX_SETTLE_MS)
		msleep(PX_SETTLE_MS - since_stop_ms);

	err = px_ctrl_retry(px, "status read", px_status_read(px, &status));
	if (err < 0)
		return err;
	if (codec_resync) {
		err = px_codec_resync_shadow(px);
		if (err < 0)
			return err;
	}

	/* SET_CUR capture/play/capture, matching the official Windows
	 * driver's start sequence (see tests/hw/REPORT.md "Windows USB
	 * trace").
	 */
	err = px_ctrl_retry(px, "SET_CUR capture rate",
			     px_set_rate_ep(px, EP_CAPTURE, rate));
	if (err < 0)
		return err;
	err = px_ctrl_retry(px, "SET_CUR play rate",
			     px_set_rate_ep(px, EP_PLAY, rate));
	if (err < 0)
		return err;
	err = px_ctrl_retry(px, "SET_CUR capture rate (verify pair)",
			     px_set_rate_ep(px, EP_CAPTURE, rate));
	if (err < 0)
		return err;

	/*
	 * GET_CUR verify: if the digital input is locked (status bit
	 * STATUS_DIGITAL_LOCK), the device's actual rate follows the
	 * external clock regardless of what we just asked for (see
	 * tests/hw/REPORT.md "Windows USB trace" / "Confirmed without the
	 * S/PDIF cable") - refuse rather than silently capture at the wrong
	 * speed. px_pcm_open()'s hw_params rate constraint (see below)
	 * normally keeps this from ever mismatching in the first place; this
	 * is defense in depth against a lock that engages in the window
	 * between open() and here.
	 */
	err = px_get_rate_ep(px, 0, &live_rate, NULL);
	if (err < 0) {
		dev_warn(&px->udev->dev, "rate verify read failed: %d\n", err);
	} else if (live_rate != rate) {
		if (status & STATUS_DIGITAL_LOCK) {
			dev_warn(&px->udev->dev,
				"refusing to start: digital input is locked at "
				"%u Hz, %u Hz was requested - no silent capture "
				"at the wrong speed\n", live_rate, rate);
			return -EINVAL;
		}
		dev_warn(&px->udev->dev,
			 "device reports %u Hz instead of the requested %u Hz\n",
			 live_rate, rate);
	}

	/* Unconditional status write, matching the Windows driver (it
	 * writes the status byte even when unchanged) - see
	 * tests/hw/REPORT.md "Windows USB trace".
	 */
	status = (status & ~STATUS_INTERNAL_CLOCK) | STATUS_STREAM | STATUS_SMALL_BUFFER |
		 (px->clock_internal ? STATUS_INTERNAL_CLOCK : 0);
	err = px_ctrl_retry(px, "status write", px_status_write(px, status));
	if (err < 0)
		return err;
	msleep(40);

	/*
	 * On a rate change, SET_CUR just reconfigured the device's clock -
	 * give it RATE_SETTLE_MS before the clear_halt/alt-cycle+submit
	 * below (see tests/hw/REPORT.md: a 44.1kHz rate change reliably
	 * stalled without this).
	 */
	if (rate_changed)
		msleep(RATE_SETTLE_MS);

	px->rate = rate;
	px->rate_acc = 0;
	px->fb_head = 0;
	px->fb_count = 0;
	px->running = true;

	/*
	 * Default: clear_halt() on EP_CAPTURE after a settle delay, no
	 * alt-cycle - matches the official Windows driver's ABORT_PIPE +
	 * SYNC_RESET_PIPE_AND_CLEAR_STALL on 0x86 (~90-175ms after the
	 * status write, see tests/hw/REPORT.md "Windows USB trace"),
	 * confirmed clean on Linux without the S/PDIF cable ("Confirmed
	 * without the S/PDIF cable", step 2: 5/5 fresh-replug trials, zero
	 * watchdog firings).
	 *
	 * start_alt_cycle=1 reverts to the older v0.1.9 sequence (cycle
	 * intf1 alt 0 -> alt 1 instead, no clear_halt) in case a different
	 * unit/firmware needs it. Either way, the capture watchdog's own
	 * alt-cycle recovery action (see px_cap_watchdog_fn()) is separate
	 * from this and always available regardless of this setting.
	 */
	if (start_alt_cycle) {
		serr = usb_set_interface(px->udev, 1, 0);
		if (!serr)
			serr = usb_set_interface(px->udev, 1, 1);
		if (serr < 0)
			dev_warn(&px->udev->dev,
				"capture start: intf1 alt-cycle failed: %d\n", serr);
	} else {
		msleep(CLEAR_HALT_DELAY_MS);
		serr = usb_clear_halt(px->udev, usb_rcvbulkpipe(px->udev, EP_CAPTURE));
		if (serr < 0)
			dev_warn(&px->udev->dev,
				"capture start: clear_halt failed: %d\n", serr);
	}

	/* URB submit order 0x86 (capture), 0x81 (feedback), 0x02 (play),
	 * matching the official Windows driver.
	 */
	for (i = 0; i < CAP_URBS; i++) {
		err = usb_submit_urb(px->cap_urb[i], GFP_KERNEL);
		if (err < 0)
			goto fail;
	}
	for (i = 0; i < FB_URBS; i++) {
		err = usb_submit_urb(px->fb_urb[i], GFP_KERNEL);
		if (err < 0)
			goto fail;
	}
	for (i = 0; i < PLAY_URBS; i++) {
		px_play_notify_elapsed(px, px_fill_play_urb(px, px->play_urb[i]));
		err = usb_submit_urb(px->play_urb[i], GFP_KERNEL);
		if (err < 0)
			goto fail;
	}
	spin_lock_irqsave(&px->desired_lock, flags);
	px->urbs_live = true;
	/* Addendum (2026-10-17, review round 3): "the next prepare/start" is
	 * the one recovery path out of give_up (see that field's own
	 * comment) - this is it.
	 */
	px->give_up = false;
	spin_unlock_irqrestore(&px->desired_lock, flags);
	return 0;

fail:
	dev_err(&px->udev->dev, "cannot start streaming: %d\n", err);
	px_stop_streaming(px);
	return err;
}

/*
 * v0.1.12 live codec apply, phase 3a (analysis/v0.1.12-plan.md sections
 * 3-5 - NOT section 6, the timeline bridge). Hardware-tested since v0.1.12,
 * see analysis/v0.1.12-plan.md section 17.
 *
 * The mode-4 hardware sequence for a live (while-streaming) codec control
 * change: kill all audio URBs, replay every changed codec register from
 * the snapshot, redo SET_CUR/GET_CUR/status/clear_halt, resubmit in the
 * same capture/feedback/playback order px_start_streaming() uses.
 * Deliberately does NOT touch px->running, px->rate, px->rate_acc,
 * px->fb_head/fb_count, or any ALSA-visible state (s->active, hwptr,
 * period_pos) - section 1's non-negotiable acceptance criteria hold even
 * without section 6's bridge. Without that bridge, the gap between kill
 * and resubmit simply produces no completions - existing period/hwptr
 * bookkeeping pauses rather than being filled with accounted silence.
 * That is the explicit phase-3a tradeoff (matching the diagnostic's
 * observed pre-bridge behavior, section 1: "~0.3s is missing at each
 * toggle; later audio shifts earlier"), not an oversight.
 *
 * A deliberate near-duplicate of px_start_streaming()'s codec-through-
 * resubmit tail, not a refactor of it: that function is hardware-
 * validated and owns first-start concerns (rate_acc/fb_head reset,
 * px->running) this path must not touch, and keeping them separate means
 * this new, untested path cannot regress the existing, validated one.
 *
 * Known gap, not fixed here: the caller only skips calling this at all
 * when desired_gen == live_applied_gen. A rapid on/off/on-again sequence
 * that nets out to no actual register difference from what's already
 * applied still bumps desired_gen each time, so this still runs the full
 * restart (and its ~300ms gap) for zero real change, since nothing here
 * compares the *whole* snapshot against px->codec before deciding
 * whether to run at all - only per-register inside the replay loop,
 * which affects what gets written, not whether the restart happens.
 * Left as a documented limitation rather than adding more untested logic
 * to special-case it.
 *
 * Caller (px_live_apply_work()) holds px->mutex and has already verified
 * px->running and !px->disconnected.
 *
 * *out_step is set on any failure (never read on success) to a short,
 * fixed string identifying which step failed, for the caller's retry log -
 * addendum (2026-10-16, v0.1.12-plan.md section 11). live_fail_step forces
 * a given step to fail without touching the device, for the same addendum's
 * fault injection (see its module_param comment).
 */
/*
 * Addendum (2026-10-17, review round 3): checked between every sleep,
 * control transfer and retry in px_live_apply_restart()/fallback() - see
 * px->live_apply_stopping's own comment. Not checked inside
 * px_ctrl_retry_stoppable()'s *first* attempt of a call (that result is
 * what decides whether to retry at all; checking before the call would
 * just move the same bound one line earlier for no benefit).
 */
static bool px_live_apply_check_stopping(struct px_dev *px, const char **out_step)
{
	if (!atomic_read(&px->live_apply_stopping))
		return false;
	*out_step = "aborted (stream stopping)";
	return true;
}

static int px_live_apply_restart(struct px_dev *px, u16 snapshot[2][CODEC_REGS],
				  const char **out_step)
{
	unsigned int rate = px->rate, live_rate;
	unsigned long flags;
	u8 status;
	int err, i, c, r, serr;
	/* Addendum (2026-10-19, review item 2): set at the handover point
	 * below, used only by the fail: path - see play_fill_carry's own
	 * comment.
	 */
	unsigned int play_hwptr_before_prefill = 0;
	bool prefill_started = false;

	px_kill_audio_urbs(px);
	spin_lock_irqsave(&px->desired_lock, flags);
	px->urbs_live = false;
	spin_unlock_irqrestore(&px->desired_lock, flags);
	/* Addendum (2026-10-18, phase 3b diagnostic pass): hwptr snapshot at
	 * the moment URBs just died, and the kill timestamp for the
	 * kill-to-first-real-completion gap measurement in px_cap_complete()/
	 * px_play_complete().
	 */
	px->restart_kill_ns = ktime_get();
	dev_dbg(&px->udev->dev, "live apply: kill hwptr cap=%u play=%u\n",
		px->cap.hwptr, px->play.hwptr);
	/* v0.1.12 phase 3b: fill the gap just opened, from here until this
	 * function's own resubmit loop below (or px_live_apply_fallback()'s,
	 * if every attempt of this function fails first) stops it again.
	 */
	px_fill_start(px);

	if (px_live_apply_check_stopping(px, out_step))
		return -ESHUTDOWN;

	if (live_settle_ms)
		msleep(min_t(unsigned int, live_settle_ms, 500));

	if (px_live_apply_check_stopping(px, out_step))
		return -ESHUTDOWN;

	if (live_fail_step == 1) {
		*out_step = "status read (fault injection)";
		return -EIO;
	}
	err = px_ctrl_retry_stoppable(px, "live apply: status read", px_status_read(px, &status));
	if (err < 0) {
		*out_step = "status read";
		return err;
	}

	/* Complete replay of every changed register, irrespective of the
	 * codec_resync module param - "complete replay is part of live
	 * apply's contract" (design doc section 4 step 3). Six logical
	 * writes at most (R7/R21/R22 x 2 codecs), matching
	 * px_codec_resync_shadow()'s own "six values" framing; skips
	 * registers the snapshot didn't actually change.
	 */
	for (c = 0; c < 2; c++) {
		for (r = 0; r < CODEC_REGS; r++) {
			bool was_adc_mux = r == WM_ADC_MUX &&
				snapshot[c][r] != px->codec[c][r];

			if (snapshot[c][r] == px->codec[c][r])
				continue;
			if (px_live_apply_check_stopping(px, out_step))
				return -ESHUTDOWN;
			if (live_fail_step == 2) {
				*out_step = "codec write (fault injection)";
				return -EIO;
			}
			err = px_ctrl_retry_stoppable(px, "live apply: codec write",
						       px_codec_write(px, c, r, snapshot[c][r]));
			if (err < 0) {
				*out_step = "codec write";
				return err;
			}
			/* Capture source actually changed in hardware just
			 * now - only at this point does the "applied source"
			 * become the new one (design doc section 7: decode
			 * with the *applied* source's RIAA state until
			 * commit; px_adc_source_put()'s live branch
			 * deliberately left this to here instead of doing it
			 * at put() time). unsigned long flags is local to
			 * this if to avoid widening every iteration's scope.
			 */
			if (was_adc_mux) {
				unsigned long riaa_flags;

				spin_lock_irqsave(&px->cap.lock, riaa_flags);
				px_riaa_update_pair(px, c);
				px_riaa_reset_pair(px, c);
				spin_unlock_irqrestore(&px->cap.lock, riaa_flags);
			}
		}
	}

	if (px_live_apply_check_stopping(px, out_step))
		return -ESHUTDOWN;
	if (live_fail_step == 3) {
		*out_step = "SET_CUR rate (fault injection)";
		return -EIO;
	}
	err = px_ctrl_retry_stoppable(px, "live apply: SET_CUR capture rate",
				       px_set_rate_ep(px, EP_CAPTURE, rate));
	if (err < 0) {
		*out_step = "SET_CUR capture rate";
		return err;
	}
	err = px_ctrl_retry_stoppable(px, "live apply: SET_CUR play rate",
				       px_set_rate_ep(px, EP_PLAY, rate));
	if (err < 0) {
		*out_step = "SET_CUR play rate";
		return err;
	}
	err = px_ctrl_retry_stoppable(px, "live apply: SET_CUR capture rate (verify pair)",
				       px_set_rate_ep(px, EP_CAPTURE, rate));
	if (err < 0) {
		*out_step = "SET_CUR capture rate (verify pair)";
		return err;
	}

	err = px_get_rate_ep(px, 0, &live_rate, NULL);
	if (err < 0)
		dev_warn(&px->udev->dev, "live apply: rate verify read failed: %d\n", err);
	else if (live_rate != rate)
		dev_warn(&px->udev->dev,
			"live apply: device reports %u Hz instead of the running %u Hz\n",
			live_rate, rate);

	if (px_live_apply_check_stopping(px, out_step))
		return -ESHUTDOWN;
	if (live_fail_step == 4) {
		*out_step = "status write (fault injection)";
		return -EIO;
	}
	status = (status & ~STATUS_INTERNAL_CLOCK) | STATUS_STREAM | STATUS_SMALL_BUFFER |
		 (px->clock_internal ? STATUS_INTERNAL_CLOCK : 0);
	err = px_ctrl_retry_stoppable(px, "live apply: status write", px_status_write(px, status));
	if (err < 0) {
		*out_step = "status write";
		return err;
	}
	msleep(40);

	if (px_live_apply_check_stopping(px, out_step))
		return -ESHUTDOWN;
	msleep(CLEAR_HALT_DELAY_MS);
	serr = usb_clear_halt(px->udev, usb_rcvbulkpipe(px->udev, EP_CAPTURE));
	if (serr < 0)
		dev_warn(&px->udev->dev, "live apply: clear_halt failed: %d\n", serr);

	/* Addendum (2026-10-17, hardware hang fix): re-check immediately
	 * before resubmitting, not just at px_live_apply_work()'s entry -
	 * this whole function runs for long enough (the settle sleep above
	 * alone, plus however many retried control transfers preceded it)
	 * that a stop/close could have been requested since. px->mutex is
	 * held throughout (caller's contract, same as px->running itself
	 * only ever being written under it), so this read is already
	 * race-free, no extra lock needed. -ESHUTDOWN routes through the
	 * same px_is_device_gone() handling as a real disconnect in the
	 * caller: no fallback, no XRUN - the stream is already being torn
	 * down through its own proper path, nothing here should race it.
	 * live_apply_stopping check added alongside it (review round 3):
	 * running can still be true while a sync_stop is actively in
	 * progress (it only clears running on the forced-teardown timeout
	 * path, not on every call) - don't resubmit into that window either.
	 */
	if (!px->running || px_live_apply_check_stopping(px, out_step)) {
		if (!*out_step)
			*out_step = "URB resubmit (stream stopping)";
		return -ESHUTDOWN;
	}
	if (live_fail_step == 5) {
		*out_step = "URB resubmit (fault injection)";
		err = -EIO;
		goto fail;
	}
	/* v0.1.12 phase 3b: handover point - stop the filler (synchronously,
	 * hrtimer_cancel() inside) before the first real URB goes back in
	 * flight, so the filler and a real completion can never both be
	 * advancing the same position at once. If any of the submits below
	 * fail, the fail: path kills everything again - the *next* retry
	 * attempt's own px_fill_start() re-arms it, same as settling for a
	 * brief filler-off gap on an uncommon failure path rather than
	 * restarting it here only to immediately tear it down again on fail.
	 */
	px_fill_stop(px);
	/* Addendum (2026-10-18, phase 3b diagnostic pass): hwptr snapshot
	 * right after the filler stops, and the one-shot pending flags that
	 * make the *next* real completion for each stream log the gap back
	 * to restart_kill_ns above.
	 */
	dev_dbg(&px->udev->dev, "live apply: handover hwptr cap=%u play=%u\n",
		px->cap.hwptr, px->play.hwptr);
	px->diag_cap_pending = true;
	px->diag_play_pending = true;
	/* Addendum (2026-10-19, review item 2): snapshot here, not inside
	 * the PLAY_URBS block below - nothing between here and there touches
	 * play.hwptr, so this is valid as the "before" baseline regardless
	 * of which of the three loops below (if any) ends up failing. The
	 * fail: path uses it to compute how much of play.hwptr's advance (if
	 * any) already happened before the failure.
	 */
	play_hwptr_before_prefill = px->play.hwptr;
	prefill_started = true;
	for (i = 0; i < CAP_URBS; i++) {
		err = usb_submit_urb(px->cap_urb[i], GFP_KERNEL);
		if (err < 0)
			goto fail;
	}
	for (i = 0; i < FB_URBS; i++) {
		err = usb_submit_urb(px->fb_urb[i], GFP_KERNEL);
		if (err < 0)
			goto fail;
	}
	{
		/* Addendum (2026-10-19, review): does the prefill below
		 * double-count against the timer+catch-up's own advance?
		 * Diagnostic only - see the dev_dbg after the loop.
		 */
		unsigned int prefill_frames;

		for (i = 0; i < PLAY_URBS; i++) {
			px_play_notify_elapsed(px, px_fill_play_urb(px, px->play_urb[i]));
			err = usb_submit_urb(px->play_urb[i], GFP_KERNEL);
			if (err < 0)
				goto fail;
		}
		prefill_frames = px->play.hwptr >= play_hwptr_before_prefill ?
			px->play.hwptr - play_hwptr_before_prefill :
			(px->play.sub && px->play.sub->runtime ?
			 px->play.sub->runtime->buffer_size : 0) -
			play_hwptr_before_prefill + px->play.hwptr;
		/* Addendum (2026-10-19, review): the actual fix - see
		 * px->play_prefill_frames's own comment. Consumed by
		 * px_play_complete()'s catch-up call.
		 */
		px->play_prefill_frames = prefill_frames;
		dev_dbg(&px->udev->dev,
			"live apply: play prefill %u frames (hwptr %u -> %u), "
			"timer play_frames=%u\n",
			prefill_frames, play_hwptr_before_prefill, px->play.hwptr,
			px->fill_play_frames);
	}
	spin_lock_irqsave(&px->desired_lock, flags);
	px->urbs_live = true;
	spin_unlock_irqrestore(&px->desired_lock, flags);
	return 0;

fail:
	/* Some audio URBs may have failed to resubmit, possibly including
	 * playback - which the existing capture watchdog never touches (it
	 * only ever resubmits feedback and capture, by original design, see
	 * px_cap_watchdog_fn()). Leave px->running as-is (still true, by
	 * design - see the function comment); px->urbs_live is already
	 * false (set at this function's entry, never set true on this
	 * path) so px_start_streaming()'s own guard knows to redo a real
	 * start rather than trust running alone. Addendum (2026-10-17): the
	 * caller's retry/fallback sequence (section 11) owns recovery from
	 * here, not an XRUN forced by this function (section 12 - that
	 * call was removed as a lock-ordering risk, not reinstated).
	 */
	if (!*out_step)
		*out_step = "URB resubmit";
	dev_err(&px->udev->dev, "live apply: resubmit failed: %d\n", err);
	/* Addendum (2026-10-19, review item 2): if play.hwptr already
	 * advanced (CAP_URBS/FB_URBS failed: 0; PLAY_URBS failed partway:
	 * the URBs submitted before the failing one already had
	 * px_fill_play_urb() advance it, a phantom advance that will never
	 * actually play - these URBs are about to be killed again below),
	 * carry that amount forward so the next attempt's px_fill_start()
	 * withholds it from a fresh reserve instead of double-counting it.
	 * See play_fill_carry's own comment.
	 */
	if (prefill_started) {
		unsigned int partial = px->play.hwptr >= play_hwptr_before_prefill ?
			px->play.hwptr - play_hwptr_before_prefill :
			(px->play.sub && px->play.sub->runtime ?
			 px->play.sub->runtime->buffer_size : 0) -
			play_hwptr_before_prefill + px->play.hwptr;

		if (partial) {
			px->play_fill_carry += partial;
			dev_dbg(&px->udev->dev,
				"live apply: resubmit failed with %u frames of "
				"partial playback prefill already applied - "
				"carrying it to the next attempt's reserve\n",
				partial);
		}
	}
	px_kill_audio_urbs(px);
	return err;
}

static bool px_is_device_gone(int err)
{
	return err == -ENODEV || err == -ESHUTDOWN;
}

/* Backoff before each retry in px_live_apply_work(), addendum section 11. */
static const unsigned int live_apply_backoff_ms[LIVE_APPLY_MAX_RETRIES] = { 100, 300 };

/*
 * Addendum (2026-10-16, v0.1.12-plan.md section 11), fallback step. Called
 * only after px_live_apply_restart() has exhausted its retries - restores
 * the stream without the codec change rather than leaving it dead: resubmit
 * capture/feedback/playback in the normal start order, same as
 * px_live_apply_restart()'s own tail, but with no codec/rate/status control
 * transfers at all, so the device keeps whatever was last genuinely
 * applied. live_fail_step==5 is checked here too (not just in
 * px_live_apply_restart()) so a single fault-injection value can drive the
 * "fallback also fails" scenario end to end - see that param's comment.
 *
 * Caller holds px->mutex and has already verified px->running and
 * !px->disconnected, same as px_live_apply_restart().
 */
static int px_live_apply_fallback(struct px_dev *px)
{
	unsigned long flags;
	const char *unused_step;
	int err, i, serr;

	/* Addendum (2026-10-17, review round 3): checked before the one
	 * potentially-slow step in this function (clear_halt, up to ~5s -
	 * see analysis/v0.1.12-plan.md section 12) - no out_step parameter
	 * here (unlike px_live_apply_restart()), so px_live_apply_work()'s
	 * own dev_warn() covers this call generically; unused_step just
	 * satisfies px_live_apply_check_stopping()'s signature.
	 */
	if (px_live_apply_check_stopping(px, &unused_step)) {
		dev_warn(&px->udev->dev, "live apply fallback: stream stopping, not resubmitting\n");
		return -ESHUTDOWN;
	}
	msleep(CLEAR_HALT_DELAY_MS);
	serr = usb_clear_halt(px->udev, usb_rcvbulkpipe(px->udev, EP_CAPTURE));
	if (serr < 0)
		dev_warn(&px->udev->dev, "live apply fallback: clear_halt failed: %d\n", serr);

	/* Addendum (2026-10-17, hardware hang fix): same re-check as
	 * px_live_apply_restart()'s - this is reached only after 1-3 failed
	 * attempts of that function, each with its own settle/backoff, so
	 * "verified at worker entry" (this function's own comment) is long
	 * since stale by the time we get here. live_apply_stopping folded in
	 * (review round 3) - running can stay true while a sync_stop is
	 * actively in progress.
	 */
	if (!px->running || px_live_apply_check_stopping(px, &unused_step)) {
		dev_warn(&px->udev->dev, "live apply fallback: stream stopping, not resubmitting\n");
		return -ESHUTDOWN;
	}
	if (live_fail_step == 5) {
		dev_warn(&px->udev->dev, "live apply fallback: fault injection - forcing resubmit to fail\n");
		err = -EIO;
		goto fail;
	}
	/* v0.1.12 phase 3b: same handover reasoning as
	 * px_live_apply_restart()'s identical call - stop the filler
	 * synchronously right before the first real URB goes back in
	 * flight.
	 */
	px_fill_stop(px);
	for (i = 0; i < CAP_URBS; i++) {
		err = usb_submit_urb(px->cap_urb[i], GFP_KERNEL);
		if (err < 0)
			goto fail;
	}
	for (i = 0; i < FB_URBS; i++) {
		err = usb_submit_urb(px->fb_urb[i], GFP_KERNEL);
		if (err < 0)
			goto fail;
	}
	{
		/* Addendum (2026-10-19, review): does the prefill below
		 * double-count against the timer+catch-up's own advance?
		 * Diagnostic only - see the dev_dbg after the loop.
		 */
		unsigned int play_hwptr_before_prefill = px->play.hwptr;
		unsigned int prefill_frames;

		for (i = 0; i < PLAY_URBS; i++) {
			px_play_notify_elapsed(px, px_fill_play_urb(px, px->play_urb[i]));
			err = usb_submit_urb(px->play_urb[i], GFP_KERNEL);
			if (err < 0)
				goto fail;
		}
		prefill_frames = px->play.hwptr >= play_hwptr_before_prefill ?
			px->play.hwptr - play_hwptr_before_prefill :
			(px->play.sub && px->play.sub->runtime ?
			 px->play.sub->runtime->buffer_size : 0) -
			play_hwptr_before_prefill + px->play.hwptr;
		/* Addendum (2026-10-19, review): the actual fix - see
		 * px->play_prefill_frames's own comment. Consumed by
		 * px_play_complete()'s catch-up call.
		 */
		px->play_prefill_frames = prefill_frames;
		dev_dbg(&px->udev->dev,
			"live apply: play prefill %u frames (hwptr %u -> %u), "
			"timer play_frames=%u\n",
			prefill_frames, play_hwptr_before_prefill, px->play.hwptr,
			px->fill_play_frames);
	}
	spin_lock_irqsave(&px->desired_lock, flags);
	px->urbs_live = true;
	spin_unlock_irqrestore(&px->desired_lock, flags);
	dev_warn(&px->udev->dev,
		"live apply: codec change not applied after %d attempts, "
		"stream restored with the previous codec state\n",
		LIVE_APPLY_MAX_RETRIES + 1);
	return 0;

fail:
	/* urbs_live is already false (never set true on this path) -
	 * px_start_streaming()'s own guard will redo a real start rather
	 * than trust px->running alone.
	 *
	 * Addendum (2026-10-19, review item 2): deliberately no
	 * play_fill_carry tracking here, unlike px_live_apply_restart()'s
	 * identical-looking fail: path. This function is always the last
	 * attempt (px_live_apply_work() calls it exactly once, after every
	 * restart retry is exhausted) - a failure here goes straight to
	 * px_live_apply_give_up(), never another px_fill_start(), so there
	 * is no "next attempt" to carry a partial prefill forward to; any
	 * phantom play.hwptr advance from a partial prefill here is moot,
	 * since the stream is being handed to the core's own XRUN recovery
	 * and the next genuine start goes through px_pcm_prepare(), which
	 * resets play.hwptr fresh regardless. give_up() clears
	 * play_fill_carry defensively anyway.
	 */
	dev_err(&px->udev->dev, "live apply fallback: resubmit failed: %d\n", err);
	px_kill_audio_urbs(px);
	return err;
}

/* Addendum (2026-10-17, lock-ordering review, superseding the 2026-10-16
 * version of this function): no longer calls into the ALSA PCM core at
 * all. The previous version called snd_pcm_stop_xrun() here, from inside
 * the live-apply worker (px->mutex held, running as delayed_work) - that
 * goes through the PCM core's action framework, which upstream's own
 * nonatomic-action path (prepare/hw_params/hw_free - see
 * snd_pcm_action_nonatomic()) holds a group-wide lock across for its
 * entire duration. px_pcm_sync_stop()/px_stop_streaming() both
 * cancel_delayed_work_sync() this same worker - if a concurrent
 * prepare()/hw_free() (on this substream or one linked/grouped with it,
 * e.g. REAPER's usual capture+playback link) was holding that lock while
 * waiting for this worker via that cancel, and this worker's own
 * snd_pcm_stop_xrun() call needed the same lock, that is a genuine
 * deadlock cycle - not ruled out, and not something this review could
 * verify safe against this kernel's exact source. Removed rather than
 * risked. Never fail silently is kept (log loudly), and the recovery
 * trigger is now active again, but without the lock-ordering risk
 * (addendum, 2026-10-17, review round 3): sets px->give_up, which
 * px_pcm_pointer() turns into SNDRV_PCM_POS_XRUN - the documented,
 * ALSA-core-sanctioned way for a .pointer op to report XRUN. This is not
 * this driver calling back into the PCM core; it's the PCM core's own
 * snd_pcm_update_hw_ptr0() (reached from wherever it next polls this
 * substream's position - a period timer, a later read()/write(), or the
 * application's own poll() wakeup) noticing the sentinel and doing its
 * usual XRUN handling, entirely on its own schedule, under its own
 * locking - this driver's worker thread has already returned by the
 * time any of that runs. No audio URBs are outstanding at this point
 * (both the restart and the fallback kill them on their own failure
 * paths before returning) and none will be resubmitted until a fresh
 * px_start_streaming() clears give_up - px_cap_watchdog_fn() also checks
 * it, so its own independent stall-recovery resubmission can't race this
 * state either.
 */
static void px_live_apply_give_up(struct px_dev *px)
{
	unsigned long flags;

	dev_err(&px->udev->dev,
		"live apply: could not restore the stream after the codec-free "
		"fallback also failed - marking the stream failed so the "
		"application's own XRUN handling takes over, no URBs will be "
		"resubmitted until the next prepare/start "
		"(see analysis/v0.1.12-plan.md section 12)\n");
	/* v0.1.12 phase 3b: stop the filler - give_up hands the stream's
	 * recovery to the core's own SNDRV_PCM_POS_XRUN handling
	 * (px_pcm_pointer()), which must not be papered over by a timer
	 * that keeps seamlessly advancing position as if nothing failed.
	 */
	px_fill_stop(px);
	/* Addendum (2026-10-19, review item 2): belt-and-braces - no current
	 * path leaves play_fill_carry nonzero here (px_live_apply_restart()'s
	 * own fail: only ever sets it on a retry, never on the path that
	 * leads here, and fallback()'s fail: deliberately never sets it -
	 * see that function's comment), but clear it anyway so a stray value
	 * can never bleed into a future, unrelated stream's first live-apply
	 * restart.
	 */
	px->play_fill_carry = 0;
	spin_lock_irqsave(&px->desired_lock, flags);
	px->give_up = true;
	spin_unlock_irqrestore(&px->desired_lock, flags);
}

/*
 * v0.1.12 live codec apply worker. Hardware-tested since v0.1.12, see
 * analysis/v0.1.12-plan.md section 17.
 *
 * mutex_trylock(), never mutex_lock(): the exact same reasoning as
 * px_cap_watchdog_fn() (see that function's comment) - this must never
 * block waiting for px->mutex, or cancel_delayed_work_sync() called
 * while holding that mutex (px_stop_streaming()) could form a wait
 * cycle. On a lost trylock, re-arm a short retry rather than waiting;
 * desired_gen makes this idempotent regardless of ordering.
 *
 * Addendum (2026-10-16, v0.1.12-plan.md section 11): once the trylock
 * succeeds, the whole retry/fallback/XRUN sequence below runs under that
 * single, continuously-held px->mutex - not re-acquired per attempt. This
 * is the same trade-off px_start_streaming() already makes by holding the
 * mutex across its own settle sleeps: a concurrent close()/stop() blocks on
 * mutex_lock() for the (bounded) duration of this sequence rather than
 * racing it, and cancel_delayed_work_sync() in px_stop_streaming() only
 * ever runs after that mutex is free, so it cancels an instance that has
 * either not started yet or has already fully finished - never one it
 * would have to wait inside the mutex for.
 */
static void px_live_apply_work(struct work_struct *work)
{
	struct px_dev *px = container_of(to_delayed_work(work), struct px_dev,
					 live_apply_work);
	u16 snapshot[2][CODEC_REGS];
	unsigned long flags;
	unsigned int gen;
	bool requeue, device_gone = false;
	const char *step;
	int err = 0, attempt;

	if (!mutex_trylock(&px->mutex)) {
		schedule_delayed_work(&px->live_apply_work,
				      msecs_to_jiffies(LIVE_APPLY_RETRY_MS));
		return;
	}

	if (px->disconnected || !px->running) {
		/* Nothing to do: idle-state puts already applied themselves
		 * synchronously, and a disconnect/stop mid-flight means this
		 * generation no longer matters - desired_codec stays staged
		 * for nothing in particular; the next real start resyncs
		 * from px->codec via px_codec_resync_shadow(), same as any
		 * other idle-to-running transition.
		 */
		mutex_unlock(&px->mutex);
		return;
	}

	spin_lock_irqsave(&px->desired_lock, flags);
	gen = px->desired_gen;
	memcpy(snapshot, px->desired_codec, sizeof(snapshot));
	spin_unlock_irqrestore(&px->desired_lock, flags);

	if (gen == px->live_applied_gen) {
		mutex_unlock(&px->mutex);
		return;		/* already applied, nothing new since */
	}

	for (attempt = 0; attempt <= LIVE_APPLY_MAX_RETRIES; attempt++) {
		step = NULL;
		err = px_live_apply_restart(px, snapshot, &step);
		if (!err)
			break;
		device_gone = px_is_device_gone(err);
		dev_warn(&px->udev->dev,
			"live apply: attempt %d/%d failed at %s: %d%s\n",
			attempt + 1, LIVE_APPLY_MAX_RETRIES + 1,
			step ? step : "(unknown step)", err,
			/* Addendum (2026-10-17, review round 5): -ESHUTDOWN
			 * here is always this driver's own "stream stopping"
			 * abort (px_live_apply_check_stopping() and the
			 * pre-resubmit running checks both use it
			 * deliberately, never a real USB error) - say so
			 * distinctly from an actual -ENODEV disconnect,
			 * instead of folding both into "device gone".
			 */
			err == -ESHUTDOWN ? " - not retrying (stream stopping, not a disconnect)" :
			device_gone ? " - device gone, not retrying" :
			attempt < LIVE_APPLY_MAX_RETRIES ? " - retrying" :
							   " - giving up, falling back");
		if (device_gone)
			break;
		if (attempt < LIVE_APPLY_MAX_RETRIES)
			msleep(live_apply_backoff_ms[attempt]);
	}

	if (!err) {
		spin_lock_irqsave(&px->desired_lock, flags);
		px->live_applied_gen = gen;
		spin_unlock_irqrestore(&px->desired_lock, flags);
		/* Pairs with the "staged gen=" traces in px_switch_put()/
		 * px_adc_source_put() - same gen number, so a test session can
		 * compute the stage-to-commit gap per toggle from dmesg
		 * timestamps alone. in_elapsed + totals (review round 5):
		 * same reasoning as the staged traces.
		 */
		dev_dbg(&px->udev->dev,
			"live apply: committed gen=%u cap.in_elapsed=%d (inc=%d dec=%d) "
			"play.in_elapsed=%d (inc=%d dec=%d)\n",
			gen, atomic_read(&px->cap.in_elapsed),
			atomic_read(&px->cap.in_elapsed_inc_total),
			atomic_read(&px->cap.in_elapsed_dec_total),
			atomic_read(&px->play.in_elapsed),
			atomic_read(&px->play.in_elapsed_inc_total),
			atomic_read(&px->play.in_elapsed_dec_total));
		/* Addendum (2026-10-17, review round 5): if either stream's
		 * in_elapsed is still nonzero right after a commit, that is
		 * itself the leak signature this round's bug produced (one
		 * inc_total ahead of dec_total, never catching up) - flag it
		 * immediately rather than waiting for sync_stop's own
		 * timeout to notice at the next close, ~100s later in the
		 * incident that found this. Ratelimited: must never itself
		 * flood if it did start happening again for some other
		 * reason.
		 */
		if (atomic_read(&px->cap.in_elapsed) != 0 || atomic_read(&px->play.in_elapsed) != 0)
			dev_warn_ratelimited(&px->udev->dev,
				"live apply: commit gen=%u left in_elapsed nonzero - "
				"cap=%d (inc=%d dec=%d) play=%d (inc=%d dec=%d), "
				"investigate before the next sync_stop times out\n",
				gen, atomic_read(&px->cap.in_elapsed),
				atomic_read(&px->cap.in_elapsed_inc_total),
				atomic_read(&px->cap.in_elapsed_dec_total),
				atomic_read(&px->play.in_elapsed),
				atomic_read(&px->play.in_elapsed_inc_total),
				atomic_read(&px->play.in_elapsed_dec_total));
	} else if (!device_gone) {
		int ferr = px_live_apply_fallback(px);

		if (ferr < 0) {
			device_gone = px_is_device_gone(ferr);
			if (!device_gone) {
				dev_err(&px->udev->dev,
					"live apply: fallback also failed (%d)\n", ferr);
				px_live_apply_give_up(px);
			}
		}
	}
	/* device_gone (either stage): px_disconnect()'s own belt-and-braces
	 * cancel_delayed_work_sync() owns teardown from here - no fallback
	 * attempt, no XRUN call, nothing further from this function.
	 */

	spin_lock_irqsave(&px->desired_lock, flags);
	/* Coalescing fence (design doc section 3): if desired_gen moved
	 * again while the sequence above was running, re-queue once for
	 * the latest snapshot instead of per intermediate edit - whatever
	 * changed mid-sequence was never actually applied, regardless of
	 * how the sequence above ended. Never requeue once the device is
	 * known gone, successful or not.
	 */
	requeue = !device_gone && px->desired_gen != gen;
	spin_unlock_irqrestore(&px->desired_lock, flags);

	mutex_unlock(&px->mutex);

	if (requeue)
		schedule_delayed_work(&px->live_apply_work,
				      msecs_to_jiffies(LIVE_APPLY_DEBOUNCE_MS));
}

/* ---------------------------------------------------------------------- */
/* PCM */

static const struct snd_pcm_hardware px_play_hw = {
	.info = SNDRV_PCM_INFO_MMAP | SNDRV_PCM_INFO_MMAP_VALID |
		SNDRV_PCM_INFO_INTERLEAVED | SNDRV_PCM_INFO_BLOCK_TRANSFER |
		SNDRV_PCM_INFO_BATCH,
	.formats = SNDRV_PCM_FMTBIT_S24_3LE,
	.rates = SNDRV_PCM_RATE_44100 | SNDRV_PCM_RATE_48000 |
		 SNDRV_PCM_RATE_88200 | SNDRV_PCM_RATE_96000,
	.rate_min = 44100,
	.rate_max = 96000,
	.channels_min = PLAY_CHANNELS,
	.channels_max = PLAY_CHANNELS,
	.buffer_bytes_max = 1024 * 1024,
	.period_bytes_min = 32 * PLAY_FRAME_BYTES,
	.period_bytes_max = 512 * 1024,
	.periods_min = 2,
	.periods_max = 1024,
};

static const struct snd_pcm_hardware px_cap_hw = {
	.info = SNDRV_PCM_INFO_MMAP | SNDRV_PCM_INFO_MMAP_VALID |
		SNDRV_PCM_INFO_INTERLEAVED | SNDRV_PCM_INFO_BLOCK_TRANSFER |
		SNDRV_PCM_INFO_BATCH,
	.formats = SNDRV_PCM_FMTBIT_S32_LE,
	.rates = SNDRV_PCM_RATE_44100 | SNDRV_PCM_RATE_48000 |
		 SNDRV_PCM_RATE_88200 | SNDRV_PCM_RATE_96000,
	.rate_min = 44100,
	.rate_max = 96000,
	.channels_min = CAP_CHANNELS,
	.channels_max = CAP_CHANNELS,
	.buffer_bytes_max = 1024 * 1024,
	.period_bytes_min = 32 * CAP_FRAME_BYTES,
	.period_bytes_max = 512 * 1024,
	.periods_min = 2,
	.periods_max = 1024,
};

static struct px_stream *px_stream_of(struct px_dev *px,
				      struct snd_pcm_substream *sub)
{
	return sub->stream == SNDRV_PCM_STREAM_PLAYBACK ? &px->play : &px->cap;
}

static int px_pcm_open(struct snd_pcm_substream *sub)
{
	struct px_dev *px = snd_pcm_substream_chip(sub);
	struct snd_pcm_runtime *rt = sub->runtime;
	struct px_stream *s = px_stream_of(px, sub);
	unsigned long flags;
	int err = 0;

	if (sub->stream == SNDRV_PCM_STREAM_PLAYBACK) {
		rt->hw = px_play_hw;
	} else {
		rt->hw = px_cap_hw;
		err = snd_pcm_hw_constraint_msbits(rt, 0, 32, 24);
		if (err < 0)
			return err;
	}

	/* both directions share one clock */
	mutex_lock(&px->mutex);
	if (px->running) {
		/*
		 * snd_pcm_hw_constraint_single() returns positive when it
		 * narrowed the interval, zero when it was already a single
		 * value, negative on error - only negative is a failure.
		 * Treating "positive" as failure here used to skip
		 * s->sub = sub below on every open of the second direction
		 * during normal full-duplex use, which left active=true
		 * (set later by trigger) with sub still NULL - see
		 * tests/hw/REPORT.md.
		 */
		err = snd_pcm_hw_constraint_single(rt, SNDRV_PCM_HW_PARAM_RATE,
						   px->rate);
		if (err < 0) {
			mutex_unlock(&px->mutex);
			dev_dbg(&px->udev->dev, "pcm open: %s err=%d\n",
				snd_pcm_stream_str(sub), err);
			return err;
		}
		err = 0;
	} else {
		/*
		 * Not yet streaming: if the digital input is already locked,
		 * the device's real rate follows that external clock
		 * regardless of what gets negotiated below (see
		 * tests/hw/REPORT.md "Windows USB trace" / "Confirmed without
		 * the S/PDIF cable") - constrain the rate to the one value
		 * that will actually work, whether this is a deliberate
		 * digital-clock setup or an unexpected lock while internal
		 * clock is selected (worth a warning either way: silently
		 * capturing at the wrong speed is exactly what this avoids).
		 * A live read failing here just leaves the full rate table in
		 * place - px_start_streaming()'s own GET_CUR verify is the
		 * backstop if the lock engages after this point.
		 */
		u8 status;

		if (px_status_read(px, &status) == 0 &&
		    (status & STATUS_DIGITAL_LOCK)) {
			unsigned int live_rate;

			if (px_get_rate_ep(px, 0, &live_rate, NULL) == 0) {
				err = snd_pcm_hw_constraint_single(rt,
						SNDRV_PCM_HW_PARAM_RATE, live_rate);
				if (err < 0) {
					mutex_unlock(&px->mutex);
					return err;
				}
				err = 0;
				if (px->clock_internal)
					dev_warn(&px->udev->dev,
						"digital input is locked at %u Hz "
						"despite internal clock selected - "
						"constraining the rate to %u Hz\n",
						live_rate, live_rate);
			}
		}
	}
	spin_lock_irqsave(&s->lock, flags);
	s->sub = sub;
	spin_unlock_irqrestore(&s->lock, flags);
	mutex_unlock(&px->mutex);
	dev_dbg(&px->udev->dev, "pcm open: %s err=%d\n",
		snd_pcm_stream_str(sub), err);
	return 0;
}

static int px_pcm_close(struct snd_pcm_substream *sub)
{
	struct px_dev *px = snd_pcm_substream_chip(sub);
	struct px_stream *s = px_stream_of(px, sub);
	unsigned long flags;

	mutex_lock(&px->mutex);
	s->prepared = false;
	/*
	 * active and sub must go together: a completion handler that has
	 * already taken s->lock may still be checking active right now, and
	 * it must see either "active, valid sub" or "inactive" - never
	 * "active, NULL sub" (that NULL deref is what used to panic here).
	 */
	spin_lock_irqsave(&s->lock, flags);
	s->active = false;
	s->sub = NULL;
	spin_unlock_irqrestore(&s->lock, flags);
	if (!px->play.prepared && !px->cap.prepared)
		px_stop_streaming(px);
	mutex_unlock(&px->mutex);
	dev_dbg(&px->udev->dev, "pcm close: %s\n", snd_pcm_stream_str(sub));
	return 0;
}

static int px_pcm_hw_params(struct snd_pcm_substream *sub,
			    struct snd_pcm_hw_params *hw)
{
	struct px_dev *px = snd_pcm_substream_chip(sub);
	struct px_stream *s = px_stream_of(px, sub);
	struct px_stream *other = s == &px->play ? &px->cap : &px->play;
	int err = 0;

	mutex_lock(&px->mutex);
	if (px->running && px->rate != params_rate(hw)) {
		if (other->prepared)
			err = -EBUSY;
		else
			px_stop_streaming(px);
	}
	mutex_unlock(&px->mutex);
	return err;
}

static int px_pcm_hw_free(struct snd_pcm_substream *sub)
{
	struct px_dev *px = snd_pcm_substream_chip(sub);
	struct px_stream *s = px_stream_of(px, sub);
	unsigned long flags;

	mutex_lock(&px->mutex);
	s->prepared = false;
	/*
	 * hw_free() returns the substream to OPEN state; it may be followed
	 * by hw_params()+prepare() again on the *same* sub (no close/open in
	 * between), so sub itself must stay valid here - only close() may
	 * NULL it. But the DMA buffer can be freed right after this call,
	 * so active must be false before we return, or a completion still
	 * in flight (because the other duplex side kept the URBs alive)
	 * could copy into a buffer that is being torn down.
	 */
	spin_lock_irqsave(&s->lock, flags);
	s->active = false;
	spin_unlock_irqrestore(&s->lock, flags);
	if (!px->play.prepared && !px->cap.prepared)
		px_stop_streaming(px);
	mutex_unlock(&px->mutex);
	dev_dbg(&px->udev->dev, "pcm hw_free: %s\n", snd_pcm_stream_str(sub));
	return 0;
}

static int px_pcm_prepare(struct snd_pcm_substream *sub)
{
	struct px_dev *px = snd_pcm_substream_chip(sub);
	struct px_stream *s = px_stream_of(px, sub);
	unsigned long flags;
	int err;

	mutex_lock(&px->mutex);
	spin_lock_irqsave(&s->lock, flags);
	s->active = false;
	s->hwptr = 0;
	s->period_pos = 0;
	spin_unlock_irqrestore(&s->lock, flags);

	err = px_start_streaming(px, sub->runtime->rate);
	if (!err)
		s->prepared = true;
	mutex_unlock(&px->mutex);
	dev_dbg(&px->udev->dev, "pcm prepare: %s rate=%u err=%d\n",
		snd_pcm_stream_str(sub), sub->runtime->rate, err);
	return err;
}

static int px_pcm_trigger(struct snd_pcm_substream *sub, int cmd)
{
	struct px_dev *px = snd_pcm_substream_chip(sub);
	struct px_stream *s = px_stream_of(px, sub);
	unsigned long flags;

	dev_dbg(&px->udev->dev, "pcm trigger: %s cmd=%d\n",
		snd_pcm_stream_str(sub), cmd);

	switch (cmd) {
	case SNDRV_PCM_TRIGGER_START:
	case SNDRV_PCM_TRIGGER_PAUSE_RELEASE:
		if (!px->running)
			return -EIO;
		spin_lock_irqsave(&s->lock, flags);
		/*
		 * Defense in depth: open() is the only place that sets
		 * s->sub, and it must match this substream before we ever
		 * let active become true - a mismatch (e.g. a future open()
		 * bug leaving sub NULL or stale) must never again silently
		 * turn into active=true with a bad sub, which is what used
		 * to NULL-deref in completions. See tests/hw/REPORT.md.
		 */
		if (s->sub != sub) {
			spin_unlock_irqrestore(&s->lock, flags);
			dev_err(&px->udev->dev,
				"pcm trigger start: %s sub mismatch (got %p, expected %p), refusing\n",
				snd_pcm_stream_str(sub), s->sub, sub);
			return -EIO;
		}
		if (sub->stream == SNDRV_PCM_STREAM_CAPTURE &&
		    cmd == SNDRV_PCM_TRIGGER_START)
			px_riaa_reset(px);
		/* A fresh start must not show a peak left over from before -
		 * same principle as the RIAA reset above, same "START only,
		 * not PAUSE_RELEASE" scope (a resumed pause keeps showing
		 * its last reading, like RIAA keeps its filter state).
		 */
		if (cmd == SNDRV_PCM_TRIGGER_START) {
			if (sub->stream == SNDRV_PCM_STREAM_CAPTURE)
				memset(px->peak_cap, 0, sizeof(px->peak_cap));
			else
				memset(px->peak_play, 0, sizeof(px->peak_play));
		}
		s->active = true;
		spin_unlock_irqrestore(&s->lock, flags);
		/* arm the capture watchdog; schedule_delayed_work() is safe
		 * under the stream lock trigger() runs with (atomic context).
		 */
		if (sub->stream == SNDRV_PCM_STREAM_CAPTURE) {
			atomic_set(&px->cap_complete_count, 0);
			spin_lock_irqsave(&s->lock, flags);
			px->cap_last_complete_jiffies = jiffies;
			px->cap_wd_attempt = 0;
			px->cap_wd_gave_up = false;
			px->cap_sum_jiffies = 0;
			px->cap_sum_completions = 0;
			px->cap_sum_bytes = 0;
			px->cap_sum_frames = 0;
			spin_unlock_irqrestore(&s->lock, flags);
			/*
			 * Bump the generation only - cap_wd_attempt/
			 * cap_wd_gave_up are owned exclusively by the watchdog
			 * itself now (see the struct px_dev comment), to avoid
			 * racing against its own state transitions if an old
			 * stall episode is still mid-recovery when a new
			 * stream starts.
			 */
			atomic_inc(&px->cap_wd_gen);
			schedule_delayed_work(&px->cap_watchdog_work,
					      msecs_to_jiffies(CAP_WD_CHECK_MS));
		}
		return 0;
	case SNDRV_PCM_TRIGGER_STOP:
	case SNDRV_PCM_TRIGGER_PAUSE_PUSH:
	case SNDRV_PCM_TRIGGER_SUSPEND:
		spin_lock_irqsave(&s->lock, flags);
		s->active = false;
		/* Meter must read 0 immediately, not just "whenever the next
		 * start happens to reset it".
		 */
		if (sub->stream == SNDRV_PCM_STREAM_CAPTURE)
			memset(px->peak_cap, 0, sizeof(px->peak_cap));
		else
			memset(px->peak_play, 0, sizeof(px->peak_play));
		spin_unlock_irqrestore(&s->lock, flags);
		/* Best-effort only: cancel_delayed_work() (not _sync) is the
		 * atomic-safe variant, since trigger() itself runs in atomic
		 * context. This just stops new ticks a little sooner; the
		 * guaranteed, final cancellation is cancel_delayed_work_sync()
		 * in px_stop_streaming() (process context), which the
		 * eventual hw_free()/close() always reaches regardless of
		 * what happens here.
		 */
		if (sub->stream == SNDRV_PCM_STREAM_CAPTURE)
			cancel_delayed_work(&px->cap_watchdog_work);
		return 0;
	}
	return -EINVAL;
}

static snd_pcm_uframes_t px_pcm_pointer(struct snd_pcm_substream *sub)
{
	struct px_dev *px = snd_pcm_substream_chip(sub);
	unsigned long flags;
	bool gave_up;

	/* Addendum (2026-10-17, review round 3): px_live_apply_give_up()'s
	 * own replacement for the removed snd_pcm_stop_xrun() call - see its
	 * comment. SNDRV_PCM_POS_XRUN is the documented sentinel a .pointer
	 * op returns to tell the core this substream is stuck; the core's
	 * own position-update path takes it from here.
	 */
	spin_lock_irqsave(&px->desired_lock, flags);
	gave_up = px->give_up;
	spin_unlock_irqrestore(&px->desired_lock, flags);
	if (gave_up)
		return SNDRV_PCM_POS_XRUN;

	return READ_ONCE(px_stream_of(px, sub)->hwptr);
}

/* Addendum (2026-10-17, review): shared diagnostic snapshot for
 * px_pcm_sync_stop(), at entry (dev_dbg) and on its timeout/kill path
 * (dev_warn_ratelimited, called separately below) - so a test session can
 * see *why* in_elapsed stayed non-zero or why N generations happened,
 * not just that it did. Reads px->cap.active/px->play.active without
 * s->lock (a best-effort snapshot for logging, not used for any decision
 * here) and the capture-only URB activity counters px_cap_watchdog_fn()
 * already maintains - there is no playback equivalent of
 * last_cap_urb_status/actual_length to report (capture has it because the
 * watchdog needed it first; not added symmetrically for playback here).
 */
static void px_sync_stop_diag(struct px_dev *px, struct snd_pcm_substream *sub,
			      const char *fmt_prefix)
{
	unsigned long flags;
	unsigned int gen, applied_gen;
	bool urbs_live;

	spin_lock_irqsave(&px->desired_lock, flags);
	gen = px->desired_gen;
	applied_gen = px->live_applied_gen;
	urbs_live = px->urbs_live;
	spin_unlock_irqrestore(&px->desired_lock, flags);

	dev_dbg(&px->udev->dev,
		"%s: stream=%s cap.in_elapsed=%d (inc=%d dec=%d) play.in_elapsed=%d "
		"(inc=%d dec=%d) cap.active=%d play.active=%d running=%d "
		"urbs_live=%d desired_gen=%u live_applied_gen=%u "
		"cap_complete_count=%d last_cap_urb(status=%d actual_length=%u)\n",
		fmt_prefix, snd_pcm_stream_str(sub), atomic_read(&px->cap.in_elapsed),
		atomic_read(&px->cap.in_elapsed_inc_total),
		atomic_read(&px->cap.in_elapsed_dec_total),
		atomic_read(&px->play.in_elapsed),
		atomic_read(&px->play.in_elapsed_inc_total),
		atomic_read(&px->play.in_elapsed_dec_total),
		px->cap.active, px->play.active,
		px->running, urbs_live, gen, applied_gen,
		atomic_read(&px->cap_complete_count), px->last_cap_urb_status,
		px->last_cap_urb_actual_length);
}

/*
 * Called by the PCM core after trigger(STOP) and before hw_free()/close(),
 * *and* before prepare() when the core is recovering a substream from
 * XRUN (snd_pcm_do_prepare() calls sync_stop first) - a hardware test
 * (2026-10-16) hit exactly that path and hung here indefinitely. Process
 * context (may sleep). trigger(STOP) has already made active false under
 * s->lock, so no completion can start a *new* snd_pcm_period_elapsed()
 * call for this stream from this point on; this just waits out whichever
 * call(s) were already in flight, so hw_free()/a prepare-triggered
 * hw_params cycle cannot free the runtime out from under them.
 *
 * Addendum (2026-10-17, hardware hang fix + lock-ordering review): two
 * layers, not one -
 *
 * 1. cancel_delayed_work_sync(&px->live_apply_work) first. sync_stop is
 *    one of the exact synchronization points the kernel's own sync_stop
 *    contract exists for (prepare/hw_free/hw_params, see above) - "make
 *    sure nothing else is still touching this stream" is precisely what
 *    it's for, and a live-apply restart concurrently killing/resubmitting
 *    URBs is exactly that kind of in-flight activity. Provably safe, not
 *    just believed safe: the live-apply worker's entire lock footprint is
 *    px->mutex (mutex_trylock() only, never blocks - see
 *    px_live_apply_work()'s own comment), desired_lock (a brief spinlock,
 *    never nested with px->mutex or any stream lock), and plain USB-core
 *    calls (usb_kill_urb/usb_submit_urb/usb_control_msg_send/recv/
 *    usb_clear_halt).
 *    It no longer calls into the ALSA PCM core at all - the one place it
 *    did (snd_pcm_stop_xrun(), in the now-removed px_live_apply_xrun())
 *    was removed specifically because snd_pcm_action_nonatomic() (which
 *    prepare/hw_params/hw_free all go through) holds a group-wide lock
 *    across its entire action, and this worker being cancelled from
 *    exactly that kind of action while itself trying to re-enter the PCM
 *    core would have been a genuine cross-subsystem deadlock risk - see
 *    analysis/v0.1.12-plan.md section 12 for the full reasoning. With
 *    that call gone, the worker cannot be waiting on anything a
 *    sync_stop caller could be holding, full stop - the same invariant
 *    px_stop_streaming()'s existing cancellation already relied on, just
 *    now actually provable, and reached from an earlier point in the
 *    teardown sequence than hw_free().
 * 2. wait_event_killable_timeout(), not plain wait_event(), on
 *    in_elapsed - bounded *and* wakeable by a fatal signal, regardless of
 *    (1) above actually eliminating every way this could get stuck, per
 *    "never block indefinitely" rather than per how confident this fix
 *    is in its own completeness (see the worst-case worker runtime bound
 *    in analysis/v0.1.12-plan.md section 12, which this timeout is
 *    generous against, not tight to). On timeout or a fatal signal:
 *    force-kill every audio URB (not just this stream's - cap+feedback+
 *    playback share retriggering), reset in_elapsed to the only value a
 *    stream with no outstanding URBs can honestly have, clear
 *    px->urbs_live so px_start_streaming()'s own guard knows to redo a
 *    real start rather than trust px->running alone, and warn
 *    (ratelimited - this path firing at all, even once, is itself
 *    noteworthy, but must never itself become a flood) so this is never
 *    silent.
 */
static int px_pcm_sync_stop(struct snd_pcm_substream *sub)
{
	struct px_dev *px = snd_pcm_substream_chip(sub);
	struct px_stream *s = px_stream_of(px, sub);
	unsigned long flags;
	long left;

	px_sync_stop_diag(px, sub, "sync_stop entry");

	/* v0.1.12 phase 3b: stop the filler synchronously before anything
	 * else below, per the task's own requirement - every stop path
	 * cancels it before waiting on in_elapsed. Idempotent/safe even if
	 * it was never running (e.g. no live-apply restart was in flight).
	 */
	px_fill_stop(px);

	/* Addendum (2026-10-17, review round 3): set before the cancel, not
	 * just relying on cancel_delayed_work_sync() to eventually win the
	 * race - the worker checks this between every sleep/control-transfer/
	 * retry (px_live_apply_check_stopping()) and aborts with -ESHUTDOWN
	 * as soon as it sees it, instead of running its full worst-case
	 * sequence (~92s - analysis/v0.1.12-plan.md section 12 "Review round
	 * 2") before this cancel can return. A refcount, not a bool: see
	 * px->live_apply_stopping's own comment for why (overlapping
	 * sync_stop/stop_streaming calls on unlinked substreams).
	 */
	atomic_inc(&px->live_apply_stopping);
	cancel_delayed_work_sync(&px->live_apply_work);
	atomic_dec(&px->live_apply_stopping);

	/* _killable, not plain _timeout: a fatal signal (SIGKILL, including
	 * the group-exit every thread of a coredumping process receives)
	 * wakes a TASK_KILLABLE wait same as it would TASK_INTERRUPTIBLE -
	 * TASK_UNINTERRUPTIBLE (plain wait_event()) is the one state that
	 * ignores even SIGKILL, and is what actually produced the hung-task
	 * warning this addendum exists to fix: the crashing process's own
	 * coredump could not wake this thread to let the crash finish
	 * cleanly, on top of timing out never being possible at all. Timeout
	 * and kill both land in the same recovery action below - either way
	 * the stream must end up with no outstanding URBs.
	 */
	left = wait_event_killable_timeout(s->elapsed_wq, atomic_read(&s->in_elapsed) == 0,
					   msecs_to_jiffies(SYNC_STOP_TIMEOUT_MS));
	if (left <= 0) {
		px_sync_stop_diag(px, sub, "sync_stop timeout/kill");
		dev_warn_ratelimited(&px->udev->dev,
			"sync_stop: %s in_elapsed stuck at %d, %s after %dms - "
			"forcing URB teardown instead of waiting indefinitely\n",
			snd_pcm_stream_str(sub), atomic_read(&s->in_elapsed),
			left < 0 ? "interrupted by a fatal signal" : "timed out",
			SYNC_STOP_TIMEOUT_MS);
		px_kill_audio_urbs(px);
		spin_lock_irqsave(&px->desired_lock, flags);
		px->urbs_live = false;
		spin_unlock_irqrestore(&px->desired_lock, flags);
		atomic_set(&s->in_elapsed, 0);
	}
	return 0;
}

static const struct snd_pcm_ops px_pcm_ops = {
	.open = px_pcm_open,
	.close = px_pcm_close,
	.hw_params = px_pcm_hw_params,
	.hw_free = px_pcm_hw_free,
	.prepare = px_pcm_prepare,
	.trigger = px_pcm_trigger,
	.sync_stop = px_pcm_sync_stop,
	.pointer = px_pcm_pointer,
};

/* ---------------------------------------------------------------------- */
/* mixer: the buttons of the Windows control panel */

/*
 * The ADC input mux (WM_ADC_MUX) is a single-select source per channel
 * pair, not a set of independent bits: the Windows panel presents it as a
 * radio group (CDlgGroupRadioButtonIterator in CPLPhaseX64USB.exe), phono
 * hardware-excludes every other source, and an all-zero register reading
 * auto-mutes. Modelled as an ALSA enum per pair instead of N booleans, so
 * the exclusivity is structural instead of enforced ad hoc in put() - see
 * tests/hw/REPORT.md's v0.1.4 open issues and analysis/NOTES.md.
 *
 * "Mute" is kept as an explicit, selectable item rather than an implicit
 * side effect of clearing everything else: an enum always has exactly one
 * selected item, so there is no "nothing selected" state to auto-mute
 * from anymore, but the hardware's muted-ADC state is still a real,
 * useful thing to be able to select on purpose.
 *
 * In 3/4 has no "Mic-HiZ-Line" source - that front jack pair is wired to
 * channels 1/2 only (see "Hardware I/O" in analysis/NOTES.md).
 */
struct px_adc_source {
	const char *name;
	u16 mask;	/* ADC mux bits for this source; 0 = explicit mute */
};

static const struct px_adc_source px_adc_12_sources[] = {
	{ "Mic-HiZ-Line",	ADC_MIC },
	{ "Phono",		ADC_PHONO },
	{ "Aux",		ADC_AUX },
	{ "Loopback 3/4",	ADC_LOOPBACK },
	{ "Sound Module",	ADC_SOUND_MODULE },
	{ "Mute",		0 },
};

static const struct px_adc_source px_adc_34_sources[] = {
	{ "Phono",		ADC_PHONO },
	{ "Aux",		ADC_AUX },
	{ "Loopback 1/2",	ADC_LOOPBACK },
	{ "Sound Module",	ADC_SOUND_MODULE },
	{ "Mute",		0 },
};

struct px_adc_mux {
	u8 codec;
	const struct px_adc_source *sources;
	unsigned int n_sources;
};

static const struct px_adc_mux px_adc_mux_12 = {
	0, px_adc_12_sources, ARRAY_SIZE(px_adc_12_sources)
};
static const struct px_adc_mux px_adc_mux_34 = {
	1, px_adc_34_sources, ARRAY_SIZE(px_adc_34_sources)
};

/* non-exclusive output routing - several of these can be on at once, so
 * they stay independent boolean switches.
 */
struct px_switch {
	const char *name;
	u8 codec;
	u8 reg;
	u16 mask;
};

static const struct px_switch px_switches[] = {
	{ "Direct 1/2 Playback Switch",		0, WM_OUT_MUX, OUT_DIRECT },
	{ "Direct 1/2 Mono Playback Switch",	0, WM_OUT_MUX, OUT_MONO },
	{ "PCM 1/2 Playback Switch",		0, WM_OUT_MUX, 0x01 },
	{ "PCM 1/2 Mono Playback Switch",	0, WM_R7, WM_R7_MONO_BITS },
	{ "Direct 3/4 Playback Switch",		1, WM_OUT_MUX, OUT_DIRECT },
	{ "Sound Module 3/4 Playback Switch",	1, WM_OUT_MUX, OUT_MONO },
	{ "PCM 3/4 Playback Switch",		1, WM_OUT_MUX, 0x01 },
	{ "PCM 3/4 Mono Playback Switch",	1, WM_R7, WM_R7_MONO_BITS },
};

static int px_switch_get(struct snd_kcontrol *kc,
			 struct snd_ctl_elem_value *uc)
{
	struct px_dev *px = snd_kcontrol_chip(kc);
	const struct px_switch *sw = &px_switches[kc->private_value];
	unsigned long flags;

	/*
	 * Addendum (2026-10-17, review round 5): reads desired_codec, not
	 * codec - bug found on real hardware: get() returned the last
	 * *applied* value instead of the last *desired* one, so a put()
	 * followed immediately by a get() (amixer's own cset-then-echo,
	 * or a panel readback) could show the stale value for as long as a
	 * live-apply restart takes to land (or indefinitely, if it never
	 * does - e.g. an aborted attempt). desired_codec always equals
	 * codec when idle or when nothing is pending (every px_codec_write()
	 * call mirrors both - see that function's own comment), so this is
	 * only a behavior change in the specific case it exists to fix.
	 * desired_lock, not px->mutex: also avoids this read blocking for
	 * however long a concurrent live-apply restart's mutex hold lasts,
	 * which px->mutex would have.
	 */
	spin_lock_irqsave(&px->desired_lock, flags);
	uc->value.integer.value[0] =
		(px->desired_codec[sw->codec][sw->reg] & sw->mask) == sw->mask;
	spin_unlock_irqrestore(&px->desired_lock, flags);
	return 0;
}

static int px_switch_put(struct snd_kcontrol *kc,
			 struct snd_ctl_elem_value *uc)
{
	struct px_dev *px = snd_kcontrol_chip(kc);
	const struct px_switch *sw = &px_switches[kc->private_value];
	bool on = uc->value.integer.value[0];
	u16 *regs, val, out;
	unsigned long flags;
	int err = 0, changed;

	/*
	 * v0.1.12 live codec apply, phase 3a - hardware-tested since v0.1.12, see
	 * analysis/v0.1.12-plan.md section 17. Relaxed read of px->running, the same
	 * convention px_cap_complete()/px_play_complete() already use: a
	 * momentarily stale value here is harmless, since
	 * px_live_apply_work() re-validates both running and disconnected
	 * state under px->mutex before doing anything, and a put() that
	 * loses this narrow race just stages into desired_codec for
	 * nothing in particular (overwritten by the next idle write or the
	 * next stream's resync). Taking px->mutex unconditionally here
	 * would make every live put() wait behind whatever restart might
	 * already be in flight - design doc section 3: "puts must not wait
	 * behind the 315ms mutex hold, otherwise asynchronous coalescing
	 * would be ineffective" - so the running case below deliberately
	 * never touches px->mutex, only the much shorter-held desired_lock.
	 */
	if (px->running) {
		spin_lock_irqsave(&px->desired_lock, flags);
		regs = px->desired_codec[sw->codec];
		val = on ? regs[sw->reg] | sw->mask : regs[sw->reg] & ~sw->mask;
		changed = val != regs[sw->reg];
		if (changed) {
			regs[sw->reg] = val;
			/* Same collateral mono-off rule as the idle path
			 * below, applied to the desired snapshot instead of
			 * hardware.
			 */
			if (sw->codec == 0 && sw->reg != WM_R7) {
				out = px->desired_codec[0][WM_OUT_MUX];
				if ((out & OUT_MONO) &&
				    (!(px->desired_codec[0][WM_ADC_MUX] & ADC_MIC) ||
				     !(out & OUT_DIRECT)))
					px->desired_codec[0][WM_OUT_MUX] = out & ~OUT_MONO;
			}
			px->desired_gen++;
		}
		spin_unlock_irqrestore(&px->desired_lock, flags);
		if (changed) {
			/* Addendum (2026-10-16, v0.1.12-plan.md section 11):
			 * matches the "committed gen=" trace in
			 * px_live_apply_work() - together they let a test
			 * session measure the stage-to-commit gap per toggle
			 * directly from dmesg timestamps, for the same
			 * generation number. dev_dbg, not dev_info: every
			 * accepted live put would otherwise log in normal
			 * use, not just test sessions - enable via dyndbg for
			 * this file when measuring. numid + comm/pid (review
			 * item 3): put() runs in the calling process's own
			 * context (ioctl syscall), so current->comm/pid
			 * identifies which process issued the write (panel,
			 * amixer, a DAW's own mixer, etc) without the driver
			 * needing to track callers itself. cap/play in_elapsed
			 * + inc/dec totals (review round 5): same reasoning as
			 * px_sync_stop_diag()'s own fields - lets a test
			 * session see whether anything was already leaking
			 * before this toggle, not just after the fact.
			 */
			dev_dbg(&px->udev->dev,
				"live apply: staged gen=%u (%s, numid=%u, by %s[%d]) "
				"cap.in_elapsed=%d (inc=%d dec=%d) play.in_elapsed=%d (inc=%d dec=%d)\n",
				px->desired_gen, sw->name, kc->id.numid,
				current->comm, current->pid,
				atomic_read(&px->cap.in_elapsed),
				atomic_read(&px->cap.in_elapsed_inc_total),
				atomic_read(&px->cap.in_elapsed_dec_total),
				atomic_read(&px->play.in_elapsed),
				atomic_read(&px->play.in_elapsed_inc_total),
				atomic_read(&px->play.in_elapsed_dec_total));
			if (px->mono_12_kctl)
				snd_ctl_notify(px->card, SNDRV_CTL_EVENT_MASK_VALUE,
					       &px->mono_12_kctl->id);
			/* LIVE_APPLY_DEBOUNCE_MS, not 0 - see that constant's
			 * comment. Adds at most that much to the staged-to-
			 * committed gap a test session measures, negligible
			 * against the ~0.5s budget.
			 */
			schedule_delayed_work(&px->live_apply_work,
					      msecs_to_jiffies(LIVE_APPLY_DEBOUNCE_MS));
		}
		return changed;
	}

	mutex_lock(&px->mutex);
	if (px->disconnected) {
		mutex_unlock(&px->mutex);
		return -ENODEV;
	}
	regs = px->codec[sw->codec];
	val = on ? regs[sw->reg] | sw->mask : regs[sw->reg] & ~sw->mask;

	changed = val != regs[sw->reg];
	if (changed)
		err = px_codec_write(px, sw->codec, sw->reg, val);

	/* the mono direct signal needs the mic input and direct monitoring;
	 * this can be this very switch (turning mono on without the
	 * prerequisites, or turning Direct off under an active mono) or it
	 * can collaterally affect the *other* mono switch's kcontrol, which
	 * needs an explicit notify - the core only auto-notifies for the
	 * control put() was actually called on.
	 */
	if (!err && sw->codec == 0 && sw->reg != WM_R7) {
		out = regs[WM_OUT_MUX];
		if ((out & OUT_MONO) &&
		    (!(regs[WM_ADC_MUX] & ADC_MIC) || !(out & OUT_DIRECT))) {
			err = px_codec_write(px, 0, WM_OUT_MUX, out & ~OUT_MONO);
			if (!err && px->mono_12_kctl)
				snd_ctl_notify(px->card, SNDRV_CTL_EVENT_MASK_VALUE,
					       &px->mono_12_kctl->id);
		}
	}
	mutex_unlock(&px->mutex);
	return err < 0 ? err : changed;
}

static int px_adc_source_find(const struct px_adc_mux *mux, u16 reg_val)
{
	unsigned int i;

	reg_val &= ~WM_ADC_MUTE;
	for (i = 0; i < mux->n_sources; i++)
		if (mux->sources[i].mask && mux->sources[i].mask == reg_val)
			return i;
	/* explicit WM_ADC_MUTE, a bare zero, or any other leftover pattern
	 * we don't recognize - "Mute" (always the last item) is the closest
	 * honest answer rather than guessing a source that isn't selected.
	 */
	return mux->n_sources - 1;
}

static int px_adc_source_info(struct snd_kcontrol *kc,
			      struct snd_ctl_elem_info *uinfo)
{
	const struct px_adc_mux *mux = (const struct px_adc_mux *)kc->private_value;
	const char *names[6];
	unsigned int i;

	for (i = 0; i < mux->n_sources; i++)
		names[i] = mux->sources[i].name;
	return snd_ctl_enum_info(uinfo, 1, mux->n_sources, names);
}

static int px_adc_source_get(struct snd_kcontrol *kc,
			     struct snd_ctl_elem_value *uc)
{
	struct px_dev *px = snd_kcontrol_chip(kc);
	const struct px_adc_mux *mux = (const struct px_adc_mux *)kc->private_value;
	unsigned long flags;

	/* See px_switch_get()'s identical fix (review round 5): reads
	 * desired_codec, not codec.
	 */
	spin_lock_irqsave(&px->desired_lock, flags);
	uc->value.enumerated.item[0] =
		px_adc_source_find(mux, px->desired_codec[mux->codec][WM_ADC_MUX]);
	spin_unlock_irqrestore(&px->desired_lock, flags);
	return 0;
}

static int px_adc_source_put(struct snd_kcontrol *kc,
			     struct snd_ctl_elem_value *uc)
{
	struct px_dev *px = snd_kcontrol_chip(kc);
	const struct px_adc_mux *mux = (const struct px_adc_mux *)kc->private_value;
	unsigned int item = uc->value.enumerated.item[0];
	unsigned long flags;
	u16 new_val, out;
	int err, changed;

	if (item >= mux->n_sources)
		return -EINVAL;
	new_val = mux->sources[item].mask ? mux->sources[item].mask : WM_ADC_MUTE;

	/*
	 * v0.1.12 live codec apply, phase 3a - hardware-tested since v0.1.12, see
	 * analysis/v0.1.12-plan.md section 17,
	 * and px_switch_put()'s identical
	 * comment (relaxed px->running read, no px->mutex in this branch).
	 * RIAA update/reset for this source change is deliberately NOT done
	 * here - capture keeps using the currently *applied* source's RIAA
	 * state (design doc section 7: "decode using the applied source's
	 * riaa_active state" until commit) for as long as it takes the
	 * worker to actually land this register write; see
	 * px_live_apply_restart()'s own RIAA handling.
	 */
	if (px->running) {
		spin_lock_irqsave(&px->desired_lock, flags);
		changed = new_val != px->desired_codec[mux->codec][WM_ADC_MUX];
		if (changed) {
			px->desired_codec[mux->codec][WM_ADC_MUX] = new_val;
			if (mux->codec == 0) {
				out = px->desired_codec[0][WM_OUT_MUX];
				if ((out & OUT_MONO) &&
				    (!(new_val & ADC_MIC) || !(out & OUT_DIRECT)))
					px->desired_codec[0][WM_OUT_MUX] = out & ~OUT_MONO;
			}
			px->desired_gen++;
		}
		spin_unlock_irqrestore(&px->desired_lock, flags);
		if (changed) {
			/* See px_switch_put()'s identical trace comment
			 * (numid + comm/pid, review item 3; in_elapsed +
			 * totals, review round 5).
			 */
			dev_dbg(&px->udev->dev,
				"live apply: staged gen=%u (Capture Source -> %s, numid=%u, by %s[%d]) "
				"cap.in_elapsed=%d (inc=%d dec=%d) play.in_elapsed=%d (inc=%d dec=%d)\n",
				px->desired_gen, mux->sources[item].name, kc->id.numid,
				current->comm, current->pid,
				atomic_read(&px->cap.in_elapsed),
				atomic_read(&px->cap.in_elapsed_inc_total),
				atomic_read(&px->cap.in_elapsed_dec_total),
				atomic_read(&px->play.in_elapsed),
				atomic_read(&px->play.in_elapsed_inc_total),
				atomic_read(&px->play.in_elapsed_dec_total));
			if (px->mono_12_kctl)
				snd_ctl_notify(px->card, SNDRV_CTL_EVENT_MASK_VALUE,
					       &px->mono_12_kctl->id);
			/* See px_switch_put()'s identical LIVE_APPLY_DEBOUNCE_MS
			 * comment.
			 */
			schedule_delayed_work(&px->live_apply_work,
					      msecs_to_jiffies(LIVE_APPLY_DEBOUNCE_MS));
		}
		return changed;
	}

	mutex_lock(&px->mutex);
	if (px->disconnected) {
		mutex_unlock(&px->mutex);
		return -ENODEV;
	}
	changed = new_val != px->codec[mux->codec][WM_ADC_MUX];
	err = changed ? px_codec_write(px, mux->codec, WM_ADC_MUX, new_val) : 0;
	if (!err && changed) {
		spin_lock_irqsave(&px->cap.lock, flags);
		px_riaa_update_pair(px, mux->codec);
		px_riaa_reset_pair(px, mux->codec);
		spin_unlock_irqrestore(&px->cap.lock, flags);
	}

	/* same mono-direct interlock as px_switch_put(); codec 1's WM_OUT_MUX
	 * bit at the same position means "Sound Module", not mono, so this
	 * never applies there.
	 */
	if (!err && mux->codec == 0) {
		out = px->codec[0][WM_OUT_MUX];
		if ((out & OUT_MONO) &&
		    (!(new_val & ADC_MIC) || !(out & OUT_DIRECT))) {
			err = px_codec_write(px, 0, WM_OUT_MUX, out & ~OUT_MONO);
			if (!err && px->mono_12_kctl)
				snd_ctl_notify(px->card, SNDRV_CTL_EVENT_MASK_VALUE,
					       &px->mono_12_kctl->id);
		}
	}
	mutex_unlock(&px->mutex);
	return err < 0 ? err : changed;
}

struct px_riaa_control {
	const char *name;
	u8 pair;
};

static const struct px_riaa_control px_riaa_controls[] = {
	{ "Phono RIAA 1/2 Capture Switch", 0 },
	{ "Phono RIAA 3/4 Capture Switch", 1 },
};

static int px_riaa_get(struct snd_kcontrol *kc,
		       struct snd_ctl_elem_value *uc)
{
	struct px_dev *px = snd_kcontrol_chip(kc);
	const struct px_riaa_control *control =
		&px_riaa_controls[kc->private_value];

	mutex_lock(&px->mutex);
	uc->value.integer.value[0] = px->riaa_switch[control->pair];
	mutex_unlock(&px->mutex);
	return 0;
}

static int px_riaa_put(struct snd_kcontrol *kc,
		       struct snd_ctl_elem_value *uc)
{
	struct px_dev *px = snd_kcontrol_chip(kc);
	const struct px_riaa_control *control =
		&px_riaa_controls[kc->private_value];
	unsigned long flags;
	bool enabled = !!uc->value.integer.value[0];
	int changed;

	mutex_lock(&px->mutex);
	if (px->disconnected) {
		mutex_unlock(&px->mutex);
		return -ENODEV;
	}
	changed = enabled != px->riaa_switch[control->pair];
	if (changed) {
		spin_lock_irqsave(&px->cap.lock, flags);
		px->riaa_switch[control->pair] = enabled;
		px_riaa_update_pair(px, control->pair);
		px_riaa_reset_pair(px, control->pair);
		spin_unlock_irqrestore(&px->cap.lock, flags);
	}
	mutex_unlock(&px->mutex);
	return changed;
}

static const char * const px_clock_names[] = { "Internal", "Digital Input" };

static int px_clock_info(struct snd_kcontrol *kc,
			 struct snd_ctl_elem_info *uinfo)
{
	return snd_ctl_enum_info(uinfo, 1, ARRAY_SIZE(px_clock_names),
				 px_clock_names);
}

static int px_clock_get(struct snd_kcontrol *kc,
			struct snd_ctl_elem_value *uc)
{
	struct px_dev *px = snd_kcontrol_chip(kc);

	uc->value.enumerated.item[0] = px->clock_internal ? 0 : 1;
	return 0;
}

static int px_clock_put(struct snd_kcontrol *kc,
			struct snd_ctl_elem_value *uc)
{
	struct px_dev *px = snd_kcontrol_chip(kc);
	bool internal = uc->value.enumerated.item[0] == 0;
	int err;

	if (uc->value.enumerated.item[0] > 1)
		return -EINVAL;
	mutex_lock(&px->mutex);
	if (px->disconnected) {
		mutex_unlock(&px->mutex);
		return -ENODEV;
	}
	if (internal == px->clock_internal) {
		mutex_unlock(&px->mutex);
		return 0;
	}
	err = px_status_update(px, STATUS_INTERNAL_CLOCK,
			       internal ? STATUS_INTERNAL_CLOCK : 0);
	if (!err)
		px->clock_internal = internal;
	mutex_unlock(&px->mutex);
	return err < 0 ? err : 1;
}

/*
 * Digital Input Lock reports live, read-only hardware state that this
 * driver never writes, so unlike px_status_update() above there's no
 * write-side cache to fall back on - px->last_digital_lock (updated
 * lazily by px_update_lock_cache(), only from here) is the best
 * available substitute while px->running, when the live read would
 * otherwise fail (see tests/hw/REPORT.md "Finding B"). When not
 * running, a live read is still attempted, and a real failure there is
 * returned as-is rather than silently served from the cache - the
 * cache is only for the case known to fail, not a general fallback.
 */
static int px_lock_get(struct snd_kcontrol *kc,
		       struct snd_ctl_elem_value *uc)
{
	struct px_dev *px = snd_kcontrol_chip(kc);
	u8 status = 0;
	int err;

	mutex_lock(&px->mutex);
	if (px->disconnected) {
		mutex_unlock(&px->mutex);
		return -ENODEV;
	}
	if (px->running) {
		uc->value.integer.value[0] = px->last_digital_lock;
		mutex_unlock(&px->mutex);
		return 0;
	}
	err = px_status_read(px, &status);
	if (err < 0) {
		mutex_unlock(&px->mutex);
		return err;
	}
	px_update_lock_cache(px, status);
	uc->value.integer.value[0] = px->last_digital_lock;
	mutex_unlock(&px->mutex);
	return 0;
}

static const struct snd_kcontrol_new px_clock_ctl = {
	.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
	.name = "Clock Source",
	.info = px_clock_info,
	.get = px_clock_get,
	.put = px_clock_put,
};

static const struct snd_kcontrol_new px_lock_ctl = {
	.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
	.name = "Digital Input Lock",
	.access = SNDRV_CTL_ELEM_ACCESS_READ | SNDRV_CTL_ELEM_ACCESS_VOLATILE,
	.info = snd_ctl_boolean_mono_info,
	.get = px_lock_get,
};

/*
 * Digital Input Rate: the device's actual current rate (GET_CUR at
 * wIndex=0), regardless of source - tracks our own SET_CUR while the
 * internal clock genuinely applies, or the external digital-input rate
 * while STATUS_DIGITAL_LOCK is set (see tests/hw/REPORT.md "Windows USB
 * trace" / "Confirmed without the S/PDIF cable"). Same live-while-not-
 * running, cached-while-running pattern as px_lock_get() above - a live
 * read is unreliable once streaming for the same control-pipe-contention
 * reason ("Finding B"), so px->rate (known correct, since
 * px_start_streaming() just verified it) serves that case instead.
 */
static int px_rate_measured_info(struct snd_kcontrol *kc,
				  struct snd_ctl_elem_info *uinfo)
{
	uinfo->type = SNDRV_CTL_ELEM_TYPE_INTEGER;
	uinfo->count = 1;
	uinfo->value.integer.min = 0;
	uinfo->value.integer.max = 200000;
	return 0;
}

static int px_rate_measured_get(struct snd_kcontrol *kc,
				 struct snd_ctl_elem_value *uc)
{
	struct px_dev *px = snd_kcontrol_chip(kc);
	unsigned int rate;
	int err;

	mutex_lock(&px->mutex);
	if (px->disconnected) {
		mutex_unlock(&px->mutex);
		return -ENODEV;
	}
	if (px->running) {
		uc->value.integer.value[0] = px->rate;
		mutex_unlock(&px->mutex);
		return 0;
	}
	err = px_get_rate_ep(px, 0, &rate, NULL);
	mutex_unlock(&px->mutex);
	if (err < 0)
		return err;
	uc->value.integer.value[0] = rate;
	return 0;
}

static const struct snd_kcontrol_new px_rate_measured_ctl = {
	.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
	.name = "Digital Input Rate",
	.access = SNDRV_CTL_ELEM_ACCESS_READ | SNDRV_CTL_ELEM_ACCESS_VOLATILE,
	.info = px_rate_measured_info,
	.get = px_rate_measured_get,
};

/*
 * Peak meters: "In 1/2 Peak"/"In 3/4 Peak"/"Out 1/2 Peak"/"Out 3/4 Peak",
 * read-only, reset-on-read (see px_cap_update_peaks()/px_play_update_peaks()
 * and the px_pcm_trigger() resets above). analysis/peak-controls-design.md
 * is the design this implements. `capture` selects px->peak_cap[]+cap.lock
 * vs. px->peak_play[]+play.lock; `channel` is the starting index of the
 * pair (0 or 2) within that array - same table-driven shape as
 * px_riaa_controls[] above.
 */
struct px_peak_control {
	const char *name;
	bool capture;
	u8 channel;
};

static const struct px_peak_control px_peak_controls[] = {
	{ "In 1/2 Peak", true, 0 },
	{ "In 3/4 Peak", true, 2 },
	{ "Out 1/2 Peak", false, 0 },
	{ "Out 3/4 Peak", false, 2 },
};

static int px_peak_info(struct snd_kcontrol *kc, struct snd_ctl_elem_info *uinfo)
{
	uinfo->type = SNDRV_CTL_ELEM_TYPE_INTEGER;
	uinfo->count = 2;
	uinfo->value.integer.min = 0;
	uinfo->value.integer.max = S24_MAX;
	return 0;
}

static int px_peak_get(struct snd_kcontrol *kc, struct snd_ctl_elem_value *uc)
{
	struct px_dev *px = snd_kcontrol_chip(kc);
	const struct px_peak_control *control = &px_peak_controls[kc->private_value];
	s32 *peaks = control->capture ? px->peak_cap : px->peak_play;
	spinlock_t *lock = control->capture ? &px->cap.lock : &px->play.lock;
	unsigned long flags;
	int ch;

	spin_lock_irqsave(lock, flags);
	for (ch = 0; ch < 2; ch++) {
		uc->value.integer.value[ch] = peaks[control->channel + ch];
		peaks[control->channel + ch] = 0;
	}
	spin_unlock_irqrestore(lock, flags);
	return 0;
}

static int px_create_mixer(struct px_dev *px)
{
	struct snd_kcontrol_new kn = {
		.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
		.info = snd_ctl_boolean_mono_info,
		.get = px_switch_get,
		.put = px_switch_put,
	};
	struct snd_kcontrol_new adc_kn = {
		.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
		.info = px_adc_source_info,
		.get = px_adc_source_get,
		.put = px_adc_source_put,
	};
	struct snd_kcontrol_new riaa_kn = {
		.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
		.info = snd_ctl_boolean_mono_info,
		.get = px_riaa_get,
		.put = px_riaa_put,
	};
	struct snd_kcontrol_new peak_kn = {
		.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
		.access = SNDRV_CTL_ELEM_ACCESS_READ | SNDRV_CTL_ELEM_ACCESS_VOLATILE,
		.info = px_peak_info,
		.get = px_peak_get,
	};
	struct snd_kcontrol *kctl;
	int i, err;

	for (i = 0; i < ARRAY_SIZE(px_switches); i++) {
		kn.name = px_switches[i].name;
		kn.private_value = i;
		kctl = snd_ctl_new1(&kn, px);
		err = snd_ctl_add(px->card, kctl);
		if (err < 0)
			return err;
		if (px_switches[i].codec == 0 && px_switches[i].reg == WM_OUT_MUX &&
		    px_switches[i].mask == OUT_MONO)
			px->mono_12_kctl = kctl;
	}
	for (i = 0; i < ARRAY_SIZE(px_riaa_controls); i++) {
		riaa_kn.name = px_riaa_controls[i].name;
		riaa_kn.private_value = i;
		err = snd_ctl_add(px->card, snd_ctl_new1(&riaa_kn, px));
		if (err < 0)
			return err;
	}

	adc_kn.name = "In 1/2 Capture Source";
	adc_kn.private_value = (unsigned long)&px_adc_mux_12;
	err = snd_ctl_add(px->card, snd_ctl_new1(&adc_kn, px));
	if (err < 0)
		return err;

	adc_kn.name = "In 3/4 Capture Source";
	adc_kn.private_value = (unsigned long)&px_adc_mux_34;
	err = snd_ctl_add(px->card, snd_ctl_new1(&adc_kn, px));
	if (err < 0)
		return err;

	err = snd_ctl_add(px->card, snd_ctl_new1(&px_clock_ctl, px));
	if (err < 0)
		return err;
	kctl = snd_ctl_new1(&px_lock_ctl, px);
	err = snd_ctl_add(px->card, kctl);
	if (err < 0)
		return err;
	px->lock_kctl = kctl;

	err = snd_ctl_add(px->card, snd_ctl_new1(&px_rate_measured_ctl, px));
	if (err < 0)
		return err;

	for (i = 0; i < ARRAY_SIZE(px_peak_controls); i++) {
		peak_kn.name = px_peak_controls[i].name;
		peak_kn.private_value = i;
		err = snd_ctl_add(px->card, snd_ctl_new1(&peak_kn, px));
		if (err < 0)
			return err;
	}
	return 0;
}

/* factory defaults of the Windows driver for 0a4a:5b01 */
static int px_codec_init(struct px_dev *px)
{
	static const u8 defaults[2][3][2] = {
		{ { WM_R7, WM_R7_STEREO }, { WM_ADC_MUX, 0x01 }, { WM_OUT_MUX, 0x01 } },
		{ { WM_R7, WM_R7_STEREO }, { WM_ADC_MUX, 0x04 }, { WM_OUT_MUX, 0x01 } },
	};
	int c, i, err;

	for (c = 0; c < 2; c++)
		for (i = 0; i < 3; i++) {
			err = px_codec_write(px, c, defaults[c][i][0],
					     defaults[c][i][1]);
			if (err < 0)
				return err;
		}
	return 0;
}

/* ---------------------------------------------------------------------- */
/* MIDI */

static void px_midi_send(struct px_dev *px)
{
	u8 *buf = px->midi_out_urb->transfer_buffer;
	int n, rc;

	if (px->midi_out_busy || !px->midi_out || px->disconnected)
		return;
	memset(buf, MIDI_FILLER, MIDI_PACKET - 1);
	n = snd_rawmidi_transmit(px->midi_out, buf, MIDI_BYTES_PER_MS);
	if (n <= 0)
		return;
	buf[MIDI_PACKET - 1] = MIDI_CONTROL;
	px->midi_out_urb->transfer_buffer_length = MIDI_PACKET;
	rc = usb_submit_urb(px->midi_out_urb, GFP_ATOMIC);
	dev_dbg(&px->udev->dev, "midi out: submit %d bytes %*phN rc=%d\n",
		n, MIDI_PACKET, buf, rc);
	if (rc == 0)
		px->midi_out_busy = true;
}

static enum hrtimer_restart px_midi_timer_fn(struct hrtimer *t)
{
	struct px_dev *px = container_of(t, struct px_dev, midi_timer);
	unsigned long flags;

	spin_lock_irqsave(&px->midi_lock, flags);
	px_midi_send(px);
	spin_unlock_irqrestore(&px->midi_lock, flags);
	return HRTIMER_NORESTART;
}

static void px_midi_out_complete(struct urb *urb)
{
	struct px_dev *px = urb->context;
	unsigned long flags;

	dev_dbg(&px->udev->dev, "midi out: complete status=%d actual_length=%u\n",
		urb->status, urb->actual_length);
	spin_lock_irqsave(&px->midi_lock, flags);
	px->midi_out_busy = false;
	if (urb->status == 0 && px->midi_out)
		/* pace the output to the MIDI wire speed */
		hrtimer_start(&px->midi_timer, ms_to_ktime(1), HRTIMER_MODE_REL);
	spin_unlock_irqrestore(&px->midi_lock, flags);
}

/*
 * Byte 8 of every IN frame is a trailing control/status byte (mirroring
 * the OUT side's byte 8), observed 0x00 on idle hardware - never passed
 * to px_midi_in_filter_frame(), only logged. See tests/hw/REPORT.md
 * "MIDI loopback diagnosis" for the dmesg evidence this is built on, and
 * for why it's a Linux-only improvement: the decomp's FUN_f1007990
 * strips 0xFD across the whole buffer with no byte-8 exclusion, so
 * Windows does not special-case this byte either.
 */
static void px_midi_in_complete(struct urb *urb)
{
	struct px_dev *px = urb->context;
	u8 *buf = urb->transfer_buffer;
	unsigned long flags;
	unsigned int off;
	u8 out[MIDI_PACKET - 1];

	dev_dbg(&px->udev->dev, "midi in: complete status=%d actual_length=%u first=%*phN\n",
		urb->status, urb->actual_length,
		min_t(unsigned int, urb->actual_length, 16), buf);

	if (urb->status == -ENOENT || urb->status == -ECONNRESET ||
	    urb->status == -ESHUTDOWN || urb->status == -ENODEV)
		return;

	if (urb->actual_length % MIDI_PACKET)
		dev_dbg_ratelimited(&px->udev->dev,
			"midi in: actual_length=%u not a multiple of %d, trailing bytes ignored\n",
			urb->actual_length, MIDI_PACKET);

	for (off = 0; off + MIDI_PACKET <= urb->actual_length; off += MIDI_PACKET) {
		const u8 *pkt = buf + off;
		int n;

		spin_lock_irqsave(&px->midi_lock, flags);
		n = px_midi_in_filter_frame(pkt, out, &px->midi_in_seen_status);
		if (n && px->midi_in)
			snd_rawmidi_receive(px->midi_in, out, n);
		spin_unlock_irqrestore(&px->midi_lock, flags);
	}

	if (usb_submit_urb(urb, GFP_ATOMIC) < 0)
		dev_err_ratelimited(&px->udev->dev, "MIDI in resubmit failed\n");
}

static int px_midi_in_open(struct snd_rawmidi_substream *sub)
{
	struct px_dev *px = sub->rmidi->private_data;
	int i, err;

	/*
	 * Fresh arm: forget any status byte from a previous session. Done
	 * here, once per open, not in px_midi_in_trigger() - the rawmidi
	 * core calls .trigger(up=1) far more often than once per open
	 * (confirmed via dyndbg: effectively once per delivered frame in
	 * testing), so resetting there discarded the leading byte of
	 * almost every frame instead of just the genuinely orphaned ones.
	 * See tests/hw/REPORT.md "MIDI loopback diagnosis". No concurrent
	 * completion can be touching this field here: no URB is submitted
	 * yet, and px_midi_in_close()'s usb_kill_urb() calls already waited
	 * out any completion from a previous open before this one could
	 * happen.
	 */
	px->midi_in_seen_status = 0;

	/* Keep MIDI_IN_URBS (5) read requests outstanding at once, matching
	 * the Windows driver (FUN_f1002f00 x5 at arm) - see tests/hw/
	 * REPORT.md. Each resubmits only itself on completion
	 * (px_midi_in_complete()); all share px->midi_lock/seen_status.
	 */
	for (i = 0; i < MIDI_IN_URBS; i++) {
		err = usb_submit_urb(px->midi_in_urb[i], GFP_KERNEL);
		if (err < 0) {
			int j;

			for (j = 0; j < i; j++)
				usb_kill_urb(px->midi_in_urb[j]);
			return err;
		}
	}
	px->midi_in_opened = true;
	return 0;
}

static int px_midi_in_close(struct snd_rawmidi_substream *sub)
{
	struct px_dev *px = sub->rmidi->private_data;
	int i;

	px->midi_in_opened = false;
	for (i = 0; i < MIDI_IN_URBS; i++)
		usb_kill_urb(px->midi_in_urb[i]);
	return 0;
}

static void px_midi_in_trigger(struct snd_rawmidi_substream *sub, int up)
{
	struct px_dev *px = sub->rmidi->private_data;
	unsigned long flags;

	spin_lock_irqsave(&px->midi_lock, flags);
	px->midi_in = up ? sub : NULL;
	spin_unlock_irqrestore(&px->midi_lock, flags);
}

static int px_midi_out_open(struct snd_rawmidi_substream *sub)
{
	return 0;
}

static int px_midi_out_close(struct snd_rawmidi_substream *sub)
{
	struct px_dev *px = sub->rmidi->private_data;
	unsigned long flags;

	spin_lock_irqsave(&px->midi_lock, flags);
	px->midi_out = NULL;
	spin_unlock_irqrestore(&px->midi_lock, flags);
	hrtimer_cancel(&px->midi_timer);
	usb_kill_urb(px->midi_out_urb);
	return 0;
}

static void px_midi_out_trigger(struct snd_rawmidi_substream *sub, int up)
{
	struct px_dev *px = sub->rmidi->private_data;
	unsigned long flags;

	spin_lock_irqsave(&px->midi_lock, flags);
	px->midi_out = up ? sub : NULL;
	if (up)
		px_midi_send(px);
	spin_unlock_irqrestore(&px->midi_lock, flags);
}

static void px_midi_out_drain(struct snd_rawmidi_substream *sub)
{
	struct px_dev *px = sub->rmidi->private_data;
	int timeout = 1000;

	while (timeout-- > 0 &&
	       (READ_ONCE(px->midi_out_busy) || !snd_rawmidi_transmit_empty(sub)))
		msleep(1);
}

static const struct snd_rawmidi_ops px_midi_in_ops = {
	.open = px_midi_in_open,
	.close = px_midi_in_close,
	.trigger = px_midi_in_trigger,
};

static const struct snd_rawmidi_ops px_midi_out_ops = {
	.open = px_midi_out_open,
	.close = px_midi_out_close,
	.trigger = px_midi_out_trigger,
	.drain = px_midi_out_drain,
};

static int px_create_midi(struct px_dev *px)
{
	struct snd_rawmidi *rmidi;
	int err;

	err = snd_rawmidi_new(px->card, "PHASE X64 MIDI", 0, 1, 1, &rmidi);
	if (err < 0)
		return err;
	strscpy(rmidi->name, "PHASE X64 USB MIDI", sizeof(rmidi->name));
	rmidi->private_data = px;
	rmidi->info_flags = SNDRV_RAWMIDI_INFO_OUTPUT | SNDRV_RAWMIDI_INFO_INPUT |
			    SNDRV_RAWMIDI_INFO_DUPLEX;
	snd_rawmidi_set_ops(rmidi, SNDRV_RAWMIDI_STREAM_OUTPUT, &px_midi_out_ops);
	snd_rawmidi_set_ops(rmidi, SNDRV_RAWMIDI_STREAM_INPUT, &px_midi_in_ops);
	return 0;
}

/* ---------------------------------------------------------------------- */
/* USB */

static void px_free_urbs(struct px_dev *px)
{
	struct urb **all[] = { px->play_urb, px->fb_urb, px->cap_urb, px->midi_in_urb };
	const int counts[] = { PLAY_URBS, FB_URBS, CAP_URBS, MIDI_IN_URBS };
	int a, i;

	for (a = 0; a < ARRAY_SIZE(all); a++)
		for (i = 0; i < counts[a]; i++) {
			if (!all[a][i])
				continue;
			kfree(all[a][i]->transfer_buffer);
			usb_free_urb(all[a][i]);
			all[a][i] = NULL;
		}
	if (px->midi_out_urb) {
		kfree(px->midi_out_urb->transfer_buffer);
		usb_free_urb(px->midi_out_urb);
		px->midi_out_urb = NULL;
	}
}

static struct urb *px_alloc_iso_urb(struct px_dev *px, unsigned int pipe,
				    int packets, int packet_size, int interval,
				    usb_complete_t complete)
{
	struct urb *urb = usb_alloc_urb(packets, GFP_KERNEL);
	int i;

	if (!urb)
		return NULL;
	urb->transfer_buffer = kzalloc(packets * packet_size, GFP_KERNEL);
	if (!urb->transfer_buffer) {
		usb_free_urb(urb);
		return NULL;
	}
	urb->dev = px->udev;
	urb->pipe = pipe;
	urb->context = px;
	urb->complete = complete;
	urb->interval = interval;
	urb->transfer_flags = URB_ISO_ASAP;
	urb->number_of_packets = packets;
	urb->transfer_buffer_length = packets * packet_size;
	for (i = 0; i < packets; i++) {
		urb->iso_frame_desc[i].offset = i * packet_size;
		urb->iso_frame_desc[i].length = packet_size;
	}
	return urb;
}

static struct urb *px_alloc_bulk_urb(struct px_dev *px, unsigned int pipe,
				     int size, usb_complete_t complete)
{
	struct urb *urb = usb_alloc_urb(0, GFP_KERNEL);
	void *buf;

	if (!urb)
		return NULL;
	buf = kzalloc(size, GFP_KERNEL);
	if (!buf) {
		usb_free_urb(urb);
		return NULL;
	}
	usb_fill_bulk_urb(urb, px->udev, pipe, buf, size, complete, px);
	return urb;
}

static int px_alloc_urbs(struct px_dev *px)
{
	struct usb_host_endpoint *ep;
	int fb_interval = 8, i;

	/* cap_urb_bytes is a user-supplied module_param - clamp to a sane
	 * multiple of the 64-byte wire frame instead of trusting it blindly.
	 */
	if (!cap_urb_bytes || cap_urb_bytes % CAP_WIRE_FRAME ||
	    cap_urb_bytes > 8192) {
		dev_warn(&px->udev->dev,
			 "cap_urb_bytes=%u invalid (must be a non-zero multiple of %u, <= 8192), using default %u\n",
			 cap_urb_bytes, CAP_WIRE_FRAME, CAP_URB_BYTES_DEFAULT);
		cap_urb_bytes = CAP_URB_BYTES_DEFAULT;
	}

	ep = usb_pipe_endpoint(px->udev, usb_rcvisocpipe(px->udev, EP_FEEDBACK));
	if (ep && ep->desc.bInterval)
		fb_interval = 1 << (ep->desc.bInterval - 1);

	for (i = 0; i < PLAY_URBS; i++) {
		px->play_urb[i] = px_alloc_iso_urb(px,
				usb_sndisocpipe(px->udev, EP_PLAY), PLAY_PACKETS,
				PLAY_MAX_PACKET, 1, px_play_complete);
		if (!px->play_urb[i])
			return -ENOMEM;
	}
	for (i = 0; i < FB_URBS; i++) {
		px->fb_urb[i] = px_alloc_iso_urb(px,
				usb_rcvisocpipe(px->udev, EP_FEEDBACK), 1,
				FB_PACKET, fb_interval, px_fb_complete);
		if (!px->fb_urb[i])
			return -ENOMEM;
	}
	for (i = 0; i < CAP_URBS; i++) {
		px->cap_urb[i] = px_alloc_bulk_urb(px,
				usb_rcvbulkpipe(px->udev, EP_CAPTURE),
				cap_urb_bytes, px_cap_complete);
		if (!px->cap_urb[i])
			return -ENOMEM;
	}
	for (i = 0; i < MIDI_IN_URBS; i++) {
		px->midi_in_urb[i] = px_alloc_bulk_urb(px,
				usb_rcvbulkpipe(px->udev, EP_MIDI_IN),
				MIDI_IN_BYTES, px_midi_in_complete);
		if (!px->midi_in_urb[i])
			return -ENOMEM;
	}
	px->midi_out_urb = px_alloc_bulk_urb(px,
			usb_sndbulkpipe(px->udev, EP_MIDI_OUT),
			MIDI_PACKET, px_midi_out_complete);
	if (!px->midi_out_urb)
		return -ENOMEM;
	return 0;
}

static int px_check_endpoints(struct px_dev *px)
{
	static const struct {
		u8 addr;
		u8 type;
	} eps[] = {
		{ EP_PLAY, USB_ENDPOINT_XFER_ISOC },
		{ EP_FEEDBACK, USB_ENDPOINT_XFER_ISOC },
		{ EP_CAPTURE, USB_ENDPOINT_XFER_BULK },
		{ EP_MIDI_IN, USB_ENDPOINT_XFER_BULK },
		{ EP_MIDI_OUT, USB_ENDPOINT_XFER_BULK },
	};
	struct usb_host_endpoint *ep;
	unsigned int pipe;
	int i;

	for (i = 0; i < ARRAY_SIZE(eps); i++) {
		if (eps[i].type == USB_ENDPOINT_XFER_ISOC)
			pipe = eps[i].addr & USB_DIR_IN ?
				usb_rcvisocpipe(px->udev, eps[i].addr) :
				usb_sndisocpipe(px->udev, eps[i].addr);
		else
			pipe = eps[i].addr & USB_DIR_IN ?
				usb_rcvbulkpipe(px->udev, eps[i].addr) :
				usb_sndbulkpipe(px->udev, eps[i].addr);
		ep = usb_pipe_endpoint(px->udev, pipe);
		if (!ep || usb_endpoint_type(&ep->desc) != eps[i].type) {
			dev_err(&px->udev->dev, "endpoint 0x%02x missing\n",
				eps[i].addr);
			return -ENODEV;
		}
	}
	return 0;
}

static struct usb_driver px_driver;

static void px_card_free(struct snd_card *card)
{
	struct px_dev *px = card->private_data;
	struct usb_interface *intf1 = px->intf1;

	px_free_urbs(px);
	/* only set here when probe failed, disconnect releases it otherwise */
	px->intf1 = NULL;
	if (intf1)
		usb_driver_release_interface(&px_driver, intf1);
}

static int px_probe(struct usb_interface *intf,
		    const struct usb_device_id *usb_id)
{
	struct usb_device *udev = interface_to_usbdev(intf);
	struct snd_card *card;
	struct px_dev *px;
	struct snd_pcm *pcm;
	static int dev_idx;
	int err;

	/* the whole device is handled from interface 0 */
	if (intf->cur_altsetting->desc.bInterfaceNumber != 0)
		return -ENODEV;
	if (udev->speed != USB_SPEED_HIGH) {
		dev_err(&udev->dev, "the PHASE X64 USB needs a high speed port\n");
		return -ENODEV;
	}
	if (dev_idx >= SNDRV_CARDS)
		return -ENODEV;
	if (!enable[dev_idx]) {
		dev_idx++;
		return -ENOENT;
	}

	err = snd_card_new(&intf->dev, index[dev_idx], id[dev_idx], THIS_MODULE,
			   sizeof(*px), &card);
	if (err < 0)
		return err;

	px = card->private_data;
	px->card = card;
	px->udev = udev;
	px->intf0 = intf;
	px->clock_internal = true;
	px->riaa_switch[0] = true;
	px->riaa_switch[1] = true;
	/* Cheap safeguard against the device autosuspending (and
	 * potentially losing firmware state) while unbound/idle before
	 * this point, or ever again while this driver stays bound.
	 */
	usb_disable_autosuspend(px->udev);
	mutex_init(&px->mutex);
	spin_lock_init(&px->lock);
	spin_lock_init(&px->play.lock);
	spin_lock_init(&px->cap.lock);
	atomic_set(&px->play.in_elapsed, 0);
	atomic_set(&px->cap.in_elapsed, 0);
	init_waitqueue_head(&px->play.elapsed_wq);
	init_waitqueue_head(&px->cap.elapsed_wq);
	INIT_DELAYED_WORK(&px->cap_watchdog_work, px_cap_watchdog_fn);
	atomic_set(&px->cap_complete_count, 0);
	atomic_set(&px->cap_wd_gen, 0);
	/* v0.1.12 live codec apply, phase 3a - hardware-tested since v0.1.12, see
	 * analysis/v0.1.12-plan.md section 17.
	 */
	spin_lock_init(&px->desired_lock);
	INIT_DELAYED_WORK(&px->live_apply_work, px_live_apply_work);
	spin_lock_init(&px->midi_lock);
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 13, 0)
	hrtimer_setup(&px->midi_timer, px_midi_timer_fn, CLOCK_MONOTONIC,
		      HRTIMER_MODE_REL);
	hrtimer_setup(&px->fill_timer, px_fill_timer_fn, CLOCK_MONOTONIC,
		      HRTIMER_MODE_REL_SOFT);
#else
	hrtimer_init(&px->midi_timer, CLOCK_MONOTONIC, HRTIMER_MODE_REL);
	px->midi_timer.function = px_midi_timer_fn;
	hrtimer_init(&px->fill_timer, CLOCK_MONOTONIC, HRTIMER_MODE_REL_SOFT);
	px->fill_timer.function = px_fill_timer_fn;
#endif
	card->private_free = px_card_free;

	if (!id[dev_idx])
		snd_card_set_id(card, "PhaseX64");
	strscpy(card->driver, "PhaseX64", sizeof(card->driver));
	strscpy(card->shortname, "PHASE X64 USB", sizeof(card->shortname));
	snprintf(card->longname, sizeof(card->longname),
		 "TerraTec PHASE X64 USB at %s", dev_name(&udev->dev));
	strscpy(card->mixername, "PHASE X64 USB", sizeof(card->mixername));

	px->intf1 = usb_ifnum_to_if(udev, 1);
	if (!px->intf1) {
		err = -ENODEV;
		goto fail;
	}
	err = usb_driver_claim_interface(&px_driver, px->intf1, px);
	if (err < 0) {
		px->intf1 = NULL;
		goto fail;
	}

	err = usb_set_interface(udev, 0, 1);
	if (!err)
		err = usb_set_interface(udev, 1, 1);
	if (err < 0) {
		dev_err(&udev->dev, "cannot select alt setting 1: %d\n", err);
		goto fail;
	}
	err = px_check_endpoints(px);
	if (err < 0)
		goto fail;
	dev_info(&udev->dev,
		 "claimed intf %u/altset %u and intf %u/altset %u; endpoints play=0x%02x fb=0x%02x cap=0x%02x midi_in=0x%02x midi_out=0x%02x\n",
		 px->intf0->cur_altsetting->desc.bInterfaceNumber,
		 px->intf0->cur_altsetting->desc.bAlternateSetting,
		 px->intf1->cur_altsetting->desc.bInterfaceNumber,
		 px->intf1->cur_altsetting->desc.bAlternateSetting,
		 EP_PLAY, EP_FEEDBACK, EP_CAPTURE, EP_MIDI_IN, EP_MIDI_OUT);
	err = px_alloc_urbs(px);
	if (err < 0)
		goto fail;

	err = px_codec_init(px);
	if (err < 0) {
		dev_err(&udev->dev, "codec init failed: %d\n", err);
		goto fail;
	}
	err = px_status_update(px, STATUS_INTERNAL_CLOCK, STATUS_INTERNAL_CLOCK);
	if (err < 0)
		goto fail;

	err = snd_pcm_new(card, "PHASE X64", 0, 1, 1, &pcm);
	if (err < 0)
		goto fail;
	pcm->private_data = px;
	strscpy(pcm->name, "PHASE X64 USB", sizeof(pcm->name));
	snd_pcm_set_ops(pcm, SNDRV_PCM_STREAM_PLAYBACK, &px_pcm_ops);
	snd_pcm_set_ops(pcm, SNDRV_PCM_STREAM_CAPTURE, &px_pcm_ops);
	snd_pcm_set_managed_buffer_all(pcm, SNDRV_DMA_TYPE_VMALLOC, NULL, 0, 0);
	px->pcm = pcm;

	err = px_create_mixer(px);
	if (err < 0)
		goto fail;
	err = px_create_midi(px);
	if (err < 0)
		goto fail;

	err = snd_card_register(card);
	if (err < 0)
		goto fail;

	usb_set_intfdata(intf, px);
	dev_idx++;
	return 0;

fail:
	snd_card_free(card);
	return err;
}

/* interface 1 is claimed by us as well, only interface 0 drives the device */
static struct px_dev *px_from_intf(struct usb_interface *intf)
{
	struct px_dev *px = usb_get_intfdata(intf);

	return px && px->intf0 == intf ? px : NULL;
}

static void px_disconnect(struct usb_interface *intf)
{
	struct px_dev *px = px_from_intf(intf);
	struct usb_interface *intf1;
	int i;

	if (!px) {
		usb_set_intfdata(intf, NULL);
		return;
	}

	snd_card_disconnect(px->card);
	mutex_lock(&px->mutex);
	px->disconnected = true;
	px_stop_streaming(px);
	mutex_unlock(&px->mutex);
	/* process context here (disconnect), unlike trigger(): _sync is
	 * correct and ensures a pending watchdog can't fire after/during
	 * teardown. Belt and braces: px_stop_streaming() above only
	 * cancels these if px->running was still true at entry (its
	 * early-return guard) - if the stream was already stopped before
	 * disconnect, that path never ran, so cancel again here
	 * unconditionally; redundant-but-safe when it already did.
	 */
	cancel_delayed_work_sync(&px->cap_watchdog_work);
	/* v0.1.12 live codec apply, phase 3a - hardware-tested since v0.1.12, see
	 * analysis/v0.1.12-plan.md section 17. Same belt-and-braces reasoning as
	 * the watchdog cancel immediately above.
	 */
	cancel_delayed_work_sync(&px->live_apply_work);
	/* v0.1.12 phase 3b: same belt-and-braces reasoning, for the fill
	 * timer - px_stop_streaming() above already stops it if px->running
	 * was true at entry; redundant-but-safe here otherwise.
	 */
	px_fill_stop(px);
	hrtimer_cancel(&px->midi_timer);
	for (i = 0; i < MIDI_IN_URBS; i++)
		usb_kill_urb(px->midi_in_urb[i]);
	usb_kill_urb(px->midi_out_urb);

	intf1 = px->intf1;
	px->intf1 = NULL;
	if (intf1)
		usb_driver_release_interface(&px_driver, intf1);
	usb_set_intfdata(intf, NULL);
	snd_card_free_when_closed(px->card);
}

static int px_suspend(struct usb_interface *intf, pm_message_t message)
{
	struct px_dev *px = px_from_intf(intf);
	int i;

	if (!px)
		return 0;
	dev_dbg(&px->udev->dev, "px_suspend: enter, message.event=%d\n",
		message.event);
	snd_power_change_state(px->card, SNDRV_CTL_POWER_D3hot);
	snd_pcm_suspend_all(px->pcm);
	mutex_lock(&px->mutex);
	px_stop_streaming(px);
	px->play.prepared = false;
	px->cap.prepared = false;
	mutex_unlock(&px->mutex);
	for (i = 0; i < MIDI_IN_URBS; i++)
		usb_kill_urb(px->midi_in_urb[i]);
	dev_dbg(&px->udev->dev, "px_suspend: exit\n");
	return 0;
}

/* Serves both .resume and .reset_resume (see px_driver below) - the two
 * callbacks aren't distinguishable from in here, so this single pair of
 * log lines covers either case.
 */
static int px_resume(struct usb_interface *intf)
{
	struct px_dev *px = px_from_intf(intf);
	int c, r, m, err = 0;

	if (!px)
		return 0;

	dev_dbg(&px->udev->dev, "px_resume: enter\n");
	mutex_lock(&px->mutex);
	usb_set_interface(px->udev, 0, 1);
	usb_set_interface(px->udev, 1, 1);
	for (c = 0; c < 2 && !err; c++)
		for (r = 0; r < CODEC_REGS && !err; r++)
			if (r == WM_R7 || r == WM_ADC_MUX || r == WM_OUT_MUX)
				err = px_codec_write(px, c, r, px->codec[c][r]);
	if (!err)
		err = px_status_update(px, STATUS_INTERNAL_CLOCK,
				       px->clock_internal ? STATUS_INTERNAL_CLOCK : 0);
	mutex_unlock(&px->mutex);
	if (px->midi_in_opened)
		for (m = 0; m < MIDI_IN_URBS; m++)
			usb_submit_urb(px->midi_in_urb[m], GFP_KERNEL);
	snd_power_change_state(px->card, SNDRV_CTL_POWER_D0);
	dev_dbg(&px->udev->dev, "px_resume: exit err=%d\n", err);
	return err;
}

static const struct usb_device_id px_ids[] = {
	{ USB_DEVICE(PX_VID, PX_PID) },
	{ }
};
MODULE_DEVICE_TABLE(usb, px_ids);

static struct usb_driver px_driver = {
	.name = "snd-usb-phasex64",
	.probe = px_probe,
	.disconnect = px_disconnect,
	.suspend = px_suspend,
	.resume = px_resume,
	.reset_resume = px_resume,
	.id_table = px_ids,
	.supports_autosuspend = 0,
};

module_usb_driver(px_driver);

MODULE_DESCRIPTION("TerraTec PHASE X64 USB audio driver");
MODULE_LICENSE("GPL");
MODULE_VERSION("0.9.2");
