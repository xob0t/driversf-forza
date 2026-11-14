// One car's port_batch worker job in Pyodide under Node (research/web-builder.md).
//
//   node tools/web-builder/run-car.mjs --job <native work/<media>.job.json> --cache <dir>
//        --out <dir> [--pyodide <dir holding pyodide.mjs>]
//   node tools/web-builder/run-car.mjs --gui <native build folder> --job <one of its work/*.job.json>
//        --cache <dir> --out <dir>      (the garage-picture step of that build, in this process)
//
// The native job is rewritten onto the Emscripten file system: the repo's src and out/db,
// the given cache folder, out/vanilla, assets/ and DSF_GAME_DIR are mounted with NODEFS
// (files are read lazily, by the reads the converter makes, as WORKERFS reads a browser
// File); the car's outputs go to --out. Writes <out>/web-stats.json: wall time, the wasm
// heap at the end (it never shrinks, so that is its peak), Node's peak RSS and the bytes
// read per file.
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "..", "..");
const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1]]] : acc), []),
);
const pyodideDir = path.resolve(args.pyodide ?? path.join(repo, "out", "web-builder", "node_modules", "pyodide"));
const out = path.resolve(args.out);
fs.mkdirSync(out, { recursive: true });
const real = (p) => fs.realpathSync.native(p);
const gameDir = process.env.DSF_GAME_DIR;
if (!gameDir) throw new Error("DSF_GAME_DIR is not set");

const mounts = [
  ["/repo/src", path.join(repo, "src")],
  ["/repo/tools", path.join(repo, "tools")],
  ["/repo/research", path.join(repo, "research")],
  // Only for forza_audio.vgmstream_path's existence check: the .exe can't run here, and
  // the decoded WAVs come from the cache (research/web-builder.md, "Engine sound").
  ["/repo/out/tools-dl", real(path.join(repo, "out", "tools-dl"))],
  ["/repo/out/db", path.join(repo, "out", "db")],
  ["/repo/out/lua-src", path.join(repo, "out", "lua-src")], // host_select's vehiclestats.lua (dsff game_data prepare)
  ["/repo/out/cache", path.resolve(args.cache)],
  ["/repo/out/vanilla", real(path.join(repo, "out", "vanilla"))],
  ["/assets", real(path.join(repo, "assets"))],
  ["/game", real(gameDir)],
  ["/work", out],
  ...(args.gui ? [["/build", path.resolve(args.gui)]] : []),
];

// Host paths in the native job -> guest paths (the job's cache and worktree paths are the
// native build's, so map them by their tail too).
const job = JSON.parse(fs.readFileSync(args.job, "utf-8"));
const norm = (p) => p.replaceAll("\\", "/").toLowerCase();
const prefixes = [
  [norm(job.cache), "/repo/out/cache"],
  [norm(job.dsf).replace(/\/install\/dngvehicles\.sp$/, ""), "/repo/out/vanilla"],
  [norm(job.game_dir), "/game"],
  ...mounts.filter(([g]) => g === "/assets").map(([g, h]) => [norm(h), g]),
  [norm(path.join(repo, "assets")), "/assets"],
];
const guest = (p) => {
  if (typeof p !== "string" || !p) return p;
  const n = norm(p);
  for (const [host, g] of prefixes) if (n.startsWith(host)) return g + p.replaceAll("\\", "/").slice(host.length);
  return p;
};
const media = job.media;
const guestJob = {
  ...job,
  dsf: guest(job.dsf),
  game_dir: "/game",
  cache: "/repo/out/cache",
  isos: job.isos.map(guest),
  source: { ...job.source, iso: guest(job.source.iso), package: guest(job.source.package) },
  sound_dir: "/work/sound",
  chunk: `/work/work/${media}.chnk`,
  report: `/work/work/${media}.json`,
  fingerprint: false,
};
fs.mkdirSync(path.join(out, "work"), { recursive: true });
if (args.gui) fs.mkdirSync(path.join(out, "gui"), { recursive: true });
fs.writeFileSync(path.join(out, "job.json"), JSON.stringify(guestJob, null, 1));

const started = performance.now();
const { loadPyodide } = await import(pathToFileURL(path.join(pyodideDir, "pyodide.mjs")).href);
const log = fs.createWriteStream(path.join(out, "pyodide.log"));
const py = await loadPyodide({
  indexURL: pyodideDir + path.sep,
  stdout: (s) => log.write(s + "\n"),
  stderr: (s) => log.write(s + "\n"),
});
const bootSeconds = (performance.now() - started) / 1000;
const heapAfterBoot = py._module.HEAPU8.byteLength;
await py.loadPackage(["numpy", "pillow", "threadpoolctl"], { messageCallback: (m) => log.write(m + "\n") });
const packagesSeconds = (performance.now() - started) / 1000 - bootSeconds;
const heapAfterPackages = py._module.HEAPU8.byteLength;

const FS = py.FS;
const NODEFS = FS.filesystems.NODEFS;
for (const [g, h] of mounts) {
  FS.mkdirTree(g);
  FS.mount(NODEFS, { root: h }, g);
}
// Count the bytes read from each mounted file (mmap reads through stream_ops.read too).
const bytesRead = new Map();
const read = NODEFS.stream_ops.read;
NODEFS.stream_ops.read = function (stream, buffer, offset, length, position) {
  const n = read.call(this, stream, buffer, offset, length, position);
  const p = stream.path ?? FS.getPath(stream.node);
  const r = bytesRead.get(p) ?? { reads: 0, bytes: 0 };
  r.reads += 1;
  r.bytes += n;
  bytesRead.set(p, r);
  return n;
};

const convertStarted = performance.now();
let code;
try {
  code = py.runPython(`
import sys
sys.path.insert(0, "/repo/tools/web-builder")
import web_worker
${args.gui ? `web_worker.gui("/build", "/work/gui", ${JSON.stringify(guestJob.isos)})` : 'web_worker.main("/work/job.json")'}
`);
} catch (error) {
  log.write(String(error) + "\n");
  code = "exception";
}
const convertSeconds = (performance.now() - convertStarted) / 1000;
const files = [...bytesRead.entries()]
  .map(([p, r]) => ({ path: p, size: (() => { try { return FS.stat(p).size; } catch { return null; } })(), ...r }))
  .sort((a, b) => b.bytes - a.bytes);
const stats = {
  media,
  code,
  seconds: { boot: +bootSeconds.toFixed(1), packages: +packagesSeconds.toFixed(1), convert: +convertSeconds.toFixed(1) },
  wasmHeap: { afterBoot: heapAfterBoot, afterPackages: heapAfterPackages, end: py._module.HEAPU8.byteLength },
  nodeMaxRssBytes: process.resourceUsage().maxRSS * 1024,
  bytesRead: files.reduce((s, f) => s + f.bytes, 0),
  files,
};
fs.writeFileSync(path.join(out, "web-stats-js.json"), JSON.stringify(stats, null, 1));
log.end();
console.log(JSON.stringify({ ...stats, files: files.slice(0, 15) }, null, 1));
process.exit(code === 0 ? 0 : 1);
