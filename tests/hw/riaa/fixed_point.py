#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Bit-exact fixed-point feasibility test using the real Linux recording.

Requires NumPy and SciPy.  It never writes an audio file.  The detailed
fixed-point comparison uses the first ten seconds of the left channel, which
keeps runs quick and preserves comparability with the original investigation.
Clipping statistics cover both channels of the complete input file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy.signal import lfilter

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import pxtools  # noqa: E402

from riaa import coefficients, q30  # noqa: E402


Q = 30
S24_MIN = -(1 << 23)
S24_MAX = (1 << 23) - 1


def round_shift(value: int, bits: int) -> int:
    """Signed round-to-nearest, ties away from zero."""
    if bits == 0:
        return value
    half = 1 << (bits - 1)
    return ((value + half) >> bits if value >= 0
            else -(((-value) + half) >> bits))


def clamp(value: int, low: int, high: int) -> int:
    return high if value > high else low if value < low else value


def df1(samples: np.ndarray, cq: tuple[int, ...], fractional: int = 0,
        error_feedback: bool = False) -> tuple[np.ndarray, int]:
    """Direct Form I with Q30 coefficients and S24 or S24.Q state."""
    b0, b1, b2, a1, a2 = cq
    output = np.empty(len(samples), dtype=np.int64)
    xm1 = xm2 = ym1 = ym2 = residue = 0
    peak_accumulator = 0
    low, high = S24_MIN << fractional, S24_MAX << fractional

    for index, sample in enumerate(samples):
        x = int(sample)
        accumulator = (b0 * x + b1 * xm1 + b2 * xm2) << fractional
        accumulator -= a1 * ym1 + a2 * ym2
        if error_feedback:
            accumulator += residue
        peak_accumulator = max(peak_accumulator, abs(accumulator))
        if not -(1 << 63) <= accumulator < (1 << 63):
            raise OverflowError(f"s64 accumulator overflow at sample {index}")

        raw_y = round_shift(accumulator, Q)
        y = clamp(raw_y, low, high)
        if error_feedback:
            # Do not integrate overload into the error-feedback state.  A
            # clamped output starts again with zero residue; otherwise a long
            # overload could preserve a huge error after the signal recovers.
            residue = accumulator - (y << Q) if y == raw_y else 0
        output[index] = round_shift(y, fractional)
        xm2, xm1 = xm1, x
        ym2, ym1 = ym1, y
    return output, peak_accumulator


def tdf2(samples: np.ndarray, cq: tuple[int, ...],
         fractional: int = 0) -> tuple[np.ndarray, int]:
    """Transposed Direct Form II with Q30 coefficients and S24.Q state."""
    b0, b1, b2, a1, a2 = cq
    output = np.empty(len(samples), dtype=np.int64)
    state1 = state2 = 0
    peak_accumulator = 0
    low, high = S24_MIN << fractional, S24_MAX << fractional

    for index, sample in enumerate(samples):
        x = int(sample) << fractional
        accumulator = b0 * x + state1
        peak_accumulator = max(peak_accumulator, abs(accumulator),
                               abs(state1), abs(state2))
        if not -(1 << 63) <= accumulator < (1 << 63):
            raise OverflowError(f"s64 accumulator overflow at sample {index}")
        y = clamp(round_shift(accumulator, Q), low, high)
        output[index] = round_shift(y, fractional)
        state1 = b1 * x - a1 * y + state2
        state2 = b2 * x - a2 * y
        if not (-(1 << 63) <= state1 < (1 << 63) and
                -(1 << 63) <= state2 < (1 << 63)):
            raise OverflowError(f"s64 state overflow at sample {index}")
    return output, peak_accumulator


def metrics(fixed: np.ndarray, reference: np.ndarray,
            rate: int) -> tuple[float, float, float, float, float]:
    error = fixed.astype(np.float64) - reference
    rms = float(np.sqrt(np.mean(error * error)))
    window = np.hanning(len(error))
    frequencies = np.fft.rfftfreq(len(error), 1.0 / rate)
    band = (frequencies >= 20.0) & (frequencies < 50.0)
    error_spectrum = np.fft.rfft(error * window)
    signal_spectrum = np.fft.rfft(reference * window)
    band_ratio = 10.0 * np.log10(
        np.sum(np.abs(error_spectrum[band]) ** 2) /
        np.sum(np.abs(signal_spectrum[band]) ** 2))
    return (float(np.max(np.abs(error))), rms,
            20.0 * np.log10(rms / (1 << 23)),
            float(band_ratio), float(np.mean(error)))


def dc_test(run, rate: int) -> tuple[float, float, int, int]:
    stimulus = np.concatenate((
        np.zeros(rate, dtype=np.int64),
        np.full(5 * rate, 100000, dtype=np.int64),
        np.zeros(5 * rate, dtype=np.int64),
    ))
    result, _ = run(stimulus)
    steady = result[5 * rate:6 * rate]
    tail = result[-rate:]
    return (float(np.mean(steady)), float(np.mean(tail)),
            int(np.min(tail)), int(np.max(tail)))


def clipping_report(samples: np.ndarray, b: np.ndarray, a: np.ndarray) -> None:
    total_over = 0
    frame_over = np.zeros(len(samples), dtype=bool)
    print("\nfull-file headroom (float reference, 0 dB at 1 kHz)")
    for channel in range(2):
        filtered = lfilter(b, a, samples[:, channel].astype(np.float64))
        over = np.abs(filtered) > S24_MAX
        peak = float(np.max(np.abs(filtered)))
        total_over += int(np.sum(over))
        frame_over |= over
        print(f"ch{channel + 1}: over={np.sum(over)}/{len(over)} "
              f"({100.0 * np.mean(over):.6f}%), "
              f"peak={peak:.3f} ({20.0 * np.log10(peak / (1 << 23)):.3f} dBFS)")
    print(f"both: over sample-values={total_over}/{samples[:, :2].size}; "
          f"frames with either channel over={np.sum(frame_over)}/{len(frame_over)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", help="24-bit stereo (or wider) WAV reference recording to analyse")
    args = parser.parse_args()

    samples, rate, channels, bits = pxtools.read_wav(args.wav)
    if channels < 2 or bits != 24:
        raise SystemExit("expected a stereo-or-wider 24-bit WAV")
    coefs = coefficients(rate)
    cq = q30(coefs)
    b = np.asarray(coefs[:3])
    a = np.asarray((1.0, *coefs[3:]))
    test = samples[:10 * rate, 0].astype(np.int64)
    reference = lfilter(b, a, test.astype(np.float64))

    variants = {
        "DF-I S24": lambda data: df1(data, cq),
        "DF-I S24.Q8": lambda data: df1(data, cq, fractional=8),
        "DF-I error feedback": lambda data: df1(data, cq, error_feedback=True),
        "TDF-II S24": lambda data: tdf2(data, cq),
        "TDF-II S24.Q8": lambda data: tdf2(data, cq, fractional=8),
    }
    operations = {
        "DF-I S24": "5 mul, 4 add/sub, 1 rounded shift; 4 histories",
        "DF-I S24.Q8": "5 mul, 4 add/sub, 3 rounded shifts; 4 histories",
        "DF-I error feedback": "5 mul, 6 add/sub, 2 rounded shifts; 5 histories",
        "TDF-II S24": "5 mul, 4 add/sub, 1 rounded shift; 2 states",
        "TDF-II S24.Q8": "5 mul, 4 add/sub, 3 rounded shifts; 2 states",
    }

    print(f"input={args.wav}, rate={rate}, detailed window=first 10 s, left channel")
    print("float:", " ".join(f"{value:+.12f}" for value in coefs))
    print("Q30: ", " ".join(str(value) for value in cq))
    print("\nvariant | max LSB | RMS LSB | RMS dBFS | 20-50 Hz err/sig | mean err LSB")
    outputs = {}
    for name, run in variants.items():
        fixed, peak_accumulator = run(test)
        outputs[name] = fixed
        maximum, rms, dbfs, low_band, mean = metrics(fixed, reference, rate)
        steady, tail_mean, tail_min, tail_max = dc_test(run, rate)
        print(f"{name}: {maximum:.6f} | {rms:.6f} | {dbfs:.3f} | "
              f"{low_band:.3f} | {mean:+.6f}; peak_acc={peak_accumulator}; "
              f"DC100k={steady:.6f}, silence tail={tail_mean:+.6f} "
              f"[{tail_min},{tail_max}]; {operations[name]}")

    print("\nform equivalence:")
    for suffix in ("S24", "S24.Q8"):
        left = outputs[f"DF-I {suffix}"]
        right = outputs[f"TDF-II {suffix}"]
        print(f"DF-I/TDF-II {suffix}: differing samples={np.sum(left != right)}, "
              f"max difference={np.max(np.abs(left - right))} LSB")
    clipping_report(samples, b, a)


if __name__ == "__main__":
    main()
