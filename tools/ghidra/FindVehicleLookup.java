// Finds code that touches the vehicle archive: FourCC immediates for the
// dngvehicles.sp chunk tags and strings naming vehicle files or tags. Exports
// both hit lists and decompiles every containing function. Read-only.
// @category DriverSF

import java.io.File;
import java.io.PrintWriter;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.scalar.Scalar;
import ghidra.program.model.symbol.Reference;

public class FindVehicleLookup extends GhidraScript {
    private static final String[] TAGS = {
        "VEHC", "SSIC", "SSLP", "VSCT", "VO3D", "UPVH", "SPSR", "SPID", "VSRT", "VSVH",
        "VSCH", "GRBK", "NONR",
    };

    private static final String[] STRING_NEEDLES = {
        "dngvehicle", "vehicles.sp", "vehicle", "vo3", "handling", "sslp", "vsct", "vehc", "upvh",
    };

    private static long fourcc(String tag) {
        return (tag.charAt(0) & 0xff) | (tag.charAt(1) & 0xff) << 8
            | (tag.charAt(2) & 0xff) << 16 | (long)(tag.charAt(3) & 0xff) << 24;
    }

    private Function container(Address address) {
        return currentProgram.getFunctionManager().getFunctionContaining(address);
    }

    private long rva(Address address) {
        return address.subtract(currentProgram.getImageBase());
    }

    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length != 1) {
            throw new IllegalArgumentException("usage: FindVehicleLookup.java <output-dir>");
        }
        File directory = new File(args[0]);
        File decompiledDirectory = new File(directory, "decompiled");
        if (!decompiledDirectory.exists() && !decompiledDirectory.mkdirs()) {
            throw new IllegalStateException("Could not create " + decompiledDirectory);
        }

        Map<Long, String> tagByValue = new LinkedHashMap<>();
        for (String tag : TAGS) {
            tagByValue.put(fourcc(tag), tag);
        }
        // Function entry -> reasons it was selected, ordered by address.
        Map<Address, StringBuilder> functions = new TreeMap<>();

        try (PrintWriter output = new PrintWriter(new File(directory, "fourcc.tsv"), "UTF-8")) {
            output.println("tag\taddress\trva\tfunction_address\tfunction_name\tinstruction");
            InstructionIterator instructions = currentProgram.getListing().getInstructions(true);
            while (instructions.hasNext() && !monitor.isCancelled()) {
                Instruction instruction = instructions.next();
                for (int operand = 0; operand < instruction.getNumOperands(); ++operand) {
                    for (Object object : instruction.getOpObjects(operand)) {
                        if (!(object instanceof Scalar)) {
                            continue;
                        }
                        String tag = tagByValue.get(((Scalar)object).getUnsignedValue());
                        if (tag == null) {
                            continue;
                        }
                        Function function = container(instruction.getAddress());
                        output.printf("%s\t%s\t0x%08X\t%s\t%s\t%s%n", tag, instruction.getAddress(),
                            rva(instruction.getAddress()),
                            function == null ? "" : function.getEntryPoint(),
                            function == null ? "" : function.getName(), instruction);
                        if (function != null) {
                            functions.computeIfAbsent(function.getEntryPoint(),
                                key -> new StringBuilder()).append(" fourcc:").append(tag);
                        }
                    }
                }
            }
        }

        try (PrintWriter output = new PrintWriter(new File(directory, "strings.tsv"), "UTF-8")) {
            output.println("string_address\tvalue\tfrom\tfunction_address\tfunction_name");
            DataIterator data = currentProgram.getListing().getDefinedData(true);
            while (data.hasNext() && !monitor.isCancelled()) {
                Data item = data.next();
                if (!item.hasStringValue()) {
                    continue;
                }
                String value = String.valueOf(item.getValue());
                String lower = value.toLowerCase(Locale.ROOT);
                boolean wanted = false;
                for (String needle : STRING_NEEDLES) {
                    wanted |= lower.contains(needle);
                }
                if (!wanted) {
                    continue;
                }
                String printable = value.replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r");
                Reference[] references = getReferencesTo(item.getAddress());
                if (references.length == 0) {
                    output.printf("%s\t%s\t\t\t%n", item.getAddress(), printable);
                }
                for (Reference reference : references) {
                    Function function = container(reference.getFromAddress());
                    output.printf("%s\t%s\t%s\t%s\t%s%n", item.getAddress(), printable,
                        reference.getFromAddress(),
                        function == null ? "" : function.getEntryPoint(),
                        function == null ? "" : function.getName());
                    if (function != null) {
                        String label = printable.length() > 40 ? printable.substring(0, 40) : printable;
                        functions.computeIfAbsent(function.getEntryPoint(),
                            key -> new StringBuilder()).append(" string:\"").append(label).append('"');
                    }
                }
            }
        }

        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(currentProgram);
        try (PrintWriter index = new PrintWriter(new File(directory, "functions.tsv"), "UTF-8")) {
            index.println("function_address\trva\tfunction_name\treasons");
            for (Map.Entry<Address, StringBuilder> entry : functions.entrySet()) {
                if (monitor.isCancelled()) {
                    break;
                }
                Function function = getFunctionAt(entry.getKey());
                index.printf("%s\t0x%08X\t%s\t%s%n", entry.getKey(), rva(entry.getKey()),
                    function.getName(), entry.getValue().toString().trim());
                DecompileResults result = decompiler.decompileFunction(function, 60, monitor);
                if (!result.decompileCompleted() || result.getDecompiledFunction() == null) {
                    printerr("Could not decompile " + entry.getKey() + ": " + result.getErrorMessage());
                    continue;
                }
                File file = new File(decompiledDirectory, String.format("%08X_%s.c",
                    entry.getKey().getOffset(), function.getName()));
                try (PrintWriter output = new PrintWriter(file, "UTF-8")) {
                    output.printf("/* VA %s; RVA 0x%08X; selected for:%s */%n%n", entry.getKey(),
                        rva(entry.getKey()), entry.getValue());
                    output.print(result.getDecompiledFunction().getC());
                }
            }
        }
        finally {
            decompiler.dispose();
        }
        println("Functions exported: " + functions.size());
    }
}
