# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
# Disassemble msnkphsu.sys per function (.pdata), annotate string xrefs and imports.
import pefile, capstone, re, struct, sys, json
f = "original-driver/msnkphsu.sys"
pe = pefile.PE(f); base = pe.OPTIONAL_HEADER.ImageBase
img = pe.get_memory_mapped_image()
imports = {}
for e in pe.DIRECTORY_ENTRY_IMPORT:
    for i in e.imports:
        imports[i.address - base] = (i.name or b"").decode()
def cstr(rva):
    s = img[rva:rva+200]
    if len(s) > 2 and s[1] == 0 and 32 <= s[0] < 127:
        w = s.decode("utf-16le", "ignore").split("\0")[0]
        return "L\"" + w + "\"" if len(w) >= 3 else None
    m = re.match(rb"[\x20-\x7e\t\n\r]{3,}", s)
    return '"' + m.group().decode().replace("\n","\n") + '"' if m else None
funcs = []
for rf in pe.DIRECTORY_ENTRY_EXCEPTION:
    funcs.append((rf.struct.BeginAddress, rf.struct.EndAddress))
funcs = sorted(set(funcs))
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64); md.detail = False
out = open("analysis/msnkphsu.asm", "w", encoding="utf-8")
names = {}
for b, e in funcs:
    out.write(f"\n;================ sub_{b:x}\n")
    for ins in md.disasm(bytes(img[b:e]), b):
        note = ""
        m = re.search(r"\[rip ([+-]) 0x([0-9a-f]+)\]", ins.op_str)
        if m:
            t = ins.address + ins.size + (1 if m.group(1)=="+" else -1)*int(m.group(2),16)
            if t in imports: note = " ; " + imports[t]
            else:
                s = cstr(t)
                note = f" ; {t:#x}" + (" " + s if s else "")
        out.write(f"{ins.address:8x}: {ins.mnemonic} {ins.op_str}{note}\n")
print(len(funcs), "functions")
