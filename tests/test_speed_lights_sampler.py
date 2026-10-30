"""lights.texel_sampler (each subsection's texels from one lookup) gives what the original
gave triangle by triangle, the mean and the seen class, on random lamp subsections.

uv run python tests/test_speed_lights_sampler.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import numpy as np

from dsfforza.convert import dsf_materials, lights, trim_art
from dsfforza.convert import forza_materials as fm
from dsfforza.formats import carbin

SIZE = 64


class _Atlases:
    def __init__(self, rng):
        self.lights = (SIZE, SIZE, bytearray(rng.integers(0, 256, (SIZE, SIZE, 4), dtype=np.uint8).tobytes()))

    def get(self, family):
        assert family == fm.LIGHTS
        return self.lights


def _sampler_reference(atlases, reduce=None):
    """The original texel_sampler: every triangle's texels looked up on its own."""
    images, materials = {}, {}

    def sample(section, sub, tri):
        key = id(sub)
        if key not in materials:
            materials[key] = dsf_materials.resolve_lamp(section, sub, fm.classify, atlases)
        m = materials[key]
        if m.family != fm.LIGHTS or m.kind not in ("textured", "swatch"):
            return None
        if fm.LIGHTS not in images:
            w, h, rgba = atlases.get(fm.LIGHTS)
            images[fm.LIGHTS] = np.frombuffer(bytes(rgba), dtype=np.uint8).reshape(h, w, 4)
        image = images[fm.LIGHTS]
        h, w = image.shape[:2]
        pool = section.vertices_for(sub)
        uvs = dsf_materials.triangle_uv(m, [pool[i].uv0 for i in tri])
        texels = trim_art.triangle_texels(image, [(u * w, v * h) for u, v in uvs])
        if not len(texels):
            return None
        if reduce is not None:
            return reduce(texels)
        return tuple(float(c) for c in texels.mean(0))

    return sample


def _section(rng, name, material, n):
    verts = [
        carbin.Vertex(tuple(rng.normal(0, 0.3, 3)), tuple(rng.uniform(-0.2, 1.2, 2)), (0.0, 0.0), (0.0, 0.0, -1.0))
        for _ in range(n)
    ]
    idx = [int(i) for i in rng.integers(0, n, 3 * int(rng.integers(1, 80)))]
    if len(idx) >= 6:
        idx[3:6] = idx[0:3]  # the same triangle twice
    scale = float(rng.choice([1.0, 0.25]))
    sub = carbin.SubSection(material, 1, 4, (0.0, scale, 0.0, scale) * 2, idx)
    return carbin.Section(name, (0, 0, 0), (0, 0, 0), (0, 0, 0), lod_vertices=verts, subsections=[sub])


def test_sampler_matches_one_triangle_at_a_time():
    rng = np.random.default_rng(17)
    asked = 0
    for case in range(20):
        atlases = _Atlases(rng)
        sections = [
            _section(rng, f"light{k}", str(rng.choice(["head_light", "lights_glass", "tail_light", "misc"])), 12)
            for k in range(6)
        ]
        # the mean, the seen class (seen_sampler) and a reduce of the texels themselves
        for got, want in (
            (lights.texel_sampler(atlases), _sampler_reference(atlases)),
            (lights.seen_sampler(atlases), _sampler_reference(atlases, lights._seen)),
            (
                lights.texel_sampler(atlases, lambda t: int(t[:, 3].max())),
                _sampler_reference(atlases, lambda t: int(t[:, 3].max())),
            ),
        ):
            for section in sections:
                sub = section.subsections[0]
                tris = sub.triangles()
                for k in rng.permutation(len(tris)):
                    assert got(section, sub, tris[k]) == want(section, sub, tris[k]), case
                    asked += 1
                other = [int(i) for i in rng.integers(0, 12, 3)]  # not one of its triangles
                assert got(section, sub, other) == want(section, sub, other)
    assert asked > 1000


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
