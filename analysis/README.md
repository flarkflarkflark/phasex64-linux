# analysis/

Developer and reverse-engineering notes - **not user documentation**. For
installing and using the driver and panel, see `../linux/README.md`.

This directory is the lab notebook behind the Linux driver: how the PHASE X64
USB protocol was worked out from the official Windows driver (2.9.34) and USB
traces, design notes for larger changes, and investigations that were closed
without a fix. Expect dated, sometimes superseded entries; later sections
correct earlier ones rather than rewriting them.

**Vendor material is not distributed.** The Windows driver binaries, their
`.inf` files and extracted strings, and all decompiler/disassembler output
(Ghidra decompilation, the `msnkphsu.sys` disassembly) stay out of this
repository. Only the findings are published. References such as
`msnkphsu.sys.c:6395`, `CPLPhaseX64USB.exe.c` or `FUN_f100e590` point at a
local Ghidra decompilation of the official driver; you'd have to produce your
own from the vendor package to follow them line by line.

- `NOTES.md` - the main notebook: device, endpoints, control requests, the
  control-panel-to-hardware mapping, ALSA control names, and dated findings.
- `loopback-and-monitoring.md`, `peak-controls-design.md`,
  `watchdog-deadlock.md`, `v0.1.11-plan.md`, `v0.1.12-plan.md` - focused design
  and investigation write-ups.
- `usb_descriptors.txt` - the device's USB descriptors.
- `ghidra_scripts/DecompileAll.java` - Ghidra script that writes every
  function of the loaded program to one `.c` file.
- `pe_info.py`, `disasm.py`, `ctx.py`, `find_calls.py` - small PE/disassembly
  helpers. They expect your own copy of the vendor files at the (gitignored)
  paths they open, e.g. `original-driver/msnkphsu.sys` and
  `analysis/msnkphsu.asm`.
- `dump_desc.py` - dumps the USB descriptors of an attached device (pyusb).
