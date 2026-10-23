// Finds instructions with a given scalar operand (e.g. a FourCC such as 'SBMC'
// = 0x434D4253) and decompiles the functions that contain them. Also decompiles
// single functions and vtables. Read-only.
//
//   scalar:VALUE   every instruction with immediate VALUE (hex); decompile its function
//   list:VALUE     like scalar, but only lists the instructions (no decompiling)
//   fn:ADDR        decompile the function at ADDR (no callees)
//   tree:ADDR      decompile the function at ADDR and its direct callees
//   table:ADDR     treat ADDR as a vtable: decompile each slot until a non-code dword
//   callers:ADDR   decompile every function with a reference to ADDR
//   vt:ADDR        find vtables holding a pointer to ADDR; decompile their slots and
//                  the functions that reference the vtable (constructors)
//
// Usage: FindScalars.java <output-dir> <mode:VALUE> [...]
// Output: <output-dir>/report.txt and <output-dir>/decompiled/<VA>_<name>.c
// @category DriverSF

import java.io.File;
import java.io.PrintWriter;
import java.util.LinkedHashSet;
import java.util.Set;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.scalar.Scalar;
import ghidra.program.model.symbol.Reference;

public class FindScalars extends GhidraScript {
    private PrintWriter report;
    private final Set<Function> toDecompile = new LinkedHashSet<>();

    private boolean isCode(long value) {
        try {
            MemoryBlock block = currentProgram.getMemory().getBlock(toAddr(value));
            return block != null && block.isExecute();
        }
        catch (Exception e) {
            return false;
        }
    }

    private Function functionAt(Address address) throws Exception {
        Function function = getFunctionAt(address);
        if (function == null) {
            if (getInstructionAt(address) == null) {
                disassemble(address);
            }
            function = createFunction(address, null);
        }
        return function;
    }

    private void findScalar(long value, boolean decompile) {
        report.printf("scalar 0x%08X:%n", value);
        var instructions = currentProgram.getListing().getInstructions(true);
        while (instructions.hasNext() && !monitor.isCancelled()) {
            Instruction instruction = instructions.next();
            boolean hit = false;
            for (int op = 0; op < instruction.getNumOperands() && !hit; ++op) {
                for (Object object : instruction.getOpObjects(op)) {
                    if (object instanceof Scalar scalar && (scalar.getUnsignedValue() & 0xffffffffL) == value) {
                        hit = true;
                        break;
                    }
                }
            }
            if (!hit) {
                continue;
            }
            Function function = getFunctionContaining(instruction.getAddress());
            report.printf("  %s %s in %s%n", instruction.getAddress(), instruction,
                function == null ? "?" : function.getEntryPoint() + " " + function.getName());
            if (function != null && decompile) {
                toDecompile.add(function);
            }
        }
    }

    private void exploreVtables(long target) throws Exception {
        report.printf("vt %08X:%n", target);
        byte[] pattern = {(byte)target, (byte)(target >> 8), (byte)(target >> 16), (byte)(target >> 24)};
        var memory = currentProgram.getMemory();
        for (MemoryBlock block : memory.getBlocks()) {
            if (!block.isInitialized() || block.isExecute()) {
                continue;
            }
            Address start = block.getStart();
            while (start != null && start.compareTo(block.getEnd()) <= 0 && !monitor.isCancelled()) {
                Address hit = memory.findBytes(start, block.getEnd(), pattern, null, true, monitor);
                if (hit == null) {
                    break;
                }
                start = hit.add(1);
                Address first = hit;
                while (isCode(memory.getInt(first.subtract(4)) & 0xffffffffL)) {
                    first = first.subtract(4);
                }
                report.printf("  vtable %s (slot %d)%n", first, hit.subtract(first) / 4);
                for (Reference reference : getReferencesTo(first)) {
                    Function user = getFunctionContaining(reference.getFromAddress());
                    report.printf("    referenced from %s in %s%n", reference.getFromAddress(),
                        user == null ? "?" : user.getEntryPoint() + " " + user.getName());
                    if (user != null) {
                        toDecompile.add(user);
                    }
                }
                for (int slot = 0; slot < 64; ++slot) {
                    long value = memory.getInt(first.add(slot * 4L)) & 0xffffffffL;
                    if (!isCode(value)) {
                        break;
                    }
                    Function function = functionAt(toAddr(value));
                    report.printf("    [%2d] +0x%02X %08X %s%n", slot, slot * 4, value,
                        function == null ? "?" : function.getName());
                    if (function != null) {
                        toDecompile.add(function);
                    }
                }
            }
        }
    }

    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 2) {
            throw new IllegalArgumentException("usage: FindScalars.java <output-dir> <mode:VALUE> [...]");
        }
        File directory = new File(args[0]);
        File decompiledDirectory = new File(directory, "decompiled");
        if (!decompiledDirectory.exists() && !decompiledDirectory.mkdirs()) {
            throw new IllegalStateException("Could not create " + decompiledDirectory);
        }
        try (PrintWriter output = new PrintWriter(new File(directory, "report.txt"), "UTF-8")) {
            report = output;
            for (int index = 1; index < args.length; ++index) {
                String[] parts = args[index].split(":", 2);
                long value = Long.parseLong(parts[1].replaceFirst("^0[xX]", ""), 16);
                switch (parts[0]) {
                    case "scalar" -> findScalar(value, true);
                    case "list" -> findScalar(value, false);
                    case "fn" -> {
                        Function function = functionAt(toAddr(value));
                        if (function != null) {
                            toDecompile.add(function);
                        }
                    }
                    case "tree" -> {
                        Function function = functionAt(toAddr(value));
                        if (function != null) {
                            toDecompile.add(function);
                            report.printf("tree %s %s calls:%n", function.getEntryPoint(), function.getName());
                            for (Function callee : function.getCalledFunctions(monitor)) {
                                report.printf("  %s %s%n", callee.getEntryPoint(), callee.getName());
                                toDecompile.add(callee);
                            }
                        }
                    }
                    case "table" -> {
                        report.printf("table %08X:%n", value);
                        for (int slot = 0; slot < 64; ++slot) {
                            long target = currentProgram.getMemory().getInt(toAddr(value + slot * 4L)) & 0xffffffffL;
                            if (!isCode(target)) {
                                break;
                            }
                            Function function = functionAt(toAddr(target));
                            report.printf("    [%2d] +0x%02X %08X %s%n", slot, slot * 4, target,
                                function == null ? "?" : function.getName());
                            if (function != null) {
                                toDecompile.add(function);
                            }
                        }
                    }
                    case "callers" -> {
                        report.printf("callers of %08X:%n", value);
                        for (Reference reference : getReferencesTo(toAddr(value))) {
                            Function caller = getFunctionContaining(reference.getFromAddress());
                            report.printf("  %s %s in %s%n", reference.getFromAddress(), reference.getReferenceType(),
                                caller == null ? "?" : caller.getEntryPoint() + " " + caller.getName());
                            if (caller != null) {
                                toDecompile.add(caller);
                            }
                        }
                    }
                    case "vt" -> exploreVtables(value);
                    default -> throw new IllegalArgumentException("unknown mode " + parts[0]);
                }
            }
            DecompInterface decompiler = new DecompInterface();
            decompiler.openProgram(currentProgram);
            try {
                for (Function function : toDecompile) {
                    if (monitor.isCancelled()) {
                        break;
                    }
                    DecompileResults result = decompiler.decompileFunction(function, 60, monitor);
                    if (!result.decompileCompleted() || result.getDecompiledFunction() == null) {
                        report.printf("decompile failed: %s %s%n", function.getEntryPoint(), result.getErrorMessage());
                        continue;
                    }
                    File file = new File(decompiledDirectory, String.format("%08X_%s.c",
                        function.getEntryPoint().getOffset(), function.getName()));
                    try (PrintWriter c = new PrintWriter(file, "UTF-8")) {
                        c.printf("/* VA %s */%n%n", function.getEntryPoint());
                        c.print(result.getDecompiledFunction().getC());
                    }
                }
            }
            finally {
                decompiler.dispose();
            }
            report.printf("decompiled %d functions%n", toDecompile.size());
        }
        println("FindScalars done: " + toDecompile.size() + " functions");
    }
}
