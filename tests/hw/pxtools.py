# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Shared helpers for the snd-usb-phasex64 hardware test session.

Minimal, dependency-free (numpy only) WAV read/write that supports the
24-bit (S24_3LE) and 32-bit (S32_LE) PCM the PHASE X64 uses natively,
since the stdlib `wave` module's 3-byte-sample support isn't reliable
enough to trust for this.

IMPORTANT - capture data is NOT full-scale 32-bit. `arecord -f S32_LE`
against this device returns each 24-bit ADC sample left-justified in the
32-bit word (value << 8, see phasex64.c's capture decode comment) - the
low 8 bits are always zero. `read_wav()` on a 32-bit capture file returns
these raw, un-shifted int32 values as-is. For anything that treats the
result as real 24-bit-range audio - re-packing into a 24-bit WAV for
listening, or comparing absolute level against a 24-bit reference -
right-shift by 8 first: `capture_to_24bit(samples)` below, or you will
get garbage (confirmed 2026-10-14: feeding the raw values straight into
write_wav(..., bits=24) produces audible white noise, not the real
signal - this cost an entire false debugging session before the bug was
found). Purely *relative* analysis (FFT band-energy ratios, L/R
correlation, zero-crossing frequency) is unaffected, since a uniform
left-shift is just a constant scale factor.

For a listening check, use `play_file()` (plughw, handles the
conversion automatically) rather than hand-rolling an `aplay -D hw:...`
invocation - see its docstring for why `hw:` is a trap here.
"""
import struct
import subprocess
import numpy as np


def capture_to_24bit(samples):
    """Convert raw 32-bit-container capture samples (24-bit value << 8,
    see the module docstring) to real 24-bit-range values. Use this
    before write_wav(..., bits=24) or any absolute-level comparison."""
    return (samples.astype(np.int64) >> 8).astype(np.int32)


def play_file(path, device="plughw:PhaseX64", timeout=None):
    """Play a WAV file for a listening check. Always use plughw (not hw:)
    for this: plughw's format-conversion plugin does the standard
    32-bit -> 24-bit conversion (take the upper 24 bits), which happens
    to exactly match this device's wire format (24-bit value << 8 in the
    32-bit container, see the module docstring) - so a raw captured file
    can be played directly with no manual bit-shifting needed. `aplay -D
    hw:...` instead requires an exact format match and silently
    misinterprets anything else; that mistake produced loud, convincing
    white noise in this project's 2026-10-14 session and cost an entire
    false debugging detour before it was found (see tests/hw/REPORT.md).
    Returns the completed subprocess.CompletedProcess."""
    return subprocess.run(["aplay", "-D", device, path],
                           capture_output=True, text=True, timeout=timeout)


def write_wav(path, samples, rate, bits):
    """samples: int32 ndarray, shape (nframes, nchannels), values already
    scaled to the target bit depth's full range."""
    nframes, nchannels = samples.shape
    bytes_per_sample = bits // 8
    block_align = nchannels * bytes_per_sample
    byte_rate = rate * block_align

    if bytes_per_sample == 3:
        # pack each sample as little-endian 3 bytes (two's complement)
        flat = samples.astype(np.int32).reshape(-1)
        buf = bytearray(len(flat) * 3)
        for i, v in enumerate(flat):
            v &= 0xFFFFFF
            buf[i * 3 + 0] = v & 0xFF
            buf[i * 3 + 1] = (v >> 8) & 0xFF
            buf[i * 3 + 2] = (v >> 16) & 0xFF
        data = bytes(buf)
    elif bytes_per_sample == 4:
        data = samples.astype('<i4').tobytes()
    elif bytes_per_sample == 2:
        data = samples.astype('<i2').tobytes()
    else:
        raise ValueError(f"unsupported bits={bits}")

    fmt_chunk = struct.pack(
        '<HHIIHH', 1, nchannels, rate, byte_rate, block_align, bits)
    with open(path, 'wb') as f:
        f.write(b'RIFF')
        f.write(struct.pack('<I', 4 + 8 + len(fmt_chunk) + 8 + len(data)))
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write(struct.pack('<I', len(fmt_chunk)))
        f.write(fmt_chunk)
        f.write(b'data')
        f.write(struct.pack('<I', len(data)))
        f.write(data)


def read_wav(path):
    """Generic RIFF/WAVE chunk parser; returns (samples int32 ndarray
    shape (nframes, nchannels), rate, nchannels, bits)."""
    with open(path, 'rb') as f:
        buf = f.read()
    assert buf[0:4] == b'RIFF' and buf[8:12] == b'WAVE', "not a WAV file"
    pos = 12
    fmt = None
    data = None
    while pos < len(buf):
        cid = buf[pos:pos + 4]
        csize = struct.unpack('<I', buf[pos + 4:pos + 8])[0]
        cbody = buf[pos + 8:pos + 8 + csize]
        if cid == b'fmt ':
            fmt = cbody
        elif cid == b'data':
            data = cbody
        pos += 8 + csize + (csize & 1)  # chunks are word-aligned
    assert fmt is not None and data is not None, "missing fmt/data chunk"

    (audio_fmt, nchannels, rate, byte_rate, block_align, bits) = \
        struct.unpack('<HHIIHH', fmt[:16])
    bytes_per_sample = bits // 8

    if bytes_per_sample == 3:
        n = len(data) // 3
        flat = np.empty(n, dtype=np.int32)
        raw = np.frombuffer(data, dtype=np.uint8)[:n * 3].reshape(n, 3)
        v = (raw[:, 0].astype(np.int32) |
             (raw[:, 1].astype(np.int32) << 8) |
             (raw[:, 2].astype(np.int32) << 16))
        v = np.where(v >= 0x800000, v - 0x1000000, v)
        flat = v
    elif bytes_per_sample == 4:
        flat = np.frombuffer(data, dtype='<i4').astype(np.int32)
    elif bytes_per_sample == 2:
        flat = np.frombuffer(data, dtype='<i2').astype(np.int32)
    else:
        raise ValueError(f"unsupported bits={bits}")

    nframes = len(flat) // nchannels
    samples = flat[:nframes * nchannels].reshape(nframes, nchannels)
    return samples, rate, nchannels, bits


def full_scale(bits):
    return (1 << (bits - 1)) - 1


def gen_tone_file(path, rate, duration_s, nchannels, bits, active_ch=None,
                   freq=1000.0, dbfs=-12.0):
    """Write a WAV with a sine of `freq` at `dbfs` on `active_ch` (0-based,
    or all channels if None), silence elsewhere."""
    n = int(round(rate * duration_s))
    t = np.arange(n) / rate
    amp = full_scale(bits) * (10 ** (dbfs / 20.0))
    tone = (amp * np.sin(2 * np.pi * freq * t)).astype(np.int32)
    out = np.zeros((n, nchannels), dtype=np.int32)
    if active_ch is None:
        for c in range(nchannels):
            out[:, c] = tone
    else:
        out[:, active_ch] = tone
    write_wav(path, out, rate, bits)
    return n


def dbfs(rms, bits):
    fs = full_scale(bits)
    if rms <= 0:
        return float('-inf')
    return 20 * np.log10(rms / fs)


def analyze_channel(col, rate):
    """RMS (dBFS-relative, caller passes bits separately) and dominant
    frequency via FFT for a single channel's int samples."""
    x = col.astype(np.float64)
    rms = np.sqrt(np.mean(x ** 2))
    # Hann window to reduce spectral leakage for frequency estimate
    n = len(x)
    if n < 8:
        return rms, 0.0
    w = np.hanning(n)
    spec = np.fft.rfft(x * w)
    mag = np.abs(spec)
    freqs = np.fft.rfftfreq(n, d=1.0 / rate)
    # ignore DC
    mag[0] = 0
    peak = np.argmax(mag)
    # parabolic interpolation around the peak for sub-bin accuracy
    if 0 < peak < len(mag) - 1:
        a, b, c = mag[peak - 1], mag[peak], mag[peak + 1]
        denom = (a - 2 * b + c)
        delta = 0.5 * (a - c) / denom if denom != 0 else 0.0
    else:
        delta = 0.0
    dom_freq = freqs[peak] + delta * (freqs[1] - freqs[0])
    return rms, dom_freq
