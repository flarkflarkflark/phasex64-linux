# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Test 2: clock/rate. Loopback on playback ch3 -> capture ch3 (confirmed by
channel_map.py) at each native rate; measure the recorded frequency and
capture hw_params while the stream is running."""
import subprocess
import time
import json
import pxtools as px

DEV = "hw:PhaseX64"
DATA = "data"
RATES = [44100, 48000, 88200, 96000]
PLAY_DUR = 4.0
REC_DUR = 6.0
HWP_PATH = "/proc/asound/PhaseX64/pcm0p/sub0/hw_params"

results = []

for rate in RATES:
    play_path = f"{DATA}/rate_play_{rate}.wav"
    rec_path = f"{DATA}/rate_rec_{rate}.wav"
    px.gen_tone_file(play_path, rate, PLAY_DUR, 4, 24, active_ch=2,
                      freq=1000.0, dbfs=-12.0)

    rec = subprocess.Popen(
        ["arecord", "-D", DEV, "-f", "S32_LE", "-c", "6", "-r", str(rate),
         "-d", str(int(REC_DUR)), rec_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.8)

    play = subprocess.Popen(
        ["aplay", "-D", DEV, play_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1.2)  # let it actually get streaming before reading hw_params

    try:
        with open(HWP_PATH) as f:
            hwp = f.read().strip()
    except OSError as e:
        hwp = f"<read failed: {e}>"

    play_out, play_err = play.communicate(timeout=PLAY_DUR + 5)
    rec_out, rec_err = rec.communicate(timeout=REC_DUR + 5)

    samples, srate, nch, bits = px.read_wav(rec_path)
    margin = int(0.3 * srate)
    core = samples[margin:-margin] if len(samples) > 2 * margin else samples
    rms, freq = px.analyze_channel(core[:, 2], srate)  # capture ch3, 0-based idx 2

    within_tol = bool(abs(freq - 1000.0) <= 0.5)
    results.append({
        "rate": rate,
        "hw_params_during_playback": hwp,
        "measured_freq_hz": round(float(freq), 3),
        "rms_dbfs": round(float(px.dbfs(rms, bits)), 2),
        "within_tolerance": within_tol,
        "play_stderr": play_err.strip(),
        "rec_stderr": rec_err.strip(),
    })
    print(f"rate={rate}: freq={freq:.3f}Hz rms={px.dbfs(rms, bits):.2f}dBFS "
          f"within_tol={within_tol}")
    if play.returncode != 0:
        print(f"  aplay FAILED: {play_err}")
    if rec.returncode != 0:
        print(f"  arecord FAILED: {rec_err}")

with open(f"{DATA}/rate_test_results.json", "w") as f:
    json.dump(results, f, indent=2)
print("wrote", f"{DATA}/rate_test_results.json")
