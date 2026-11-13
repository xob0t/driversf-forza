"""What the CoG height offset of rule CY (0.052 m) could be (research/handling.md, "CoG height
offset: static round", issue #520).

For the 48 vanilla/Forza pairs (`handling_dsf.PAIRS`) it takes the residual of Reflections'
p30 against the physical part of rule CY, `p30 - (hub height + CMHeight - drive tyre radius)`,
and prints its correlation, slope and intercept against each candidate reference: DSF's static
sag (FUN_0088D750), the ride offset and dead zone (p25, p4), DSF's own drive radius (p360),
Forza's ride heights, BlockDim height, bounding box and BottomCenterWheelbasePos. A candidate
that is the offset has slope 1, intercept 0 and a small spread of `residual - candidate`.
Also how many vanilla p30 values are whole centimetres (hand-set values).

    uv run python tools/re/cog_offset.py
"""

from __future__ import annotations

import sqlite3

import numpy as np

from dsfforza.build import handling as H
from dsfforza.build import handling_dsf as D
from dsfforza.convert import wheel_clearance as wc

BODY_FIELDS = (
    "ModelFrontStockRideHeight",
    "ModelRearStockRideHeight",
    "BottomCenterWheelbasePosx",
    "BottomCenterWheelbasePosy",
    "BottomCenterWheelbasePosZ",
)


def _body(car) -> dict:
    con = sqlite3.connect(f"file:{car.source}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        cid = con.execute("SELECT Id FROM Data_Car WHERE MediaName = ? COLLATE NOCASE", (car.media,)).fetchone()[0]
        bid = con.execute("SELECT CarBodyID FROM List_UpgradeCarBody WHERE Ordinal = ? AND IsStock = 1", (cid,))
        row = con.execute("SELECT * FROM Data_CarBody WHERE Id = ?", (bid.fetchone()[0],)).fetchone()
        return {k: row[k] for k in BODY_FIELDS if k in row.keys()}  # noqa: SIM118 - sqlite3.Row has no __contains__ by key
    finally:
        con.close()


def rows() -> list[dict]:
    fleet, dbs, out = D.load_fleet(), D.default_dbs(), []
    for sid, media in D.PAIRS.items():
        if sid not in fleet:
            continue
        f = fleet[sid]

        def v(pid, f=f):
            return D.value(f["params"], pid)

        car = H.load_forza(media, dbs)
        sag = max(v(4), wc.SAG_PER_KG * v(0) / v(1))
        out.append(
            {
                "media": media,
                "residual": v(30) - (f["hub_y"] + car.cm_height - car.drive_radius),
                "sag": sag,
                "p25": v(25),
                "p4": v(4),
                "R - p360": car.drive_radius - v(360),
                "BlockDimY / 2": car.block[1] / 2,
                "bbox min y": car.bbox_min[1],
                "CMHeight": car.cm_height,
                **_body(car),
            }
        )
    return out


def main() -> None:
    data = rows()
    res = np.array([r["residual"] for r in data])
    q = np.percentile(res, [25, 50, 75])
    print(f"{len(data)} pairs; residual median {q[1]:.4f} (IQR {q[0]:.4f}..{q[2]:.4f}), sd {res.std():.4f}")
    print(f"{'candidate':28s} {'median':>8s} {'r':>7s} {'slope':>7s} {'icpt':>8s} {'sd(res - x)':>12s}")
    for key in [k for k in data[0] if k not in ("media", "residual")]:
        x = np.array([float(r.get(key) or 0.0) for r in data])
        if x.std() == 0:
            continue
        slope, icpt = np.polyfit(x, res, 1)
        r = np.corrcoef(x, res)[0, 1]
        print(f"{key:28s} {np.median(x):8.4f} {r:+7.3f} {slope:+7.3f} {icpt:+8.4f} {(res - x).std():12.4f}")
    p30 = np.array([D.value(f["params"], 30) for f in D.load_fleet().values() if f["wheels"] == 4])
    for step in (0.01, 0.05):
        share = np.mean(np.abs(p30 / step - np.round(p30 / step)) < 1e-3)
        print(f"vanilla p30 (n {len(p30)}) multiples of {step} m: {share:.2f}")


if __name__ == "__main__":
    main()
