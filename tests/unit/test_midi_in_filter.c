/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Unit tests for px_midi_in_filter_frame() (the pure MIDI-in frame filter
 * in linux/snd-usb-phasex64/midi_in_filter.h). Plain userspace C, no
 * kernel build involved - the header is deliberately kernel-free:
 *
 *   gcc -Wall -Wextra -std=c99 -o /tmp/test_midi_in_filter \
 *       tests/unit/test_midi_in_filter.c && /tmp/test_midi_in_filter
 *
 * Each case feeds one or more 9-byte-frame data portions (8 bytes each -
 * byte 8, the trailing control byte, is never passed to the filter, see
 * midi_in_filter.h) through px_midi_in_filter_frame() with a single
 * seen_status variable carried across calls, exactly as
 * px_midi_in_complete() does across an URB's packets and across URBs
 * within one MIDI-in open - and compares the concatenated output against
 * what should have been forwarded to snd_rawmidi_receive().
 *
 * These cases are drawn directly from the 2026-10-16 hardware capture in
 * tests/hw/REPORT.md "MIDI loopback diagnosis" (cases 2 and 5 below are
 * the literal Run C bytes) and from the bug that capture exposed: the
 * driver used to reset seen_status in px_midi_in_trigger(), which the
 * capture showed firing far more often than once per open - these tests
 * check the *filter itself* is correct when the caller manages
 * seen_status correctly (reset once, not per frame); they do not
 * exercise px_midi_in_trigger()/px_midi_in_open() directly, since that
 * needs the full rawmidi/URB stack, not just this pure function.
 *
 * Copyright (C) 2026 flarkflarkflark
 */
#include <stdio.h>
#include <string.h>

#include "../../linux/snd-usb-phasex64/midi_in_filter.h"

#define F PX_MIDI_FILLER

static int failures;

static void print_bytes(const unsigned char *b, int n)
{
	int i;

	for (i = 0; i < n; i++)
		printf("%02x ", b[i]);
}

/*
 * frames: nframes * 8 bytes (data portion only, no byte 8). Runs them
 * through the filter in order with one seen_status carried across all
 * of them, concatenates the output, and compares against expect.
 */
static void check(const char *name, int seen_status_in,
		   const unsigned char *frames, int nframes,
		   const unsigned char *expect, int expect_len)
{
	unsigned char got[256];
	int got_len = 0;
	int seen_status = seen_status_in;
	int i;

	for (i = 0; i < nframes; i++) {
		unsigned char out[PX_MIDI_PACKET - 1];
		int n = px_midi_in_filter_frame(frames + i * (PX_MIDI_PACKET - 1),
						 out, &seen_status);

		memcpy(got + got_len, out, n);
		got_len += n;
	}

	if (got_len != expect_len || memcmp(got, expect, expect_len) != 0) {
		failures++;
		printf("FAIL %s\n     got:      ", name);
		print_bytes(got, got_len);
		printf("\n     expected: ");
		print_bytes(expect, expect_len);
		printf("\n");
	} else {
		printf("ok   %s (%d bytes)\n", name, got_len);
	}
}

int main(void)
{
	/* 1. Idle frame, pure junk before any status byte - everything
	 * dropped. This is literally the first two IN completions in Run
	 * B/C before anything was sent.
	 */
	{
		const unsigned char frames[] = { 0x00, 0x00, F, F, F, F, F, F };
		const unsigned char expect[] = { 0 }; /* unused, expect_len is 0 */

		check("idle junk at start, nothing forwarded", 0,
		      frames, 1, expect, 0);
	}

	/* 2. A note on/off split across 3 packets exactly as captured on
	 * hardware (Run C, tests/hw/REPORT.md): 90 3C | 7F 80 3C | 00.
	 * Running status from frame 1 must survive into frames 2 and 3.
	 */
	{
		const unsigned char frames[] = {
			0x90, 0x3c, F,    F, F, F, F, F,
			0x7f, 0x80, 0x3c, F, F, F, F, F,
			0x00, F,    F,    F, F, F, F, F,
		};
		const unsigned char expect[] = { 0x90, 0x3c, 0x7f, 0x80, 0x3c, 0x00 };

		check("note on/off split across 3 packets (Run C)", 0,
		      frames, 3, expect, sizeof(expect));
	}

	/* 3. A SysEx split across 4 packets: F0 7D 01 | 02 03 04 | 05 06 07
	 * | 08 F7. Status (F0) in frame 1 must survive through to frame 4.
	 */
	{
		const unsigned char frames[] = {
			0xf0, 0x7d, 0x01, F, F, F, F, F,
			0x02, 0x03, 0x04, F, F, F, F, F,
			0x05, 0x06, 0x07, F, F, F, F, F,
			0x08, 0xf7, F,    F, F, F, F, F,
		};
		const unsigned char expect[] = {
			0xf0, 0x7d, 0x01, 0x02, 0x03, 0x04,
			0x05, 0x06, 0x07, 0x08, 0xf7,
		};

		check("sysex split across 4 packets", 0,
		      frames, 4, expect, sizeof(expect));
	}

	/* 4. Running status across a frame boundary: a full Note On, then
	 * a second Note On in the next frame with the status byte omitted
	 * (bare "3E 7F" data pair relying on running status).
	 */
	{
		const unsigned char frames[] = {
			0x90, 0x3c, 0x7f, F, F, F, F, F,
			0x3e, 0x7f, F,    F, F, F, F, F,
		};
		const unsigned char expect[] = { 0x90, 0x3c, 0x7f, 0x3e, 0x7f };

		check("running status across a frame boundary", 0,
		      frames, 2, expect, sizeof(expect));
	}

	/* 5. Junk data bytes and a real message in the SAME frame: the
	 * junk must be dropped and the transition to real data at the
	 * status byte must still work within one frame.
	 */
	{
		const unsigned char frames[] = {
			0x00, 0x00, 0x90, 0x3c, 0x7f, F, F, F,
		};
		const unsigned char expect[] = { 0x90, 0x3c, 0x7f };

		check("junk then real message in one frame", 0,
		      frames, 1, expect, sizeof(expect));
	}

	/* 6. seen_status already true from an earlier message (e.g. the
	 * substream was armed, something was already sent, and now a
	 * data-only running-status frame arrives on its own): must be
	 * passed through whole, confirming the filter itself has no
	 * problem with a data-only frame once a real status byte has been
	 * seen - the hardware-level byte loss seen in Run D (tests/hw/
	 * REPORT.md) is not something this pure function can be at fault
	 * for, since those bytes never reached it (not even dmesg's raw
	 * "first=" dump showed them).
	 */
	{
		const unsigned char frames[] = { 0x01, 0x02, F, F, F, F, F, F };
		const unsigned char expect[] = { 0x01, 0x02 };

		check("data-only frame with status already seen", 1,
		      frames, 1, expect, sizeof(expect));
	}

	if (failures) {
		printf("\n%d test(s) FAILED\n", failures);
		return 1;
	}
	printf("\nall tests passed\n");
	return 0;
}
