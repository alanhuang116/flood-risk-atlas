"""Pluvial (rain-on-grid) hazard for one HUC-8 on the GPU.

Runs (batched): NOAA Atlas 14 24-h depths for T = 2..1000 yr, plus two extended runs at
1.15x and 1.30x the 1000-yr depth so that climate-intensified rainfall can be interpolated
without extrapolation.  Results are sampled at residential buildings:
    depth_b = max(0, z_cell + hmax_cell - z_building)   if hmax_cell >= 0.10 m, else 0
together with the 24-h rainfall depth of each run at the building, which later lets us
interpolate depth for any (climate-scaled) rainfall.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

from floodrisk.config import path
from floodrisk.hazard import domain
from floodrisk.hazard.pluvial.solver import PluvialInputs, run_batch, scs_smax_mm

log = logging.getLogger(__name__)

ATLAS_RPS = [2, 5, 10, 25, 50, 100, 200, 500, 1000]
EXT_MULT = [1.15, 1.30]
RUN_LABELS = [f"T{T}" for T in ATLAS_RPS] + [f"T1000x{m:.2f}" for m in EXT_MULT]
DESIGN_RP_DRAINED = 5
MIN_DEPTH_M = 0.10

# TR-55 curve numbers (HSG A, B, C, D) by NLCD class
CN_TABLE = {
    11: (99, 99, 99, 99), 12: (98, 98, 98, 98),
    21: (49, 69, 79, 84), 22: (61, 75, 83, 87), 23: (77, 85, 90, 92), 24: (89, 92, 94, 95),
    31: (77, 86, 91, 94), 41: (36, 60, 73, 79), 42: (36, 60, 73, 79), 43: (36, 60, 73, 79),
    52: (35, 56, 70, 77), 71: (49, 69, 79, 84), 81: (49, 69, 79, 84), 82: (67, 78, 85, 89),
    90: (86, 86, 86, 86), 95: (89, 89, 89, 89),
}
# Manning's n by NLCD class (Kalyanapu et al. 2009)
N_TABLE = {11: 0.025, 12: 0.022, 21: 0.0404, 22: 0.0678, 23: 0.0678, 24: 0.0404, 31: 0.0113,
           41: 0.36, 42: 0.32, 43: 0.40, 52: 0.40, 71: 0.368, 81: 0.325, 82: 0.037,
           90: 0.086, 95: 0.1825}


def curve_number(nlcd: np.ndarray, hsg: np.ndarray) -> np.ndarray:
    """CN per cell; dual groups (A/D..C/D) use the drained group except in wetlands; missing -> C (D if dense urban)."""
    g = hsg.astype(np.int16).copy()
    dual = (g >= 5) & (g <= 7)
    wet = np.isin(nlcd, (90, 95))
    g = np.where(dual & wet, 4, np.where(dual, g - 4, g))
    g = np.where(g == 0, np.where(np.isin(nlcd, (23, 24)), 4, 3), g)
    cn = np.full(nlcd.shape, 75.0, dtype=np.float32)
    for cls, vals in CN_TABLE.items():
        m = nlcd == cls
        if m.any():
            cn[m] = np.asarray(vals, np.float32)[np.clip(g[m] - 1, 0, 3)]
    return cn


def manning(nlcd: np.ndarray) -> np.ndarray:
    n = np.full(nlcd.shape, 0.06, dtype=np.float32)
    for cls, v in N_TABLE.items():
        n[nlcd == cls] = v
    return n


def read_hsg(grid: domain.Grid) -> np.ndarray:
    vrt = path("interim", "vrt", "hsg_conus.vrt")
    from rasterio.enums import Resampling
    if not vrt.exists():
        return np.zeros((grid.height, grid.width), np.uint8)
    return domain.warp_to_grid(vrt, grid, Resampling.mode, dtype="uint8", dst_nodata=0)


def resolution_for(n_buildings: int, area_km2: float) -> float:
    """Adaptive hydraulic resolution: 30 m where homes are dense, coarser in sparse watersheds."""
    density = n_buildings / max(area_km2, 1.0)
    if density >= 60 or n_buildings >= 150_000:     # large metros: 212 HUC-8s, 58 % of homes
        return 30.0
    if density >= 4:
        return 60.0
    return 90.0


def prepare(huc8: str, flowlines=None, res: float | None = None):
    geom = domain.huc8_geom(huc8)
    grid = domain.make_grid(geom, res=res, buffer=600.0)
    dem = domain.read_dem(grid)
    mask = domain.mask_from_geom(grid, geom)
    nlcd = domain.read_nlcd(grid)
    hsg = read_hsg(grid)
    z = dem.copy()
    if flowlines is not None and len(flowlines):
        burn = domain.rasterize_lines(grid, flowlines.geometry.values, np.ones(len(flowlines), np.uint8),
                                      dtype="uint8") > 0
        z = np.where(burn & np.isfinite(z), z - 2.0, z)        # keep culverts / bridges open
    # open water (sea, bays, lakes, wide rivers) is a sink: inactive cells -> free-outfall boundary.
    # Rain-on-grid otherwise piles rain onto enclosed bays/lakes and floods their shores artificially.
    water = nlcd == 11
    active = mask & np.isfinite(z) & ~water
    p_runs = [domain.read_atlas14_mm(grid, T) for T in ATLAS_RPS]
    p_runs += [p_runs[-1] * m for m in EXT_MULT]
    p_runs = np.stack(p_runs).astype(np.float32)
    p_runs = np.where(np.isfinite(p_runs), p_runs, 0.0)
    p_des = np.where(np.isfinite(p_runs[1]), domain.read_atlas14_mm(grid, DESIGN_RP_DRAINED), 0.0)
    cn = curve_number(nlcd, hsg)
    inp = PluvialInputs(z=np.where(np.isfinite(z), z, 0.0).astype(np.float32), active=active,
                        manning=manning(nlcd), smax_mm=scs_smax_mm(cn),
                        drained=np.isin(nlcd, (22, 23, 24)),
                        p_design_mm=np.nan_to_num(p_des).astype(np.float32), p_runs_mm=p_runs,
                        dx=grid.res)
    return grid, dem, inp


def process_huc8(huc8: str, buildings: pd.DataFrame, out_dir: Path, flowlines=None,
                 keep_rasters: bool = False) -> dict:
    t0 = time.time()
    t = domain.huc8_table()
    area = float(t.loc[t.huc8 == huc8, "areasqkm"].sum())
    res = resolution_for(len(buildings), area)
    grid, dem, inp = prepare(huc8, flowlines, res=res)
    t1 = time.time()
    # two batches: frequent storms stay shallow -> larger CFL time step than the extreme runs
    from dataclasses import replace
    split = ATLAS_RPS.index(50) + 1
    h_a = run_batch(replace(inp, p_runs_mm=inp.p_runs_mm[:split]), storm_hours=24.0, drain_hours=6.0)
    h_b = run_batch(replace(inp, p_runs_mm=inp.p_runs_mm[split:]), storm_hours=24.0, drain_hours=6.0)
    hmax = np.concatenate([h_a, h_b], axis=0)
    t2 = time.time()
    r, c = grid.rowcol(buildings.x5070.to_numpy(), buildings.y5070.to_numpy())
    inside = grid.inside(r, c) & inp.active[np.clip(r, 0, grid.height - 1), np.clip(c, 0, grid.width - 1)]
    rr, cc = np.where(inside, r, 0), np.where(inside, c, 0)
    zc = dem[rr, cc]
    zb = buildings.grnd_elv_m.to_numpy(dtype=np.float64)
    zb = np.where(np.isfinite(zb) & (zb > -100), zb, zc)
    out = {"fd_id": buildings.fd_id.values}
    for k, lab in enumerate(RUN_LABELS):
        h = hmax[k, rr, cc]
        d = np.where(inside & (h >= MIN_DEPTH_M), zc + h - zb, 0.0)
        out[f"d_{lab}"] = np.clip(d, 0, None).astype(np.float32)
    for k, T in enumerate(ATLAS_RPS):
        out[f"p_T{T}"] = np.where(inside, inp.p_runs_mm[k, rr, cc], np.nan).astype(np.float32)
    from floodrisk.utils.http import atomic_parquet
    atomic_parquet(pd.DataFrame(out), out_dir / "buildings" / f"{huc8}.parquet")
    if keep_rasters:
        for k, lab in enumerate(RUN_LABELS):
            domain.write_tif(out_dir / "rasters" / huc8 / f"hmax_{lab}.tif",
                             np.where(inp.active, hmax[k], np.nan).astype(np.float32), grid, nodata=np.nan)
    return {"huc8": huc8, "cells": int(inp.active.sum()), "buildings": int(len(buildings)),
            "prep_s": round(t1 - t0, 1), "gpu_s": round(t2 - t1, 1), "res_m": res,
            "bld_wet_100": int((out["d_T100"] > 0).sum())}
