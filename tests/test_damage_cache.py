"""damage.host_setup's cache across cars: a car whose host entries differ from another
car's only by the UIDs renew_uids gave them gets, through the cache, exactly the fields
it would make itself (materials put back to its own UIDs, every dict in the same order);
a corrupt file is made again; old code versions' folders are pruned. No game files: the
host's fields are random (as in tests/test_speed_damage.py) and host_key is faked.

uv run python tests/test_damage_cache.py     # or: uv run python -m tests damage_cache
"""

from __future__ import annotations

import os
import random
import tempfile
import time
from pathlib import Path
from unittest import mock

from dsfforza.convert import damage
from dsfforza.convert.damage import host_cache

PAIRS = [(0, 0), (1, 2), (3, 4), (5, 6)]


def _host_field(rng: random.Random, points: int) -> damage.HostField:
    pts, disp, pairs = [], [], []
    for _ in range(points):
        p = tuple(rng.gauss(0, 0.4) for _ in range(3))
        pts.append(p)
        disp.append(tuple(rng.gauss(0, 0.05) for _ in range(3)))
        pairs.append(rng.choice(PAIRS))
    f = damage.HostField(pts, disp, pairs, damage.Box.of(pts))
    groups = {}
    for n, pair in enumerate(pairs):
        groups.setdefault(pair, []).append(n)
    for pair, ns in groups.items():
        f.by_pair[pair] = damage.Grid([f.normalise(pts[n]) for n in ns], ns)
    f.all_grid = damage.Grid([f.normalise(p) for p in pts], list(range(len(pts))))
    f.tris = [((pts[0], pts[1], pts[2]), PAIRS[1], (0.0, 0.0, 1.0), True)]
    return f


def _uv1_tris(rng: random.Random, materials: list) -> list:
    tris = []
    for _ in range(rng.choice([5, 80, 300])):
        a = tuple(rng.uniform(-1, 1) for _ in range(3))
        corners = (a, *(tuple(a[i] + rng.uniform(-0.05, 0.05) for i in range(3)) for _ in range(2)))
        uvs = tuple((rng.random(), rng.random()) for _ in range(3))
        m = rng.randrange(len(materials))
        tris.append((corners, uvs, rng.choice(PAIRS), m, m < 2, (0.0, 0.0, 1.0)))
    return tris


def _car(rng_seed: int, uids: list, shared: bytes, separate_exterior: bool):
    """One car's host fields, materials named by `uids` (the entry's own, renewed per
    car) and `shared` (an engine material, the same for every car)."""
    rng = random.Random(rng_seed)
    materials = [*uids[:3], shared]
    box = damage.Box((-1, -1, -1), (1, 1, 1))
    full = _host_field(rng, 300)
    full.uv1 = damage.Uv1Field([(*t[:3], materials[t[3]], *t[4:]) for t in _uv1_tris(rng, materials)], box)
    ext = full
    if separate_exterior:
        ext = _host_field(rng, 60)
        ext.uv1 = damage.Uv1Field([(*t[:3], materials[t[3]], *t[4:]) for t in _uv1_tris(rng, materials)], box)
    interior = damage.Uv1Field([(*t[:3], materials[t[3]], *t[4:]) for t in _uv1_tris(rng, materials)], box)
    for f in {id(full): full, id(ext): ext}.values():
        f.local = set(uids[:3])
    return full, ext, interior


def _state(fields) -> list:
    def grid(g):
        return repr((g.cell, g.points, g.values, list(g.cells.items())))

    def uv1(u):
        return (
            "None"
            if u is None
            else repr((u.box, u.tris, u.chart)) + "".join(f"{k!r}{grid(g)}" for k, g in u.groups.items())
        )

    def field(f):
        return (
            repr((f.points, f.disp, f.pairs, f.box, f.tris, sorted(f.local)))
            + grid(f.all_grid)
            + "".join(f"{k!r}{grid(g)}" for k, g in f.by_pair.items())
            + uv1(f.uv1)
        )

    full, ext, interior = fields
    return [field(full), field(ext), uv1(interior), full is ext]


def _run(folder: Path, seed: int, uids: list, separate_exterior: bool):
    """host_setup for one car with host_key, _host_compute and local_materials faked."""
    fresh = _car(seed, uids, b"shared01", separate_exterior)
    with (
        mock.patch.object(host_cache, "host_key", return_value=(f"key{seed}{separate_exterior}", list(uids))),
        mock.patch.object(host_cache, "_host_compute", return_value=fresh),
        mock.patch.object(host_cache, "local_materials", return_value=set(uids[:3])),
    ):
        got = damage.host_setup(None, object(), object() if separate_exterior else None, object(), folder)
    return got, _car(seed, uids, b"shared01", separate_exterior)


def test_a_cached_host_is_the_cars_own():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for separate in (True, False):
            car_a = [bytes([n, 0x12, 1, 2, 3, 4, 5, 0x2D]) for n in range(1, 6)]
            car_b = [bytes([n, 0x12, 9, 9, 9, 9, 9, 0x2D]) for n in range(1, 6)]
            got_a, want_a = _run(folder, 7, car_a, separate)  # made, stored
            assert _state(got_a) == _state(want_a)
            files = list(folder.rglob("*.z"))
            assert files and not list(folder.rglob("*.tmp"))
            got_b, want_b = _run(folder, 7, car_b, separate)  # another car on the host: loaded
            assert _state(got_b) == _state(want_b)
            assert _state(got_b) != _state(want_a)  # its own UIDs, not the first car's
            got_b2, _ = _run(folder, 7, car_b, separate)
            assert _state(got_b2) == _state(want_b)


def test_an_unreadable_file_is_made_again():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        uids = [bytes([n, 0x12, 1, 2, 3, 4, 5, 0x2D]) for n in range(1, 6)]
        _run(folder, 3, uids, True)
        for f in folder.rglob("*.z"):
            f.write_bytes(b"not a cache file")
        got, want = _run(folder, 3, uids, True)
        assert _state(got) == _state(want)


def test_old_code_versions_are_pruned():
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        old, recent, keep = cache / "aaaaaaaaaaaa", cache / "bbbbbbbbbbbb", cache / "cccccccccccc"
        for d in (old, recent, keep):
            d.mkdir()
            (d / "x.z").write_bytes(b"x")
        then = time.time() - (damage.HOST_CACHE_KEEP_DAYS + 1) * 86400
        os.utime(old, (then, then))
        damage._prune_host_cache(cache, keep)
        assert not old.exists() and recent.exists() and keep.exists()


def test_uids_are_numbered_by_first_appearance():
    renewed = {b"AAAAAAAA", b"BBBBBBBB"}
    order: dict = {}
    out = damage._canonical(b"xxBBBBBBBByyAAAAAAAAzzBBBBBBBB", renewed, order)
    assert list(order) == [b"BBBBBBBB", b"AAAAAAAA"]
    assert out == b"xx\xffUID\0\0\0\0yy\xffUID\1\0\0\0zz\xffUID\0\0\0\0"
    assert damage._canonical(b"short", renewed, {}) == b"short"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
