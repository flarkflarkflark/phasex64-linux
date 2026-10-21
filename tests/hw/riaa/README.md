# RIAA analysis tests

`test_response.py` uses only the Python standard library. It prints the final
float/Q30 tables and pole radii, reports digital-minus-analogue response at the
nine review frequencies, and unit-tests regeneration, normalization,
stability, dense-grid response, Q30 quantization and the top-octave improvement:

```sh
python tests/hw/riaa/test_response.py
```

`design_compare.py` requires NumPy and SciPy. It reproduces the objective
comparison of global/multiple-prewarp BLT, matched-z, unconstrained fit and the
selected matched-z/minimax-extra-zero hybrid. The optimizer is analysis-only;
the driver uses the fixed reference table.

```sh
python tests/hw/riaa/design_compare.py
```

`fixed_point.py` requires NumPy and SciPy. It performs the bit-exact integer
variant comparison on the first ten seconds of the left channel, the DC and
silence-tail tests, and a full-file stereo clipping scan. It reads but never
writes the WAV:

```sh
python tests/hw/riaa/fixed_point.py <your-reference-recording.wav>
```

`test_kernel_model.py` mirrors the kernel's Q30/S24.Q8 arithmetic and state
transitions. It checks the literal C coefficient table, exact arithmetic at
all four rates, S24 saturation, bit-identical bypass/deserialization, pair
independence and every required reset path:

```sh
python tests/hw/riaa/test_kernel_model.py
```

No script in this directory opens the audio device, loads a module or creates
an audio/packet-capture artifact.
