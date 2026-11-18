"""Step 12a — download all NFIP claims (OpenFEMA NfipClaims v3) by state, for national validation.

Writes data/raw/nfip/state/claims_<ST>.parquet (resumable: finished states are skipped).
Usage: python scripts/12a_download_nfip_national.py [--workers 4]
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.http import atomic_parquet, session  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402

API = "https://www.fema.gov/api/open/v3/NfipClaims"
FIELDS = ["id", "state", "reportedZipCode", "countyCode", "yearOfLoss", "occupancyType", "causeOfDamage",
          "buildingDamageAmount", "contentsDamageAmount", "amountPaidOnBuildingClaim",
          "amountPaidOnContentsClaim", "floodEvent", "primaryResidenceIndicator"]
STATES = ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
          "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
          "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
          "PR", "VI", "GU", "AS", "MP"]


def fetch_state(st: str) -> tuple[str, int]:
    out = path("raw", "nfip", "state", mkdir=True) / f"claims_{st}.parquet"
    if out.exists():
        return st, -1
    rows, skip = [], 0
    while True:
        r = session().get(API, params={"$filter": f"state eq '{st}'", "$select": ",".join(FIELDS),
                                        "$orderby": "id", "$top": 10000, "$skip": skip, "$format": "json"},
                          timeout=300)
        r.raise_for_status()
        batch = r.json().get("NfipClaims", [])
        rows += batch
        if len(batch) < 10000:
            break
        skip += 10000
    atomic_parquet(pd.DataFrame(rows, columns=FIELDS), out)
    return st, len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    log = setup("12a_download_nfip")
    with ThreadPoolExecutor(args.workers) as ex:
        for st, n in ex.map(fetch_state, STATES):
            log.info("%s: %s", st, "cached" if n < 0 else f"{n:,} claims")


if __name__ == "__main__":
    main()
