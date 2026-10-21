#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
# Loop control_sweep.py + enum_verify.py (unchanged) up to 10 times or
# ~15 minutes, stopping at the FIRST failed capture. On failure: capture
# diagnostics immediately, do NOT attempt any recovery, do NOT continue
# the loop.
set -u
cd "$(dirname "$0")"

MAX_ITER=10
MAX_SECONDS=900
START=$(date +%s)
DIAG_DIR="data/failure_diag"
mkdir -p "$DIAG_DIR"

for i in $(seq 1 $MAX_ITER); do
	NOW=$(date +%s)
	ELAPSED=$((NOW - START))
	if [ $ELAPSED -ge $MAX_SECONDS ]; then
		echo "=== time budget (${MAX_SECONDS}s) exhausted after $((i-1)) iterations, stopping ==="
		exit 0
	fi

	echo "=== iteration $i (elapsed ${ELAPSED}s) ==="
	LOG_BASELINE=$(wc -l < ~/px-live.log)

	echo "--- control_sweep.py ---"
	python3 control_sweep.py > "$DIAG_DIR/sweep_iter${i}.log" 2>&1
	SWEEP_RC=$?

	echo "--- enum_verify.py ---"
	python3 enum_verify.py > "$DIAG_DIR/verify_iter${i}.log" 2>&1
	VERIFY_RC=$?

	if grep -qE "aplay FAILED|arecord FAILED" "$DIAG_DIR/verify_iter${i}.log"; then
		echo "!!! FAILURE DETECTED at iteration $i !!!"
		echo "capturing diagnostics immediately, no recovery attempted"

		# kernel log around the failure, from this iteration's start
		tail -n +$((LOG_BASELINE+1)) ~/px-live.log > "$DIAG_DIR/kernel_log_at_failure.txt"

		# live device state, captured as-is, right now
		amixer -c PhaseX64 contents > "$DIAG_DIR/amixer_contents_at_failure.txt" 2>&1
		amixer -c PhaseX64 cget name='Digital Input Lock' > "$DIAG_DIR/digital_input_lock_at_failure.txt" 2>&1
		cat /proc/asound/card3/pcm0c/sub0/status > "$DIAG_DIR/pcm0c_status_at_failure.txt" 2>&1
		cat /proc/asound/card3/pcm0c/sub0/hw_params > "$DIAG_DIR/pcm0c_hw_params_at_failure.txt" 2>&1
		cat /proc/asound/card3/pcm0p/sub0/status > "$DIAG_DIR/pcm0p_status_at_failure.txt" 2>&1
		cat /proc/asound/card3/pcm0p/sub0/hw_params > "$DIAG_DIR/pcm0p_hw_params_at_failure.txt" 2>&1

		echo "iteration: $i" > "$DIAG_DIR/SUMMARY.txt"
		echo "sweep_rc: $SWEEP_RC" >> "$DIAG_DIR/SUMMARY.txt"
		echo "verify_rc: $VERIFY_RC" >> "$DIAG_DIR/SUMMARY.txt"
		grep -E "aplay FAILED|arecord FAILED" "$DIAG_DIR/verify_iter${i}.log" >> "$DIAG_DIR/SUMMARY.txt"

		echo "=== diagnostics saved to $DIAG_DIR ==="
		exit 1
	fi

	echo "iteration $i: clean"
done

echo "=== completed $MAX_ITER iterations with no failure ==="
exit 0
