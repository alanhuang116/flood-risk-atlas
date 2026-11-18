"""Step 05b — CONUS 24-h precipitation-frequency mosaic (AMS): NOAA Atlas 14 + NOAA Atlas 2 gap fill.

NOAA Atlas 14 does not cover western Washington / Oregon (to be replaced by NOAA Atlas 15). There
the current NWS standard is still NOAA Atlas 2 (Miller et al. 1973), digitised at 15" for the 2- and
100-yr 24-h depths (HDSC, https://hdsc.nws.noaa.gov/pub/hdsc/data/westus/). Other return periods:
  * 5-50 yr : NOAA Atlas 2 interpolation equations (partial-duration series)
        P5 = 0.674 P2 + 0.278 P100,  P10 = 0.496 P2 + 0.449 P100,
        P25 = 0.293 P2 + 0.669 P100, P50 = 0.146 P2 + 0.835 P100
  * PDS -> AMS: x0.88 (2-yr), x0.96 (5-yr), x0.99 (10-yr), x1 (>= 25-yr)  (TP-40 / NOAA Atlas 2)
  * 200-1000 yr: P100 x median Atlas 14 ratio P_T / P100 of the neighbouring regions (lat >= 40 N,
    Interior Northwest and California volumes)
Atlas 14 wins wherever it has data. Output: data/interim/precip/pf24h_ams_{T}yr.tif (EPSG:4269,
int32, 1/1000 inch, nodata -9) on the Atlas 14 30" lattice.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402
from rasterio.warp import Resampling, reproject  # noqa: E402

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402

RPS = [2, 5, 10, 25, 50, 100, 200, 500, 1000]
REGIONS = ["sa", "orb", "sw", "mw", "se", "ne", "tx", "inw"]          # CONUS Atlas 14 volumes, old -> new
RES = 1.0 / 120.0
X0, Y1 = -125.0 - 1.0 / 240.0, 49.5 + 1.0 / 240.0                     # Atlas 14 lattice (edges at (2k-1)/240)
W, H = int(np.ceil((-66.5 - X0) / RES)), int(np.ceil((Y1 - 24.25) / RES))
TRANSFORM = from_origin(X0, Y1, RES, RES)
CRS = "EPSG:4269"
NA2_EQ = {5: (0.674, 0.278), 10: (0.496, 0.449), 25: (0.293, 0.669), 50: (0.146, 0.835)}
PDS_TO_AMS = {2: 0.88, 5: 0.96, 10: 0.99}


def atlas14_asc(reg: str, rp: int) -> Path | None:
    src, unz = path("raw", "atlas14", reg), path("interim", "atlas14", reg, mkdir=True)
    for name in (f"{reg}{rp}yr24ha_ams.zip", f"{reg}{rp}yr24ha.zip"):
        zf = src / name
        if zf.exists():
            with zipfile.ZipFile(zf) as z:
                names = [m for m in z.namelist() if m.lower().endswith((".asc", ".txt"))]
                if not names:            # e.g. the legacy semiarid-SW archive (.ver4), superseded by "sw"
                    return None
                tgt = unz / Path(names[0]).name
                if not tgt.exists():
                    tgt.write_bytes(z.read(names[0]))
                return tgt
    return None


def to_lattice(fp: Path, resampling=Resampling.nearest, scale: float = 1.0) -> np.ndarray:
    """Source grid -> mosaic lattice (float, NaN outside / nodata)."""
    out = np.full((H, W), np.nan, dtype=np.float32)
    with rasterio.open(fp) as s:
        a = s.read(1).astype(np.float32)
        nod = s.nodata
        bad = ~np.isfinite(a) | (a <= 0)
        if nod is not None:
            bad |= a == nod
        a[bad] = np.nan
        reproject(a * scale, out, src_transform=s.transform, src_crs=s.crs or CRS, dst_transform=TRANSFORM,
                  dst_crs=CRS, src_nodata=np.nan, dst_nodata=np.nan, resampling=resampling)
    return out


def main() -> None:
    log = setup("05b_precip_mosaic")
    od = path("interim", "precip", mkdir=True)
    # --- Atlas 14 per return period (later volumes win)
    a14 = {}
    for rp in RPS:
        m = np.full((H, W), np.nan, dtype=np.float32)
        for reg in REGIONS:
            fp = atlas14_asc(reg, rp)
            if fp is None:
                continue
            g = to_lattice(fp)
            m = np.where(np.isfinite(g), g, m)
        a14[rp] = m                                               # 1/1000 inch
        log.info("Atlas 14 %4d-yr: %.1f%% of lattice", rp, 100 * np.isfinite(m).mean())
    # --- tail ratios from the neighbouring Atlas 14 regions (Interior NW, California; lat >= 40 N)
    lat = Y1 - (np.arange(H) + 0.5) * RES
    lon = X0 + (np.arange(W) + 0.5) * RES
    band = (lat[:, None] >= 40.0) & (lon[None, :] <= -110.0)
    ratios = {}
    for rp in (200, 500, 1000):
        ok = band & np.isfinite(a14[rp]) & np.isfinite(a14[100]) & (a14[100] > 0)
        ratios[rp] = float(np.median(a14[rp][ok] / a14[100][ok]))
    log.info("tail ratios P_T/P100 (neighbouring Atlas 14): %s", {k: round(v, 3) for k, v in ratios.items()})
    # --- Atlas 2 (in x 1e5 -> 1/1000 inch), 15" -> 30" average
    na2 = path("raw", "atlas2")
    p2 = to_lattice(na2 / "na2_westus_2yr24hr.asc", Resampling.average, 0.01)
    p100 = to_lattice(na2 / "na2_westus_100yr24hr.asc", Resampling.average, 0.01)
    pds = {2: p2, 100: p100, **{rp: a * p2 + b * p100 for rp, (a, b) in NA2_EQ.items()}}
    na2_ams = {rp: pds[rp] * PDS_TO_AMS.get(rp, 1.0) for rp in pds}
    na2_ams.update({rp: p100 * ratios[rp] for rp in ratios})
    prof = dict(driver="GTiff", height=H, width=W, count=1, dtype="int32", crs=CRS, transform=TRANSFORM,
                nodata=-9, compress="deflate", tiled=True, blockxsize=512, blockysize=512)
    for rp in RPS:
        fill = np.isnan(a14[rp]) & np.isfinite(na2_ams[rp])
        m = np.where(fill, na2_ams[rp], a14[rp])
        out = np.where(np.isfinite(m), np.round(m), -9).astype(np.int32)
        tmp = od / f"pf24h_ams_{rp}yr.tif.tmp"
        with rasterio.open(tmp, "w", **prof) as dst:
            dst.write(out, 1)
        tmp.replace(od / f"pf24h_ams_{rp}yr.tif")
        log.info("mosaic %4d-yr written: Atlas 2 fill %.2f%% of lattice", rp, 100 * fill.mean())


if __name__ == "__main__":
    main()
