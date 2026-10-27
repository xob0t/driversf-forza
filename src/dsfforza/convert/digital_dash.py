"""Live digital displays in the cockpit view, through DSF's own digital dials.

Driver: San Francisco has a live digital-dashboard path of its own, used by 14
stock interiors (Volt, LP670, Alfa 8C, ...; research/cockpit.md, "Digital
displays"): materials `*_Digital_Dials.matxml` whose vertex shader adds
`g_fDigiData[(id - 1) * 4 + comp]` to the vertex's v texture coordinate. The
vertex format is the needles' (DashboardInstrument (u, v, id, comp)), and the
game fills, every frame (FUN_00891670):

    id 1, comp 0: rpm / redline x 0.5
    id 2, comp 0..3: mph digits, units to thousands: (digit + 1) x 23/256,
                     0 for a leading blank
    id 6, comp 0: gear / 7 (0 = reverse)
    id 7, comp 0: 0.5 when the gear isn't reverse
    ids 3..5: never written (0)

So a digit is a quad over the blank cell of a strip of 11 cells (blank, 0..9,
23/256 apart), a gear a quad over the first of 7 cells, and an rpm bar segment
a quad whose single v samples a column that is transparent below a threshold
and lit above it (alpha-tested away when transparent). Static artwork uses id
3 (no offset). Everything samples one texture per car, built here.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# The stock Volt's digital material (entry 148, Chevrolet_Volt_2011_Digital_Dials.matxml):
# alpha test 0.5, g_texDiffuseMap b012ecfc52fb762a. Its submodel sits in the
# interior's needle part with MODL flag 1, in a needle-layout buffer.
STOCK_MATERIAL = bytes.fromhex("e012cc80f0aa8829")
STOCK_TEXTURE = bytes.fromhex("b012ecfc52fb762a")
MODL_FLAG = 1

RPM = (1, 0)  # (id, comp): rpm / redline x 0.5
SPEED_DIGITS = [(2, k) for k in range(4)]  # units, tens, hundreds, thousands
GEAR = (6, 0)  # gear / 7, 0 = reverse
STATIC = (3, 0)  # never written: no offset
DIGIT_PITCH = 23 / 256  # v between digit cells (blank, 0..9)
GEAR_PITCH = 1 / 7  # v between gear cells (R, 1..6)
BAR_LIT_FROM = 0.505  # bar columns: lit from this v up, transparent below
BAR_MAX = 0.49  # largest threshold (rpm at the redline gives 0.5)


@dataclass
class Quad:
    corners: list  # 4 Forza-space points, in order round the quad
    uvs: list  # 4 (u, v) texture coordinates
    slot: tuple  # (id, comp)


@dataclass
class DigitalPlan:
    """What a car's digital displays add to its interior: one texture and quads."""

    width: int
    height: int
    rgba: bytearray
    quads: list = field(default_factory=list)
    report: dict = field(default_factory=dict)

    def blit(self, x: int, y: int, image) -> None:
        """Paste an RGBA PIL image at texel (x, y) (top left)."""
        w, h = image.size
        data = image.tobytes()
        for row in range(h):
            o = ((y + row) * self.width + x) * 4
            self.rgba[o : o + w * 4] = data[row * w * 4 : (row + 1) * w * 4]

    def fill(self, x0: int, y0: int, x1: int, y1: int, colour) -> None:
        px = bytes(colour)
        for row in range(y0, y1):
            o = (row * self.width + x0) * 4
            self.rgba[o : o + (x1 - x0) * 4] = px * (x1 - x0)


def rgba_dds(width: int, height: int, rgba: bytes | bytearray) -> bytes:
    """An uncompressed single-mip DDS (A8R8G8B8: BGRA bytes) of RGBA texels.
    DSF's DXTC loader maps 32-bit RGB DDS to D3DFMT_A8R8G8B8 and never drops
    the top mip of a single-mip texture."""
    head = struct.pack(
        "<4s7I44s2I4s5I5I",
        b"DDS ",
        124,
        0x100F,  # caps, height, width, pitch, pixel format
        height,
        width,
        width * 4,
        0,
        1,
        bytes(44),
        32,
        0x41,  # RGB | alpha
        b"\0\0\0\0",
        32,
        0x00FF0000,
        0x0000FF00,
        0x000000FF,
        0xFF000000,
        0x1000,  # texture
        0,
        0,
        0,
        0,
    )
    bgra = bytearray(rgba)
    bgra[0::4], bgra[2::4] = rgba[2::4], rgba[0::4]
    return head + bytes(bgra)


def dxt5_dds(width: int, height: int, rgba: bytes | bytearray, template: bytes | None = None) -> bytes:
    """A single-mip DXT5 DDS (the stock digital textures are DXT5/DXT1; a single
    mip is never dropped by the texture-quality setting and keeps the cells and
    bar columns from bleeding into each other)."""
    from dsfforza.convert import dsf_materials as dm

    return dm.dds_file(rgba, width, height, 1, template)


_STOCK: dict = {}


def stock_material(stream):
    """The stock Volt digital MACO (Entry) and its TEXC, from the source archive."""
    key = id(stream)
    if key not in _STOCK:
        from dsfforza.convert import dsf_materials as dm
        from dsfforza.formats import chnk

        root = chnk.read_chunk(stream, 0)
        found = None
        for e in root.entries:
            for _, c in chnk.walk([e]):
                if c.tag == "MACO" and c.is_chunk and dm.material_info(stream, c)["uid"] == STOCK_MATERIAL:
                    found = c
                    break
            if found is not None:
                break
        if found is None:
            raise ValueError("stock digital material e012cc80f0aa8829 (Volt) not in the source archive")
        _STOCK[key] = found
    return _STOCK[key]


def vertices(plan: DigitalPlan, place, stride: int) -> tuple[list[bytes], list[tuple[int, int, int]]]:
    """Needle-layout vertices and double-sided triangles for the plan's quads."""
    from dsfforza.convert import cockpit

    verts, tris = [], []
    for q in plan.quads:
        a, b, c = q.corners[0], q.corners[1], q.corners[2]
        n = cockpit._norm(cockpit._cross(cockpit._sub(b, a), cockpit._sub(c, a)))
        base = len(verts)
        for p, uv in zip(q.corners, q.uvs):
            verts.append(cockpit._needle_vertex(place.point(p), uv, q.slot[0], place.normal(n), stride, q.slot[1]))
        tris += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
        tris += [(base, base + 2, base + 1), (base, base + 3, base + 2)]
    return verts, tris


def prototype_plan(display, version: int = 3) -> DigitalPlan:
    """In-game test of the mechanism on one display (cockpit.Display): a static
    background (id 3), three speed digits, a gear and a ten-segment rpm bar,
    drawn with a stock font. Not Forza's artwork."""
    from PIL import Image, ImageDraw, ImageFont

    W, H = 256, 512
    plan = DigitalPlan(W, H, bytearray(W * H * 4))
    font = ImageFont.load_default(size=36)

    def cell(text, w, h, colour=(255, 200, 40, 255)):
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        if text:
            box = d.textbbox((0, 0), text, font=font)
            d.text(((w - (box[2] - box[0])) / 2 - box[0], (h - (box[3] - box[1])) / 2 - box[1]), text, font=font, fill=colour)
        return im

    # Digits: column x 0..40, cells of DIGIT_PITCH * H texels (blank, 0..9).
    pitch = DIGIT_PITCH * H
    for k, t in enumerate([""] + [str(i) for i in range(10)]):
        plan.blit(0, round(k * pitch), cell(t, 40, int(pitch) - 2))
    # Gear: column x 48..88, cells R, 1..6.
    gp = GEAR_PITCH * H
    for k, t in enumerate("R123456"):
        plan.blit(48, round(k * gp), cell(t, 40, int(gp) - 2, (120, 230, 255, 255)))
    # Bar: column x 96..104 lit from BAR_LIT_FROM.
    plan.fill(96, round(BAR_LIT_FROM * H), 104, H, (60, 255, 60, 255))
    # Background: x 128..256, a flat dark blue.
    plan.fill(128, 0, 256, 64, (20, 40, 70, 255))

    u0, u1, v0, v1 = display.rect
    lift = 0.0015

    def rect(x0, x1, y0, y1, uvs, slot, dz):
        corners = [display.point(x, y, dz) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        plan.quads.append(Quad(corners, uvs, slot))

    def tex_rect(x0, y0, x1, y1):  # texel box -> uv corners (bottom-left, bottom-right, top-right, top-left)
        return [(x0 / W, y1 / H), (x1 / W, y1 / H), (x1 / W, y0 / H), (x0 / W, y0 / H)]

    rect(u0, u1, v0, v1, tex_rect(130, 2, 254, 62), STATIC, lift)
    w, h = u1 - u0, v1 - v0
    dh = 0.45 * h
    dw = dh * 40 / (pitch - 2)
    for j in range(3):  # units on the right
        x1 = u1 - 0.06 * w - j * dw
        rect(x1 - dw, x1, v0 + 0.1 * h, v0 + 0.1 * h + dh, tex_rect(0, 0, 40, pitch - 2), SPEED_DIGITS[j], 2 * lift)
    gx = u0 + 0.06 * w
    rect(gx, gx + dw, v0 + 0.1 * h, v0 + 0.1 * h + dh, tex_rect(48, 0, 88, gp - 2), GEAR, 2 * lift)
    seg = 0.8 * w / 10
    for k in range(10):
        t = min(BAR_MAX, 0.5 * (k + 1) / 10)
        vb = (BAR_LIT_FROM - t) * H
        x0 = u0 + 0.1 * w + k * seg
        rect(x0, x0 + 0.8 * seg, v1 - 0.25 * h, v1 - 0.08 * h, [(100 / W, vb / H)] * 4, RPM, 2 * lift)
    plan.report = {"prototype": True, "display": display.section}
    return plan
