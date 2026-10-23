// Decompile every .luac under a directory with unluac, in one JVM.
//
//   java -cp _ref/downloads/unluac/unluac.jar tools/unluac/BatchDecompile.java <in dir> <out dir>
//
// Writes <out dir>/<same path>.lua; on a decompiler error writes the
// disassembly to <path>.lasm instead and lists the file in errors.txt.

import java.io.IOException;
import java.io.PrintWriter;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.Stream;

import unluac.Configuration;
import unluac.Main;

public class BatchDecompile {
  public static void main(String[] args) throws IOException {
    Path in = Path.of(args[0]);
    Path out = Path.of(args[1]);
    List<Path> files;
    try (Stream<Path> walk = Files.walk(in)) {
      files = walk.filter(p -> p.toString().endsWith(".luac")).sorted().collect(Collectors.toList());
    }
    int ok = 0, failed = 0;
    Files.createDirectories(out);
    try (PrintWriter errors = new PrintWriter(Files.newBufferedWriter(out.resolve("errors.txt")))) {
      for (Path file : files) {
        String rel = in.relativize(file).toString();
        Path target = out.resolve(rel.substring(0, rel.length() - 1));
        Files.createDirectories(target.getParent());
        try {
          Main.decompile(file.toString(), target.toString(), new Configuration());
          ok++;
        } catch (Throwable e) {
          failed++;
          errors.println(rel + "\t" + e);
          try {
            Files.deleteIfExists(target);
            Main.disassemble(file.toString(), target.toString().replaceAll("\\.lua$", ".lasm"));
          } catch (Throwable e2) {
            errors.println(rel + "\tdisassembly failed: " + e2);
          }
        }
      }
    }
    System.out.println(ok + " decompiled, " + failed + " failed");
  }
}
