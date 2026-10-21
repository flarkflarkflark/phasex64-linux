#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""RIAA playback-EQ coefficient and response helpers.

The analogue transfer function is

    (1 + s * 318 us) / ((1 + s * 3180 us) (1 + s * 75 us))

The selected digital design maps the three finite analogue poles/zero with
matched-z and uses the otherwise surplus digital zero to minimise the maximum
20 Hz--20 kHz magnitude error.  The numerator is normalized so the digital
response is exactly 0 dB at 1 kHz.  Coefficients use

    y[n] = b0*x[n] + b1*x[n-1] + b2*x[n-2]
           - a1*y[n-1] - a2*y[n-2]

The final table is literal reference data: coefficient generation/optimisation
is an offline analysis step, never kernel runtime work.
"""

from __future__ import annotations

import cmath
import math


TAU_3180 = 3180e-6
TAU_318 = 318e-6
TAU_75 = 75e-6
REFERENCE_HZ = 1000.0
RATES = (44100, 48000, 88200, 96000)


# Matched-z finite roots, with one scalar minimax search for the extra zero on
# a 200001-point logarithmic 20 Hz--20 kHz grid.  See design_compare.py.
EXTRA_ZEROS = {
    44100: -0.165198493464524,
    48000: -0.151053364826351,
    88200: -0.112378933941834,
    96000: -0.110459180945781,
}

FINAL_COEFFICIENTS = {
    44100: (0.228910667793636, -0.175340342942947, -0.035213056747011,
            -1.731979026020091, 0.733832928312397),
    48000: (0.214818305484235, -0.168746850591770, -0.030391314430797,
            -1.750935179102223, 0.752518919515998),
    88200: (0.126855331060762, -0.108156284027933, -0.013756547019375,
            -1.856141159796850, 0.856640489535953),
    96000: (0.117298884688064, -0.100562058095814, -0.012539193319164,
            -1.867054403682534, 0.867478483604059),
}


def normalize(coefs: tuple[float, float, float, float, float],
              rate: int) -> tuple[float, float, float, float, float]:
    """Scale a biquad numerator to unity magnitude at 1 kHz."""
    b0, b1, b2, a1, a2 = coefs
    gain = abs(digital_response(coefs, REFERENCE_HZ, rate))
    return b0 / gain, b1 / gain, b2 / gain, a1, a2


def coefficients(rate: int) -> tuple[float, float, float, float, float]:
    """Return selected ``b0, b1, b2, a1, a2`` reference coefficients."""
    return FINAL_COEFFICIENTS[rate]


def bilinear_coefficients(
        rate: int, prewarp_hz: float = REFERENCE_HZ,
) -> tuple[float, float, float, float, float]:
    """Return the original globally-prewarped bilinear design."""
    omega = 2.0 * math.pi * prewarp_hz
    k = omega / math.tan(omega / (2.0 * rate))

    b0 = 1.0 + k * TAU_318
    b1 = 2.0
    b2 = 1.0 - k * TAU_318
    ks = k * (TAU_3180 + TAU_75)
    k2 = k * k * TAU_3180 * TAU_75
    a0 = 1.0 + ks + k2
    a1 = (2.0 - 2.0 * k2) / a0
    a2 = (1.0 - ks + k2) / a0
    return normalize((b0 / a0, b1 / a0, b2 / a0, a1, a2), rate)


def matched_z_coefficients(
        rate: int, extra_zero: float = -1.0,
) -> tuple[float, float, float, float, float]:
    """Map finite analogue roots with matched-z and add *extra_zero*."""
    pole_low = math.exp(-1.0 / (TAU_3180 * rate))
    pole_high = math.exp(-1.0 / (TAU_75 * rate))
    zero = math.exp(-1.0 / (TAU_318 * rate))
    return normalize((1.0, -(zero + extra_zero), zero * extra_zero,
                      -(pole_low + pole_high), pole_low * pole_high), rate)


def breakpoint_prewarped_coefficients(
        rate: int, extra_zero: float = -1.0,
) -> tuple[float, float, float, float, float]:
    """Individually prewarp each finite analogue break frequency.

    This is the useful interpretation of "multiple prewarp" for one biquad:
    locate every real digital root at its exact analogue corner frequency,
    then provide the required surplus zero separately.
    """
    def root(tau: float) -> float:
        tangent = math.tan(1.0 / (2.0 * tau * rate))
        return (1.0 - tangent) / (1.0 + tangent)

    pole_low, pole_high, zero = root(TAU_3180), root(TAU_75), root(TAU_318)
    return normalize((1.0, -(zero + extra_zero), zero * extra_zero,
                      -(pole_low + pole_high), pole_low * pole_high), rate)


def digital_response(coefs: tuple[float, float, float, float, float],
                     frequency: float, rate: int) -> complex:
    b0, b1, b2, a1, a2 = coefs
    z_inv = cmath.exp(-1j * 2.0 * math.pi * frequency / rate)
    return ((b0 + b1 * z_inv + b2 * z_inv * z_inv) /
            (1.0 + a1 * z_inv + a2 * z_inv * z_inv))


def analog_response(frequency: float) -> complex:
    s = 1j * 2.0 * math.pi * frequency
    return ((1.0 + s * TAU_318) /
            ((1.0 + s * TAU_3180) * (1.0 + s * TAU_75)))


def db(value: complex | float) -> float:
    return 20.0 * math.log10(abs(value))


def analog_db(frequency: float) -> float:
    return db(analog_response(frequency) / analog_response(REFERENCE_HZ))


def digital_db(coefs: tuple[float, float, float, float, float],
               frequency: float, rate: int) -> float:
    return db(digital_response(coefs, frequency, rate))


def q30(coefs: tuple[float, ...]) -> tuple[int, ...]:
    return tuple(round(value * (1 << 30)) for value in coefs)
