# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
import pefile, sys, re
for f in sys.argv[1:]:
    pe = pefile.PE(f)
    print("=====", f, "machine", hex(pe.FILE_HEADER.Machine), "entry", hex(pe.OPTIONAL_HEADER.AddressOfEntryPoint))
    for s in pe.sections:
        print("  sect", s.Name.rstrip(b"\0").decode(), hex(s.VirtualAddress), hex(s.Misc_VirtualSize))
    mods = {}
    for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        mods[e.dll.decode()] = [i.name.decode() if i.name else str(i.ordinal) for i in e.imports]
    for m, fns in mods.items():
        print(" ", m, len(fns)); print("    ", ", ".join(fns))
    data = open(f, "rb").read()
    strs = set(m.group().decode() for m in re.finditer(rb"[\x20-\x7e]{6,}", data))
    wstrs = set(m.group().decode("utf-16le") for m in re.finditer(rb"(?:[\x20-\x7e]\x00){6,}", data))
    print("  ascii strings:", len(strs), "wide:", len(wstrs))
    open(f + ".strings.txt", "w", encoding="utf-8").write("\n".join(sorted(strs)) + "\n--- WIDE ---\n" + "\n".join(sorted(wstrs)))
