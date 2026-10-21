#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""Print the objective comparison used to select the RIAA biquad.

NumPy/SciPy are analysis-only dependencies.  The selected coefficients are
literal reference data in riaa.py and in the driver.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import minimize_scalar

from riaa import (EXTRA_ZEROS, RATES, TAU_318, TAU_3180, TAU_75, analog_db,
                  bilinear_coefficients,
                  breakpoint_prewarped_coefficients, coefficients, digital_db,
                  matched_z_coefficients)


FREQUENCIES = (20, 50, 100, 500, 1000, 2000, 10000, 15000, 20000)

# Unconstrained four-root minimax fits.  They establish the best attainable
# magnitude error for a general real-root biquad, but move the 20 Hz response
# by 0.24/0.15 dB at 44.1/48 kHz.  That is an unnecessary low-band trade-off.
FITTED = {
    44100: (0.232462250117552, -0.169562735599040, -0.042480162117862,
            -1.713188146945632, 0.715328480073344),
    48000: (0.216502662471163, -0.164595611631351, -0.035130742090804,
            -1.739903041476906, 0.741637739634890),
    88200: (0.126627053379881, -0.107440092540789, -0.014223375222066,
            -1.855735535519373, 0.856237702200577),
    96000: (0.117099798934104, -0.099997024038120, -0.012892600044914,
            -1.866791175542712, 0.867216940400898),
}


FREQUENCY_GRID = np.geomspace(20.0, 20000.0, 200001)
_s = 2j * np.pi * FREQUENCY_GRID
_h = (1.0 + _s * TAU_318) / ((1.0 + _s * TAU_3180) *
                              (1.0 + _s * TAU_75))
_s_ref = 2j * np.pi * 1000.0
_h_ref = ((1.0 + _s_ref * TAU_318) /
          ((1.0 + _s_ref * TAU_3180) * (1.0 + _s_ref * TAU_75)))
ANALOG_GRID_DB = 20.0 * np.log10(np.abs(_h / _h_ref))


def max_error(coefs: tuple[float, ...], rate: int) -> float:
    b0, b1, b2, a1, a2 = coefs
    z = np.exp(-2j * np.pi * FREQUENCY_GRID / rate)
    response = ((b0 + b1 * z + b2 * z * z) /
                (1.0 + a1 * z + a2 * z * z))
    errors = 20.0 * np.log10(np.abs(response)) - ANALOG_GRID_DB
    return float(np.max(np.abs(errors)))


def optimise_extra_zero(factory, rate: int) -> tuple[float, tuple[float, ...]]:
    result = minimize_scalar(
        lambda zero: max_error(factory(rate, zero), rate),
        bounds=(-0.30, -0.05), method="bounded",
        options={"xatol": 1e-14, "maxiter": 500})
    return float(result.x), factory(rate, float(result.x))


def main() -> None:
    print("maximum absolute error, 20 Hz--20 kHz (dB)")
    print("rate  BLT-1k  BLT-best  multi/-1  matched/-1  fitted  selected")
    for rate in RATES:
        best_blt = minimize_scalar(
            lambda prewarp: max_error(bilinear_coefficients(rate, prewarp), rate),
            bounds=(20.0, 20000.0), method="bounded").fun
        methods = (max_error(bilinear_coefficients(rate), rate), best_blt,
                   max_error(breakpoint_prewarped_coefficients(rate), rate),
                   max_error(matched_z_coefficients(rate), rate),
                   max_error(FITTED[rate], rate), max_error(coefficients(rate), rate))
        print(f"{rate:5d} " + " ".join(f"{value:9.4f}" for value in methods))

    print("\nselected response error versus analogue (dB)")
    print("rate " + " ".join(f"{frequency:>9} Hz" for frequency in FREQUENCIES))
    for rate in RATES:
        values = [digital_db(coefficients(rate), f, rate) - analog_db(f)
                  for f in FREQUENCIES]
        print(f"{rate:5d} " + " ".join(f"{value:+10.6f}" for value in values))

    print("\nrecomputed matched-z hybrid surplus zeros")
    for rate in RATES:
        zero, _ = optimise_extra_zero(matched_z_coefficients, rate)
        print(f"{rate}: stored={EXTRA_ZEROS[rate]:+.15f} regenerated={zero:+.15f} "
              f"delta={zero - EXTRA_ZEROS[rate]:+.3e}")


if __name__ == "__main__":
    main()
