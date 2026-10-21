#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Source-level invariants for the Windows-equivalent codec resync path."""

from __future__ import annotations

import unittest
from pathlib import Path


DRIVER = (Path(__file__).resolve().parents[2] /
          "linux/snd-usb-phasex64/phasex64.c")


def function(source: str, name: str) -> str:
    start = source.index(f"static int {name}(")
    end = source.index("\nstatic ", start + 1)
    return source[start:end]


class CodecResyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = DRIVER.read_text()

    def test_default_is_enabled_with_escape_hatch(self) -> None:
        self.assertIn("static bool codec_resync = true;", self.source)
        self.assertIn("module_param(codec_resync, bool, 0444);", self.source)

    def test_resync_uses_only_current_shadow_values(self) -> None:
        body = function(self.source, "px_codec_resync_shadow")
        self.assertIn("{ WM_R7, WM_ADC_MUX, WM_OUT_MUX }", body)
        self.assertIn("for (c = 0; c < 2; c++)", body)
        self.assertIn("px->codec[c][regs[i]]", body)
        self.assertNotIn("WM_R7_STEREO", body)
        self.assertIn("px_ctrl_retry", body)

    def test_resync_is_between_status_read_and_set_cur(self) -> None:
        body = function(self.source, "px_start_streaming")
        status = body.index("px_status_read")
        resync = body.index("px_codec_resync_shadow")
        set_cur = body.index("px_set_rate_ep")
        self.assertLess(status, resync)
        self.assertLess(resync, set_cur)

    def test_start_is_serialized_with_mixer_puts(self) -> None:
        body = function(self.source, "px_pcm_prepare")
        lock = body.index("mutex_lock(&px->mutex)")
        start = body.index("px_start_streaming")
        unlock = body.index("mutex_unlock(&px->mutex)")
        self.assertLess(lock, start)
        self.assertLess(start, unlock)


if __name__ == "__main__":
    unittest.main(verbosity=2)
