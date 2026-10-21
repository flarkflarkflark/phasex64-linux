# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Test 4: stress, the old crash pattern.
a) continuous 4ch playback + 200x capture open/close
b) continuous 6ch capture + 200x playback open/close
c) 50x both directions opened/closed simultaneously
This is exactly the duplex-open pattern that triggered the v0.1.1 WARN
(second direction's sub never set) - the most important regression test
for the v0.1.2/v0.1.3 fixes.

Usage: python3 stress_test.py a|b|c   (run one stage, results merged into
the shared JSON so the kernel log can be checked between stages)
"""
import subprocess
import sys
import time
import json
import os
import pxtools as px

DEV = "hw:PhaseX64"
DATA = "data"
RATE = 48000
N_CYCLES_AB = 200
N_CYCLES_C = 50
HOLD_S = 0.15

play_path = f"{DATA}/stress_play.wav"
cont_play_path = f"{DATA}/stress_cont_play.wav"
results_path = f"{DATA}/stress_test_results.json"


def quick_cycle(cmd, hold_s=HOLD_S):
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, text=True)
    time.sleep(hold_s)
    p.terminate()
    try:
        _, err = p.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        _, err = p.communicate()
    return p.returncode, (err or "").strip()


def errorish(err):
    low = err.lower()
    return ('error' in low and 'no error' not in low) or 'resubmit' in low


def stage_a():
    print("stress (a): continuous 4ch playback + 200x capture open/close")
    cont = subprocess.Popen(["aplay", "-D", DEV, cont_play_path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    time.sleep(0.5)
    errs = []
    for i in range(N_CYCLES_AB):
        rc, err = quick_cycle(["arecord", "-D", DEV, "-f", "S32_LE", "-c", "6",
                                "-r", str(RATE), "-d", "5", "/dev/null"])
        if errorish(err):
            errs.append((i, err))
    died_early = cont.poll() is not None
    cont.terminate()
    _, cont_err = cont.communicate(timeout=5)
    r = {
        "cycles": N_CYCLES_AB,
        "error_count": len(errs),
        "errors": errs[:20],
        "continuous_stream_died_early": died_early,
        "continuous_stream_stderr": cont_err.strip(),
    }
    print(f"  errors: {len(errs)}, continuous died early: {died_early}")
    return r


def stage_b():
    print("stress (b): continuous 6ch capture + 200x playback open/close")
    cont = subprocess.Popen(["arecord", "-D", DEV, "-f", "S32_LE", "-c", "6",
                              "-r", str(RATE), "-d", "180", "/dev/null"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    time.sleep(0.5)
    errs = []
    for i in range(N_CYCLES_AB):
        rc, err = quick_cycle(["aplay", "-D", DEV, play_path])
        if errorish(err):
            errs.append((i, err))
    died_early = cont.poll() is not None
    cont.terminate()
    _, cont_err = cont.communicate(timeout=5)
    r = {
        "cycles": N_CYCLES_AB,
        "error_count": len(errs),
        "errors": errs[:20],
        "continuous_stream_died_early": died_early,
        "continuous_stream_stderr": cont_err.strip(),
    }
    print(f"  errors: {len(errs)}, continuous died early: {died_early}")
    return r


def stage_c():
    print("stress (c): 50x both directions opened/closed simultaneously")
    errs = []
    hangs = 0
    for i in range(N_CYCLES_C):
        rec = subprocess.Popen(["arecord", "-D", DEV, "-f", "S32_LE", "-c", "6",
                                 "-r", str(RATE), "-d", "5", "/dev/null"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        play = subprocess.Popen(["aplay", "-D", DEV, play_path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        time.sleep(HOLD_S)
        rec.terminate()
        play.terminate()
        try:
            _, rec_err = rec.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            rec.kill()
            _, rec_err = rec.communicate()
            hangs += 1
        try:
            _, play_err = play.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            play.kill()
            _, play_err = play.communicate()
            hangs += 1
        if errorish(rec_err or "") or errorish(play_err or ""):
            errs.append((i, (rec_err or "").strip(), (play_err or "").strip()))
    r = {
        "cycles": N_CYCLES_C,
        "error_count": len(errs),
        "errors": errs[:20],
        "hangs_requiring_kill": hangs,
    }
    print(f"  errors: {len(errs)}, hangs: {hangs}")
    return r


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "abc"
    px.gen_tone_file(play_path, RATE, 5.0, 4, 24, active_ch=0, freq=440.0, dbfs=-18.0)
    px.gen_tone_file(cont_play_path, RATE, 180.0, 4, 24, active_ch=None, freq=200.0, dbfs=-18.0)

    results = json.load(open(results_path)) if os.path.exists(results_path) else {}
    if "a" in stage:
        results["a_continuous_play_200x_capture"] = stage_a()
    if "b" in stage:
        results["b_continuous_capture_200x_playback"] = stage_b()
    if "c" in stage:
        results["c_50x_simultaneous_both_directions"] = stage_c()

    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)
    print("wrote", results_path)
