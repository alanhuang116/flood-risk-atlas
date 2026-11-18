"""Step 09a — FEMA calibration of every fluvial reach that has FEMA information (CSI / no-SFHA).

Stores Q_SRC(H*) per reach so that k can be recomputed for any channel-capacity parameters, and the
national table used to build the regional drainage-area fallback for reaches without FEMA data.
Outputs: data/interim/hazard/fluvial/calib_raw/{huc8}.parquet, .../calib_raw_national.parquet
Usage:   python scripts/09a_calibrate_fluvial.py --workers 8 [--overwrite]
"""
from __future__ import annotations

import argparse
import glob
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402


def run_one(huc8: str) -> dict:
    from floodrisk import pipeline
    from floodrisk.config import return_periods
    from floodrisk.hazard.fluvial.calibrate import calibrate_reaches
    from floodrisk.hazard.fluvial.capacity import ANCHOR_NATURAL, ANCHOR_URBAN
    from floodrisk.utils.http import atomic_parquet
    t0 = time.time()
    try:
        hz = path("interim", "hazard")
        fp_b, fp_s = hz / "fluvial" / "buildings" / f"{huc8}.parquet", hz / "fluvial" / "src" / f"{huc8}.parquet"
        if not fp_b.exists() or not fp_s.exists():
            return {"huc8": huc8, "status": "no-fluvial"}
        ex = pipeline.load_buildings(huc8)
        fb, src = pd.read_parquet(fp_b), pd.read_parquet(fp_s)
        fc = hz / "coastal" / "buildings" / f"{huc8}.parquet"
        cb = pd.read_parquet(fc, columns=["fd_id", "baseline_rp100"]) if fc.exists() else None
        fr = hz / "fluvial" / "reaches" / f"{huc8}.parquet"
        dev = pd.read_parquet(fr).set_index("comid").dev_frac if fr.exists() else None
        # k is anchored at the physical channel capacity, never at the NFIP-calibrated one (capacity.py)
        cal = calibrate_reaches(fb, ex, src, pipeline.flood_quantiles(), None, cb, reach_dev=dev,
                                rps=return_periods(), tc_natural=ANCHOR_NATURAL,
                                tc_urban=ANCHOR_URBAN, informed_only=True)
        atomic_parquet(cal.assign(huc8=huc8), path("interim", "hazard", "fluvial", "calib_raw", mkdir=True) / f"{huc8}.parquet")
        return {"huc8": huc8, "status": "ok", "n": len(cal), "sec": round(time.time() - t0, 1)}
    except Exception as exc:  # noqa: BLE001
        return {"huc8": huc8, "status": f"error {exc!r}", "trace": traceback.format_exc(limit=4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()
    log = setup("09a_calibrate_fluvial")
    from floodrisk import pipeline
    od = path("interim", "hazard", "fluvial", "calib_raw", mkdir=True)
    hucs = pipeline.huc8_list().huc8.tolist()
    todo = [h for h in hucs if args.overwrite or not (od / f"{h}.parquet").exists()]
    log.info("fluvial calibration: %d HUC8 to run", len(todo))
    with ProcessPoolExecutor(args.workers) as ex:
        futs = [ex.submit(run_one, h) for h in todo]
        for k, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r["status"] not in ("ok", "no-fluvial"):
                log.error("%s %s\n%s", r["huc8"], r["status"], r.get("trace", ""))
            elif k % 200 == 0:
                log.info("[%d/%d] last %s", k, len(todo), r)
    files = glob.glob(str(od / "*.parquet"))
    nat = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    nat["huc2"] = nat.huc8.str[:2]
    nat.to_parquet(path("interim", "hazard", "fluvial", "calib_raw_national.parquet"), index=False)
    log.info("national: %d FEMA-informed reaches %s", len(nat), nat.method.value_counts().to_dict())


if __name__ == "__main__":
    main()
