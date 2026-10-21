# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""v0.1.4 Task 2: verify the Capture Source enum assumption. Plays a
distinct tone per playback pair (ch1/2=440Hz, ch3/4=1kHz) and, for each
item of In 1/2 Capture Source and In 3/4 Capture Source in turn, records
all 6 capture channels and reports which tone (if any) arrives where.
The other pair's source is held at "Mute" while sweeping one pair, to
keep the two measurements from interfering with each other."""
import subprocess
import time
import json
import pxtools as px

DEV = "hw:PhaseX64"
DATA = "data"
RATE = 48000
PLAY_DUR = 3.0
REC_DUR = 5.0

IN_12 = "In 1/2 Capture Source"
IN_34 = "In 3/4 Capture Source"
ITEMS_12 = ["Mic-HiZ-Line", "Phono", "Aux", "Loopback 3/4", "Sound Module", "Mute"]
ITEMS_34 = ["Phono", "Aux", "Loopback 1/2", "Sound Module", "Mute"]
MUTE_12 = ITEMS_12.index("Mute")
MUTE_34 = ITEMS_34.index("Mute")


def cget(numid_name):
    out = subprocess.run(["amixer", "-c", "PhaseX64", "cget", "name=" + numid_name],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if ": values=" in line:
            return line.split("values=", 1)[1].strip()
    return None


def cset(name, value):
    subprocess.run(["amixer", "-c", "PhaseX64", "-q", "cset", "name=" + name, value],
                   check=True)


def record_once(tag):
    play_path = f"{DATA}/enumv_play.wav"
    rec_path = f"{DATA}/enumv_rec_{tag}.wav"

    rec = subprocess.Popen(
        ["arecord", "-D", DEV, "-f", "S32_LE", "-c", "6", "-r", str(RATE),
         "-d", str(int(REC_DUR)), rec_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    time.sleep(0.8)
    play = subprocess.run(["aplay", "-D", DEV, play_path],
                          capture_output=True, text=True)
    _, rec_err = rec.communicate(timeout=REC_DUR + 5)

    samples, srate, nch, bits = px.read_wav(rec_path)
    margin = int(0.5 * srate)
    core = samples[margin:-margin] if len(samples) > 2 * margin else samples

    per_ch = []
    for ch in range(nch):
        rms, freq = px.analyze_channel(core[:, ch], srate)
        dbfs = px.dbfs(rms, bits)
        label = "silence"
        if dbfs > -40:
            if abs(freq - 440) < 3:
                label = "440Hz"
            elif abs(freq - 1000) < 3:
                label = "1000Hz"
            else:
                label = f"tone@{freq:.0f}Hz?"
        per_ch.append({"ch": ch + 1, "dbfs": round(float(dbfs), 1),
                       "freq": round(float(freq), 1), "label": label})
    ok = play.returncode == 0 and rec.returncode == 0
    return per_ch, ok, play.stderr.strip(), rec_err.strip()


def main():
    print("generating dual-tone file: ch1/2=440Hz ch3/4=1000Hz @ -12dBFS...")
    n = int(RATE * PLAY_DUR)
    import numpy as np
    t = np.arange(n) / RATE
    amp = px.full_scale(24) * (10 ** (-12 / 20.0))
    out = np.zeros((n, 4), dtype=np.int32)
    out[:, 0] = (amp * np.sin(2 * np.pi * 440.0 * t)).astype(np.int32)
    out[:, 1] = (amp * np.sin(2 * np.pi * 440.0 * t)).astype(np.int32)
    out[:, 2] = (amp * np.sin(2 * np.pi * 1000.0 * t)).astype(np.int32)
    out[:, 3] = (amp * np.sin(2 * np.pi * 1000.0 * t)).astype(np.int32)
    px.write_wav(f"{DATA}/enumv_play.wav", out, RATE, 24)

    baseline_12 = cget(IN_12)
    baseline_34 = cget(IN_34)
    print(f"baseline: {IN_12}={baseline_12} ({ITEMS_12[int(baseline_12)]}), "
          f"{IN_34}={baseline_34} ({ITEMS_34[int(baseline_34)]})")

    results = {"in_12": [], "in_34": []}

    print(f"\n=== sweeping {IN_12} (holding {IN_34}=Mute) ===")
    cset(IN_34, str(MUTE_34))
    for i, item in enumerate(ITEMS_12):
        cset(IN_12, str(i))
        per_ch, ok, perr, rerr = record_once(f"12_{i}")
        results["in_12"].append({"item": item, "per_channel": per_ch, "ok": ok})
        summary = ", ".join(f"ch{c['ch']}={c['label']}" for c in per_ch
                            if c["label"] != "silence")
        print(f"  {item:15s}: {summary or '(all silent)'}")
        if not ok:
            print(f"    aplay_err={perr!r} arecord_err={rerr!r}")
    cset(IN_12, baseline_12)

    print(f"\n=== sweeping {IN_34} (holding {IN_12}=Mute) ===")
    cset(IN_12, str(MUTE_12))
    for i, item in enumerate(ITEMS_34):
        cset(IN_34, str(i))
        per_ch, ok, perr, rerr = record_once(f"34_{i}")
        results["in_34"].append({"item": item, "per_channel": per_ch, "ok": ok})
        summary = ", ".join(f"ch{c['ch']}={c['label']}" for c in per_ch
                            if c["label"] != "silence")
        print(f"  {item:15s}: {summary or '(all silent)'}")
        if not ok:
            print(f"    aplay_err={perr!r} arecord_err={rerr!r}")
    cset(IN_34, baseline_34)

    cset(IN_12, baseline_12)
    cset(IN_34, baseline_34)
    final_12, final_34 = cget(IN_12), cget(IN_34)
    print(f"\nrestored: {IN_12}={final_12}, {IN_34}={final_34} "
          f"(ok={final_12 == baseline_12 and final_34 == baseline_34})")

    with open(f"{DATA}/enum_verify_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("wrote", f"{DATA}/enum_verify_results.json")


if __name__ == "__main__":
    main()
