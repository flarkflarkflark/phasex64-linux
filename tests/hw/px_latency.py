#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""px_latency.py - sample-accurate round-trip latency measurement, PHASE X64.

Not run by the assistant: the `alsa` subcommand opens hw:PhaseX64 directly
(raw ALSA, PipeWire stopped) and is for the user to run by hand. `gen`,
`analyze`, `null`, `rpp` and `selftest` never touch any audio device. See
tests/hw/README.md, "Latency measurement (px_latency.py)".

  gen       write the stimulus WAV (4ch, 24-bit): single-sample impulses on
            Out 1/2 (ch 1+2), Out 3/4 silent.
  alsa      raw-ALSA run: open playback + capture on hw:PhaseX64, snd_pcm_link
            them, prefill, start both in the same trigger, play the stimulus
            and record 6ch. Because both streams start in one linked trigger,
            playback frame p and capture frame p are the *same* ALSA instant,
            so "where the impulse lands in the capture minus where it was in
            the playback stream" is exactly the delay the driver's pointer
            does NOT account for. Also reads what the driver reports as extra
            delay (snd_pcm_avail_delay() minus the ring-buffer part), so the
            same run shows "measured hidden" next to "reported hidden".
  analyze   offsets of the impulses in an already-recorded WAV (e.g. a REAPER
            take rendered from project time 0) against the stimulus.
            Offset 0 = perfectly compensated; >0 = recording is late.
  null      null test of one analog channel between two saved captures,
            aligned on the digital tap (0 dB = unchanged check).
  rpp       REAPER's latency compensation error, from a saved project:
            raw offset of each recorded take minus the shift REAPER applied
            (SOFFS - POSITION). See tests/hw/reaper-latency/README.md.
  selftest  offline check of the analysis, `analyze`, `null`, `rpp` and the
            `alsa` I/O loop (against a simulated device) on synthetic data;
            exits 0 only if every check passes.

Channels are 1-based. Capture: 1/2 = In 1/2, 3/4 = In 3/4, 5/6 = the
bit-exact internal digital tap of Out 1/2 (no cable involved, see
analysis/NOTES.md, "Hardware I/O").

Default analog path needs no cable: `In 3/4 Capture Source` = `Loopback 1/2`
(In 3/4 then records Out 1/2's actual analog output: DAC of codec 0 -> ADC of
codec 1). `--analog-ch 3` (default) reads it; ch 5 is the digital reference.

Safety, `alsa` subcommand: it never writes a mixer control. Before opening
anything it checks /proc that no PCM substream of the card is open, then
reads (never sets) Digital Input Lock (live read, since nothing streams),
Clock Source, PCM 1/2 and Direct 1/2 and In 3/4 Capture Source, and stops
with the exact amixer command for you to run if one is wrong. PCMs are
opened non-blocking, so a busy device fails at once instead of waiting.
Both PCMs are closed on every exit path, Ctrl-C/SIGTERM/SIGHUP included.
After each run (device idle again) it reads `Digital Input Rate` - a live
GET_CUR while not streaming - and marks the run invalid if the device does
not report the requested rate.
"""
import argparse
import ctypes
import ctypes.util
import gc
import os
import signal
import struct
import subprocess
import sys
import tempfile
import time

import numpy as np

PLAY_CH, CAP_CH = 4, 6
SND_PCM_STREAM_PLAYBACK, SND_PCM_STREAM_CAPTURE = 0, 1
SND_PCM_NONBLOCK = 0x1
SND_PCM_ACCESS_RW_INTERLEAVED = 3
SND_PCM_FORMAT_S32_LE, SND_PCM_FORMAT_S24_3LE = 10, 32
EINTR, EAGAIN, EBUSY, EPIPE, ESTRPIPE = 4, 11, 16, 32, 86

# All four rates the driver advertises. 88.2/96 kHz were tested on hardware
# once without the S/PDIF cable and without PipeWire (tests/hw/REPORT.md,
# "Acceptance tests (2026-10-14, cable removed ...)"); the older "88.2 kHz /
# 96 kHz break the capture engine" section there is marked invalid (it was
# measured with the cable in).
RATES = (44100, 48000, 88200, 96000)

LEAD_S = 0.5        # silence before the first impulse
SPACING_S = 0.25    # impulse spacing - far larger than any plausible latency
N_IMPULSES = 16
AMPLITUDE = 0.5     # -6 dBFS single-sample impulse
MAXLAG_S = 0.1      # search window, +-100 ms
POLL_MS = 1         # snd_pcm_wait() timeout per pass of the I/O loop
STALL_S = 2.0       # no capture data and no playback space for this long = abort
RATE_READBACK_DELAY_S = 0.5
CLEANUP_SIGNALS = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}


class Interrupted(Exception):
    pass


def _on_signal(signum, _frame):
    raise Interrupted(signal.Signals(signum).name)


# --------------------------------------------------------------------------
# stimulus and WAV I/O (no external modules beyond numpy)

def stimulus(rate):
    """float64 array [frames, 4]: impulses on ch 1+2, plus the impulse
    positions (frame indices) in the playback stream."""
    lead, spacing = int(LEAD_S * rate), int(SPACING_S * rate)
    frames = lead + N_IMPULSES * spacing + int(MAXLAG_S * rate)
    x = np.zeros((frames, PLAY_CH))
    pos = lead + spacing * np.arange(N_IMPULSES)
    x[pos, 0] = AMPLITUDE
    x[pos, 1] = AMPLITUDE
    return x, pos


def to_s24_3le(x):
    v = np.clip(np.round(x * (1 << 23)), -(1 << 23), (1 << 23) - 1).astype("<i4")
    return v.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()


def write_wav24(path, x, rate):
    data = to_s24_3le(x.reshape(-1))
    ch = x.shape[1]
    fmt = struct.pack("<HHIIHH", 1, ch, rate, rate * ch * 3, ch * 3, 24)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        f.write(b"data" + struct.pack("<I", len(data)) + data)


def read_wav(path):
    """-> (float64 [frames, ch], rate). PCM 16/24/32 and float 32/64,
    including WAVE_FORMAT_EXTENSIBLE (REAPER renders)."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:4] not in (b"RIFF", b"RF64") or raw[8:12] != b"WAVE":
        raise ValueError(f"{path}: not a WAV file")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid, size = raw[pos:pos + 4], struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError(f"{path}: missing fmt/data chunk")
    tag, ch, rate, _, _, bits = struct.unpack("<HHIIHH", fmt[:16])
    if tag == 0xFFFE:
        tag = struct.unpack("<H", fmt[24:26])[0]
    width = bits // 8
    usable = len(data) - len(data) % (width * ch)
    data = data[:usable]
    if tag == 3:
        x = np.frombuffer(data, dtype="<f4" if bits == 32 else "<f8").astype(np.float64)
    elif tag == 1 and bits == 24:
        b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = np.where(v >= 1 << 23, v - (1 << 24), v) / float(1 << 23)
    elif tag == 1 and bits in (16, 32):
        x = np.frombuffer(data, dtype="<i2" if bits == 16 else "<i4") / float(1 << (bits - 1))
    else:
        raise ValueError(f"{path}: unsupported WAV format tag={tag} bits={bits}")
    return x.reshape(-1, ch), rate


# --------------------------------------------------------------------------
# analysis

def find_offset(ref, sig, maxlag):
    """Lag (fractional samples) of `sig` relative to `ref`, searched in
    [-maxlag, +maxlag], via FFT cross-correlation + parabolic interpolation
    on |cc|. Returns (lag, polarity, normalized peak)."""
    n = len(ref) + len(sig)
    nfft = 1 << (n - 1).bit_length()
    cc = np.fft.irfft(np.fft.rfft(sig, nfft) * np.conj(np.fft.rfft(ref, nfft)), nfft)
    lags = np.r_[np.arange(0, maxlag + 1), np.arange(-maxlag, 0)]
    vals = np.r_[cc[:maxlag + 1], cc[nfft - maxlag:]]
    i = int(np.argmax(np.abs(vals)))
    lag = int(lags[i])
    y0, y1, y2 = (abs(cc[(lag + d) % nfft]) for d in (-1, 0, 1))
    den = y0 - 2 * y1 + y2
    frac = 0.5 * (y0 - y2) / den if den else 0.0
    norm = np.sqrt(np.sum(ref ** 2) * np.sum(sig ** 2)) or 1.0
    return lag + frac, int(np.sign(cc[lag % nfft])) or 1, abs(cc[lag % nfft]) / norm


def per_impulse(sig, pos, lag, half=48):
    """Integer peak position of each impulse around its expected place -
    a spread > 1 sample means drift, an xrun or a dropout inside the run."""
    out = []
    base = int(round(lag))
    for p in pos:
        lo = p + base - half
        if lo < 0 or lo + 2 * half > len(sig):
            continue
        out.append(lo + int(np.argmax(np.abs(sig[lo:lo + 2 * half]))) - p)
    return np.array(out)


def report(label, ref, sig, pos, rate):
    """Print one channel's offset line; returns (lag, polarity, corr, spread)."""
    maxlag = int(MAXLAG_S * rate)
    lag, pol, q = find_offset(ref, sig, maxlag)
    imp = per_impulse(sig, pos, lag)
    spread = int(imp.max() - imp.min()) if len(imp) else -1
    peak_db = 20 * np.log10(np.max(np.abs(sig)) or 1e-12)
    flag = ""
    if q < 0.3:
        flag = "  <-- weak correlation: wrong channel/source, or no signal?"
    elif spread > 1:
        flag = "  <-- impulses not at a constant offset (drift/xrun/dropout)"
    print(f"  {label:<14} offset {lag:9.2f} samples ({lag / rate * 1e3:7.3f} ms)  "
          f"polarity {'+' if pol > 0 else '-'}  corr {q:4.2f}  "
          f"per-impulse spread {spread}  peak {peak_db:6.1f} dBFS{flag}")
    return lag, pol, q, spread


# --------------------------------------------------------------------------
# read-only device state (/proc and amixer cget - never cset)

def substream_owner(card, sub):
    """None if the substream is closed, else a short description of who
    holds it (from /proc/asound/<card>/<sub>/sub0/status)."""
    path = f"/proc/asound/{card}/{sub}/sub0/status"
    with open(path, encoding="ascii", errors="replace") as f:
        text = f.read()
    if text.strip() == "closed":
        return None
    pid = None
    for line in text.splitlines():
        if line.strip().startswith("owner_pid"):
            pid = line.split(":", 1)[1].strip()
    comm = "?"
    if pid:
        try:
            with open(f"/proc/{pid}/comm", encoding="ascii", errors="replace") as f:
                comm = f.read().strip()
        except OSError:
            pass
    state = text.splitlines()[0].strip()
    return f"{state}, owner pid {pid or '?'} ({comm})"


def amixer_get(card, name):
    """(value, item_name) of one control via `amixer cget` (read-only).
    item_name is set for enumerated controls. (None, None) if unreadable."""
    try:
        res = subprocess.run(["amixer", "-c", card, "cget", f"name={name}"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if res.returncode != 0:
        return None, None
    items, value = {}, None
    for line in res.stdout.splitlines():
        s = line.strip()
        if s.startswith("; Item #"):
            num, _, label = s[len("; Item #"):].partition(" ")
            items[num] = label.strip("'")
        elif s.startswith(": values="):
            value = s.split("=", 1)[1].strip()
    return value, items.get(value)


def proc_rate(card, sub):
    try:
        with open(f"/proc/asound/{card}/{sub}/sub0/hw_params", encoding="ascii") as f:
            for line in f:
                if line.startswith("rate:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "?"


def preflight(args):
    card = args.card
    if not os.path.isdir(f"/proc/asound/{card}"):
        sys.exit(f"ABORT: no ALSA card '{card}' (driver not loaded, device unplugged, "
                 "or a different card id - see --card).")
    busy = [(sub, who) for sub in ("pcm0p", "pcm0c")
            if (who := substream_owner(card, sub)) is not None]
    if busy:
        lines = "\n".join(f"  {sub}: {who}" for sub, who in busy)
        sys.exit(f"ABORT: hw:{card} is in use:\n{lines}\nStop PipeWire first (see "
                 "tests/hw/README.md) - this script needs the device to itself.")

    def q(name):
        return f"amixer -c {card} cset name='{name}'"

    problems = []
    lock, _ = amixer_get(card, "Digital Input Lock")
    print(f"preflight: Digital Input Lock = {lock} (live read, nothing streaming)")
    if lock is None:
        problems.append("could not read 'Digital Input Lock' (driver too old?)")
    elif lock != "off":
        problems.append("Digital Input Lock is on: unplug the S/PDIF coax loopback "
                        "cable (with it, capture follows the S/PDIF clock - "
                        "tests/hw/REPORT.md, \"Windows USB trace (2026-10-14)\")")
    _, clock = amixer_get(card, "Clock Source")
    print(f"preflight: Clock Source = {clock}")
    if clock != "Internal":
        problems.append(f"Clock Source is {clock!r}: {q('Clock Source')} 'Internal'")
    pcm12, _ = amixer_get(card, "PCM 1/2 Playback Switch")
    print(f"preflight: PCM 1/2 Playback Switch = {pcm12}")
    if pcm12 != "on":
        problems.append(f"PCM 1/2 is off (no stimulus would reach Out 1/2): "
                        f"{q('PCM 1/2 Playback Switch')} on")
    direct12, _ = amixer_get(card, "Direct 1/2 Playback Switch")
    print(f"preflight: Direct 1/2 Playback Switch = {direct12}")
    if direct12 != "off":
        problems.append(f"Direct 1/2 is on (In 1/2 would be mixed into Out 1/2): "
                        f"{q('Direct 1/2 Playback Switch')} off")
    _, src34 = amixer_get(card, "In 3/4 Capture Source")
    print(f"preflight: In 3/4 Capture Source = {src34}")
    if args.analog_ch in (3, 4) and src34 != "Loopback 1/2" and not args.cabled:
        problems.append(f"In 3/4 Capture Source is {src34!r}: "
                        f"{q('In 3/4 Capture Source')} 'Loopback 1/2' "
                        "(or pass --cabled if you patched Out 1/2 into In 3/4 yourself)")
    rate, _ = amixer_get(card, "Digital Input Rate")
    print(f"preflight: Digital Input Rate = {rate} Hz (device's current rate, before "
          f"this run; {args.rate} requested)")
    if problems:
        print("\nABORT - nothing was opened or changed. Fix these yourself, then rerun:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(2)


# --------------------------------------------------------------------------
# raw ALSA (ctypes, libasound)

class Alsa:
    def __init__(self):
        self.lib = ctypes.CDLL(ctypes.util.find_library("asound") or "libasound.so.2")
        L = self.lib
        vp, ul = ctypes.c_void_p, ctypes.c_ulong
        L.snd_strerror.restype = ctypes.c_char_p
        L.snd_pcm_open.argtypes = [ctypes.POINTER(vp), ctypes.c_char_p, ctypes.c_int,
                                   ctypes.c_int]
        L.snd_pcm_readi.restype = ctypes.c_long
        L.snd_pcm_readi.argtypes = [vp, vp, ul]
        L.snd_pcm_writei.restype = ctypes.c_long
        L.snd_pcm_writei.argtypes = [vp, vp, ul]
        L.snd_pcm_wait.argtypes = [vp, ctypes.c_int]
        for fn in ("snd_pcm_hw_params_any", "snd_pcm_hw_params"):
            getattr(L, fn).argtypes = [vp, vp]
        L.snd_pcm_hw_params_set_access.argtypes = [vp, vp, ctypes.c_int]
        L.snd_pcm_hw_params_set_format.argtypes = [vp, vp, ctypes.c_int]
        L.snd_pcm_hw_params_set_channels.argtypes = [vp, vp, ctypes.c_uint]
        L.snd_pcm_hw_params_set_rate.argtypes = [vp, vp, ctypes.c_uint, ctypes.c_int]
        L.snd_pcm_hw_params_set_period_size.argtypes = [vp, vp, ul, ctypes.c_int]
        L.snd_pcm_hw_params_set_periods.argtypes = [vp, vp, ctypes.c_uint, ctypes.c_int]
        L.snd_pcm_hw_params_malloc.argtypes = [ctypes.POINTER(vp)]
        L.snd_pcm_hw_params_free.argtypes = [vp]
        L.snd_pcm_sw_params_malloc.argtypes = [ctypes.POINTER(vp)]
        L.snd_pcm_sw_params_free.argtypes = [vp]
        L.snd_pcm_sw_params_current.argtypes = [vp, vp]
        L.snd_pcm_sw_params.argtypes = [vp, vp]
        L.snd_pcm_sw_params_set_start_threshold.argtypes = [vp, vp, ul]
        L.snd_pcm_avail_delay.argtypes = [vp, ctypes.POINTER(ctypes.c_long),
                                          ctypes.POINTER(ctypes.c_long)]
        L.snd_pcm_avail.restype = ctypes.c_long
        L.snd_pcm_avail.argtypes = [vp]
        for fn in ("snd_pcm_start", "snd_pcm_drop", "snd_pcm_close", "snd_pcm_unlink"):
            getattr(L, fn).argtypes = [vp]
        L.snd_pcm_link.argtypes = [vp, vp]

    def err(self, r):
        return f"{self.lib.snd_strerror(r).decode()} ({r})"

    def chk(self, r, what):
        if r < 0:
            raise RuntimeError(f"{what}: {self.err(r)}")
        return r

    def open(self, dev, stream, fmt, ch, rate, period, periods):
        """Non-blocking open (a busy device returns -EBUSY at once instead
        of waiting); I/O stays non-blocking too, paced by snd_pcm_wait()."""
        L, h = self.lib, ctypes.c_void_p()
        r = L.snd_pcm_open(ctypes.byref(h), dev.encode(), stream, SND_PCM_NONBLOCK)
        if r == -EBUSY:
            raise RuntimeError(f"{dev} is busy (PipeWire or another program holds it) - "
                               "stop it first, see tests/hw/README.md")
        self.chk(r, f"open {dev}")
        try:
            self._configure(h, fmt, ch, rate, period, periods)
        except Exception:
            L.snd_pcm_close(h)
            raise
        return h

    def _configure(self, h, fmt, ch, rate, period, periods):
        L, hp, sp = self.lib, ctypes.c_void_p(), ctypes.c_void_p()
        self.chk(L.snd_pcm_hw_params_malloc(ctypes.byref(hp)), "hw_params_malloc")
        try:
            self.chk(L.snd_pcm_hw_params_any(h, hp), "hw_params_any")
            self.chk(L.snd_pcm_hw_params_set_access(h, hp, SND_PCM_ACCESS_RW_INTERLEAVED),
                     "access")
            self.chk(L.snd_pcm_hw_params_set_format(h, hp, fmt), "format")
            self.chk(L.snd_pcm_hw_params_set_channels(h, hp, ch), "channels")
            self.chk(L.snd_pcm_hw_params_set_rate(h, hp, rate, 0), f"rate {rate}")
            self.chk(L.snd_pcm_hw_params_set_period_size(h, hp, period, 0), f"period {period}")
            self.chk(L.snd_pcm_hw_params_set_periods(h, hp, periods, 0), f"periods {periods}")
            self.chk(L.snd_pcm_hw_params(h, hp), "hw_params")
        finally:
            L.snd_pcm_hw_params_free(hp)
        # no auto-start in either direction: the linked snd_pcm_start() does it
        self.chk(L.snd_pcm_sw_params_malloc(ctypes.byref(sp)), "sw_params_malloc")
        try:
            self.chk(L.snd_pcm_sw_params_current(h, sp), "sw_params_current")
            self.chk(L.snd_pcm_sw_params_set_start_threshold(h, sp, 1 << 30), "start_threshold")
            self.chk(L.snd_pcm_sw_params(h, sp), "sw_params")
        finally:
            L.snd_pcm_sw_params_free(sp)

    def extra_delay(self, h, playback, buffer_frames):
        """What the driver reports beyond the ring buffer (runtime->delay)."""
        avail, delay = ctypes.c_long(), ctypes.c_long()
        self.chk(self.lib.snd_pcm_avail_delay(h, ctypes.byref(avail), ctypes.byref(delay)),
                 "avail_delay")
        ring = buffer_frames - avail.value if playback else avail.value
        return delay.value - ring


def run_once(alsa, args, period, periods, x):
    rate, dev = args.rate, f"hw:{args.card}"
    buf = period * periods
    L = alsa.lib
    play = cap = None
    linked = False
    try:
        play = alsa.open(dev, SND_PCM_STREAM_PLAYBACK, SND_PCM_FORMAT_S24_3LE, PLAY_CH,
                         rate, period, periods)
        cap = alsa.open(dev, SND_PCM_STREAM_CAPTURE, SND_PCM_FORMAT_S32_LE, CAP_CH,
                        rate, period, periods)
        alsa.chk(L.snd_pcm_link(cap, play), "snd_pcm_link")
        linked = True

        # Capture: exactly `total` frames, read straight into one
        # preallocated buffer. Playback: the stimulus plus enough silence
        # that the top-up below can never run dry before capture is done.
        total = len(x) + buf + int(MAXLAG_S * rate)
        pframes = total + buf + rate // 2
        out = np.zeros((pframes, PLAY_CH))
        out[:len(x)] = x
        pdata = to_s24_3le(out.reshape(-1))
        pbuf = ctypes.create_string_buffer(pdata, len(pdata))
        pbase, pf = ctypes.addressof(pbuf), PLAY_CH * 3
        cbuf = (ctypes.c_int32 * (total * CAP_CH))()
        cbase, cf = ctypes.addressof(cbuf), CAP_CH * 4
        state = {"wpos": 0, "got": 0}

        def avail(h, what):
            a = L.snd_pcm_avail(h)
            if a in (-EPIPE, -ESTRPIPE):
                raise RuntimeError(f"{what} xrun - run invalid")
            return alsa.chk(a, f"{what} avail")

        def top_up():
            """Fill playback to the brim, independent of capture (the old
            loop wrote only as many frames as it had just read, so the fill
            level sank by the capture pipeline's lag - the 64x2 underruns)."""
            moved = False
            while state["wpos"] < pframes:
                n = min(avail(play, "playback"), pframes - state["wpos"])
                if n <= 0:
                    break
                w = L.snd_pcm_writei(play, ctypes.c_void_p(pbase + state["wpos"] * pf), n)
                if w == -EAGAIN:
                    break
                if w in (-EPIPE, -ESTRPIPE):
                    raise RuntimeError("playback underrun (xrun) - run invalid")
                state["wpos"] += alsa.chk(w, "writei")
                moved = True
            return moved

        def drain():
            moved = False
            while state["got"] < total:
                n = min(avail(cap, "capture"), total - state["got"])
                if n <= 0:
                    break
                r = L.snd_pcm_readi(cap, ctypes.c_void_p(cbase + state["got"] * cf), n)
                if r == -EAGAIN:
                    break
                if r in (-EPIPE, -ESTRPIPE):
                    raise RuntimeError("capture overrun (xrun) - run invalid")
                state["got"] += alsa.chk(r, "readi")
                moved = True
            return moved

        # No allocations, no prints, no garbage collection inside the
        # real-time loop - everything is reported after the run.
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            top_up()                       # prefill the whole buffer
            alsa.chk(L.snd_pcm_start(play), "snd_pcm_start (linked)")
            extra, rates, idle_ms = None, None, 0
            while state["got"] < total:
                # Wakes on a capture period, or after POLL_MS at the latest:
                # the driver moves its pointers once per 1 ms URB, so a 1 ms
                # poll sees every step. Top-up and drain run on every pass.
                r = L.snd_pcm_wait(cap, POLL_MS)
                if r in (-EPIPE, -ESTRPIPE):
                    raise RuntimeError("capture xrun - run invalid")
                if r < 0 and r != -EINTR:
                    alsa.chk(r, "snd_pcm_wait")
                moved = drain()
                moved = top_up() or moved
                idle_ms = 0 if moved else idle_ms + POLL_MS
                if idle_ms >= STALL_S * 1000:
                    raise RuntimeError(f"no progress for {STALL_S:.0f} s (device stalled?) "
                                       "- run invalid")
                if extra is None and state["got"] >= 0.3 * rate:
                    extra = (alsa.extra_delay(play, True, buf),
                             alsa.extra_delay(cap, False, buf))
                    rates = (proc_rate(args.card, "pcm0p"), proc_rate(args.card, "pcm0c"))
        finally:
            if gc_was_enabled:
                gc.enable()
        c = np.ctypeslib.as_array(cbuf).reshape(-1, CAP_CH) / float(1 << 31)
        return c, extra, rates
    finally:
        # Every exit path, exceptions and signals included: drop, unlink,
        # close. Each step separately, so one failure can't skip the rest;
        # signals are held off meanwhile (a second Ctrl-C lands afterwards).
        held = signal.pthread_sigmask(signal.SIG_BLOCK, CLEANUP_SIGNALS)
        try:
            for h in (play, cap):
                if h is not None:
                    L.snd_pcm_drop(h)
            if linked:
                L.snd_pcm_unlink(cap)
            for h in (play, cap):
                if h is not None:
                    L.snd_pcm_close(h)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, held)


def verify_rate(args):
    """Live GET_CUR via `Digital Input Rate`, read once the device is idle
    again (both PCMs closed -> streaming stopped, px_pcm_close()). True if
    it matches, False on mismatch, None if unreadable."""
    time.sleep(RATE_READBACK_DELAY_S)
    for sub in ("pcm0p", "pcm0c"):
        if substream_owner(args.card, sub) is not None:
            print("  rate readback skipped: someone else opened the card meanwhile")
            return None
    value, _ = amixer_get(args.card, "Digital Input Rate")
    if value is None:
        print("  rate readback FAILED (control read error) - result kept, unverified")
        return None
    ok = value == str(args.rate)
    print(f"  device rate (GET_CUR, idle): {value} Hz - "
          f"{'matches' if ok else 'MISMATCH, run INVALID'}")
    return ok


def cmd_alsa(args):
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _on_signal)
    preflight(args)
    alsa = Alsa()
    x, pos = stimulus(args.rate)
    ref = x[:, 0]
    rows = []
    for spec in args.buffers.split(","):
        period, periods = (int(v) for v in spec.split("x"))
        for rep in range(args.reps):
            print(f"\nrate {args.rate}  period {period} x {periods}  run {rep + 1}/{args.reps}")
            try:
                c, extra, rates = run_once(alsa, args, period, periods, x)
            except RuntimeError as e:
                print(f"  FAILED: {e}")
                continue
            if rates:
                print(f"  ALSA rate: playback {rates[0]}, capture {rates[1]}")
            rate_ok = verify_rate(args)
            if args.save:
                write_wav24(f"{args.save}-{args.rate}-{period}x{periods}-{rep + 1}.wav",
                            c, args.rate)
            tap = report(f"ch{args.tap_ch} (tap)", ref, c[:, args.tap_ch - 1], pos,
                         args.rate)[0]
            ana = report(f"ch{args.analog_ch} (analog)", ref, c[:, args.analog_ch - 1], pos,
                         args.rate)[0]
            if extra:
                print(f"  driver-reported extra delay: playback {extra[0]}  capture {extra[1]}"
                      f"  (sum {extra[0] + extra[1]})")
            print(f"  analog - tap = {ana - tap:.2f} samples (converters + analog path)")
            if rate_ok is not False:
                rows.append((period, periods, tap, ana, extra, rate_ok))
    if rows:
        print("\nsummary (samples):  period x n   tap      analog   analog-tap  reported  rate")
        for period, periods, tap, ana, extra, rate_ok in rows:
            rep = f"{extra[0] + extra[1]}" if extra else "-"
            print(f"                    {period:5d} x {periods}  {tap:8.2f} {ana:8.2f} "
                  f"{ana - tap:9.2f}   {rep:>8}  {'ok' if rate_ok else 'unverified'}")
    else:
        print("\nno valid runs")
        sys.exit(1)


def cmd_gen(args):
    x, pos = stimulus(args.rate)
    write_wav24(args.out, x[:, :args.channels], args.rate)
    print(f"wrote {args.out}: {args.channels} ch, {len(x)} frames @ {args.rate}, impulses at frames "
          f"{pos[0]}..{pos[-1]} every {pos[1] - pos[0]}")


def cmd_analyze(args):
    """Returns {channel: (lag, polarity, corr, spread)}."""
    sig_all, rate = read_wav(args.wav)
    x, pos = stimulus(rate)
    ref = x[:, 0]
    chans = [int(c) for c in args.channels.split(",")] if args.channels else \
        list(range(1, sig_all.shape[1] + 1))
    print(f"{args.wav}: {sig_all.shape[1]} ch @ {rate}; stimulus assumed to start at "
          f"frame {args.start} of this file")
    return {ch: report(f"ch{ch}", ref, sig_all[args.start:, ch - 1], pos, rate)
            for ch in chans}


def cmd_null(args):
    """Null test of one analog channel between two captures (e.g. control
    untouched vs. set to -10 dB and back to 0 dB), aligned on the digital
    tap so a different start skew between the runs cannot fake a residual.
    Returns (shift, level_db, residual_db)."""
    a_all, ra = read_wav(args.a)
    b_all, rb = read_wav(args.b)
    if ra != rb:
        sys.exit("rates differ")
    tap_a, tap_b = a_all[:, args.tap_ch - 1], b_all[:, args.tap_ch - 1]
    shift, _, _ = find_offset(tap_a, tap_b, int(MAXLAG_S * ra))
    shift = int(round(shift))
    a, b = a_all[:, args.ch - 1], b_all[:, args.ch - 1]
    if shift > 0:
        b = b[shift:]
    elif shift < 0:
        a = a[-shift:]
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]

    def rms(v):
        return np.sqrt(np.mean(v ** 2)) or 1e-12

    level = 20 * np.log10(rms(b) / rms(a))
    resid = 20 * np.log10(rms(b - a) / rms(a))
    print(f"tap realignment {shift} samples; ch{args.ch}: level B-A {level:+.3f} dB, "
          f"residual {resid:.1f} dB below signal")
    print("compare against the same command on two baseline runs (A vs A'): that "
          "residual is the noise floor of the null itself")
    return shift, level, resid


def parse_rpp(path):
    """Tracks of a saved REAPER project: [{name, items: [{position, soffs,
    playrate, file, takes}]}]. Only what `rpp` needs; FILE paths are made
    absolute relative to the project's own directory."""
    base = os.path.dirname(os.path.abspath(path))
    tracks, stack = [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("<"):
                kind = line[1:].split(" ", 1)[0]
                stack.append(kind)
                if kind == "TRACK":
                    tracks.append({"name": "", "items": []})
                elif kind == "ITEM" and tracks:
                    tracks[-1]["items"].append({"position": 0.0, "soffs": 0.0,
                                                "playrate": 1.0, "file": None, "takes": 1})
                continue
            if line == ">":
                if stack:
                    stack.pop()
                continue
            key, _, rest = line.partition(" ")
            if not stack or not tracks:
                continue
            if stack[-1] == "TRACK" and key == "NAME":
                tracks[-1]["name"] = rest.strip().strip('"')
            elif "ITEM" in stack and tracks[-1]["items"]:
                item = tracks[-1]["items"][-1]
                if stack[-1] == "ITEM" and key == "POSITION":
                    item["position"] = float(rest.split()[0])
                elif stack[-1] == "ITEM" and key == "SOFFS" and item["takes"] == 1:
                    item["soffs"] = float(rest.split()[0])
                elif stack[-1] == "ITEM" and key == "PLAYRATE" and item["takes"] == 1:
                    item["playrate"] = float(rest.split()[0])
                elif stack[-1] == "ITEM" and key == "TAKE":
                    item["takes"] += 1
                elif key == "FILE" and item["file"] is None:
                    name = rest.strip().split('"')[1] if rest.strip().startswith('"') \
                        else rest.strip().split()[0]
                    item["file"] = name if os.path.isabs(name) else os.path.join(base, name)
    return tracks


def cmd_rpp(args):
    """REAPER's own latency compensation error, straight from the saved
    project: the recorded WAV's raw offset against the stimulus, minus the
    shift REAPER applied to the item (SOFFS - POSITION). Needs no render.
    Returns {track: error_samples} (first item of each track)."""
    tracks = {t["name"]: t for t in parse_rpp(args.project)}
    results = {}
    for name in args.tracks.split(","):
        t = tracks.get(name)
        if not t or not t["items"]:
            print(f"{name}: no track/item of that name in {args.project}")
            continue
        for n, item in enumerate(t["items"]):
            if not item["file"] or not os.path.exists(item["file"]):
                print(f"{name} item {n + 1}: recorded file missing ({item['file']})")
                continue
            sig, rate = read_wav(item["file"])
            x, pos = stimulus(rate)
            print(f"{name} item {n + 1}: {os.path.basename(item['file'])} @ {rate} Hz, "
                  f"POSITION {item['position']:.6f} s, SOFFS {item['soffs']:.6f} s")
            lag = report("raw recording", x[:, 0], sig[:, 0], pos, rate)[0]
            comp = (item["soffs"] - item["position"]) * rate
            err = lag - comp
            warn = ""
            if abs(item["playrate"] - 1.0) > 1e-9:
                warn += f"  <-- PLAYRATE {item['playrate']} != 1"
            if item["takes"] > 1:
                warn += "  <-- several takes, first one used"
            if abs(item["position"]) > 0.05:
                warn += "  <-- recording did not start at 0 - restart from the beginning"
            print(f"  REAPER compensation {comp:8.2f} samples; "
                  f"remaining error {err:8.2f} samples ({err / rate * 1e3:.3f} ms){warn}")
            results.setdefault(name, err)
    return results


class _SimLib:
    """Stand-in for libasound, for the selftest only: a simulated device
    in virtual time with the driver's shape - playback consumed in 1 ms
    steps, capture delivered in 32-frame bulk URBs - whose capture ch5
    carries the playback stream TAP frames late and ch3 TAP+ANALOG late.
    Virtual time only advances inside snd_pcm_wait(), i.e. an ideal host:
    this proves the I/O loop's logic and index bookkeeping, not real-time
    behaviour."""
    TAP, ANALOG, CAP_LAG, CAP_URB = 217, 53, 40, 32

    def __init__(self, rate, buf, period):
        self.rate, self.buf, self.period = rate, buf, period
        self.t = 0                       # virtual time in frames since start
        self.started = self.xrun = False
        self.appl_p = self.appl_c = 0
        self.play = np.zeros(rate * 20, dtype=np.int64)  # ch1 of every written frame

    def hw_p(self):
        return (self.t * 1000 // self.rate) * self.rate // 1000 if self.started else 0

    def hw_c(self):
        if not self.started or self.t < self.CAP_LAG:
            return 0
        return (self.t - self.CAP_LAG) // self.CAP_URB * self.CAP_URB

    def check(self):
        if self.hw_p() > self.appl_p or self.hw_c() - self.appl_c > self.buf:
            self.xrun = True
        return -EPIPE if self.xrun else 0

    def snd_pcm_avail(self, h):
        if self.check():
            return -EPIPE
        if h == "P":
            return self.buf - (self.appl_p - self.hw_p())
        return self.hw_c() - self.appl_c

    def snd_pcm_writei(self, h, ptr, n):
        a = self.snd_pcm_avail("P")
        if a < 0:
            return a
        k = min(n, a)
        if k <= 0:
            return -EAGAIN
        b = np.frombuffer(ctypes.string_at(ptr.value, k * PLAY_CH * 3), dtype=np.uint8)
        b = b.reshape(k, PLAY_CH * 3)[:, :3].astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        self.play[self.appl_p:self.appl_p + k] = np.where(v >= 1 << 23, v - (1 << 24), v)
        self.appl_p += k
        return k

    def snd_pcm_readi(self, h, ptr, n):
        a = self.snd_pcm_avail("C")
        if a < 0:
            return a
        k = min(n, a)
        if k <= 0:
            return -EAGAIN
        frames = np.zeros((k, CAP_CH), dtype="<i4")
        for col, lag in ((4, self.TAP), (2, self.TAP + self.ANALOG)):
            idx = np.arange(self.appl_c, self.appl_c + k) - lag
            ok = (idx >= 0) & (idx < self.appl_p)
            frames[ok, col] = self.play[idx[ok]] << 8
        ctypes.memmove(ptr.value, frames.tobytes(), frames.nbytes)
        self.appl_c += k
        return k

    def snd_pcm_wait(self, h, ms):
        if self.check():
            return -EPIPE
        need = self.appl_c + self.period
        t_ready = self.CAP_LAG + -(-need // self.CAP_URB) * self.CAP_URB
        t_end = self.t + max(1, ms * self.rate // 1000)
        self.t = min(t_ready, t_end) if self.hw_c() < need else self.t
        if self.check():
            return -EPIPE
        return 1 if self.hw_c() >= need else 0

    def snd_pcm_avail_delay(self, h, avail, delay):
        a = self.snd_pcm_avail(h)
        avail._obj.value = a
        delay._obj.value = self.buf - a if h == "P" else a
        return 0

    def snd_pcm_start(self, h):
        self.started = True
        return 0

    def snd_pcm_link(self, a, b):
        return 0

    def snd_pcm_drop(self, h):
        return 0

    def snd_pcm_unlink(self, h):
        return 0

    def snd_pcm_close(self, h):
        return 0

    def snd_strerror(self, r):
        return b"simulated error"


class _SimAlsa(Alsa):
    def __init__(self, rate, buf, period):     # pylint: disable=super-init-not-called
        self.lib = _SimLib(rate, buf, period)

    def open(self, dev, stream, fmt, ch, rate, period, periods):
        return "P" if stream == SND_PCM_STREAM_PLAYBACK else "C"


def _fractional_delay(sig, lag, pol=1):
    n = len(sig)
    f = np.fft.rfftfreq(n)
    spec = np.fft.rfft(sig) * np.exp(-2j * np.pi * f * lag)
    spec[f > 0.45] = 0                          # band-limit like a codec filter
    return pol * np.fft.irfft(spec, n)


def cmd_selftest(args):
    rate = 48000
    x, pos = stimulus(rate)
    ref = x[:, 0]
    rng = np.random.default_rng(1)
    failures = []

    print("find_offset:")
    for true_lag, pol in ((187.3, -1), (41.0, 1), (-12.6, 1), (0.0, 1)):
        sig = _fractional_delay(ref, true_lag, pol) * 0.9
        sig += rng.normal(0, 10 ** (-90 / 20), len(sig))
        lag, p, q = find_offset(ref, sig, int(MAXLAG_S * rate))
        good = abs(lag - true_lag) < 0.2 and p == pol
        if not good:
            failures.append(f"find_offset {true_lag}")
        print(f"  true {true_lag:7.2f} pol {pol:+d} -> found {lag:7.2f} pol {p:+d} "
              f"corr {q:.2f}  {'ok' if good else 'FAIL'}")

    # synthetic 6ch captures: ch3 = analog (233.4 late, inverted, band-limited),
    # ch5 = digital tap (exactly 192 late); B = same take started 7 samples
    # later with ch3 0.1 dB quieter
    n = len(x)
    cap_a = np.zeros((n, CAP_CH))
    cap_a[:, 2] = _fractional_delay(ref, 233.4, -1) * 0.9
    cap_a[192:, 4] = ref[:n - 192]
    cap_a += rng.normal(0, 3e-5, cap_a.shape)
    cap_b = np.vstack([np.zeros((7, CAP_CH)), cap_a[:-7]])
    cap_b[:, 2] *= 10 ** (-0.1 / 20)
    cap_b[:, 2] += rng.normal(0, 3e-5, n)

    keep = args.keep
    tmp = None if keep else tempfile.TemporaryDirectory()
    out_dir = keep or tmp.name
    os.makedirs(out_dir, exist_ok=True)
    path_a = os.path.join(out_dir, "synthetic_capture_a.wav")
    path_b = os.path.join(out_dir, "synthetic_capture_b.wav")
    write_wav24(path_a, cap_a, rate)
    write_wav24(path_b, cap_b, rate)

    print("\nanalyze (synthetic capture A; expect ch3 ~233.4 -, ch5 192.00 +, ch1 weak):")
    res = cmd_analyze(argparse.Namespace(wav=path_a, channels="3,5,1", start=0))
    if not (abs(res[3][0] - 233.4) < 0.3 and res[3][1] == -1):
        failures.append("analyze ch3")
    if not (abs(res[5][0] - 192.0) < 0.01 and res[5][1] == 1 and res[5][3] == 0):
        failures.append("analyze ch5")
    if not res[1][2] < 0.3:
        failures.append("analyze ch1 (should be flagged weak)")

    print("\nnull (A vs B; expect realignment 7, level -0.100 dB):")
    shift, level, resid = cmd_null(argparse.Namespace(a=path_a, b=path_b, ch=3, tap_ch=5))
    if not (shift == 7 and abs(level + 0.1) < 0.01 and resid < -30):
        failures.append("null")
    print("\nnull (A vs A; expect realignment 0, level +0.000 dB, no residual):")
    shift, level, resid = cmd_null(argparse.Namespace(a=path_a, b=path_a, ch=3, tap_ch=5))
    if not (shift == 0 and abs(level) < 1e-6 and resid < -150):
        failures.append("null identical")

    # The real run_once() I/O loop against the simulated device, at the
    # buffer sizes that xran on hardware (64x2) and a larger one.
    print(f"\nrun_once on a simulated device (tap {_SimLib.TAP}, analog "
          f"{_SimLib.TAP + _SimLib.ANALOG}; ideal host, so this checks logic, not timing):")
    for sim_rate, period, periods in ((48000, 64, 2), (44100, 64, 2), (48000, 256, 2)):
        sim = _SimAlsa(sim_rate, period * periods, period)
        sx, spos = stimulus(sim_rate)
        try:
            c, extra, _ = run_once(sim, argparse.Namespace(rate=sim_rate, card="sim"),
                                   period, periods, sx)
        except RuntimeError as e:
            failures.append(f"sim {sim_rate} {period}x{periods}: {e}")
            print(f"  {sim_rate} {period}x{periods}: FAILED {e}")
            continue
        print(f"  {sim_rate} {period}x{periods}:")
        tap = report("ch5 (tap)", sx[:, 0], c[:, 4], spos, sim_rate)
        ana = report("ch3 (analog)", sx[:, 0], c[:, 2], spos, sim_rate)
        if not (abs(tap[0] - _SimLib.TAP) < 0.01 and tap[3] == 0 and
                abs(ana[0] - _SimLib.TAP - _SimLib.ANALOG) < 0.01 and extra == (0, 0)):
            failures.append(f"sim {sim_rate} {period}x{periods}")

    # ...and the simulator does detect an underrun (else the above is vacuous)
    sim = _SimLib(48000, 128, 64)
    sim.snd_pcm_start("P")
    sim.snd_pcm_wait("C", 5)
    if sim.snd_pcm_avail("P") != -EPIPE:
        failures.append("sim does not detect underruns")
    print(f"  simulator underrun check: {'ok' if sim.xrun else 'FAIL'}")

    # rpp: a saved REAPER project whose recorded items are 270 (in3) and
    # 217 (in5) samples late in the raw files, with REAPER having shifted
    # both by 192 samples (SOFFS 0.004 s) -> remaining error 78 and 25.
    m3 = np.zeros(n)
    m3[270:] = ref[:n - 270]
    m5 = np.zeros(n)
    m5[217:] = ref[:n - 217]
    os.makedirs(os.path.join(out_dir, "recorded"), exist_ok=True)
    write_wav24(os.path.join(out_dir, "recorded", "in3.wav"), m3[:, None], rate)
    write_wav24(os.path.join(out_dir, "recorded", "in5.wav"), m5[:, None], rate)
    rpp_path = os.path.join(out_dir, "synthetic.rpp")
    with open(rpp_path, "w", encoding="ascii") as f:
        for name in ("in3", "in5"):
            f.write(f'<TRACK\n  NAME {name}\n  <ITEM\n    POSITION 0\n    SOFFS 0.004\n'
                    f'    PLAYRATE 1 1 0 -1 0 0.0025\n    <SOURCE WAVE\n'
                    f'      FILE "recorded/{name}.wav"\n    >\n  >\n>\n')
    print("\nrpp (synthetic project; expect remaining error 78.00 for in3, 25.00 for in5):")
    res = cmd_rpp(argparse.Namespace(project=rpp_path, tracks="in3,in5"))
    if not (abs(res.get("in3", 1e9) - 78) < 0.01 and abs(res.get("in5", 1e9) - 25) < 0.01):
        failures.append("rpp")

    if tmp:
        tmp.cleanup()
    else:
        print(f"\nsynthetic files kept in {out_dir}")
    print(f"\nselftest: {'PASS' if not failures else 'FAIL: ' + ', '.join(failures)}")
    sys.exit(0 if not failures else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen", help="write the stimulus WAV")
    g.add_argument("--rate", type=int, default=48000, choices=RATES)
    g.add_argument("--out", default="px_latency_stimulus.wav")
    g.add_argument("--channels", type=int, default=PLAY_CH, choices=(1, 2, PLAY_CH),
                   help="1 = mono (for REAPER: plays on both sides of a stereo track), "
                        "2 = Out 1/2 only, 4 = all outputs (default)")
    a = sub.add_parser("alsa", help="raw-ALSA linked play+capture measurement "
                       "(opens the device - user only)")
    a.add_argument("--card", default="PhaseX64", help="ALSA card id (default %(default)s)")
    a.add_argument("--rate", type=int, default=48000, choices=RATES,
                   help="88.2/96 kHz: tested once without PipeWire (tests/hw/REPORT.md, "
                        "\"Acceptance tests (2026-10-14 ...)\"); small periods there may "
                        "xrun in this Python loop - such a run is reported FAILED")
    a.add_argument("--buffers", default="64x2,64x3,128x2,256x2,512x2",
                   help="comma list of PERIODxPERIODS (default %(default)s)")
    a.add_argument("--reps", type=int, default=3, help="fresh open/start per repetition")
    a.add_argument("--analog-ch", type=int, default=3, help="1-based analog loopback capture ch")
    a.add_argument("--tap-ch", type=int, default=5, help="1-based digital tap capture ch")
    a.add_argument("--cabled", action="store_true",
                   help="Out 1/2 is patched into the analog channel by cable; skip the "
                        "In 3/4 = Loopback 1/2 check")
    a.add_argument("--save", default=None, help="prefix: also save each 6ch capture as WAV")
    z = sub.add_parser("analyze", help="offsets in an existing recording")
    z.add_argument("wav")
    z.add_argument("--channels", default=None, help="comma list, 1-based (default: all)")
    z.add_argument("--start", type=int, default=0,
                   help="frame in the WAV where the stimulus starts (default 0)")
    nl = sub.add_parser("null", help="null test of two saved captures")
    nl.add_argument("a")
    nl.add_argument("b")
    nl.add_argument("--ch", type=int, default=3, help="1-based analog channel to null")
    nl.add_argument("--tap-ch", type=int, default=5, help="1-based tap channel for alignment")
    rp = sub.add_parser("rpp", help="REAPER's compensation error, from a saved project")
    rp.add_argument("project", help="the saved .rpp with the recorded takes")
    rp.add_argument("--tracks", default="in3,in5", help="track names (default %(default)s)")
    st = sub.add_parser("selftest", help="offline check of analysis, analyze, null, rpp "
                        "and the I/O loop (simulated device)")
    st.add_argument("--keep", default=None,
                    help="directory to keep the synthetic WAVs in (default: temporary)")
    args = ap.parse_args()
    cmds = {"gen": cmd_gen, "alsa": cmd_alsa, "analyze": cmd_analyze, "null": cmd_null,
            "rpp": cmd_rpp, "selftest": cmd_selftest}
    try:
        cmds[args.cmd](args)
    except (KeyboardInterrupt, Interrupted) as e:
        print(f"\ninterrupted ({e or 'SIGINT'}) - any open PCMs were dropped and closed")
        sys.exit(130)


if __name__ == "__main__":
    main()
