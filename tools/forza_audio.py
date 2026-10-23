"""Forza (FM4 / FH1, Xbox 360) engine audio: list, extract and decode engine soundbanks.

Where things are (both games, paths case-insensitive):

  media/audio/cars/Engines/Soundbanks/LOD1/<code>_<name>_<Emission>.fsb
        one FSB4 bank per engine recording and emission, shared between cars.
        Emission is EngAmb (engine bay), Exh (exhaust) or Int (intake). <code>
        looks like <cylinders><aspiration><variant>, e.g. 6N0, 2RT2, 12N2.
        There is no LOD0/LOD2 folder and no .fev for these banks.
  media/audio/cars/Engines/EngineTuning.zip    (FM4: enginetuning.zip)
        <MediaName>_ET.xml per car: idle/redline rpm, volumes, DSP curves and,
        per emission and upgrade level L0-L3, the harmonic tuning file to use.
  media/audio/cars/Engines/HarmonicTuning.zip  (FM4: harmonictuning.zip)
        <..>_HT.xml: <Soundbank name="X.fsb"> with one <Loop wavebankindex=
        rpm_min= rpm_sample= rpm_max= volume=> per FSB subsong, grouped as
        EngineAmbient, EngineIntake, ExhaustL, ExhaustR. ExhaustR indices run
        past the bank (index - count): the right channel of stereo subsongs.
  FM4 disc 2 car packs (PIRS under content/) add banks under
        Media/DLCZips/000N_pri_65/Media/audio/cars/Engines/Soundbanks/LOD1/;
        their cars' _ET.xml files are in disc 1's enginetuning.zip.

So car -> engine sound is: <car>_ET.xml -> <x>_HT.xml (per emission) -> bank +
rpm table. The zips use method 21 (LZX), read with ForzaZip.

FSB4 layout (little-endian): "FSB4", u32 count, u32 header_bytes, u32
data_bytes, u32 version, u32 flags, 8-byte hash, 16-byte guid; then per
subsong u16 size, char name[30], u32 samples, u32 bytes, u32 loop_start,
u32 loop_end, u32 mode, i32 rate, u16 vol, i16 pan, u16 pri, u16 channels,
f32 min_dist, f32 max_dist, ... (size - 0x50 extra bytes: XMA seek table).
Sample data follows the headers back to back.

Sources are disc images (packages under content/ are scanned too), single
packages ("image.iso::content/.../4d53091000000001"), or plain .fsb files.

    python tools/forza_audio.py banks   <iso> [pattern]
    python tools/forza_audio.py info    <fsb | iso::path>
    python tools/forza_audio.py car     <MediaName> --iso <iso> [--iso <iso2>] [--out DIR] [--wav DIR]
    python tools/forza_audio.py extract <iso> <bank-pattern> <out-dir>
    python tools/forza_audio.py decode  <fsb> <out-dir> [--vgmstream EXE]
    python tools/forza_audio.py fev     <fev | iso::path>

Decoding uses vgmstream-cli (XMA needs its FFmpeg build); pass --vgmstream or
it is looked for at out/tools-dl/vgmstream/vgmstream-cli.exe.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import struct
import subprocess
import sys
import wave
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable

sys.path.insert(0, str(Path(__file__).parent))
from forzazip import ForzaZip  # noqa: E402
from stfs import Stfs, StfsError  # noqa: E402
from xiso import SECTOR, XIso  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VGMSTREAM = ROOT / "out" / "tools-dl" / "vgmstream" / "vgmstream-cli.exe"
BANK_DIR = "audio/cars/engines/soundbanks/"
EMISSIONS = {"EngineAmbient": "EngAmb", "EngineIntake": "Int", "Exhaust": "Exh"}

# FSOUND_* sample mode bits (FMOD 3 / FSB3-4)
MODE_BITS = {
    0x00000001: "LOOP_OFF", 0x00000002: "LOOP_NORMAL", 0x00000004: "LOOP_BIDI",
    0x00000008: "8BITS", 0x00000010: "16BITS", 0x00000020: "MONO", 0x00000040: "STEREO",
    0x00000200: "MPEG", 0x00001000: "HW3D", 0x00002000: "2D", 0x00080000: "HW2D",
    0x00100000: "3D", 0x00400000: "IMAADPCM", 0x00800000: "VAG", 0x01000000: "XMA",
    0x02000000: "GCADPCM", 0x04000000: "MULTICHANNEL", 0x08000000: "OGG/CELT",
    0x10000000: "MPEG_LAYER3",
}
CODECS = (("XMA", 0x01000000), ("IMAADPCM", 0x00400000), ("VAG", 0x00800000),
          ("GCADPCM", 0x02000000), ("MPEG", 0x10000200), ("OGG/CELT", 0x08000000))
# FMOD_FSB_SOURCE_* bank flags
BANK_FLAGS = {0x01: "FORMAT", 0x02: "BASICHEADERS", 0x04: "ENCRYPTED", 0x08: "BIGENDIANPCM",
              0x10: "NOTINTERLEAVED", 0x20: "MPEG_PADDED", 0x40: "MPEG_PADDED4"}


# --------------------------------------------------------------------------- FSB4

@dataclass
class Subsong:
    index: int
    name: str
    samples: int
    size: int
    loop_start: int
    loop_end: int
    mode: int
    rate: int
    channels: int
    offset: int
    header_size: int

    @property
    def codec(self) -> str:
        for name, bits in CODECS:
            if self.mode & bits:
                return name
        return "PCM8" if self.mode & 0x08 else "PCM16"

    @property
    def seconds(self) -> float:
        return self.samples / self.rate if self.rate else 0.0

    @property
    def loop(self) -> str:
        kind = "normal" if self.mode & 0x02 else "bidi" if self.mode & 0x04 else \
            "off" if self.mode & 0x01 else "unflagged"
        return f"{kind} {self.loop_start}-{self.loop_end}"

    @property
    def mode_names(self) -> str:
        return "|".join(n for b, n in MODE_BITS.items() if self.mode & b) or "0"


@dataclass
class Fsb:
    version: int
    flags: int
    header_bytes: int
    data_bytes: int
    subsongs: list[Subsong] = field(default_factory=list)

    @property
    def flag_names(self) -> str:
        return "|".join(n for b, n in BANK_FLAGS.items() if self.flags & b) or "0"


def parse_fsb(data: bytes) -> Fsb:
    if data[:4] != b"FSB4":
        raise ValueError(f"not an FSB4 bank (magic {data[:4]!r})")
    count, header_bytes, data_bytes, version, flags = struct.unpack_from("<IIIII", data, 4)
    fsb = Fsb(version, flags, header_bytes, data_bytes)
    pos, offset, first = 0x30, 0x30 + header_bytes, None
    for i in range(count):
        if flags & 0x02 and first is not None:          # basic headers: samples + bytes only
            samples, size = struct.unpack_from("<II", data, pos)
            sub = Subsong(i, f"#{i}", samples, size, 0, samples - 1, first.mode, first.rate,
                          first.channels, offset, 8)
            pos += 8
        else:
            hsize = struct.unpack_from("<H", data, pos)[0]
            name = data[pos + 2:pos + 32].split(b"\0")[0].decode("ascii", "replace")
            samples, size, loop_start, loop_end, mode, rate = struct.unpack_from("<IIIIIi", data, pos + 32)
            channels = struct.unpack_from("<H", data, pos + 62)[0]
            sub = Subsong(i, name, samples, size, loop_start, loop_end, mode, rate, channels, offset, hsize)
            pos += hsize
        first = first or sub
        fsb.subsongs.append(sub)
        offset += sub.size
    return fsb


def print_fsb(fsb: Fsb, title: str = "") -> None:
    total = sum(s.size for s in fsb.subsongs)
    print(f"{title}FSB4 version {fsb.version:#x}, flags {fsb.flags:#x} ({fsb.flag_names}), "
          f"{len(fsb.subsongs)} subsongs, headers {fsb.header_bytes} B, data {fsb.data_bytes} B"
          f"{'' if total == fsb.data_bytes else f' (sum of subsongs {total})'}")
    print(f"  {'#':>3} {'name':24} {'codec':8} {'rate':>6} {'ch':>2} {'samples':>8} {'sec':>6} "
          f"{'bytes':>7}  loop / mode")
    for s in fsb.subsongs:
        print(f"  {s.index:>3} {s.name:24} {s.codec:8} {s.rate:>6} {s.channels:>2} {s.samples:>8} "
              f"{s.seconds:6.3f} {s.size:>7}  {s.loop}  {s.mode_names}")


# --------------------------------------------------------------------------- sources

@dataclass
class AudioFile:
    where: str                         # "FM4 disc 1" style label (disc image or package)
    path: str                          # path inside the disc or package
    size: int
    opener: Callable[[], BinaryIO]

    def read(self) -> bytes:
        with self.opener() as stream:
            return stream.read()

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


class Source:
    """Audio files of one disc image (plus its STFS packages) or one package."""

    def __init__(self, spec: str):
        self.spec, self.files, self._images = spec, [], []
        if "::" in spec:
            image_path, inner = spec.split("::", 1)
            image = self._image(image_path)
            entry = image.find(inner)
            self._add_package(image, entry, Path(image_path).name)
        else:
            image = self._image(spec)
            label = Path(spec).name
            for entry in image.files():
                low = entry.path.lower()
                if entry.is_dir:
                    continue
                if "/audio/" in f"/{low}":
                    self.files.append(AudioFile(label, entry.path, entry.size,
                                                lambda e=entry, im=image: im.open(e)))
                elif low.startswith("content/") and entry.size >= 0x1000 and \
                        image.read(entry, 0, 4) in (b"CON ", b"LIVE", b"PIRS"):
                    self._add_package(image, entry, label)

    def _image(self, path: str) -> XIso:
        image = XIso(path)
        self._images.append(image)
        return image

    def _add_package(self, image: XIso, entry, label: str) -> None:
        try:
            package = Stfs(image.stream, image.base + entry.sector * SECTOR, entry.size)
        except StfsError as error:
            print(f"warning: {entry.path}: {error}", file=sys.stderr)
            return
        where = f"{label}::{entry.path}"
        for inner in package.files():
            if not inner.is_dir and "/audio/" in f"/{inner.path.lower()}":
                self.files.append(AudioFile(where, inner.path, inner.size,
                                            lambda e=inner, p=package: p.open(e)))

    def find_name(self, name: str) -> list[AudioFile]:
        low = name.lower()
        return [f for f in self.files if f.name.lower() == low]

    def close(self) -> None:
        for image in self._images:
            image.close()


def open_file(spec: str) -> bytes:
    """Bytes of a plain file or of "image.iso::path/in/image"."""
    if "::" not in spec:
        return Path(spec).read_bytes()
    image_path, inner = spec.split("::", 1)
    image = XIso(image_path)
    try:
        return image.read(image.find(inner))
    finally:
        image.close()


def engine_banks(files: list[AudioFile]) -> list[AudioFile]:
    return [f for f in files if BANK_DIR in f.path.lower() and f.path.lower().endswith(".fsb")]


# --------------------------------------------------------------------------- tuning

@dataclass
class Loop:
    group: str             # EngineAmbient / EngineIntake / ExhaustL / ExhaustR
    index: int             # FSB subsong index (wavebankindex)
    rpm_min: int
    rpm_sample: int
    rpm_max: int
    volume: float


@dataclass
class Harmonic:
    file: str
    bank: str
    loops: list[Loop]
    root: ET.Element


@dataclass
class EngineTuning:
    car: str
    file: str
    settings: dict[str, str]
    volumes: dict[str, str]
    upgrades: dict[str, dict[str, str]]     # emission -> {L0: ht file, ...}
    xml: bytes


class Tuning:
    """The EngineTuning and HarmonicTuning zips of the given sources (first one found wins)."""

    def __init__(self, sources: list[Source]):
        self.et: dict[str, tuple[ForzaZip, object]] = {}
        self.ht: dict[str, tuple[ForzaZip, object]] = {}
        self.zips: list[AudioFile] = []
        for source in sources:
            for f in source.files:
                low = f.path.lower()
                if not low.endswith(".zip") or "/engines/" not in low:
                    continue
                kind = {"enginetuning.zip": self.et, "harmonictuning.zip": self.ht}.get(f.name.lower())
                if kind is None:
                    continue
                self.zips.append(f)
                archive = ForzaZip(f.opener())
                for info in archive.entries():
                    kind.setdefault(info.filename.rsplit("/", 1)[-1].lower(), (archive, info))

    def engine(self, car: str) -> EngineTuning:
        key = f"{car}_et.xml".lower()
        if key not in self.et:
            raise KeyError(f"no {car}_ET.xml in any EngineTuning zip")
        archive, info = self.et[key]
        raw = archive.read(info)
        root = ET.fromstring(raw)
        upgrades = {}
        for tag in EMISSIONS:
            node = root.find(f"{tag}/Upgrade")
            if node is not None:
                upgrades[tag] = dict(node.attrib)
        settings = root.find("EngineSettings")
        volumes = root.find("VolumeAdjustments")
        return EngineTuning(car, info.filename, dict(settings.attrib) if settings is not None else {},
                            dict(volumes.attrib) if volumes is not None else {}, upgrades, raw)

    def harmonic(self, name: str) -> tuple[Harmonic, bytes]:
        key = name.lower()
        if key not in self.ht:
            raise KeyError(f"no {name} in any HarmonicTuning zip")
        archive, info = self.ht[key]
        raw = archive.read(info)
        root = ET.fromstring(raw.lstrip())
        loops = []
        for group in root:
            for node in group.findall("Loop"):
                a = node.attrib
                loops.append(Loop(group.tag, int(a["wavebankindex"]), int(a["rpm_min"]),
                                  int(a["rpm_sample"]), int(a["rpm_max"]), float(a.get("volume", 1))))
        return Harmonic(info.filename, root.get("name", ""), loops, root), raw


# --------------------------------------------------------------------------- decoding

def vgmstream_path(given: str | None) -> Path:
    path = Path(given) if given else DEFAULT_VGMSTREAM
    if not path.is_file():
        raise SystemExit(f"vgmstream-cli not found at {path} (pass --vgmstream)")
    return path.resolve()


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def decode_bank(fsb_path: Path, fsb: Fsb, out_dir: Path, vgmstream: Path) -> list[Path]:
    """One WAV per subsong, named <index>_<sample name>.wav. vgmstream runs from the caller's cwd."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for s in fsb.subsongs:
        target = (out_dir / f"{s.index:02d}_{safe(s.name)}.wav").resolve()
        result = subprocess.run([str(vgmstream), "-s", str(s.index + 1), "-o", str(target),
                                 str(fsb_path.resolve())], capture_output=True, text=True)
        if result.returncode or not target.is_file():
            raise RuntimeError(f"vgmstream failed on {fsb_path} #{s.index}: {result.stderr.strip()}")
        written.append(target)
    return written


def wav_stats(path: Path) -> dict[str, float]:
    """Length, peak and loop-seam jump of a 16-bit WAV (seam = |last - first| / median |step|)."""
    with wave.open(str(path), "rb") as w:
        ch, rate, frames = w.getnchannels(), w.getframerate(), w.getnframes()
        raw = w.readframes(frames)
    pcm = struct.unpack(f"<{len(raw) // 2}h", raw)
    first = pcm[0::ch]
    steps = sorted(abs(first[i + 1] - first[i]) for i in range(0, len(first) - 1, 7))
    median = steps[len(steps) // 2] or 1
    return {"frames": frames, "rate": rate, "channels": ch, "peak": max(abs(x) for x in pcm) / 32768,
            "seam": abs(first[-1] - first[0]) / median}


# --------------------------------------------------------------------------- FEV

def fev_strings(data: bytes) -> list[str]:
    """Printable strings of an FMOD Designer .fev (FEV1); event, layer and parameter names."""
    if data[:4] != b"FEV1":
        raise ValueError("not an FEV1 file")
    out = []
    pos = 0
    while pos + 4 <= len(data):              # names are stored as u32 length + bytes (NUL included)
        n = struct.unpack_from("<I", data, pos)[0]
        if 2 <= n <= 256 and pos + 4 + n <= len(data) and data[pos + 3 + n] == 0 and \
                all(32 <= b < 127 for b in data[pos + 4:pos + 3 + n]):
            out.append(data[pos + 4:pos + 3 + n].decode())
            pos += 4 + n
        else:
            pos += 1
    return out


# --------------------------------------------------------------------------- commands

def cmd_banks(args) -> None:
    source = Source(args.source)
    try:
        banks = [f for f in engine_banks(source.files) if fnmatch.fnmatch(f.name.lower(), args.pattern.lower())]
        engines: dict[str, dict[str, AudioFile]] = {}
        for f in banks:
            stem = f.name[:-4]
            m = re.match(r"(.+)_(EngAmb|Exh|Int)(_.*)?$", stem, re.I)
            kind = {"engamb": "EngAmb", "exh": "Exh", "int": "Int"}[m.group(2).lower()] if m else "other"
            key = m.group(1) if m else stem
            engines.setdefault(key.lower(), {})[kind] = f
        for f in sorted(banks, key=lambda f: (f.where, f.path.lower())):
            print(f"{f.size:>9} {f.where}  {f.path}")
        others = [f for f in source.files if f.path.lower().endswith((".zip", ".fev")) and
                  "/cars/" in f.path.lower()]
        print(f"\n{len(banks)} engine banks, {sum(f.size for f in banks) / 1e6:.1f} MB, "
              f"{len(engines)} engine names", file=sys.stderr)
        for kind in ("EngAmb", "Exh", "Int"):
            n = sum(1 for e in engines.values() if kind in e)
            print(f"  {kind}: {n}", file=sys.stderr)
        print("car audio zips / fev:", file=sys.stderr)
        for f in others:
            print(f"  {f.size:>9} {f.path}", file=sys.stderr)
    finally:
        source.close()


def cmd_info(args) -> None:
    print_fsb(parse_fsb(open_file(args.fsb)), f"{args.fsb}: ")


def cmd_fev(args) -> None:
    for s in fev_strings(open_file(args.fev)):
        print(s)


def cmd_extract(args) -> None:
    source = Source(args.source)
    try:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        hits = [f for f in source.files if fnmatch.fnmatch(f.name.lower(), args.pattern.lower())]
        for f in hits:
            (out / f.name).write_bytes(f.read())
            print(f"{f.size:>9} {f.where}  {f.path} -> {out / f.name}")
        if not hits:
            raise SystemExit(f"nothing matches {args.pattern}")
    finally:
        source.close()


def cmd_decode(args) -> None:
    path = Path(args.fsb)
    fsb = parse_fsb(path.read_bytes())
    for wav in decode_bank(path, fsb, Path(args.out), vgmstream_path(args.vgmstream)):
        print(wav)


def cmd_car(args) -> None:
    sources = [Source(spec) for spec in args.iso]
    try:
        tuning = Tuning(sources)
        et = tuning.engine(args.car)
        s = et.settings
        print(f"{args.car}: {et.file}  idle {s.get('audio_rpm_idle')} rpm, redline {s.get('audio_rpm_redline')} rpm, "
              f"num_cylinders {s.get('num_cylinders')}, EngineVolume {et.volumes.get('EngineVolume')}, "
              f"TurboVolume {et.volumes.get('TurboVolume')}, SuperChargerVolume {et.volumes.get('SuperChargerVolume')}")
        out = Path(args.out) if args.out else None
        if out:
            out.mkdir(parents=True, exist_ok=True)
            (out / Path(et.file).name).write_bytes(et.xml)
        vgm = vgmstream_path(args.vgmstream) if args.wav else None
        done: set[str] = set()
        for emission, levels in et.upgrades.items():
            for level, ht_name in levels.items():
                if ht_name in done:
                    print(f"\n[{emission} {level}] {ht_name} (same as above)")
                    continue
                done.add(ht_name)
                try:
                    ht, raw = tuning.harmonic(ht_name)
                except KeyError:   # "NA.xml": no sound for this emission at this upgrade level
                    print(f"\n[{emission} {level}] {ht_name} (no such HT file)")
                    continue
                hits = [f for src in sources for f in src.find_name(ht.bank) if BANK_DIR in f.path.lower()]
                print(f"\n[{emission} {level}] {ht_name} -> {ht.bank}"
                      f"  ({hits[0].where}::{hits[0].path}, {hits[0].size} B)" if hits else
                      f"\n[{emission} {level}] {ht_name} -> {ht.bank}  (BANK NOT FOUND in given sources)")
                if out:
                    (out / Path(ht.file).name).write_bytes(raw)
                if not hits:
                    continue
                data = hits[0].read()
                fsb = parse_fsb(data)
                bank_path = None
                if out:
                    bank_path = out / ht.bank
                    bank_path.write_bytes(data)
                wavs = {}
                if vgm and bank_path:
                    for w in decode_bank(bank_path, fsb, Path(args.wav) / Path(ht.bank).stem, vgm):
                        wavs[int(w.name.split("_", 1)[0])] = w
                by_index = {s.index: s for s in fsb.subsongs}
                print(f"  {'group':13} {'#':>3} {'sample':16} {'rpm_min':>7} {'rpm':>6} {'rpm_max':>7} {'vol':>5} "
                      f"{'codec':5} {'rate':>6} {'ch':>2} {'sec':>6}  loop" + ("   seam  peak" if wavs else ""))
                count = len(fsb.subsongs)
                for lp in ht.loops:
                    sub = by_index.get(lp.index)
                    if sub is None and lp.group == "ExhaustR" and lp.index - count in by_index:
                        # ExhaustR indices run past the bank: index - count is the stereo
                        # subsong whose right channel is the right exhaust
                        right = by_index[lp.index - count]
                        print(f"  {lp.group:13} {lp.index:>3} = right channel of #{right.index} {right.name}"
                              f" ({lp.rpm_min}/{lp.rpm_sample}/{lp.rpm_max}, vol {lp.volume:.2f})")
                        continue
                    if sub is None:
                        print(f"  {lp.group:13} {lp.index:>3} (missing subsong)")
                        continue
                    extra = ""
                    if lp.index in wavs:
                        st = wav_stats(wavs[lp.index])
                        extra = f"  {st['seam']:5.1f} {st['peak']:5.2f}"
                    print(f"  {lp.group:13} {lp.index:>3} {sub.name:16} {lp.rpm_min:>7} {lp.rpm_sample:>6} "
                          f"{lp.rpm_max:>7} {lp.volume:5.2f} {sub.codec:5} {sub.rate:>6} {sub.channels:>2} "
                          f"{sub.seconds:6.3f}  {sub.loop}{extra}")
                unused = sorted(set(by_index) - {lp.index for lp in ht.loops})
                if unused:
                    print(f"  subsongs not in this HT: {[by_index[i].name for i in unused]}")
    finally:
        for source in sources:
            source.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("banks", help="list engine soundbanks of a disc image or package")
    p.add_argument("source")
    p.add_argument("pattern", nargs="?", default="*")
    p = sub.add_parser("info", help="FSB4 header and subsong table")
    p.add_argument("fsb")
    p = sub.add_parser("fev", help="names in an FMOD Designer .fev")
    p.add_argument("fev")
    p = sub.add_parser("extract", help="extract audio files by name pattern")
    p.add_argument("source")
    p.add_argument("pattern")
    p.add_argument("out")
    p = sub.add_parser("decode", help="decode every subsong of an FSB to WAV with vgmstream-cli")
    p.add_argument("fsb")
    p.add_argument("out")
    p.add_argument("--vgmstream")
    p = sub.add_parser("car", help="resolve a car's engine sound: ET -> HT -> banks and rpm tables")
    p.add_argument("car")
    p.add_argument("--iso", action="append", required=True,
                   help="disc image or package; give FM4 disc 1 and disc 2 for pack cars")
    p.add_argument("--out", help="write the ET, HT xml and banks here")
    p.add_argument("--wav", help="decode the banks to <wav>/<bank>/ (needs --out)")
    p.add_argument("--vgmstream")
    args = parser.parse_args(argv)
    {"banks": cmd_banks, "info": cmd_info, "fev": cmd_fev, "extract": cmd_extract,
     "decode": cmd_decode, "car": cmd_car}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
