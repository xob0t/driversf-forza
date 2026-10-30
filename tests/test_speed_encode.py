"""forza2dsf.encode_vertex (spherical() and the clamps written out, the structs made once)
gives exactly the bytes the code before it gave. The reference below is that code, kept
here only to compare against, on random vertices.

uv run python tests/test_speed_encode.py     # or: uv run python -m tests speed_encode
"""

from __future__ import annotations

import random
import struct

import numpy as np

from dsfforza.convert import forza2dsf as f2d


def _encode_reference(stride: int, pos, uv0, uv1, normal, s9: bytes = f2d.RIGID, sky: float = 0.0) -> bytes:
    """The original encode_vertex."""
    q = [max(-32768, min(32767, round(c * f2d.POSITION_SCALE))) for c in pos]
    p0, p1 = f2d.spherical(normal)
    head = struct.pack("<4h", q[0], q[1], q[2], max(0, min(4096, round(sky * 4096))))
    s5 = bytes((p0, p1, 128, 128))
    if stride == 32:
        return head + struct.pack("<4e", uv0[0], uv0[1], uv1[0], uv1[1]) + s9 + s5 + struct.pack("<4e", *pos, 1.0)
    if stride == 28:
        return head + struct.pack("<2e", uv0[0], uv0[1]) + s9 + s5 + struct.pack("<4e", *pos, 1.0)
    if stride == 20:
        return head + struct.pack("<2e", uv0[0], uv0[1]) + s9 + s5
    raise ValueError(stride)


def _value(rng: random.Random) -> float:
    return rng.choice(
        [
            rng.uniform(-3, 3),
            rng.uniform(-9, 9),  # past the int16 range at 4096 a metre: clamped
            0.0,
            -0.0,
            rng.randint(-24, 24) / 8192,  # halfway between two steps (round half to even)
            1.0,
            -1.0,
        ]
    )


def test_encode_vertex_matches_the_original():
    rng = random.Random(13)
    for n in range(60000):
        pos = tuple(_value(rng) for _ in range(3))
        normal = tuple(rng.choice([rng.gauss(0, 1), 0.0, -0.0, 1.0, -1.0, 1e-300]) for _ in range(3))
        uv0, uv1 = (rng.random(), rng.uniform(-2, 2)), (rng.random(), rng.random())
        sky = rng.choice([0.0, rng.random(), 1.3, -0.2, 0.5 / 4096])
        s9 = bytes(rng.randrange(256) for _ in range(4))
        if n % 7 == 0:  # numpy scalars, as some callers pass
            pos, normal = tuple(np.float64(c) for c in pos), tuple(np.float32(c) for c in normal)
        for stride in (20, 28, 32):
            assert f2d.encode_vertex(stride, pos, uv0, uv1, normal, s9, sky) == _encode_reference(
                stride, pos, uv0, uv1, normal, s9, sky
            )


def test_encode_vertex_fails_alike():
    for args in (
        (24, (0.0, 0.0, 0.0), (0, 0), (0, 0), (0.0, 1.0, 0.0)),  # no such stride
        (32, (float("nan"), 0.0, 0.0), (0, 0), (0, 0), (0.0, 1.0, 0.0)),
        (32, (float("inf"), 0.0, 0.0), (0, 0), (0, 0), (0.0, 1.0, 0.0)),
        (32, (0.0, 0.0, 0.0), (1e6, 0), (0, 0), (0.0, 1.0, 0.0)),  # past the half-float range
    ):
        errors = []
        for fn in (f2d.encode_vertex, _encode_reference):
            try:
                fn(*args)
                errors.append(None)
            except Exception as error:  # the same kind of error both ways
                errors.append(type(error))
        assert errors[0] == errors[1] and errors[0] is not None, (args, errors)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
