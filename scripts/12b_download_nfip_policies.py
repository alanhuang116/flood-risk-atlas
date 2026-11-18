"""Step 12b — NFIP policies in the SFHA (OpenFEMA NfipPolicies v3), for the post-FIRM elevation model.

One cross-section of policy terms: policies effective in calendar 2019, the last full year before Risk
Rating 2.0 (Oct 2021), while post-FIRM SFHA policies were still rated from elevation certificates. Only
A and V zones are kept (the NFIP elevation rules apply there). Per policy: block group (censusGeoid),
rated zone, occupancy, post-FIRM indicator, construction date and the elevation fields
(lowest floor - BFE, basement / enclosure type, elevated-building indicator).

Writes data/raw/nfip/policies_sfha_2019/<ST>.parquet (resumable: finished states are skipped).
Usage: python scripts/12b_download_nfip_policies.py [--workers 4] [--year 2019]
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

API = "https://www.fema.gov/api/open/v3/NfipPolicies"
FIELDS = ["id", "propertyState", "censusGeoid", "reportedZipCode", "nfipCommunityNumberCurrent",
          "ratedFloodZone", "floodZoneCurrent", "occupancyType", "policyCount", "policyEffectiveDate",
          "postFIRMConstructionIndicator", "preFIRMConstructionIndicator", "originalConstructionDate",
          "elevationDifference", "elevatedBuildingIndicator", "basementEnclosureCrawlspaceType",
          "lowestFloorElevation", "baseFloodElevation", "lowestAdjacentGrade", "elevationCertificateIndicator",
          "postFirmVzoneIndicator", "numberOfFloorsInInsuredBuilding", "rateMethod", "foundationType"]
STATES = ["AL", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
          "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
          "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"]


PAGE = 10000


def _get(params: dict, attempts: int = 8) -> dict:
    """GET with retries on broken connections (urllib3 does not retry errors while reading the body)."""
    import time
    for k in range(attempts):
        try:
            r = session().get(API, params=params, timeout=600)
            r.raise_for_status()
            return r.json()
        except Exception:  # noqa: BLE001
            if k == attempts - 1:
                raise
            time.sleep(min(120, 5 * 2 ** k))


def fetch_state(st: str, year: int, page_workers: int = 4) -> tuple[str, int]:
    """One state; queried month by month so that no page needs a large $skip (slow on the server)."""
    out = path("raw", "nfip", f"policies_sfha_{year}", mkdir=True) / f"{st}.parquet"
    if out.exists():
        return st, -1
    zone = "(startswith(ratedFloodZone,'A') or startswith(ratedFloodZone,'V'))"
    jobs = []
    for mth in range(1, 13):
        y1, m1 = (year, mth + 1) if mth < 12 else (year + 1, 1)
        flt = (f"propertyState eq '{st}' and policyEffectiveDate ge '{year}-{mth:02d}-01T00:00:00.000Z' and "
               f"policyEffectiveDate lt '{y1}-{m1:02d}-01T00:00:00.000Z' and {zone}")
        n = int(_get({"$filter": flt, "$top": 1, "$count": "true", "$select": "id"})["metadata"]["count"])
        jobs += [(flt, skip) for skip in range(0, n, PAGE)]
    def page(job) -> list:
        flt, skip = job
        return _get({"$filter": flt, "$select": ",".join(FIELDS), "$orderby": "id", "$top": PAGE,
                     "$skip": skip, "$format": "json"}).get("NfipPolicies", [])

    with ThreadPoolExecutor(page_workers) as ex:
        rows = [r for batch in ex.map(page, jobs) for r in batch]
    atomic_parquet(pd.DataFrame(rows, columns=FIELDS).drop_duplicates("id"), out)
    return st, len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--year", type=int, default=2019)
    args = ap.parse_args()
    log = setup("12b_download_nfip_policies")
    with ThreadPoolExecutor(args.workers) as ex:
        for st, n in ex.map(lambda s: fetch_state(s, args.year), STATES):
            log.info("%s: %s", st, "cached" if n < 0 else f"{n:,} SFHA policies")


if __name__ == "__main__":
    main()
