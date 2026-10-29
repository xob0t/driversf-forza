"""The contact sheet of a render_compare run: one HTML page, worst cars first.

Per car its score, its flags and, per view and mode, ours | Forza side by side (the
JPEGs render_compare wrote; flagged regions outlined in magenta).

    uv run dsff render_compare sheet out/rc/master [--top 50]
"""

from __future__ import annotations

import html
import json
from pathlib import Path

PAGE = """<!doctype html><meta charset="utf-8"><title>{title}</title>
<style>
body{{background:#1d1f21;color:#ddd;font:13px/1.4 system-ui,sans-serif;margin:16px}}
h1{{font-size:18px}} h2{{font-size:15px;margin:22px 0 4px}} .s{{color:#f8a}} .e{{color:#f66}}
ul{{margin:2px 0 6px 18px;padding:0}} li{{margin:0}} .v{{display:inline-block;margin:2px 6px 8px 0;vertical-align:top}}
.v img{{width:{width}px;display:block}} .v span{{color:#999;font-size:11px}} a{{color:#8cf}}
table{{border-collapse:collapse}} td,th{{padding:1px 8px;text-align:left}} tr:nth-child(even){{background:#26282b}}
</style>
<h1>{title}</h1>
<p>{summary}</p>
<table><tr><th>#</th><th>car</th><th>score</th><th>worst flags</th></tr>{rows}</table>
{cars}
"""


def write(run: Path, top: int = 0, title: str | None = None, width: int = 560) -> Path:
    """run/sheet.html from run/roster.json and run/cars/."""
    run = Path(run)
    summary = json.loads((run / "roster.json").read_text(encoding="utf-8"))
    ranking = summary["ranking"]
    if top:
        ranking = ranking[:top]
    rows, cars = [], []
    for n, car in enumerate(ranking, 1):
        media = car["media"]
        flags = car.get("flags") or []
        worst = "; ".join(html.escape(f["text"]) for f in flags[:3])
        err = f' <span class="e">{html.escape(car["error"])}</span>' if car.get("error") else ""
        rows.append(
            f'<tr><td>{n}</td><td><a href="#{media}">{media}</a></td><td>{car.get("score") or 0:.2f}</td><td>{worst}{err}</td></tr>'
        )
        items = "".join(
            f"<li>{html.escape(f['text'])} <span class=s>({f['view']}, {f['severity']})</span></li>" for f in flags
        )
        folder = run / "cars" / media
        shots = []
        for img in sorted(folder.glob("*.jpg")) if folder.exists() else []:
            rel = img.relative_to(run).as_posix()
            shots.append(f'<div class="v"><img loading="lazy" src="{rel}"><span>{img.stem} (ours | Forza)</span></div>')
        cars.append(
            f'<h2 id="{media}">{n}. {media} (spool {car.get("spool_id")}) score {car.get("score") or 0:.2f}{err}</h2>'
            f"<ul>{items}</ul>{''.join(shots)}"
        )
    out = run / "sheet.html"
    text = PAGE.format(
        title=html.escape(title or f"render_compare: {summary.get('build')}"),
        summary=html.escape(
            f"{summary['cars']} cars, {summary['errors']} errors, {summary['seconds']} s; views {', '.join(summary['views'])}"
        ),
        rows="".join(rows),
        cars="".join(cars),
        width=width,
    )
    out.write_text(text, encoding="utf-8")
    return out
