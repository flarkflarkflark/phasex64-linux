#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
# Stress test for the v0.1.7 usb_clear_halt()/watchdog-escalation fix:
# repeatedly kill an arecord mid-capture at a random point (SIGTERM, every
# 5th iteration SIGKILL instead) with no clean stop, then verify a normal
# capture right afterward still succeeds. Checks whether a bulk IN pipe
# killed mid-transfer can wedge subsequent captures - see
# tests/hw/REPORT.md's live capture-stall reproduction, where the three
# captures immediately preceding the stall were killed exactly this way.
#
# Stops at the FIRST failed normal capture and prints the iteration
# number. Does not attempt any recovery. Counts "capture watchdog" and
# "clear_halt" lines added to ~/px-live.log during the run, as a rough
# signal of whether the fix's escalation path is firing at all.
set -u
cd "$(dirname "$0")"

DEV="hw:PhaseX64"
MAX_ITER=50
RATE=48000
DATA="data"
DIAG_DIR="$DATA/kill_stress_failure"
KILL_WAV="/tmp/kill_stress_kill.wav"
NORMAL_WAV="/tmp/kill_stress_normal.wav"
PXLOG="$HOME/px-live.log"

mkdir -p "$DATA"

baseline_lines=0
if [ -f "$PXLOG" ]; then
	baseline_lines=$(wc -l < "$PXLOG")
fi

for i in $(seq 1 "$MAX_ITER"); do
	kill_delay_ms=$(( (RANDOM % 851) + 50 ))  # 50..900 ms inclusive
	kill_delay_s=$(printf '0.%03d' "$kill_delay_ms")

	if (( i % 5 == 0 )); then
		sig="SIGKILL"
	else
		sig="SIGTERM"
	fi

	arecord -D "$DEV" -f S32_LE -c 6 -r "$RATE" "$KILL_WAV" >/dev/null 2>&1 &
	apid=$!
	sleep "$kill_delay_s"
	kill -s "$sig" "$apid" 2>/dev/null
	wait "$apid" 2>/dev/null

	if arecord -D "$DEV" -f S32_LE -c 6 -r "$RATE" -d 1 "$NORMAL_WAV" \
			>"$DATA/kill_stress_normal_iter${i}.log" 2>&1; then
		echo "iteration $i: killed via $sig after ${kill_delay_ms}ms, normal capture OK"
	else
		echo "!!! FAILURE at iteration $i: normal capture failed after $sig kill (delay ${kill_delay_ms}ms) !!!"
		mkdir -p "$DIAG_DIR"
		echo "iteration: $i" > "$DIAG_DIR/SUMMARY.txt"
		echo "signal: $sig" >> "$DIAG_DIR/SUMMARY.txt"
		echo "kill_delay_ms: $kill_delay_ms" >> "$DIAG_DIR/SUMMARY.txt"
		cp "$DATA/kill_stress_normal_iter${i}.log" "$DIAG_DIR/normal_capture_fail.log"
		tail -n +"$((baseline_lines + 1))" "$PXLOG" > "$DIAG_DIR/kernel_log_since_start.txt" 2>/dev/null
		cat /proc/asound/card3/pcm0c/sub0/status > "$DIAG_DIR/pcm0c_status.txt" 2>&1
		amixer -c PhaseX64 contents > "$DIAG_DIR/amixer_contents.txt" 2>&1
		echo "iteration: $i"
		break
	fi
done

new_lines=""
if [ -f "$PXLOG" ]; then
	new_lines=$(tail -n +"$((baseline_lines + 1))" "$PXLOG")
fi
watchdog_count=$(grep -c "capture watchdog" <<<"$new_lines" || true)
clearhalt_count=$(grep -c "clear_halt" <<<"$new_lines" || true)

echo "=== summary ==="
echo "capture watchdog lines added to $PXLOG: $watchdog_count"
echo "clear_halt lines added to $PXLOG: $clearhalt_count"
