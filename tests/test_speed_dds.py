"""dds_file with `reuse` (a texture encoded again with a few texels changed: only the changed
4 x 4 blocks are encoded) gives the same bytes as encoding it from scratch.

uv run python tests/test_speed_dds.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import numpy as np

from dsfforza.convert import dsf_materials as dm


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
    # set_daytime renders the atlas again with the day-lit texels set: the regions kept from the
    # first render give what a render with them set from the start gives.
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

    day = np.zeros((size, size), bool)
    day[:, 8:12] = True
    again = texturing()
    first = bytes(again.render())
    again.daytime_texels, again._glow_done = day, False
    fresh = texturing()
    fresh.daytime_texels = day
    assert bytes(again.render()) == bytes(fresh.render()) != first


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
