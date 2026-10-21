# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
import re, sys
target = sys.argv[1]
lines = open("analysis/msnkphsu.asm", encoding="utf-8").read().splitlines()
func = None
for i, l in enumerate(lines):
    if l.startswith(";=="): func = l.split()[-1]
    if re.search(r"call 0x%s$" % target, l):
        ctx = []
        for k in range(i-1, max(0, i-45), -1):
            if lines[k].startswith(";==") or "call " in lines[k]: break
            if re.search(r"(mov|lea|xor|movzx) (edx|r8d|r9d|ecx|dword ptr \[rsp \+ 0x(20|28|30|38|40|48|50)\]|byte ptr \[rsp \+ 0x(20|28|30|38|40|48|50)\]|word ptr \[rsp \+ 0x(20|28|30)\]|qword ptr \[rsp \+ 0x(38|40)\]|r9|rdx|r8)\b", lines[k]):
                ctx.append(lines[k].split(":",1)[1].strip())
        print(f"{func} @ {l.split(':')[0].strip()}:  " + " | ".join(reversed(ctx)))
