#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""px_latency.py - sample-accurate round-trip latency measurement, PHASE X64.

Not run by the assistant: the `alsa` subcommand opens hw:PhaseX64 directly
(raw ALSA, PipeWire stopped) and is for the user to run by hand. `gen`,
`analyze`, `null` and `selftest` never touch any audio device. See
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
  selftest  offline check of the analysis, `analyze` and `null` on synthetic
            data; exits 0 only if every check passes.

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
WAIT_MS = 500       # snd_pcm_wait() timeout per poll
STALL_S = 2.0       # no capture data (or no playback space) for this long = abort
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

    def wait(self, h, what):
        """Block (in WAIT_MS slices, so signals get through) until `h` is
        ready; raises on xrun, on STALL_S without progress, or on error."""
        waited = 0.0
        while True:
            r = self.lib.snd_pcm_wait(h, WAIT_MS)
            if r > 0:
                return
            if r == 0:
                waited += WAIT_MS / 1000
                if waited >= STALL_S:
                    raise RuntimeError(f"{what}: no progress for {STALL_S:.0f} s "
                                       "(device stalled?) - run invalid")
                continue
            if r == -EINTR:
                continue
            if r in (-EPIPE, -ESTRPIPE):
                raise RuntimeError(f"{what}: xrun - run invalid")
            self.chk(r, f"{what}: snd_pcm_wait")

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

        total = len(x) + buf + int(MAXLAG_S * rate)
        out = np.zeros((total + buf, PLAY_CH))
        out[:len(x)] = x
        pdata = to_s24_3le(out.reshape(-1))
        pbuf = ctypes.create_string_buffer(pdata, len(pdata))
        pbase = ctypes.addressof(pbuf)
        pf = PLAY_CH * 3
        cbuf = (ctypes.c_int32 * (CAP_CH * period))()
        carr = np.ctypeslib.as_array(cbuf)
        captured = []

        def write(frames, wpos, what):
            done = 0
            while done < frames:
                w = L.snd_pcm_writei(play, ctypes.c_void_p(pbase + (wpos + done) * pf),
                                     frames - done)
                if w == -EAGAIN:
                    alsa.wait(play, what)
                    continue
                if w in (-EPIPE, -ESTRPIPE):
                    raise RuntimeError(f"{what}: playback underrun (xrun) - run invalid")
                alsa.chk(w, what)
                done += w
            return done

        # prefill the whole playback buffer, then one linked start
        wpos = write(buf, 0, "prefill")
        alsa.chk(L.snd_pcm_start(play), "snd_pcm_start (linked)")
        t_start = time.monotonic()
        extra = None
        got = 0
        while got < total:
            alsa.wait(cap, "capture")
            r = L.snd_pcm_readi(cap, cbuf, period)
            if r == -EAGAIN:
                continue
            if r in (-EPIPE, -ESTRPIPE):
                raise RuntimeError("capture overrun (xrun) - run invalid")
            alsa.chk(r, "readi")
            captured.append(carr[:r * CAP_CH].copy())
            got += r
            wpos += write(r, wpos, "playback")
            if extra is None and time.monotonic() - t_start > 0.3:
                extra = (alsa.extra_delay(play, True, buf), alsa.extra_delay(cap, False, buf))
                print(f"  ALSA rate: playback {proc_rate(args.card, 'pcm0p')}, "
                      f"capture {proc_rate(args.card, 'pcm0c')}")
        c = np.concatenate(captured).reshape(-1, CAP_CH) / float(1 << 31)
        return c, extra
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
                c, extra = run_once(alsa, args, period, periods, x)
            except RuntimeError as e:
                print(f"  FAILED: {e}")
                continue
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
    write_wav24(args.out, x, args.rate)
    print(f"wrote {args.out}: {len(x)} frames @ {args.rate}, impulses at frames "
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
    st = sub.add_parser("selftest", help="offline check of analysis, analyze and null")
    st.add_argument("--keep", default=None,
                    help="directory to keep the synthetic WAVs in (default: temporary)")
    args = ap.parse_args()
    cmds = {"gen": cmd_gen, "alsa": cmd_alsa, "analyze": cmd_analyze, "null": cmd_null,
            "selftest": cmd_selftest}
    try:
        cmds[args.cmd](args)
    except (KeyboardInterrupt, Interrupted) as e:
        print(f"\ninterrupted ({e or 'SIGINT'}) - any open PCMs were dropped and closed")
        sys.exit(130)


if __name__ == "__main__":
    main()
