"""Garage pictures for ported cars: Install/gui.sp images and their names.

vehicleStats[id].Picture1 (e.g. "nissan_370z") names the car's picture in the
garage and car-select screens. Picture2/3 ("_icon_large", "_icon_small") are
read only by the online lobby Lua (onlineGetVehicleLargeImage/SmallImage).
Install/gui.sp (research/gui-assets.md, confirmed-static):

    root   921 x SPOI (spoolable item: MDXN/DRSP {TEXC {UIDC u64, DXTC DDS}, MDID}),
           SSIC, BINF
    SSIC/NONR
      UIDL  u32 count, count x {u32 name hash, u64 texture UID}
      SPOL  u32 count, count x u64 texture UID, in SPOI order
      SSLP  0x1C header (+0x02 u16 count), count x {u16 spool id, u16 root index, u16 kind}

The name hash is the scripts' CRC variant (lua_scripts.CRC_TABLE, reflected
0xEDB88320 with the top byte of each entry flipped, init 0xFFFFFFFF, no final
xor) of the lower-cased name. Car pictures are 512 x 256 DXT5 without mips:
the car in a front three-quarter view on the right, a fading photo strip with
the maker's logo and model name on the left, transparent elsewhere.

`add_pictures` writes a NEW gui.sp: the stock bytes unchanged, new SPOIs
appended after the last entry, and a rebuilt SSIC (UIDL/SPOL/SSLP with the new
names) appended too; the root entry table (which has room for 100 more entries
before the first payload) points at them. Nothing in the stock file moves.

Pictures come from the Forza disc: FH1 media/ui/Textures.zip holds
Thumbnails/Thumbnail_<Data_Car.Id>.xds (768 x 288, 32-bit, one per FH1 car)
and UI4/Logos/Manufacturer_Logos*/L_MAN_<Make>_Logo.xds. The car itself is a
render of the converted model from the stock pictures' camera (car_render.py);
FH1's thumbnails (eye level, mirrored) only with --fh1-thumbnails.

    uv run dsff gui_assets build out/ported.json --out out/gui [--name MEDIA=TEXT] [--wide-glyphs] [--jobs 2]
    uv run dsff gui_assets camera [--cars nissan_370z,...] [--thumbnails out/ported.json]
    uv run dsff gui_assets extract <name> out/pic.png [--gui gui.sp]
    uv run dsff gui_assets glyphtest --ids 1,2,3 [--out out/hud/glyphtest]
    uv run dsff gui_assets strings <regex>
"""

from __future__ import annotations

import argparse
import copy
import io
import itertools
import math
import struct
import sys
from pathlib import Path

import numpy as np

from dsfforza import REPO
from dsfforza.formats import chnk
from dsfforza.formats.vehicle_mesh import child, payload
from dsfforza.game import game_paths


def stock_file(relative: str) -> Path:
    """A stock game file: tools/install.ps1's backup copy when the game folder
    holds an installed (patched) one, else the game's own."""
    game = game_paths.game_dir()  # $DSF_GAME_DIR (src/dsfforza/build/port_batch.py, tools/install.ps1 set it)
    backup = game / "dsfforza-backup" / relative
    return backup if backup.exists() else game / relative


FH1_ISO = REPO / "assets" / "Forza.Horizon.XBOX360.iso"
PICTURE_SIZE = (512, 256)


# --- names ------------------------------------------------------------------------


def name_hash(name: str) -> int:
    from dsfforza.formats.lua_scripts import crc

    return crc(name.lower())


# --- gui.sp ---------------------------------------------------------------------------


def _texc(entry: chnk.Entry) -> chnk.Entry:
    return child(child(child(entry, "MDXN"), "DRSP"), "TEXC")


def read_tables(stream, root) -> dict:
    ssic = next(e for e in root.entries if e.tag == "SSIC")
    nonr = child(ssic, "NONR")
    uidl, spol, sslp = (payload(stream, child(nonr, t)) for t in ("UIDL", "SPOL", "SSLP"))
    n = struct.unpack_from("<I", uidl)[0]
    names = {struct.unpack_from("<I", uidl, 4 + 12 * i)[0]: uidl[8 + 12 * i : 16 + 12 * i] for i in range(n)}
    m = struct.unpack_from("<I", spol)[0]
    order = [spol[4 + 8 * i : 12 + 8 * i] for i in range(m)]
    count = struct.unpack_from("<H", sslp, 2)[0]
    records = [struct.unpack_from("<HHH", sslp, 0x1C + 6 * i) for i in range(count)]
    return {"ssic": ssic, "names": names, "order": order, "records": records, "sslp_header": sslp[:0x1C]}


def find_picture(stream, root, name: str) -> bytes | None:
    """The DDS of a named gui.sp image, or None."""
    t = read_tables(stream, root)
    uid = t["names"].get(name_hash(name))
    if uid is None:
        return None
    index = t["order"].index(uid)
    root_index = next(r[1] for r in t["records"] if r[0] == index)
    drsp = child(child(root.entries[root_index], "MDXN"), "DRSP")
    for texc in (c for c in drsp.children if c.tag == "TEXC"):  # several when pictures share a SPOI
        if payload(stream, child(texc, "UIDC"))[:8] == uid:
            return payload(stream, child(texc, "DXTC"))
    return payload(stream, child(_texc(root.entries[root_index]), "DXTC"))


def append_patch(source: Path, data: bytearray, root, replace: dict, add: list) -> bytearray:
    """Edit a CHNK file without moving any stock byte: the entries in
    `replace` (root index -> Entry with children) and `add` are serialized and
    appended at the end, each at its alignment, and the root entry table is
    patched to point at them (replaced rows retargeted, added rows after the
    last one). When the root table has no room before the first payload, the
    payloads are moved down to make room (gui.sp has room for 100 rows)."""
    with source.open("rb") as stream:
        blobs = []
        for e in list(replace.values()) + list(add):
            buf = io.BytesIO()
            if e.children is not None:
                chnk.write_chunk(buf, stream, e.children, e.chunk_version)
            else:
                buf.write(e.payload)
            blobs.append((e, buf.getvalue()))
    magic, _total, count, version = chnk.HEADER.unpack_from(data, 0)
    table_end = chnk.HEADER.size + chnk.ENTRY.size * (count + len(add))
    first_payload = min(chnk.ENTRY.unpack_from(data, chnk.HEADER.size + chnk.ENTRY.size * i)[1] for i in range(count))
    if table_end > first_payload:
        # Grow the root table: move every root payload (stock bytes unchanged,
        # nested CHNK offsets are relative) down by a multiple of the largest
        # alignment, and shift the existing rows' offsets with them.
        largest = 1 << max(e.align_log2 for e in root.entries)
        delta = -(-(table_end - first_payload) // largest) * largest
        data[first_payload:first_payload] = chnk.FILL * delta
        for i in range(count):
            o = chnk.HEADER.size + chnk.ENTRY.size * i
            struct.pack_into("<I", data, o + 4, chnk.ENTRY.unpack_from(data, o)[1] + delta)
    rows = []
    for e, blob in blobs:
        data += chnk.FILL * (-len(data) % (1 << e.align_log2))
        rows.append((e, len(data), len(blob)))
        data += blob + e.name_raw
    largest = max(e.align_log2 for e in root.entries)
    data += chnk.FILL * (-len(data) % (1 << largest))

    def row(e, offset, size):
        return chnk.ENTRY.pack(
            e.tag.encode("latin-1"), offset, e.version, len(e.name_raw), e.align_log2, e.reserved, size
        )

    slots = list(replace) + [count + k for k in range(len(add))]
    for slot, (e, offset, size) in zip(slots, rows):
        data[chnk.HEADER.size + chnk.ENTRY.size * slot : chnk.HEADER.size + chnk.ENTRY.size * (slot + 1)] = row(
            e, offset, size
        )
    chnk.HEADER.pack_into(data, 0, magic, len(data), count + len(add), version)
    return data


def add_pictures(
    source: Path, out: Path, pictures: dict[str, bytes], uid_seed: int = 0x2E5A00, root_limit: int = chnk.ENTRY_LIMIT
) -> dict:
    """Write a copy of gui.sp with `pictures` (name -> DDS) added. Names that
    already exist get their image replaced in the copy instead (a new SPOI,
    the name pointed at it). One SPOI per picture while the root stays under
    `root_limit` entries (gui.sp: 923, so 177 pictures); beyond that several
    pictures share a SPOI (one TEXC each in its DRSP), each with its own SPOL
    UID and SSLP spool ID pointing at the shared root entry (untested in
    game). Returns a report."""
    with source.open("rb") as stream:
        root = chnk.read_chunk(stream, 0)
        t = read_tables(stream, root)
        template = next(e for e in root.entries if e.tag == "SPOI")
        ssic_index = next(i for i, e in enumerate(root.entries) if e.tag == "SSIC")
        data = bytearray(source.read_bytes())
    used_uids = set(t["order"]) | set(t["names"].values())
    names, order, records = dict(t["names"]), list(t["order"]), list(t["records"])
    new_entries, report = [], {"added": [], "replaced": []}
    room = max(1, root_limit - len(root.entries))
    per_item = max(1, -(-len(pictures) // room))
    items = list(pictures.items())
    for start in range(0, len(items), per_item):
        spoi = copy.deepcopy(template)
        drsp = child(child(spoi, "MDXN"), "DRSP")
        texc_template = _texc(spoi)
        mdid = child(drsp, "MDID")
        texcs, uids = [], []
        for n, (name, dds) in enumerate(items[start : start + per_item], start):
            uid = _new_uid(uid_seed + n, used_uids)
            used_uids.add(uid)
            texc = copy.deepcopy(texc_template)
            child(texc, "UIDC").payload = uid
            child(texc, "DXTC").payload = dds
            texcs.append(texc)
            uids.append(uid)
            h = name_hash(name)
            (report["replaced"] if h in names else report["added"]).append(name)
            names[h] = uid
            records.append((len(order), len(root.entries) + len(new_entries), 0))
            order.append(uid)
        drsp.children = texcs + [mdid]
        mdid.payload = b"".join(uids)
        new_entries.append(spoi)
    report["pictures_per_item"] = per_item

    # The new SSIC: same chunk layout, tables rebuilt.
    ssic = copy.deepcopy(t["ssic"])
    nonr = child(ssic, "NONR")
    child(nonr, "UIDL").payload = struct.pack("<I", len(names)) + b"".join(
        struct.pack("<I", h) + u for h, u in names.items()
    )
    child(nonr, "SPOL").payload = struct.pack("<I", len(order)) + b"".join(order)
    header = bytearray(t["sslp_header"])
    struct.pack_into("<H", header, 2, len(records))
    child(nonr, "SSLP").payload = bytes(header) + b"".join(struct.pack("<HHH", *r) for r in records)

    data = append_patch(source, data, root, {ssic_index: ssic}, new_entries)
    count = len(root.entries)
    out.write_bytes(data)
    report.update({"entries": count + len(new_entries), "size": len(data), "images": len(order)})
    return report


def _new_uid(n: int, used: set) -> bytes:
    """Stock-shaped texture UID (byte 1 = 0x12, byte 7 below 0x80, research/upvh.md)."""
    while True:
        uid = bytes([(n << 4) & 0xFF, 0x12, (n >> 4) & 0xFF, (n >> 12) & 0xFF, 0xF0, 0x5A, 0x2E, 0x2D])
        if uid not in used:
            return uid
        n += 1


# --- Forza images -------------------------------------------------------------------


def untile32(data: bytes) -> tuple[int, int, bytearray]:
    """An Xbox 360 32-bit (k_8_8_8_8, format 6) .xds as RGBA8."""
    from dsfforza.formats import xds

    fmt, width, height = xds.parse_header(data)
    if fmt not in (6, 50):  # k_8_8_8_8 and its _AS_16_16_16_16 alias
        raise ValueError(f"format {fmt}, not 32-bit")
    import numpy as np

    src = data[xds.HEADER_SIZE :]
    aligned_w, aligned_h = (width + 31) & ~31, (height + 31) & ~31
    out = np.zeros((width * height, 4), dtype=np.uint8)
    # Every whole texel of the data, up to the aligned surface, as the texel loop read them.
    count = min(aligned_w * aligned_h, len(src) // 4)
    n = np.arange(count, dtype=np.int64)
    x, y = xds.tiled_x(n, width, 4), xds.tiled_y(n, width, 4)
    inside = (x < width) & (y < height)
    argb = np.frombuffer(src, dtype=np.uint8, count=count * 4).reshape(count, 4)[inside]  # big-endian ARGB
    dst = (y * width + x)[inside]
    _, last = np.unique(dst[::-1], return_index=True)  # where two land on one texel, the later wins
    keep = len(dst) - 1 - last
    out[dst[keep]] = argb[keep][:, [1, 2, 3, 0]]
    return width, height, bytearray(out.tobytes())


def read_xds_rgba(data: bytes) -> tuple[int, int, bytearray]:
    from dsfforza.formats import xds

    fmt, _, _ = xds.parse_header(data)
    if fmt in (6, 50):
        return untile32(data)
    blocks, w, h, fourcc = xds.untile(data)
    return w, h, xds.decode_rgba(blocks, w, h, fourcc)


LOGO_ALIASES = {
    "mercedes-benz": "Mercedes",
    "srt": "VIP",
    "hennessey": "HEN",
    "spada vetture sport": "Spada",
    "citroën": "Citroen",
    "citroen": "Citroen",
}


class ForzaUI:
    """FH1's media/ui/Textures.zip on the disc image."""

    def __init__(self, iso: Path = FH1_ISO):
        from dsfforza.formats.forzazip import ForzaZip
        from dsfforza.formats.xiso import XIso

        self.image = XIso(iso)
        self.zip = ForzaZip(self.image.open(self.image.find("media/ui/Textures.zip")))
        self.names = {i.filename.lower(): i for i in self.zip.entries()}

    def read(self, name: str) -> tuple[int, int, bytearray] | None:
        info = self.names.get(name.lower())
        return read_xds_rgba(self.zip.read(info)) if info else None

    def thumbnail(self, car_id: int):
        return self.read(f"Thumbnails/Thumbnail_{car_id}.xds")

    def logo(self, make: str):
        key = LOGO_ALIASES.get(make.lower(), make).replace(" ", "").replace("-", "")
        for folder in ("Manufacturer_Logos_LG", "Manufacturer_Logos", "Manufacturer_Logos_I"):
            found = self.read(f"UI4/Logos/{folder}/L_MAN_{key}_Logo.xds")
            if found:
                return found
        return None

    def close(self) -> None:
        self.image.close()


def write_png(path: Path, width: int, height: int, rgba) -> None:
    from dsfforza.formats import xds

    xds.write_png(path, width, height, bytearray(rgba))


# --- RGBA image helpers (bytearray, row-major) ----------------------------------------


class Image:
    def __init__(self, width: int, height: int, rgba=None, fill=(0, 0, 0, 0)):
        self.w, self.h = width, height
        self.px = bytearray(rgba) if rgba is not None else bytearray(bytes(fill) * (width * height))

    @classmethod
    def from_array(cls, rgba) -> Image:
        """From an (h, w, 4) uint8 array."""
        h, w = rgba.shape[:2]
        return cls(w, h, np.ascontiguousarray(rgba, dtype=np.uint8).tobytes())

    def array(self):
        """(h, w, 4) uint8 view of the pixels (writes go to the image)."""
        return np.frombuffer(self.px, np.uint8).reshape(self.h, self.w, 4)

    def crop(self, x0: int, y0: int, x1: int, y1: int) -> Image:
        return Image.from_array(self.array()[y0:y1, x0:x1])

    def bbox(self, threshold: int = 8, region=None) -> tuple[int, int, int, int] | None:
        x0, y0, x1, y1 = region or (0, 0, self.w, self.h)
        hit = self.array()[y0:y1, x0:x1, 3] > threshold
        ys, xs = np.flatnonzero(hit.any(axis=1)), np.flatnonzero(hit.any(axis=0))
        if not len(ys):
            return None
        return (x0 + int(xs[0]), y0 + int(ys[0]), x0 + int(xs[-1]) + 1, y0 + int(ys[-1]) + 1)

    def flip_h(self) -> Image:
        return Image.from_array(self.array()[:, ::-1])

    def resize(self, width: int, height: int) -> Image:
        """Area-average resample with alpha weighting (for shrinking): each output
        pixel averages the source area it covers, colours weighted by alpha."""
        wy, wx = _area_weights(self.h, height), _area_weights(self.w, width)
        src = self.array().astype(np.float64)
        a = src[..., 3]
        acc_a = wy @ a @ wx.T
        acc = np.stack([wy @ (src[..., c] * a) @ wx.T for c in range(3)], axis=-1)
        total = wy.sum(axis=1)[:, None] * wx.sum(axis=1)[None]
        out = np.zeros((height, width, 4), np.uint8)
        rgb = np.minimum(255, np.floor(acc / np.where(acc_a > 0, acc_a, 1)[..., None] + 0.5))
        out[..., :3] = np.where((acc_a > 0)[..., None], rgb, 0)
        out[..., 3] = np.minimum(255, np.floor(acc_a / np.maximum(total, 1e-9) + 0.5))
        return Image.from_array(out)

    def fit(self, max_w: int, max_h: int) -> Image:
        scale = min(max_w / self.w, max_h / self.h)
        return self.resize(max(1, round(self.w * scale)), max(1, round(self.h * scale)))

    def paste(self, other: Image, left: int, top: int) -> None:
        """Alpha-composite `other` over this image (both straight alpha)."""
        x0, y0 = max(0, left), max(0, top)
        x1, y1 = min(self.w, left + other.w), min(self.h, top + other.h)
        if x0 >= x1 or y0 >= y1:
            return
        src = other.array()[y0 - top : y1 - top, x0 - left : x1 - left].astype(np.float64)
        dst = self.array()[y0:y1, x0:x1]
        a, b = src[..., 3] / 255, dst[..., 3] / 255
        out_a = a + b * (1 - a)
        rgb = (src[..., :3] * a[..., None] + dst[..., :3] * (b * (1 - a))[..., None]) / np.where(out_a > 0, out_a, 1)[
            ..., None
        ]
        on = a > 0
        dst[..., :3] = np.where(on[..., None], np.floor(rgb + 0.5), dst[..., :3])
        dst[..., 3] = np.where(on, np.floor(out_a * 255 + 0.5), dst[..., 3])


def _area_weights(size: int, out: int):
    """(out, size) matrix: how much of source cell j output cell i covers."""
    edges = np.arange(out + 1) * (size / out)
    lo, hi = edges[:-1, None], edges[1:, None]
    j = np.arange(size)[None]
    return np.clip(np.minimum(hi, j + 1) - np.maximum(lo, j), 0, None)


# --- nameplates: a small stroke font ----------------------------------------------------
# Strokes on a 4 x 6 grid (x right, y down); each glyph is a list of polylines.

STROKES = {
    "A": [[(0, 6), (0, 2), (2, 0), (4, 2), (4, 6)], [(0, 3.5), (4, 3.5)]],
    "B": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)], [(0, 0), (3, 0), (4, 1), (4, 2), (3, 3)]],
    "C": [[(4, 1), (3, 0), (1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5)]],
    "D": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 1), (3, 0), (0, 0)]],
    "E": [[(4, 0), (0, 0), (0, 6), (4, 6)], [(0, 3), (3, 3)]],
    "F": [[(4, 0), (0, 0), (0, 6)], [(0, 3), (3, 3)]],
    "G": [[(4, 1), (3, 0), (1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5), (4, 3), (2, 3)]],
    "H": [[(0, 0), (0, 6)], [(4, 0), (4, 6)], [(0, 3), (4, 3)]],
    "I": [[(1, 0), (3, 0)], [(2, 0), (2, 6)], [(1, 6), (3, 6)]],
    "J": [[(4, 0), (4, 5), (3, 6), (1, 6), (0, 5)]],
    "K": [[(0, 0), (0, 6)], [(4, 0), (0, 3.5)], [(1.3, 2.7), (4, 6)]],
    "L": [[(0, 0), (0, 6), (4, 6)]],
    "M": [[(0, 6), (0, 0), (2, 3), (4, 0), (4, 6)]],
    "N": [[(0, 6), (0, 0), (4, 6), (4, 0)]],
    "O": [[(1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0)]],
    "P": [[(0, 6), (0, 0), (3, 0), (4, 1), (4, 2), (3, 3), (0, 3)]],
    "Q": [[(1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0)], [(2.5, 4.5), (4, 6)]],
    "R": [[(0, 6), (0, 0), (3, 0), (4, 1), (4, 2), (3, 3), (0, 3)], [(2, 3), (4, 6)]],
    "S": [[(4, 1), (3, 0), (1, 0), (0, 1), (0, 2), (1, 3), (3, 3), (4, 4), (4, 5), (3, 6), (1, 6), (0, 5)]],
    "T": [[(0, 0), (4, 0)], [(2, 0), (2, 6)]],
    "U": [[(0, 0), (0, 5), (1, 6), (3, 6), (4, 5), (4, 0)]],
    "V": [[(0, 0), (2, 6), (4, 0)]],
    "W": [[(0, 0), (1, 6), (2, 3), (3, 6), (4, 0)]],
    "X": [[(0, 0), (4, 6)], [(4, 0), (0, 6)]],
    "Y": [[(0, 0), (2, 3), (4, 0)], [(2, 3), (2, 6)]],
    "Z": [[(0, 0), (4, 0), (0, 6), (4, 6)]],
    "0": [[(1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0)], [(4, 1), (0, 5)]],
    "1": [[(1, 1), (2, 0), (2, 6)], [(1, 6), (3, 6)]],
    "2": [[(0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (0, 6), (4, 6)]],
    "3": [[(0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (3, 3), (1, 3)], [(3, 3), (4, 4), (4, 5), (3, 6), (1, 6), (0, 5)]],
    "4": [[(3, 6), (3, 0), (0, 4), (4, 4)]],
    "5": [[(4, 0), (0, 0), (0, 3), (3, 3), (4, 4), (4, 5), (3, 6), (0, 6)]],
    "6": [[(4, 1), (3, 0), (1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)]],
    "7": [[(0, 0), (4, 0), (1.5, 6)]],
    "8": [
        [
            (1, 3),
            (0, 2),
            (0, 1),
            (1, 0),
            (3, 0),
            (4, 1),
            (4, 2),
            (3, 3),
            (1, 3),
            (0, 4),
            (0, 5),
            (1, 6),
            (3, 6),
            (4, 5),
            (4, 4),
            (3, 3),
        ]
    ],
    "9": [[(4, 3), (1, 3), (0, 2), (0, 1), (1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (1, 6), (0, 5)]],
    "-": [[(0.5, 3.2), (3.5, 3.2)]],
    ".": [[(1.8, 5.8), (2.2, 5.8)]],
    "/": [[(0, 6), (4, 0)]],
    "&": [[(4, 6), (1, 2), (1, 1), (2, 0), (3, 1), (3, 2), (0, 4), (0, 5), (1, 6), (2, 6), (4, 4)]],
}


def _segment_distance(px, py, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _plate_coverage(segs, width: int, height: int, ss: int, stroke: float, outline: float, reach: float):
    """[row][column] -> (core, edge): how many of a pixel's ss x ss samples lie within
    `stroke` of the nearest stroke segment, and how many within the outline beyond it.
    The distances come from numpy over the segments near each row; a sample within
    1e-9 of either limit is measured again with _segment_distance over the pixel's
    nearby segments (math.hypot), as the per-sample loop did, so every count is the
    loop's. Segments farther than `reach` from a pixel never decide it either way."""
    import numpy as np

    offsets = np.array([(sx + 0.5) / ss for sx in range(ss)])
    block = 24  # columns measured together, against the segments within reach of them
    out = []
    for py in range(height):
        near = [(a, b) for a, b in segs if min(a[1], b[1]) - reach <= py + 1 and max(a[1], b[1]) + reach >= py]
        row = [(0, 0)] * width
        out.append(row)
        if not near:
            continue
        seg = np.array([(a[0], a[1], b[0], b[1]) for a, b in near], dtype=np.float64)
        lo, hi = np.minimum(seg[:, 0], seg[:, 2]) - reach, np.maximum(seg[:, 0], seg[:, 2]) + reach
        for c0 in range(0, width, block):
            c1 = min(width, c0 + block)
            pick = np.flatnonzero((lo <= c1) & (hi >= c0))
            if not len(pick):
                continue
            ax, ay, bx, by = (seg[pick, k] for k in range(4))
            dx, dy = bx - ax, by - ay
            length = dx * dx + dy * dy
            # samples (column, sy, sx) along the first axis, segments along the second
            n = c1 - c0
            fx = (np.arange(c0, c1, dtype=np.float64)[:, None, None] + offsets[None, None, :]) * np.ones((1, ss, 1))
            fy = np.zeros((n, ss, ss)) + (py + offsets)[None, :, None]
            fx, fy = fx.reshape(-1, 1), fy.reshape(-1, 1)
            with np.errstate(divide="ignore", invalid="ignore"):
                t = np.where(length == 0, 0.0, np.clip(((fx - ax) * dx + (fy - ay) * dy) / length, 0.0, 1.0))
            d = np.hypot(fx - (ax + t * dx), fy - (ay + t * dy)).min(axis=1)
            core = d <= stroke
            edge = ~core & (d <= stroke + outline)
            unsure = np.flatnonzero((np.abs(d - stroke) < 1e-9) | (np.abs(d - (stroke + outline)) < 1e-9))
            for i in unsure.tolist():
                k, rest = divmod(i, ss * ss)
                px = c0 + k
                sy, sx = divmod(rest, ss)
                local = [(a, b) for a, b in near if min(a[0], b[0]) - reach <= px + 1 and max(a[0], b[0]) + reach >= px]
                fxi, fyi = px + (sx + 0.5) / ss, py + (sy + 0.5) / ss
                exact = min(_segment_distance(fxi, fyi, a, b) for a, b in local)
                core[i], edge[i] = exact <= stroke, stroke < exact <= stroke + outline
            counts = zip(
                core.reshape(n, -1).sum(axis=1).tolist(), edge.reshape(n, -1).sum(axis=1).tolist(), strict=True
            )
            row[c0:c1] = list(counts)
    return out


_TEXT_PLATES: dict = {}


def text_plate(text: str, height: int = 40, italic: float = 0.22) -> Image:
    """A chrome nameplate in the style of DSF's vehicles_font model badges:
    bold strokes, light-to-dark vertical gradient, dark outline, transparent
    background. Characters without strokes are skipped. Cached (a few seconds
    of pure Python per name; the garage picture and the HUD plate of a car
    share one render): treat the result as read-only."""
    key = (text.upper(), height, italic)
    if key not in _TEXT_PLATES:
        _TEXT_PLATES[key] = _text_plate(*key)
    return _TEXT_PLATES[key]


def _text_plate(text: str, height: int, italic: float) -> Image:
    cell = height / 9.0  # grid unit in pixels
    stroke = cell * 0.95  # half-width of a stroke
    outline = cell * 0.55
    advance = 5.4 * cell
    widths = [advance if ch != " " else advance * 0.6 for ch in text]
    width = int(sum(widths) + 2 * (stroke + outline) + italic * 6 * cell + 2)
    img = Image(width, height)
    origin_y = 1.5 * cell
    segs = []
    x = stroke + outline + 1
    for ch, w in zip(text, widths):
        for line in STROKES.get(ch, []):
            pts = [(x + gx * cell + italic * (6 - gy) * cell, origin_y + gy * cell) for gx, gy in line]
            segs += list(itertools.pairwise(pts))
        x += w
    ss = 3
    reach = stroke + outline + 1
    counts = _plate_coverage(segs, width, height, ss, stroke, outline, reach)
    for py in range(height):
        for px in range(width):
            core, edge = counts[py][px]
            if not core and not edge:
                continue
            t = (py - origin_y) / (6 * cell)  # 0 top .. 1 bottom of the letters
            # chrome: bright top, a dark band just under the middle, bright again below
            shade = 235 - 150 * math.exp(-(((t - 0.55) / 0.18) ** 2)) - 40 * max(0.0, t)
            c, e = core / (ss * ss), edge / (ss * ss)
            alpha = min(1.0, c + e)
            lum = max(0.0, (shade * c + 20 * e) / max(alpha, 1e-6))
            o = (py * width + px) * 4
            img.px[o : o + 4] = bytes((int(lum), int(lum), int(min(255, lum * 1.03)), int(alpha * 255)))
    return img


PLATE_RENDER_H = 76  # = 2 x the garage picture's plate height (build: nameplate(text, 124, 38))


def nameplate(text: str, max_w: int = 98, max_h: int = 34, min_h: int = 18) -> Image:
    """A model nameplate fitted to max_w x max_h; a long name that would come
    out lower than min_h is set on two lines (split at the middle space), as
    stock plates such as BEETLE / CONVERTIBLE are."""
    render_h = max(2 * max_h, PLATE_RENDER_H)  # one render size for picture and HUD plates (cache)
    one = text_plate(text, render_h).fit(max_w, max_h)
    words = text.split()
    if one.h >= min_h or len(words) < 2:
        return one
    best = min(range(1, len(words)), key=lambda k: abs(len(" ".join(words[:k])) - len(" ".join(words[k:]))))
    lines = [text_plate(" ".join(words[:best]), render_h), text_plate(" ".join(words[best:]), render_h)]
    width = max(l.w for l in lines)
    stacked = Image(width, sum(l.h for l in lines))
    stacked.paste(lines[0], (width - lines[0].w) // 2, 0)
    stacked.paste(lines[1], (width - lines[1].w) // 2, lines[0].h)
    return stacked.fit(max_w, max_h)


# --- the garage picture -----------------------------------------------------------------

PANEL = (0, 0, 471, 198)  # opaque backdrop rectangle of stock pictures (x0, y0, x1, y1)
CAR_BOX = (100, 14, 498, 244)  # where stock pictures put the car
LOGO_BOX = (14, 78, 126, 140)
PLATE_BOX = (8, 144, 132, 182)
CACHE = REPO / "out" / "cache" / "gui"
FOG_FROM, FOG_BLEND = 140, 40  # backdrop columns right of FOG_FROM are synthetic fog
VEHICLE_STATS_LUA = REPO / "out" / "lua-src" / "LuaScripts.copy" / "vehiclestats" / "vehiclestats.lua"


def stock_picture_names() -> list[str]:
    import re

    if not VEHICLE_STATS_LUA.exists():
        return []
    return sorted(set(re.findall(r'Picture1 = "([^"]+)"', VEHICLE_STATS_LUA.read_text(encoding="utf-8"))))


def stock_pictures(gui: Path | None = None) -> list[Image]:
    gui = gui or stock_file("Install/gui.sp")
    from dsfforza.convert import dsf_materials as dm

    out = []
    with gui.open("rb") as stream:
        root = chnk.read_chunk(stream, 0)
        for name in stock_picture_names():
            dds = find_picture(stream, root, name)
            if dds is None:
                continue
            w, h, px = dm.decode_dds(dds)
            if (w, h) == PICTURE_SIZE:
                out.append(Image(w, h, px))
    return out


def stock_backdrop(gui: Path | None = None) -> Image:
    gui = gui or stock_file("Install/gui.sp")
    """The photo strip every stock car picture shares, recovered per pixel as
    the most common colour across the stock pictures (the cars cover
    different parts of it). Pixels no picture shows are filled along their
    row; the logo and name area is filled from the rows above and below.
    Cached in out/cache/gui/backdrop.rgba."""
    cached = CACHE / "backdrop.rgba"
    w, h = PICTURE_SIZE
    if cached.exists():
        data = cached.read_bytes()
        if len(data) == w * h * 4:  # else cut short (written in place before): rebuild
            return Image(w, h, data)
    pics = stock_pictures(gui)
    x0, y0, x1, y1 = PANEL
    img = Image(w, h)
    known = bytearray(w * h)
    need = max(8, len(pics) // 5)
    for y in range(y0, y1):
        for x in range(x0, FOG_FROM):
            o = (y * w + x) * 4
            bins: dict = {}
            for p in pics:
                if p.px[o + 3] < 250:
                    continue
                key = (p.px[o] >> 3, p.px[o + 1] >> 3, p.px[o + 2] >> 3)
                bins.setdefault(key, []).append(p.px[o : o + 3])
            best, best_n = None, 0
            for k in bins:
                n = sum(
                    len(bins.get((k[0] + a, k[1] + b, k[2] + c), ()))
                    for a in (-1, 0, 1)
                    for b in (-1, 0, 1)
                    for c in (-1, 0, 1)
                )
                if n > best_n:
                    best, best_n = k, n
            if best is None or best_n < need:
                continue
            members = [
                m
                for a in (-1, 0, 1)
                for b in (-1, 0, 1)
                for c in (-1, 0, 1)
                for m in bins.get((best[0] + a, best[1] + b, best[2] + c), ())
            ]
            img.px[o : o + 4] = bytes([sum(m[c] for m in members) // len(members) for c in range(3)] + [255])
            known[y * w + x] = 1
    # The logo and name area differs per car: rebuild it from the rows around it.
    lx0, ly0, lx1, ly1 = 0, LOGO_BOX[1] - 10, PLATE_BOX[2] + 6, PLATE_BOX[3] + 8
    for x in range(lx0, lx1):
        top, bottom = (ly0 * w + x) * 4, ((ly1 - 1) * w + x) * 4
        for y in range(ly0, ly1):
            t = (y - ly0) / max(1, ly1 - 1 - ly0)
            o = (y * w + x) * 4
            img.px[o : o + 4] = bytes(
                [int(img.px[top + c] * (1 - t) + img.px[bottom + c] * t) for c in range(3)] + [255]
            )
            known[y * w + x] = 1
    # Right of the photo the cars cover the strip in most pictures and the most
    # common colour there is a dark car part: use the fog gradient of the
    # photo's columns instead, blended in over FOG_BLEND pixels.
    fog_x = FOG_FROM
    for y in range(y0, y1):
        cols = [x for x in range(8, fog_x) if known[y * w + x]]
        if not cols:
            continue
        mean = [sum(img.px[(y * w + x) * 4 + c] for x in cols) // len(cols) for c in range(3)]
        for x in range(fog_x - FOG_BLEND, x1):
            o = (y * w + x) * 4
            t = min(1.0, max(0.0, (x - (fog_x - FOG_BLEND)) / FOG_BLEND))
            if not known[y * w + x]:
                t = 1.0
            img.px[o : o + 4] = bytes([int(img.px[o + c] * (1 - t) + mean[c] * t) for c in range(3)] + [255])
    # Atomic: out/cache is shared by every worktree's builds.
    from dsfforza.build.port_car import write_atomic

    CACHE.mkdir(parents=True, exist_ok=True)
    write_atomic(cached, bytes(img.px))
    return img


def forza_car_image(ui: ForzaUI, car_id: int) -> Image | None:
    """The car from FH1's thumbnail (left two thirds; the right third is a
    top view), cropped and mirrored to face right like DSF's pictures."""
    found = ui.thumbnail(car_id)
    if found is None:
        return None
    img = Image(*found)
    box = img.bbox(12, (0, 0, img.w * 2 // 3, img.h))
    return img.crop(*box).flip_h() if box else None


def logo_image(ui: ForzaUI, make: str) -> Image | None:
    found = ui.logo(make)
    if found is None:
        return None
    img = Image(*found)
    box = img.bbox(12)
    return img.crop(*box) if box else None


def compose_picture(car: Image, logo: Image | None, plate: Image | None, backdrop: Image) -> Image:
    w, h = PICTURE_SIZE
    out = Image(w, h, backdrop.px)
    # The car's own box (render_car: without its floor shadow) fits CAR_BOX's
    # right and bottom edges; the rest of the image (the shadow) goes around it.
    bx0, by0, bx1, by1 = CAR_BOX
    sx0, sy0, sx1, sy1 = getattr(car, "solid", None) or (0, 0, car.w, car.h)
    scale = min((bx1 - bx0) / (sx1 - sx0), (by1 - by0) / (sy1 - sy0))
    fitted = car.resize(max(1, round(car.w * scale)), max(1, round(car.h * scale)))
    out.paste(fitted, bx1 - round(sx1 * scale), by1 - round(sy1 * scale))
    for img, (x0, y0, x1, y1) in ((logo, LOGO_BOX), (plate, PLATE_BOX)):
        if img is not None:
            f = img.fit(x1 - x0, y1 - y0)
            out.paste(f, x0 + (x1 - x0 - f.w) // 2, y0 + (y1 - y0 - f.h) // 2)
    return out


def picture_dds(img: Image, template: bytes | None = None) -> bytes:
    """512 x 256 DXT5 without mips, header like the stock pictures'."""
    from dsfforza.convert import dsf_materials as dm

    dds = bytearray(dm.dds_file(img.px, img.w, img.h, 1, template))
    struct.pack_into("<I", dds, 28, 0)  # stock pictures: mip count 0
    return bytes(dds)


# --- software render of a converted car (car_render.py) ---------------------------------

_ROOTS: dict = {}


def _root_entries(archive: Path, stream) -> list:
    """The archive's root entries, read once per file version (render_car runs once per
    car, and a batch archive is a few GB)."""
    st = archive.stat()
    key = (str(archive.resolve()), st.st_size, st.st_mtime_ns)
    if key not in _ROOTS:
        _ROOTS.clear()
        _ROOTS[key] = chnk.read_chunk(stream, 0).entries
    return _ROOTS[key]


RENDER_SIZE = (1000, 560)  # about 2.4 x the car's size in the picture: the fit averages it down


def render_car(archive: Path, spool_id: int, size: tuple[int, int] = RENDER_SIZE, camera=None) -> Image:
    """The converted car as the stock garage pictures show theirs (car_render.py:
    exterior, interior through the glass, floor shadow), cropped; `.solid` is the
    car's own box in the image (without the shadow), which compose_picture fits."""
    from dsfforza.game import car_render

    scene = car_render.load_scene(archive, spool_id)
    rgba = car_render.render(scene, camera or car_render.STOCK_CAMERA, *size)
    img = Image.from_array(rgba)
    solid = img.bbox(250)
    box = img.bbox(4)
    if box is None or solid is None:
        return img
    out = img.crop(*box)
    out.solid = (solid[0] - box[0], solid[1] - box[1], solid[2] - box[0], solid[3] - box[1])
    return out


# --- the camera of the stock pictures and of FH1's thumbnails (`gui_assets camera`) ---------

FIT_LEFT = 135  # stock pictures: the logo and name left of this, not the car


def _mask_box(mask) -> tuple[int, int, int, int] | None:
    ys, xs = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    return (int(xs[0]), int(ys[0]), int(xs[-1]) + 1, int(ys[-1]) + 1) if len(ys) else None


def _rescale(mask, width: int, height: int):
    """Nearest-neighbour resize of a bool mask."""
    ys = (np.arange(height) + 0.5) * mask.shape[0] / height
    xs = (np.arange(width) + 0.5) * mask.shape[1] / width
    return mask[ys.astype(int)][:, xs.astype(int)]


def stock_car_mask(picture: Image, backdrop: Image):
    """(256, 512) bool: where a stock picture shows its car (opaque, and outside the
    backdrop panel or unlike the backdrop)."""
    a = picture.array().astype(int)
    yy, xx = np.mgrid[0 : picture.h, 0 : picture.w]
    outside = (xx >= PANEL[2]) | (yy >= PANEL[3])
    diff = np.abs(a[..., :3] - backdrop.array()[..., :3].astype(int)).sum(axis=2)
    mask = (a[..., 3] >= 200) & (outside | (diff > 40))
    mask[:, :FIT_LEFT] = False
    return mask


def _stock_score(target):
    """IoU right of FIT_LEFT of a coverage mask placed like the target: scaled to its
    height, aligned on its right and bottom edges."""
    _, ty0, tx1, ty1 = _mask_box(target)

    def score(cov) -> float:
        box = _mask_box(cov)
        if box is None:
            return -1.0
        x0, y0, x1, y1 = box
        h = ty1 - ty0
        w = max(1, round((x1 - x0) * h / (y1 - y0)))
        placed = np.zeros_like(target)
        scaled = _rescale(cov[y0:y1, x0:x1], w, h)
        left = tx1 - w
        placed[ty0:ty1, max(0, left) : tx1] = scaled[:, max(0, -left) :]
        a, b = placed[:, FIT_LEFT:], target[:, FIT_LEFT:]
        return float((a & b).sum() / max(1, (a | b).sum()))

    return score


def _shape_score(target):
    """IoU of the two masks' boxes scaled to one size, less the log of their aspect
    ratios' mismatch (a thumbnail shows the whole car)."""
    x0, y0, x1, y1 = _mask_box(target)
    t = _rescale(target[y0:y1, x0:x1], 400, 200)
    aspect = (x1 - x0) / (y1 - y0)

    def score(cov) -> float:
        box = _mask_box(cov)
        if box is None:
            return -1.0
        c = _rescale(cov[box[1] : box[3], box[0] : box[2]], 400, 200)
        mismatch = abs(math.log((box[2] - box[0]) / (box[3] - box[1]) / aspect))
        return float((c & t).sum() / max(1, (c | t).sum())) - mismatch

    return score


def fit_cameras(manifest: Path | None = None, names: list[str] | None = None, log=print) -> list[dict]:
    """The camera of each stock picture (`names`, default all cars), fitted with
    renders of the stock car's silhouette; with `manifest`, the camera of each
    ported car's FH1 thumbnail instead, fitted with renders of the converted car."""
    import json

    from dsfforza.game import car_render

    out = []
    if manifest is None:
        archive = game_paths.stock(None)
        backdrop = stock_backdrop()
        spool = {}
        for sid, fields in stock_stats().items():
            spool.setdefault(str(fields.get("Picture1", "")).strip('"'), sid)
        with stock_file("Install/gui.sp").open("rb") as stream:
            root = chnk.read_chunk(stream, 0)
            jobs = []
            for name in names or stock_picture_names():
                dds = find_picture(stream, root, name)
                if dds is not None and name in spool:
                    from dsfforza.convert import dsf_materials as dm

                    jobs.append((name, spool[name], Image(*dm.decode_dds(dds))))
        for name, sid, picture in jobs:
            scene = car_render.load_scene(archive, sid, interior=False)
            score, cam = car_render.fit_camera(scene, _stock_score(stock_car_mask(picture, backdrop)))
            out.append({"car": name, "spool_id": sid, "score": score, **cam.__dict__})
            log(
                f"{name} (spool {sid}): IoU {score:.3f}, yaw {cam.yaw:.0f}, pitch {cam.pitch:.0f}, distance {cam.distance:.2f}"
            )
    else:
        archive = Path(manifest).with_suffix(".sp")
        ui = ForzaUI()
        for car in json.loads(Path(manifest).read_text())["cars"]:
            if names and car["media"] not in names:
                continue
            car_id = forza_car_id(car["media"])
            thumb = forza_car_image(ui, car_id) if car_id else None
            if thumb is None:
                continue
            scene = car_render.load_scene(archive, car["spool_id"], interior=False)
            score, cam = car_render.fit_camera(scene, _shape_score(thumb.array()[..., 3] > 128))
            out.append({"car": car["media"], "spool_id": car["spool_id"], "score": score, **cam.__dict__})
            log(
                f"{car['media']}: score {score:.3f}, yaw {cam.yaw:.0f}, pitch {cam.pitch:.0f}, distance {cam.distance:.2f}"
            )
        ui.close()
    if out:
        med = {k: float(np.median([o[k] for o in out])) for k in ("yaw", "pitch", "distance")}
        log(f"median of {len(out)}: yaw {med['yaw']:.1f}, pitch {med['pitch']:.1f}, distance {med['distance']:.2f}")
    return out


# --- HUD badge and model name: media/Fonts.fchunk "vehicles_font" -----------------------
#
# The HUD and garage print vehicleStats Manufacturer and Model as one character
# each ("%c") in the font "vehicles_font" (Fonts.fchunk PCKG 2): a 1024 x 500
# DXT5 atlas of maker badges and model nameplates. FNTD:
#   +0x00 u32 pages (1), u32 glyph count, u32 kerning pairs (0), u32 ?,
#   u16[8] (1, 2, 99, 10, 56, 1024, 512, 0), +0x20 pages x {u64 texture UID,
#   u32 width, u32 height}, then per glyph (sorted by code) {u16 code, x, y, w,
#   h, x offset, y offset, advance 99, 99, page}: glyphs sit in a 99 x 99 cell,
#   bottom-aligned (y offset + h = 99) and centred.
# Codes 33..255 are used except 60, 127..159, 212, 213 and 245; glyphs 74..78,
# 88, 160, 244 and 252 exist but no vehicleStats entry uses them.

VEHICLES_FONT = "vehicles_font"
FREE_CODES = (212, 213, 245)  # no glyph, Latin-1 = cp1252
SPARE_CODES = (
    252,
    244,
    88,
    78,
    77,
    76,
    75,
    74,
)  # glyphs no vehicle uses; not 160 (0xA0, no-break space: drawn blank, M5 in out/testdrive3)
UNTESTED_CODES = tuple(range(128, 160))  # cp1252 differs from Latin-1 here
CELL = 99


def font_package(stream, root) -> tuple[int, chnk.Entry]:
    for i, e in enumerate(root.entries):
        if e.tag == "PCKG":
            fntd = child(child(e, "NONR"), "FNTD")
            if fntd.name == VEHICLES_FONT:
                return i, e
    raise ValueError("no vehicles_font in Fonts.fchunk")


def read_glyphs(fntd: bytes) -> dict[int, list[int]]:
    """{code: [code, x, y, w, h, x offset, y offset, advance, 99, page]}. FNTD:
    u32 pages, u32 glyphs, u32 kerning pairs, ..., at 0x20 pages x {u64 texture
    UID, u32 width, u32 height}, then the glyph records (20 bytes, by code)."""
    pages, count = struct.unpack_from("<II", fntd, 0)
    base = 0x20 + 16 * pages
    return {
        struct.unpack_from("<H", fntd, base + 20 * i)[0]: list(struct.unpack_from("<10H", fntd, base + 20 * i))
        for i in range(count)
    }


def glyph_images(fonts: Path | None = None) -> tuple[Image, dict]:
    fonts = fonts or stock_file("media/Fonts.fchunk")
    """The vehicles_font texture pages stacked top to bottom (page order), and
    the glyph records (y within its page)."""
    from dsfforza.convert import dsf_materials as dm

    with fonts.open("rb") as stream:
        root = chnk.read_chunk(stream, 0)
        _, pk = font_package(stream, root)
        fntd = payload(stream, child(child(pk, "NONR"), "FNTD"))
        textures = {
            payload(stream, child(t, "UIDC"))[:8]: payload(stream, child(t, "DXTC"))
            for t in child(child(pk, "MDXN"), "DRSP").children
            if t.tag == "TEXC"
        }
    pages = struct.unpack_from("<I", fntd, 0)[0]
    images = [Image(*dm.decode_dds(textures[fntd[0x20 + 16 * p : 0x28 + 16 * p]])) for p in range(pages)]
    sheet = Image(max(i.w for i in images), sum(i.h for i in images))
    y = 0
    for img in images:
        sheet.px[y * sheet.w * 4 : (y + img.h) * sheet.w * 4] = img.px
        y += img.h
    return sheet, read_glyphs(fntd)


# Stock glyph metrics (vehicles_font, 186 glyphs): maker badges sit on the
# cell's bottom (y offset + h = 99, about 56 x 56), model nameplates are
# centred on y = 25 (y offset = 25 - h / 2, median 97 x 21, up to 47 high);
# both are centred across the 99-wide cell. The first ported glyphs were all
# bottom-aligned, so their nameplates sat about 60 units low, over the HUD's
# boost bar (user report, out/testdrive).
BADGE_SIZE = 58
PLATE_CENTRE_Y = 25


def glyph_y(img: Image, h: int) -> int:
    if getattr(img, "kind", "badge") == "plate":
        return max(0, min(CELL - h, round(PLATE_CENTRE_Y - h / 2)))
    return CELL - h


# 16-bit glyph codes (confirmed-static, research/gui-assets.md "Unlimited badges
# and plates"): the font takes any u16 code and several texture pages (record
# +0x12 = page index), but the stock HUD passes Manufacturer/Model as one byte.
# dsfforza.asi [hud] gives each HUD text element a slot code (HUD_SLOT_CODES),
# copies the 16-bit glyph's record (position, size, offsets, page) over the
# slot's record in the loaded font and sets the element's text to that one
# byte, for cars with vehicleStats ManufacturerGlyph/ModelGlyph. Wide codes start here and live on extra
# pages, the same size as page 0 (the game sizes every page from page 0's
# FNTD entry, FUN_00729550).
WIDE_FIRST = 0x100
HUD_SLOT_CODES = (1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 14, 15)  # empty records the plugin rewrites (mod_hud.cpp)


def _font_uid(n: int, used: set) -> bytes:
    """Texture UID for an extra vehicles_font page: stock-shaped (byte 1 = 0x12,
    byte 7 below 0x80) and apart from gui.sp's picture UIDs (byte 3 = 0x7F)."""
    while True:
        uid = bytes([(n << 4) & 0xFF, 0x12, (n >> 4) & 0xFF, 0x7F, 0xF0, 0x5A, 0x2E, 0x2D])
        if uid not in used:
            return uid
        n += 1


def _dxt5_sparse(img: Image) -> bytes:
    """DXT5 of an image that is mostly transparent: empty 4 x 4 blocks get one
    precomputed block, the rest dsf_materials.encode_dxt5."""
    from dsfforza.convert import dsf_materials as dm

    empty = dm.encode_dxt5(bytes(64), 4, 4)
    out = bytearray()
    w = img.w
    for by in range(0, img.h, 4):
        for bx in range(0, w, 4):
            rows = [img.px[((by + y) * w + bx) * 4 : ((by + y) * w + bx + 4) * 4] for y in range(4)]
            if not any(r[3::4].count(0) != 4 for r in rows):
                out += empty
            else:
                out += dm.encode_dxt5(b"".join(rows), 4, 4)
    return bytes(out)


def _shelf_pack(fitted: dict[int, Image], width: int, height: int | None, top: int = 1):
    """Shelf-pack glyphs 1 texel apart, tallest first. With `height`, start a new
    page when one is full: returns {code: (page, x, y)} and the y extent of the
    last row (without `height`, everything is page 0 from `top` down)."""
    placed, page, x, y, shelf = {}, 0, 1, top, 0
    for code in sorted(fitted, key=lambda c: (-fitted[c].h, c)):
        g = fitted[code]
        if x + g.w + 1 > width:
            x, y, shelf = 1, y + shelf + 1, 0
        if height is not None and y + g.h + 1 > height:
            page, x, y, shelf = page + 1, 1, 1, 0
        placed[code] = (page, x, y)
        x += g.w + 1
        shelf = max(shelf, g.h)
    return placed, y + shelf + 1


def add_glyphs(fonts: Path, out: Path, glyphs: dict[int, Image], textures: dict[bytes, bytes] | None = None) -> dict:
    """Write a copy of Fonts.fchunk whose vehicles_font has `glyphs` (code ->
    image, at most 98 x 98). Codes below 256 go in new rows under the stock
    atlas (page 0; the stock DXT blocks are kept, only the new rows are
    encoded); existing codes are redirected to the new image. Codes from 256
    (WIDE_FIRST) go on extra texture pages the size of page 0, each a new TEXC
    in the font's DRSP with its UID in MDID and in FNTD's page table.
    `textures` ({u64 UID: DDS}, e.g. the garage card emblem) ride along as
    more TEXCs in the DRSP (in MDID, not in FNTD), as add_textures does."""
    from dsfforza.convert import dsf_materials as dm

    with fonts.open("rb") as stream:
        root = chnk.read_chunk(stream, 0)
        index, pk = font_package(stream, root)
        pk = copy.deepcopy(pk)
        fntd_e = child(child(pk, "NONR"), "FNTD")
        drsp = child(child(pk, "MDXN"), "DRSP")
        texc = child(drsp, "TEXC")
        dxtc = child(texc, "DXTC")
        fntd = bytearray(payload(stream, fntd_e))
        dds = payload(stream, dxtc)
        uid0 = payload(stream, child(texc, "UIDC"))[:8]
        mdid_uids = payload(stream, child(drsp, "MDID"))
        used_uids = {
            payload(stream, child(t, "UIDC"))[:8]
            for e in root.entries
            if e.tag == "PCKG"
            for t in child(child(e, "MDXN"), "DRSP").children
            if t.tag == "TEXC"
        }
        data = bytearray(fonts.read_bytes())
    height, width = struct.unpack_from("<II", dds, 12)
    pages, count = struct.unpack_from("<II", fntd, 0)
    if pages != 1:
        raise ValueError(f"vehicles_font has {pages} pages; expected the stock single page")
    records = read_glyphs(fntd)
    tail = bytes(fntd[0x30 + 20 * count :])  # kerning pairs (none in stock)
    fitted = {
        code: (img if img.w <= CELL - 1 and img.h <= CELL - 1 else img.fit(CELL - 1, CELL - 1))
        for code, img in glyphs.items()
    }
    narrow = {c: g for c, g in fitted.items() if c < WIDE_FIRST}
    wide = {c: g for c, g in fitted.items() if c >= WIDE_FIRST}

    # Page 0: the stock atlas plus rows below it for one-byte codes.
    new_height = height
    if narrow:
        placed0, bottom = _shelf_pack(narrow, width, None, height + 1)
        new_height = (bottom + 3) // 4 * 4
        extra = Image(width, new_height - height)
        for code, (_, gx, gy) in placed0.items():
            extra.paste(narrow[code], gx, gy - height)
            records[code] = [
                code,
                gx,
                gy,
                narrow[code].w,
                narrow[code].h,
                (CELL - narrow[code].w) // 2,
                glyph_y(glyphs[code], narrow[code].h),
                CELL,
                CELL,
                0,
            ]
        new_dds = bytearray(dds[: dm.DDS_HEADER_SIZE])
        struct.pack_into("<I", new_dds, 12, new_height)
        struct.pack_into("<I", new_dds, 20, width * new_height)  # linear size (DXT5: 1 byte per texel)
        new_dds += dds[dm.DDS_HEADER_SIZE : dm.DDS_HEADER_SIZE + width * height]
        new_dds += dm.encode_dxt5(extra.px, width, extra.h)
        dxtc.payload = bytes(new_dds)
    else:
        placed0 = {}

    # Pages 1..n for 16-bit codes, the size of page 0, and the plugin's slot records.
    page_uids = [uid0]
    placed_wide = {}
    if wide:
        placed_wide, _ = _shelf_pack(wide, width, new_height)
        n_pages = max(p for p, _, _ in placed_wide.values()) + 1
        sheets = [Image(width, new_height) for _ in range(n_pages)]
        for code, (p, gx, gy) in placed_wide.items():
            sheets[p].paste(wide[code], gx, gy)
            records[code] = [
                code,
                gx,
                gy,
                wide[code].w,
                wide[code].h,
                (CELL - wide[code].w) // 2,
                glyph_y(glyphs[code], wide[code].h),
                CELL,
                CELL,
                p + 1,
            ]
        new_texcs = []
        for n, sheet in enumerate(sheets):
            uid = _font_uid(n, used_uids)
            used_uids.add(uid)
            page_dds = bytearray(dds[: dm.DDS_HEADER_SIZE])
            struct.pack_into("<I", page_dds, 12, new_height)
            struct.pack_into("<I", page_dds, 20, width * new_height)
            page_dds += _dxt5_sparse(sheet)
            t = copy.deepcopy(texc)
            child(t, "UIDC").payload = uid
            child(t, "DXTC").payload = bytes(page_dds)
            new_texcs.append(t)
            page_uids.append(uid)
        for code in HUD_SLOT_CODES:
            if code in records:
                raise ValueError(
                    f"glyph code {code} is a dsfforza.asi HUD slot; don't use --control-codes with 16-bit glyphs"
                )
            records[code] = [code, 0, 0, 0, 0, 0, 0, CELL, CELL, 0]
        mdid = child(drsp, "MDID")
        drsp.children = [c for c in drsp.children if c.tag != "MDID"] + new_texcs + [mdid]
        mdid.payload = mdid_uids = b"".join(page_uids)
    if textures:
        mdid = child(drsp, "MDID")
        extra = []
        for uid, tex in textures.items():
            t = copy.deepcopy(texc)
            child(t, "UIDC").payload = uid
            child(t, "DXTC").payload = tex
            extra.append(t)
        drsp.children = [c for c in drsp.children if c.tag != "MDID"] + extra + [mdid]
        mdid.payload = bytes(mdid_uids) + b"".join(textures)

    header = bytearray(fntd[:0x20])
    struct.pack_into("<II", header, 0, len(page_uids), len(records))
    table = b"".join(uid + struct.pack("<II", width, new_height) for uid in page_uids)
    body = b"".join(struct.pack("<10H", *records[c]) for c in sorted(records))
    fntd_e.payload = bytes(header) + table + body + tail
    data = append_patch(fonts, data, root, {index: pk}, [])
    out.write_bytes(data)
    glyph_report = {str(c): list(placed0[c][1:]) for c in placed0}
    glyph_report.update(
        {str(c): [placed_wide[c][1], placed_wide[c][2], f"page {placed_wide[c][0] + 1}"] for c in placed_wide}
    )
    return {"glyphs": glyph_report, "height": new_height, "pages": len(page_uids), "count": len(records)}


# --- garage card emblem (research/gui-assets.md, "Garage card emblem") ---------------------
#
# The "SF" disc at a garage car card's top right is the sprite object
# Spr3D.unlocked<N> of card N (GUI_FrontEnd.fchunk). All 20 share one sprite
# resource: UVs CARD_SF_BOX into the card icon atlas (texture
# 20125a7f761d9d2c, 512 x 256 BGRA). For a car with vehicleStats
# GarageEmblem = 1, dsfforza.asi [hud] (card_emblem) points that object at the
# card picture resource Header_Sprite_20 (free while the maker group has fewer
# than 20 cars), copies the SF resource's UVs into it and binds texture
# FORZA_LOGO_UID, which rides in the vehicles_font package of Fonts.fchunk so
# it is always loaded. The sprite sizes itself from the bound texture, so the
# emblem texture is laid out like the atlas: 512 x 256, the Forza logo in the
# SF rect, transparent elsewhere (a 100 x 72 texture drew at a fifth of the
# size, a 512 x 256 one with full UVs five times too big).

CARD_SF_BOX = (242, 2, 345, 75)  # SF disc UV rect in the atlas, texels (in-game UVs x 512 / 256)
CARD_ATLAS_SIZE = (512, 256)
FORZA_LOGO_UID = bytes.fromhex("f012ef7ef05a2e2d")  # = mod_hud.cpp kLogoLo/kLogoHi
EMBLEM_FIELD = "GarageEmblem"  # vehicleStats field: 1 = Forza emblem


def forza_emblem_logo(ui: ForzaUI) -> Image:
    """FH1's Forza logo with alpha (ui4/common/forza_logo3: orange and silver)."""
    found = ui.read("UI4/Common/forza_logo3.xds")
    if found is None:
        raise ValueError("forza_logo3.xds not in FH1 Textures.zip")
    img = Image(*found)
    return img.crop(*img.bbox(12))


def forza_emblem_image(logo: Image, margin: int = 2) -> Image:
    """The emblem texture: the card icon atlas's layout with `logo` fitted
    (aspect kept, centred) in the SF disc's rect, transparent elsewhere."""
    x0, y0, x1, y1 = CARD_SF_BOX
    out = Image(*CARD_ATLAS_SIZE)
    fitted = logo.fit(x1 - x0 - 2 * margin, y1 - y0 - 2 * margin)
    out.paste(fitted, x0 + (x1 - x0 - fitted.w) // 2, y0 + (y1 - y0 - fitted.h) // 2)
    return out


def add_textures(fonts: Path, out: Path, textures: dict[bytes, bytes]) -> dict:
    """Write a copy of Fonts.fchunk (stock or already patched) whose
    vehicles_font package also carries `textures` ({u64 UID: DDS}): a TEXC
    each in its DRSP, UIDs added to MDID. The font doesn't use them (FNTD is
    unchanged); the package just keeps them loaded for the plugin."""
    with fonts.open("rb") as stream:
        root = chnk.read_chunk(stream, 0)
        index, pk = font_package(stream, root)
        pk = copy.deepcopy(pk)
        drsp = child(child(pk, "MDXN"), "DRSP")
        texc = child(drsp, "TEXC")
        mdid = child(drsp, "MDID")
        uids = bytearray(payload(stream, mdid))
        present = {payload(stream, child(t, "UIDC"))[:8] for t in drsp.children if t.tag == "TEXC"}
        data = bytearray(fonts.read_bytes())
    new = []
    for uid, dds in textures.items():
        if uid in present:
            raise ValueError(f"{fonts}: texture {uid.hex()} is already in vehicles_font")
        t = copy.deepcopy(texc)
        child(t, "UIDC").payload = uid
        child(t, "DXTC").payload = dds
        new.append(t)
        uids += uid
    drsp.children = [c for c in drsp.children if c.tag != "MDID"] + new + [mdid]
    mdid.payload = bytes(uids)
    out.write_bytes(append_patch(fonts, data, root, {index: pk}, []))
    return {"textures": [uid.hex() for uid in textures]}


def emblem_dds(ui: ForzaUI, template: bytes | None = None) -> tuple[bytes, Image]:
    img = forza_emblem_image(forza_emblem_logo(ui))
    return picture_dds(img, template), img


# --- names: Locale/<language>/global.fchunk and vehicleStats ------------------------------
#
# global.fchunk SPOO/NONR/SLRR: SLRH {u32 count, ...}, SLRC count x {u32 byte
# offset into SLRS, u32 string id} (ids ascending), SLRS UTF-16 strings, each
# 0-terminated. "ID:242768" in Lua is string 242768 ("NISSAN").


def read_strings(path: Path | None = None) -> dict[int, str]:
    path = path or stock_file("Locale/English/global.fchunk")
    with path.open("rb") as stream:
        root = chnk.read_chunk(stream, 0)
        slrr = child(child(root.entries[0], "NONR"), "SLRR")
        lookup, text = payload(stream, child(slrr, "SLRC")), payload(stream, child(slrr, "SLRS"))
    out = {}
    for i in range(len(lookup) // 8):
        offset, sid = struct.unpack_from("<II", lookup, 8 * i)
        end = offset
        while end + 1 < len(text) and text[end : end + 2] != b"\0\0":  # bounded: the last string may lack its 0
            end += 2
        out[sid] = text[offset:end].decode("utf-16-le")
    return out


# New strings for ported makers and models: IDs from here up (stock IDs run
# 36190 to 249144 in global.fchunk and loc.sp), "ID:<n>" in vehicleStats.
FIRST_STRING_ID = 700000


def add_strings(source: Path, out: Path, texts: dict[int, str]) -> dict:
    """Write a copy of a Locale global.fchunk with `texts` (id -> text) added:
    SLRC gains {byte offset, id} rows (kept sorted by id), SLRS the UTF-16
    texts, SLRH its count; the package is appended and the root row retargeted
    (append_patch), so no stock byte moves."""
    with source.open("rb") as stream:
        root = chnk.read_chunk(stream, 0)
        index = next(i for i, e in enumerate(root.entries) if e.tag == "SPOO")
        spoo = copy.deepcopy(root.entries[index])
        slrr = child(child(spoo, "NONR"), "SLRR")
        head, lookup, text = (bytearray(payload(stream, child(slrr, t))) for t in ("SLRH", "SLRC", "SLRS"))
        data = bytearray(source.read_bytes())
    rows = [struct.unpack_from("<II", lookup, 8 * i) for i in range(len(lookup) // 8)]
    existing = {sid for _, sid in rows}
    for sid, value in sorted(texts.items()):
        if sid in existing:
            raise ValueError(f"string id {sid} already in {source}")
        rows.append((len(text), sid))
        text += value.encode("utf-16-le") + b"\0\0"
    rows.sort(key=lambda r: r[1])
    struct.pack_into("<I", head, 0, len(rows))
    child(slrr, "SLRH").payload = bytes(head)
    child(slrr, "SLRC").payload = b"".join(struct.pack("<II", *r) for r in rows)
    child(slrr, "SLRS").payload = bytes(text)
    data = append_patch(source, data, root, {index: spoo}, [])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return {"strings": len(texts), "total": len(rows)}


def stock_stats() -> dict[int, dict]:
    """vehicleStats.lua (decompiled stock copy) as {id: {field: value}} (game_data.read_vehiclestats)."""
    if not VEHICLE_STATS_LUA.exists():
        return {}
    from dsfforza.game import game_data

    return game_data.read_vehiclestats(VEHICLE_STATS_LUA)


def stock_codes(strings: dict[int, str]) -> tuple[dict, dict]:
    """({MAKE: (Manufacturer code, ManufacturerName)}, {(MAKE, MODEL): (Model code, ModelName)})
    from the stock vehicleStats, names upper-cased as the strings are."""
    makes, models = {}, {}
    for fields in stock_stats().values():
        try:
            mf_id, mo_id = fields["ManufacturerName"], fields["ModelName"]
            make = strings.get(int(mf_id.split(":")[1]), "").upper()
            model = strings.get(int(mo_id.split(":")[1]), "").upper()
            makes.setdefault(make, (int(fields["Manufacturer"]), mf_id))
            models.setdefault((make, model), (int(fields["Model"]), mo_id))
        except (KeyError, ValueError, IndexError):
            continue
    return makes, models


def pretty_model(model: str) -> str:
    """ "SkylineGTR" -> "SKYLINE GTR" (media names run words together)."""
    import re

    return re.sub(r"(?<=[a-z])(?=[A-Z0-9])|(?<=[A-Z])(?=[A-Z][a-z])", " ", model).upper().strip()


def forza_car_id(media: str) -> int | None:
    import sqlite3

    db_path = REPO / "out" / "cache" / "fh1" / "gamedb.slt"
    if not db_path.exists():
        return None
    db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT Id FROM Data_Car WHERE lower(MediaName) = lower(?)", (media,)).fetchone()
    finally:
        db.close()
    return row[0] if row else None


# --- names and glyph codes for a batch -----------------------------------------------------
#
# vehicleStats Manufacturer and Model are single characters ("%c") of
# vehicles_font, so the code space is one byte: 186 stock glyphs, 44 more codes
# used so far (FREE, SPARE, 128..159), at most about 28 more among the control
# codes 1..31 (CONTROL_CODES, untested: the text path may act on them). That
# is about 70 new glyphs per install however the font grows, so badges are
# shared per maker, nameplates per name (all the Mustangs' "MUSTANG" family
# plate when needed), stock badges and plates are reused when DSF has the same
# maker or model, and whatever still does not fit gets one shared blank plate
# (the HUD shows the badge alone, not the host's name).

CONTROL_CODES = tuple(c for c in range(1, 32) if c not in (9, 10, 13))
# Forza maker names (List_CarMake, upper-case) that are the same badge in DSF.
MAKE_ALIASES = {
    "SRT": "DODGE",
    "VIPER": "DODGE",
    "VW": "VOLKSWAGEN",
    "MERCEDES": "MERCEDES-BENZ",
    "LANDROVER": "LAND ROVER",
    "GMC TRUCK": "GMC",
    "GMCTRUCK": "GMC",
}


def dsf_make(make: str, model: str = "") -> str:
    """Forza maker -> DSF maker key (upper-case, as DSF's strings). DSF has no
    Mercedes-Benz or Chrysler badge (its SLR is under McLaren, "mercades" in
    Picture1), so those stay their own makers except where DSF files the model
    under another badge: SLR McLaren -> MCLAREN, Range Rover -> RANGE ROVER,
    SRT (Viper) -> DODGE."""
    key = make.upper().strip()
    key = MAKE_ALIASES.get(key.replace(" ", ""), MAKE_ALIASES.get(key, key))
    m = model.upper()
    if key == "MERCEDES-BENZ" and "MCLAREN" in m:
        return "MCLAREN"
    if key == "LAND ROVER" and "RANGE ROVER" in m:
        return "RANGE ROVER"
    return key


def car_texts(cars: list[dict]) -> dict[str, dict]:
    """{media: {make, model, family, year}}: Forza's own names (Data_Car
    DisplayName, List_CarMake, List_FamilyModel via src/dsfforza/formats/forza_strings.py),
    upper-cased; media-name guesses where a game has no entry."""
    from dsfforza.build.port_car import make_model

    out = {}
    by_game: dict[str, list[str]] = {}
    for car in cars:
        by_game.setdefault(str(car.get("game", "FH1")).lower(), []).append(car["media"])
    found: dict[str, dict] = {}
    try:
        from dsfforza.formats import forza_strings

        for game, media in by_game.items():
            for other in [game] + [g for g in ("fh1", "fm4") if g != game]:
                missing = [m for m in media if m not in found]
                if not missing:
                    break
                try:
                    found.update({k: v for k, v in forza_strings.car_names(other, missing).items() if v.get("display")})
                except Exception:
                    continue
    except ImportError:
        pass
    for car in cars:
        media = car["media"]
        make, model = make_model(media)
        info = found.get(media, {})
        name = (info.get("display") or pretty_model(model)).upper().strip()
        family = (info.get("family") or "").upper().strip()
        out[media] = {
            "make": (info.get("make") or make).upper(),
            "model": name,
            "family": family if family and family != "NONE" else None,
            "year": info.get("year"),
        }
    return out


def plan_glyphs(
    cars: list[dict], texts: dict[str, dict], makes: dict, models: dict, free: list[int], logos
) -> tuple[dict, dict, dict]:
    """Assign Manufacturer/Model codes. Returns ({media: entry fields},
    {code: Image}, notes). `logos(make)` gives the Forza logo Image or None.

    Stock badges and plates are reused where DSF has the maker or the model
    (or its family) under that maker. New badges are one per maker, new
    plates one per name. When the codes don't cover every new badge and
    plate: plates fall back to family names (Forza List_FamilyModel) where
    that merges cars, then badges and plates are chosen by the number of cars
    they cover, and two codes become a blank badge and a blank plate for the
    rest (the HUD then shows nothing there, rather than the host's)."""
    entries: dict[str, dict] = {car["media"]: {} for car in cars}
    glyphs: dict[int, Image] = {}
    notes = {"badges": {}, "plates": {}, "family_plates": [], "blank_plate": [], "blank_badge": [], "no_logo": []}
    free = list(free)
    new_makes: dict[str, list[str]] = {}
    for car in cars:
        media, t = car["media"], texts[car["media"]]
        t["dsf_make"] = key = dsf_make(t["make"], t["model"])
        if key in makes:
            entries[media]["Manufacturer"], entries[media]["ManufacturerName"] = makes[key]
        else:
            entries[media]["ManufacturerName"] = key
            new_makes.setdefault(key, []).append(media)
    badge_logos = {}
    for key, media in new_makes.items():
        logo = logos(key) or logos(texts[media[0]]["make"])
        if logo is None:
            notes["no_logo"].append(key)  # keeps the host's badge
        else:
            badge_logos[key] = logo
    want: dict[str, str] = {}
    for car in cars:
        media, t = car["media"], texts[car["media"]]
        entries[media]["ModelName"] = t["model"]
        for text in (t["model"], t["family"]):
            if text and (t["dsf_make"], text) in models:
                entries[media]["Model"], entries[media]["ModelName"] = models[(t["dsf_make"], text)]
                break
        else:
            want[media] = t["model"]

    def need():
        return len(badge_logos) + len(set(want.values()))

    if need() > len(free):
        # Family names where several cars share one (most shared first).
        fam_count: dict[str, int] = {}
        for media in want:
            fam = texts[media]["family"]
            if fam:
                fam_count[fam] = fam_count.get(fam, 0) + 1
        for media in sorted(want, key=lambda m: -fam_count.get(texts[m]["family"] or "", 0)):
            if need() <= len(free):
                break
            fam = texts[media]["family"]
            if fam and fam_count.get(fam, 0) > 1 and want[media] != fam:
                want[media] = fam
                notes["family_plates"].append(media)
    plate_cars: dict[str, list[str]] = {}
    for media, text in want.items():
        plate_cars.setdefault(text, []).append(media)
    items = [(len(new_makes[k]), "badge", k) for k in badge_logos] + [
        (len(m), "plate", t) for t, m in plate_cars.items()
    ]
    items.sort(key=lambda i: (-i[0], i[1], i[2]))
    room = len(free)
    if len(items) > room:  # keep a code for each kind of blank that is needed
        room = max(0, len(free) - 1)
        if len({kind for _, kind, _ in items[room:]}) > 1:
            room = max(0, len(free) - 2)
    chosen, rest = items[:room], items[room:]
    for _, kind, key in chosen:
        code = free.pop(0)
        if kind == "badge":
            glyphs[code] = badge_logos[key].fit(BADGE_SIZE, BADGE_SIZE)
            glyphs[code].kind = "badge"
            notes["badges"][key] = code
            for media in new_makes[key]:
                entries[media]["Manufacturer"] = code
        else:
            glyphs[code] = nameplate(key)
            glyphs[code].kind = "plate"
            notes["plates"][key] = code
            for media in plate_cars[key]:
                entries[media]["Model"] = code
    blanks = {}
    for _, kind, key in rest:
        if kind not in blanks:
            if not free:
                continue
            blanks[kind] = free.pop(0)
            glyphs[blanks[kind]] = Image(1, 1)
            glyphs[blanks[kind]].kind = kind
            notes[f"blank_{kind}_code"] = blanks[kind]
        for media in new_makes[key] if kind == "badge" else plate_cars[key]:
            entries[media]["Manufacturer" if kind == "badge" else "Model"] = blanks[kind]
            notes[f"blank_{kind}"].append(media)
    notes["codes_left"] = len(free)
    return entries, glyphs, notes


def plan_wide_glyphs(
    cars: list[dict], texts: dict[str, dict], makes: dict, models: dict, logos, first: int = WIDE_FIRST
) -> tuple[dict, dict, dict]:
    """16-bit codes for dsfforza.asi [hud]: {media: {ManufacturerGlyph,
    ModelGlyph}}, {code: Image}, notes. Run after plan_glyphs (which sets
    texts[media]["dsf_make"]). A car whose maker DSF has keeps the stock badge
    (no ManufacturerGlyph), else one new badge per maker from the Forza logo;
    a car whose exact model DSF has keeps the stock plate, else one plate per
    name (its full name, no family fallback). No budget: codes from `first`
    up, badges then plates, each sorted by name. Manufacturer/Model from
    plan_glyphs stay as the fallback for a game without the plugin."""
    entries: dict[str, dict] = {car["media"]: {} for car in cars}
    glyphs: dict[int, Image] = {}
    notes = {"badges": {}, "plates": {}, "no_logo": [], "first": first}
    badge_cars: dict[str, list[str]] = {}
    plate_cars: dict[str, list[str]] = {}
    for car in cars:
        media, t = car["media"], texts[car["media"]]
        key = t.get("dsf_make") or dsf_make(t["make"], t["model"])
        if key not in makes:
            badge_cars.setdefault(key, []).append(media)
        if (key, t["model"]) not in models:
            plate_cars.setdefault(t["model"], []).append(media)
    code = first
    for key in sorted(badge_cars):
        logo = logos(key) or logos(texts[badge_cars[key][0]]["make"])
        if logo is None:
            notes["no_logo"].append(key)
            continue
        glyphs[code] = logo.fit(BADGE_SIZE, BADGE_SIZE)
        glyphs[code].kind = "badge"
        notes["badges"][key] = code
        for media in badge_cars[key]:
            entries[media]["ManufacturerGlyph"] = code
        code += 1
    for text in sorted(plate_cars):
        glyphs[code] = nameplate(text)
        glyphs[code].kind = "plate"
        notes["plates"][text] = code
        for media in plate_cars[text]:
            entries[media]["ModelGlyph"] = code
        code += 1
    if code > 0xD800:
        raise ValueError(f"{code - first} wide glyphs run into the UTF-16 surrogate range")
    notes["count"] = code - first
    return entries, glyphs, notes


class _Renders:
    """render_car for the cars build() will render (all, or with `thumbnails` those
    without an FH1 thumbnail), in `jobs` processes a few cars ahead of the loop that
    uses them; with jobs 1 (or a car not foreseen, e.g. an empty thumbnail) in this
    process when asked."""

    def __init__(self, archive: Path, cars: list[dict], ui: ForzaUI, jobs: int, thumbnails: bool = False):
        self.archive, self.jobs, self.futures, self.pool = archive, jobs, {}, None
        self.order = []  # (car index, spool ID) of the cars that will need a render
        if jobs > 1:
            for n, car in enumerate(cars):
                car_id = forza_car_id(car["media"])
                if not thumbnails or not car_id or f"thumbnails/thumbnail_{car_id}.xds" not in ui.names:
                    self.order.append((n, car["spool_id"]))
        if len(self.order) > 1:
            from concurrent.futures import ProcessPoolExecutor

            self.pool = ProcessPoolExecutor(max_workers=jobs)

    def ahead(self, n: int) -> None:
        """Start the renders of the cars up to a few past car index n."""
        if self.pool is None:
            return
        while self.order and self.order[0][0] <= n + 2 * self.jobs:
            _, sid = self.order.pop(0)
            self.futures[sid] = self.pool.submit(render_car, self.archive, sid)

    def get(self, sid: int) -> Image:
        future = self.futures.pop(sid, None)
        return future.result() if future is not None else render_car(self.archive, sid)

    def close(self) -> None:
        if self.pool is not None:
            self.pool.shutdown(cancel_futures=True)


def build(
    manifest: Path,
    out_dir: Path,
    names: dict[str, str] | None = None,
    archive: Path | None = None,
    gui: Path | None = None,
    fonts: Path | None = None,
    log=print,
    control_codes: bool = False,
    wide_glyphs: bool = False,
    root_limit: int = chnk.ENTRY_LIMIT,
    jobs: int = 1,
    thumbnails: bool = False,
) -> dict:
    """Garage pictures, HUD glyphs and dsfforza\\hud.lua for the cars of a
    src/dsfforza/build/port_car.py manifest. Writes out_dir/gui.sp, out_dir/Fonts.fchunk,
    out_dir/hud.lua and a PNG preview per picture. `wide_glyphs` also gives
    every car without a stock badge/plate its own 16-bit glyph
    (ManufacturerGlyph/ModelGlyph, drawn by dsfforza.asi [hud]; the one-byte
    Manufacturer/Model plan stays as the fallback). Every car also gets
    GarageEmblem = 1 and Fonts.fchunk the Forza emblem texture: with
    dsfforza.asi the garage card shows the Forza logo instead of the SF disc.
    Every picture is a render of the converted car from the stock pictures'
    camera (render_car); `thumbnails` takes FH1's own thumbnail where there is one
    instead (sharper, but at eye level and mirrored: gui-assets.md, "Camera").
    `jobs` > 1 renders in that many processes (render_car is the slow step; each
    render is the same either way)."""
    import json

    data = json.loads(Path(manifest).read_text())
    archive = archive or Path(manifest).with_suffix(".sp")
    names = names or {}
    strings = read_strings()
    makes, models = stock_codes(strings)
    used_codes = set()
    for fields in stock_stats().values():
        for key in ("Manufacturer", "Model"):
            if fields.get(key, "").isdigit():
                used_codes.add(int(fields[key]))
    pool = FREE_CODES + SPARE_CODES + UNTESTED_CODES + (CONTROL_CODES if control_codes else ())
    if wide_glyphs:  # the plugin's slot codes must stay free
        pool = tuple(c for c in pool if c not in HUD_SLOT_CODES)
    free = [c for c in pool if c not in used_codes]
    ui = ForzaUI()
    gui = gui or stock_file("Install/gui.sp")
    fonts = fonts or stock_file("media/Fonts.fchunk")
    backdrop = stock_backdrop(gui)
    with gui.open("rb") as stream:
        template = find_picture(stream, chnk.read_chunk(stream, 0), "nissan_370z")
    texts = car_texts(data["cars"])
    for media, text in names.items():
        if media in texts:
            texts[media]["model"] = text.upper()
    logo_cache: dict = {}

    def logos(make: str):
        if make not in logo_cache:
            logo_cache[make] = logo_image(ui, make.title()) or logo_image(ui, make)
        return logo_cache[make]

    planned, glyphs, notes = plan_glyphs(data["cars"], texts, makes, models, free, logos)
    pictures, hud, report = {}, {}, {"_codes": notes}
    if wide_glyphs:
        wide, wide_glyph_images, report["_wide_codes"] = plan_wide_glyphs(data["cars"], texts, makes, models, logos)
        for media, fields in wide.items():
            planned[media].update(fields)
        glyphs.update(wide_glyph_images)
    renders = _Renders(Path(archive), data["cars"], ui, jobs, thumbnails)
    for n, car in enumerate(data["cars"]):
        media, sid = car["media"], car["spool_id"]
        renders.ahead(n)
        t = texts[media]
        model_text = t["model"]
        entry = dict(planned[media])
        logo = logos(t["dsf_make"]) or logos(t["make"])
        # Picture: a render of the converted car (or, with 	humbnails, Forza's own).
        car_id = forza_car_id(media) if thumbnails else None
        image = forza_car_image(ui, car_id) if car_id else None
        source = "FH1 thumbnail"
        if image is None:
            image, source = renders.get(sid), "render"
        picture = compose_picture(image, logo, nameplate(model_text, 124, 38), backdrop)
        name = f"dsfforza_{media.lower()}"
        pictures[name] = picture_dds(picture, template)
        entry["Picture1"] = name
        entry[EMBLEM_FIELD] = 1  # Forza logo instead of the SF disc on the garage card (dsfforza.asi)
        hud[sid] = entry
        preview = Image(picture.w, picture.h, picture.px)
        flat = bytearray(preview.px)
        for i in range(preview.w * preview.h):
            a = flat[i * 4 + 3] / 255
            bg = 90 if ((i % preview.w) // 16 + (i // preview.w) // 16) % 2 else 140
            for c in range(3):
                flat[i * 4 + c] = int(flat[i * 4 + c] * a + bg * (1 - a))
            flat[i * 4 + 3] = 255
        out_dir.mkdir(parents=True, exist_ok=True)
        write_png(out_dir / f"{name}.png", preview.w, preview.h, flat)
        report[media] = {"spool_id": sid, "picture": name, "picture_source": source, **entry}
        wide_text = "".join(f", {k} {entry[k]:#x}" for k in ("ManufacturerGlyph", "ModelGlyph") if k in entry)
        log(
            f"{sid} {media}: picture {name} ({source}), Manufacturer {entry.get('Manufacturer')}, "
            f"Model {entry.get('Model')} ({model_text}){wide_text}"
        )
    renders.close()
    emblem, emblem_img = emblem_dds(ui, template)
    ui.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    write_png(out_dir / "garage_emblem.png", emblem_img.w, emblem_img.h, emblem_img.px)
    report["_gui"] = add_pictures(gui, out_dir / "gui.sp", pictures, root_limit=root_limit)
    # Fonts.fchunk: new glyphs, plus the garage card emblem texture (always).
    report["_fonts"] = add_glyphs(fonts, out_dir / "Fonts.fchunk", glyphs, {FORZA_LOGO_UID: emblem})
    sheet, _records = glyph_images(out_dir / "Fonts.fchunk")
    write_png(out_dir / "vehicles_font.png", sheet.w, sheet.h, sheet.px)
    # Plain-text maker and model names don't show in the garage: give them new
    # string IDs in copies of every installed language's global.fchunk (the
    # English text for all of them).
    plain = sorted(
        {
            v
            for e in hud.values()
            for k, v in e.items()
            if k in ("ManufacturerName", "ModelName") and isinstance(v, str) and not v.startswith("ID:")
        }
    )
    string_ids = {text: FIRST_STRING_ID + n for n, text in enumerate(plain)}
    for e in hud.values():
        for k in ("ManufacturerName", "ModelName"):
            if e.get(k) in string_ids:
                e[k] = f"ID:{string_ids[e[k]]}"
    texts = {sid: text for text, sid in string_ids.items()}
    groups = continuation_groups(hud, texts, FIRST_STRING_ID + len(plain))
    for labels in groups.values():
        texts.update(labels)
    report["_groups"] = {maker: [f"ID:{sid}" for sid in labels] for maker, labels in groups.items()}
    report["_strings"] = write_locales(out_dir, texts)
    write_hud_lua(out_dir / "hud.lua", hud, report["_groups"])
    (out_dir / "gui-assets.json").write_text(json.dumps(report, indent=1, default=str))
    return report


def write_locales(out_dir: Path, texts: dict[int, str]) -> dict:
    """Copies of every installed language's global.fchunk with `texts` added
    (the same text in all of them)."""
    report = {}
    if not texts:
        return report
    locale = game_paths.game_dir() / "Locale"
    languages = sorted(p.name for p in locale.iterdir() if (p / "global.fchunk").exists()) if locale.exists() else []
    for lang in languages:
        report[lang] = add_strings(
            stock_file(f"Locale/{lang}/global.fchunk"), out_dir / "Locale" / lang / "global.fchunk", texts
        )
    return report


GARAGE_PER_GROUP = 20  # cars per garage maker group (FUN_006C7BB0; register.lua GARAGE_MAX_PER_GROUP)


def continuation_groups(hud: dict[int, dict], new_texts: dict[int, str], first_id: int) -> dict[str, dict[int, str]]:
    """Labels for the extra garage groups of makers with more than 20 selectable cars:
    {ManufacturerName: {string id: "<maker> 2", ...}}. A maker's cars are its stock
    selectable cars plus the ported ones in `hud`; register.lua puts the cars that
    don't fit its own group into "<maker> 2", "<maker> 3"... (hud.lua `_groups`)
    instead of the mixed FORZA groups. `new_texts`: this build's new strings (id ->
    text) for makers DSF doesn't have; stock maker names come from global.fchunk."""
    counts: dict[str, int] = {}
    for sid, fields in stock_stats().items():
        if fields.get("Selectable") == "true" and sid <= 303:
            key = fields.get("ManufacturerName", "")
            counts[key] = counts.get(key, 0) + 1
    for entry in hud.values():
        key = entry.get("ManufacturerName")
        if isinstance(key, str):
            counts[key] = counts.get(key, 0) + 1
    ours = {e.get("ManufacturerName") for e in hud.values()}
    stock_texts = None
    out, next_id = {}, first_id
    for maker in sorted(k for k in ours if isinstance(k, str) and counts[k] > GARAGE_PER_GROUP):
        number = int(maker.split(":", 1)[1]) if maker.startswith("ID:") else None
        text = new_texts.get(number) if number is not None else maker
        if text is None:
            stock_texts = stock_texts if stock_texts is not None else read_strings()
            text = stock_texts.get(number, maker)
        labels = {}
        for k in range(2, -(-counts[maker] // GARAGE_PER_GROUP) + 1):
            labels[next_id] = f"{text} {k}"
            next_id += 1
        out[maker] = labels
    return out


def write_hud_lua(path: Path, hud: dict[int, dict], groups: dict[str, list[str]] | None = None) -> None:
    lines = [
        "-- Generated by tools/gui_assets.py; vehicleStats overrides per ported car.",
        "-- Picture1 needs the gui.sp copy, new Manufacturer/Model codes the Fonts.fchunk copy;",
        "-- ManufacturerGlyph/ModelGlyph (16-bit codes) also need dsfforza.asi [hud];",
        "-- GarageEmblem = 1 (Forza logo on the garage card) needs it and the Fonts.fchunk copy.",
        "-- _groups: the extra garage groups of makers with more than 20 cars (register.lua).",
        "return {",
    ]
    for sid, entry in sorted(hud.items()):
        fields = ", ".join(f"{k} = {v!r}" if isinstance(v, int) else f'{k} = "{v}"' for k, v in entry.items())
        lines.append(f"\t[{sid}] = {{ {fields} }},")
    if groups:
        lines.append("\t_groups = {")
        for maker, labels in sorted(groups.items()):
            lines.append(f'\t\t["{maker}"] = {{ ' + ", ".join(f'"{x}"' for x in labels) + " },")
        lines.append("\t},")
    lines.append("}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def glyph_test(out_dir: Path, ids: list[int], plates: int = 400, badges: int = 40, fonts: Path | None = None) -> dict:
    fonts = fonts or stock_file("media/Fonts.fchunk")
    """A stress test for 16-bit codes: a Fonts.fchunk copy with `badges`
    numbered badges ("B1".."Bn") and `plates` numbered nameplates ("P1"
    ..) on extra pages, and a hud.lua giving the cars `ids` codes spread over
    them, last ones first (so the farthest pages are used)."""

    def label(text: str, height: int) -> Image:
        # Per-character renders pasted side by side: hundreds of labels in seconds.
        chars = [text_plate(ch, height) if ch != " " else Image(height // 3, height) for ch in text]
        out = Image(sum(c.w for c in chars) - 8 * (len(chars) - 1), height)
        x = 0
        for c in chars:
            out.paste(c, x, 0)
            x += c.w - 8
        return out

    glyphs: dict[int, Image] = {}
    for n in range(badges):
        g = label(f"B{n + 1}", 40).fit(BADGE_SIZE, BADGE_SIZE)
        g.kind = "badge"
        glyphs[WIDE_FIRST + n] = g
    for n in range(plates):
        g = label(f"P{n + 1}", 40).fit(98, 34)
        g.kind = "plate"
        glyphs[WIDE_FIRST + badges + n] = g
    out_dir.mkdir(parents=True, exist_ok=True)
    report = add_glyphs(fonts, out_dir / "Fonts.fchunk", glyphs)
    sheet, records = glyph_images(out_dir / "Fonts.fchunk")
    write_png(out_dir / "vehicles_font.png", sheet.w, sheet.h, sheet.px)
    hud = {}
    for i, sid in enumerate(ids):
        b, p = badges - 1 - (i % badges), plates - 1 - i * max(1, plates // max(1, len(ids)))
        hud[sid] = {"ManufacturerGlyph": WIDE_FIRST + b, "ModelGlyph": WIDE_FIRST + badges + p % plates}
        report.setdefault("cars", {})[str(sid)] = {
            "badge": f"B{b + 1}",
            "plate": f"P{p % plates + 1}",
            "plate_page": records[WIDE_FIRST + badges + p % plates][9],
        }
    write_hud_lua(out_dir / "hud.lua", hud)
    report.pop("glyphs", None)
    (out_dir / "glyph-test.json").write_text(__import__("json").dumps(report, indent=1))
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="pictures, HUD glyphs and hud.lua for a port_car manifest")
    b.add_argument("manifest", type=Path, help="<out>.json from src/dsfforza/build/port_car.py")
    b.add_argument("--out", type=Path, default=Path("out/gui"))
    b.add_argument("--archive", type=Path, help="the ported archive (default: manifest with .sp)")
    b.add_argument(
        "--install-lua",
        action="store_true",
        help="also write hud.lua to tools/game/lua/dsfforza/hud.lua (register.lua applies it; "
        "only with the matching gui.sp and Fonts.fchunk copies installed)",
    )
    b.add_argument(
        "--control-codes",
        action="store_true",
        help="also use glyph codes 1..31 (except tab/newline/return; untested in game)",
    )
    b.add_argument(
        "--name",
        action="append",
        default=[],
        metavar="MEDIA=TEXT",
        help='model name for the nameplate, e.g. NIS_SkylineGTR_93="SKYLINE GT-R"',
    )
    b.add_argument(
        "--wide-glyphs",
        action="store_true",
        help="every car its own badge/plate as 16-bit glyphs on extra font pages "
        "(ManufacturerGlyph/ModelGlyph; needs dsfforza.asi [hud], else the one-byte codes show)",
    )
    b.add_argument(
        "--root-limit",
        type=int,
        default=chnk.ENTRY_LIMIT,
        help=f"gui.sp root entries before pictures share a SPOI (default {chnk.ENTRY_LIMIT}, the stock "
        "CHNK reader; dsfforza.asi [archive] lifts it, e.g. 16384: one SPOI per picture)",
    )
    b.add_argument("--jobs", type=int, default=1, help="processes for the car renders (default 1)")
    b.add_argument(
        "--fh1-thumbnails",
        action="store_true",
        help="FH1's own thumbnail where there is one instead of the render (eye level, mirrored)",
    )
    c = sub.add_parser("camera", help="measure the stock pictures' camera (or FH1 thumbnails') against renders")
    c.add_argument("--cars", default="", help="comma-separated stock picture names / media names (default: all)")
    c.add_argument("--thumbnails", type=Path, metavar="MANIFEST", help="fit the FH1 thumbnails of these ported cars")
    t = sub.add_parser("glyphtest", help="16-bit glyph stress test: numbered badges and plates, hud.lua for IDs")
    t.add_argument("--out", type=Path, default=Path("out/hud/glyphtest"))
    t.add_argument("--ids", required=True, help="comma-separated vehicle IDs (registered ports)")
    t.add_argument("--plates", type=int, default=400)
    t.add_argument("--badges", type=int, default=40)
    e = sub.add_parser("extract", help="a gui.sp picture as PNG")
    e.add_argument("name")
    e.add_argument("png", type=Path)
    e.add_argument("--gui", type=Path, default=None)
    s = sub.add_parser("strings", help="search Locale global.fchunk strings")
    s.add_argument("pattern")
    args = parser.parse_args(argv)
    if args.cmd == "build":
        names = dict(n.split("=", 1) for n in args.name)
        build(
            args.manifest,
            args.out,
            names,
            args.archive,
            control_codes=args.control_codes,
            wide_glyphs=args.wide_glyphs,
            root_limit=args.root_limit,
            jobs=args.jobs,
            thumbnails=args.fh1_thumbnails,
        )
        if args.install_lua:
            target = REPO / "tools" / "game" / "lua" / "dsfforza" / "hud.lua"
            target.write_text((args.out / "hud.lua").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            print(f"installed {target}")
    elif args.cmd == "camera":
        fit_cameras(args.thumbnails, [c for c in args.cars.split(",") if c])
    elif args.cmd == "glyphtest":
        report = glyph_test(args.out, [int(i) for i in args.ids.split(",") if i], args.plates, args.badges)
        print(__import__("json").dumps(report, indent=1))
    elif args.cmd == "extract":
        from dsfforza.convert import dsf_materials as dm

        args.gui = args.gui or stock_file("Install/gui.sp")
        with args.gui.open("rb") as stream:
            dds = find_picture(stream, chnk.read_chunk(stream, 0), args.name)
        if dds is None:
            raise SystemExit(f"{args.name}: not in {args.gui}")
        w, h, px = dm.decode_dds(dds)
        write_png(args.png, w, h, px)
    else:
        import re

        for sid, text in read_strings().items():
            if re.search(args.pattern, text, re.I):
                print(sid, text)


if __name__ == "__main__":
    sys.exit(main())
