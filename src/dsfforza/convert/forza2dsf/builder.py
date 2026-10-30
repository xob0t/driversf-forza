"""Builder: the new submodels, buffers and materials of a DSF model package."""

from __future__ import annotations

from dataclasses import dataclass, field

from dsfforza.convert.forza2dsf.materials import (
    CHROME,
    CHROME_LOD1,
    GLASS,
    GLASS_LOD1,
    MATERIAL_LAYOUT,
    PAINT,
    PAINT_LOD1,
    TRIM,
    TRIM_LOD1,
    TYRE,
    TYRE_LOD1,
)

# --- building a DSF model package ---------------------------------------------


@dataclass
class NewSub:
    uid: bytes
    material: bytes
    stride: int
    base_vertex: int
    first_index: int
    index_count: int
    flag: int


@dataclass
class Builder:
    uid_prefix: bytes
    counter: int = 0
    buffers: dict[int, bytearray] = field(default_factory=dict)  # stride -> vertex bytes
    indices: list[int] = field(default_factory=list)
    subs: list[NewSub] = field(default_factory=list)
    # material UID -> (vertex stride, MODL flag): MATERIAL_LAYOUT plus this conversion's
    # host materials (convert shares one dict between its builders).
    layout: dict[bytes, tuple[int, int]] = field(default_factory=lambda: dict(MATERIAL_LAYOUT))

    def new_uid(self) -> bytes:
        """A vanilla-looking resource UID: [counter<<4] 0x12 [counter>>4] prefix[3]
        then byte 7 = 0x2D.

        The engine stores a resolved resource as a pointer in the same 8-byte
        slot and marks it by setting the top bit of byte 7 (research/upvh.md),
        so byte 7 must keep that bit clear. Our first scheme put a counter in
        byte 7 and crashed once it reached 0x80; a scheme with byte 7 = 0x2E
        and arbitrary bytes 0..6 crashed too. This layout, copied from vanilla
        UIDs (byte 1 = 0x12, byte 7 = 0x2D), loads with 130+ new submodels
        (runs 20261022-004457 and -004637). Unique for 4095 resources per
        builder; different builders must use different prefixes."""
        self.counter += 1
        if self.counter > 0xFFF:
            raise ValueError("too many resources for one UID prefix")
        p = self.uid_prefix
        return bytes([(self.counter << 4) & 0xFF, 0x12, self.counter >> 4, p[0], p[1], p[2], p[3] & 0x7F, 0x2D])

    def add(self, material: bytes, verts: list[bytes], tris: list[tuple[int, int, int]]) -> NewSub:
        stride, flag = self.layout[material]
        buf = self.buffers.setdefault(stride, bytearray())
        base = len(buf) // stride
        for v in verts:
            buf += v
        first = len(self.indices)
        for t in tris:
            self.indices.extend(t)
        sub = NewSub(self.new_uid(), material, stride, base, first, len(tris) * 3, flag)
        self.subs.append(sub)
        return sub


SHARED = {
    "paint": PAINT,
    "glass": GLASS,
    "trim": TRIM,
    "tyre": TYRE,
    "chrome": CHROME,
    "spoke_disc": bytes.fromhex("001262258aa7c428"),  # SPOKE_DISC
    "skip": None,
}
SHARED_EXTERIOR = {
    "paint": PAINT_LOD1,
    "glass": GLASS_LOD1,
    "trim": TRIM_LOD1,
    "tyre": TYRE_LOD1,
    "chrome": CHROME_LOD1,
    "spoke_disc": bytes.fromhex("10122b9e53b14e2a"),  # SPOKE_DISC_LOD1
    "skip": None,
}
ROLE_OF = {PAINT: "paint", GLASS: "glass", TRIM: "trim", TYRE: "tyre", CHROME: "chrome"}
