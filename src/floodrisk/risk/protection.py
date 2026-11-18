"""Flood-defence standard (return period, yr) of every residential building.

Sources (the highest applicable standard wins):
  * FEMA FIRM zones in the NSI: X "area with reduced flood risk due to levee" (accredited levee) and
    A99 (federal flood-protection system)                       -> risk.protection.fema_levee_rp
  * USACE National Levee Database leveed areas (data/raw/nld/leveed_areas.parquet):
      overtopping AEP reported                                  -> 1 / AEP
      FEMA-accredited / provisionally accredited system         -> risk.protection.nld_accredited_rp
      other systems                                             -> risk.protection.nld_other_rp
The implicit coastal defence rule (deep flooding by today's 2-yr storm tide) is applied in the
risk engine because it needs the coastal depths.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import numpy as np
import pandas as pd

from floodrisk.config import path

log = logging.getLogger(__name__)


def fema_protection(ex: pd.DataFrame, rp: float) -> np.ndarray:
    fz = ex.firmzone.fillna("").astype(str).str.upper().str.strip()
    zs = ex.zone_sub.fillna("").astype(str).str.upper()
    levee_x = zs.str.contains("LEVEE") & (zs.str.contains("REDUCED")) & ~fz.eq("D")
    a99 = fz.eq("A99")
    return np.where((levee_x | a99).to_numpy(), float(rp), 0.0)


def _nld_rp(df: pd.DataFrame, accredited_rp: float, other_rp: float) -> np.ndarray:
    aep = pd.to_numeric(df.overtopping_aep, errors="coerce").to_numpy(float)
    aep = np.where(aep > 0.5, aep / 100.0, aep)              # tolerate percent units
    acc = df.fema_accreditation.fillna("").astype(str).str.upper()
    is_acc = (acc.str.contains("ACCREDITED") & ~acc.str.contains("NON")).to_numpy()
    T = np.where(is_acc, accredited_rp, other_rp).astype(float)
    ok = np.isfinite(aep) & (aep > 0) & (aep <= 0.5)
    T[ok] = np.clip(1.0 / aep[ok], 2.0, 10000.0)
    return T


@lru_cache(maxsize=4)
def load_leveed_areas(accredited_rp: float, other_rp: float):
    """Leveed-area polygons in EPSG:5070 with their defence standard ``T`` (None if not downloaded)."""
    fp = path("raw", "nld", "leveed_areas.parquet")
    if not fp.exists():
        log.warning("NLD leveed areas not found (%s) - FEMA levee zones only", fp)
        return None
    import geopandas as gpd
    g = gpd.read_parquet(fp)
    g = g[g.geometry.notna() & ~g.geometry.is_empty].to_crs("EPSG:5070")
    g["geometry"] = g.geometry.buffer(0)
    g["T"] = _nld_rp(g, accredited_rp, other_rp)
    return g[["leveed_area_id", "systemId", "T", "geometry"]].reset_index(drop=True)


def nld_protection(ex: pd.DataFrame, accredited_rp: float, other_rp: float) -> np.ndarray:
    g = load_leveed_areas(float(accredited_rp), float(other_rp))
    out = np.zeros(len(ex))
    if g is None or len(ex) == 0:
        return out
    import shapely
    x, y = ex.x5070.to_numpy(float), ex.y5070.to_numpy(float)
    xmin, ymin, xmax, ymax = np.nanmin(x), np.nanmin(y), np.nanmax(x), np.nanmax(y)
    sub = g.cx[xmin:xmax, ymin:ymax]
    if len(sub) == 0:
        return out
    pts = shapely.points(x, y)
    tree = shapely.STRtree(sub.geometry.values)
    bi, pi = tree.query(pts, predicate="within")             # (building index, polygon index)
    if len(bi):
        np.maximum.at(out, bi, sub["T"].to_numpy(float)[pi])
    return out


def protection_T(ex: pd.DataFrame, prm) -> np.ndarray:
    """Per-building defence standard for fluvial + coastal flooding (0 = undefended)."""
    t_fema = fema_protection(ex, prm.fema_levee_rp)
    t_nld = nld_protection(ex, prm.nld_accredited_rp, prm.nld_other_rp)
    return np.maximum(t_fema, t_nld)
