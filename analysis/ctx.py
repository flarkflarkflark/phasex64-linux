# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
import sys
lines = open("analysis/msnkphsu.asm", encoding="utf-8").read().splitlines()
idx = {l.split(":")[0].strip(): i for i, l in enumerate(lines) if ":" in l and not l.startswith(";")}
for a in sys.argv[2:]:
    i = idx[a]; n = int(sys.argv[1])
    print("-----", a); print("\n".join(lines[i-n:i+3]))
