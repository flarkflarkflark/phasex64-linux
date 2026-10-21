# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Re-analysis of an existing glitch_test.py capture using a global
least-squares fit (amplitude+phase fit over the whole recording) with
residual-RMS-per-block outlier detection, instead of the broken per-block
phase-unwrapping approach in the original glitch_test.py."""
import sys
import json
import numpy as np
import pxtools as px

DATA = "data"
RATE = 48000
FREQ = 997.0
BLOCK = 480  # 10 ms

rec_path = f"{DATA}/glitch_rec.wav"
samples, srate, nch, bits = px.read_wav(rec_path)
assert srate == RATE

margin = int(1.5 * srate)
core = samples[margin:-margin] if len(samples) > 2 * margin else samples
x = core[:, 2].astype(np.float64)  # capture ch3

w = 2 * np.pi * FREQ / RATE
n = np.arange(len(x))
cos_g = np.cos(w * n)
sin_g = np.sin(w * n)
A = 2 * np.mean(x * cos_g)
B = 2 * np.mean(x * sin_g)
ref = A * cos_g + B * sin_g
resid = x - ref

n_blocks = len(x) // BLOCK
resid_b = resid[:n_blocks * BLOCK].reshape(n_blocks, BLOCK)
rms_b = np.sqrt(np.mean(resid_b ** 2, axis=1))
block_starts = np.arange(n_blocks) * BLOCK

med = np.median(rms_b)
mad = np.median(np.abs(rms_b - med)) + 1e-12
threshold = med + 10 * mad
glitch_idx = np.where(rms_b > threshold)[0]
glitch_times = (block_starts[glitch_idx] / srate) + (margin / srate)

signal_amp = np.hypot(A, B)
signal_dbfs = px.dbfs(signal_amp, bits)

results = {
    "method": "global least-squares fit + residual RMS per 10ms block, "
              "outlier = median + 10*MAD",
    "duration_s_analyzed": round(len(x) / srate, 1),
    "n_blocks_analyzed": int(n_blocks),
    "fitted_signal_dbfs": round(float(signal_dbfs), 2),
    "residual_rms_median": float(med),
    "residual_rms_median_db_below_signal": round(
        float(20 * np.log10(med / max(signal_amp, 1e-9))), 1),
    "residual_rms_max": float(rms_b.max()),
    "threshold": float(threshold),
    "glitch_count": int(len(glitch_idx)),
    "glitch_timestamps_s": [round(float(t), 3) for t in glitch_times[:500]],
}

with open(f"{DATA}/glitch_test_results.json", "w") as f:
    json.dump(results, f, indent=2)

print(f"blocks analyzed: {n_blocks}")
print(f"fitted signal level: {signal_dbfs:.2f} dBFS")
print(f"residual rms median: {med:.4f} ({results['residual_rms_median_db_below_signal']} dB below signal)")
print(f"residual rms max: {rms_b.max():.4f}")
print(f"threshold: {threshold:.4f}")
print(f"glitch count: {len(glitch_idx)}")
for t in glitch_times[:20]:
    print("  glitch at", round(float(t), 3), "s")
print("wrote", f"{DATA}/glitch_test_results.json")
