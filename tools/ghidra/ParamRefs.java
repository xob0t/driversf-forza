// Finds instructions whose memory operand uses a displacement inside a window,
// e.g. the VO3D parameter array of the vehicle specification
// (spec + 0x2C70 + 4 * id, block + 0x2960 + 4 * id). Groups hits by function,
// writes hits.tsv (function, instruction address, displacement) and decompiles
// the functions with the most distinct displacements. Read-only.
//
// Usage: ParamRefs.java <output-dir> <lo-hex> <hi-hex> <min-distinct> <max-decompile>
// @category DriverSF

import java.io.File;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.lang.OperandType;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.scalar.Scalar;

public class ParamRefs extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        File directory = new File(args[0]);
        long lo = Long.parseLong(args[1], 16);
        long hi = Long.parseLong(args[2], 16);
        int minDistinct = Integer.parseInt(args[3]);
        int maxDecompile = Integer.parseInt(args[4]);
        File decompiled = new File(directory, "decompiled");
        decompiled.mkdirs();

        Map<Function, TreeSet<Long>> byFunction = new HashMap<>();
        try (PrintWriter hits = new PrintWriter(new File(directory, "hits.tsv"), "UTF-8")) {
            var instructions = currentProgram.getListing().getInstructions(true);
            while (instructions.hasNext() && !monitor.isCancelled()) {
                Instruction instruction = instructions.next();
                for (int op = 0; op < instruction.getNumOperands(); ++op) {
                    int type = instruction.getOperandType(op);
                    if ((type & OperandType.DYNAMIC) == 0) {
                        continue; // only [reg + disp] style operands
                    }
                    for (Object object : instruction.getOpObjects(op)) {
                        if (!(object instanceof Scalar scalar)) {
                            continue;
                        }
                        long value = scalar.getUnsignedValue();
                        if (value < lo || value >= hi) {
                            continue;
                        }
                        Function function = getFunctionContaining(instruction.getAddress());
                        hits.printf("%s\t%s\t0x%X\t%s%n",
                            function == null ? "?" : function.getEntryPoint(), instruction.getAddress(), value,
                            instruction);
                        if (function != null) {
                            byFunction.computeIfAbsent(function, f -> new TreeSet<>()).add(value);
                        }
                    }
                }
            }
        }

        List<Map.Entry<Function, TreeSet<Long>>> ranked = new ArrayList<>(byFunction.entrySet());
        ranked.sort((a, b) -> b.getValue().size() - a.getValue().size());
        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(currentProgram);
        int done = 0;
        try (PrintWriter summary = new PrintWriter(new File(directory, "functions.tsv"), "UTF-8")) {
            for (var entry : ranked) {
                Function function = entry.getKey();
                StringBuilder values = new StringBuilder();
                for (long value : entry.getValue()) {
                    values.append(String.format("%X ", value));
                }
                summary.printf("%s\t%s\t%d\t%s%n", function.getEntryPoint(), function.getName(),
                    entry.getValue().size(), values.toString().trim());
                if (entry.getValue().size() < minDistinct || done >= maxDecompile || monitor.isCancelled()) {
                    continue;
                }
                DecompileResults result = decompiler.decompileFunction(function, 90, monitor);
                if (!result.decompileCompleted() || result.getDecompiledFunction() == null) {
                    continue;
                }
                try (PrintWriter c = new PrintWriter(new File(decompiled, String.format("%08X_%s.c",
                        function.getEntryPoint().getOffset(), function.getName())), "UTF-8")) {
                    c.print(result.getDecompiledFunction().getC());
                }
                ++done;
            }
        }
        finally {
            decompiler.dispose();
        }
        println("ParamRefs done: " + byFunction.size() + " functions, decompiled " + done);
    }
}
