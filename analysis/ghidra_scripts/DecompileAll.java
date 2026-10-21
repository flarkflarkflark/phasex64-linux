// SPDX-License-Identifier: GPL-2.0-only
// Copyright (C) 2026 flarkflarkflark
// Decompile every function of the current program into <outdir>/<program>.c
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.listing.*;
import java.io.*;

public class DecompileAll extends GhidraScript {
    @Override
    public void run() throws Exception {
        String outDir = getScriptArgs()[0];
        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);
        File f = new File(outDir, currentProgram.getName() + ".c");
        try (PrintWriter w = new PrintWriter(new FileWriter(f))) {
            for (Function fn : currentProgram.getFunctionManager().getFunctions(true)) {
                DecompileResults r = di.decompileFunction(fn, 120, monitor);
                w.println("// ===== " + fn.getName() + " @ " + fn.getEntryPoint());
                if (r.decompileCompleted()) w.println(r.getDecompiledFunction().getC());
                else w.println("// decompile failed: " + r.getErrorMessage());
            }
        }
        println("wrote " + f);
    }
}
