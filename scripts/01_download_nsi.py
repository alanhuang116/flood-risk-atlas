"""Step 01 — download NSI 2026 for every US county/territory (resumable).

Usage:  python scripts/01_download_nsi.py [--workers 4] [--fips 48167 48201 ...]
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from floodrisk.config import path  # noqa: E402
from floodrisk.exposure.nsi import county_list, download_county  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--fips", nargs="*", help="subset of 5-digit county FIPS")
    args = ap.parse_args()
    log = setup("01_download_nsi")

    fips_all = args.fips or county_list()
    out_root = path("raw", "nsi", mkdir=True)
    todo = [f for f in fips_all if not (out_root / f[:2] / f"{f}.parquet").exists()]
    log.info("counties: %d total, %d to download, workers=%d", len(fips_all), len(todo), args.workers)

    manifest = out_root / "_manifest.csv"
    new_file = not manifest.exists()
    t0 = time.time()
    done = n_tot = 0
    with open(manifest, "a", newline="", encoding="utf-8") as mf, \
            ProcessPoolExecutor(max_workers=args.workers) as ex:
        w = csv.DictWriter(mf, fieldnames=["fips", "status", "n", "n_res", "mb", "sec"])
        if new_file:
            w.writeheader()
        futs = {ex.submit(download_county, f): f for f in todo}
        for fut in as_completed(futs):
            res = fut.result()
            w.writerow({k: res.get(k, "") for k in w.fieldnames})
            mf.flush()
            done += 1
            n_tot += res.get("n", 0) or 0
            if not str(res["status"]).startswith(("ok", "exists")):
                log.warning("county %s -> %s", res["fips"], res["status"])
            if done % 25 == 0 or done == len(todo):
                el = time.time() - t0
                log.info("%d/%d counties | %s structures | %.1f min elapsed", done, len(todo),
                         f"{n_tot:,}", el / 60)
    log.info("finished in %.1f min", (time.time() - t0) / 60)


if __name__ == "__main__":
    main()
