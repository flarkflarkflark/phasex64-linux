#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Analyze USBPcap traces of the PHASE X64 USB (0a4a:5b01) with tshark.

Ground truth for the capture-throughput investigation: what the official
Windows driver actually sends/receives, compared to snd-usb-phasex64.

Per file:
  * device detection (bus/device address via the 0a4a:5b01 descriptor,
    fallback: the device that does bulk-IN on 0x86); override with
    --bus/--addr
  * every control transfer in order (bmRequestType, bRequest, wValue,
    wIndex, wLength, data, status), decoded where known: status byte 0x49,
    WM8776 writes 0x41, SET_CUR/GET_CUR sample rate, SET_INTERFACE /
    SET_CONFIGURATION, CLEAR_FEATURE; plus pipe operations (abort/reset)
  * per endpoint: bytes/s, URBs/s, transfer/packet sizes, a 1 s timeline
  * isoc OUT 0x02: packets per URB, packet sizes -> frames per microframe
  * feedback 0x81: first-byte values (frames per ms)
  * bulk IN 0x86: throughput, completion sizes, frames/s at 64 B/frame
  * the stream-start sequence around the first 0x86 transfer, next to the
    start sequence parsed from linux/snd-usb-phasex64/phasex64.c
With several files: a side-by-side summary table.
With --wav: duration/rate/levels/dominant frequency of REAPER recordings.

Usage:
  py tests/hw/pcap_analyze.py tests/hw/captures/asio_small.pcapng
  py tests/hw/pcap_analyze.py a.pcapng b.pcapng c.pcapng --json out.json
  py tests/hw/pcap_analyze.py --wav tests/hw/captures/*.wav
"""

import argparse
import collections
import json
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys

VID, PID = 0x0A4A, 0x5B01
EP_PLAY, EP_FB, EP_CAP, EP_MIDI_IN, EP_MIDI_OUT = 0x02, 0x81, 0x86, 0x83, 0x04
CAP_WIRE_FRAME = 64
PLAY_FRAME = 12

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DRIVER_C = os.path.join(REPO, "linux", "snd-usb-phasex64", "phasex64.c")

URB_FUNCTIONS = {
    0x0000: "SELECT_CONFIGURATION", 0x0001: "SELECT_INTERFACE",
    0x0002: "ABORT_PIPE", 0x0008: "CONTROL_TRANSFER",
    0x0009: "BULK_OR_INTERRUPT_TRANSFER", 0x000A: "ISOCH_TRANSFER",
    0x000B: "GET_DESCRIPTOR_FROM_DEVICE", 0x0017: "VENDOR_DEVICE",
    0x0018: "VENDOR_INTERFACE", 0x0019: "VENDOR_ENDPOINT",
    0x001A: "CLASS_DEVICE", 0x001B: "CLASS_INTERFACE",
    0x001C: "CLASS_ENDPOINT", 0x001E: "SYNC_RESET_PIPE_AND_CLEAR_STALL",
    0x001F: "CLASS_OTHER", 0x0020: "VENDOR_OTHER",
    0x0028: "GET_DESCRIPTOR_FROM_INTERFACE", 0x0030: "SYNC_RESET_PIPE",
    0x0031: "SYNC_CLEAR_STALL", 0x0032: "CONTROL_TRANSFER_EX",
}
PIPE_OPS = {0x0002, 0x001E, 0x0030, 0x0031}

FIELDS = [
    "frame.number", "frame.time_relative", "usb.bus_id", "usb.device_address",
    "usb.endpoint_address", "usb.transfer_type", "usb.irp_info.direction",
    "usb.irp_id", "usb.function", "usb.usbd_status", "usb.data_len",
    "usb.control_stage", "usb.bmRequestType", "usb.setup.bRequest",
    "usb.setup.wValue", "usb.setup.wIndex", "usb.setup.wLength",
    "usb.capdata", "usb.data_fragment", "usb.control.Response",
    "usb.win32.iso_num_packets", "usb.win32.iso_offset",
    "usb.win32.iso_data_len", "usb.win32.iso_status", "usb.iso.data",
]


# ---------------------------------------------------------------------------
# tshark

def find_tshark(explicit=None):
    for cand in (explicit, shutil.which("tshark"),
                 r"C:\Program Files\Wireshark\tshark.exe"):
        if cand and os.path.isfile(cand):
            return cand
    sys.exit("tshark not found (install Wireshark or pass --tshark)")


def run_tshark(tshark, path, display_filter, fields):
    cmd = [tshark, "-r", path, "-T", "fields", "-E", "header=n",
           "-E", "separator=\t", "-E", "occurrence=a", "-E", "aggregator=|",
           "-E", "quote=n"]
    if display_filter:
        cmd += ["-Y", display_filter]
    for f in fields:
        cmd += ["-e", f]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        if "cut short" in res.stderr and res.stdout:
            # copy of a capture that is still being written
            print("warning: %s is truncated, using the complete packets"
                  % path, file=sys.stderr)
        else:
            sys.exit("tshark failed on %s:\n%s" % (path, res.stderr.strip()))
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        parts += [""] * (len(fields) - len(parts))
        yield dict(zip(fields, parts))


def num(s, default=None):
    if s is None or s == "":
        return default
    s = s.split("|")[0]
    try:
        return int(s, 0)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return default


def nums(s):
    return [num(x) for x in s.split("|")] if s else []


def hexbytes(*vals):
    for v in vals:
        if v:
            return bytes.fromhex(v.split("|")[0].replace(":", ""))
    return b""


# ---------------------------------------------------------------------------
# device detection and parsing

def find_device(tshark, path):
    hits = collections.Counter()
    for r in run_tshark(tshark, path,
                        "usb.idVendor == 0x%04x && usb.idProduct == 0x%04x"
                        % (VID, PID), ["usb.bus_id", "usb.device_address"]):
        hits[(num(r["usb.bus_id"]), num(r["usb.device_address"]))] += 1
    if hits:
        return hits.most_common(1)[0][0], "descriptor"
    for r in run_tshark(tshark, path,
                        "usb.endpoint_address == 0x%02x && usb.transfer_type == 3"
                        % EP_CAP, ["usb.bus_id", "usb.device_address"]):
        hits[(num(r["usb.bus_id"]), num(r["usb.device_address"]))] += 1
    if hits:
        return hits.most_common(1)[0][0], "bulk-IN 0x86 heuristic"
    return None, None


class Trace:
    def __init__(self, path):
        self.path = path
        self.duration = 0.0
        self.controls = []          # completed control transfers
        self.pipe_ops = []          # (t, function, ep)
        self.urbs = collections.defaultdict(list)   # ep -> list of dicts
        self.first_submit = {}      # ep -> t


def parse(tshark, path, bus, addr):
    tr = Trace(path)
    open_ctl = {}
    flt = "usb.bus_id == %d && usb.device_address == %d" % (bus, addr)
    for r in run_tshark(tshark, path, flt, FIELDS):
        t = num(r["frame.time_relative"], 0.0)
        tr.duration = max(tr.duration, t)
        ep = num(r["usb.endpoint_address"], 0)
        ttype = num(r["usb.transfer_type"], -1)
        done = num(r["usb.irp_info.direction"], 0) == 1   # PDO->FDO completion
        irp = r["usb.irp_id"].split("|")[0]
        func = num(r["usb.function"], -1)
        status = num(r["usb.usbd_status"], 0)
        dlen = num(r["usb.data_len"], 0)

        if func in PIPE_OPS and not done:
            tr.pipe_ops.append((t, URB_FUNCTIONS.get(func, hex(func)), ep))
            continue

        if ttype == 2:     # control
            stage = num(r["usb.control_stage"], -1)
            data = hexbytes(r["usb.capdata"], r["usb.data_fragment"],
                            r["usb.control.Response"])
            if stage == 0 or r["usb.bmRequestType"]:
                open_ctl[irp] = {
                    "t": t, "func": func,
                    "bmRequestType": num(r["usb.bmRequestType"], 0),
                    "bRequest": num(r["usb.setup.bRequest"], 0),
                    "wValue": num(r["usb.setup.wValue"], 0),
                    "wIndex": num(r["usb.setup.wIndex"], 0),
                    "wLength": num(r["usb.setup.wLength"], 0),
                    "data": b"", "status": None, "t_done": None,
                }
            ctl = open_ctl.get(irp)
            if ctl is None:
                continue
            # USBPcap: OUT data rides in the SETUP packet (stage 0),
            # IN data arrives with the completion
            if data:
                ctl["data"] = data
            if done and stage != 1:
                ctl["status"] = status
                ctl["t_done"] = t
                tr.controls.append(ctl)
                del open_ctl[irp]
            continue

        if ttype not in (0, 1, 3):
            continue
        if not done:
            tr.first_submit.setdefault(ep, t)
        entry = {"t": t, "done": done, "len": dlen, "status": status}
        if ttype == 0:
            entry["npk"] = num(r["usb.win32.iso_num_packets"], 0)
            entry["offs"] = nums(r["usb.win32.iso_offset"])
            entry["lens"] = nums(r["usb.win32.iso_data_len"])
            entry["data"] = hexbytes(r["usb.capdata"], r["usb.iso.data"]) \
                if ep == EP_FB and done else b""
        tr.urbs[ep].append(entry)
    for ctl in open_ctl.values():       # never completed
        tr.controls.append(ctl)
    tr.controls.sort(key=lambda c: c["t"])
    return tr


# ---------------------------------------------------------------------------
# decoding

def rate_of(data):
    return data[0] | data[1] << 8 | data[2] << 16 if len(data) >= 3 else None


def describe(c):
    bm, req, val, idx = c["bmRequestType"], c["bRequest"], c["wValue"], c["wIndex"]
    d = c["data"]
    kind = (bm >> 5) & 3
    rcpt = bm & 0x1F
    if c["func"] in (0x0000, 0x0001, 0x000B, 0x0028):
        # synthesized by USBPcap from a non-control URB, setup fields unreliable
        return "URB %s" % URB_FUNCTIONS[c["func"]]
    if kind == 0:
        if req == 0x0B and rcpt == 1:
            return "SET_INTERFACE intf=%d alt=%d" % (idx, val)
        if req == 0x09:
            return "SET_CONFIGURATION %d" % val
        if req == 0x01 and rcpt == 2:
            return "CLEAR_FEATURE(HALT) ep=0x%02x" % idx
        if req == 0x06:
            return "GET_DESCRIPTOR type=%d idx=%d len=%d" % (val >> 8, val & 0xFF,
                                                            c["wLength"])
        return "standard req 0x%02x" % req
    if kind == 2:
        if req == 0x49 and bm & 0x80:
            return "STATUS read -> %s" % ("0x%02x" % d[0] if d else "?")
        if req == 0x49:
            return "STATUS write 0x%02x" % (val & 0xFF)
        if req == 0x41:
            codec = {0x0102: 0, 0x0106: 1}.get(idx, "?")
            return "CODEC%s write R%d=0x%03x" % (codec, val >> 9, val & 0x1FF)
        if req == 0x56:
            return "FIRMWARE read -> %s" % d.hex()
        return "vendor req 0x%02x" % req
    if kind == 1:
        if req == 0x01 and val == 0x0100:
            return "SET_CUR rate ep=0x%02x %s" % (idx, rate_of(d))
        if req == 0x81 and val == 0x0100:
            return "GET_CUR rate ep=0x%02x -> %s" % (idx, rate_of(d))
        return "class req 0x%02x" % req
    return "req 0x%02x" % req


def window_rate(times_bytes, t0, t1):
    tot = sum(b for t, b in times_bytes if t0 <= t <= t1)
    return tot / (t1 - t0) if t1 > t0 else 0.0


def ep_stats(tr, ep):
    urbs = tr.urbs.get(ep, [])
    is_in = bool(ep & 0x80)
    # data travels with the completion for IN, with the submission for OUT
    rel = [u for u in urbs if u["done"] == is_in]
    if not rel:
        return None
    t0, t1 = rel[0]["t"], rel[-1]["t"]
    tb = []
    for u in rel:
        if u.get("lens") and not is_in:
            size = sum(packet_lengths(u))
        elif u.get("lens") and is_in:
            size = sum(x or 0 for x in u["lens"])
        else:
            size = u["len"]
        tb.append((u["t"], size))
    total = sum(b for _, b in tb)
    span = max(t1 - t0, 1e-9)
    buckets = collections.Counter()
    for t, b in tb:
        buckets[int(t)] += b
    full = [buckets[s] for s in range(int(t0) + 1, int(t1))]
    sizes = collections.Counter(b for _, b in tb)
    errors = collections.Counter(u["status"] for u in rel if u["status"])
    return {
        "urbs": len(rel), "bytes": total, "first": t0, "last": t1,
        "bytes_per_s": total / span, "urbs_per_s": len(rel) / span,
        "median_1s_bytes": statistics.median(full) if full else None,
        "timeline_1s": [buckets[s] for s in range(int(t0), int(t1) + 1)],
        "sizes": sizes.most_common(8),
        "errors": {hex(k): v for k, v in errors.items()},
    }


def packet_lengths(u):
    """isoc OUT: lengths are often only implied by the offsets on submit."""
    lens = [x or 0 for x in u.get("lens", [])]
    if any(lens):
        return lens
    offs = u.get("offs", [])
    if not offs:
        return []
    ends = offs[1:] + [u["len"]]
    return [max(0, e - o) for o, e in zip(offs, ends)]


def iso_out_stats(tr):
    subs = [u for u in tr.urbs.get(EP_PLAY, []) if not u["done"]]
    if not subs:
        return None
    pk = collections.Counter()
    per_urb = collections.Counter()
    total_pk = 0
    for u in subs:
        lens = packet_lengths(u)
        per_urb[len(lens)] += 1
        total_pk += len(lens)
        for n in lens:
            pk[n] += 1
    span = max(subs[-1]["t"] - subs[0]["t"], 1e-9)
    frames = sum(n * c for n, c in pk.items()) / PLAY_FRAME
    return {
        "packets_per_urb": per_urb.most_common(4),
        "packets_per_s": total_pk / span,
        "packet_sizes": [(n, n / PLAY_FRAME, c) for n, c in pk.most_common(8)],
        "frames_per_s": frames / span,
    }


def feedback_stats(tr):
    vals = collections.Counter()
    raw = []
    for u in tr.urbs.get(EP_FB, []):
        if not u["done"] or not u["data"]:
            continue
        lens = [x or 0 for x in u.get("lens", [])]
        offs = u.get("offs", []) or [0]
        if not lens:
            lens = [len(u["data"])]
        for o, n in zip(offs, lens):
            if n:
                pkt = u["data"][o:o + n]
                if pkt:
                    vals[pkt[0]] += 1
                    if len(raw) < 6:
                        raw.append(pkt[:8].hex())
    if not vals:
        return None
    total = sum(vals.values())
    mean = sum(v * c for v, c in vals.items()) / total
    return {"values": sorted(vals.items()), "mean": mean,
            "implied_rate": mean * 1000, "raw_examples": raw}


def start_sequence(tr):
    """Control + pipe events around the first bulk-IN 0x86 submission."""
    t_cap = tr.first_submit.get(EP_CAP)
    if t_cap is None:
        return None, []
    ev = [(c["t"], describe(c)) for c in tr.controls
          if c["t"] >= t_cap - 5.0 and c["t"] <= t_cap + 0.5]
    ev += [(t, "%s ep=0x%02x" % (f, ep)) for t, f, ep in tr.pipe_ops
           if t_cap - 5.0 <= t <= t_cap + 0.5]
    for ep, t in tr.first_submit.items():
        if t_cap - 5.0 <= t <= t_cap + 0.5:
            ev.append((t, "first URB submit ep=0x%02x" % ep))
    ev.sort()
    return t_cap, ev


# ---------------------------------------------------------------------------
# Linux driver start sequence (static, from phasex64.c)

def strip_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def function_body(src, name):
    m = re.search(r"\n(?:static\s+)?[\w\s\*]*\b%s\s*\([^)]*\)\s*\{" % name, src)
    if not m:
        return ""
    i, depth = m.end(), 1
    while depth and i < len(src):
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    return src[m.end():i]


def linux_sequence(path=DRIVER_C):
    if not os.path.isfile(path):
        return []
    src = strip_comments(open(path, encoding="utf-8").read())
    body = function_body(src, "px_start_streaming")
    pat = re.compile(
        r"px_set_rate_ep\(px,\s*(EP_\w+)"
        r"|px_status_update\(px,\s*([^;]+?)\)\s*\)?;"
        r"|usb_set_interface\([^,]+,\s*(\d+),\s*(\d+)\)"
        r"|usb_clear_halt\([^,]+,\s*([^)]+)\)"
        r"|msleep\(([^)]+)\)"
        r"|usb_submit_urb\(px->(\w+?)_urb")
    out = []
    for m in pat.finditer(body):
        if m.group(1):
            out.append("SET_CUR rate ep=%s" % m.group(1))
        elif m.group(2):
            out.append("STATUS read-modify-write (%s)"
                       % " ".join(m.group(2).split()))
        elif m.group(3):
            out.append("SET_INTERFACE intf=%s alt=%s" % (m.group(3), m.group(4)))
        elif m.group(5):
            out.append("CLEAR_HALT %s" % m.group(5))
        elif m.group(6):
            out.append("msleep(%s)" % m.group(6))
        elif m.group(7):
            name = "submit %s URBs" % m.group(7)
            if not out or out[-1] != name:
                out.append(name)
    return out


# ---------------------------------------------------------------------------
# WAV check

def read_wav_any(path):
    buf = open(path, "rb").read()
    if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(buf):
        cid, size = buf[pos:pos + 4], struct.unpack("<I", buf[pos + 4:pos + 8])[0]
        if cid == b"fmt ":
            fmt = buf[pos + 8:pos + 8 + size]
        elif cid == b"data":
            data = buf[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    tag, ch, rate, _, _, bits = struct.unpack("<HHIIHH", fmt[:16])
    if tag == 0xFFFE:
        tag = struct.unpack("<H", fmt[24:26])[0]
    import numpy as np
    if tag == 3 and bits == 32:
        x = np.frombuffer(data, "<f4").astype(np.float64)
    elif tag == 3 and bits == 64:
        x = np.frombuffer(data, "<f8")
    elif bits == 24:
        raw = np.frombuffer(data, np.uint8)[:len(data) // 3 * 3].reshape(-1, 3)
        v = raw[:, 0].astype(np.int32) | raw[:, 1].astype(np.int32) << 8 | \
            raw[:, 2].astype(np.int32) << 16
        x = np.where(v >= 0x800000, v - 0x1000000, v) / float(1 << 23)
    elif bits in (16, 32):
        x = np.frombuffer(data, "<i%d" % (bits // 8)) / float(1 << (bits - 1))
    else:
        raise ValueError("unsupported format tag=%d bits=%d" % (tag, bits))
    n = len(x) // ch
    return x[:n * ch].reshape(n, ch), rate, ch, bits, tag


def wav_report(paths):
    import numpy as np
    sys.path.insert(0, os.path.dirname(__file__))
    from pxtools import analyze_channel
    for p in paths:
        x, rate, ch, bits, tag = read_wav_any(p)
        print("\n== %s" % p)
        print("  %d ch, %d Hz, %d bit (%s), %.3f s"
              % (ch, rate, bits, "float" if tag == 3 else "PCM", len(x) / rate))
        for c in range(ch):
            col = x[:, c]
            peak = float(np.max(np.abs(col))) if len(col) else 0.0
            rms, freq = analyze_channel(col, rate)
            jumps = int(np.sum(np.abs(np.diff(col)) > 0.5))
            print("  ch%d: peak %6.1f dBFS  rms %6.1f dBFS  dominant %8.1f Hz"
                  "  big jumps %d" % (
                      c + 1, 20 * np.log10(peak) if peak > 0 else -999,
                      20 * np.log10(rms) if rms > 0 else -999, freq, jumps))


# ---------------------------------------------------------------------------
# report

def fmt_rate(b):
    return "%.3f MB/s" % (b / 1e6) if b is not None else "-"


def analyze_file(tshark, path, bus=None, addr=None):
    if bus is None or addr is None:
        dev, how = find_device(tshark, path)
        if dev is None:
            sys.exit("%s: PHASE X64 not found (use --bus/--addr)" % path)
        bus, addr = dev
    else:
        how = "command line"
    tr = parse(tshark, path, bus, addr)
    res = {"file": os.path.basename(path), "bus": bus, "addr": addr,
           "found_by": how, "duration": tr.duration, "endpoints": {}}
    for ep in sorted(tr.urbs):
        st = ep_stats(tr, ep)
        if st:
            res["endpoints"]["0x%02x" % ep] = st
    res["iso_out"] = iso_out_stats(tr)
    res["feedback"] = feedback_stats(tr)
    res["controls"] = [{"t": round(c["t"], 6), "desc": describe(c),
                        "bmRequestType": "0x%02x" % c["bmRequestType"],
                        "bRequest": "0x%02x" % c["bRequest"],
                        "wValue": "0x%04x" % c["wValue"],
                        "wIndex": "0x%04x" % c["wIndex"],
                        "wLength": c["wLength"], "data": c["data"].hex(),
                        "status": None if c["status"] is None else hex(c["status"]),
                        "urb_function": URB_FUNCTIONS.get(c["func"], hex(c["func"]))}
                       for c in tr.controls]
    res["pipe_ops"] = [{"t": round(t, 6), "op": f, "ep": "0x%02x" % ep}
                       for t, f, ep in tr.pipe_ops]
    t_cap, seq = start_sequence(tr)
    res["start_t"] = t_cap
    res["start_sequence"] = [{"t": round(t, 6), "event": e} for t, e in seq]
    cap = res["endpoints"].get("0x%02x" % EP_CAP)
    if cap:
        cap["frames_per_s"] = cap["bytes_per_s"] / CAP_WIRE_FRAME
        if cap["median_1s_bytes"] is not None:
            cap["median_frames_per_s"] = cap["median_1s_bytes"] / CAP_WIRE_FRAME
    return res


def print_file(res, linux_seq):
    print("\n" + "=" * 78)
    print("%s  (bus %d, device %d, found by %s, %.2f s)"
          % (res["file"], res["bus"], res["addr"], res["found_by"], res["duration"]))
    print("=" * 78)

    print("\n-- control transfers (%d)" % len(res["controls"]))
    print("  %10s  %-4s %-4s %-6s %-6s %4s  %-22s %-6s %s" % (
        "t [s]", "bmRT", "bReq", "wValue", "wIndex", "wLen", "data", "status",
        "meaning"))
    for c in res["controls"]:
        print("  %10.6f  %s %s %s %s %4d  %-22s %-6s %s" % (
            c["t"], c["bmRequestType"][2:].rjust(4), c["bRequest"][2:].rjust(4),
            c["wValue"], c["wIndex"], c["wLength"], c["data"][:22],
            (c["status"] or "-")[:6], c["desc"]))
    if res["pipe_ops"]:
        print("\n-- pipe operations")
        for p in res["pipe_ops"]:
            print("  %10.6f  %s %s" % (p["t"], p["op"], p["ep"]))

    print("\n-- endpoints")
    for ep, st in res["endpoints"].items():
        print("  %s: %d URBs, %s, %.1f URBs/s, active %.2f-%.2f s"
              % (ep, st["urbs"], fmt_rate(st["bytes_per_s"]), st["urbs_per_s"],
                 st["first"], st["last"]))
        print("        sizes (bytes x count): %s" % ", ".join(
            "%dx%d" % sc for sc in st["sizes"]))
        print("        1 s timeline [kB]: %s" % " ".join(
            "%d" % (b / 1000) for b in st["timeline_1s"]))
        if st["errors"]:
            print("        error statuses: %s" % st["errors"])
    cap = res["endpoints"].get("0x%02x" % EP_CAP)
    if cap:
        print("\n-- bulk IN 0x86 (capture): %s = %.0f frames/s (mean), median 1 s "
              "bucket %s frames/s; 48 kHz would be 3.072 MB/s"
              % (fmt_rate(cap["bytes_per_s"]), cap["frames_per_s"],
                 "%.0f" % cap["median_frames_per_s"]
                 if cap.get("median_frames_per_s") else "-"))
    io = res["iso_out"]
    if io:
        print("\n-- isoc OUT 0x02 (playback): %.0f packets/s, %.0f frames/s"
              % (io["packets_per_s"], io["frames_per_s"]))
        print("        packets per URB x count: %s" % io["packets_per_urb"])
        print("        packet size (bytes, frames) x count: %s" % ", ".join(
            "%d/%.1f x%d" % p for p in io["packet_sizes"]))
    else:
        print("\n-- isoc OUT 0x02: no traffic")
    fb = res["feedback"]
    if fb:
        print("\n-- feedback 0x81: values %s, mean %.3f -> %.0f Hz; raw %s"
              % (fb["values"], fb["mean"], fb["implied_rate"], fb["raw_examples"]))
    else:
        print("\n-- feedback 0x81: no data")

    print("\n-- stream start (around first 0x86 submit at %s s)"
          % ("%.6f" % res["start_t"] if res["start_t"] is not None else "-"))
    win = res["start_sequence"]
    width = max([len(e["event"]) for e in win] + [30])
    print("  %-*s | %s" % (width + 22, "Windows (this trace)", "Linux phasex64.c"))
    prev = None
    for i in range(max(len(win), len(linux_seq))):
        left = ""
        if i < len(win):
            e = win[i]
            d = "" if prev is None else "+%.1f ms" % ((e["t"] - prev) * 1000)
            prev = e["t"]
            left = "%10.6f %9s  %s" % (e["t"], d, e["event"])
        right = linux_seq[i] if i < len(linux_seq) else ""
        print("  %-*s | %s" % (width + 22, left, right))


def print_compare(results):
    print("\n" + "=" * 78)
    print("side-by-side")
    print("=" * 78)
    rows = []

    def ep(r, e, k):
        st = r["endpoints"].get(e)
        return st.get(k) if st else None

    rows.append(("duration [s]", [r["duration"] for r in results], "%.2f"))
    for e in ("0x86", "0x02", "0x81", "0x83", "0x04"):
        rows.append(("%s MB/s" % e, [None if ep(r, e, "bytes_per_s") is None
                                     else ep(r, e, "bytes_per_s") / 1e6
                                     for r in results], "%.3f"))
        rows.append(("%s URBs/s" % e, [ep(r, e, "urbs_per_s") for r in results],
                     "%.1f"))
    rows.append(("0x86 frames/s", [ep(r, "0x86", "frames_per_s")
                                   for r in results], "%.0f"))
    rows.append(("0x02 frames/s", [r["iso_out"]["frames_per_s"] if r["iso_out"]
                                   else None for r in results], "%.0f"))
    rows.append(("0x02 packets/URB", [r["iso_out"]["packets_per_urb"][0][0]
                                      if r["iso_out"] else None
                                      for r in results], "%s"))
    rows.append(("feedback mean", [r["feedback"]["mean"] if r["feedback"]
                                   else None for r in results], "%.3f"))
    rows.append(("control transfers", [len(r["controls"]) for r in results], "%d"))
    def status_writes(r):
        cnt = collections.Counter(c["desc"].split()[-1] for c in r["controls"]
                                  if c["desc"].startswith("STATUS write"))
        return " ".join("%s x%d" % kv for kv in sorted(cnt.items())) or "-"

    rows.append(("status writes", [status_writes(r) for r in results], "%s"))
    rows.append(("SET_CUR rates", [" ".join(sorted({c["desc"].split()[-1]
                                                    for c in r["controls"]
                                                    if c["desc"].startswith("SET_CUR")}))
                                   or "-" for r in results], "%s"))
    w = max(14, max(len(r["file"]) for r in results))
    print("  %-18s" % "" + "".join("%-*s" % (w + 2, r["file"]) for r in results))
    for name, vals, f in rows:
        cells = ["-" if v is None else (f % v) for v in vals]
        print("  %-18s" % name + "".join("%-*s" % (w + 2, c) for c in cells))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pcaps", nargs="*", help=".pcap/.pcapng files")
    ap.add_argument("--wav", nargs="+", help="check REAPER WAV recordings")
    ap.add_argument("--tshark", help="path to tshark")
    ap.add_argument("--bus", type=int)
    ap.add_argument("--addr", type=int)
    ap.add_argument("--driver", default=DRIVER_C, help="phasex64.c to compare")
    ap.add_argument("--json", help="write all results as JSON")
    args = ap.parse_args()
    if not args.pcaps and not args.wav:
        ap.error("give pcap files and/or --wav files")

    results = []
    if args.pcaps:
        tshark = find_tshark(args.tshark)
        linux_seq = linux_sequence(args.driver)
        for p in args.pcaps:
            res = analyze_file(tshark, p, args.bus, args.addr)
            print_file(res, linux_seq)
            results.append(res)
        if len(results) > 1:
            print_compare(results)
        if args.json:
            with open(args.json, "w") as f:
                json.dump({"linux_start_sequence": linux_seq, "files": results},
                          f, indent=1, default=str)
    if args.wav:
        wav_report(args.wav)


if __name__ == "__main__":
    main()
