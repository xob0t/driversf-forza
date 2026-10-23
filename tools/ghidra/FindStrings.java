// Lists defined strings (ASCII and UTF-16) whose value matches any of the given
// case-insensitive regexes, with every code/data reference to each string and the
// containing function. Read-only; decompiles nothing.
// Usage: FindStrings.java <output-dir> <regex> [...]
// Writes <output-dir>/strings.tsv: address, value, ref-from, function.
// @category DriverSF

import java.io.File;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;

public class FindStrings extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 2) {
            throw new IllegalArgumentException("usage: FindStrings.java <output-dir> <regex> [...]");
        }
        File directory = new File(args[0]);
        directory.mkdirs();
        List<Pattern> patterns = new ArrayList<>();
        for (int i = 1; i < args.length; ++i) {
            patterns.add(Pattern.compile(args[i], Pattern.CASE_INSENSITIVE));
        }
        int hits = 0;
        try (PrintWriter out = new PrintWriter(new File(directory, "strings.tsv"), "UTF-8")) {
            out.println("string_address\tvalue\tfrom\tfunction_address\tfunction_name");
            DataIterator data = currentProgram.getListing().getDefinedData(true);
            while (data.hasNext() && !monitor.isCancelled()) {
                Data item = data.next();
                if (!item.hasStringValue()) {
                    continue;
                }
                Object value = item.getValue();
                if (value == null) {
                    continue;
                }
                String text = value.toString();
                boolean match = false;
                for (Pattern pattern : patterns) {
                    if (pattern.matcher(text).find()) {
                        match = true;
                        break;
                    }
                }
                if (!match) {
                    continue;
                }
                ++hits;
                String shown = text.replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r");
                boolean any = false;
                for (Reference reference : getReferencesTo(item.getAddress())) {
                    any = true;
                    Function user = getFunctionContaining(reference.getFromAddress());
                    out.printf("%s\t%s\t%s\t%s\t%s%n", item.getAddress(), shown, reference.getFromAddress(),
                        user == null ? "" : user.getEntryPoint(), user == null ? "" : user.getName());
                }
                if (!any) {
                    out.printf("%s\t%s\t\t\t%n", item.getAddress(), shown);
                }
            }
        }
        println("FindStrings done: " + hits + " strings");
    }
}
