"""dds_file with `reuse` (a texture encoded again with a few texels changed: only the changed
4 x 4 blocks are encoded) gives the same bytes as encoding it from scratch, and the vectorised
DXT5 encoder gives the bytes of the per-block reference below.

uv run python tests/test_speed_dds.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import struct

import numpy as np

from dsfforza.convert import dsf_materials as dm

# --- the per-block DXT5 reference that dsf_materials._colour_blocks/_alpha_blocks vectorise ---


def _q565(r: int, g: int, b: int) -> int:
    return ((r * 31 + 127) // 255) << 11 | ((g * 63 + 127) // 255) << 5 | ((b * 31 + 127) // 255)


def _x565(c: int) -> tuple[int, int, int]:
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def _colour_block(px: list[tuple[int, int, int]]) -> bytes:
    lo = [min(p[c] for p in px) for c in range(3)]
    hi = [max(p[c] for p in px) for c in range(3)]
    # Pick the bounding-box diagonal that follows the colours' correlation.
    axis = max(range(3), key=lambda c: hi[c] - lo[c])
    mean = [sum(p[c] for p in px) / 16 for c in range(3)]
    e0, e1 = list(hi), list(lo)
    for c in range(3):
        if c != axis and sum((p[axis] - mean[axis]) * (p[c] - mean[c]) for p in px) < 0:
            e0[c], e1[c] = lo[c], hi[c]
    inset = [(e0[c] - e1[c]) / 16 for c in range(3)]
    e0 = [max(0, min(255, round(e0[c] - inset[c]))) for c in range(3)]
    e1 = [max(0, min(255, round(e1[c] + inset[c]))) for c in range(3)]
    c0, c1 = _q565(*e0), _q565(*e1)
    if c0 < c1:
        c0, c1 = c1, c0
    if c0 == c1:
        return struct.pack("<HHI", c0, c1, 0)
    p0, p1 = _x565(c0), _x565(c1)
    pal = [p0, p1, tuple((2 * a + b) // 3 for a, b in zip(p0, p1)), tuple((a + 2 * b) // 3 for a, b in zip(p0, p1))]
    bits = 0
    for i, p in enumerate(px):
        best = min(range(4), key=lambda k: (p[0] - pal[k][0]) ** 2 + (p[1] - pal[k][1]) ** 2 + (p[2] - pal[k][2]) ** 2)
        bits |= best << (2 * i)
    return struct.pack("<HHI", c0, c1, bits)


def _alpha_block(alpha: list[int]) -> bytes:
    a0, a1 = max(alpha), min(alpha)
    if a0 == a1:
        return bytes((a0, a1)) + bytes(6)
    pal = [a0, a1] + [((7 - k) * a0 + k * a1) // 7 for k in range(1, 7)]
    bits = 0
    for i, a in enumerate(alpha):
        bits |= min(range(8), key=lambda k: abs(a - pal[k])) << (3 * i)
    return bytes((a0, a1)) + bits.to_bytes(6, "little")


def test_vectorised_blocks_match_the_reference():
    rng = np.random.default_rng(9)
    px = rng.integers(0, 256, (300, 16, 4), dtype=np.int64)
    px[:20] = px[:20, :1]  # flat blocks: one colour, one alpha
    px[20:40, :, :3] = rng.integers(100, 104, (20, 16, 3))  # nearly flat
    colour, alpha = dm._colour_blocks(px[:, :, :3]), dm._alpha_blocks(px[:, :, 3])
    for k, block in enumerate(px.tolist()):
        assert colour[k].tobytes() == _colour_block([tuple(t[:3]) for t in block]), k
        assert alpha[k].tobytes() == _alpha_block([t[3] for t in block]), k


def test_dds_reuse_matches_a_fresh_encoding():
    rng = np.random.default_rng(6)
    for fourcc in (b"DXT5", b"DXT1"):
        for width, height, mips in ((64, 32, 5), (256, 256, 9), (16, 16, 3)):
            image = rng.integers(0, 256, (height, width, 4), dtype=np.uint8)
            image[: height // 2, : width // 2] = (40, 80, 120, 200)  # flat blocks too
            reuse: dict = {}
            for step in range(4):
                if step == 1:
                    image[3:9, 5:7, 3] //= 3  # a few texels: their blocks and mips change
                elif step == 2:
                    pass  # nothing changed
                elif step == 3:
                    image[:, :, 0] = 255 - image[:, :, 0]  # every block
                rgba = bytearray(image.tobytes())
                want = dm.dds_file(rgba, width, height, mips, fourcc=fourcc)
                got = dm.dds_file(rgba, width, height, mips, fourcc=fourcc, reuse=reuse)
                assert got == want, (fourcc, width, height, step)


def test_render_again_matches_a_fresh_render():
    # A second render with the lamp glow changed: the regions kept from the first render give
    # what a render with that glow from the start gives.
    from pathlib import Path

    from dsfforza.convert import forza_materials as fm

    size = 32
    rng = np.random.default_rng(1)
    src = bytearray(rng.integers(0, 256, (size, size, 4), dtype=np.uint8).tobytes())

    class Atl:
        def get(self, family):
            return size, size, src

    def texturing():
        t = dm.Texturing(Path(), size=(64, 32))
        t.families = (fm.LIGHTS, fm.NODAMAGE)
        t.atlases = Atl()
        t.regions = {
            fm.LIGHTS: [dm.Region(fm.LIGHTS, (8, 4, 16, 12), (0, 0), (8 + 2 * dm.PAD, 8 + 2 * dm.PAD))],
            fm.NODAMAGE: [dm.Region(fm.NODAMAGE, (0, 0, 20, 20), (24, 0), (10 + 2 * dm.PAD, 10 + 2 * dm.PAD))],
        }
        t.scale = {fm.LIGHTS: 1.0, fm.NODAMAGE: 0.5}
        return t

    glow = np.full((size, size), np.nan)
    glow[:, 8:12] = 77.0
    again = texturing()
    first = bytes(again.render())
    again._lamp_glow, again._glow_done = glow, True
    fresh = texturing()
    fresh._lamp_glow, fresh._glow_done = glow, True
    assert bytes(again.render()) == bytes(fresh.render()) != first


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
