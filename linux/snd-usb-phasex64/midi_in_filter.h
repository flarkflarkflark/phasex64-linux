/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Pure MIDI-in frame filter for the PHASE X64 USB driver.
 *
 * Deliberately free of any kernel headers so it can be compiled and
 * unit-tested standalone in userspace (see tests/unit/test_midi_in_filter.c)
 * as well as #include'd directly from phasex64.c. Keep it that way: no
 * linux kernel headers, no driver types, nothing beyond plain C and
 * these two defines.
 *
 * Copyright (C) 2026 flarkflarkflark
 */
#ifndef PX_MIDI_IN_FILTER_H
#define PX_MIDI_IN_FILTER_H

#define PX_MIDI_PACKET	9	/* must match MIDI_PACKET in phasex64.c */
#define PX_MIDI_FILLER	0xfd	/* must match MIDI_FILLER in phasex64.c */

/*
 * Filters one 9-byte MIDI-in frame into at most 8 output bytes in `out`,
 * returning the count written:
 *
 * - Bytes 0..7 are data, stripped of 0xFD filler.
 * - Byte 8 is a trailing control byte, observed 0x00 on idle hardware,
 *   and is never looked at or forwarded here - the caller must not pass
 *   it in `pkt[8]` at all (pass only the first 8 bytes of the frame).
 * - Any data byte (bit 7 clear) seen before the first status byte
 *   (bit 7 set) since *seen_status was last cleared to 0 is dropped - a
 *   data byte with no preceding status is meaningless on the wire.
 *
 * *seen_status is the caller's running-status state: 0 or 1, persisted
 * across calls, and must be cleared to 0 exactly once per logical
 * "arm" (on this device: once per MIDI-in open, NOT once per
 * .trigger() call - see tests/hw/REPORT.md "MIDI loopback diagnosis"
 * for why those differ in practice). This function never clears it
 * itself, only ever sets it to 1.
 */
static inline int px_midi_in_filter_frame(const unsigned char *pkt,
					   unsigned char *out,
					   int *seen_status)
{
	int i, n = 0;

	for (i = 0; i < PX_MIDI_PACKET - 1; i++) {
		unsigned char b = pkt[i];

		if (b == PX_MIDI_FILLER)
			continue;
		if (b & 0x80)
			*seen_status = 1;
		else if (!*seen_status)
			continue;
		out[n++] = b;
	}
	return n;
}

#endif /* PX_MIDI_IN_FILTER_H */
