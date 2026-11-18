"""Coastal inundation per HUC-8: hydraulically connected "bathtub" with inland attenuation.

For each scenario s and return period T:
    WL(cell) = storm_tide_T(nearest COAST-RP station, NAVD88) + SLR_s(nearest AR6 location)
               - alpha * distance_to_sea(cell)
    flooded  = 8-connected cells with z < WL that are connected to sea seeds
    depth_b  = WL(cell) - ground elevation of the building (NSI, USGS 10 m)
Sea seeds: NLCD open water (class 11) or DEM no-data, with elevation <= 1 m, forming
components that touch the domain edge (open coast, bays, tidal rivers).
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import ndimage
from scipy.spatial import cKDTree
from pyproj import Transformer

from floodrisk.config import return_periods, settings
from floodrisk.hazard import domain
from floodrisk.hazard.climate import SeaLevel, scenario_list
from floodrisk.hazard.coastal.water_levels import station_levels_navd88

log = logging.getLogger(__name__)
ALPHA_M_PER_KM = 0.25          # inland attenuation of storm-tide level (calibrated on FEMA BFEs)
SEED_MAX_ELEV = 1.0
_TO_LL = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)
_TO_ALB = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)


def coastal_huc8s(max_km: float = 30.0) -> list[str]:
    """HUC-8s whose polygon lies within ``max_km`` of a COAST-RP station."""
    st = station_levels_navd88()
    x, y = _TO_ALB.transform(st.lon.values, st.lat.values)
    import geopandas as gpd
    pts = gpd.GeoSeries(gpd.points_from_xy(x, y), crs="EPSG:5070").buffer(max_km * 1000)
    hucs = domain.huc8_table()
    hit = gpd.sjoin(hucs[["huc8", "geometry"]], gpd.GeoDataFrame(geometry=pts), predicate="intersects")
    return sorted(hit.huc8.unique())


def sea_seeds(dem: np.ndarray, nlcd: np.ndarray) -> np.ndarray:
    water = ((nlcd == 11) | ~np.isfinite(dem)) & ~(np.isfinite(dem) & (dem > SEED_MAX_ELEV))
    lab, n = ndimage.label(water, structure=np.ones((3, 3)))
    if n == 0:
        return water
    edge = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    edge = edge[edge > 0]
    # also keep large water bodies (>= 20 km2 at 30 m) near sea level (big bays not touching edge)
    sizes = ndimage.sum_labels(np.ones_like(lab), lab, index=np.arange(1, n + 1))
    big = np.flatnonzero(sizes >= 20e6 / 900.0) + 1
    keep = np.zeros(n + 1, bool)
    keep[edge] = True
    keep[big] = True
    return keep[lab]


def process_huc8(huc8: str, buildings: pd.DataFrame, out_dir: Path, sea: SeaLevel,
                 alpha: float = ALPHA_M_PER_KM, buffer_m: float = 8000.0) -> dict:
    geom = domain.huc8_geom(huc8)
    grid = domain.make_grid(geom, buffer=buffer_m)
    dem = domain.read_dem(grid)
    nlcd = domain.read_nlcd(grid)
    seeds = sea_seeds(dem, nlcd)
    res = {"huc8": huc8, "buildings": int(len(buildings)), "seed_cells": int(seeds.sum())}
    out_fp = out_dir / "buildings" / f"{huc8}.parquet"
    out_fp.parent.mkdir(parents=True, exist_ok=True)
    rps = return_periods()
    scen = scenario_list()
    cols = [f"{s}_rp{T}" for s, _, _ in scen for T in rps]
    if seeds.sum() == 0 or len(buildings) == 0:
        pd.DataFrame({"fd_id": buildings.fd_id.values}).assign(**{c: np.float32(0) for c in cols}).to_parquet(out_fp)
        return res

    # distance to sea (km) and nearest station per cell
    dist_km = ndimage.distance_transform_edt(~seeds) * grid.res / 1000.0
    st = station_levels_navd88()
    sx, sy = _TO_ALB.transform(st.lon.values, st.lat.values)
    tree = cKDTree(np.column_stack([sx, sy]))
    ny, nx = dem.shape
    xs = grid.transform.c + (np.arange(nx) + 0.5) * grid.res
    ys = grid.transform.f - (np.arange(ny) + 0.5) * grid.res
    # nearest station on a coarse 300 m lattice (fast) then upsample
    step = 10
    cx, cy = np.meshgrid(xs[::step], ys[::step])
    _, sidx_c = tree.query(np.column_stack([cx.ravel(), cy.ravel()]))
    sidx = np.repeat(np.repeat(sidx_c.reshape(cx.shape), step, 0), step, 1)[:ny, :nx]

    # building cells
    r, c = grid.rowcol(buildings.x5070.to_numpy(), buildings.y5070.to_numpy())
    inside = grid.inside(r, c)
    rr, cc = np.where(inside, r, 0), np.where(inside, c, 0)
    zb = buildings.grnd_elv_m.to_numpy(dtype=np.float64)
    zb = np.where(np.isfinite(zb) & (zb > -100), zb, dem[rr, cc])
    lon_c, lat_c = _TO_LL.transform(np.mean(xs), np.mean(ys))

    zfill = np.where(np.isfinite(dem), dem, -10.0)
    out = {"fd_id": buildings.fd_id.values}
    wl_cols = [f"wl{T}" for T in rps]
    base_wl = st[wl_cols].to_numpy()[sidx]          # (ny, nx, nrp)
    for s_id, ssp, year in scen:
        slr = float(sea.at(ssp, year, np.array([lon_c]), np.array([lat_c]))[0])
        for k, T in enumerate(rps):
            wl = base_wl[..., k] + slr - alpha * dist_km
            wet = (zfill < wl) | seeds
            lab, _ = ndimage.label(wet, structure=np.ones((3, 3)))
            seed_labels = np.unique(lab[seeds])
            conn = np.isin(lab, seed_labels[seed_labels > 0])
            d = np.where(inside & conn[rr, cc], wl[rr, cc] - zb, 0.0)
            out[f"{s_id}_rp{T}"] = np.clip(d, 0, None).astype(np.float32)
    from floodrisk.utils.http import atomic_parquet
    atomic_parquet(pd.DataFrame(out), out_fp)
    res["flooded_100_base"] = int((out[f"baseline_rp100"] > 0).sum())
    return res
