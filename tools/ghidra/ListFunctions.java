// Lists the functions whose name matches a regular expression (case-insensitive),
// with their entry point and the number of places that call them. Read-only.
// Usage: ListFunctions.java <output-file> <regex> [<regex> ...]
// @category DriverSF

import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;

import java.io.PrintWriter;
import java.util.regex.Pattern;

public class ListFunctions extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        try (PrintWriter out = new PrintWriter(args[0], "UTF-8")) {
            for (int i = 1; i < args.length; i++) {
                Pattern pattern = Pattern.compile(args[i], Pattern.CASE_INSENSITIVE);
                out.printf("== %s%n", args[i]);
                for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
                    if (!pattern.matcher(f.getName(true)).find()) {
                        continue;
                    }
                    int callers = 0;
                    for (Reference r : getReferencesTo(f.getEntryPoint())) {
                        if (r.getReferenceType().isCall()) {
                            callers++;
                        }
                    }
                    out.printf("%s\t%s\t%d callers%n", f.getEntryPoint(), f.getName(true), callers);
                }
            }
        }
    }
}
