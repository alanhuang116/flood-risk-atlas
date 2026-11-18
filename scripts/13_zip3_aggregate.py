"""Step 13 (side analysis) — aggregate ZIP results to 3-digit ZIP prefixes (ZIP3).

The Freddie Mac Single-Family Loan-Level Dataset reports only the first three digits of the
property ZIP code ("postal code" = ZIP3 + "00"). This step produces a ZIP3 table that can be
joined to the loan files: sums of exposure and losses, and value-weighted damage ratios.

Output: data/outputs/zip3_flood_risk_long.parquet / .csv
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402


def main() -> None:
    log = setup("13_zip3_aggregate")
    long = pd.read_parquet(path("outputs", "zip_flood_risk_long.parquet"))
    long["zip3"] = long.zip5.str[:3]
    keys = ["zip3", "scenario_id", "ssp", "hazard_year", "peril"]
    sums = ["n_res_buildings", "n_housing_units", "value_total_usd", "aal_usd", "l100_usd", "l1000_usd",
            "n_bldg_flooded_100yr"]
    z3 = long.groupby(keys, as_index=False)[sums].sum()
    z3["n_zip5"] = long.groupby(keys).zip5.nunique().to_numpy()
    for m in ("aal", "l100", "l1000"):
        z3[f"{m}_ratio"] = z3[f"{m}_usd"] / z3.value_total_usd
    z3["pct_bldg_flooded_100yr"] = z3.n_bldg_flooded_100yr / z3.n_res_buildings
    z3["freddie_postal_code"] = z3.zip3 + "00"     # join key used in the Freddie Mac origination file
    out = path("outputs")
    z3.to_parquet(out / "zip3_flood_risk_long.parquet", index=False)
    z3.to_csv(out / "zip3_flood_risk_long.csv", index=False, float_format="%.6g")
    log.info("ZIP3 table: %d prefixes x %d scenario-peril rows", z3.zip3.nunique(), len(z3))


if __name__ == "__main__":
    main()
