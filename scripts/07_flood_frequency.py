"""Step 07 — flood-frequency quantiles for every NWM v3.0 reach (LP3, Bulletin 17B logic).

Inputs : data/interim/nwm/nwm_v3_annual_max_wy.parquet (water years 1979-2023)
         data/interim/nhdplus/flowline_attributes.parquet (for HUC-4 regional skew)
Output : data/interim/nwm/flood_quantiles.parquet  (feature_id, q2 ... q1000, fit stats)
Only complete water years 1980-2022 are used.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from floodrisk.config import path, return_periods, settings  # noqa: E402
from floodrisk.hazard.fluvial.frequency import lp3_fit, lp3_quantiles  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402


def fill_zero_reaches(out: pd.DataFrame, rps: list[int], log, max_steps: int = 200) -> pd.DataFrame:
    """Reaches without routed NWM flow (lake ArtificialPaths, minor distributaries, masked reaches)
    take the quantiles of the nearest downstream reach with flow, scaled by (DA/DA_ref)^0.7;
    minor divergence paths (Divergence == 2) carry half of that flow."""
    attr = pd.read_parquet(path("interim", "nhdplus", "flowline_attributes.parquet"),
                           columns=["comid", "hydroseq", "dnhydroseq", "totdasqkm", "divergence"])
    qcols = [f"q{T}" for T in rps]
    o = out.set_index("feature_id")
    a = attr.drop_duplicates("comid").set_index("comid")
    a = a.join(o[qcols], how="left")
    pos = a["q100"].fillna(0) > 0
    hs2comid = dict(zip(a.hydroseq.values, a.index.values))
    down = {c: hs2comid.get(d) for c, d in zip(a.index.values, a.dnhydroseq.values)}
    da = a.totdasqkm.to_dict()
    pos_set = set(a.index[pos])
    filled = {}
    for c in a.index[~pos]:
        cur, steps = down.get(c), 0
        while cur is not None and cur not in pos_set and steps < max_steps:
            cur, steps = down.get(cur), steps + 1
        if cur is None or cur not in pos_set:
            continue
        ratio = (max(da.get(c, 0), 0.01) / max(da.get(cur, 0), 0.01)) ** 0.7
        f = min(ratio, 1.5) * (0.5 if a.at[c, "divergence"] == 2 else 1.0)
        filled[c] = a.loc[cur, qcols].to_numpy(dtype=np.float64) * f
    if filled:
        fid = np.fromiter(filled.keys(), dtype=np.int64)
        vals = np.stack(list(filled.values())).astype(np.float32)
        idx = o.index.get_indexer(fid)
        ok = idx >= 0
        arr = o[qcols].to_numpy()
        arr[idx[ok]] = vals[ok]
        o[qcols] = arr
        o["filled_from_downstream"] = False
        o.loc[fid[ok], "filled_from_downstream"] = True
        log.info("filled %d zero-flow reaches from downstream neighbours", int(ok.sum()))
    return o.reset_index()


def main() -> None:
    log = setup("07_flood_frequency")
    wy0, wy1 = settings()["hazard"]["water_years"]
    am = pd.read_parquet(path("interim", "nwm", "nwm_v3_annual_max_wy.parquet"))
    cols = [f"wy{y}" for y in range(wy0, wy1 + 1)]
    q = am[cols].to_numpy(dtype=np.float64)
    fid = am.feature_id.to_numpy()
    log.info("%d reaches x %d water years", *q.shape)

    # pass 1: station skew -> regional (HUC-4 median) skew
    fit0 = lp3_fit(q, 0.0)
    attr = pd.read_parquet(path("interim", "nhdplus", "flowline_attributes.parquet"),
                           columns=["comid", "reachcode", "totdasqkm"])
    huc4 = pd.Series(fid).map(attr.set_index("comid").reachcode.str[:4]).fillna("0000").to_numpy()
    g = pd.DataFrame({"huc4": huc4, "g": fit0["skew_station"], "n": fit0["n"] - fit0["n_zero"]})
    reg = g[(g.n >= 30) & np.isfinite(g.g)].groupby("huc4").g.median().clip(-1.0, 1.0)
    rskew = pd.Series(huc4).map(reg).fillna(0.0).to_numpy()
    log.info("regional skew: %d HUC4 regions (median of medians %.2f)", len(reg), float(reg.median()))

    fit = lp3_fit(q, rskew)
    rps = return_periods()
    Q = lp3_quantiles(fit, rps)
    out = pd.DataFrame({"feature_id": fid})
    for k, T in enumerate(rps):
        out[f"q{T}"] = Q[:, k].astype(np.float32)
    for k in ("n", "n_zero", "n_low", "mean", "std", "skew_station", "skew_weighted"):
        out[k] = fit[k].astype(np.float32)
    out["q_mean_am"] = np.nanmean(q, axis=1).astype(np.float32)
    out = fill_zero_reaches(out, rps, log)
    fp = path("interim", "nwm", "flood_quantiles.parquet")
    out.to_parquet(fp, index=False)
    log.info("wrote %s; median q100/q2 ratio %.2f", fp, float(np.nanmedian(out.q100 / out.q2.replace(0, np.nan))))


if __name__ == "__main__":
    main()
