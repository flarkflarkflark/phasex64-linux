#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Bit-exact model tests for the kernel RIAA capture-path state machine."""

from __future__ import annotations

import random
import re
import unittest
from pathlib import Path

import numpy as np

from fixed_point import S24_MAX, S24_MIN, df1, round_shift
from riaa import RATES, coefficients, q30


REPO = Path(__file__).resolve().parents[3]
DRIVER = REPO / "linux/snd-usb-phasex64/phasex64.c"
Q = 30
STATE_BITS = 8


def driver_coefficients() -> dict[int, tuple[int, ...]]:
    source = DRIVER.read_text()
    table = source.split("px_riaa_coeffs[] = {", 1)[1].split("};", 1)[0]
    rows = re.findall(r"\{\s*(\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),"
                      r"\s*(-?\d+),\s*(-?\d+)\s*\}", table)
    return {int(row[0]): tuple(map(int, row[1:])) for row in rows}


class CaptureModel:
    def __init__(self, master: bool = True):
        self.master = master
        self.switches = [True, True]
        self.phono = [False, False]
        self.states = [[0, 0, 0, 0] for _ in range(4)]  # x1, x2, y1, y2
        self.coefs = None

    def reset_pair(self, pair: int) -> None:
        for channel in range(pair * 2, pair * 2 + 2):
            self.states[channel] = [0, 0, 0, 0]

    def reset(self) -> None:
        for pair in range(2):
            self.reset_pair(pair)

    def set_rate(self, rate: int) -> None:
        self.coefs = driver_coefficients()[rate]
        self.reset()

    def stream_start(self) -> None:
        self.reset()

    def set_source(self, pair: int, phono: bool) -> None:
        if self.phono[pair] != phono:
            self.phono[pair] = phono
            self.reset_pair(pair)

    def set_switch(self, pair: int, enabled: bool) -> None:
        if self.switches[pair] != enabled:
            self.switches[pair] = enabled
            self.reset_pair(pair)

    def sample(self, channel: int, value: int) -> int:
        b0, b1, b2, a1, a2 = self.coefs
        x1, x2, y1, y2 = self.states[channel]
        accumulator = ((b0 * value + b1 * x1 + b2 * x2) << STATE_BITS)
        accumulator -= a1 * y1 + a2 * y2
        y = round_shift(accumulator, Q)
        y = min(S24_MAX << STATE_BITS, max(S24_MIN << STATE_BITS, y))
        output = round_shift(y, STATE_BITS)
        output = min(S24_MAX, max(S24_MIN, output))
        self.states[channel] = [value, x1, y, y1]
        return output

    def frame(self, values: list[int]) -> list[int]:
        result = list(values)
        for pair in range(2):
            active = self.master and self.switches[pair] and self.phono[pair]
            if active:
                for channel in range(pair * 2, pair * 2 + 2):
                    result[channel] = self.sample(channel, result[channel])
        return result


class KernelRIAAModelTest(unittest.TestCase):
    def test_driver_q30_table_matches_reference(self) -> None:
        self.assertEqual(driver_coefficients(),
                         {rate: q30(coefficients(rate)) for rate in RATES})

    def test_fixed_point_equivalence_all_rates(self) -> None:
        rng = random.Random(0x5A17)
        samples = np.asarray([rng.randint(S24_MIN, S24_MAX)
                              for _ in range(2000)], dtype=np.int64)
        for rate in RATES:
            model = CaptureModel()
            model.set_rate(rate)
            model.set_source(0, True)
            actual = np.asarray([model.frame([int(value), 0, 0, 0, 0, 0])[0]
                                 for value in samples], dtype=np.int64)
            expected, _ = df1(samples, q30(coefficients(rate)), fractional=8)
            with self.subTest(rate=rate):
                np.testing.assert_array_equal(actual, expected)

    def test_bypass_is_bit_identical(self) -> None:
        model = CaptureModel()
        model.set_rate(96000)
        frames = ([S24_MIN, -1, 0, 1, S24_MAX, 123456],
                  [S24_MAX, 7, -8, S24_MIN, 42, -42])
        for frame in frames:
            self.assertEqual(model.frame(frame), frame)
        model.set_source(0, True)
        model.set_switch(0, False)
        for frame in frames:
            self.assertEqual(model.frame(frame), frame)

    def test_deserialize_refactor_keeps_s32_container_bits(self) -> None:
        rng = random.Random(0x24B17)
        for _ in range(10000):
            value = rng.randrange(1 << 24)
            old = (value << 8) & 0xffffffff
            signed = value if value < (1 << 23) else value - (1 << 24)
            new = ((signed & 0xffffffff) << 8) & 0xffffffff
            self.assertEqual(new, old)

    def test_pair_switches_and_sources_are_independent(self) -> None:
        model = CaptureModel()
        model.set_rate(48000)
        model.set_source(0, True)
        original = [100000, -100000, 200000, -200000, 300000, -300000]
        first = model.frame(original)
        self.assertNotEqual(first[:2], original[:2])
        self.assertEqual(first[2:], original[2:])

        model.set_switch(0, False)
        model.set_source(1, True)
        second = model.frame(original)
        self.assertEqual(second[:2], original[:2])
        self.assertNotEqual(second[2:4], original[2:4])
        self.assertEqual(second[4:], original[4:])

    def test_all_transitions_reset_state(self) -> None:
        def first_impulse(rate=44100):
            fresh = CaptureModel()
            fresh.set_rate(rate)
            fresh.set_source(0, True)
            return fresh.frame([100000, 0, 0, 0, 0, 0])[0]

        model = CaptureModel()
        model.set_rate(44100)
        model.set_source(0, True)
        for _ in range(100):
            model.frame([100000, 0, 0, 0, 0, 0])
        model.stream_start()
        self.assertEqual(model.frame([100000, 0, 0, 0, 0, 0])[0], first_impulse())

        for _ in range(100):
            model.frame([100000, 0, 0, 0, 0, 0])
        model.set_switch(0, False)
        model.set_switch(0, True)
        self.assertEqual(model.frame([100000, 0, 0, 0, 0, 0])[0], first_impulse())

        for _ in range(100):
            model.frame([100000, 0, 0, 0, 0, 0])
        model.set_source(0, False)
        model.set_source(0, True)
        self.assertEqual(model.frame([100000, 0, 0, 0, 0, 0])[0], first_impulse())

        model.set_rate(96000)
        self.assertEqual(model.frame([100000, 0, 0, 0, 0, 0])[0],
                         first_impulse(96000))

    def test_saturation_clamps_without_wrap(self) -> None:
        model = CaptureModel()
        model.set_rate(44100)
        model.set_source(0, True)
        result = [model.frame([S24_MAX, S24_MIN, 0, 0, 0, 0])[:2]
                  for _ in range(5000)]
        flat = [sample for frame in result for sample in frame]
        self.assertTrue(all(S24_MIN <= sample <= S24_MAX for sample in flat))
        self.assertIn(S24_MAX, flat)
        self.assertIn(S24_MIN, flat)

    def test_conservative_s64_bound(self) -> None:
        for rate, coefs in driver_coefficients().items():
            bound = sum(abs(value) for value in coefs) * (1 << 23) * (1 << 8)
            with self.subTest(rate=rate):
                self.assertLess(bound, 1 << 63)


if __name__ == "__main__":
    unittest.main(verbosity=2)
