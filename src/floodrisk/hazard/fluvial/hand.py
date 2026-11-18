"""HAND (Height Above Nearest Drainage) fluvial hazard per HUC-8.

Method (after Nobre et al. 2011; Zheng et al. 2018; NOAA OWP FIM):
  1. Rivers = NHDPlus V2.1 flowlines with total drainage area >= threshold (default 50 km2),
     rasterised on the 30 m grid (larger rivers drawn last so they win at confluences).
  2. Flow directions on a stream-burned, depression-breached DEM (WhiteboxTools, D8).
  3. Every cell is traced downstream (pointer jumping) to its first river cell:
        HAND = z(cell) - z(river cell);  reach = COMID of that river cell.
  4. Synthetic rating curve (SRC) per reach from the HAND "catchment" geometry:
        V(H) = sum_{HAND<H} (H - HAND) * A;   B(H) = sum_{HAND<H} A * sqrt(1 + s^2)
        cross-section XA = V/L, wetted perimeter P = B/L, R = XA/P
        Q(H) = XA * R^(2/3) * S^(1/2) / n
  5. Buildings: HAND_b = ground elevation of the building (NSI, USGS 10 m) - z(river cell);
     flood depth for a discharge Q is  max(0, H(Q) - HAND_b).
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from floodrisk.config import path, settings
from floodrisk.hazard import domain

log = logging.getLogger(__name__)

# WhiteboxTools D8 pointer codes -> (drow, dcol)
_WBT_D8 = {1: (-1, 1), 2: (0, 1), 4: (1, 1), 8: (1, 0), 16: (1, -1), 32: (0, -1), 64: (-1, -1), 128: (-1, 0)}
STAGES = np.round(np.concatenate([np.arange(0, 3, 0.1), np.arange(3, 10, 0.25), np.arange(10, 30.01, 0.5)]), 3)
MANNING_N = 0.06


def _wbt():
    import whitebox
    w = whitebox.WhiteboxTools()
    w.set_verbose_mode(False)
    return w


def flow_pointer(dem: np.ndarray, grid: domain.Grid, streams: np.ndarray, workdir: Path) -> np.ndarray:
    """D8 pointer (WhiteboxTools encoding) on a stream-burned, breached DEM."""
    nod = -9999.0
    z = np.where(np.isfinite(dem), dem, nod).astype(np.float32)
    z = np.where((streams > 0) & (z > nod), z - 10.0, z).astype(np.float32)   # stream burning
    f_dem, f_br, f_ptr = workdir / "dem.tif", workdir / "breached.tif", workdir / "ptr.tif"
    domain.write_tif(f_dem, z, grid, nodata=nod, compress="lzw")
    w = _wbt()
    w.work_dir = str(workdir)
    # Lindsay (2016) complete breaching: ~100x faster than least-cost breaching on flat terrain.
    # WhiteboxTools fails transiently now and then under heavy parallel load -> retry.
    for attempt in range(1, 4):
        for f in (f_br, f_ptr):
            f.unlink(missing_ok=True)
        if (w.breach_depressions(str(f_dem), str(f_br), fill_pits=True) == 0 and f_br.exists()
                and w.d8_pointer(str(f_br), str(f_ptr)) == 0 and f_ptr.exists()):
            break
        log.warning("whitebox attempt %d failed in %s", attempt, workdir)
    else:
        raise RuntimeError("whitebox breach_depressions/d8_pointer failed after 3 attempts")
    import rasterio
    with rasterio.open(f_ptr) as src:
        return src.read(1)


def downstream_index(ptr: np.ndarray) -> np.ndarray:
    ny, nx = ptr.shape
    idx = np.arange(ny * nx, dtype=np.int64).reshape(ny, nx)
    down = np.full(ny * nx, -1, dtype=np.int64)
    p = ptr.astype(np.int64)
    for code, (dr, dc) in _WBT_D8.items():
        rr, cc = np.nonzero(p == code)
        r2, c2 = rr + dr, cc + dc
        ok = (r2 >= 0) & (r2 < ny) & (c2 >= 0) & (c2 < nx)
        down[idx[rr[ok], cc[ok]]] = idx[r2[ok], c2[ok]]
    return down


def nearest_drainage(down: np.ndarray, is_stream: np.ndarray, max_iter: int = 40) -> np.ndarray:
    """Index of the first stream cell reached downstream from every cell (-1 if none)."""
    tgt = down.copy()
    s = np.flatnonzero(is_stream)
    tgt[s] = s
    for _ in range(max_iter):
        ok = tgt >= 0
        nxt = tgt.copy()
        nxt[ok] = tgt[tgt[ok]]
        if np.array_equal(nxt, tgt):
            break
        tgt = nxt
    # cells whose chain never hit a stream
    bad = tgt >= 0
    bad[bad] = ~is_stream[tgt[bad]]
    tgt[bad] = -1
    return tgt


def synthetic_rating_curves(hand: np.ndarray, reach: np.ndarray, slope_cell: np.ndarray,
                            reaches: pd.DataFrame, cell_area: float) -> pd.DataFrame:
    """SRC per reach -> long table (comid, stage_m, q_cms, xa_m2, top_width_m)."""
    ok = np.isfinite(hand) & (reach > 0) & (hand <= STAGES[-1])
    h, r, bf = hand[ok], reach[ok], np.sqrt(1.0 + slope_cell[ok] ** 2)
    order = np.lexsort((h, r))
    h, r, bf = h[order], r[order], bf[order]
    uniq, start, counts = np.unique(r, return_index=True, return_counts=True)
    rows = []
    rinfo = reaches.set_index("rid")
    for rid, s0, n in zip(uniq, start, counts):
        if rid not in rinfo.index:
            continue
        info = rinfo.loc[rid]
        L = max(float(info.length_m), 60.0)
        S = max(float(info.slope), 1e-5)
        hh = h[s0:s0 + n].astype(np.float64)
        cb = np.concatenate([[0.0], np.cumsum(bf[s0:s0 + n], dtype=np.float64)])
        ch = np.concatenate([[0.0], np.cumsum(hh)])
        k = np.searchsorted(hh, STAGES, side="left")
        vol = cell_area * (k * STAGES - ch[k])
        bed = cell_area * cb[k]
        xa = vol / L
        wp = np.maximum(bed / L, 1e-6)
        rh = xa / wp
        q = xa * rh ** (2.0 / 3.0) * np.sqrt(S) / MANNING_N
        tw = k * cell_area / L
        rows.append(pd.DataFrame({"comid": int(info.comid), "stage_m": STAGES, "q_cms": q,
                                  "xa_m2": xa, "top_width_m": tw}))
    if not rows:
        return pd.DataFrame(columns=["comid", "stage_m", "q_cms", "xa_m2", "top_width_m"])
    return pd.concat(rows, ignore_index=True)


def process_huc8(huc8: str, flowlines: gpd.GeoDataFrame, buildings: pd.DataFrame,
                 out_dir: Path, min_da_km2: float | None = None, keep_rasters: bool = False) -> dict:
    """Run HAND + SRC for one HUC-8.

    flowlines: NHDPlus flowlines (EPSG:5070) intersecting the domain, with columns
               comid, totdasqkm, lengthkm, slope, qe_ma_cms, geometry.
    buildings: residential structures of the HUC-8 with columns fd_id, x5070, y5070, grnd_elv_m.
    """
    min_da = float(min_da_km2 or settings()["hazard"]["fluvial_min_drainage_km2"])
    geom = domain.huc8_geom(huc8)
    grid = domain.make_grid(geom)
    dem = domain.read_dem(grid)
    mask = domain.mask_from_geom(grid, geom)
    riv = flowlines[(flowlines.totdasqkm >= min_da) & flowlines.geometry.notna()].copy()
    riv = riv.sort_values("totdasqkm").reset_index(drop=True)
    riv["rid"] = np.arange(1, len(riv) + 1, dtype=np.int32)
    streams = (domain.rasterize_lines(grid, riv.geometry.values, riv.rid.values)
               if len(riv) else np.zeros((grid.height, grid.width), np.int32))
    streams[~np.isfinite(dem)] = 0

    res = {"huc8": huc8, "cells": grid.width * grid.height, "reaches": int(len(riv)),
           "buildings": int(len(buildings))}
    out_b = out_dir / "buildings" / f"{huc8}.parquet"
    out_s = out_dir / "src" / f"{huc8}.parquet"
    if len(riv) == 0:
        b = pd.DataFrame({"fd_id": buildings.fd_id.values, "comid": np.int64(0), "hand_b_m": np.nan,
                          "hand_cell_m": np.nan})
        from floodrisk.utils.http import atomic_parquet
        atomic_parquet(pd.DataFrame(columns=["comid", "stage_m", "q_cms", "xa_m2", "top_width_m"]), out_s)
        atomic_parquet(b, out_b)
        return res

    with tempfile.TemporaryDirectory(dir=path("interim", mkdir=True)) as td:
        ptr = flow_pointer(dem, grid, streams, Path(td))
    down = downstream_index(ptr)
    flat_streams = streams.ravel()
    tgt = nearest_drainage(down, flat_streams > 0)
    zflat = dem.ravel()
    ok = tgt >= 0
    hand = np.full(zflat.shape, np.nan, dtype=np.float32)
    hand[ok] = zflat[ok] - zflat[tgt[ok]]
    hand[hand < 0] = 0.0
    reach = np.zeros(zflat.shape, dtype=np.int32)
    reach[ok] = flat_streams[tgt[ok]]
    zriv = np.full(zflat.shape, np.nan, dtype=np.float32)
    zriv[ok] = zflat[tgt[ok]]

    gy, gx = np.gradient(np.where(np.isfinite(dem), dem, np.nanmean(dem)), grid.res)
    slope = np.hypot(gx, gy).ravel()
    src = synthetic_rating_curves(hand, reach, slope, riv[["rid", "comid", "length_m", "slope"]],
                                  grid.res ** 2)

    # urbanisation of each reach's local catchment (drives the assumed channel capacity)
    nlcd = domain.read_nlcd(grid).ravel()
    okc = reach > 0
    n_all = np.bincount(reach[okc], minlength=len(riv) + 1)
    n_dev = np.bincount(reach[okc & np.isin(nlcd, (22, 23, 24))], minlength=len(riv) + 1)
    dev_frac = np.where(n_all > 0, n_dev / np.maximum(n_all, 1), 0.0)
    reach_attr = pd.DataFrame({"comid": riv.comid.values, "dev_frac": dev_frac[riv.rid.values].astype(np.float32),
                               "n_cells": n_all[riv.rid.values]})
    from floodrisk.utils.http import atomic_parquet
    atomic_parquet(reach_attr, out_dir / "reaches" / f"{huc8}.parquet")

    # buildings -> HAND relative to their own (10 m) ground elevation
    r, c = grid.rowcol(buildings.x5070.to_numpy(), buildings.y5070.to_numpy())
    inside = grid.inside(r, c)
    lin = np.where(inside, r * grid.width + c, 0)
    rid_b = np.where(inside, reach[lin], 0)
    zr_b = np.where(inside, zriv[lin], np.nan)
    zg = buildings.grnd_elv_m.to_numpy(dtype=np.float64)
    zg = np.where(np.isfinite(zg) & (zg > -100), zg, np.where(inside, zflat[lin], np.nan))
    hand_b = np.where(rid_b > 0, np.maximum(zg - zr_b, 0.0), np.nan)
    comid_map = np.zeros(len(riv) + 1, dtype=np.int64)
    comid_map[riv.rid.values] = riv.comid.values
    b = pd.DataFrame({"fd_id": buildings.fd_id.values, "comid": comid_map[rid_b],
                      "hand_b_m": hand_b.astype(np.float32),
                      "hand_cell_m": np.where(inside, hand[lin], np.nan).astype(np.float32)})
    from floodrisk.utils.http import atomic_parquet
    atomic_parquet(src, out_s)          # rating curves first ...
    atomic_parquet(b, out_b)            # ... buildings file last (it marks the HUC-8 as done)
    if keep_rasters:
        rd = out_dir / "rasters" / huc8
        domain.write_tif(rd / "hand.tif", np.where(mask.ravel(), hand, np.nan).reshape(dem.shape).astype(np.float32), grid, nodata=np.nan)
        domain.write_tif(rd / "streams.tif", streams, grid, nodata=0)
    res.update({"src_rows": int(len(src)), "bld_assigned": int((b.comid > 0).sum())})
    return res
