# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Test 3: glitch detection. 5 minutes full duplex at 48000, continuous
997 Hz on playback ch3, recorded on capture ch3 (per channel_map.py).
Detects discontinuities by comparing each 10ms block's fitted phase
against the phase predicted from block 0 + the known frequency - a
dropped/inserted sample or a dropout shows up as a phase-error outlier."""
import subprocess
import sys
import time
import json
import numpy as np
import pxtools as px

DEV = "hw:PhaseX64"
DATA = "data"
RATE = 48000
DURATION_S = 300
FREQ = 997.0
BLOCK = 480  # 10 ms at 48kHz

play_path = f"{DATA}/glitch_play.wav"
rec_path = f"{DATA}/glitch_rec.wav"

print("generating 5-minute test tone...")
px.gen_tone_file(play_path, RATE, DURATION_S, 4, 24, active_ch=2,
                  freq=FREQ, dbfs=-12.0)

rec = subprocess.Popen(
    ["arecord", "-v", "-D", DEV, "-f", "S32_LE", "-c", "6", "-r", str(RATE),
     "-d", str(DURATION_S + 2), rec_path],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
time.sleep(0.8)

play = subprocess.Popen(
    ["aplay", "-v", "-D", DEV, play_path],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

print(f"running for {DURATION_S}s...")
play_out, play_err = play.communicate(timeout=DURATION_S + 30)
rec_out, rec_err = rec.communicate(timeout=DURATION_S + 30)

if play.returncode != 0:
    print(f"aplay FAILED: {play_err}", file=sys.stderr)
if rec.returncode != 0:
    print(f"arecord FAILED: {rec_err}", file=sys.stderr)

xrun_lines = [l for l in (play_err + rec_err).splitlines()
              if 'overrun' in l.lower() or 'underrun' in l.lower() or
                 'xrun' in l.lower()]

samples, srate, nch, bits = px.read_wav(rec_path)
margin = int(0.5 * srate)
core = samples[margin:-margin] if len(samples) > 2 * margin else samples
x = core[:, 2].astype(np.float64)  # capture ch3

n_blocks = len(x) // BLOCK
x = x[:n_blocks * BLOCK].reshape(n_blocks, BLOCK)
w = 2 * np.pi * FREQ / RATE
t = np.arange(BLOCK)
cos_ref = np.cos(w * t)
sin_ref = np.sin(w * t)
a = 2 * np.mean(x * cos_ref, axis=1)
b = 2 * np.mean(x * sin_ref, axis=1)
amp = np.hypot(a, b)
phase = np.arctan2(b, a)

block_starts = np.arange(n_blocks) * BLOCK
expected = np.mod(phase[0] + w * block_starts, 2 * np.pi)
expected_wrapped = np.mod(expected + np.pi, 2 * np.pi) - np.pi
phase_err = np.mod(phase - expected_wrapped + np.pi, 2 * np.pi) - np.pi

med_err = np.median(phase_err)
med_amp_db = np.median(20 * np.log10(np.maximum(amp, 1e-9)))
amp_db = 20 * np.log10(np.maximum(amp, 1e-9))

PHASE_GLITCH_RAD = 0.3   # ~2.3 samples of phase error at 997Hz/48kHz
AMP_GLITCH_DB = 6.0

phase_glitch = np.abs(phase_err - med_err) > PHASE_GLITCH_RAD
amp_glitch = np.abs(amp_db - med_amp_db) > AMP_GLITCH_DB
glitch = phase_glitch | amp_glitch
glitch_idx = np.where(glitch)[0]
glitch_times = (block_starts[glitch_idx] / srate) + (margin / srate)

results = {
    "duration_s": DURATION_S,
    "rate": RATE,
    "freq": FREQ,
    "n_blocks_analyzed": int(n_blocks),
    "median_amp_dbfs": round(float(px.dbfs(np.median(amp), bits)), 3),
    "glitch_count": int(len(glitch_idx)),
    "glitch_timestamps_s": [round(float(tt), 3) for tt in glitch_times[:200]],
    "xrun_lines": xrun_lines,
    "play_returncode": play.returncode,
    "rec_returncode": rec.returncode,
}

with open(f"{DATA}/glitch_test_results.json", "w") as f:
    json.dump(results, f, indent=2)

print(f"blocks analyzed: {n_blocks}")
print(f"glitch count: {len(glitch_idx)}")
print(f"xrun lines: {len(xrun_lines)}")
for l in xrun_lines[:20]:
    print("  ", l)
print("wrote", f"{DATA}/glitch_test_results.json")
