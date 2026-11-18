"""Step 04b — post-FIRM construction in the SFHA, from NFIP policies (input of the elevation model).

NFIP floodplain rules (44 CFR 60.3) require homes built in the SFHA after the community's first FIRM
(post-FIRM) to have the lowest floor - basement included - at or above the base flood elevation (BFE);
in V zones the lowest horizontal member, on an open pile / column foundation. The NSI does not apply
this (its foundation heights are the same inside and outside the SFHA), so the loss model treats it
explicitly (risk/engine.py, ``postfirm_elevation``). This step estimates, from the 2019 cross-section of
NFIP policies in A and V zones (scripts/12b), two inputs:

  1. p_post: the share of SFHA homes that are post-FIRM, per census block group. Policy counts are
     pooled hierarchically (block group -> county -> state -> nation) with ``PRIOR_N`` pseudo-policies
     of the next level, so sparse block groups inherit the county share.
  2. the distribution of (lowest floor - BFE) of post-FIRM homes, by zone (A / V), summarised by its
     terciles (three equally weighted classes, centred at the 1/6, 1/2 and 5/6 quantiles). Negative
     values (homes below today's BFE: map revisions after construction, non-compliance) are kept.

Writes data/interim/postfirm/bg_postfirm.parquet (bg12, p_post, n_pol) and
       data/interim/postfirm/elevation_classes.json; summary in logs.
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.http import atomic_parquet  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402

RES_OCC = {1, 2, 3, 11, 12, 13, 14, 15, 16}
PRIOR_N = 10.0                      # pseudo-policies of the parent level in each pooled share
ELEV_RANGE = (-15.0, 25.0)          # plausible (lowest floor - BFE), ft; outside = sentinel / data error
QUANTS = (1 / 6, 1 / 2, 5 / 6)      # class centres of the three equally weighted elevation classes


def load_policies(year: int = 2019) -> pd.DataFrame:
    fs = sorted(glob.glob(str(path("raw", "nfip", f"policies_sfha_{year}") / "*.parquet")))
    d = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    d = d[d.occupancyType.isin(RES_OCC)].copy()
    # rated zones: A, AE, AH, AO, AHB, AOB, A01-A30, V, VE, V01-V30 (not A99 / AR: levee zones without the rule)
    z = d.ratedFloodZone.fillna("").str.upper().str.strip()
    is_a = z.str.fullmatch(r"A(E|H|O|HB|OB|\d{1,2})?")
    is_v = z.str.fullmatch(r"V(E|\d{1,2})?")
    d["zone"] = np.where(is_v, "V", "A")
    d = d[is_a | is_v]
    g = pd.to_numeric(d.censusGeoid, errors="coerce")
    d["bg12"] = g.astype("Int64").astype(str).str.zfill(12).where(g.notna())   # leading zero lost for FIPS<10
    d["post"] = d.postFIRMConstructionIndicator.fillna(False).astype(bool)
    return d


def main() -> None:
    log = setup("04b_postfirm")
    d = load_policies()
    log.info("residential SFHA policies 2019: %s (A %s, V %s); post-FIRM %.1f %%", f"{len(d):,}",
             f"{(d.zone == 'A').sum():,}", f"{(d.zone == 'V').sum():,}", 100 * d.post.mean())
    d = d[d.bg12.notna()].copy()
    d["st"], d["cty"] = d.bg12.str[:2], d.bg12.str[:5]
    p_nat = d.post.mean()
    st = d.groupby("st").agg(n=("post", "size"), k=("post", "sum"))
    st["p"] = (st.k + PRIOR_N * p_nat) / (st.n + PRIOR_N)
    cty = d.groupby("cty").agg(n=("post", "size"), k=("post", "sum"))
    cty["p"] = (cty.k + PRIOR_N * st.p.reindex(cty.index.str[:2]).fillna(p_nat).to_numpy()) / (cty.n + PRIOR_N)
    bg = d.groupby("bg12").agg(n=("post", "size"), k=("post", "sum"))

    # every block group with SFHA homes in the exposure (sparse ones inherit the county / state share)
    import duckdb
    expo = (path("processed", "exposure") / "state=*" / "residential.parquet").as_posix()
    from floodrisk.exposure.zones import ELEVATION_RULE, sql_in
    eb = duckdb.sql(f"""SELECT substr(cbfips, 1, 12) AS bg12, count(*) AS n_sfha FROM read_parquet('{expo}',
                        hive_partitioning=true) WHERE upper(trim(firmzone)) IN {sql_in(ELEVATION_RULE)}
                        GROUP BY 1""").df()
    out = eb.merge(bg.reset_index(), on="bg12", how="left").fillna({"n": 0, "k": 0})
    p_cty = cty.p.reindex(out.bg12.str[:5]).to_numpy()
    p_st = st.p.reindex(out.bg12.str[:2]).fillna(p_nat).to_numpy()
    prior = np.where(np.isfinite(p_cty), p_cty, p_st)
    out["p_post"] = ((out.k + PRIOR_N * prior) / (out.n + PRIOR_N)).astype("float32")
    out = out.rename(columns={"n": "n_pol"})[["bg12", "p_post", "n_pol", "n_sfha"]]
    od = path("interim", "postfirm", mkdir=True)
    atomic_parquet(out, od / "bg_postfirm.parquet")
    w = out.n_sfha / out.n_sfha.sum()
    log.info("block groups with SFHA homes: %s (%.0f %% with >= 1 policy); building-weighted p_post %.3f",
             f"{len(out):,}", 100 * (out.n_pol > 0).mean(), float((w * out.p_post).sum()))
    log.info("state p_post:\n%s", st.assign(p=st.p.round(3)).sort_values("n", ascending=False).head(20).to_string())

    # elevation of post-FIRM homes relative to the BFE (ft), by zone
    e = pd.to_numeric(d.elevationDifference, errors="coerce")
    ok = d.post & e.between(*ELEV_RANGE)
    classes = {}
    for zone in ("A", "V"):
        v = e[ok & (d.zone == zone)].to_numpy(float)
        q = np.quantile(v, QUANTS)
        classes[zone] = {"elev_ft": [round(float(x), 2) for x in q], "weight": [1 / 3] * 3, "n": int(len(v)),
                         "mean": round(float(v.mean()), 2), "share_below_bfe": round(float((v < 0).mean()), 3)}
        log.info("post-FIRM %s zone: n=%s lowest floor - BFE terciles %s ft, mean %.2f, below BFE %.1f %%",
                 zone, f"{len(v):,}", classes[zone]["elev_ft"], v.mean(), 100 * (v < 0).mean())
    bs = pd.to_numeric(d.basementEnclosureCrawlspaceType, errors="coerce")
    for flag, lab in ((True, "post-FIRM"), (False, "pre-FIRM")):
        m = (d.post == flag) & bs.notna() & (d.zone == "A")
        log.info("%s A zone: basement / enclosure type shares %s", lab,
                 bs[m].value_counts(normalize=True).round(3).to_dict())
    pre_e = e[~d.post & e.between(*ELEV_RANGE)]
    log.info("pre-FIRM homes with an elevation record: n=%s median %.1f ft", f"{len(pre_e):,}", pre_e.median())
    meta = {"source": "OpenFEMA NfipPolicies v3, residential policies in A/V zones effective in 2019",
            "prior_n": PRIOR_N, "quantiles": QUANTS, "national_p_post": round(float(p_nat), 4), "classes": classes}
    (od / "elevation_classes.json").write_text(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
