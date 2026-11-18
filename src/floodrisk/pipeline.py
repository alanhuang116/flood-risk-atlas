"""Shared helpers for the per-HUC-8 pipeline drivers."""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import Transformer

from floodrisk.config import path

log = logging.getLogger(__name__)
_TO_ALB = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)


def exposure_root() -> Path:
    """Exposure partitions; override with FLOODRISK_EXPOSURE_ROOT (e.g. for the pilot)."""
    import os
    env = os.environ.get("FLOODRISK_EXPOSURE_ROOT")
    return Path(env) if env else path("processed", "exposure")


def load_buildings(huc8: str, root: Path | None = None) -> pd.DataFrame:
    """Residential buildings of one HUC-8 (all states), with EPSG:5070 coordinates."""
    root = Path(root or exposure_root())
    glob = (root / "state=*" / "residential.parquet").as_posix()
    pf = path("interim", "postfirm", "bg_postfirm.parquet")       # post-FIRM share of SFHA homes (step 04b)
    if pf.exists():
        df = duckdb.sql(f"""SELECT e.*, p.p_post FROM read_parquet('{glob}', hive_partitioning=true) e
                            LEFT JOIN read_parquet('{pf.as_posix()}') p ON substr(e.cbfips, 1, 12) = p.bg12
                            WHERE e.huc8 = '{huc8}' ORDER BY e.fd_id""").df()
    else:
        df = duckdb.sql(f"SELECT * FROM read_parquet('{glob}', hive_partitioning=true) WHERE huc8 = '{huc8}'").df()
    if len(df):
        x, y = _TO_ALB.transform(df.x.to_numpy(), df.y.to_numpy())
        df["x5070"], df["y5070"] = x, y
    return df


CONUS_HUC2 = tuple(f"{i:02d}" for i in range(1, 19))   # v0.1 hazard coverage: HUC regions 01-18


def huc8_list(root: Path | None = None, conus_only: bool = True) -> pd.DataFrame:
    """HUC-8s with residential buildings, most buildings first (so important watersheds finish first)."""
    root = Path(root or exposure_root())
    glob = (root / "state=*" / "residential.parquet").as_posix()
    df = duckdb.sql(f"SELECT huc8, count(*) AS n FROM read_parquet('{glob}', hive_partitioning=true) "
                    f"WHERE huc8 IS NOT NULL GROUP BY huc8 ORDER BY n DESC").df()
    if conus_only:
        df = df[df.huc8.str[:2].isin(CONUS_HUC2)].reset_index(drop=True)
    return df


def flowlines_for(huc8: str, buffer_m: float = 3000.0) -> gpd.GeoDataFrame:
    """NHDPlus flowlines of the HUC-8 and of neighbouring HUC-8s intersecting its buffered domain."""
    from floodrisk.hazard import domain
    t = domain.huc8_table()
    geom = domain.huc8_geom(huc8).buffer(buffer_m)
    neigh = t.loc[t.intersects(geom), "huc8"].tolist()
    d = path("interim", "nhdplus", "flowlines_by_huc8")
    parts = [gpd.read_parquet(d / f"{h}.parquet") for h in neigh if (d / f"{h}.parquet").exists()]
    if not parts:
        return gpd.GeoDataFrame(columns=["comid", "totdasqkm", "geometry"], geometry="geometry", crs="EPSG:5070")
    g = pd.concat(parts, ignore_index=True)
    g = gpd.GeoDataFrame(g, geometry="geometry", crs="EPSG:5070")
    return g[g.intersects(geom)].reset_index(drop=True)


@lru_cache(maxsize=1)
def flood_quantiles() -> pd.DataFrame:
    return pd.read_parquet(path("interim", "nwm", "flood_quantiles.parquet"))


@lru_cache(maxsize=1)
def qmean_by_comid() -> pd.Series:
    a = pd.read_parquet(path("interim", "nhdplus", "flowline_attributes.parquet"), columns=["comid", "qe_ma_cms"])
    return a.set_index("comid").qe_ma_cms
