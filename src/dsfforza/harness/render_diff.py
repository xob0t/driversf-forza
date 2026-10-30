"""Before/after: which cars a converter change touches, and how their renders change.

Two builds (master's and a PR's): the cars whose drawn data differ (each car's three
VEHC entries, their MDXN and the MODL, UPVH and paint palette of NONR, hashed in both
archives: handling, sound and the rest of NONR don't count) are rendered from both
builds against the same Forza reference (render_compare), and per view, mode and region
the change is described: how much of the region's pixels changed and by how much, what
draws it (DSF program) before and after, and the flags each build raises against Forza
(fixed, new). Cars whose drawn data are the same are listed as unchanged without a render.

    uv run dsff render_compare diff --before out/build --after ../pr/out/build --out out/rc/pr [--jobs 4]
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np

from dsfforza.harness import render_compare as rc

VISUAL_NONR = ("MODL", "UPVH", "SPSR")  # NONR children that change how a car looks
CHANGED = 6.0  # delta E of a pixel that counts as changed


def _digest_entry(stream, entry, h) -> None:
    if entry.children:
        for c in entry.children:
            _digest_entry(stream, c, h)
        return
    stream.seek(entry.offset)
    h.update(entry.tag.encode())
    h.update(stream.read(entry.size))


def visual_digests(archive: Path, cars: list[dict]) -> dict[str, str]:
    """{media: digest of what the game draws of the car} for a build's archive."""
    from dsfforza.formats import upvh as uv
    from dsfforza.game.gui_assets import _root_entries

    out = {}
    archive = Path(archive)
    with archive.open("rb") as stream:
        roots = _root_entries(archive, stream)
        for car in cars:
            try:
                kinds = uv.entries_for(stream, roots, car["spool_id"], None)
            except (KeyError, StopIteration):
                continue
            out[car["media"]] = vehc_digest(stream, kinds)
    return out


def vehc_digest(stream, kinds: dict) -> str:
    """Digest of what the game draws of one car: {kind: VEHC entry} (upvh.entries_for)."""
    h = hashlib.blake2b(digest_size=16)
    for kind in sorted(kinds):
        for part in kinds[kind].children:
            if part.tag == "MDXN":
                _digest_entry(stream, part, h)
            elif part.tag == "NONR":
                for leaf in part.children:
                    if leaf.tag in VISUAL_NONR:
                        _digest_entry(stream, leaf, h)
    return h.hexdigest()


def changed_cars(before: Path, after: Path) -> tuple[list[dict], dict]:
    """(cars of the after build whose drawn data differ from the before build's, counts)."""
    a = {c["media"]: c for c in rc.roster(before)}
    b = rc.roster(after)
    da = visual_digests(Path(before) / "dngvehicles.sp", list(a.values()))
    db = visual_digests(Path(after) / "dngvehicles.sp", b)
    changed, same, new = [], [], []
    for car in b:
        m = car["media"]
        if m not in da:
            new.append(m)
        elif da[m] != db.get(m):
            changed.append({**car, "before_spool_id": a[m]["spool_id"]})
        else:
            same.append(m)
    return changed, {
        "changed": len(changed),
        "unchanged": len(same),
        "new": new,
        "gone": sorted(set(a) - {c["media"] for c in b}),
    }


# Regions a change is listed in. "other" is ours with no Forza counterpart: what the
# converter adds (virtual displays' housings, moved wheel displays: #249).
LISTED = [*rc.SCORED, "cabin", "other", "displays", "glass"]
ADDED = "added"  # how "other" is named in the report


def region_changes(ga: np.ndarray, gb: np.ndarray, d: np.ndarray, names=LISTED) -> list[dict]:
    """Per region (ID maps ga, gb of the two builds; d: per-pixel delta E between their
    renders): the share of the region's pixels that moved or changed by CHANGED or more,
    when that is 2% or more and the region has MIN_PIXELS."""
    out = []
    for name in names:
        rid = rc.RID[name]
        ma, mb = ga == rid, gb == rid
        both = ma & mb
        n = int((ma | mb).sum())
        if n < rc.MIN_PIXELS:
            continue
        moved = int((ma ^ mb).sum())
        diff = int((both & (d > CHANGED)).sum())
        share = (moved + diff) / n
        if share < 0.02:
            continue
        out.append(
            {
                "region": ADDED if name == "other" else name,
                "changed": round(share, 3),
                "pixels_before": int(ma.sum()),
                "pixels_after": int(mb.sum()),
                "dE": round(float(d[both].mean()), 1) if both.any() else None,
            }
        )
    return out


def compare_builds(
    before: Path, after: Path, car: dict, roots_a, roots_b, out: Path, views, size, cockpit: bool = True
) -> dict:
    """One changed car: both builds' renders and scores, and what changed per region (the
    exterior views, and the cockpit view when both builds have a cockpit)."""
    media = car["media"]
    t0 = time.time()
    res: dict = {"media": media, "spool_id": car["spool_id"]}
    rep_a, rep_b = rc.car_report(before, media), rc.car_report(after, media)
    if rep_a is None or rep_b is None:
        res["error"] = "no converter report in one of the builds"
        return res
    try:
        pa = rc.build_pair(Path(before) / "dngvehicles.sp", roots_a, car["before_spool_id"], media, rep_a)
        pb = rc.build_pair(Path(after) / "dngvehicles.sp", roots_b, car["spool_id"], media, rep_b)
        metrics_a, metrics_b, changes = {}, {}, []
        folder = out / "cars" / media
        jobs = [(vname, rc.VIEWS[vname], pa, pb) for vname in views]
        # The cockpit view from the after build's eye (one camera for both: the pixels compare).
        camera = rc.cockpit_camera(rep_b) if rc.cockpit_camera(rep_a) else None
        if cockpit and camera is not None:
            ca = rc.build_cockpit_pair(Path(before) / "dngvehicles.sp", roots_a, car["before_spool_id"], media, rep_a)
            cb = rc.build_cockpit_pair(Path(after) / "dngvehicles.sp", roots_b, car["spool_id"], media, rep_b)
            jobs.append(("cockpit", rc.View("cockpit", camera, modes=("day", "dark")), ca, cb))
        for vname, view, qa, qb in jobs:
            # One frame for both builds: their own extents differ when a part moves (#254).
            box = rc.union_box(qa.box, qb.box)
            ra = rc.render_view(qa, view, size, box)
            rb = rc.render_view(qb, view, size, box)
            metrics_a[vname] = {"day": rc.region_scores(ra)}
            metrics_b[vname] = {"day": rc.region_scores(rb)}
            for mode in view.modes:
                if mode != "day":
                    metrics_a[vname][f"glow_{mode}"] = rc.glow_scores(ra, mode)
                    metrics_b[vname][f"glow_{mode}"] = rc.glow_scores(rb, mode)
                la, lb = rc._lab(ra["ours"][mode], mode), rc._lab(rb["ours"][mode], mode)
                d = np.linalg.norm(la - lb, axis=-1)
                ga, gb = ra["regions"]["ours"], rb["regions"]["ours"]
                for entry in region_changes(ra["regions"]["ours"], rb["regions"]["ours"], d):
                    entry.update({"view": vname, "mode": mode})
                    name = entry["region"]
                    if mode == "day":
                        sa = metrics_a[vname]["day"].get(name, {})
                        sb = metrics_b[vname]["day"].get(name, {})
                        for key in ("L_ours", "contrast_ours", "structure"):
                            if key in sa and key in sb:
                                entry[f"{key}_before"], entry[f"{key}_after"] = sa[key], sb[key]
                        if sa.get("drawn_as") != sb.get("drawn_as"):
                            entry["drawn_as_before"], entry["drawn_as_after"] = sa.get("drawn_as"), sb.get("drawn_as")
                    changes.append(entry)
                if any(c["view"] == vname and c["mode"] == mode for c in changes):
                    a_img = rc.to_srgb8(ra["ours"][mode], ra["alpha"]["ours"], mode)
                    b_img = rc.to_srgb8(rb["ours"][mode], rb["alpha"]["ours"], mode)
                    f_img = rc.to_srgb8(rb["ref"][mode], rb["alpha"]["ref"], mode)
                    mark = (d > CHANGED) | (ga != gb)
                    b_mark = b_img.copy()
                    rc.outline(b_mark, _grow(mark, 2), (255, 200, 40))
                    gap = np.full((a_img.shape[0], 4, 3), 20, np.uint8)
                    rc.save_jpeg(
                        folder / f"diff-{vname}-{mode}.jpg", np.concatenate([a_img, gap, b_mark, gap, f_img], axis=1)
                    )
        fa, fb = rc.flags_for(metrics_a), rc.flags_for(metrics_b)
        ka = {(f["region"], f["what"]): f for f in fa}
        kb = {(f["region"], f["what"]): f for f in fb}
        res.update(
            {
                "match": {"before": pa.match, "after": pb.match},
                "score_before": rc.car_score(fa),
                "score_after": rc.car_score(fb),
                "fixed": [ka[k]["text"] for k in ka if k not in kb],
                "new": [kb[k]["text"] for k in kb if k not in ka],
                "changes": sorted(changes, key=lambda c: -c["changed"]),
                "seconds": round(time.time() - t0, 1),
            }
        )
    except Exception as error:  # noqa: BLE001 - one car's failure is a result, not the run's end
        import traceback

        res["error"] = f"{type(error).__name__}: {error}"
        res["traceback"] = traceback.format_exc(limit=6)
    (out / "cars").mkdir(parents=True, exist_ok=True)
    (out / "cars" / f"{media}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def _grow(mask: np.ndarray, r: int) -> np.ndarray:
    return rc._box_blur(mask.astype(np.float32), r) > 0


def _worker(args):
    before, after, cars, out, views, size = args
    # gui_assets._root_entries keeps one archive's roots: read both here.
    from dsfforza.formats import chnk

    archive_b = Path(after) / "dngvehicles.sp"
    with archive_b.open("rb") as stream:
        roots_b = chnk.read_chunk(stream, 0).entries
    archive_a = Path(before) / "dngvehicles.sp"
    with archive_a.open("rb") as stream:
        roots_a = chnk.read_chunk(stream, 0).entries
    results = []
    for car in cars:
        r = compare_builds(Path(before), Path(after), car, roots_a, roots_b, Path(out), views, size)
        print(f"{car['media']}: {r.get('score_before')} -> {r.get('score_after')} {r.get('error', '')}", flush=True)
        results.append(
            {
                k: r.get(k)
                for k in ("media", "spool_id", "score_before", "score_after", "fixed", "new", "changes", "error")
            }
        )
    return results


def run(
    before: Path, after: Path, out: Path, jobs: int = 1, views=rc.DEFAULT_VIEWS, size=rc.SIZE, limit: int = 0
) -> dict:
    t0 = time.time()
    out.mkdir(parents=True, exist_ok=True)
    changed, counts = changed_cars(before, after)
    t_digest = time.time() - t0
    todo = changed[:limit] if limit else changed
    jobs = max(1, min(jobs, len(todo) or 1))
    chunks = [todo[i::jobs] for i in range(jobs)]
    results: list = []
    if todo:
        if jobs == 1:
            results = _worker((str(before), str(after), todo, str(out), views, size))
        else:
            from concurrent.futures import ProcessPoolExecutor

            with ProcessPoolExecutor(jobs) as pool:
                args = [(str(before), str(after), c, str(out), views, size) for c in chunks]
                results = [r for part in pool.map(_worker, args) for r in part]
    results.sort(key=lambda r: -max((c["changed"] for c in r.get("changes") or []), default=0))
    visible = [r["media"] for r in results if r.get("changes")]
    summary = {
        "before": str(before),
        "after": str(after),
        **counts,
        "rendered": len(todo),
        "drawn_differently": len(visible),
        "renders_alike": [r["media"] for r in results if not r.get("changes") and not r.get("error")],
        "seconds": round(time.time() - t0, 1),
        "digest_seconds": round(t_digest, 1),
        "views": list(views),
        "cars": results,
    }
    (out / "diff.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


PAGE = """<!doctype html><meta charset="utf-8"><title>{title}</title>
<style>
body{{background:#1d1f21;color:#ddd;font:13px/1.4 system-ui,sans-serif;margin:16px}}
h1{{font-size:18px}} h2{{font-size:15px;margin:22px 0 4px}} .g{{color:#8d8}} .b{{color:#f88}}
ul{{margin:2px 0 6px 18px;padding:0}} img{{width:{width}px;display:block;margin:2px 0 8px}} span{{color:#999}}
</style>
<h1>{title}</h1><p>{summary}</p>{cars}
"""


def write_sheet(out: Path, width: int = 900) -> Path:
    """out/diff.html: per changed car, before | after (changes outlined) | Forza."""
    import html

    s = json.loads((out / "diff.json").read_text(encoding="utf-8"))
    parts = []
    for car in s["cars"]:
        media = car["media"]
        lines = []
        for c in (car.get("changes") or [])[:12]:
            extra = ""
            if c.get("drawn_as_before") is not None or c.get("drawn_as_after") is not None:
                extra = f"; drawn as {c.get('drawn_as_before')} -> {c.get('drawn_as_after')}"
            lines.append(
                f"<li>{c['region']} ({c['view']}, {c['mode']}): {c['changed']:.0%} of its pixels, dE {c['dE']}{html.escape(extra)}</li>"
            )
        fixed = "".join(f"<li class=g>fixed: {html.escape(t)}</li>" for t in car.get("fixed") or [])
        new = "".join(f"<li class=b>new: {html.escape(t)}</li>" for t in car.get("new") or [])
        imgs = "".join(
            f'<img loading="lazy" src="{p.relative_to(out).as_posix()}"><span>{p.stem}: before | after | Forza</span>'
            for p in sorted((out / "cars" / media).glob("diff-*.jpg"))
        )
        err = f" <span class=b>{html.escape(car['error'])}</span>" if car.get("error") else ""
        parts.append(
            f"<h2>{media}: score {car.get('score_before')} -> {car.get('score_after')}{err}</h2><ul>{fixed}{new}{''.join(lines)}</ul>{imgs}"
        )
    path = out / "diff.html"
    text = PAGE.format(
        title=html.escape(f"render_compare diff: {s['before']} -> {s['after']}"),
        summary=html.escape(
            f"{s['changed']} cars' drawn data changed ({s.get('drawn_differently')} drawn differently, "
            f"{len(s.get('renders_alike', []))} rendering alike), {s['unchanged']} unchanged, {len(s['new'])} new, "
            f"{len(s['gone'])} gone; "
            f"{s['rendered']} rendered in {s['seconds']} s (digests {s['digest_seconds']} s)"
        ),
        cars="".join(parts),
        width=width,
    )
    path.write_text(text, encoding="utf-8")
    return path
