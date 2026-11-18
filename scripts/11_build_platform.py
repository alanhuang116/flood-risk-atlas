"""Step 11 — build the static data bundle for the web platform (platform/data/).

Files written
  meta.json            scenarios, metrics, map class breaks, states, coverage, version
  zips.json            ZIP index: code, state, dominant county, centroid, exposure
  m/{scen}__{peril}.bin  Float32 [n_zip x 7]: aal_usd, aal_ratio, l100_usd, l100_ratio,
                         l1000_usd, l1000_ratio, pct_bldg_flooded_100yr  (fixed defence crests)
  m/{scen}_adapt__{peril}.bin  the same for raised crests (future scenarios)
  rp/{state}.json      per-ZIP losses at every return period (loss-exceedance curves); raised-crest curves
                       under "{scen}_adapt" for ZIPs with defended homes (others: same as fixed crests)
  zcta.pmtiles         ZIP polygons (vector tiles)
  downloads/           product CSV + data dictionary
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from floodrisk.config import PROJECT_ROOT, path, return_periods, scenarios, settings  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402

PERILS = ["combined", "fluvial", "pluvial", "coastal"]
METRICS = [
    ("aal_usd", "Expected annual loss", "Annual loss", "usd"),
    ("aal_ratio", "Expected annual loss (% of value)", "Annual loss ratio", "pct"),
    ("l100_usd", "1-in-100-year loss", "100-yr loss", "usd"),
    ("l100_ratio", "1-in-100-year loss (% of value)", "100-yr loss ratio", "pct"),
    ("l1000_usd", "1-in-1,000-year loss", "1,000-yr loss", "usd"),
    ("l1000_ratio", "1-in-1,000-year loss (% of value)", "1,000-yr loss ratio", "pct"),
    ("pct_bldg_flooded_100yr", "Homes flooded at 1-in-100 (%)", "Homes flooded", "pct"),
]
STATES = {"01": ("AL", "Alabama"), "02": ("AK", "Alaska"), "04": ("AZ", "Arizona"), "05": ("AR", "Arkansas"),
          "06": ("CA", "California"), "08": ("CO", "Colorado"), "09": ("CT", "Connecticut"), "10": ("DE", "Delaware"),
          "11": ("DC", "District of Columbia"), "12": ("FL", "Florida"), "13": ("GA", "Georgia"), "15": ("HI", "Hawaii"),
          "16": ("ID", "Idaho"), "17": ("IL", "Illinois"), "18": ("IN", "Indiana"), "19": ("IA", "Iowa"),
          "20": ("KS", "Kansas"), "21": ("KY", "Kentucky"), "22": ("LA", "Louisiana"), "23": ("ME", "Maine"),
          "24": ("MD", "Maryland"), "25": ("MA", "Massachusetts"), "26": ("MI", "Michigan"), "27": ("MN", "Minnesota"),
          "28": ("MS", "Mississippi"), "29": ("MO", "Missouri"), "30": ("MT", "Montana"), "31": ("NE", "Nebraska"),
          "32": ("NV", "Nevada"), "33": ("NH", "New Hampshire"), "34": ("NJ", "New Jersey"), "35": ("NM", "New Mexico"),
          "36": ("NY", "New York"), "37": ("NC", "North Carolina"), "38": ("ND", "North Dakota"), "39": ("OH", "Ohio"),
          "40": ("OK", "Oklahoma"), "41": ("OR", "Oregon"), "42": ("PA", "Pennsylvania"), "44": ("RI", "Rhode Island"),
          "45": ("SC", "South Carolina"), "46": ("SD", "South Dakota"), "47": ("TN", "Tennessee"), "48": ("TX", "Texas"),
          "49": ("UT", "Utah"), "50": ("VT", "Vermont"), "51": ("VA", "Virginia"), "53": ("WA", "Washington"),
          "54": ("WV", "West Virginia"), "55": ("WI", "Wisconsin"), "56": ("WY", "Wyoming"), "72": ("PR", "Puerto Rico"),
          "78": ("VI", "U.S. Virgin Islands"), "66": ("GU", "Guam"), "60": ("AS", "American Samoa"), "69": ("MP", "Northern Mariana Islands")}


def nice(v: float) -> float:
    if v <= 0 or not np.isfinite(v):
        return v
    e = np.floor(np.log10(v))
    m = v / 10 ** e
    for step in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5, 10):
        if m <= step * 1.0001:
            return float(step * 10 ** e)
    return float(10 ** (e + 1))


def breaks_for(values: np.ndarray, k: int = 7) -> list[float]:
    v = values[np.isfinite(values) & (values > 0)]
    if len(v) < 10:
        return [1e3, 1e4, 1e5, 1e6, 1e7, 1e8]
    qs = np.quantile(v, [i / k for i in range(1, k)])
    out = []
    for q in qs:
        b = nice(q)
        if out and b <= out[-1]:
            b = nice(out[-1] * 1.5)
        out.append(b)
    return out


def main() -> None:
    from floodrisk.config import paused
    if paused("11_build_platform"):
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("--coverage", default="United States")
    ap.add_argument("--exposure-root", default=None)
    args = ap.parse_args()
    log = setup("11_build_platform")
    out = PROJECT_ROOT / "platform" / "data"
    (out / "m").mkdir(parents=True, exist_ok=True)
    (out / "rp").mkdir(parents=True, exist_ok=True)
    (out / "downloads").mkdir(parents=True, exist_ok=True)

    long = pd.read_parquet(path("outputs", "zip_flood_risk_long.parquet"))
    if "defence_scenario" not in long.columns:
        long["defence_scenario"] = "fixed-crest"
    zips = (long[["zip5", "state_fips", "n_res_buildings", "n_housing_units", "value_total_usd"]]
            .drop_duplicates("zip5").sort_values("zip5").reset_index(drop=True))
    zi = {z: i for i, z in enumerate(zips.zip5)}
    n = len(zips)

    # dominant county + centroid from the building stock
    root = Path(args.exposure_root) if args.exposure_root else path("processed", "exposure")
    glob = (root / "state=*" / "residential.parquet").as_posix()
    g = duckdb.sql(f"""SELECT zcta, mode(county_fips) AS cty, avg(x) AS lon, avg(y) AS lat
                       FROM read_parquet('{glob}', hive_partitioning=true) WHERE zcta IS NOT NULL GROUP BY zcta""").df()
    names = pd.read_csv(path("raw", "census", "national_county2020.txt"), sep="|", dtype=str)
    names["fips"] = names.STATEFP + names.COUNTYFP
    cmap = dict(zip(names.fips, names.COUNTYNAME))
    g = g.set_index("zcta").reindex(zips.zip5)
    zjson = {
        "zip": zips.zip5.tolist(), "st": zips.state_fips.tolist(),
        "cty": [cmap.get(c, "") for c in g.cty.fillna("")],
        "lon": np.round(g.lon.to_numpy(), 4).tolist(), "lat": np.round(g.lat.to_numpy(), 4).tolist(),
        "nb": zips.n_res_buildings.astype(int).tolist(), "nu": zips.n_housing_units.round().astype(int).tolist(),
        "val": np.round(zips.value_total_usd.to_numpy(), -2).tolist(),
    }
    (out / "zips.json").write_text(json.dumps(zjson, separators=(",", ":")))

    # metric binaries
    pooled = {m[0]: [] for m in METRICS}
    has_adapt = (long.defence_scenario == "raised-crest").any()
    for dlab, suf in (("fixed-crest", ""), ("raised-crest", "_adapt")):
        if suf and not has_adapt:
            continue
        for sid in scenarios():
            if suf and sid == "baseline":
                continue
            for p in PERILS:
                d = long[(long.defence_scenario == dlab) & (long.scenario_id == sid) &
                         (long.peril == p)].set_index("zip5").reindex(zips.zip5)
                arr = np.zeros((n, len(METRICS)), dtype=np.float32)
                for k, (key, *_r) in enumerate(METRICS):
                    v = d[key].to_numpy(dtype=np.float64)
                    arr[:, k] = np.nan_to_num(v, nan=0.0)
                    pooled[key].append(v)
                (out / "m" / f"{sid}{suf}__{p}.bin").write_bytes(arr.tobytes())
    brk = {k: breaks_for(np.concatenate(v)) for k, v in pooled.items()}

    # loss-exceedance curves per state. Raised-crest partial sums exist only for HUC-8 parts of a ZCTA with
    # defended homes; the other parts of such a ZCTA contribute their fixed-crest losses (identical there).
    src = (path("processed", "risk", "zcta_rp") / "*.parquet").as_posix()
    zrp = duckdb.sql(f"""
        WITH z AS (SELECT * FROM read_parquet('{src}')),
             f AS (SELECT * FROM z WHERE NOT ends_with(scenario_id, '_adapt')),
             a AS (SELECT huc8, zcta, peril, replace(scenario_id, '_adapt', '') AS sid, rp, loss_usd
                   FROM z WHERE ends_with(scenario_id, '_adapt')),
             za AS (SELECT DISTINCT zcta FROM a)
        SELECT zcta, peril, scenario_id, rp, sum(loss_usd) AS loss FROM f GROUP BY ALL
        UNION ALL
        SELECT f.zcta, f.peril, f.scenario_id || '_adapt' AS scenario_id, f.rp,
               sum(coalesce(a.loss_usd, f.loss_usd)) AS loss
        FROM f JOIN za USING (zcta)
        LEFT JOIN a ON a.huc8 = f.huc8 AND a.zcta = f.zcta AND a.peril = f.peril AND a.sid = f.scenario_id
                   AND a.rp = f.rp
        WHERE f.scenario_id <> 'baseline'
        GROUP BY ALL""").df()
    rps = return_periods()
    zrp = zrp[zrp.zcta.isin(zi)]
    zrp["st"] = zrp.zcta.map(dict(zip(zips.zip5, zips.state_fips)))
    for st, part in zrp.groupby("st"):
        rec: dict = {}
        piv = part.pivot_table(index=["zcta", "scenario_id", "peril"], columns="rp", values="loss", aggfunc="sum")
        piv = piv.reindex(columns=rps).fillna(0.0)
        for (z, s, p), row in piv.iterrows():
            rec.setdefault(z, {}).setdefault(s, {})[p] = [round(float(x), 0) for x in row.to_numpy()]
        (out / "rp" / f"{st}.json").write_text(json.dumps(rec, separators=(",", ":")))

    # states + bbox
    states = []
    for st, part in pd.DataFrame({"st": zjson["st"], "lon": zjson["lon"], "lat": zjson["lat"]}).dropna().groupby("st"):
        ab, nm = STATES.get(st, (st, st))
        states.append({"fips": st, "abbr": ab, "name": nm,
                       "bbox": [float(part.lon.quantile(0.005)) - 0.2, float(part.lat.quantile(0.005)) - 0.2,
                                float(part.lon.quantile(0.995)) + 0.2, float(part.lat.quantile(0.995)) + 0.2]})
    lon, lat = np.array(zjson["lon"], float), np.array(zjson["lat"], float)
    ok = np.isfinite(lon) & np.isfinite(lat)
    if args.coverage == "United States":
        view = {"center": [-96.5, 38.6], "zoom": 3.6}
    else:
        view = {"center": [float(np.median(lon[ok])), float(np.median(lat[ok]))], "zoom": 8.2}

    sc = [{"id": k, "ssp": v["ssp"] or "present", "year": int(v["year"]), "label": v["label"]} for k, v in scenarios().items()]
    defences = [{"id": "fixed", "label": "Fixed crest", "desc": "Flood defences keep today's crest height"}]
    if has_adapt:
        defences.append({"id": "adapt", "label": "Raised crest",
                         "desc": "Flood defences are raised with the hazard and keep their design standard"})
    meta = {
        "version": settings()["project"]["version"], "generated": date.today().isoformat(),
        "coverage_label": args.coverage, "default_view": view, "zip_count": n,
        "scenarios": sc, "defences": defences, "perils": PERILS, "rps": rps,
        "metrics": [{"key": k, "label": lab, "short": sh, "unit": u, "col": i} for i, (k, lab, sh, u) in enumerate(METRICS)],
        "breaks": brk, "change_breaks": [-40, -15, -3, 3, 15, 40], "states": states,
        "price_level": settings()["exposure"]["price_level"], "exposure": settings()["exposure"]["nsi_version"],
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1))

    pm = path("outputs", "platform", "zcta.pmtiles")
    if pm.exists() and (not (out / "zcta.pmtiles").exists() or pm.stat().st_mtime > (out / "zcta.pmtiles").stat().st_mtime):
        shutil.copy2(pm, out / "zcta.pmtiles")
    for f in ("zip_flood_risk_long.csv",):
        src = path("outputs", f)
        if src.exists():
            shutil.copy2(src, out / "downloads" / f)
    dd = PROJECT_ROOT / "docs" / "data_dictionary.csv"
    if dd.exists():
        shutil.copy2(dd, out / "downloads" / "data_dictionary.csv")
    (PROJECT_ROOT / "platform" / "docs").mkdir(exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "docs" / "methodology.md", PROJECT_ROOT / "platform" / "docs" / "methodology.md")
    log.info("platform bundle: %d ZIPs, %d states -> %s", n, len(states), out)


if __name__ == "__main__":
    main()
