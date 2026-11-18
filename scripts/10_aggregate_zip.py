"""Step 10 — aggregate building-level risk to 5-digit ZIP (2020 ZCTA) -> product tables.

Outputs (data/outputs/):
  zip_flood_risk_long.parquet / .csv   one row per ZIP x defence scenario x scenario-year x peril (product)
  zip_flood_risk_wide.parquet          one row per ZIP, all metrics as columns (platform)
  summary_state.csv, summary_national.csv
Defence scenarios: "fixed-crest" (defences keep today's crest; default) and "raised-crest" (defences are
raised with the hazard so that they keep their design standard; data/processed/risk/buildings_adapt).
Both carry the present-climate rows (identical) so that each is a complete set.
Damage ratios are value-weighted: sum(loss) / sum(full replacement value of structure + contents).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from floodrisk.config import path, scenarios, settings  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402

PERILS = ["fluvial", "pluvial", "coastal", "combined"]
DEFENDED_PERILS = ("fluvial", "coastal", "combined")     # perils the raised-crest variant changes
METRICS = (("aal", "aal"), ("l100", "l100"), ("l1000", "l1000"))
DEFENCE = {"fixed": "fixed-crest", "adapt": "raised-crest"}


def main() -> None:
    from floodrisk.config import paused
    if paused("10_aggregate_zip"):
        return
    log = setup("10_aggregate_zip")
    scen = scenarios()
    future = [s for s in scen if s != "baseline"]
    from floodrisk.pipeline import exposure_root
    rdir = path("processed", "risk", "buildings")
    adir = path("processed", "risk", "buildings_adapt")
    risk = (rdir / "*.parquet").as_posix()
    expo = (exposure_root() / "state=*" / "residential.parquet").as_posix()
    has_adapt = adir.exists() and any(adir.glob("*.parquet"))
    sums = []
    for p in PERILS:
        for s in scen:
            for m, col in METRICS:
                sums.append(f"sum(r.{col}_{p}_{s}) AS {m}__{p}__{s}")
            sums.append(f"sum(CASE WHEN r.d100_{p}_{s} > 0 THEN 1 ELSE 0 END) AS nfl100__{p}__{s}")
            if has_adapt and s in future and p in DEFENDED_PERILS:      # raised-crest variant
                for m, col in METRICS:
                    sums.append(f"sum(coalesce(a.{col}_{p}_{s}, r.{col}_{p}_{s})) AS {m}__{p}__{s}_adapt")
                sums.append(f"sum(CASE WHEN coalesce(a.d100_{p}_{s}, r.d100_{p}_{s}) > 0 THEN 1 ELSE 0 END) "
                            f"AS nfl100__{p}__{s}_adapt")
    cols = duckdb.sql(f"DESCRIBE SELECT * FROM read_parquet('{risk}') LIMIT 1").df().column_name.tolist()
    post = "sum(coalesce(r.p_postfirm, 0))" if "p_postfirm" in cols else "0"
    join_a = (f"LEFT JOIN read_parquet('{(adir / '*.parquet').as_posix()}') a USING (fd_id)" if has_adapt else "")
    sql = f"""
      SELECT r.zcta AS zip5,
             mode(e.state_fips) AS state_fips,
             count(*) AS n_res_buildings,
             sum(coalesce(e.resunits, 1)) AS n_housing_units,
             sum(e.fullrep) AS value_struct_usd,
             sum(e.cont_fullrep) AS value_cont_usd,
             sum(e.val_struct) AS value_struct_depr_usd,
             sum(e.pop_night) AS pop_night,
             sum(CASE WHEN greatest(coalesce(r.protect_rp_fluvial, 0), coalesce(r.protect_rp_coastal, 0)) > 0
                      THEN 1 ELSE 0 END) AS n_bldg_defended,
             {post} AS n_bldg_postfirm,
             {', '.join(sums)}
      FROM read_parquet('{risk}') r
      JOIN read_parquet('{expo}', hive_partitioning=true) e USING (fd_id)
      {join_a}
      WHERE r.zcta IS NOT NULL
      GROUP BY r.zcta
    """
    con = duckdb.connect()
    con.execute("PRAGMA threads=16")
    wide = con.execute(sql).df()
    wide["value_total_usd"] = wide.value_struct_usd + wide.value_cont_usd
    log.info("aggregated %d ZIPs, %s buildings (raised-crest variant: %s)", len(wide),
             f"{int(wide.n_res_buildings.sum()):,}", "yes" if has_adapt else "no")
    # complete the variant: present climate and the perils defences do not change are those of fixed crests
    for p in PERILS:
        for s in scen:
            for m in ("aal", "l100", "l1000", "nfl100"):
                k = f"{m}__{p}__{s}_adapt"
                if k not in wide.columns:
                    wide[k] = wide[f"{m}__{p}__{s}"]

    # ---- long product table
    meta_cols = ["zip5", "state_fips", "n_res_buildings", "n_housing_units", "value_struct_usd",
                 "value_cont_usd", "value_total_usd", "value_struct_depr_usd", "pop_night", "n_bldg_defended",
                 "n_bldg_postfirm"]
    rows = []
    for dk, dlab in DEFENCE.items():
        suf = "" if dk == "fixed" else "_adapt"
        for sid, sv in scen.items():
            for p in PERILS:
                d = wide[meta_cols].copy()
                d.insert(1, "defence_scenario", dlab)
                d.insert(2, "scenario_id", sid)
                d.insert(3, "ssp", sv["ssp"] or "present")
                d.insert(4, "hazard_year", int(sv["year"]))
                d.insert(5, "peril", p)
                d["aal_usd"] = wide[f"aal__{p}__{sid}{suf}"]
                d["l100_usd"] = wide[f"l100__{p}__{sid}{suf}"]
                d["l1000_usd"] = wide[f"l1000__{p}__{sid}{suf}"]
                d["n_bldg_flooded_100yr"] = wide[f"nfl100__{p}__{sid}{suf}"].astype(int)
                rows.append(d)
    long = pd.concat(rows, ignore_index=True)
    v = long.value_total_usd.replace(0, np.nan)
    long["aal_ratio"] = long.aal_usd / v
    long["l100_ratio"] = long.l100_usd / v
    long["l1000_ratio"] = long.l1000_usd / v
    long["pct_bldg_flooded_100yr"] = long.n_bldg_flooded_100yr / long.n_res_buildings
    long["exposure_vintage"] = settings()["exposure"]["nsi_version"]
    long["price_level"] = settings()["exposure"]["price_level"]
    long["model_version"] = settings()["project"]["version"]

    out = path("outputs", mkdir=True)
    long.to_parquet(out / "zip_flood_risk_long.parquet", index=False)
    long.to_csv(out / "zip_flood_risk_long.csv", index=False, float_format="%.6g")
    wide.to_parquet(out / "zip_flood_risk_wide.parquet", index=False)

    # ---- summaries
    agg = {"n_res_buildings": "sum", "n_housing_units": "sum", "value_total_usd": "sum", "aal_usd": "sum",
           "l100_usd": "sum", "l1000_usd": "sum", "n_bldg_flooded_100yr": "sum", "n_bldg_postfirm": "sum"}
    nat = long.groupby(["defence_scenario", "scenario_id", "hazard_year", "peril"]).agg(agg).reset_index()
    nat["aal_ratio"] = nat.aal_usd / nat.value_total_usd
    nat.to_csv(out / "summary_national.csv", index=False)
    st = long.groupby(["state_fips", "defence_scenario", "scenario_id", "peril"]).agg(agg).reset_index()
    st["aal_ratio"] = st.aal_usd / st.value_total_usd
    st.to_csv(out / "summary_state.csv", index=False)
    q = nat.query("peril=='combined' and defence_scenario=='fixed-crest'").set_index("scenario_id").aal_usd / 1e9
    qa = nat.query("peril=='combined' and defence_scenario=='raised-crest'").set_index("scenario_id").aal_usd / 1e9
    log.info("national AAL (combined), US$ bn - fixed crest: %s; raised crest: %s",
             q.round(2).to_dict(), qa.round(2).to_dict())


if __name__ == "__main__":
    main()
