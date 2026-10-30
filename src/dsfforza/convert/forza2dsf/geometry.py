"""add_geometry: Forza sections into a Builder at one LOD."""

from __future__ import annotations

from dsfforza.convert.forza2dsf.builder import ROLE_OF, SHARED, Builder, NewSub
from dsfforza.convert.forza2dsf.materials import (
    CHROME,
    GLASS,
    PAINT,
    PAINT_UV,
    PLATE_TECHNIQUE,
    TRIM,
    TRIM_UV,
    chrome_uv,
    material_for,
)
from dsfforza.convert.forza2dsf.vertices import (
    GLASS_RECT,
    INTERIOR_GLASS_RECT,
    RIGID,
    Placement,
    encode_vertex,
    glass_link,
    glass_uv,
    vertex_sky,
)
from dsfforza.convert.forza2dsf.wheels import (
    WHEEL_BLUR_ALWAYS,
    WHEEL_BLUR_FADE_IN,
    WHEEL_BLUR_SECTION,
    WHEEL_BLUR_SKY,
    WHEEL_UV,
    TyreMap,
    is_blur_disc,
    is_rotor,
    wheel_lod,
    wheel_material,
    wheel_normal,
    wheel_point,
    wheel_role,
    wheel_skies,
)


def add_geometry(
    builder: Builder,
    sections,
    lod: int,
    place: Placement,
    wheel: bool,
    textures=None,
    merge: bool = False,
    shared: dict | None = None,
    skin: dict[str, bytes] | None = None,
    glass_links: dict | None = None,
    glass_rect: tuple[float, float, float, float] = GLASS_RECT,
    lamps=None,
    keep=None,
    wheel_fit: tuple[float, float] = (1.0, 1.0),
    exposed: bool = False,
    glass_sight=None,
) -> list[NewSub]:
    """Add every subsection of `sections` at Forza `lod`, one DSF submodel per
    (section, material), and return the submodels.

    wheel: the sections are the Forza wheel model: turned into DSF's wheel frame
    (wheel_point, scaled by wheel_fit), Forza's blur sections left out (wheel_blur_section's
    disc, among the sections, fades in instead). `lod` goes through
    wheel_lod; the tyre is on the shared tyre material, unwrapped onto the vanilla
    tread strip and sidewall ring (TyreMap). Rims sample the car's wheel texture
    through `textures` (planned with the wheel, wheel_material), else they are on
    the shared tyre material with a swatch per wheel_role.

    textures: a dsf_materials.Binding (see dsf_materials.prepare). Lamps,
    badges and interior then use the entry's Chassis/Lights materials with UVs
    in the converted atlas, other parts DSF's shared materials by
    forza_materials role, and paint points at the atlas's plain texel.
    merge: one submodel per material across all sections (split at 64K
    vertices), for fewer render objects when the sections share a MODL part.
    shared: role -> material UID map instead of SHARED (the interior entry
    uses its host's own paint and glass). skin: section name -> vertex
    semantic 9 bytes (encode_vertex), e.g. the steering wheel's hierarchy node.
    glass_links: {"front": s9, "rear": s9} (host_glass_links): windscreen and
    rear-window triangles are bound to the host's glass-panel records like
    vanilla glass, instead of the chassis. glass_rect: GLASS_RECT (default) maps
    each window onto its slot of the glass texture (glass.SlotMapper); another
    rectangle squashes Forza's glass UVs into it, except INTERIOR_GLASS_RECT
    (the interior entry), which lays each window out like vanilla interior glass
    (glass.InteriorGlass; per-window report in builder.interior_glass);
    glass_sight (cockpit.GlassSight) tells it which glass the driver sees."""
    textured = textures is not None
    tyre_map = skies = None
    if wheel:
        lod = wheel_lod(sections, lod)
        skies = wheel_skies([s for s in sections if s.name != WHEEL_BLUR_SECTION], lod, wheel_fit)
        tyre_map = TyreMap.build(
            [
                [wheel_point(s.vertices_for(sub)[i].position, wheel_fit) for i in t]
                for s in sections
                for sub in s.subsections
                if sub.lod == lod and wheel_role(sub.material) == "tyre"
                for t in sub.triangles()
            ]
        )
    shared = SHARED if shared is None else shared
    glass_materials = {GLASS} | ({shared["glass"]} if shared.get("glass") else set())
    skin = skin or {}
    batches: list[dict[bytes, list]] = []  # per batch: material -> [(section, sub)]
    for section in sections:
        if not merge or not batches:
            batches.append({})
        for sub in section.subsections:
            if sub.lod != lod:
                continue
            if wheel:
                material = wheel_material(textures, section, sub, shared)
            elif textured:
                material = textures.material(section, sub, shared)
            else:
                material = material_for(sub.material)
                material = shared.get(ROLE_OF[material], material) if material in ROLE_OF else material
            if material is None:
                continue
            batches[-1].setdefault(material, []).append((section, sub))
    # Exterior/full glass: each window mapped by its own shape onto its panel
    # slot of the glass texture (src/dsfforza/convert/glass.py), from all of the section's
    # glass triangles, so per-panel calls (keep) agree on the mapping.
    slots = None
    if not wheel and glass_rect == GLASS_RECT:
        from dsfforza.convert import glass

        slots = glass.SlotMapper()
        for groups in batches:
            for material, items in groups.items():
                if material in glass_materials:
                    for section, sub in items:
                        pool = section.vertices_for(sub)
                        for tri in sub.triangles():
                            slots.add(section.name, [place.point(pool[i].position) for i in tri], sub.material)
    # Interior glass (glass_rect=INTERIOR_GLASS_RECT): collected per window and
    # rebuilt in the vanilla interior layout (glass.InteriorGlass: smear surface,
    # crack lines for states 1-2, rim shards for state 3), each window on its
    # own s9 so the panel's crack state shows from the cockpit.
    interior_glass = not wheel and glass_rect == INTERIOR_GLASS_RECT
    added = []
    for groups in batches:
        for material, items in groups.items():
            stride = builder.layout[material][0]
            verts_out, remap, tris = [], {}, []
            window = None
            if interior_glass and material in glass_materials:
                from dsfforza.convert import glass

                window = glass.InteriorGlass(seed=builder.uid_prefix.hex(), sight=glass_sight)

            def flush():  # called within this iteration only
                if tris:  # noqa: B023
                    added.append(builder.add(material, list(verts_out), list(tris)))  # noqa: B023
                verts_out.clear(), remap.clear(), tris.clear()  # noqa: B023

            for section, sub in items:
                pool = section.vertices_for(sub)
                x_off, x_scale, y_off, y_scale = sub.uv_transform[:4]
                role = wheel_role(sub.material) if wheel else None
                # Shared chrome: its grey's texel (bare metal's own, chrome_uv); in the vertex
                # key, so metal of two greys on one vertex pool keeps both.
                grey_uv = chrome_uv(sub.material) if material == CHROME or material == shared.get("chrome") else None
                for k, tri in enumerate(sub.triangles()):
                    if keep is not None and not keep(section, sub, k, tri):
                        continue  # damage.DamagePlan: another panel's triangle
                    if merge and len(verts_out) > 0xFFFF - 3:
                        flush()
                    uvs, tag = (
                        textures.triangle_uvs(section, sub, k, [pool[i].uv0 for i in tri])
                        if textured and not (wheel and is_rotor(sub))
                        else (None, None)
                    )
                    if tag == "drop":
                        continue  # alpha-tested away in Forza (dsf_materials.DROP)
                    s9 = skin.get(section.name, RIGID)
                    disc = wheel and is_blur_disc(sub)  # wheel_blur_section: fades in, or always drawn
                    if disc:
                        s9 = WHEEL_BLUR_ALWAYS if getattr(sub, "always", False) else WHEEL_BLUR_FADE_IN
                    if glass_links and material in glass_materials and not wheel:
                        s9 = glass_link(
                            glass_links,
                            section.name,
                            [place.point(pool[i].position) for i in tri],
                            [place.normal(pool[i].normal) for i in tri],
                            s9,
                        )
                    if lamps is not None and not wheel:
                        s9 = lamps.s9(sub, k, s9)  # lights.LampPlan: light id + 1 in byte 2
                    if window is not None:
                        normals = [place.normal(pool[i].normal) for i in tri]
                        window.add(
                            section.name,
                            [place.point(pool[i].position) for i in tri],
                            normals,
                            [vertex_sky(pool[i], pool, n) for i, n in zip(tri, normals)],
                            s9,
                            sub.material,
                        )
                        continue
                    pane = None  # glass: the window (slot group) this triangle belongs to
                    if slots is not None and material in glass_materials:
                        pane = slots.classify(section.name, [place.point(pool[i].position) for i in tri], sub.material)[
                            1
                        ]
                    # damage.split_exposed subsections: sentinel UV0 for DamagePlan (exposed=True)
                    shown = getattr(sub, "exposed_uv", None) if exposed and not wheel else None
                    tyre_uv = None  # tyre: tread strip / sidewall ring UVs for this triangle (TyreMap)
                    if tyre_map is not None and role == "tyre":
                        tyre_uv = [
                            (round(u, 5), round(w, 5))
                            for u, w in tyre_map.uvs([wheel_point(pool[i].position, wheel_fit) for i in tri])
                        ]
                    out = []
                    for n, i in enumerate(tri):
                        key = (
                            id(pool),
                            i,
                            x_off,
                            x_scale,
                            y_off,
                            y_scale,
                            tag,
                            s9,
                            role,
                            pane,
                            tyre_uv[n] if tyre_uv else None,
                            shown,
                            grey_uv,
                        )
                        if key not in remap:
                            v = pool[i]
                            pos = wheel_point(v.position, wheel_fit) if wheel else place.point(v.position)
                            uv0 = uvs[n] if uvs else (v.uv0[0] * x_scale + x_off, v.uv0[1] * y_scale + y_off)
                            uv1 = v.uv1
                            if disc and tag is None:
                                uv0 = v.uv1  # the shared SpokeDisc texture, mapped like vanilla discs
                            elif wheel and tag is None:
                                uv0 = tyre_uv[n] if tyre_uv else WHEEL_UV[role]
                            elif sub.material == PLATE_TECHNIQUE:
                                uv0 = v.uv0  # the plate in the shared paint atlas (src/dsfforza/convert/plates.py)
                            elif material == PAINT or material == shared.get("paint"):
                                # The paint shader overlays the chassis texture: a plain texel.
                                uv0 = textures.paint_uv if textured else PAINT_UV
                            elif material in glass_materials and pane is not None:
                                uv0 = slots.uv(pane, pos)  # the window's own slot (src/dsfforza/convert/glass.py)
                            elif material in glass_materials:
                                uv0 = glass_uv(uv0, glass_rect)  # another rectangle (not used by the converter)
                            elif material == TRIM or material == shared.get("trim"):
                                # Shared trim samples DSF's global chassis texture (engine bay,
                                # undercarriage, grilles, plate...); Forza's swatch/tiling UVs
                                # land anywhere in it (the Veyron's carbon, x246 tiling, drew
                                # noise over its whole upper body). Vanilla flat trims use its
                                # dark block.
                                uv0 = TRIM_UV
                            elif material == CHROME or material == shared.get("chrome"):
                                # Shared chrome: a grey texel of the same texture; the
                                # reflection does the rest. UV1 as for paint (the
                                # damage decal; damage.DamagePlan takes the host's).
                                uv0 = grey_uv
                            elif tag is not None:
                                uv1 = uv0  # atlas-textured; the Lights material's UV1 feeds its decal map
                            if shown:
                                uv0 = shown
                            remap[key] = len(verts_out)
                            normal = wheel_normal(v.normal, wheel_fit) if wheel else place.normal(v.normal)
                            sky = skies.get((id(pool), i)) if skies is not None else None
                            if disc:
                                sky = WHEEL_BLUR_SKY
                            if sky is None:
                                sky = vertex_sky(v, pool, normal)
                            verts_out.append(encode_vertex(stride, pos, uv0, uv1, normal, s9, sky))
                        out.append(remap[key])
                    tris.append(tuple(out))
            if window is not None:
                for corners, _region in window.triangles():
                    if merge and len(verts_out) > 0xFFFF - 3:
                        flush()
                    out = []
                    for pos, uv0, normal, sky, s9 in corners:
                        key = ("glass", tuple(round(c, 5) for c in pos), uv0, s9)
                        if key not in remap:
                            remap[key] = len(verts_out)
                            verts_out.append(encode_vertex(stride, pos, uv0, uv0, normal, s9, sky))
                        out.append(remap[key])
                    tris.append(tuple(out))
                report = getattr(builder, "interior_glass", {})
                report.update(window.report)
                builder.interior_glass = report
            if len(verts_out) > 0xFFFF:
                raise ValueError(f"{items[0][0].name}: {len(verts_out)} vertices in one submodel")
            flush()
    return added
