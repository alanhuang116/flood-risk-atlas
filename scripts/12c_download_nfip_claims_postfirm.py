"""Step 12c — SFHA residential NFIP claims with the post-FIRM indicator (check of the post-FIRM model, §10.2).

Writes data/raw/nfip/claims_sfha_postfirm/<ST>.parquet (resumable); queries by state and 5-year loss-year blocks so
that no page needs a large $skip. Usage: python scripts/12c_download_nfip_claims_postfirm.py
"""
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from floodrisk.config import path  # noqa: E402
from floodrisk.utils.http import atomic_parquet, session  # noqa: E402

API = "https://www.fema.gov/api/open/v3/NfipClaims"
FIELDS = ["id", "state", "countyCode", "yearOfLoss", "ratedFloodZone", "occupancyType", "postFIRMConstructionIndicator",
          "buildingDamageAmount", "contentsDamageAmount", "elevationDifference", "originalConstructionDate",
          "causeOfDamage"]
STATES = ["AL", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
          "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
          "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"]
OCC = "(" + " or ".join(f"occupancyType eq {o}" for o in (1, 2, 3, 11, 12, 13, 14, 15, 16)) + ")"


def get(params, attempts=8):
    for k in range(attempts):
        try:
            r = session().get(API, params=params, timeout=600)
            r.raise_for_status()
            return r.json()
        except Exception:  # noqa: BLE001
            if k == attempts - 1:
                raise
            time.sleep(min(120, 5 * 2 ** k))


def fetch(st):
    out = path("raw", "nfip", "claims_sfha_postfirm", mkdir=True) / f"{st}.parquet"
    if out.exists():
        return st, -1
    jobs = []
    for y0 in range(1980, 2025, 5):
        flt = (f"state eq '{st}' and yearOfLoss ge {y0} and yearOfLoss le {min(y0 + 4, 2024)} and {OCC} and "
               f"(startswith(ratedFloodZone,'A') or startswith(ratedFloodZone,'V'))")
        n = int(get({"$filter": flt, "$top": 1, "$count": "true", "$select": "id"})["metadata"]["count"])
        jobs += [(flt, s) for s in range(0, n, 10000)]
    def page(j):
        flt, s = j
        return get({"$filter": flt, "$select": ",".join(FIELDS), "$orderby": "id", "$top": 10000, "$skip": s}).get("NfipClaims", [])
    with ThreadPoolExecutor(4) as ex:
        rows = [r for b in ex.map(page, jobs) for r in b]
    atomic_parquet(pd.DataFrame(rows, columns=FIELDS).drop_duplicates("id"), out)
    return st, len(rows)


if __name__ == "__main__":
    with ThreadPoolExecutor(3) as ex:
        for st, n in ex.map(fetch, STATES):
            print(st, n, flush=True)
