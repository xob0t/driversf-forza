// Decompiles every function whose entry point lies in [start, end) into
// <output-dir>/decompiled/<VA>_<name>.c and lists them in report.txt. Read-only.
// Usage: DecompileRange.java <output-dir> <hex-start> <hex-end> [<hex-start> <hex-end> ...]
// @category DriverSF

import java.io.File;
import java.io.PrintWriter;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class DecompileRange extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        File directory = new File(args[0]);
        File decompiled = new File(directory, "decompiled");
        decompiled.mkdirs();
        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(currentProgram);
        int count = 0;
        try (PrintWriter report = new PrintWriter(new File(directory, "report.txt"), "UTF-8")) {
            for (int i = 1; i + 1 < args.length; i += 2) {
                Address start = toAddr(args[i]);
                Address end = toAddr(args[i + 1]);
                for (Function function : currentProgram.getFunctionManager().getFunctions(start, true)) {
                    if (monitor.isCancelled() || function.getEntryPoint().compareTo(end) >= 0) {
                        break;
                    }
                    DecompileResults result = decompiler.decompileFunction(function, 60, monitor);
                    if (!result.decompileCompleted() || result.getDecompiledFunction() == null) {
                        report.printf("%s %s decompile failed: %s%n", function.getEntryPoint(), function.getName(),
                            result.getErrorMessage());
                        continue;
                    }
                    report.printf("%s %s%n", function.getEntryPoint(), function.getName());
                    File file = new File(decompiled, String.format("%08X_%s.c",
                        function.getEntryPoint().getOffset(), function.getName()));
                    try (PrintWriter c = new PrintWriter(file, "UTF-8")) {
                        c.printf("/* VA %s */%n%n", function.getEntryPoint());
                        c.print(result.getDecompiledFunction().getC());
                    }
                    ++count;
                }
            }
        }
        finally {
            decompiler.dispose();
        }
        println("DecompileRange done: " + count + " functions");
    }
}
