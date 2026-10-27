// Dumps data and references. Read-only.
//
//   dw:ADDR:N     N dwords from ADDR (as hex, float, and the string a dword points to, if any)
//   refs:ADDR     every instruction or data reference to ADDR, with its function
//
// Usage: DumpData.java <output-file> <mode:ADDR[:N]> [...]
// @category DriverSF

import java.io.File;
import java.io.PrintWriter;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.symbol.Reference;

public class DumpData extends GhidraScript {
    private String stringAt(Memory mem, long value) {
        try {
            Address a = toAddr(value);
            if (!mem.contains(a)) {
                return null;
            }
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < 80; i++) {
                byte b = mem.getByte(a.add(i));
                if (b == 0) {
                    break;
                }
                if (b < 32 || b > 126) {
                    return null;
                }
                sb.append((char) b);
            }
            return sb.length() >= 3 ? sb.toString() : null;
        }
        catch (Exception e) {
            return null;
        }
    }

    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        Memory mem = currentProgram.getMemory();
        try (PrintWriter out = new PrintWriter(new File(args[0]), "UTF-8")) {
            for (int i = 1; i < args.length; i++) {
                String[] parts = args[i].split(":");
                Address addr = toAddr(parts[1]);
                if (parts[0].equals("dw")) {
                    int n = parts.length > 2 ? Integer.parseInt(parts[2]) : 16;
                    out.printf("dw %s x %d:%n", addr, n);
                    for (int k = 0; k < n; k++) {
                        Address a = addr.add(4 * k);
                        int v = mem.getInt(a);
                        String s = stringAt(mem, v & 0xffffffffL);
                        out.printf("  %s %08x %g%s%n", a, v, Float.intBitsToFloat(v), s != null ? "  \"" + s + "\"" : "");
                    }
                }
                else if (parts[0].equals("refs")) {
                    out.printf("refs to %s:%n", addr);
                    for (Reference r : getReferencesTo(addr)) {
                        Function f = getFunctionContaining(r.getFromAddress());
                        out.printf("  %s %s in %s%n", r.getFromAddress(), r.getReferenceType(),
                            f == null ? "?" : f.getEntryPoint() + " " + f.getName());
                    }
                }
            }
        }
    }
}
