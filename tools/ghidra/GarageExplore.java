// Explores addresses in three ways and decompiles what it finds. Read-only.
//
//   fn:ADDR   decompile the function at ADDR and every function it calls directly
//   vt:ADDR   find vtables holding a pointer to code at ADDR (a stub with no
//             function yet), then decompile every slot of each vtable
//   ptr:ADDR  find 4-byte pointers to ADDR in initialized memory, dump the 16
//             dwords around each, and decompile any of those dwords that point
//             to code (e.g. Lua registration tables: name, function, help)
//   callers:ADDR  decompile the function at ADDR and every function that calls it
//   table:ADDR    treat ADDR as a vtable: decompile each slot until a non-code dword
//   calls:ADDR    scan every instruction for a flow (call/jump) to ADDR; finds
//                 callers Ghidra has no references for, e.g. calls to stubs that
//                 were never made into functions
//
//   dec:ADDR      decompile only the function at ADDR
//   asm:ADDR      also write the function's disassembly with instruction bytes
//                 to asm/<ADDR>.asm (for patch design)
//   refs:ADDR     every reference to ADDR (data or code); decompile the users
//   range:START-END  decompile every function whose entry lies in [START, END)
//   scalar:HEX    every instruction with an operand scalar (immediate or
//                 displacement) equal to HEX; decompile up to 60 users
//   allof:HEX+HEX+...  functions (comma also works outside PowerShell) whose instructions use every one of these
//                 scalars (immediates or displacements); decompile up to 60
//
// Copy of SpoolExplore for the garage/save work (own log file).
// Usage: SpoolExplore.java <output-dir> <mode:ADDR> [...]
// @category DriverSF

import java.io.File;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.scalar.Scalar;

public class GarageExplore extends GhidraScript {
    private static final int VTABLE_MAX_SLOTS = 48;

    private Memory memory;
    private PrintWriter report;
    private final Set<Function> toDecompile = new LinkedHashSet<>();

    private boolean isCode(long value) {
        Address address;
        try {
            address = toAddr(value);
        }
        catch (Exception e) {
            return false;
        }
        MemoryBlock block = memory.getBlock(address);
        return block != null && block.isExecute();
    }

    private Function functionAt(Address address) throws Exception {
        Function function = getFunctionAt(address);
        if (function == null) {
            Instruction instruction = getInstructionAt(address);
            if (instruction == null) {
                disassemble(address);
            }
            // Read-only project: created functions are discarded at the end of the run.
            function = createFunction(address, null);
        }
        return function;
    }

    private List<Address> findPointers(long target) throws Exception {
        List<Address> hits = new ArrayList<>();
        byte[] pattern = {
            (byte)target, (byte)(target >> 8), (byte)(target >> 16), (byte)(target >> 24),
        };
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
                hits.add(hit);
                start = hit.add(1);
            }
        }
        return hits;
    }

    private void exploreFunction(Address address) throws Exception {
        Function function = functionAt(address);
        if (function == null) {
            report.printf("fn %s: no function%n", address);
            return;
        }
        toDecompile.add(function);
        report.printf("fn %s %s calls:%n", function.getEntryPoint(), function.getName());
        for (Function callee : function.getCalledFunctions(monitor)) {
            report.printf("  %s %s%n", callee.getEntryPoint(), callee.getName());
            toDecompile.add(callee);
        }
    }

    private void exploreVtables(Address address) throws Exception {
        report.printf("vt %s:%n", address);
        for (Address hit : findPointers(address.getOffset())) {
            // Walk back to the first slot: stop at the first dword that isn't a code pointer.
            Address first = hit;
            while (isCode(memory.getInt(first.subtract(4)) & 0xffffffffL)) {
                first = first.subtract(4);
            }
            List<Reference> references = new ArrayList<>();
            for (Reference reference : getReferencesTo(first)) {
                references.add(reference);
            }
            report.printf("  vtable %s (slot %d holds %s), referenced from %s%n", first,
                hit.subtract(first) / 4, address, references.isEmpty() ? "nothing" :
                    references.stream().map(r -> r.getFromAddress() + " in " +
                        (getFunctionContaining(r.getFromAddress()) == null ? "?" :
                            getFunctionContaining(r.getFromAddress()).getName()))
                        .reduce((a, b) -> a + ", " + b).get());
            for (int slot = 0; slot < VTABLE_MAX_SLOTS; ++slot) {
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
            // Functions that install the vtable are usually the constructors.
            for (Reference reference : references) {
                Function user = getFunctionContaining(reference.getFromAddress());
                if (user != null) {
                    toDecompile.add(user);
                }
            }
        }
    }

    private void exploreCallers(Address address) throws Exception {
        Function function = functionAt(address);
        if (function == null) {
            report.printf("callers %s: no function%n", address);
            return;
        }
        toDecompile.add(function);
        report.printf("callers of %s %s:%n", function.getEntryPoint(), function.getName());
        for (Reference reference : getReferencesTo(function.getEntryPoint())) {
            Function caller = getFunctionContaining(reference.getFromAddress());
            report.printf("  %s %s in %s%n", reference.getFromAddress(), reference.getReferenceType(),
                caller == null ? "?" : caller.getEntryPoint() + " " + caller.getName());
            if (caller != null) {
                toDecompile.add(caller);
            }
        }
    }

    private void exploreCallSites(Address address) throws Exception {
        report.printf("calls to %s:%n", address);
        var instructions = currentProgram.getListing().getInstructions(true);
        while (instructions.hasNext() && !monitor.isCancelled()) {
            Instruction instruction = instructions.next();
            for (Address flow : instruction.getFlows()) {
                if (!flow.equals(address)) {
                    continue;
                }
                Function caller = getFunctionContaining(instruction.getAddress());
                report.printf("  %s %s in %s%n", instruction.getAddress(), instruction,
                    caller == null ? "?" : caller.getEntryPoint() + " " + caller.getName());
                if (caller != null) {
                    toDecompile.add(caller);
                }
            }
        }
    }

    private void exploreTable(Address address) throws Exception {
        report.printf("table %s:%n", address);
        for (int slot = 0; slot < VTABLE_MAX_SLOTS; ++slot) {
            long value = memory.getInt(address.add(slot * 4L)) & 0xffffffffL;
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

    private void explorePointers(Address address) throws Exception {
        report.printf("ptr %s:%n", address);
        for (Address hit : findPointers(address.getOffset())) {
            report.printf("  at %s:", hit);
            for (int index = -8; index < 8; ++index) {
                Address cell = hit.add(index * 4L);
                long value = memory.getInt(cell) & 0xffffffffL;
                report.printf(" %s%08X", index == 0 ? "*" : "", value);
                if (isCode(value)) {
                    Function function = functionAt(toAddr(value));
                    if (function != null) {
                        toDecompile.add(function);
                    }
                }
            }
            report.println();
        }
    }


    private File directory;

    private void exploreDecompileOnly(Address address) throws Exception {
        Function function = functionAt(address);
        report.printf("dec %s %s%n", address, function == null ? "no function" : function.getName());
        if (function != null) {
            toDecompile.add(function);
        }
    }

    private void exploreAsm(Address address) throws Exception {
        Function function = getFunctionContaining(address);
        if (function == null) {
            function = functionAt(address);
        }
        if (function == null) {
            report.printf("asm %s: no function%n", address);
            return;
        }
        toDecompile.add(function);
        File asmDirectory = new File(directory, "asm");
        asmDirectory.mkdirs();
        File file = new File(asmDirectory, String.format("%08X_%s.asm",
            function.getEntryPoint().getOffset(), function.getName()));
        try (PrintWriter out = new PrintWriter(file, "UTF-8")) {
            var instructions = currentProgram.getListing().getInstructions(function.getBody(), true);
            while (instructions.hasNext()) {
                Instruction instruction = instructions.next();
                StringBuilder bytes = new StringBuilder();
                for (byte b : instruction.getBytes()) {
                    bytes.append(String.format("%02X ", b & 0xff));
                }
                out.printf("%s  %-30s %s%n", instruction.getAddress(), bytes.toString().trim(), instruction);
            }
        }
        report.printf("asm %s -> %s%n", function.getEntryPoint(), file.getName());
    }

    private void exploreRefs(Address address) throws Exception {
        report.printf("refs to %s:%n", address);
        for (Reference reference : getReferencesTo(address)) {
            Function user = getFunctionContaining(reference.getFromAddress());
            Instruction instruction = getInstructionAt(reference.getFromAddress());
            report.printf("  %s %s %s in %s%n", reference.getFromAddress(), reference.getReferenceType(),
                instruction == null ? "" : instruction.toString(),
                user == null ? "?" : user.getEntryPoint() + " " + user.getName());
            if (user != null) {
                toDecompile.add(user);
            }
        }
    }

    private void exploreScalar(long value) throws Exception {
        report.printf("scalar %X:%n", value);
        int added = 0;
        var instructions = currentProgram.getListing().getInstructions(true);
        while (instructions.hasNext() && !monitor.isCancelled()) {
            Instruction instruction = instructions.next();
            boolean hit = false;
            for (int op = 0; op < instruction.getNumOperands() && !hit; ++op) {
                for (Object object : instruction.getOpObjects(op)) {
                    if (object instanceof Scalar scalar && (scalar.getUnsignedValue() == value
                            || scalar.getSignedValue() == value)) {
                        hit = true;
                        break;
                    }
                }
            }
            if (!hit) {
                continue;
            }
            Function user = getFunctionContaining(instruction.getAddress());
            report.printf("  %s %s in %s%n", instruction.getAddress(), instruction,
                user == null ? "?" : user.getEntryPoint() + " " + user.getName());
            if (user != null && added < 60 && toDecompile.add(user)) {
                ++added;
            }
        }
    }

    private void exploreAllOf(String list) throws Exception {
        String[] items = list.split("[,+ ]+");
        long[] values = new long[items.length];
        for (int i = 0; i < items.length; ++i) {
            values[i] = Long.parseUnsignedLong(items[i], 16);
        }
        report.printf("allof %s:%n", list);
        java.util.Map<Function, Long> masks = new java.util.LinkedHashMap<>();
        long full = (1L << values.length) - 1;
        var instructions = currentProgram.getListing().getInstructions(true);
        while (instructions.hasNext() && !monitor.isCancelled()) {
            Instruction instruction = instructions.next();
            long mask = 0;
            for (int op = 0; op < instruction.getNumOperands(); ++op) {
                for (Object object : instruction.getOpObjects(op)) {
                    if (object instanceof Scalar scalar) {
                        for (int i = 0; i < values.length; ++i) {
                            if (scalar.getUnsignedValue() == values[i] || scalar.getSignedValue() == values[i]) {
                                mask |= 1L << i;
                            }
                        }
                    }
                }
            }
            if (mask == 0) {
                continue;
            }
            Function user = getFunctionContaining(instruction.getAddress());
            if (user != null) {
                masks.merge(user, mask, (a, b) -> a | b);
            }
        }
        int added = 0;
        for (var entry : masks.entrySet()) {
            if (entry.getValue() == full) {
                report.printf("  %s %s%n", entry.getKey().getEntryPoint(), entry.getKey().getName());
                if (added < 60 && toDecompile.add(entry.getKey())) {
                    ++added;
                }
            }
        }
    }

    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 2) {
            throw new IllegalArgumentException(
                "usage: SpoolExplore.java <output-dir> <fn|vt|ptr>:<address> [...]");
        }
        memory = currentProgram.getMemory();
        directory = new File(args[0]);
        File decompiledDirectory = new File(directory, "decompiled");
        if (!decompiledDirectory.exists() && !decompiledDirectory.mkdirs()) {
            throw new IllegalStateException("Could not create " + decompiledDirectory);
        }

        try (PrintWriter output = new PrintWriter(new File(directory, "report.txt"), "UTF-8")) {
            report = output;
            for (int index = 1; index < args.length; ++index) {
                String[] parts = args[index].split(":", 2);
                if (parts[0].equals("range")) {
                    String[] bounds = parts[1].split("-");
                    Address start = toAddr(bounds[0]);
                    Address end = toAddr(bounds[1]);
                    int found = 0;
                    for (Function function : currentProgram.getFunctionManager().getFunctions(start, true)) {
                        if (function.getEntryPoint().compareTo(end) >= 0) {
                            break;
                        }
                        toDecompile.add(function);
                        report.printf("range fn %s %s%n", function.getEntryPoint(), function.getName());
                        ++found;
                    }
                    report.printf("range %s: %d functions%n", parts[1], found);
                    continue;
                }
                if (parts[0].equals("allof")) {
                    exploreAllOf(parts[1]);
                    continue;
                }
                if (parts[0].equals("scalar")) {
                    exploreScalar(Long.parseUnsignedLong(parts[1], 16));
                    continue;
                }
                Address address = toAddr(parts[1]);
                switch (parts[0]) {
                    case "fn" -> exploreFunction(address);
                    case "vt" -> exploreVtables(address);
                    case "ptr" -> explorePointers(address);
                    case "callers" -> exploreCallers(address);
                    case "table" -> exploreTable(address);
                    case "calls" -> exploreCallSites(address);
                    case "dec" -> exploreDecompileOnly(address);
                    case "asm" -> exploreAsm(address);
                    case "refs" -> exploreRefs(address);
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
                        report.printf("decompile failed: %s %s%n", function.getEntryPoint(),
                            result.getErrorMessage());
                        continue;
                    }
                    File file = new File(decompiledDirectory, String.format("%08X_%s.c",
                        function.getEntryPoint().getOffset(), function.getName()));
                    try (PrintWriter c = new PrintWriter(file, "UTF-8")) {
                        c.printf("/* VA %s; RVA 0x%08X */%n%n", function.getEntryPoint(),
                            function.getEntryPoint().subtract(currentProgram.getImageBase()));
                        c.print(result.getDecompiledFunction().getC());
                    }
                }
            }
            finally {
                decompiler.dispose();
            }
            report.printf("decompiled %d functions%n", toDecompile.size());
        }
        println("SpoolExplore done: " + toDecompile.size() + " functions");
    }
}
