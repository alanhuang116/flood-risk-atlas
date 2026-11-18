"""Step 08 — run a hazard module for HUC-8 watersheds (resumable).

Usage:
  python scripts/08_run_hazard.py --peril fluvial  --huc8 12040204 12040104
  python scripts/08_run_hazard.py --peril pluvial  --all --workers 2
  python scripts/08_run_hazard.py --peril coastal  --all
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402


def out_dir(peril: str) -> Path:
    return path("interim", "hazard", peril, mkdir=True)


def run_one(peril: str, huc8: str, keep_rasters: bool = False) -> dict:
    from floodrisk import pipeline
    t0 = time.time()
    try:
        b = pipeline.load_buildings(huc8)
        if peril == "fluvial":
            from floodrisk.hazard.fluvial.hand import process_huc8
            res = process_huc8(huc8, pipeline.flowlines_for(huc8), b, out_dir(peril), keep_rasters=keep_rasters)
        elif peril == "pluvial":
            from floodrisk.hazard.pluvial.run_huc import process_huc8
            res = process_huc8(huc8, b, out_dir(peril), flowlines=pipeline.flowlines_for(huc8, 600.0),
                               keep_rasters=keep_rasters)
        elif peril == "coastal":
            from floodrisk.hazard.climate import SeaLevel
            from floodrisk.hazard.coastal.inundation import process_huc8
            res = process_huc8(huc8, b, out_dir(peril), SeaLevel())
        else:
            raise ValueError(peril)
        res["status"] = "ok"
    except Exception as exc:  # noqa: BLE001
        res = {"huc8": huc8, "status": f"error: {exc!r}", "trace": traceback.format_exc(limit=3)}
    res["sec"] = round(time.time() - t0, 1)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peril", required=True, choices=["fluvial", "pluvial", "coastal"])
    ap.add_argument("--huc8", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--keep-rasters", action="store_true")
    args = ap.parse_args()
    log = setup(f"08_hazard_{args.peril}")

    if args.all:
        from floodrisk import pipeline
        hucs = pipeline.huc8_list().huc8.tolist()
        if args.peril == "coastal":
            from floodrisk.hazard.coastal.inundation import coastal_huc8s
            coast = set(coastal_huc8s())
            hucs = [h for h in hucs if h in coast]
    else:
        hucs = args.huc8 or []
    od = out_dir(args.peril)
    todo = [h for h in hucs if not (od / "buildings" / f"{h}.parquet").exists()]
    log.info("%s: %d HUC8 (%d to run), workers=%d", args.peril, len(hucs), len(todo), args.workers)
    man = od / "_manifest.csv"
    fields = ["huc8", "status", "sec", "cells", "buildings", "reaches", "bld_assigned", "prep_s", "gpu_s",
              "res_m", "bld_wet_100", "seed_cells", "flooded_100_base", "src_rows"]
    new = not man.exists()
    with open(man, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        t0 = time.time()
        if args.workers <= 1:
            it = (run_one(args.peril, h, args.keep_rasters) for h in todo)
            results = it
        else:
            ex = ProcessPoolExecutor(max_workers=args.workers)
            futs = [ex.submit(run_one, args.peril, h, args.keep_rasters) for h in todo]
            results = (f.result() for f in as_completed(futs))
        for k, res in enumerate(results, 1):
            w.writerow(res)
            fh.flush()
            if res["status"] != "ok":
                log.error("%s %s: %s\n%s", args.peril, res["huc8"], res["status"], res.get("trace", ""))
            else:
                log.info("[%d/%d] %s ok in %.0fs %s", k, len(todo), res["huc8"], res["sec"],
                         {k_: v for k_, v in res.items() if k_ not in ("huc8", "status", "sec")})
        log.info("done in %.1f min", (time.time() - t0) / 60)


if __name__ == "__main__":
    main()
