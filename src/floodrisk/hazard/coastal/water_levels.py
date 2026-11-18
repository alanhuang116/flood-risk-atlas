"""Extreme coastal water levels (storm tide + sea-level rise) in NAVD88.

* Storm tide return levels: COAST-RP (Dullaart et al. 2021, GTSM + STORM synthetic TCs +
  ERA5 extratropical storms), metres relative to mean sea level, RP 1-1000 yr.
* MSL -> NAVD88: NOAA CO-OPS tidal datums (NTDE 1983-2001) at the nearest water-level
  station with both MSL and NAVD88 published.
* Sea-level rise: IPCC AR6 (see floodrisk.hazard.climate.SeaLevel), metres rel. 1995-2014.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import numpy as np
import pandas as pd
import xarray as xr
from scipy.spatial import cKDTree

from floodrisk.config import path
from floodrisk.utils.http import session

log = logging.getLogger(__name__)

RPS = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000]
US_BOXES = [(-125.5, 24.0, -66.0, 50.0),   # CONUS
            (-161.0, 18.5, -154.0, 23.0),  # Hawaii
            (-68.0, 17.5, -64.0, 18.8),    # Puerto Rico / USVI
            (-180.0, 50.0, -129.0, 72.0)]  # Alaska
COOPS = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi"


@lru_cache(maxsize=1)
def coast_rp() -> pd.DataFrame:
    ds = xr.open_dataset(path("raw", "coast_rp", "COAST-RP.nc"))
    x = ds["station_x_coordinate"].values.astype(float)
    y = ds["station_y_coordinate"].values.astype(float)
    keep = np.zeros(len(x), bool)
    for x0, y0, x1, y1 in US_BOXES:
        keep |= (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
    df = pd.DataFrame({"station": ds["station_id"].values[keep], "lon": x[keep], "lat": y[keep]})
    for rp in RPS:
        df[f"st{rp}"] = ds[f"storm_tide_rp_{rp:04d}"].values[keep].astype(float)
    ds.close()
    df = df.dropna(subset=[f"st{rp}" for rp in RPS]).reset_index(drop=True)
    return df


def fetch_coops_datums(refresh: bool = False) -> pd.DataFrame:
    """MSL relative to NAVD88 (m) at NOAA CO-OPS water-level stations (cached)."""
    out = path("raw", "coops", mkdir=True) / "station_datums.parquet"
    if out.exists() and not refresh:
        return pd.read_parquet(out)
    st = session().get(f"{COOPS}/stations.json", params={"type": "waterlevels"}, timeout=60).json()["stations"]
    rows = []
    for s in st:
        try:
            d = session().get(f"{COOPS}/stations/{s['id']}/datums.json", params={"units": "metric"},
                              timeout=30).json()
        except Exception:  # noqa: BLE001
            continue
        vals = {x["name"]: x.get("value") for x in d.get("datums") or []}
        navd, msl = vals.get("NAVD88"), vals.get("MSL")
        if navd is None or msl is None:
            continue
        rows.append({"id": s["id"], "name": s.get("name"), "lon": float(s["lng"]), "lat": float(s["lat"]),
                     "msl_navd88_m": float(msl) - float(navd), "epoch": d.get("epoch")})
    df = pd.DataFrame(rows)
    df.to_parquet(out, index=False)
    log.info("CO-OPS datums for %d stations", len(df))
    return df


def _xyz(lon, lat):
    lo, la = np.radians(lon), np.radians(lat)
    return np.column_stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])


@lru_cache(maxsize=1)
def station_levels_navd88(max_datum_km: float = 150.0) -> pd.DataFrame:
    """COAST-RP stations with storm-tide levels converted to NAVD88 (columns wl{rp}, m)."""
    df = coast_rp().copy()
    dat = fetch_coops_datums()
    dat = dat[np.isfinite(dat.msl_navd88_m) & (dat.msl_navd88_m.abs() < 3)]
    tree = cKDTree(_xyz(dat.lon.values, dat.lat.values))
    dist, idx = tree.query(_xyz(df.lon.values, df.lat.values))
    km = dist * 6371.0
    off = dat.msl_navd88_m.values[idx]
    df["msl_navd88_m"] = np.where(km <= max_datum_km, off, 0.0)
    df["datum_station"] = np.where(km <= max_datum_km, dat.id.values[idx], "")
    df["datum_km"] = km
    for rp in RPS:
        df[f"wl{rp}"] = df[f"st{rp}"] + df["msl_navd88_m"]
    return df
