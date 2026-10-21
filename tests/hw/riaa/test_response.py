#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Unit tests and printable tables for the selected RIAA biquad."""

from __future__ import annotations

import cmath
import math
import unittest

from riaa import (EXTRA_ZEROS, RATES, analog_db, bilinear_coefficients,
                  coefficients, digital_db, matched_z_coefficients, q30)


FREQUENCIES = (20, 50, 100, 500, 1000, 2000, 10000, 15000, 20000)
EXPECTED_Q30 = {
    44100: (245790958, -188270260, -37809732, -1859698319, 787947107),
    48000: (230659399, -181190551, -32632425, -1880052333, 808011037),
    88200: (136209875, -116131926, -14770980, -1993016395, 919810722),
    96000: (125948718, -107977688, -13463856, -2004734401, 931447929),
}
MAX_INBAND_ERROR_DB = {
    44100: 0.342948,
    48000: 0.222990,
    88200: 0.014311,
    96000: 0.010007,
}


def pole_radii(coefs: tuple[float, ...]) -> tuple[float, float]:
    *_, a1, a2 = coefs
    discriminant = complex(a1 * a1 - 4.0 * a2)
    return (abs((-a1 + cmath.sqrt(discriminant)) / 2.0),
            abs((-a1 - cmath.sqrt(discriminant)) / 2.0))


class RIAAResponseTest(unittest.TestCase):
    def test_reference_table_is_regenerated_by_hybrid(self) -> None:
        for rate in RATES:
            generated = matched_z_coefficients(rate, EXTRA_ZEROS[rate])
            for actual, expected in zip(generated, coefficients(rate)):
                with self.subTest(rate=rate):
                    self.assertAlmostEqual(actual, expected, places=14)

    def test_response_against_analog_curve(self) -> None:
        # Dense log grid catches extrema between the requested report points.
        points = 20001
        for rate in RATES:
            maximum = 0.0
            for index in range(points):
                frequency = 20.0 * (1000.0 ** (index / (points - 1)))
                deviation = digital_db(coefficients(rate), frequency, rate) - analog_db(frequency)
                maximum = max(maximum, abs(deviation))
            with self.subTest(rate=rate):
                self.assertLessEqual(maximum, MAX_INBAND_ERROR_DB[rate])

    def test_one_khz_is_unity(self) -> None:
        for rate in RATES:
            with self.subTest(rate=rate):
                self.assertAlmostEqual(digital_db(coefficients(rate), 1000, rate),
                                       0.0, places=10)

    def test_poles_are_stable(self) -> None:
        for rate in RATES:
            with self.subTest(rate=rate):
                self.assertTrue(all(radius < 1.0
                                    for radius in pole_radii(coefficients(rate))))

    def test_q30_reference_and_quantization(self) -> None:
        for rate in RATES:
            floating = coefficients(rate)
            quantized = q30(floating)
            with self.subTest(rate=rate):
                self.assertEqual(quantized, EXPECTED_Q30[rate])
                self.assertLessEqual(max(abs(a - b / (1 << 30))
                                         for a, b in zip(floating, quantized)),
                                     0.5 / (1 << 30))
            qcoefs = tuple(value / (1 << 30) for value in quantized)
            maximum = max(abs(digital_db(qcoefs, frequency, rate) -
                              digital_db(floating, frequency, rate))
                          for frequency in (20.0 * (1000.0 ** (i / 10000.0))
                                            for i in range(10001)))
            self.assertLessEqual(maximum, 5e-6)

    def test_selected_design_fixes_high_frequency_warp(self) -> None:
        for rate in RATES:
            old = bilinear_coefficients(rate)
            for frequency in (10000, 15000, 20000):
                old_error = abs(digital_db(old, frequency, rate) - analog_db(frequency))
                new_error = abs(digital_db(coefficients(rate), frequency, rate) -
                                analog_db(frequency))
                with self.subTest(rate=rate, frequency=frequency):
                    self.assertLess(new_error, old_error)


def print_report() -> None:
    print("coefficients: b0 b1 b2 a1 a2 (a0 = 1)")
    for rate in RATES:
        coefs = coefficients(rate)
        print(f"{rate:5d}: " + " ".join(f"{value:+.15f}" for value in coefs))
        print("       Q30: " + " ".join(str(value) for value in q30(coefs)))
        print("       pole radii: " + " ".join(f"{value:.15f}"
                                                for value in pole_radii(coefs)))

    print("\nresponse: digital dB (deviation from analogue dB)")
    print("rate " + " ".join(f" {frequency:>8} Hz" for frequency in FREQUENCIES))
    for rate in RATES:
        coefs = coefficients(rate)
        print(f"{rate:5d}", end="")
        for frequency in FREQUENCIES:
            value = digital_db(coefs, frequency, rate)
            deviation = value - analog_db(frequency)
            print(f" {value:+7.3f} ({deviation:+7.3f})", end="")
        print()


if __name__ == "__main__":
    print_report()
    print()
    unittest.main(argv=[__file__], verbosity=2)
