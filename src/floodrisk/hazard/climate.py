"""Climate-change adjustment factors for each scenario-year.

* Extreme rainfall / river floods: CMIP6 multi-model ensemble (World Bank CCKP cmip6-x0.25,
  bias-adjusted, 0.25 deg).  Relative change of the mean annual-maximum precipitation
      f = 1 + anomaly(window) / climatology(1995-2014)
  using rx1day for pluvial design rainfall and rx5day for river-flood magnitudes.
  2030 = window 2020-2039; 2060 = mean of 2040-2059 and 2060-2079 (centred on 2060).
* Sea level: IPCC AR6 regional relative sea-level projections (medium confidence, total
  incl. vertical land motion), metres relative to 1995-2014, nearest AR6 location.
The baseline scenario ("present climate") uses factor 1 and AR6 sea level for 2020.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import xarray as xr
from scipy.spatial import cKDTree

from floodrisk.config import path, scenarios, settings

_CCKP = "{kind}-{var}-annual-mean_cmip6-x0.25_ensemble-all-{exp}_climatology_{stat}_{win}.nc"


def _open(var: str, kind: str, exp: str, stat: str, win: str) -> xr.DataArray:
    fp = path("raw", "cckp", var, _CCKP.format(kind=kind, var=var, exp=exp, stat=stat, win=win))
    ds = xr.open_dataset(fp)
    da = ds[f"{kind}-{var}-annual-mean"].squeeze("time", drop=True).load()
    ds.close()
    return da


@lru_cache(maxsize=64)
def precip_factor(var: str, ssp: str | None, year: int, stat: str = "median") -> xr.DataArray:
    """Multiplicative change factor (lat, lon) for ``var`` in {'rx1day','rx5day'}."""
    hist = _open(var, "climatology", "historical", "median", "1995-2014")
    if ssp is None:
        return xr.ones_like(hist)
    wins = settings()["climate"]["cckp_windows"][year]
    anom = sum(_open(var, "anomaly", ssp, stat, w) for w in wins) / len(wins)
    f = 1.0 + anom / hist.where(hist > 1.0)
    return f.fillna(1.0).clip(0.7, 2.0)


def sample_grid(da: xr.DataArray, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Bilinear sample of a regular lat/lon grid at points."""
    out = da.interp(lon=xr.DataArray(lon, dims="p"), lat=xr.DataArray(lat, dims="p"),
                    method="linear")
    v = out.values.astype(np.float64)
    if np.isnan(v).any():  # coast / edge -> nearest
        nn = da.sel(lon=xr.DataArray(lon, dims="p"), lat=xr.DataArray(lat, dims="p"),
                    method="nearest").values
        v = np.where(np.isnan(v), nn, v)
    return v


class SeaLevel:
    """AR6 relative sea-level change (m, rel. 1995-2014) at the nearest AR6 location."""

    def __init__(self, quantile: float | None = None):
        q = (quantile if quantile is not None else settings()["climate"]["slr_quantile"]) / 100.0
        self.data: dict[str, xr.DataArray] = {}
        lat = lon = None
        for ssp in ("ssp245", "ssp585"):
            ds = xr.open_dataset(path("raw", "ar6_slr", f"total_{ssp}_medium_confidence_values.nc"))
            self.data[ssp] = ds["sea_level_change"].sel(quantiles=q, method="nearest").load() / 1000.0
            lat, lon = ds["lat"].values, ds["lon"].values
            ds.close()
        ok = np.isfinite(self.data["ssp245"].sel(years=2020).values)
        self.loc_index = np.flatnonzero(ok)
        xyz = _lonlat_to_xyz(lon[ok], lat[ok])
        self.tree = cKDTree(xyz)

    def at(self, ssp: str | None, year: int, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        _, idx = self.tree.query(_lonlat_to_xyz(np.asarray(lon), np.asarray(lat)))
        locs = self.loc_index[idx]
        da = self.data[ssp or "ssp245"]
        yr = 2020 if ssp is None else year
        return da.sel(years=yr).values[locs]


def _lonlat_to_xyz(lon, lat):
    lo, la = np.radians(lon), np.radians(lat)
    return np.column_stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])


def scenario_list() -> list[tuple[str, str | None, int]]:
    return [(k, v["ssp"], int(v["year"])) for k, v in scenarios().items()]
