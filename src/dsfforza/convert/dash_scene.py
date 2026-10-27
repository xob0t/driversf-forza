"""Forza's digital dash scenes (formats/anark.py): state and an offline renderer.

The scene is a 1280x720 presentation seen by an orthographic camera at z=-600 looking
+z, 1 unit = 1 presentation pixel, y up, origin at the centre. The game draws it into
the render target the cockpit's digital_gauge shader samples; a display shows its part
of it through the subsection's first UV transform (uv_transform[0:4]) applied to UV0
(research/cockpit.md, "Digital displays").
"""

from __future__ import annotations

import itertools
import math
import re
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from dsfforza.formats import anark, xds
from dsfforza.formats.anark import h24

PRES_W, PRES_H = 1280, 720
CURVE_SIGN = -1
UV_SIGN = -1

P = {
    name: h24(name)
    for name in [
        "position.x",
        "position.y",
        "position.z",
        "rotation.x",
        "rotation.y",
        "rotation.z",
        "scale.x",
        "scale.y",
        "scale.z",
        "pivot.x",
        "pivot.y",
        "pivot.z",
        "opacity",
        "diffuse.r",
        "diffuse.g",
        "diffuse.b",
        "diffuse.a",
        "textcolor.r",
        "textcolor.g",
        "textcolor.b",
        "textcolor.a",
        "size",
        "tracking",
        "leading",
        "horzalign",
        "vertalign",
        "font",
        "endtime",
        "positionu",
        "positionv",
    ]
}
HP = {v: k for k, v in P.items()}

# UIContract fields (24-bit name hashes; names cracked where shown, else the bound node).
F = {
    "TEXT_SPEED": 0xF1A7EB,
    "TEXT_ODOMETER": 0x0765FF,
    "TRIP": 0x5CE8CE,
    "METER_TACH": 0x667472,
    "TEXT_TACH": 0x1BB29C,
    "TACH_MAX": 0x8BDC30,
    "METER_GEAR": 0x8EC7D5,
    "TEXT_GEAR": 0x4405FF,
    "GEAR_COUNT": 0xB42F93,
    "METER_FUEL": 0x01B14E,
    "TEXT_FUEL": 0xB6EF78,
    "METER_SPEED": 0x2D5B95,
    "SPEED_MAX": 0x1FE357,
    "TEXT_BOOST": 0x4BD82F,
    "TEXT_CURRENT_LAP_TIME": 0xB74AB7,
    "TEXT_LAST_LAP_TIME": 0xDC8BD8,
    "TEXT_BEST_LAP_TIME": 0xFB9F66,
    "PLACE": 0x446A9B,
    "PLACE_OF": 0x8C0956,
    "TEXT_CURRENT_LAP": 0x2EF57D,
    "TEXT_MAX_LAPS": 0x20D35B,
    "METER_ABS": 0xCD6BF1,
    "METER_STM": 0x1F2EF7,
    "METER_TCS": 0x4CF99F,
    "METER_SHIFT_LIGHTS": 0xEF8E64,
    "TEXT_CLOCK": 0xC92224,
    "METER_OIL_PRESSURE": 0x0C0752,
    "TEXT_OIL_PRESSURE": 0xFE717C,
    "METER_OIL_TEMP": 0x60E6E3,
    "TEXT_OIL_TEMP": 0xDFBB0D,
    "OIL_TEMP_MIN": 0x0C66AF,
    "OIL_TEMP_MAX": 0x27D7C1,
    "METER_WATER_TEMP": 0x3A43C8,
    "TEXT_WATER_TEMP": 0x3DA2F2,
    "WATER_TEMP_MIN": 0x8A9D94,
    "WATER_TEMP_MAX": 0xDAA5A6,
    "TEXT_EXT_TEMP": 0xCD5CEA,
    "METER_TACH_MEMORY": 0x63A72E,
}
BEH = {
    0x20B25330: "PROGRESS_BAR",
    0x00CE884D: "COCKPIT_LIGHT",
    0xAE4BB809: "SHIFT_LIGHTS",
    0x1D469C91: "GEARED_RPM_PROGRESS_BAR",
    0x8BC40DE5: "CAR_PRES_GAUGE_ANIM",
}


def f32(raw: bytes) -> float:
    return struct.unpack(">f", raw)[0]


def u32(raw: bytes) -> int:
    return struct.unpack(">I", raw)[0]


# ------------------------------------------------------------------ assets


def decode_xds(d: bytes) -> np.ndarray:
    """RGBA float image. Textures with a side of 16 px (4 blocks) keep their blocks at an offset
    of 4 blocks inside the tiled surface (packed-mip placement, seen on 16x256, 16x32, 64x16,
    16x16 dash textures); dsfforza's untile reads them as empty, so locate the blocks here."""
    fmt, w, h = xds.parse_header(d)
    fourcc, bs = xds.FORMATS[fmt]
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    payload = d[xds.HEADER_SIZE :]
    count = len(payload) // bs
    n = np.arange(count, dtype=np.int64)
    x, y = xds.tiled_x(n, bw, bs), xds.tiled_y(n, bw, bs)
    raw = np.frombuffer(payload, np.uint8, count * bs).reshape(count, bs)
    nz = raw.any(1)
    ox = oy = 0
    if nz.any():
        ox = 4 if bw == 4 and x[nz].min() >= 4 else 0
        oy = 4 if bh == 4 and y[nz].min() >= 4 else 0
    out = np.zeros((bw * bh, bs), np.uint8)
    xx, yy = x - ox, y - oy
    ok = (xx >= 0) & (yy >= 0) & (xx < bw) & (yy < bh)
    dst = (yy * bw + xx)[ok]
    out[dst] = raw[ok].reshape(-1, bs // 2, 2)[:, :, ::-1].reshape(-1, bs)
    rgba = xds.decode_rgba(out.tobytes(), w, h, fourcc)
    return np.array(rgba, dtype=np.float32).reshape(h, w, 4) / 255.0


class Assets:
    def __init__(self, folder: Path, media: Path):
        self.folder = folder
        self.media = media
        self.tex_cache: dict[str, np.ndarray | None] = {}
        self.font_cache: dict[str, anark.VFont | None] = {}
        self.missing: set[str] = set()

    def texture(self, game_path: str) -> np.ndarray | None:
        key = game_path.lower()
        if key in self.tex_cache:
            return self.tex_cache[key]
        parts = game_path.replace("\\", "/").split("/")
        stem = Path(parts[-1]).stem.lower()
        cands = []
        if "shared/dashboards" in key.replace("\\", "/"):
            sub = "/".join(parts[parts.index(next(p for p in parts if p.lower() == "dashboards")) + 1 :])
            cands += [self.media / "shared" / "dashboards" / Path(sub).with_suffix(".xds")]
        cands += list(self.folder.iterdir())
        cands += list((self.media / "shared" / "dashboards").rglob("*.xds"))
        img = None
        for c in cands:
            if c.is_file() and c.stem.lower() == stem and c.suffix.lower() == ".xds":
                img = decode_xds(c.read_bytes())
                break
        if img is None:
            self.missing.add(game_path)
        self.tex_cache[key] = img
        return img

    def font(self, name: str) -> anark.VFont | None:
        """Scene font name -> <target>_vector_aa.dt through fontmap.xml (longest mapping name
        that prefixes the scene's name: 'dg5_quartz' for 'dg5_quartzms', 'lcd ' for 'lcd bold',
        'horizon_dg1', 'a_endurance' for 'a_endurancepro')."""
        key = name.lower()
        if key not in self.font_cache:
            target = None
            fm = self.media / "fonts" / "fontmap.xml"
            if fm.exists():
                maps = re.findall(
                    r'fontname="([^"]+)"\s+target="([^"]+)"', fm.read_text(encoding="utf-8", errors="replace")
                )
                best = max((m for m in maps if key.startswith(m[0].lower())), key=lambda m: len(m[0]), default=None)
                target = best[1].lower() if best else None
            if target is None:
                m = re.match(r"(dg\d)", key)
                target = m.group(1) if m else None
            p = self.media / "fonts" / f"{target}_vector_aa.dt" if target else None
            self.font_cache[key] = anark.load_font(p.read_bytes(), target) if p and p.exists() else None
            if self.font_cache[key] is None:
                self.missing.add("font:" + name)
        return self.font_cache[key]


# ------------------------------------------------------------------ scene


def find_files(folder: Path):
    """The Dash_*.bsg/.fbf/.bgf triple in a folder (or a stem path inside a multi-dash folder)."""
    if folder.is_dir():
        bsgs = [p for p in folder.iterdir() if p.suffix.lower() == ".bsg"]
        if len(bsgs) != 1:
            raise ValueError(f"{folder}: {len(bsgs)} .bsg files; pass <folder>/<stem>")
        stem = bsgs[0].with_suffix("")
    else:
        stem = folder
    out = {}
    for p in stem.parent.iterdir():
        if p.stem.lower() == stem.name.lower() and p.suffix.lower() in (".bsg", ".fbf", ".bgf"):
            out[p.suffix.lower()] = p
    return stem.parent, out


def track_value(keys, t: float) -> float:
    """Keys: (time ms, value, ?, out-handle, ?, in-handle of the next key). Cubic Bezier in value
    between keys, linear in time; held outside the key range."""
    if t <= keys[0][0]:
        return keys[0][1]
    for k0, k1 in itertools.pairwise(keys):
        if t <= k1[0]:
            span = k1[0] - k0[0]
            s = (t - k0[0]) / span if span > 0 else 1.0
            p0, p1, p2, p3 = k0[1], k0[3], k0[5], k1[1]
            return (1 - s) ** 3 * p0 + 3 * (1 - s) ** 2 * s * p1 + 3 * (1 - s) * s * s * p2 + s**3 * p3
    return keys[-1][1]


class Dash:
    def __init__(self, folder: Path, media: Path):
        base, files = find_files(folder)
        self.name = files[".bsg"].stem
        self.bsg = anark.parse_bsg(files[".bsg"].read_bytes())
        self.fbf = anark.parse_fbf(files[".fbf"].read_bytes())
        self.bgf = anark.parse_bgf(files[".bgf"].read_bytes())
        self.assets = Assets(base, media)
        self.objs = {o.id: o for o in self.fbf["objects"]}
        self.nodes = {n.id: n for n in self.bsg["nodes"]}
        self.E = self.bgf["elems"]
        self.S = self.bgf["strings"]
        self.children: dict[int, list[int]] = {}
        for e in self.E:
            if e.parent >= 0:
                self.children.setdefault(e.parent, []).append(e.index)
        self.slides: dict[int, list[dict]] = {}
        for s in self.bgf["slides"]:
            self.slides.setdefault(s["comp"], []).append(s)
        self.contract = {}
        self.behaviours = {}
        for e in self.E:
            if e.type == 8:
                if (e.hash & 0xFFFFFF) == 0xFC7A1A:
                    self.contract = {k & 0xFFFFFF: (k >> 24, raw) for k, raw in e.props}
                else:
                    kind = BEH.get(e.hash, f"{e.hash:08x}")
                    self.behaviours[e.parent] = kind
        self.comp_state: dict[int, tuple[int, float]] = {}
        for ci, sl in self.slides.items():
            self.comp_state[ci] = (1 if len(sl) > 1 else 0, 0.0)
        self.text_override: dict[int, str] = {}
        self.log: list[str] = []

    # -- names and paths
    def ename(self, i: int) -> str:
        o = self.objs.get(self.E[i].id)
        return o.name if o else ("Scene" if i == 0 else f"#{i}")

    def resolve(self, path: str, start: int = 0) -> int | None:
        parts = path.split(".")
        if parts[0] in ("this", "Scene"):
            return None
        cur = start  # "parent" of the UIContract behaviour = the Scene
        for p in parts[1:] if parts[0] == "parent" else parts:
            nxt = [c for c in self.children.get(cur, []) if self.ename(c) == p]
            if not nxt:
                return None
            cur = nxt[0]
        return cur

    def field(self, key: str):
        v = self.contract.get(F[key])
        return v

    def bound(self, key: str) -> int | None:
        v = self.field(key)
        if not v:
            return None
        _, raw = v
        idx = u32(raw)
        if idx >= len(self.S):
            return None
        return self.resolve(self.S[idx])

    def number(self, key: str) -> float:
        v = self.field(key)
        if not v:
            return 0.0
        _, raw = v
        x = u32(raw)
        if x < 0x01000000:
            return float(x)
        y = f32(raw)
        return 0.0 if math.isnan(y) else y

    # -- control
    def set_slide(self, comp: int, name: str, frac: float | None = None, t: float | None = None):
        sl = self.slides.get(comp, [])
        for i, s in enumerate(sl):
            if s["name"].upper() == name.upper():
                tt = t if t is not None else (max(0.0, min(1.0, frac)) * s["dur"] if frac is not None else 0.0)
                self.comp_state[comp] = (i, tt)
                return True
        return False

    def progress(self, key: str, frac: float):
        c = self.bound(key)
        if c is None:
            return
        if not self.set_slide(c, "PROGRESS", frac=frac):
            self.log.append(f"{key}: bound to {self.ename(c)} without a PROGRESS slide")
        else:
            i, t = self.comp_state[c]
            self.log.append(
                f"{key} -> {self.ename(c)} PROGRESS t={t:.0f}/{self.slides[c][i]['dur']} ms (fraction {frac:.3f})"
            )

    def text(self, key: str, s: str):
        e = self.bound(key)
        if e is not None:
            self.text_override[e] = s
            self.log.append(f"{key} -> {self.ename(e)} = {s!r}")

    def apply_state(
        self,
        mph=0.0,
        rpm=0.0,
        gear="N",
        fuel=0.75,
        water=190.0,
        lights=False,
        kmh=False,
        tach_max_default=10000.0,
        extra_text: dict | None = None,
    ):
        speed = mph * 1.609344 if kmh else mph
        self.text("TEXT_SPEED", f"{speed:.0f}")
        tmax = self.number("TACH_MAX") or tach_max_default
        self.progress("METER_TACH", rpm / tmax)
        self.progress("METER_TACH_MEMORY", rpm / tmax)
        self.text("TEXT_TACH", f"{rpm:.0f}")
        smax = self.number("SPEED_MAX") or 200.0
        self.progress("METER_SPEED", speed / smax)
        self.text("TEXT_GEAR", str(gear))
        g = self.bound("METER_GEAR")
        if g is not None:
            n = self.number("GEAR_COUNT") or 6
            names = [s["name"] for s in self.slides.get(g, [])]
            self.log.append(f"METER_GEAR -> {self.ename(g)} slides {names} gears={n:g}")
            # Gear-picture gauges (Audi TT/RS6, Bentley, Gallardo...): PROGRESS keys show gear k on
            # [100(k-1), 100k) ms of a 100(n-1) ms slide, so fraction = (gear - 1) / (n - 1).
            # N and R: not in the data (shown as gear 1 here).
            # Two scenes (Reventon, Reventon Roadster) have one slide per gear: R, N, 1..6.
            if self.behaviours.get(g) == "GEARED_RPM_PROGRESS_BAR":
                # Reventon: one slide per gear (R, N, 1..6), each an rpm sweep
                self.set_slide(g, str(gear), frac=rpm / tmax)
                self.log.append(f"METER_GEAR/TACH -> {self.ename(g)} slide {gear} t={self.comp_state[g][1]:.0f}")
            elif not self.set_slide(g, str(gear)):
                gi = int(gear) if str(gear).isdigit() else 1
                self.set_slide(g, "PROGRESS", frac=(gi - 1) / max(1.0, n - 1))
        self.progress("METER_FUEL", fuel)
        self.text("TEXT_FUEL", f"{fuel * 100:.0f}")
        lo, hi = self.number("WATER_TEMP_MIN"), self.number("WATER_TEMP_MAX")
        if hi <= lo:
            lo, hi = 100.0, 260.0
        self.progress("METER_WATER_TEMP", (water - lo) / (hi - lo))
        self.text("TEXT_WATER_TEMP", f"{water:.0f}")
        lo, hi = self.number("OIL_TEMP_MIN"), self.number("OIL_TEMP_MAX")
        if hi <= lo:
            lo, hi = 100.0, 300.0
        self.progress("METER_OIL_TEMP", (water + 10 - lo) / (hi - lo))
        self.progress("METER_OIL_PRESSURE", 0.5)
        defaults = {
            "TEXT_ODOMETER": "1234",
            "TRIP": "56.7",
            "TEXT_CLOCK": "12:00",
            "TEXT_CURRENT_LAP": "1",
            "TEXT_MAX_LAPS": "3",
            "PLACE": "1",
            "PLACE_OF": "8",
            "TEXT_CURRENT_LAP_TIME": "0:00.000",
            "TEXT_LAST_LAP_TIME": "0:00.000",
            "TEXT_BEST_LAP_TIME": "0:00.000",
            "TEXT_OIL_PRESSURE": "50",
            "TEXT_OIL_TEMP": "200",
            "TEXT_EXT_TEMP": "72",
            "TEXT_BOOST": "0.0",
        }
        defaults.update(extra_text or {})
        for k, v in defaults.items():
            self.text(k, v)
        for c, kind in self.behaviours.items():
            if kind == "COCKPIT_LIGHT":
                self.set_slide(c, "ON" if lights else "OFF")
        sl = self.bound("METER_SHIFT_LIGHTS")
        if sl is not None and self.behaviours.get(sl) == "SHIFT_LIGHTS":
            # Guess: the lights cover the top 35 % of the tach range, then flash (FLASH slide).
            frac = (rpm / tmax - 0.60) / 0.35
            if frac >= 1.0 and self.set_slide(sl, "FLASH", t=150.0):
                self.log.append(f"METER_SHIFT_LIGHTS -> {self.ename(sl)} FLASH")
            else:
                self.set_slide(sl, "PROGRESS", frac=frac)
                self.log.append(
                    f"METER_SHIFT_LIGHTS -> {self.ename(sl)} PROGRESS fraction {max(0, min(1, frac)):.2f} (guessed mapping)"
                )

    # -- property evaluation
    def owner(self, i: int) -> int | None:
        p = self.E[i].parent
        while p >= 0:
            if self.E[p].type == 7:
                return p
            p = self.E[p].parent
        return None

    def props(self, i: int) -> tuple[dict, bool]:
        e = self.E[i]
        n = self.nodes.get(e.id)
        v: dict[str, float] = {}
        if n is not None:
            v.update(zip(("position.x", "position.y", "position.z"), n.pos))
            v.update(zip(("rotation.x", "rotation.y", "rotation.z"), n.rot))
            v.update(zip(("scale.x", "scale.y", "scale.z"), n.scale))
            v["opacity"] = n.opacity * 100.0
        for k, raw in e.props:
            nm = HP.get(k & 0xFFFFFF)
            if nm and nm not in ("font", "horzalign", "vertalign", "endtime"):
                v[nm] = f32(raw)
            elif nm in ("horzalign", "vertalign"):
                v[nm] = u32(raw)
        visible = True
        c = self.owner(i)
        if c is not None and c in self.slides:
            sl = self.slides[c]
            si, t = self.comp_state.get(c, (0, 0.0))
            cur = sl[si]
            order = [sl[0]] if si == 0 else [sl[0], cur]
            listed = False
            for s in order:
                for en in s["entries"]:
                    if en["elem"] != i:
                        continue
                    for k, raw in en["props"]:
                        nm = HP.get(k & 0xFFFFFF)
                        if nm and nm != "endtime":
                            v[nm] = f32(raw)
                    if s is cur:
                        listed = en["active"] == 1 or si == 0
                        for on, ti in en["tracks"]:
                            if on:
                                tr = self.bgf["tracks"][ti]
                                nm = HP.get(tr["prop"] & 0xFFFFFF)
                                if nm:
                                    v[nm] = track_value(tr["keys"], t)
            visible = listed
        return v, visible

    def local_matrix(self, v: dict) -> np.ndarray:
        """Row-vector 4x4: v' = ((v - pivot) * S) * R + position (confirmed on the S2000 tach cover)."""
        piv = np.array([v.get("pivot.x", 0.0), v.get("pivot.y", 0.0), v.get("pivot.z", 0.0)])
        sc = np.array([v.get("scale.x", 1.0), v.get("scale.y", 1.0), v.get("scale.z", 1.0)])
        rx, ry, rz = v.get("rotation.x", 0.0), v.get("rotation.y", 0.0), v.get("rotation.z", 0.0)
        pos = np.array([v.get("position.x", 0.0), v.get("position.y", 0.0), v.get("position.z", 0.0)])
        T0 = np.eye(4)
        T0[3, :3] = -piv
        S = np.diag([*sc, 1.0])
        cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
        Rx = np.array([[1, 0, 0, 0], [0, cx, sx, 0], [0, -sx, cx, 0], [0, 0, 0, 1]])
        Ry = np.array([[cy, 0, -sy, 0], [0, 1, 0, 0], [sy, 0, cy, 0], [0, 0, 0, 1]])
        Rz = np.array([[cz, sz, 0, 0], [-sz, cz, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        T1 = np.eye(4)
        T1[3, :3] = pos
        return T0 @ S @ Rx @ Ry @ Rz @ T1

    # -- drawing list
    def drawables(self):
        out = []

        def walk(i, parent_m, parent_op, parent_vis):
            e = self.E[i]
            o = self.objs.get(e.id)
            if e.type in (2, 3, 8):  # camera, light, behaviour
                return
            v, vis = self.props(i)
            m = self.local_matrix(v) @ parent_m
            op = parent_op * max(0.0, min(100.0, v.get("opacity", 100.0))) / 100.0
            vis = parent_vis and vis
            if o is not None and o.type == 4 and e.type == 1 and vis and op > 0:
                mat = next((self.E[c] for c in self.children.get(i, []) if self.E[c].type == 5), None)
                colour = [1.0, 1.0, 1.0, 1.0]
                tex = None
                if mat is not None:
                    mo = self.objs.get(mat.id)
                    if mo is not None:
                        colour = list(mo.props["ambient"])  # Anark "diffuse" (the authored colour)
                    mv, _ = self.props(mat.index)
                    for j, ch in enumerate("rgba"):
                        if f"diffuse.{ch}" in mv:
                            colour[j] = mv[f"diffuse.{ch}"] / 255.0
                    img = next((self.E[c] for c in self.children.get(mat.index, []) if self.E[c].type == 6), None)
                    if img is not None and img.id in self.objs:
                        tex = self.assets.texture(self.objs[img.id].props["path"])
                        iv, _ = self.props(img.index)
                        f = self.objs[img.id].props["f"]  # (?, positionu, positionv, scaleu, scalev, ...)
                        pu, pv = iv.get("positionu", f[1]), iv.get("positionv", f[2])
                        su, sv = iv.get("scaleu", f[3]), iv.get("scalev", f[4])
                        if (pu, pv, su, sv) != (0.0, 0.0, 1.0, 1.0) and tex is not None:
                            tex = ("xform", tex, (pu, pv, su, sv))
                mesh = self.fbf["meshes"]["heads"][o.props["mesh"]]
                if mesh["a"] != 18:
                    out.append(("mesh", m, colour, op, tex, mesh, i, len(out)))
                else:
                    out.append(("quad", m, colour, op, tex, o.name, i, len(out)))
            if o is not None and o.type == 1 and e.type == 4 and vis and op > 0:
                text = self.text_override.get(i, o.props["text"])
                tv = {k: v[k] for k in v}
                tcol = [tv.get(f"textcolor.{c}", 255.0) / 255.0 for c in "rgba"]
                out.append(("text", m, tcol, op, text, o, v, i, len(out)))
            for c in self.children.get(i, []):
                walk(c, m, op, vis)

        walk(0, np.eye(4), 1.0, True)
        # far to near (camera at z=-600 looking +z): larger world z first; stable in tree order
        out.sort(key=lambda d: (-d[1][3, 2], d[-1]))
        return out


# ------------------------------------------------------------------ raster


class Canvas:
    def __init__(self, width: int, ss: int = 2, view=None):
        """`width` pixels for the whole presentation; `view` (x0, x1, y0, y1, in
        presentation pixels from the top left) draws only that part."""
        self.scale = width / PRES_W * ss
        self.ss = ss
        self.view = view or (0, PRES_W, 0, PRES_H)
        vx0, vx1, vy0, vy1 = self.view
        self.ox, self.oy = vx0 * self.scale, vy0 * self.scale
        self.W = int(round((vx1 - vx0) * width / PRES_W)) * ss
        self.H = int(round((vy1 - vy0) * width / PRES_W)) * ss
        self.rgb = np.zeros((self.H, self.W, 3), np.float32)
        self.alpha = np.zeros((self.H, self.W), np.float32)

    def to_px(self, x, y):
        return (x + PRES_W / 2) * self.scale - self.ox, (PRES_H / 2 - y) * self.scale - self.oy

    def blend(self, y0, x0, rgb, a):
        h, w = a.shape
        dst = self.rgb[y0 : y0 + h, x0 : x0 + w]
        dst *= 1 - a[..., None]
        dst += rgb * a[..., None]
        cov = self.alpha[y0 : y0 + h, x0 : x0 + w]
        cov *= 1 - a
        cov += a

    def quad(self, m, colour, op, tex):
        A = m[:2, :2]  # local (x, y) -> world (x, y) rows
        t = m[3, :2]
        corners = np.array([[-50, -50], [50, -50], [50, 50], [-50, 50]], float) @ A + t
        px = np.array([self.to_px(x, y) for x, y in corners])
        x0, y0 = np.floor(px.min(0)).astype(int)
        x1, y1 = np.ceil(px.max(0)).astype(int)
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, self.W), min(y1, self.H)
        if x1 <= x0 or y1 <= y0:
            return
        det = np.linalg.det(A)
        if abs(det) < 1e-9:
            return
        Ai = np.linalg.inv(A)
        gx, gy = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
        wx = (gx + self.ox) / self.scale - PRES_W / 2
        wy = PRES_H / 2 - (gy + self.oy) / self.scale
        loc = (np.stack([wx - t[0], wy - t[1]], -1)) @ Ai
        u = (loc[..., 0] + 50) / 100
        vm = (loc[..., 1] + 50) / 100
        inside = (u >= 0) & (u <= 1) & (vm >= 0) & (vm <= 1)
        if not inside.any():
            return
        col = np.array(colour, np.float32)
        rgb = np.zeros(u.shape + (3,), np.float32)
        a = np.zeros(u.shape, np.float32)
        if tex is not None:  # sampled only where the quad is
            s = tex_sample(tex, u[inside].astype(np.float32), vm[inside].astype(np.float32))
            rgb[inside] = s[..., :3] * col[:3]
            a[inside] = s[..., 3] * col[3] * op
        else:
            rgb[inside] = col[:3]
            a[inside] = col[3] * op
        self.blend(y0, x0, rgb, a)

    def mesh(self, m, colour, op, tex, mesh):
        """A non-rectangle model mesh (FH1 Zagato tach arc, old Celica needle): triangles with
        uv; one side of two-sided meshes (vertex normal z >= 0)."""
        V = mesh["verts"]
        col = np.array(colour, np.float32)
        cover = np.zeros((self.H, self.W), np.float32)
        acc_rgb = np.zeros((self.H, self.W, 3), np.float32)
        for tri in mesh["tris"]:
            if any(V[k][5] < 0 for k in tri):
                continue
            pts = []
            for k in tri:
                wx, wy, _, _ = np.array([V[k][0], V[k][1], V[k][2], 1.0]) @ m
                pts.append(self.to_px(wx, wy))
            pts = np.array(pts)
            x0, y0 = np.floor(pts.min(0)).astype(int)
            x1, y1 = np.ceil(pts.max(0)).astype(int) + 1
            x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, self.W), min(y1, self.H)
            if x1 <= x0 or y1 <= y0:
                continue
            gx, gy = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
            (ax, ay), (bx, by), (cx, cy) = pts
            dd = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(dd) < 1e-9:
                continue
            l1 = ((by - cy) * (gx - cx) + (cx - bx) * (gy - cy)) / dd
            l2 = ((cy - ay) * (gx - cx) + (ax - cx) * (gy - cy)) / dd
            l3 = 1 - l1 - l2
            ins = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
            if not ins.any():
                continue
            u = l1 * V[tri[0]][3] + l2 * V[tri[1]][3] + l3 * V[tri[2]][3]
            v = l1 * V[tri[0]][4] + l2 * V[tri[1]][4] + l3 * V[tri[2]][4]
            if tex is not None:
                smp = tex_sample(tex, u, v)
                rgb, a = smp[..., :3] * col[:3], smp[..., 3] * col[3] * op
            else:
                rgb, a = np.broadcast_to(col[:3], u.shape + (3,)), np.full(u.shape, col[3] * op, np.float32)
            sl = (slice(y0, y1), slice(x0, x1))
            cover[sl] = np.where(ins, a, cover[sl])
            acc_rgb[sl] = np.where(ins[..., None], rgb, acc_rgb[sl])
        ys, xs = np.nonzero(cover)
        if len(ys):
            y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
            self.blend(y0, x0, acc_rgb[y0:y1, x0:x1], cover[y0:y1, x0:x1])

    def triangles(self, tris_px, uv, rgb, a_total, curved):
        """Glyph triangles in pixel space -> coverage mask (inside: u*u - v <= 0, Loop-Blinn style)."""
        if not len(tris_px):
            return
        x0, y0 = np.floor(tris_px.reshape(-1, 2).min(0)).astype(int) - 1
        x1, y1 = np.ceil(tris_px.reshape(-1, 2).max(0)).astype(int) + 1
        x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, self.W), min(y1, self.H)
        if x1 <= x0 or y1 <= y0:
            return
        k = 4  # coverage supersampling
        mask = Image.new("L", ((x1 - x0) * k, (y1 - y0) * k), 0)
        dr = ImageDraw.Draw(mask)
        cov = np.zeros(((y1 - y0) * k, (x1 - x0) * k), np.float32)
        for tri, tuv, cv in zip(tris_px, uv, curved):
            pts = [((x - x0) * k, (y - y0) * k) for x, y in tri]
            if not cv:
                dr.polygon(pts, fill=255)
            else:
                rasterize_curve(cov, np.array(pts), tuv, cv)
        cov = np.maximum(cov, np.asarray(mask, np.float32) / 255.0)
        cov = cov.reshape(y1 - y0, k, x1 - x0, k).mean((1, 3))
        self.blend(y0, x0, np.broadcast_to(np.array(rgb, np.float32), cov.shape + (3,)), cov * a_total)

    def image(self, alpha: bool = False):
        """The canvas, box-filtered down by the supersampling. With `alpha`, RGBA with
        straight (unpremultiplied) colour over a transparent background."""
        img = self.rgb.reshape(self.H // self.ss, self.ss, self.W // self.ss, self.ss, 3).mean((1, 3))
        if not alpha:
            return Image.fromarray((np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8))
        a = self.alpha.reshape(self.H // self.ss, self.ss, self.W // self.ss, self.ss).mean((1, 3))
        rgb = np.where(a[..., None] > 1e-6, img / np.maximum(a[..., None], 1e-6), 0.0)
        out = np.concatenate([np.clip(rgb, 0, 1), np.clip(a, 0, 1)[..., None]], -1)
        return Image.fromarray((out * 255 + 0.5).astype(np.uint8), "RGBA")


def rasterize_curve(cov, pts, tuv, wind=1):
    x0, y0 = np.floor(pts.min(0)).astype(int)
    x1, y1 = np.ceil(pts.max(0)).astype(int) + 1
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(x1, cov.shape[1]), min(y1, cov.shape[0])
    if x1 <= x0 or y1 <= y0:
        return
    gx, gy = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
    (ax, ay), (bx, by), (cx, cy) = pts
    d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(d) < 1e-9:
        return
    l1 = ((by - cy) * (gx - cx) + (cx - bx) * (gy - cy)) / d
    l2 = ((cy - ay) * (gx - cx) + (ax - cx) * (gy - cy)) / d
    l3 = 1 - l1 - l2
    inside = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
    u = l1 * tuv[0, 0] + l2 * tuv[1, 0] + l3 * tuv[2, 0]
    v = l1 * tuv[0, 1] + l2 * tuv[1, 1] + l3 * tuv[2, 1]
    # Curve triangles carry (u, v) = (-1, 1), (0, -1), (1, 1): the curve is v = u^2. Which side is
    # filled follows the triangle's winding (pixel space, y down).
    f = u * u - v
    hit = inside & (f <= 0)  # winding-dependent fill made it worse (see NOTES)
    cov[y0:y1, x0:x1] = np.maximum(cov[y0:y1, x0:x1], hit.astype(np.float32))


def tex_sample(tex, u, v):
    """Mesh uv (v up) -> texture; the image matrix flips v. A tuple carries the image's
    positionu/positionv (only FH1 AST_V12Zagato_12 animates them: its scrolling tach mask)."""
    if isinstance(tex, tuple):
        # fbf image matrix = scale * (uv + position): e.g. (7.5, 0.5025 = 7.5 * 0.067) on the Zagato
        _, img, (pu, pv, su, sv) = tex
        return sample_bilinear(img, su * (u + pu), 1 - sv * (v + pv))
    return sample_bilinear(tex, u, 1 - v)


def sample_bilinear(tex, u, v, clamp=True):
    h, w = tex.shape[:2]
    if not clamp:  # outside 0..1: transparent (clamp-to-border)
        out = sample_bilinear(tex, u, v)
        return np.where(((u >= 0) & (u <= 1) & (v >= 0) & (v <= 1))[..., None], out, 0.0)
    x = np.clip(u, 0, 1) * w - 0.5
    y = np.clip(v, 0, 1) * h - 0.5
    xi = np.floor(x).astype(int)
    yi = np.floor(y).astype(int)
    fx = (x - xi)[..., None]
    fy = (y - yi)[..., None]
    x0, x1 = np.clip(xi, 0, w - 1), np.clip(xi + 1, 0, w - 1)
    y0, y1 = np.clip(yi, 0, h - 1), np.clip(yi + 1, 0, h - 1)
    return (
        tex[y0, x0] * (1 - fx) * (1 - fy)
        + tex[y0, x1] * fx * (1 - fy)
        + tex[y1, x0] * (1 - fx) * fy
        + tex[y1, x1] * fx * fy
    )


def draw_text(canvas: Canvas, dash: Dash, m, tcol, op, text, o, v, only=None):
    """A text element; with `only`, just the characters at those indices (laid out as
    the whole text)."""
    p = o.props
    font = dash.assets.font(p["font"])
    if font is None or not text:
        return
    size = v.get("size", p["size"])
    tracking = v.get("tracking", p["tracking"])
    halign = int(v.get("horzalign", p["halign"]))
    valign = int(v.get("vertalign", p["valign"]))
    glyphs = [font.glyphs.get(ord(c)) for c in text]
    adv = [g.advance * size if g else 0.30 * size for g in glyphs]
    width = sum(adv) + tracking * max(0, len(text) - 1)
    x = {0: 0.0, 1: -width / 2, 2: -width}.get(halign, 0.0)
    # vertical: centre on the digit '8' (glyph mesh extents, right half), a guess
    ref = font.glyphs.get(ord("8")) or next(iter(font.glyphs.values()))
    rv = ref.verts[ref.verts[:, 0] >= 0]
    top, bot = float(rv[:, 1].max()), float(rv[:, 1].min())
    y = {0: -top * size, 1: -(top + bot) / 2 * size, 2: -bot * size}.get(valign, -(top + bot) / 2 * size)
    tris_px, uvs, curved = [], [], []
    for n, (ch, g, a) in enumerate(zip(text, glyphs, adv)):
        if g is not None and ch != " " and (only is None or n in only):  # the space glyph holds a big box, not drawn
            V = g.verts
            for tri in g.tris:
                if V[tri, 0].mean() <= 0:  # left half of each glyph mesh: a mirrored complement, not drawn
                    continue
                pts = []
                for vi in tri:
                    wx, wy, _, _ = np.array([x + V[vi, 0] * size, y + V[vi, 1] * size, 0.0, 1.0]) @ m
                    pts.append(canvas.to_px(wx, wy))
                tris_px.append(pts)
                uvs.append(V[tri, 2:4])
                if np.any(V[tri, 2] != 0):
                    # convex vs concave curve: the winding of (u=-1, u=0, u=+1) in glyph space
                    o = sorted(tri, key=lambda k: V[k, 2])
                    e1, e2 = V[o[1], :2] - V[o[0], :2], V[o[2], :2] - V[o[0], :2]
                    curved.append(1 if e1[0] * e2[1] - e1[1] * e2[0] > 0 else -1)
                else:
                    curved.append(0)
        x += a + tracking
    canvas.triangles(np.array(tris_px), np.array(uvs), tcol[:3], tcol[3] * op, curved)


def render(
    dash: Dash, width: int = 1280, keep=None, only=None, alpha: bool = False, ss: int = 2, view=None
) -> Image.Image:
    """The scene in its current state. `keep(element index)` picks the drawables (all by
    default); `only` maps a text element to the character indices drawn; with `alpha`
    an RGBA image over a transparent background; `view` (Canvas) a part of it."""
    cv = Canvas(width, ss, view)
    for d in dash.drawables():
        if keep is not None and not keep(d[-2]):
            continue
        if d[0] == "quad":
            cv.quad(d[1], d[2], d[3], d[4])
        elif d[0] == "mesh":
            cv.mesh(d[1], d[2], d[3], d[4], d[5])
        else:
            _, m, tcol, op, text, o, v, i, _ = d
            draw_text(cv, dash, m, tcol, op, text, o, v, (only or {}).get(i))
    return cv.image(alpha)


def crop_t1(img: Image.Image, t1, out_w: int = 512, aspect: float | None = None) -> Image.Image:
    """The part of the render target a display shows: UV0 in 0..1 -> RT uv = uv0 * scale + offset."""
    xo, xs, yo, ys = t1
    W, H = img.size
    box = (xo * W, yo * H, (xo + xs) * W, (yo + ys) * H)
    if aspect is None:
        aspect = (xs * W) / (ys * H)
    return img.crop(tuple(int(round(b)) for b in box)).resize(
        (out_w, max(1, int(round(out_w / aspect)))), Image.LANCZOS
    )
