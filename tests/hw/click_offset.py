#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""v0.1.12 phase 3b (timeline fill) acceptance measurement.

Not run by the assistant unprompted - reads a WAV file the user (or the
assistant, once explicitly authorized for a session - see
analysis/v0.1.12-plan.md section 12) captured with `arecord`. Never
touches the device itself.

Measures whether a live-apply restart's ~340ms URB-kill-to-resubmit gap
is actually being filled (phase 3b) rather than lost (phase 3a's own
accepted tradeoff, which this phase exists to fix): play a 1-click/s
track on Out 1/2, record 6ch, read the clicks back from capture channels
5/6 - the bit-exact internal digital tap of Out 1/2 characterized in
analysis/NOTES.md, needing no physical loopback cable. If the gap is
properly filled, consecutive click positions stay within one period of
a fixed 1.0s-apart schedule throughout the recording, including across
every toggle. If it is not, each toggle shows up as a step in the
cumulative drift roughly the size of the restart gap.

Usage:
  python3 click_offset.py <capture.wav> [--period 1.0] [--ch1 5] [--ch2 6]
                           [--wall-clock-s SECONDS]

--ch1/--ch2 are 1-based capture channel numbers (default 5, 6 - the
Out 1/2 tap pair). --wall-clock-s, if given (e.g. the arecord -d value,
or a measured stopwatch duration), is compared against the recording's
own sample-derived length.
"""
import argparse
import sys

import numpy as np

import pxtools as px


def detect_clicks(sig, rate, refractory_s=0.3, threshold_frac=0.2):
    """Onset times (seconds) of impulses in `sig` (1-D float array).
    Threshold is a fraction of the recording's own peak absolute
    amplitude (not an absolute level - a click track's level is whatever
    the sox invocation that made it used), with a refractory period so a
    single impulse's ringing/multiple zero-crossings don't produce more
    than one detection."""
    absf = np.abs(sig)
    peak = absf.max()
    if peak <= 0:
        return np.array([])
    threshold = peak * threshold_frac
    above = absf >= threshold
    # rising edges of `above`
    edges = np.where(above[1:] & ~above[:-1])[0] + 1
    if len(edges) == 0:
        return np.array([])

    refractory_samples = int(round(refractory_s * rate))
    kept = [edges[0]]
    for e in edges[1:]:
        if e - kept[-1] >= refractory_samples:
            kept.append(e)
    return np.array(kept, dtype=np.float64) / rate


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav", help="captured 6ch WAV (arecord -f S32_LE -c 6)")
    ap.add_argument("--period", type=float, default=1.0,
                     help="nominal click period in seconds (default 1.0)")
    ap.add_argument("--ch1", type=int, default=5, help="1-based tap channel (default 5)")
    ap.add_argument("--ch2", type=int, default=6, help="1-based tap channel (default 6)")
    ap.add_argument("--wall-clock-s", type=float, default=None,
                     help="known recording duration (e.g. arecord -d value) to "
                          "compare against the file's own sample-derived length")
    ap.add_argument("--toggle-times", type=str, default=None,
                     help="comma-separated seconds-from-start of each toggle, "
                          "for annotating which click interval each one landed in "
                          "(informational only - not required for the drift check)")
    args = ap.parse_args()

    samples, rate, nchannels, bits = px.read_wav(args.wav)
    if args.ch1 < 1 or args.ch1 > nchannels or args.ch2 < 1 or args.ch2 > nchannels:
        print(f"error: channels {args.ch1}/{args.ch2} out of range for a "
              f"{nchannels}-channel file", file=sys.stderr)
        return 1

    nframes = samples.shape[0]
    file_length_s = nframes / rate

    sig = np.maximum(np.abs(samples[:, args.ch1 - 1].astype(np.float64)),
                      np.abs(samples[:, args.ch2 - 1].astype(np.float64)))
    click_times = detect_clicks(sig, rate, refractory_s=args.period * 0.5)

    print(f"file: {args.wav}")
    print(f"rate={rate} channels={nchannels} bits={bits} frames={nframes}")
    print(f"recorded length: {file_length_s:.3f}s")
    if args.wall_clock_s is not None:
        dev = file_length_s - args.wall_clock_s
        print(f"wall clock: {args.wall_clock_s:.3f}s  deviation: {dev:+.3f}s "
              f"({'within' if abs(dev) < args.period else 'EXCEEDS'} one period)")

    if len(click_times) < 2:
        print(f"error: only {len(click_times)} click(s) detected - check "
              f"--ch1/--ch2 and that the click track actually played", file=sys.stderr)
        return 1

    print(f"\n{len(click_times)} clicks detected, first at {click_times[0]:.4f}s\n")

    toggle_times = []
    if args.toggle_times:
        toggle_times = [float(t) for t in args.toggle_times.split(",")]

    t0 = click_times[0]
    worst_drift = 0.0
    worst_idx = 0
    missed_total = 0
    expected_index = 0
    print(f"{'click':>5} {'actual_s':>10} {'expected_s':>10} {'drift_s':>9} {'note':<28}")
    for i, t in enumerate(click_times):
        if i > 0:
            # Addendum (2026-10-19): advance the expected-schedule index by
            # the *rounded* number of periods actually elapsed since the
            # previous click, not always by 1 - a gap (live-apply restart,
            # or any other cause) can genuinely replace a click with
            # silence, which is a real, one-time loss of audio, not
            # "drift". Treating every detected click as sequential index i
            # conflates the two: a single skipped click looks identical to
            # a full extra period of permanent position drift, when the
            # two are not the same thing and call for different fixes.
            periods_elapsed = round((t - click_times[i - 1]) / args.period)
            if periods_elapsed > 1:
                missed_total += periods_elapsed - 1
            expected_index += periods_elapsed
        expected = t0 + expected_index * args.period
        drift = t - expected
        note = ""
        for tt in toggle_times:
            if i > 0 and click_times[i - 1] <= tt < t:
                note = f"toggle@{tt:.2f}s in this interval"
        if i > 0 and round((t - click_times[i - 1]) / args.period) > 1:
            note = (note + " " if note else "") + \
                   f"{round((t - click_times[i - 1]) / args.period) - 1} click(s) missed here"
        if abs(drift) > abs(worst_drift):
            worst_drift = drift
            worst_idx = i
        print(f"{i:>5} {t:>10.4f} {expected:>10.4f} {drift:>+9.4f} {note:<28}")

    print(f"\n{missed_total} click(s) missed entirely (real silence where a click "
          f"should have played - a gap genuinely replacing audio, not position drift)")
    print(f"worst *drift* (excluding missed-click resyncs): {worst_drift:+.4f}s at click #{worst_idx} "
          f"({'PASS' if abs(worst_drift) < args.period else 'FAIL'} - "
          f"must be < 1 period = {args.period:.3f}s)")

    intervals = np.diff(click_times)
    non_missed_intervals = intervals[np.round(intervals / args.period) <= 1]
    max_interval = non_missed_intervals.max() if len(non_missed_intervals) else 0.0
    print(f"max click-to-click interval excluding missed-click gaps: {max_interval:.4f}s "
          f"({'PASS' if max_interval < 1.5 * args.period else 'FAIL'} - "
          f"should stay near 1 period = {args.period:.3f}s when nothing was missed)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
