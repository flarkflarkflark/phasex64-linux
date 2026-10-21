# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Test 1: channel map. For each playback channel 1-4, play a 1 kHz sine
on just that channel and record all 6 inputs, then report RMS/dominant
frequency per input channel."""
import subprocess
import sys
import time
import json
import pxtools as px

RATE = 48000
PLAY_DUR = 3.0
REC_DUR = 5.0
DEV = "hw:PhaseX64"
DATA = "data"

results = []

for ch in range(4):
    play_path = f"{DATA}/chmap_play_ch{ch + 1}.wav"
    rec_path = f"{DATA}/chmap_rec_ch{ch + 1}.wav"
    px.gen_tone_file(play_path, RATE, PLAY_DUR, 4, 24, active_ch=ch,
                      freq=1000.0, dbfs=-12.0)

    rec = subprocess.Popen(
        ["arecord", "-D", DEV, "-f", "S32_LE", "-c", "6", "-r", str(RATE),
         "-d", str(int(REC_DUR)), rec_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.8)  # let capture actually start before we play

    play = subprocess.run(
        ["aplay", "-D", DEV, play_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if play.returncode != 0:
        print(f"ch{ch+1}: aplay FAILED: {play.stderr}", file=sys.stderr)

    rec_out, rec_err = rec.communicate(timeout=REC_DUR + 5)
    if rec.returncode != 0:
        print(f"ch{ch+1}: arecord FAILED: {rec_err}", file=sys.stderr)

    samples, rate, nch, bits = px.read_wav(rec_path)
    # drop first/last 0.3s to avoid onset/offset transients
    margin = int(0.3 * rate)
    core = samples[margin:-margin] if len(samples) > 2 * margin else samples

    per_ch = []
    for inch in range(nch):
        rms, freq = px.analyze_channel(core[:, inch], rate)
        per_ch.append({
            "input_ch": inch + 1,
            "rms_dbfs": round(px.dbfs(rms, bits), 2),
            "dominant_hz": round(freq, 2),
        })
    results.append({
        "output_ch": ch + 1,
        "play_stderr": play.stderr.strip(),
        "arecord_stderr": rec_err.strip(),
        "inputs": per_ch,
    })
    print(f"output ch{ch+1} done: " +
          ", ".join(f"in{p['input_ch']}={p['rms_dbfs']}dBFS@{p['dominant_hz']}Hz"
                    for p in per_ch))

with open(f"{DATA}/channel_map_results.json", "w") as f:
    json.dump(results, f, indent=2)
print("wrote", f"{DATA}/channel_map_results.json")
