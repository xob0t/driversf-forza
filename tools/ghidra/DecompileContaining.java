// Decompiles the function containing each given address, even where analysis
// left no function: walks back to the nearest 16-byte-aligned address preceded
// by 0xCC/0x90 padding (or a RET), disassembles from there, creates a temporary
// function (discarded in a read-only run) and decompiles it. Read-only.
// Usage: DecompileContaining.java <output-dir> <hex-address> [...]
// @category DriverSF

import java.io.File;
import java.io.PrintWriter;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.Memory;

public class DecompileContaining extends GhidraScript {
    private Address guessStart(Memory memory, Address address) throws Exception {
        Address candidate = address;
        for (int back = 0; back < 0x4000; ++back) {
            candidate = address.subtract(back);
            if ((candidate.getOffset() & 0xf) != 0) {
                continue;
            }
            int previous = memory.getByte(candidate.subtract(1)) & 0xff;
            int current = memory.getByte(candidate) & 0xff;
            if ((previous == 0xcc || previous == 0x90 || previous == 0xc3) && current != 0xcc) {
                return candidate;
            }
        }
        return null;
    }

    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        File directory = new File(args[0]);
        File decompiled = new File(directory, "decompiled");
        decompiled.mkdirs();
        Memory memory = currentProgram.getMemory();
        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(currentProgram);
        try (PrintWriter report = new PrintWriter(new File(directory, "report.txt"), "UTF-8")) {
            for (int i = 1; i < args.length; ++i) {
                Address address = toAddr(args[i]);
                Function function = getFunctionContaining(address);
                if (function == null) {
                    Address start = guessStart(memory, address);
                    if (start == null) {
                        report.printf("%s: no start found%n", address);
                        continue;
                    }
                    if (getInstructionAt(start) == null) {
                        disassemble(start);
                    }
                    function = getFunctionAt(start);
                    if (function == null) {
                        function = createFunction(start, null);
                    }
                    if (function == null || !function.getBody().contains(address)) {
                        report.printf("%s: guessed start %s, function %s does not contain it%n", address, start,
                            function == null ? "null" : function.getBody());
                        if (function == null) {
                            continue;
                        }
                    }
                }
                report.printf("%s in %s %s%n", address, function.getEntryPoint(), function.getName());
                DecompileResults result = decompiler.decompileFunction(function, 90, monitor);
                if (!result.decompileCompleted() || result.getDecompiledFunction() == null) {
                    report.printf("  decompile failed: %s%n", result.getErrorMessage());
                    continue;
                }
                File file = new File(decompiled, String.format("%08X_%s.c",
                    function.getEntryPoint().getOffset(), function.getName()));
                try (PrintWriter c = new PrintWriter(file, "UTF-8")) {
                    c.printf("/* VA %s; contains %s */%n%n", function.getEntryPoint(), address);
                    c.print(result.getDecompiledFunction().getC());
                }
            }
        }
        finally {
            decompiler.dispose();
        }
    }
}
