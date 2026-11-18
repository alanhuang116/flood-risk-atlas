"""Step 09 — building-level losses, AAL and RP losses per HUC-8 (resumable).

Writes data/processed/risk/buildings/{huc8}.parquet (fixed defence crests), zcta_rp/{huc8}.parquet (ZCTA
losses at every return period; scenario ids ending in "_adapt" = raised crests) and, for defended homes,
buildings_adapt/{huc8}.parquet (raised-crest variant of the future scenarios, risk.protection.adapt_variant).
Usage:  python scripts/09_compute_risk.py --all [--workers 8]   |   --huc8 12040204 ...
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402

_MODEL = None


def run_one(huc8: str) -> dict:
    global _MODEL
    from floodrisk.risk.engine import LossModel
    from floodrisk.risk.inputs import fluvial_k, load_components
    from floodrisk.risk.protection import protection_T
    t0 = time.time()
    try:
        if _MODEL is None:
            _MODEL = LossModel()
        ex, comps = load_components(huc8)
        if len(ex) == 0:
            return {"huc8": huc8, "status": "empty"}
        fb = comps.fluvial_bld
        prm = _MODEL.params
        comps.reach_dev, comps.src_k = fluvial_k(huc8, comps, prm)
        comps.protect_T = protection_T(ex, prm)
        res, zrp = _MODEL.run(comps)
        res.insert(1, "zcta", ex.zcta.values)
        res.insert(2, "huc8", huc8)
        res["protect_rp_fluvial"] = _MODEL.last_protect["fluvial"].astype("float32")
        res["protect_rp_coastal"] = _MODEL.last_protect["coastal"].astype("float32")
        from floodrisk.utils.http import atomic_parquet
        fa = path("processed", "risk", "buildings_adapt", mkdir=True) / f"{huc8}.parquet"
        if _MODEL.last_adapt is not None and len(_MODEL.last_adapt):    # raised-crest variant, defended homes
            atomic_parquet(_MODEL.last_adapt, fa, compression="zstd")
        elif fa.exists():
            fa.unlink()
        atomic_parquet(zrp.assign(huc8=huc8), path("processed", "risk", "zcta_rp", mkdir=True) / f"{huc8}.parquet",
                       compression="zstd")
        atomic_parquet(res, path("processed", "risk", "buildings", mkdir=True) / f"{huc8}.parquet",
                       compression="zstd")
        return {"huc8": huc8, "status": "ok", "n": len(res), "sec": round(time.time() - t0, 1),
                "has": "".join(k[0] for k, v in (("fluvial", fb), ("pluvial", comps.pluvial_bld),
                                                  ("coastal", comps.coastal_bld)) if v is not None)}
    except Exception as exc:  # noqa: BLE001
        return {"huc8": huc8, "status": f"error {exc!r}", "trace": traceback.format_exc(limit=4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--huc8", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--ready", action="store_true",
                    help="only HUC-8s whose pluvial hazard is finished (run while the GPU stage continues)")
    args = ap.parse_args()
    log = setup("09_compute_risk")
    from floodrisk import pipeline
    hucs = pipeline.huc8_list().huc8.tolist() if args.all else (args.huc8 or [])
    od = path("processed", "risk", "buildings", mkdir=True)
    todo = [h for h in hucs if args.overwrite or not (od / f"{h}.parquet").exists()]
    from floodrisk.config import paused
    if paused("09_compute_risk"):
        return
    if args.ready:
        pv = path("interim", "hazard", "pluvial", "buildings")
        todo = [h for h in todo if (pv / f"{h}.parquet").exists()]
    log.info("risk: %d HUC8 to compute", len(todo))
    if args.workers <= 1:
        results = (run_one(h) for h in todo)
    else:
        ex = ProcessPoolExecutor(max_workers=args.workers)
        results = (f.result() for f in as_completed([ex.submit(run_one, h) for h in todo]))
    for k, r in enumerate(results, 1):
        if r["status"] == "ok":
            log.info("[%d/%d] %s n=%s %ss perils=%s", k, len(todo), r["huc8"], r["n"], r["sec"], r["has"])
        else:
            log.error("[%d/%d] %s %s\n%s", k, len(todo), r["huc8"], r["status"], r.get("trace", ""))


if __name__ == "__main__":
    main()
