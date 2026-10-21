# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""MIDI loopback stress test (phasex64, v0.1.11.x).

Sends a known MIDI message N times over the physical 5-pin DIN loopback
cable (MIDI Out -> MIDI In) while a PCM stream is running, reads back
whatever actually arrives each time, and reports how often the received
bytes exactly match what was sent. Written to pin down the "sporadic
1-2 bytes lost at the start of a burst" symptom from tests/hw/REPORT.md
"MIDI loopback diagnosis, round 2" with a number instead of a handful of
one-off captures - run once against a baseline build and once against a
candidate fix, and compare the two loss rates.

Requires the 5-pin DIN loopback cable connected (MIDI Out -> MIDI In)
and the snd-usb-phasex64 module already loaded - this script does not
load/unload the module or touch ALSA mixer state. It only calls `amidi`
and (briefly) starts/stops `arecord` to keep a PCM stream running, since
MIDI doesn't work at all without one (see tests/hw/REPORT.md "MIDI
loopback diagnosis").

Do not run this inside a non-hardware session: it opens the real MIDI
port and sends real bytes over the real cable, same rules as
tests/hw/control_sweep.py and mixer_test.py.

Usage:
    python3 tests/hw/midi_stress.py [-n 50] [-t 1.0] [-p hw:PhaseX64,0,0]
                                     [-o data/midi_stress_results.json]

Exit status is 0 if every message round-tripped exactly, 1 otherwise -
suitable for comparing two builds with a one-line result each.
"""
import argparse
import json
import os
import subprocess
import sys
import time

CARD = "PhaseX64"

# (name, hex bytes as sent to `amidi -S`)
MESSAGES = [
    ("note_on_off", "90 3C 7F 80 3C 00"),
    ("sysex", "F0 7D 01 02 03 04 05 06 07 08 F7"),
]


def amidi_l_mentions_phasex64(amidi_l_output):
    """True if `amidi -l`'s output names this device. `amidi -l` lists
    the MIDI *port* name ("PHASE X64 USB MIDI"), not the ALSA card id
    ("PhaseX64") that -p/hw:PhaseX64,... uses - those are two different
    strings, so this checks for the port name, case-insensitively (seen
    as "PHASE X64 USB MIDI" in practice; be lenient on case regardless).
    """
    return "phase x64" in amidi_l_output.lower()


def check_card_present(port_is_explicit):
    """Sanity-check the device is actually there before running a long
    stress test against it - but only when there's something to check:
    if the caller passed -p explicitly, trust it outright (so e.g. an
    unusual card index, or a /proc-only unnamed port, still works), and
    otherwise accept either `amidi -l` naming this device or its
    /proc/asound/<CARD> directory existing - the two independent ways
    the device can be confirmed present."""
    if port_is_explicit:
        return
    out = subprocess.run(["amidi", "-l"], capture_output=True, text=True).stdout
    if amidi_l_mentions_phasex64(out):
        return
    if os.path.exists(f"/proc/asound/{CARD}"):
        return
    print(f"error: {CARD} not found in `amidi -l` output "
          f"(got: {out!r}) or /proc/asound/{CARD} - is the card present "
          f"and the module loaded? Pass -p explicitly to skip this check.",
          file=sys.stderr)
    sys.exit(2)


def hex_to_bytes(hexstr):
    return bytes(int(b, 16) for b in hexstr.split())


def run_one(port, send_hex, timeout, recv_path, settle):
    """Arm a fresh receiver, send once, return the raw bytes actually
    received (possibly empty)."""
    if os.path.exists(recv_path):
        os.remove(recv_path)

    recv = subprocess.Popen(["amidi", "-p", port, "-r", recv_path,
                              "-t", str(timeout)])
    time.sleep(settle)  # let the port actually open/arm before sending
    subprocess.run(["amidi", "-p", port, "-S", send_hex], check=True)
    recv.wait()

    if os.path.exists(recv_path):
        with open(recv_path, "rb") as f:
            data = f.read()
        os.remove(recv_path)
        return data
    return b""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-n", "--count", type=int, default=50,
                     help="iterations per message type (default 50)")
    ap.add_argument("-t", "--timeout", type=float, default=1.0,
                     help="amidi -r listen timeout per iteration, seconds (default 1.0)")
    ap.add_argument("-p", "--port", default=None,
                     help="amidi port, e.g. hw:PhaseX64,0,0 or hw:0,0 "
                          "(default: hw:PhaseX64,0,0; passing this explicitly "
                          "also skips the `amidi -l` presence check, so an "
                          "unusual index still works)")
    ap.add_argument("--settle", type=float, default=0.1,
                     help="delay between arming the receiver and sending, seconds")
    ap.add_argument("--stream-rate", type=int, default=48000)
    ap.add_argument("-o", "--output", default="data/midi_stress_results.json")
    ap.add_argument("--no-stream", action="store_true",
                     help="skip starting the PCM stream (expect total loss - see REPORT.md)")
    args = ap.parse_args()

    check_card_present(port_is_explicit=args.port is not None)
    if args.port is None:
        args.port = "hw:PhaseX64,0,0"

    recv_path = "/tmp/midi_stress_recv.bin"
    total_iters = len(MESSAGES) * args.count
    stream_duration = int(total_iters * (args.timeout + args.settle + 0.3) + 5)

    stream = None
    if not args.no_stream:
        print(f"starting arecord for {stream_duration}s to keep a PCM stream running "
              f"(MIDI needs one - see tests/hw/REPORT.md)")
        stream = subprocess.Popen(
            ["arecord", "-D", f"hw:{CARD}", "-f", "S32_LE", "-r", str(args.stream_rate),
             "-c", "6", "-d", str(stream_duration), "/dev/null"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)  # let px_start_streaming() settle before the first send

    results = {"port": args.port, "count": args.count, "timeout": args.timeout,
               "stream": not args.no_stream, "messages": {}}
    overall_ok = True

    try:
        for name, send_hex in MESSAGES:
            expect = hex_to_bytes(send_hex)
            matches = 0
            iterations = []

            for i in range(args.count):
                got = run_one(args.port, send_hex, args.timeout, recv_path, args.settle)
                ok = (got == expect)
                matches += ok
                iterations.append({
                    "i": i,
                    "sent": send_hex,
                    "received": got.hex(" "),
                    "match": ok,
                })
                if not ok:
                    print(f"  {name} #{i}: MISMATCH sent=[{send_hex}] "
                          f"received=[{got.hex(' ')}]")
                time.sleep(args.settle)

            loss = args.count - matches
            loss_rate = loss / args.count if args.count else 0.0
            results["messages"][name] = {
                "sent_hex": send_hex,
                "matches": matches,
                "total": args.count,
                "loss": loss,
                "loss_rate": loss_rate,
                "iterations": iterations,
            }
            print(f"{name}: {matches}/{args.count} exact matches, "
                  f"loss rate {loss_rate:.1%}")
            if loss:
                overall_ok = False
    finally:
        if stream:
            stream.terminate()
            stream.wait()

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(results, f, indent=2)
    print(f"wrote {args.output}")

    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
