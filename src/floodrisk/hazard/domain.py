"""HUC-8 computational domains and raster inputs on a common 30 m CONUS Albers grid.

Every HUC-8 is processed on its own grid (EPSG:5070, cells aligned to multiples of the
resolution so neighbouring domains share cell edges). National mosaics are exposed as GDAL
VRTs built once from the downloaded tiles.
"""
from __future__ import annotations

import logging
import subprocess
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio import features
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject

from floodrisk.config import path, settings

log = logging.getLogger(__name__)
CRS = "EPSG:5070"


@dataclass
class Grid:
    transform: rasterio.Affine
    width: int
    height: int
    crs: str = CRS

    @property
    def res(self) -> float:
        return self.transform.a

    @property
    def bounds(self):
        x0, y1 = self.transform.c, self.transform.f
        return x0, y1 - self.height * self.res, x0 + self.width * self.res, y1

    def rowcol(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        c = np.floor((x - self.transform.c) / self.res).astype(np.int64)
        r = np.floor((self.transform.f - y) / self.res).astype(np.int64)
        return r, c

    def inside(self, r: np.ndarray, c: np.ndarray) -> np.ndarray:
        return (r >= 0) & (r < self.height) & (c >= 0) & (c < self.width)


def make_grid(geom, res: float | None = None, buffer: float = 1500.0) -> Grid:
    res = float(res or settings()["hazard"]["grid_resolution_m"])
    minx, miny, maxx, maxy = geom.buffer(buffer).bounds
    x0 = np.floor(minx / res) * res
    y1 = np.ceil(maxy / res) * res
    w = int(np.ceil((maxx - x0) / res))
    h = int(np.ceil((y1 - miny) / res))
    return Grid(from_origin(x0, y1, res, res), w, h)


# ----------------------------------------------------------------------------- watersheds
@lru_cache(maxsize=1)
def huc8_table() -> gpd.GeoDataFrame:
    gdb = path("raw", "wbd", "WBD_National_GDB.gdb")
    if not gdb.exists():
        with zipfile.ZipFile(path("raw", "wbd", "WBD_National_GDB.zip")) as z:
            z.extractall(path("raw", "wbd"))
    g = gpd.read_file(gdb, layer="WBDHU8", columns=["huc8", "name", "states", "areasqkm"], engine="pyogrio")
    return g.to_crs(CRS)


def huc8_geom(huc8: str):
    t = huc8_table()
    return t.loc[t.huc8 == huc8].geometry.union_all()


def mask_from_geom(grid: Grid, geom) -> np.ndarray:
    return features.rasterize([(geom, 1)], out_shape=(grid.height, grid.width),
                              transform=grid.transform, fill=0, dtype="uint8").astype(bool)


# ----------------------------------------------------------------------------- national VRTs
def _gdalbuildvrt(out: Path, files: list[Path], extra: list[str] | None = None) -> Path:
    lst = out.with_suffix(".txt")
    lst.write_text("\n".join(str(f) for f in files))
    cmd = ["gdalbuildvrt", *(extra or []), "-input_file_list", str(lst), str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


@lru_cache(maxsize=1)
def dem_vrt() -> Path:
    d = path("raw", "dem3dep_1as")
    out = path("interim", "vrt", mkdir=True) / "dem_1as.vrt"
    if not out.exists():
        _gdalbuildvrt(out, sorted(d.glob("USGS_1_*.tif")))
    return out


ATLAS14_ORDER = ["sa", "orb", "sw", "hi", "pr", "ak", "mw", "se", "ne", "tx", "inw"]  # old -> new


@lru_cache(maxsize=16)
def atlas14_vrt(rp: int) -> Path:
    """Mosaic of NOAA Atlas 14 24-h depth grids for return period ``rp`` (units: 1/1000 inch)."""
    src = path("raw", "atlas14")
    unz = path("interim", "atlas14", mkdir=True)
    files = []
    for reg in ATLAS14_ORDER:
        for name in (f"{reg}{rp}yr24ha_ams.zip", f"{reg}{rp}yr24ha.zip"):
            zf = src / reg / name
            if not zf.exists():
                continue
            with zipfile.ZipFile(zf) as z:
                asc = [n for n in z.namelist() if n.lower().endswith((".asc", ".txt"))]
                for n in asc:
                    tgt = unz / reg / Path(n).name
                    if not tgt.exists():
                        tgt.parent.mkdir(parents=True, exist_ok=True)
                        tgt.write_bytes(z.read(n))
                    files.append(tgt)
            break
    out = path("interim", "vrt", mkdir=True) / f"atlas14_{rp}yr24h.vrt"
    if not out.exists():
        _gdalbuildvrt(out, files, ["-srcnodata", "-9", "-vrtnodata", "-9", "-a_srs", "EPSG:4269"])
    return out


def nlcd_path(kind: str = "LndCov") -> Path:
    d = path("raw", "nlcd")
    tif = sorted(d.glob(f"**/Annual_NLCD_{kind}_2024_CU_C1V1*.tif"))
    if not tif:
        zf = d / f"Annual_NLCD_{kind}_2024_CU_C1V1.zip"
        with zipfile.ZipFile(zf) as z:
            z.extractall(d / kind)
        tif = sorted(d.glob(f"**/Annual_NLCD_{kind}_2024_CU_C1V1*.tif"))
    return tif[0]


# ----------------------------------------------------------------------------- sampling
def warp_to_grid(src_path: Path, grid: Grid, resampling: Resampling, dtype="float32",
                 src_nodata=None, dst_nodata=np.nan, band: int = 1) -> np.ndarray:
    out = np.full((grid.height, grid.width), dst_nodata, dtype=dtype)
    with rasterio.open(src_path) as src:
        reproject(source=rasterio.band(src, band), destination=out,
                  src_transform=src.transform, src_crs=src.crs,
                  src_nodata=src_nodata if src_nodata is not None else src.nodata,
                  dst_transform=grid.transform, dst_crs=grid.crs, dst_nodata=dst_nodata,
                  resampling=resampling, num_threads=4)
    return out


def read_dem(grid: Grid) -> np.ndarray:
    z = warp_to_grid(dem_vrt(), grid, Resampling.bilinear)
    z[z < -500] = np.nan
    return z


def read_atlas14_mm(grid: Grid, rp: int) -> np.ndarray:
    """24-h AMS precipitation depth (mm): NOAA Atlas 14, gap-filled with NOAA Atlas 2 where Atlas 14 has
    no coverage (western WA / OR; mosaic from scripts/05b_build_precip_mosaic.py)."""
    v = warp_to_grid(atlas14_vrt(rp), grid, Resampling.bilinear, src_nodata=-9)
    gap = ~(v > 0)
    fill = path("interim", "precip", f"pf24h_ams_{rp}yr.tif")
    if gap.any() and fill.exists():
        f = warp_to_grid(fill, grid, Resampling.bilinear, src_nodata=-9)
        v = np.where(gap & (f > 0), f, v)
    return v * 0.0254  # 1/1000 inch -> mm


def read_nlcd(grid: Grid) -> np.ndarray:
    return warp_to_grid(nlcd_path("LndCov"), grid, Resampling.nearest, dtype="uint8", dst_nodata=0)


def read_impervious(grid: Grid) -> np.ndarray:
    return warp_to_grid(nlcd_path("FctImp"), grid, Resampling.average, dtype="float32")


def rasterize_lines(grid: Grid, geoms, values, dtype="int32", fill=0, all_touched=True) -> np.ndarray:
    shapes = ((g, v) for g, v in zip(geoms, values))
    return features.rasterize(shapes, out_shape=(grid.height, grid.width), transform=grid.transform,
                              fill=fill, dtype=dtype, all_touched=all_touched)


def write_tif(fp: Path, arr: np.ndarray, grid: Grid, nodata=None, compress="deflate") -> Path:
    fp.parent.mkdir(parents=True, exist_ok=True)
    prof = dict(driver="GTiff", width=grid.width, height=grid.height, count=1, dtype=arr.dtype,
                crs=grid.crs, transform=grid.transform, nodata=nodata, compress=compress,
                tiled=True, blockxsize=512, blockysize=512, BIGTIFF="IF_SAFER")
    with rasterio.open(fp, "w", **prof) as dst:
        dst.write(arr, 1)
    return fp
