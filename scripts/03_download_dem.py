"""Step 03 — download USGS 3DEP 1 arc-second (~30 m) DEM tiles (resumable).

Tiles are named by their north-west corner: ``n30w096`` covers 29-30N, 96-95W.
We probe every candidate 1x1 degree tile over CONUS (and optionally HI/PR) with HEAD
requests and download the ones that exist.

Usage:  python scripts/03_download_dem.py [--workers 6] [--region conus|hi|pr|all]
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.http import download, session  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402

BASE = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1/TIFF/current"
REGIONS = {
    "conus": (range(25, 51), range(67, 126)),   # north edge, west edge (deg)
    "hi": (range(19, 24), range(155, 161)),
    "pr": (range(18, 20), range(64, 68)),
}


def tile_url(n: int, w: int) -> tuple[str, str]:
    name = f"n{n:02d}w{w:03d}"
    return name, f"{BASE}/{name}/USGS_1_{name}.tif"


def exists(url: str) -> bool:
    r = session().head(url, timeout=30)
    return r.status_code == 200


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--region", default="conus", choices=[*REGIONS, "all"])
    args = ap.parse_args()
    log = setup("03_download_dem")
    regions = list(REGIONS) if args.region == "all" else [args.region]
    out_dir = path("raw", "dem3dep_1as", mkdir=True)
    index_file = out_dir / f"_tiles_{args.region}.json"

    if index_file.exists():
        tiles = json.loads(index_file.read_text())
    else:
        cands = [tile_url(n, w) for r in regions for n in REGIONS[r][0] for w in REGIONS[r][1]]
        log.info("probing %d candidate tiles", len(cands))
        tiles = []
        with ThreadPoolExecutor(max_workers=32) as ex:
            futs = {ex.submit(exists, url): (name, url) for name, url in cands}
            for fut in as_completed(futs):
                if fut.result():
                    tiles.append(futs[fut])
        tiles.sort()
        index_file.write_text(json.dumps(tiles))
    log.info("%d tiles exist", len(tiles))

    todo = [(n, u) for n, u in tiles if not (out_dir / f"USGS_1_{n}.tif").exists()]
    log.info("%d tiles to download", len(todo))
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(download, u, out_dir / f"USGS_1_{n}.tif"): n for n, u in todo}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001
                log.error("tile %s failed: %s", futs[fut], exc)
            done += 1
            if done % 50 == 0 or done == len(todo):
                log.info("%d/%d tiles downloaded", done, len(todo))


if __name__ == "__main__":
    main()
