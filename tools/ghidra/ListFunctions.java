// Lists the functions whose name matches a regular expression (case-insensitive),
// with their entry point and the number of places that call them. Read-only.
// Usage: ListFunctions.java <output-file> <regex> [<regex> ...]
// @category DriverSF

import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;

import java.io.IOException;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

public class ListFunctions extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        // Every pattern is compiled before the output file is opened (and truncated).
        List<Pattern> patterns = new ArrayList<>();
        for (int i = 1; i < args.length; i++) {
            patterns.add(Pattern.compile(args[i], Pattern.CASE_INSENSITIVE));
        }
        try (PrintWriter out = new PrintWriter(args[0], "UTF-8")) {
            for (Pattern pattern : patterns) {
                out.printf("== %s%n", pattern.pattern());
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
            out.flush();
            if (out.checkError()) {
                throw new IOException("writing " + args[0] + " failed");
            }
        }
    }
}
