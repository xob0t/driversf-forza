// One-pass cross-reference scan for the chunk reader (CHNK entry table) work.
// Read-only.
//
//   call:ADDR    every instruction that flows (call/jmp) to ADDR
//   scalar:HEX   every instruction with an operand scalar (immediate or
//                displacement) equal to HEX
//   refs:ADDR    every reference to ADDR (Ghidra references)
//   asm:ADDR     disassembly with bytes of the function containing ADDR
//   dec:ADDR     decompile the function at ADDR
//   deccallers   decompile every function that holds a call: or scalar: hit
//                (up to 200)
//
// All call: and scalar: items are matched in a single pass over the listing.
// Usage: ArchiveExplore.java <output-dir> <item> [...]
// @category DriverSF

import java.io.File;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.scalar.Scalar;
import ghidra.program.model.symbol.Reference;

public class ArchiveExplore extends GhidraScript {
    private final Set<Function> toDecompile = new LinkedHashSet<>();

    private Function functionAt(Address address) throws Exception {
        Function function = getFunctionAt(address);
        if (function == null) {
            function = getFunctionContaining(address);
        }
        if (function == null) {
            if (getInstructionAt(address) == null) {
                disassemble(address);
            }
            function = createFunction(address, null);
        }
        return function;
    }

    private static String fn(Function f) {
        return f == null ? "?" : f.getEntryPoint() + " " + f.getName();
    }

    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        File directory = new File(args[0]);
        new File(directory, "decompiled").mkdirs();
        new File(directory, "asm").mkdirs();

        Map<Long, List<String>> callHits = new LinkedHashMap<>();
        Map<Long, List<String>> scalarHits = new LinkedHashMap<>();
        List<String> other = new ArrayList<>();
        boolean decCallers = false;
        for (int i = 1; i < args.length; ++i) {
            String[] parts = args[i].split(":", 2);
            switch (parts[0]) {
                case "call" -> callHits.put(Long.parseUnsignedLong(parts[1], 16), new ArrayList<>());
                case "scalar" -> scalarHits.put(Long.parseUnsignedLong(parts[1], 16), new ArrayList<>());
                case "deccallers" -> decCallers = true;
                default -> other.add(args[i]);
            }
        }
        Set<Function> hitFunctions = new LinkedHashSet<>();

        try (PrintWriter report = new PrintWriter(new File(directory, "report.txt"), "UTF-8")) {
            if (!callHits.isEmpty() || !scalarHits.isEmpty()) {
                var instructions = currentProgram.getListing().getInstructions(true);
                while (instructions.hasNext() && !monitor.isCancelled()) {
                    Instruction instruction = instructions.next();
                    if (!callHits.isEmpty()) {
                        for (Address flow : instruction.getFlows()) {
                            List<String> list = callHits.get(flow.getOffset());
                            if (list != null) {
                                Function caller = getFunctionContaining(instruction.getAddress());
                                list.add(String.format("  %s %s in %s", instruction.getAddress(), instruction, fn(caller)));
                                if (caller != null) {
                                    hitFunctions.add(caller);
                                }
                            }
                        }
                    }
                    if (!scalarHits.isEmpty()) {
                        Set<Long> seen = new LinkedHashSet<>();
                        for (int op = 0; op < instruction.getNumOperands(); ++op) {
                            for (Object object : instruction.getOpObjects(op)) {
                                if (object instanceof Scalar scalar) {
                                    for (long v : new long[] {scalar.getUnsignedValue(), scalar.getSignedValue()}) {
                                        if (scalarHits.containsKey(v) && seen.add(v)) {
                                            Function user = getFunctionContaining(instruction.getAddress());
                                            scalarHits.get(v).add(String.format("  %s %s in %s",
                                                instruction.getAddress(), instruction, fn(user)));
                                            if (user != null) {
                                                hitFunctions.add(user);
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                for (var e : callHits.entrySet()) {
                    report.printf("calls to %08X (%d):%n", e.getKey(), e.getValue().size());
                    e.getValue().forEach(report::println);
                }
                for (var e : scalarHits.entrySet()) {
                    report.printf("scalar %X (%d):%n", e.getKey(), e.getValue().size());
                    e.getValue().forEach(report::println);
                }
                report.printf("functions with hits (%d):%n", hitFunctions.size());
                for (Function f : hitFunctions) {
                    report.printf("  %s%n", fn(f));
                }
            }

            for (String item : other) {
                String[] parts = item.split(":", 2);
                Address address = toAddr(parts[1]);
                switch (parts[0]) {
                    case "refs" -> {
                        report.printf("refs to %s:%n", address);
                        for (Reference reference : getReferencesTo(address)) {
                            Function user = getFunctionContaining(reference.getFromAddress());
                            Instruction instruction = getInstructionAt(reference.getFromAddress());
                            report.printf("  %s %s %s in %s%n", reference.getFromAddress(), reference.getReferenceType(),
                                instruction == null ? "" : instruction.toString(), fn(user));
                        }
                    }
                    case "dec" -> {
                        Function f = functionAt(address);
                        if (f != null) {
                            toDecompile.add(f);
                        }
                    }
                    case "asm" -> {
                        Function f = functionAt(address);
                        if (f == null) {
                            report.printf("asm %s: no function%n", address);
                            break;
                        }
                        File file = new File(new File(directory, "asm"), String.format("%08X_%s.asm",
                            f.getEntryPoint().getOffset(), f.getName()));
                        try (PrintWriter out = new PrintWriter(file, "UTF-8")) {
                            var it = currentProgram.getListing().getInstructions(f.getBody(), true);
                            while (it.hasNext()) {
                                Instruction ins = it.next();
                                StringBuilder bytes = new StringBuilder();
                                for (byte b : ins.getBytes()) {
                                    bytes.append(String.format("%02X ", b & 0xff));
                                }
                                out.printf("%s  %-30s %s%n", ins.getAddress(), bytes.toString().trim(), ins);
                            }
                        }
                        report.printf("asm %s -> %s%n", f.getEntryPoint(), file.getName());
                    }
                    default -> report.printf("unknown item %s%n", item);
                }
            }
        }

        if (decCallers) {
            int n = 0;
            for (Function f : hitFunctions) {
                if (n++ >= 200) {
                    break;
                }
                toDecompile.add(f);
            }
        }
        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(currentProgram);
        for (Function f : toDecompile) {
            DecompileResults results = decompiler.decompileFunction(f, 120, monitor);
            String code = results.decompileCompleted() ? results.getDecompiledFunction().getC()
                : "// decompile failed: " + results.getErrorMessage();
            File file = new File(new File(directory, "decompiled"), String.format("%08X_%s.c",
                f.getEntryPoint().getOffset(), f.getName()));
            try (PrintWriter out = new PrintWriter(file, "UTF-8")) {
                out.printf("/* VA %s */%n%s", f.getEntryPoint(), code);
            }
        }
        decompiler.dispose();
        println("ArchiveExplore done: " + toDecompile.size() + " functions decompiled");
    }
}
