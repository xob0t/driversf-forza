"""The host side of a DamagePlan (host_setup), cached across cars in out/cache/damage-host."""

from __future__ import annotations

import dataclasses
import sys
from dataclasses import dataclass
from pathlib import Path

from dsfforza.convert.damage.core import K_NEAREST, load_mesh
from dsfforza.convert.damage.host import host_field
from dsfforza.convert.damage.uv import Uv1Field, local_materials
from dsfforza.formats import vehicle_mesh as vm
from dsfforza.formats.vehicle_mesh import child, payload

# --- the host side of a DamagePlan, cached across cars ------------------------------------
#
# The host's fields (host_field, Uv1Field.read) take 2 to 3 s a car and depend only on the
# host's entries, which every car on that host clones. A clone differs from its host by the
# UIDs renew_uids gives its resources (per car), and by the materials (MACO) the car's
# texturing adds before the damage plan (and a rear wheel part, split_rear_wheel). So the
# cache key hashes only what host_setup reads (MODL part 0's LODs at its masks, their
# submodels and buffers, and per submodel whether its material is the entry's own), with each entry-defined UID numbered by its first appearance there,
# and the cached fields hold the material UIDs (the only UIDs in them) as those numbers,
# put back to the car's on loading. Two clones with the same key read the same bytes up to
# that numbering, so host_setup made the same fields for both. Files are written under a
# temporary name and renamed: parallel workers may write the same key (the same bytes) but
# never read half a file.

HOST_CACHE_ENV = "DSFF_DAMAGE_CACHE"  # a folder, or "off"; default out/cache/damage-host
HOST_CACHE_VERSION = 1  # raise when what host_setup makes changes through code outside the files hashed
HOST_CACHE_TAGS = {"VXHD", "VDCL", "VXDT", "IXHD", "IXDT"}  # the buffer chunks load_mesh reads
HOST_CACHE_HEADERS = {"VXHD", "IXHD"}  # of them, the ones holding UIDs
_HOST_CODE: dict = {}  # "digest": _host_code()


def _host_cache_dir() -> Path | None:
    import os

    from dsfforza import REPO

    value = os.environ.get(HOST_CACHE_ENV, "")
    if value.lower() == "off":
        return None
    return Path(value) if value else REPO / "out" / "cache" / "damage-host"


def _host_code() -> bytes:
    """What the cached fields are made by: the damage package's files, vehicle_mesh.py, the
    Python version."""
    if "digest" not in _HOST_CODE:
        import hashlib

        h = hashlib.blake2b(digest_size=16)
        h.update(f"{HOST_CACHE_VERSION} {sys.version_info[:2]}".encode())
        for path in [*sorted(Path(__file__).parent.glob("*.py")), Path(vm.__file__)]:
            h.update(path.read_bytes())
        _HOST_CODE["digest"] = h.digest()
    return _HOST_CODE["digest"]


def _canonical(data: bytes, renewed: set, order: dict) -> bytes:
    """`data` with each 8-byte UID of `renewed` replaced by a token of its number in
    `order` (numbered on its first appearance; clone_vehicle._remap's scan)."""
    out = bytearray(data)
    i, end = 0, len(data) - 7
    while i < end:
        uid = data[i : i + 8]
        if uid not in renewed:
            i += 1
            continue
        n = order.setdefault(uid, len(order))
        out[i : i + 8] = b"\xffUID" + n.to_bytes(4, "little")
        i += 8
    return bytes(out)


def host_key(stream, entries) -> tuple[str, list]:
    """(cache key, the entries' own UIDs by number) of host_setup over `entries`
    (full, exterior, interior; None for an absent one): per entry, MODL part 0's LODs at
    the mask host_setup reads (4, 2, 64) with their submodels' SBBH (and whether each
    material is the entry's own) and, on its first use, each buffer's chunks; nothing
    else (another part the conversion added, materials it added) changes the key."""
    import hashlib

    from dsfforza.build import clone_vehicle as cv

    present = [e for e in entries if e is not None]
    renewed = {u for e in present for u in cv.defined_uids(stream, e)} | {
        u for e in present for u in cv.part_uids(stream, e)
    }
    order: dict = {}
    h = hashlib.blake2b(digest_size=20)
    h.update(_host_code())

    def put(tag: bytes, data: bytes) -> None:
        h.update(tag + len(data).to_bytes(8, "little"))
        h.update(data)

    for e, mask in zip(entries, (4, 2, 64), strict=True):
        if e is None:
            put(b"-", b"")
            continue
        local = local_materials(stream, e)
        sbbh, buffers = [], {}  # SBBH of each SBMC in DRSP order (load_mesh's); buffer chunks by UID
        for c in child(child(e, "MDXN"), "DRSP").children:
            if c.tag == "SBMC":
                sbbh.append(payload(stream, child(c, "SBBH")))
            elif c.tag in ("VXCK", "IXCK"):
                buffers.setdefault(payload(stream, child(c, "VXHD" if c.tag == "VXCK" else "IXHD"))[:8], c)
        mesh = load_mesh(stream, e)
        done = set()
        for lod in mesh.parts[0].lods if mesh.parts else ():
            if lod.mask != mask:
                continue
            put(b"LOD", len(lod.submodels).to_bytes(4, "little"))
            for n in lod.submodels:
                sub = mesh.submodels[n]
                put(b"local", b"1" if sub.material in local else b"0")
                put(b"SBBH", _canonical(sbbh[n], renewed, order))
                for uid in (sub.vertex_buffer, sub.index_buffer):
                    if uid in done:
                        continue
                    done.add(uid)
                    c = buffers.get(uid)
                    for k in c.children if c is not None else ():
                        if k.tag in HOST_CACHE_TAGS:
                            data = payload(stream, k)
                            put(
                                k.tag.encode(),
                                _canonical(data, renewed, order) if k.tag in HOST_CACHE_HEADERS else data,
                            )
    return h.hexdigest(), list(order)


def _swap_uv1(u: Uv1Field | None, swap) -> Uv1Field | None:
    """A copy of `u` with swap() applied to its material UIDs (triangles and group keys,
    in the same order); the rest (grids, charts) shared."""
    if u is None:
        return None
    out = object.__new__(Uv1Field)
    out.__dict__.update(u.__dict__)
    out.tris = [(*t[:3], swap(t[3]), *t[4:]) for t in u.tris]
    out.groups = {((k[0], k[1], swap(k[2])) if k[0] == "pm" else k): g for k, g in u.groups.items()}
    return out


def _host_compute(stream, full, exterior, interior) -> tuple:
    full_field = host_field(stream, full, 4)
    ext_field = host_field(stream, exterior, 2) if exterior is not None else full_field
    for f, vehc, mask in ((full_field, full, 4), (ext_field, exterior, 2)):
        if vehc is not None:
            f.local = local_materials(stream, vehc)
            f.uv1 = Uv1Field.read(stream, vehc, mask, f.local)
    int_uv1 = None
    if interior is not None:
        int_uv1 = Uv1Field.read(stream, interior, 64, local_materials(stream, interior))
    return full_field, ext_field, int_uv1


def host_setup(stream, full, exterior, interior, cache: Path | bool | None = True) -> tuple:
    """(full field, exterior field, interior Uv1Field) of the host entries: host_field and
    Uv1Field.read (masks 4, 2, 64), from the cache when a car on this host made them
    already (`cache`: True for _host_cache_dir(), a folder, or None for none). The cache
    holds each host's fields zlib-compressed (about 4 MB), one folder per code version."""
    import os
    import pickle
    import zlib

    cache = _host_cache_dir() if cache is True else cache
    if cache is None:
        return _host_compute(stream, full, exterior, interior)
    key, uids = host_key(stream, (full, exterior, interior))
    folder = cache / _host_code().hex()[:12]  # one per code version (_prune_host_cache)
    path = folder / f"{key}.z"
    try:
        stored = pickle.loads(zlib.decompress(path.read_bytes()))
    except (OSError, EOFError, zlib.error, pickle.UnpicklingError):  # none yet, or unreadable: made again below
        stored = None
    if stored is not None:

        def back(m):
            return uids[m[1]] if isinstance(m, tuple) else m

        fields = []
        for f, vehc in ((stored[0], full), (stored[1], exterior)):
            if f is not None:
                f.uv1 = _swap_uv1(f.uv1, back)
                f.local = local_materials(stream, vehc)
            fields.append(f)
        return fields[0], fields[1] if fields[1] is not None else fields[0], _swap_uv1(stored[2], back)
    full_field, ext_field, int_uv1 = _host_compute(stream, full, exterior, interior)
    place = {u: n for n, u in enumerate(uids)}

    def out(m):
        return ("uid", place[m]) if m in place else m

    stored = (
        dataclasses.replace(full_field, uv1=_swap_uv1(full_field.uv1, out), local=set()),
        dataclasses.replace(ext_field, uv1=_swap_uv1(ext_field.uv1, out), local=set())
        if ext_field is not full_field
        else None,
        _swap_uv1(int_uv1, out),
    )
    try:
        if not folder.is_dir():
            folder.mkdir(parents=True, exist_ok=True)
            _prune_host_cache(cache, folder)
        temp = path.with_name(f".{key}.{os.getpid()}.tmp")
        temp.write_bytes(zlib.compress(pickle.dumps(stored, protocol=pickle.HIGHEST_PROTOCOL), 1))
        temp.replace(path)
    except OSError:
        pass  # a read-only or full disk, a file another worker holds: no cache, the same fields
    return full_field, ext_field, int_uv1


HOST_CACHE_KEEP_DAYS = 14  # a code version's folder nobody wrote to for this long goes


def _prune_host_cache(cache: Path, keep: Path) -> None:
    """Remove the folders of other code versions untouched for HOST_CACHE_KEEP_DAYS
    (about 4 MB a host each). A worker still reading one only makes its fields again."""
    import shutil
    import time

    old = time.time() - HOST_CACHE_KEEP_DAYS * 86400
    for d in cache.iterdir():
        try:
            if d.is_dir() and d != keep and d.stat().st_mtime < old:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _set_idle_priority() -> None:
    from dsfforza.build import gentle

    gentle.set_priority(gentle.IDLE)


@dataclass
class DamageOptions:
    hull: bool = False  # also map the root collision hull (PDL) onto the Forza body (port_car --damage hull)
    k: int = K_NEAREST
