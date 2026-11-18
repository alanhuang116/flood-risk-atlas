"""Step 05 — national 30 m Hydrologic Soil Group raster from gNATSGO (Planetary Computer).

Downloads the CONUS gNATSGO map-unit-key (mukey) tiles and the ``muaggatt`` table, maps each
mukey to ``hydgrpdcd`` (dominant-condition HSG) and writes uint8 tiles + a VRT:
  1=A 2=B 3=C 4=D 5=A/D 6=B/D 7=C/D 0=no data
Access uses anonymous Planetary Computer SAS tokens (refreshed automatically).
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import rasterio  # noqa: E402

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.http import download, session  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
SAS = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{}"
HSG_CODE = {"A": 1, "B": 2, "C": 3, "D": 4, "A/D": 5, "B/D": 6, "C/D": 7}


class Token:
    """Planetary Computer SAS token, refreshed 5 min before its own ``msft:expiry``."""

    def __init__(self, collection: str):
        self.collection, self.tok, self.expiry = collection, None, 0.0

    def get(self, force: bool = False) -> str:
        if force or self.tok is None or time.time() > self.expiry - 300:
            j = session().get(SAS.format(self.collection), timeout=30).json()
            self.tok = j["token"]
            exp = pd.Timestamp(j["msft:expiry"]).timestamp()
            self.expiry = exp if exp > time.time() + 600 else time.time() + 600
        return self.tok


def main() -> None:
    log = setup("05_build_soils_hsg")
    raw = path("raw", "gnatsgo", mkdir=True)
    out = path("interim", "hsg", mkdir=True)
    tok_r, tok_t = Token("gnatsgo-rasters"), Token("gnatsgo-tables")

    # 1) muaggatt -> mukey/hydgrpdcd
    mf = raw / "muaggatt.parquet"
    if not mf.exists():
        url = (f"https://soils.blob.core.windows.net/gnatsgo/july2020/parquet/muaggatt.parquet/part.0.parquet"
               f"?{tok_t.get()}")
        download(url, mf)
    mu = pd.read_parquet(mf, columns=["mukey", "hydgrpdcd"])
    mu["code"] = mu.hydgrpdcd.astype("string").map(HSG_CODE).fillna(0).astype(np.uint8)
    mu["mukey"] = mu.mukey.astype(np.int64)
    mu = mu.sort_values("mukey")
    keys, codes = mu.mukey.to_numpy(), mu.code.to_numpy()
    log.info("muaggatt: %d map units", len(mu))

    # 2) CONUS mukey tiles
    feats = session().post(f"{STAC}/search", json={"collections": ["gnatsgo-rasters"],
                                                   "bbox": [-125, 24, -66, 50], "limit": 1000},
                           timeout=60).json()["features"]
    feats = [f for f in feats if f["id"].startswith("conus")]
    log.info("%d CONUS mukey tiles", len(feats))

    def one(f):
        tid = f["id"]
        dst = out / f"hsg_{tid}.tif"
        if dst.exists():
            return tid, "exists"
        src = raw / "mukey" / f"{tid}.tif"
        if not src.exists():
            try:
                download(f"{f['assets']['mukey']['href']}?{tok_r.get()}", src, attempts=2)
            except RuntimeError:
                download(f"{f['assets']['mukey']['href']}?{tok_r.get(force=True)}", src, attempts=4)
        with rasterio.open(src) as s:
            a = s.read(1).astype(np.int64)
            prof = s.profile
        i = np.clip(np.searchsorted(keys, a), 0, len(keys) - 1)
        hsg = np.where(keys[i] == a, codes[i], 0).astype(np.uint8)
        prof.update(dtype="uint8", nodata=0, compress="deflate", tiled=True, blockxsize=512, blockysize=512)
        with rasterio.open(dst, "w", **prof) as d:
            d.write(hsg, 1)
        src.unlink()          # mukey tile no longer needed
        return tid, "ok"

    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for fut in as_completed([ex.submit(one, f) for f in feats]):
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001
                log.error("tile failed: %s", exc)
            done += 1
            if done % 25 == 0:
                log.info("%d/%d tiles", done, len(feats))
    import subprocess
    tifs = sorted(out.glob("hsg_*.tif"))
    lst = out / "tiles.txt"
    lst.write_text("\n".join(str(t) for t in tifs))
    vrt = path("interim", "vrt", mkdir=True) / "hsg_conus.vrt"
    subprocess.run(["gdalbuildvrt", "-input_file_list", str(lst), str(vrt)], check=True)
    log.info("HSG VRT: %s (%d tiles)", vrt, len(tifs))


if __name__ == "__main__":
    main()
