"""Step 12 — validate modelled AAL against NFIP claims (OpenFEMA v3).

Empirical insured damage per ZIP = sum of building + contents *damage* amounts of residential
claims, CPI-adjusted to 2025 USD, averaged over 1980-2024 (45 years). It is divided by the
county residential NFIP penetration rate (OpenFEMA NfipResidentialPenetrationRates) to give an
indicative total residential flood damage per year, compared with modelled AAL (baseline).
Claims are also split by cause: tidal (coastal), river overflow (fluvial), rainfall (pluvial).

Usage: python scripts/12_validate_nfip.py --counties 48201 48167 ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.http import session  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402

API = "https://www.fema.gov/api/open/v3/NfipClaims"
API_PEN = "https://www.fema.gov/api/open/v1/NfipResidentialPenetrationRates"
CPI = {1978: 65.2, 1979: 72.6, 1980: 82.4, 1981: 90.9, 1982: 96.5, 1983: 99.6, 1984: 103.9, 1985: 107.6,
       1986: 109.6, 1987: 113.6, 1988: 118.3, 1989: 124.0, 1990: 130.7, 1991: 136.2, 1992: 140.3, 1993: 144.5,
       1994: 148.2, 1995: 152.4, 1996: 156.9, 1997: 160.5, 1998: 163.0, 1999: 166.6, 2000: 172.2, 2001: 177.1,
       2002: 179.9, 2003: 184.0, 2004: 188.9, 2005: 195.3, 2006: 201.6, 2007: 207.3, 2008: 215.3, 2009: 214.5,
       2010: 218.1, 2011: 224.9, 2012: 229.6, 2013: 233.0, 2014: 236.7, 2015: 237.0, 2016: 240.0, 2017: 245.1,
       2018: 251.1, 2019: 255.7, 2020: 258.8, 2021: 271.0, 2022: 292.7, 2023: 304.7, 2024: 313.7, 2025: 322.0}
RES_OCC = {1, 2, 3, 11, 12, 13, 14, 15, 16}
CAUSE = {"1": "coastal", "2": "fluvial", "3": "fluvial", "4": "pluvial"}
FIELDS = ["reportedZipCode", "countyCode", "yearOfLoss", "occupancyType", "causeOfDamage",
          "buildingDamageAmount", "contentsDamageAmount", "amountPaidOnBuildingClaim", "amountPaidOnContentsClaim",
          "floodEvent"]


def fetch_claims(county: str) -> pd.DataFrame:
    out = path("raw", "nfip", mkdir=True) / f"claims_{county}.parquet"
    if out.exists():
        return pd.read_parquet(out)
    rows, skip = [], 0
    while True:
        r = session().get(API, params={"$filter": f"countyCode eq '{county}'", "$select": ",".join(FIELDS),
                                        "$top": 10000, "$skip": skip, "$format": "json"}, timeout=180)
        r.raise_for_status()
        batch = r.json().get("NfipClaims", [])
        rows += batch
        if len(batch) < 10000:
            break
        skip += 10000
    df = pd.DataFrame(rows, columns=FIELDS)
    df.to_parquet(out, index=False)
    return df


def penetration() -> pd.DataFrame:
    out = path("raw", "nfip", mkdir=True) / "residential_penetration.parquet"
    if out.exists():
        return pd.read_parquet(out)
    r = session().get(API_PEN, params={"$top": 10000, "$format": "json"}, timeout=120).json()
    df = pd.DataFrame(r["NfipResidentialPenetrationRates"])
    df.to_parquet(out, index=False)
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--counties", nargs="+", required=True)
    ap.add_argument("--y0", type=int, default=1980)
    ap.add_argument("--y1", type=int, default=2024)
    args = ap.parse_args()
    log = setup("12_validate_nfip")
    cl = pd.concat([fetch_claims(c) for c in args.counties], ignore_index=True)
    cl = cl[cl.occupancyType.isin(RES_OCC) & cl.yearOfLoss.between(args.y0, args.y1)].copy()
    f = cl.yearOfLoss.map(lambda y: CPI[2025] / CPI.get(int(y), CPI[2025]))
    cl["damage_2025"] = (cl.buildingDamageAmount.fillna(0) + cl.contentsDamageAmount.fillna(0)) * f
    cl["peril"] = cl.causeOfDamage.astype(str).map(CAUSE).fillna("other")
    nyr = args.y1 - args.y0 + 1
    log.info("%d residential claims %d-%d, damage $%.2fB (2025 USD)", len(cl), args.y0, args.y1, cl.damage_2025.sum() / 1e9)
    log.info("top events: %s", cl.groupby("floodEvent").damage_2025.sum().sort_values(ascending=False).head(6).round(-6).to_dict())

    pen = penetration()
    # share of ALL residential structures with an NFIP contract (current, 2026)
    pen_map = pen.set_index("fipsCode")["resPenetrationRate"].astype(float).to_dict()
    log.info("penetration (all residential): %s", {c: pen_map.get(c) for c in args.counties})

    z = cl.groupby(["reportedZipCode", "countyCode"]).damage_2025.sum().reset_index()
    z["pen"] = z.countyCode.map(pen_map).astype(float)
    z["pen"] = np.where(z.pen > 1, z.pen / 100.0, z.pen)       # percent -> fraction if needed
    z["claims_aal"] = z.damage_2025 / nyr
    z["est_total_aal"] = z.claims_aal / z.pen.clip(lower=0.02)
    zz = z.groupby("reportedZipCode").agg(claims_aal=("claims_aal", "sum"), est_total_aal=("est_total_aal", "sum")).reset_index()

    long = pd.read_parquet(path("outputs", "zip_flood_risk_long.parquet"))
    m = long[(long.scenario_id == "baseline")].pivot_table(index="zip5", columns="peril", values="aal_usd").reset_index()
    j = m.merge(zz, left_on="zip5", right_on="reportedZipCode", how="left").fillna({"claims_aal": 0, "est_total_aal": 0})
    from scipy.stats import spearmanr
    ok = (j.combined > 0) | (j.est_total_aal > 0)
    rho = spearmanr(j.combined[ok], j.est_total_aal[ok]).statistic
    res = {"zips": int(ok.sum()), "spearman_combined_vs_claims": round(float(rho), 3),
           "modelled_aal_total": float(j.combined.sum()), "claims_aal_total": float(j.claims_aal.sum()),
           "penetration_adjusted_total": float(j.est_total_aal.sum())}
    byp = cl.groupby("peril").damage_2025.sum() / nyr
    res["claims_aal_by_cause"] = byp.round(0).to_dict()
    res["modelled_aal_by_peril"] = {p: float(m[p].sum()) for p in ("fluvial", "pluvial", "coastal", "combined") if p in m}
    outd = path("outputs", "validation", mkdir=True)
    j.to_csv(outd / "nfip_vs_model_by_zip.csv", index=False)
    (outd / "nfip_validation.json").write_text(json.dumps(res, indent=2))
    log.info("validation: %s", json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
