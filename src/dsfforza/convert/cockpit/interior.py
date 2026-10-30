"""The interior entry (N+1000) built from the Forza cockpit: materials, needles and
convert_interior."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from dsfforza.convert import forza_materials as fm
from dsfforza.convert.cockpit.displays import VIRTUAL_NEEDLE_COLOURS
from dsfforza.convert.cockpit.geometry import (
    DIGITAL_TECHNIQUES,
    DISPLAY_COLOUR,
    FLAT_COCKPIT,
    FLAT_DEFAULT,
    FORCE_FLAT,
    GAUGE_IDS,
    INTERIOR_MASK,
    INTERIOR_PAINT,
    LIGHT_SECTIONS,
    PARK_MID,
    SHELL_SKIP,
    SKIP_SECTIONS,
    VIRTUAL_ONLY_FLAT,
    CockpitFit,
    _add,
    flat_colour,
    rotate_about,
)
from dsfforza.convert.cockpit.visibility import (
    CULL_OFFSETS,
    GlassSight,
    cull_sections,
    decimate_section,
    shell_closes,
    visible_triangles,
)

# --- the interior entry ---------------------------------------------------------------


@dataclass
class InteriorMaterials:
    textured: bytes  # local "Interior" material (samples the interior TEXC)
    needles: bytes | None  # local "Dials" material, stride 20, alpha-tested
    texture: bytes  # interior TEXC UID
    shared: dict  # role -> material UID (paint, glass, trim)
    layout: dict  # material -> (stride, MODL flag), from the host
    needle_part: int | None
    cabin_part: int


def local_material_kind(info: dict) -> str | None:
    """'dials' or 'interior' for an interior MACO (dsf_materials.material_info), by its
    MATX name or alpha test, else None."""
    base = info["path"].lower().replace("\\", "/").rsplit("/", 1)[-1]
    if "_dials" in base or "g_hAlphaTestRefValue" in info["params"]:
        return "dials"
    return "interior" if "_interior" in base else None


def host_interior(stream, interior, known: dict | None = None) -> InteriorMaterials:
    """Read the host interior's materials, strides and MODL flags. known: the
    conversion's material layouts (forza2dsf.Builder.layout; default
    forza2dsf.MATERIAL_LAYOUT), for shared materials the interior doesn't use."""
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.convert import forza2dsf
    from dsfforza.formats.vehicle_mesh import child

    drsp = child(child(interior, "MDXN"), "DRSP")
    local, texc = {}, {}
    for e in drsp.children:
        if e.tag == "MACO":
            info = dm.material_info(stream, e)
            kind = local_material_kind(info)
            if kind and kind not in local:
                local[kind] = info
        elif e.tag == "TEXC":
            texc[dm.current(stream, child(e, "UIDC"))[:8]] = e
    if "interior" not in local:
        raise ValueError("host interior has no _Interior material")
    # Strides and flags per material, and which part holds the needles.
    subs, strides = {}, {}
    for e in drsp.children:
        if e.tag == "SBMC":
            d = dm.current(stream, child(e, "SBBH"))
            subs[d[:8]] = (d[8:16], d[16:24], struct.unpack_from("<I", d, 0x3C)[0])
        elif e.tag == "VXCK":
            d = dm.current(stream, child(e, "VXHD"))
            strides[d[:8]] = struct.unpack_from("<I", d, 12)[0]
    modl = dm.current(stream, child(child(interior, "NONR"), "MODL"))
    parts = struct.unpack_from("<I", modl)[0]
    layout, sizes, needle_part, cabin_part = {}, {}, None, 0
    for p in range(parts):
        count, offset = struct.unpack_from("<II", modl, 0x10 + p * 0x60 + 0x48)
        for k in range(count):
            _mask, n, uid_off, extra_off = struct.unpack_from("<4I", modl, offset + k * 16)
            for i in range(n):
                uid = modl[uid_off + i * 8 : uid_off + i * 8 + 8]
                flag = struct.unpack_from("<I", modl, extra_off + i * 4)[0]
                mat, vb, indices = subs[uid]
                layout.setdefault(mat, (strides[vb], flag))
                sizes[mat] = sizes.get(mat, 0) + indices
                if local.get("dials") and mat == local["dials"]["uid"]:
                    needle_part = p
    shared_used = [m for m in layout if m not in {i["uid"] for i in local.values()}]

    def pick(stride: int, flag: int | None, fallback: bytes) -> bytes:
        found = [m for m in shared_used if layout[m][0] == stride and (flag is None or layout[m][1] == flag)]
        return max(found, key=lambda m: sizes[m]) if found else fallback

    glass = pick(28, 7, forza2dsf.GLASS)
    # Every vanilla interior paints with INTERIOR_PAINT; picking the largest
    # stride-32 material instead took GLOBAL_LOD_Chrome (a012f01306d57a2a) on
    # hosts that use more chrome than paint (193, the Mustang's host).
    paint = INTERIOR_PAINT if INTERIOR_PAINT in shared_used else pick(32, 5, forza2dsf.PAINT)
    # Without the engine trim the largest stride-28 material can be a chassis
    # one (193: Chassis_LOD1); keep the engine trim, MATERIAL_LAYOUT has its layout.
    trim = forza2dsf.TRIM
    # Outside chrome seen from the seat (A-pillar trim, window frames, bonnet strips): the
    # shared chrome vanilla interiors use (host 193: 3,700 triangles), issue #232.
    chrome = forza2dsf.CHROME
    for uid in (glass, paint, trim, chrome):
        layout.setdefault(uid, (forza2dsf.MATERIAL_LAYOUT if known is None else known).get(uid, (28, 5)))
    tex = next(iter(local["interior"]["textures"]))
    if tex not in texc:
        raise ValueError(f"interior texture {tex.hex()} is not in the interior entry")
    return InteriorMaterials(
        local["interior"]["uid"],
        local["dials"]["uid"] if "dials" in local else None,
        tex,
        {"paint": paint, "glass": glass, "trim": trim, "chrome": chrome, "tyre": None, "skip": None},
        layout,
        needle_part,
        cabin_part,
    )


def _needle_vertex(pos, uv, gauge_id: int, normal, stride: int = 20, comp: int = 0) -> bytes:
    """Host needle layout: s0 4 x i16, s8 4 x f16 (u, v, gauge id, comp), then at
    stride 20 s5 4 x u8 (the normal). Hosts 250 and 293 draw their Dials
    material from a stride-16 buffer without the normal (145 vanilla interiors
    use 20)."""
    from dsfforza.convert import forza2dsf

    q = [max(-32768, min(32767, round(c * forza2dsf.POSITION_SCALE))) for c in pos]
    head = struct.pack("<4h4e", q[0], q[1], q[2], 4096, uv[0], uv[1], float(gauge_id), float(comp))
    if stride == 16:
        return head
    p0, p1 = forza2dsf.spherical(normal)
    return head + struct.pack("<4B", p0, p1, 128, 128)


# Gloss materials (copies of the car's Lights material, about 11 KB each) the interior
# entry takes at most for the outside parts it holds (issue #232).
COCKPIT_GLOSS_MATERIALS = 6
# Vertex-cluster cells (m) tried in turn until the interior MDXN fits.
DECIMATE_LADDER = (0.0, 0.004, 0.007, 0.01, 0.015, 0.02, 0.03)
# Largest vanilla interior MDXN (Sports Coupe 10); port_car enforces the same.
INTERIOR_MDXN_LIMIT = 0x12D000


@dataclass
class CockpitOptions:
    shell_lod: int = 5  # main-carbin LOD for the outer body seen through the windows
    shell: bool = True
    headlights: bool = False  # the cockpit carbin's headlight sections (lights_LOD0)
    atlas_size: tuple[int, int] | None = None
    # DXT1 halves the atlas (vanilla interiors ship DXT1 textures too); the
    # Interior material then loses its alpha specular mask, and needle cells
    # stay opaque for the alpha-tested Dials material.
    atlas_format: str = "DXT1"
    # interior_LOD0 holds the whole cabin (trims, dials, labels); nodamage_LOD0
    # only the outside parts seen through the windows (A-pillars, mirrors,
    # bonnet), so the cabin gets four times the texel density.
    atlas_weights: dict = field(default_factory=lambda: {fm.NODAMAGE: 1.0, fm.LIGHTS: 1.0, fm.INTERIOR: 4.0})
    # Lift, tone and filler apply to nodamage_LOD0 only, which in the cockpit
    # atlas holds just the outside parts seen through the windows (A-pillars,
    # mirrors, bonnet; fm.COCKPIT_EXTERIOR_SECTIONS). Cabin trims come from
    # interior_LOD0 in their own colours, unchanged: dark cabins come out at
    # luminance 25 to 60, the Desanne's interior texels have median 44. The
    # three were tuned when cabin trims were wrongly read from nodamage_LOD0
    # (research/cockpit.md, "Cabin trims sample interior_LOD0").
    # Floor for nodamage_LOD0 trim texels (near-black swatches).
    trim_lift: int = 40
    # nodamage_LOD0 (brightness, saturation) after the lift.
    trim_tone: tuple = (0.85, 0.55)
    # Source RGB for nodamage_LOD0 texels with alpha < 16, before lift and
    # tone: paint-through filler (white on most cars, a badge plate's grey on
    # the Jeep GC SRT8), not trim colour; near black like Forza's swatches.
    filler: tuple | None = (10, 10, 12)
    decimate: float = 0.0  # fixed vertex-cluster cell (m); 0 = step through DECIMATE_LADDER
    cull: bool = True  # drop triangles never seen from the head
    cull_resolution: int = 256  # cube-map face size for the visibility pass
    mdxn_limit: int = INTERIOR_MDXN_LIMIT


class CockpitBinding:
    """dsf_materials.Binding-like object for add_geometry: textured cockpit
    subsections use the interior's own material through the cockpit atlas,
    the rest the host interior's paint/glass/trim. Paint samples the shared
    paint atlas at paint_uv (the converted exterior's paint texel). On the
    outside parts (`outside(section)`: the cockpit carbin's bonnet, A-pillars and
    mirrors, and the shell), chrome takes the shared chrome and the atlas trims
    Texturing gave a gloss material take its copy of the Lights material (`gloss`:
    Texturing kind -> UID), as on the exterior (issue #232)."""

    def __init__(self, texturing, materials: InteriorMaterials, paint_uv, gloss: dict | None = None, outside=None):
        self.texturing, self.materials, self._paint_uv = texturing, materials, paint_uv
        self.gloss = gloss or {}
        self.outside = outside or (lambda section: False)

    def _flat(self, section, sub) -> bool:
        """Borrowed main-carbin sections (`section.flat`, a steering wheel
        added to a cockpit that has none) whose UVs don't fit the atlases:
        everything but paint and glass is drawn flat."""
        return getattr(section, "flat", False) and self.texturing.material(section, sub) not in (fm.PAINT, fm.GLASS)

    def material(self, section, sub, shared):
        from dsfforza.convert import dsf_materials as dm

        if sub.material.lower() in DIGITAL_TECHNIQUES:
            return self.materials.textured
        if FORCE_FLAT.match(sub.material) or self._flat(section, sub):
            return self.materials.textured
        kind = self.texturing.material(section, sub)
        if (
            kind != fm.SKIP
            and shared.get("chrome")
            and self.outside(section)
            and fm.classify(sub.material)[0] == fm.CHROME
        ):
            return shared["chrome"]  # add_geometry puts UV0 on its grey texel (forza2dsf.CHROME_UV)
        if kind in self.gloss:
            return self.gloss[kind]
        if dm.is_atlas_kind(kind) or kind == fm.TRIM:
            return self.materials.textured
        return shared.get(kind)

    def triangle_uvs(self, section, sub, triangle_index, raw_uvs):
        if sub.material.lower() in DIGITAL_TECHNIQUES:
            key = ("display",) + getattr(sub, "display_rgba", DISPLAY_COLOUR)
            return [self.texturing.solid_uv(key)] * len(raw_uvs), key
        if (
            FORCE_FLAT.match(sub.material)
            or self._flat(section, sub)
            or (self.texturing.material(section, sub) == fm.TRIM and not self._chrome(section, sub))
        ):
            key = ("flat",) + flat_colour(sub.material)
            return [self.texturing.solid_uv(key)] * len(raw_uvs), key
        return self.texturing.triangle_uvs(section, sub, triangle_index, raw_uvs)

    def _chrome(self, section, sub) -> bool:
        return self.outside(section) and fm.classify(sub.material)[0] == fm.CHROME

    @property
    def paint_uv(self):
        return self._paint_uv


def _needle_colour(atlases, section, sub) -> tuple[int, int, int, int]:
    if getattr(sub, "needle_rgba", None) is not None:  # a dash scene's needle (dash_needles)
        return sub.needle_rgba
    if sub.material in VIRTUAL_NEEDLE_COLOURS:
        return VIRTUAL_NEEDLE_COLOURS[sub.material]
    m = fm.resolve(section, sub, fm.classify_cockpit)
    if m.family is not None:
        try:
            r, g, b, _ = fm.footprint_colour(atlases.get(m.family), m.bounds)
        except FileNotFoundError:
            pass
        else:
            return (r, g, b, 255)
    return (200, 200, 200, 255)


def convert_interior(
    stream,
    interior,
    car,
    cf: CockpitFit,
    place,
    seed: bytes,
    paint_uv,
    opts: CockpitOptions,
    wheel_skin: bytes,
    log=print,
    glass_links: dict | None = None,
    glass_skin: dict | None = None,
    layout: dict | None = None,
    lights_template=None,
) -> dict:
    """Rebuild the interior entry (kind 1) from the Forza cockpit, in place.

    Cockpit sections (LOD 0) minus the race cage and, by default, the
    headlights; triangles never seen from the head (cube-map visibility from
    the eye and 8 cm either side, glass transparent) are dropped. The outer
    body from the main carbin at `shell_lod` fills the window views. If the
    MDXN is over `mdxn_limit`, the cockpit is vertex-clustered with growing
    cells (DECIMATE_LADDER), then the shell drops to its coarsest LOD.
    lights_template: (Lights MACO, its texture UID) of the car (forza2dsf.lights_template):
    the outside parts the cockpit holds then take Forza's specular and reflection from
    copies of it, as on the exterior (issue #232); None: no gloss."""
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.convert import forza2dsf
    from dsfforza.formats.chnk import entry_size
    from dsfforza.formats.vehicle_mesh import child

    # layout: the conversion's material layouts (forza2dsf.convert's), plus the interior's.
    mats = host_interior(stream, interior, layout)
    layout = {**(forza2dsf.MATERIAL_LAYOUT if layout is None else layout), **mats.layout}
    mdxn = child(interior, "MDXN")
    drsp = child(mdxn, "DRSP")
    original_children = list(drsp.children)
    texc = next(
        e for e in drsp.children if e.tag == "TEXC" and dm.current(stream, child(e, "UIDC"))[:8] == mats.texture
    )
    host_dds = dm.current(stream, child(texc, "DXTC"))
    height, width = struct.unpack_from("<II", host_dds, 12)
    mips = struct.unpack_from("<I", host_dds, 28)[0] or dm.DEFAULT_MIPS
    modl = child(child(interior, "NONR"), "MODL")
    host_modl = dm.current(stream, modl)

    static = {
        n: s
        for n, s in cf.static.items()
        if n not in SKIP_SECTIONS and (opts.headlights or not LIGHT_SECTIONS.search(n))
    }
    animated = {f: g for f, g in cf.gauges.items() if f in GAUGE_IDS and mats.needles is not None}
    # Needles without a DSF gauge stay as static geometry (level and temperature
    # gauges at half, others at rest).
    for family, poses in cf.needles.items():
        if family in animated:
            continue
        pose = poses.get("mid" if PARK_MID.match(family) else "min") or next(iter(poses.values()))
        static[pose.name] = pose
    names = list(static)
    sections = [static[n] for n in names]
    culled_report = None
    if opts.cull:
        # From the final head camera (camera_eye raises it above the Forza eye)
        # and a head-movement volume around it, plus the Forza eye itself.
        e = cf.camera or cf.eye
        eyes = [_add(e, o) for o in CULL_OFFSETS] + ([cf.eye] if cf.camera else [])
        before = sum(len(sub.triangles()) for s in sections for sub in s.subsections if sub.lod == 0)
        sections = cull_sections(sections, visible_triangles(sections, eyes, opts.cull_resolution))
        after = sum(len(sub.triangles()) for s in sections for sub in s.subsections if sub.lod == 0)
        culled_report = {"triangles_before": before, "triangles_after": after}
        log(f"cockpit visibility: {after} of {before} triangles seen from the head")

    atlases = fm.AtlasCache(car.folder, lod0=cf.lod0_atlases)
    needle_colours = {
        ("needle",) + c: c
        for g in animated.values()
        for sub in g.rest.subsections
        for c in [_needle_colour(atlases, g.rest, sub)]
    }
    needle_colours[("display",) + DISPLAY_COLOUR] = DISPLAY_COLOUR
    for s in sections:  # display faces under a virtual dial (flatten_face)
        for sub in s.subsections:
            colour = getattr(sub, "display_rgba", None)
            if colour is not None:
                needle_colours[("display",) + colour] = colour
    # The flat cells every interior's atlas has; a virtual dial's dark marks and a
    # virtual display add theirs only where they are drawn (other cars' atlases stay
    # as they were).
    used = {sub.material for s in sections for sub in s.subsections}
    flat = {
        c
        for pattern, c in FLAT_COCKPIT
        if pattern.pattern not in VIRTUAL_ONLY_FLAT or any(pattern.search(m) for m in used)
    }
    for colour in flat | {FLAT_DEFAULT}:
        needle_colours[("flat",) + colour] = colour
    families = (fm.NODAMAGE, fm.INTERIOR) + ((fm.LIGHTS,) if opts.headlights else ())
    classifier = fm.classify_cockpit
    if not cf.lod0_atlases:
        # The main carbin's cabin (stub cockpit carbin): main atlases, whose
        # dial faces are in nodamage, not interior_LOD0.
        families = (fm.NODAMAGE,) + ((fm.LIGHTS,) if opts.headlights else ())

        # Main-carbin techniques, not the cockpit_ variants: no section rule.
        def classifier(name):
            role, family = fm.classify_cockpit(name)
            return role, fm.NODAMAGE if family == fm.INTERIOR else family

    shell = []
    if opts.shell:
        skip = (
            {n.lower() for n in cf.static}
            | {n.lower() for n in cf.shell_skip}
            | {p.name.lower() for poses in cf.needles.values() for p in poses.values()}
        )
        shell = [s for s in car.body if s.name.lower() not in skip and not SHELL_SKIP.match(s.name)]
    shell_lods = sorted({sub.lod for s in shell for sub in s.subsections if sub.lod > 0})
    shell_ids = {id(s) for s in shell}

    def outside(section) -> bool:
        """The parts of the car's outside the interior entry draws: the cockpit carbin's
        bonnet, A-pillars and mirrors (fm.COCKPIT_EXTERIOR_SECTIONS), and the shell."""
        return id(section) in shell_ids or bool(fm.COCKPIT_EXTERIOR_SECTIONS.match(section.name))

    def gloss_filter(section, sub) -> bool:  # chrome takes the shared chrome (CockpitBinding)
        return outside(section) and fm.classify(sub.material)[0] != fm.CHROME

    # What the driver sees of the glass: crack lines start and rim shards sit there.
    sight = GlassSight(sections, cf.camera or cf.eye, place)
    shell_lod = opts.shell_lod
    # Step to finer shell LODs while the shell closes the windscreen or the cockpit.
    while shell and shell_lod > 1:
        closed = shell_closes(shell, max((l for l in shell_lods if l <= shell_lod), default=shell_lod), cf)
        if not closed:
            break
        log(f"cockpit shell: LOD {shell_lod} closes the {closed}; trying LOD {shell_lod - 1}")
        shell_lod -= 1
    ladder = [(cell, shell_lod) for cell in ([opts.decimate] if opts.decimate else DECIMATE_LADDER)]
    if shell_lods and max(shell_lods) > shell_lod and not shell_closes(shell, max(shell_lods), cf):
        ladder.append((ladder[-1][0], max(shell_lods)))
    attempts = []
    for cell, shell_lod in ladder:
        drsp.children = list(original_children)
        current = [decimate_section(s, cell) for s in sections] if cell > 0 else sections
        t = dm.Texturing(
            car.folder,
            size=opts.atlas_size or (width, height),
            mips=mips,
            plain=(40, 40, 42, 0),
            lod0=cf.lod0_atlases,
            classifier=classifier,
            families=families,
            weights=dict(opts.atlas_weights),
            fourcc=opts.atlas_format.encode(),
            lift={fm.NODAMAGE: opts.trim_lift} if opts.trim_lift else {},
            tone={fm.NODAMAGE: opts.trim_tone} if opts.trim_tone else {},
            filler={fm.NODAMAGE: opts.filler} if opts.filler else {},
            cutout_colour=(34, 34, 36, 0),
            gloss_enabled=lights_template is not None,
            gloss_filter=gloss_filter,
            gloss_max=COCKPIT_GLOSS_MATERIALS,
        )
        t.plan(current, {0}, extra_solids=needle_colours)
        child(texc, "DXTC").payload = t.dds(template=host_dds)
        gloss = {}
        if t.gloss_materials:
            template, template_tex = lights_template
            prefix = seed[:3] + bytes([seed[3] ^ 0x2B])
            gloss = dm.add_gloss(stream, drsp, template, t.gloss_materials, prefix, {template_tex: mats.texture})
            layout.update(dict.fromkeys(gloss.values(), dm.STRIDES[dm.LIGHTS_MAT]))
        binding = CockpitBinding(t, mats, paint_uv, gloss, outside)
        builder = forza2dsf.Builder(seed[:3] + bytes([seed[3] ^ 0x55]), layout=layout)
        cabin = forza2dsf.add_geometry(
            builder,
            current,
            0,
            place,
            wheel=False,
            textures=binding,
            merge=True,
            shared=mats.shared,
            skin={**(glass_skin or {}), "steering_wheel": wheel_skin},
            glass_links=glass_links,
            glass_rect=forza2dsf.INTERIOR_GLASS_RECT,
            glass_sight=sight,
        )
        lod = max((l for l in shell_lods if l <= shell_lod), default=shell_lods[0] if shell_lods else 0)
        if shell:
            cabin += forza2dsf.add_geometry(
                builder,
                shell,
                lod,
                place,
                wheel=False,
                textures=binding,
                merge=True,
                shared=mats.shared,
                glass_links=glass_links,
                glass_rect=forza2dsf.INTERIOR_GLASS_RECT,
                skin=glass_skin,
                glass_sight=sight,
            )
        needle_subs = []
        for family, g in animated.items():
            verts, tris, remap = [], [], {}
            pool = g.rest.lod0_vertices
            cal = cf.calibration.get(family)
            # The engine draws the rest pose at 0 rpm / 0 mph: turn the min pose there.
            turn = cal.rest_deg if cal is not None and abs(cal.rest_deg) >= 0.25 else 0.0
            for sub in g.rest.subsections:
                uv = t.solid_uv(("needle",) + _needle_colour(atlases, g.rest, sub))
                for tri in sub.triangles():
                    out = []
                    for i in tri:
                        if (i, uv) not in remap:
                            remap[(i, uv)] = len(verts)
                            v = pool[i]
                            pos, nrm = v.position, v.normal
                            if turn:
                                pos = rotate_about(pos, g.pivot, g.normal, turn)
                                nrm = rotate_about(nrm, (0.0, 0.0, 0.0), g.normal, turn)
                            stride = layout[mats.needles][0]
                            verts.append(
                                _needle_vertex(place.point(pos), uv, GAUGE_IDS[family], place.normal(nrm), stride)
                            )
                        out.append(remap[(i, uv)])
                    tris.append(tuple(out))
            if tris:
                needle_subs.append(builder.add(mats.needles, verts, tris))
        digital_report, digital_templates = None, None
        if "skipped" in cf.digital_displays:
            log(f"digital displays: not drawn ({cf.digital_displays['skipped']})")
        if cf.digital_plan is not None and mats.needles is not None:
            from dsfforza.convert import digital_dash as dd

            plan = cf.digital_plan
            tex_uid, mat_uid = builder.new_uid(), builder.new_uid()
            drsp.children.append(
                dm.texture_entry(stream, texc, dd.dxt5_dds(plan.width, plan.height, plan.rgba), tex_uid)
            )
            drsp.children.append(
                dm.retarget_material(stream, dd.vanilla_material(stream), {dd.VANILLA_TEXTURE: tex_uid}, mat_uid)
            )
            # The digital-dials shaders are drawn from the Volt's own buffer layout (no
            # normal): a buffer of their own, stride 16 (hosts 250 and 293 draw their
            # needles from that layout too, so they share it).
            stride = dd.VANILLA_STRIDE
            builder.layout[mat_uid] = (stride, dd.MODL_FLAG)
            digital_templates = {stride: dd.vanilla_buffer(stream)}
            verts, tris = dd.vertices(plan, place, stride)
            if tris:
                needle_subs.append(builder.add(mat_uid, verts, tris))
            digital_report = {
                **cf.digital_displays,
                "quads": len(plan.quads),
                "texture": [plan.width, plan.height],
                "material": mat_uid.hex(),
            }
        forza2dsf.rebuild_drsp(stream, drsp, builder, keep_host_materials=True, templates=digital_templates)
        part_lods = {mats.cabin_part: {INTERIOR_MASK: cabin}}
        if mats.needle_part is not None:
            part_lods[mats.needle_part] = {INTERIOR_MASK: needle_subs}
        else:
            part_lods[mats.cabin_part][INTERIOR_MASK] = cabin + needle_subs
        modl.payload = forza2dsf.rebuild_modl(host_modl, part_lods)
        size = entry_size(mdxn)
        attempts.append({"decimate": cell, "shell_lod": lod if shell else None, "mdxn": size})
        vertex_bytes = sum(len(b) for b in builder.buffers.values())
        log(
            f"interior entry: {len(builder.subs)} submodels ({len(current)} cockpit sections, {len(shell)} shell "
            f"sections at LOD {lod}, {len(needle_subs)} needles), {vertex_bytes // 1024} KiB vertices, "
            f"{len(builder.indices)} indices, cluster cell {cell * 1000:.0f} mm: MDXN {size:#x}"
            + ("" if size <= opts.mdxn_limit else f" > {opts.mdxn_limit:#x}, retrying")
        )
        if size <= opts.mdxn_limit:
            break
    log(f"interior atlas: {t.summary()}")
    if t.gloss_materials:
        log(
            "interior gloss materials (intensity/power/reflection/fresnel power/bias): "
            + "; ".join(
                f"{n}: {m.intensity:g}/{m.power:g}/{m.reflection:g}/{m.fresnel_power:g}/{m.bias:g}"
                for n, m in enumerate(t.gloss_materials)
            )
            + " for "
            + ", ".join(f"{k} {sum(v.values())}" for k, v in sorted(t.gloss_report.items()))
        )
    glass_windows = getattr(builder, "interior_glass", {})
    log(
        "interior glass: "
        + ", ".join(
            f"{g} s9 {w['s9']} {w['surface_m2']} m2"
            + (f", cracks {w['crack_m']} m, {w['shard_triangles']} shards" if "crack_m" in w else "")
            for g, w in sorted(glass_windows.items())
        )
    )
    return {
        "glass_windows": glass_windows,
        "submodels": len(builder.subs),
        "vertex_bytes": vertex_bytes,
        "indices": len(builder.indices),
        "cockpit_sections": names,
        "shell_sections": len(shell),
        "needles": sorted(animated),
        "parked_needles": sorted(f for f in cf.needles if f not in animated),
        "digital_triangles": cf.digital,
        "display_face_triangles": cf.display_faces,
        **({"virtual_gauges": cf.virtual} if cf.virtual else {}),
        **({"digital_displays": digital_report} if digital_report else {}),
        "atlas": t.summary(),
        **({"gloss": {k: sum(v.values()) for k, v in t.gloss_report.items()}} if t.gloss_materials else {}),
        "chrome_triangles": sum(b.index_count // 3 for b in builder.subs if b.material == mats.shared["chrome"]),
        "visibility": culled_report,
        "attempts": attempts,
        "decimate": attempts[-1]["decimate"],
        "shell_lod": attempts[-1]["shell_lod"],
    }
