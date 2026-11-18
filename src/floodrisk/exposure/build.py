"""Build the national residential exposure table from raw NSI county files.

Steps
  1. keep residential occupancies (RES1 single-family, RES2 manufactured, RES3A-F multi-family);
  2. de-duplicate structures (CT counties are queried under both county schemes);
  3. assign the 2020 ZCTA (5-digit ZIP proxy) from the 2020 census block via the official
     Census block->ZCTA relationship file (no spatial join needed);
  4. derive value fields: full replacement cost of structure and contents (USD, NSI price level);
  5. assign HUC-8 watershed (WBD) for hazard processing; write one Parquet per state.
"""
from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from floodrisk.config import path

log = logging.getLogger(__name__)

RES_SQL = "st_damcat = 'RES' AND (occtype LIKE 'RES1%' OR occtype LIKE 'RES2%' OR occtype LIKE 'RES3%')"


def _rel_file() -> Path:
    return path("raw", "census", "tab20_zcta520_tabblock20_natl.txt")


def stage_residential(con: duckdb.DuckDBPyConnection | None = None, src_files: list[Path] | None = None,
                      out: Path | None = None) -> Path:
    """DuckDB pass: filter + dedupe + block->ZCTA join -> interim parquet (one file)."""
    con = con or duckdb.connect()
    con.execute("PRAGMA threads=16")
    if src_files:
        src = "[" + ", ".join(f"'{Path(f).as_posix()}'" for f in src_files) + "]"
    else:
        src = "'" + (path("raw", "nsi") / "*" / "*.parquet").as_posix() + "'"
    rel = _rel_file().as_posix()
    out = Path(out or (path("interim", "exposure", mkdir=True) / "residential_stage.parquet"))
    out.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE blk AS
        SELECT GEOID_TABBLOCK_20 AS cbfips, GEOID_ZCTA5_20 AS zcta
        FROM read_csv('{rel}', delim='|', header=true, all_varchar=true)
        WHERE GEOID_ZCTA5_20 IS NOT NULL AND GEOID_ZCTA5_20 <> ''
    """)
    con.execute(f"""
        COPY (
          WITH r AS (
            SELECT *, row_number() OVER (PARTITION BY fd_id ORDER BY fd_id) AS rn
            FROM read_parquet({src}, union_by_name=true)
            WHERE {RES_SQL}
          )
          SELECT r.fd_id, r.x, r.y, r.cbfips,
                 substr(r.cbfips, 1, 2) AS state_fips, substr(r.cbfips, 1, 5) AS county_fips,
                 b.zcta,
                 r.occtype, split_part(r.occtype, '-', 1) AS occ_main,
                 r.bldgtype, r.found_type, r.found_ht, r.num_story, r.sqft, r.resunits,
                 r.val_struct, r.val_cont, r.fullrep,
                 CASE WHEN r.val_struct > 0 THEN r.val_cont * r.fullrep / r.val_struct
                      ELSE r.val_cont END AS cont_fullrep,
                 r.ground_elv AS ground_elv_ft, r.grnd_elv_m,
                 r.firmzone, r.zone_sub,
                 CASE WHEN r.static_bfe > -9000 THEN r.static_bfe END AS static_bfe_ft,
                 r.med_yr_blt, r.pop_night, r.usastrucid
          FROM r LEFT JOIN blk b USING (cbfips)
          WHERE r.rn = 1
        ) TO '{out.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 1000000)
    """)
    n = con.execute(f"SELECT count(*), count(zcta) FROM read_parquet('{out.as_posix()}')").fetchone()
    log.info("staged %s residential structures (%s with ZCTA from block relationship)", f"{n[0]:,}", f"{n[1]:,}")
    return out


def load_huc8() -> gpd.GeoDataFrame:
    gdb = path("raw", "wbd", "WBD_National_GDB.gdb")
    if not gdb.exists():
        import zipfile
        with zipfile.ZipFile(path("raw", "wbd", "WBD_National_GDB.zip")) as z:
            z.extractall(path("raw", "wbd"))
    g = gpd.read_file(gdb, layer="WBDHU8", columns=["huc8", "name", "states", "areasqkm"],
                      engine="pyogrio")
    return g.to_crs("EPSG:4326")


def load_zcta() -> gpd.GeoDataFrame:
    zf = path("raw", "census", "tl_2024_us_zcta520.zip")
    g = gpd.read_file(f"zip://{zf.as_posix()}", engine="pyogrio", columns=["ZCTA5CE20"])
    return g.rename(columns={"ZCTA5CE20": "zcta"}).to_crs("EPSG:4326")


def _assign_polygon_ids(x: np.ndarray, y: np.ndarray, polys: gpd.GeoDataFrame, id_col: str,
                        nearest_fallback: bool = True) -> np.ndarray:
    pts = shapely.points(x, y)
    tree = shapely.STRtree(polys.geometry.values)
    ip, ig = tree.query(pts, predicate="intersects")
    out = np.full(len(pts), None, dtype=object)
    ids = polys[id_col].to_numpy()
    out[ip] = ids[ig]
    miss = np.flatnonzero(pd.isna(out))
    if nearest_fallback and len(miss):
        ip2, ig2 = tree.query_nearest(pts[miss], return_distance=False)
        out[miss[ip2]] = ids[ig2]
    return out


class Huc8Raster:
    """HUC-8 lookup on a 150 m CONUS Albers raster (fast point-in-polygon for ~100 M buildings).

    150 m precision is sufficient: every hazard domain is buffered by >= 600 m beyond its HUC-8.
    Points outside the raster or on no-data cells fall back to exact polygon tests.
    """

    RES = 150.0
    BOUNDS = (-2_400_000.0, 200_000.0, 2_300_000.0, 3_200_000.0)   # EPSG:5070, CONUS

    def __init__(self, huc8_ll: gpd.GeoDataFrame):
        from rasterio import features
        from rasterio.transform import from_origin
        g = huc8_ll.to_crs("EPSG:5070").reset_index(drop=True)
        x0, y0, x1, y1 = self.BOUNDS
        self.w, self.h = int((x1 - x0) / self.RES), int((y1 - y0) / self.RES)
        self.tf = from_origin(x0, y1, self.RES, self.RES)
        self.ids = g.huc8.to_numpy()
        shapes = ((geom, i + 1) for i, geom in enumerate(g.geometry.values) if geom is not None)
        self.grid = features.rasterize(shapes, out_shape=(self.h, self.w), transform=self.tf,
                                       fill=0, dtype="int32")
        from pyproj import Transformer
        self.to_alb = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)

    def lookup(self, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        x, y = self.to_alb.transform(lon, lat)
        c = np.floor((x - self.tf.c) / self.RES).astype(np.int64)
        r = np.floor((self.tf.f - y) / self.RES).astype(np.int64)
        ok = (r >= 0) & (r < self.h) & (c >= 0) & (c < self.w)
        idx = np.zeros(len(lon), dtype=np.int64)
        idx[ok] = self.grid[r[ok], c[ok]]
        out = np.full(len(lon), None, dtype=object)
        out[idx > 0] = self.ids[idx[idx > 0] - 1]
        return out


CONUS_STATES = {"01", "04", "05", "06", "08", "09", "10", "11", "12", "13", "16", "17", "18", "19", "20", "21",
                "22", "23", "24", "25", "26", "27", "28", "29", "30", "31", "32", "33", "34", "35", "36", "37",
                "38", "39", "40", "41", "42", "44", "45", "46", "47", "48", "49", "50", "51", "53", "54", "55", "56"}


def finalize_by_state(stage: Path, out_dir: Path | None = None) -> Path:
    """Add HUC-8 and fill missing ZCTA (nearest polygon); write state partitions."""
    out_dir = Path(out_dir or path("processed", "exposure", mkdir=True))
    huc8 = load_huc8()
    hr = Huc8Raster(huc8)
    zcta = load_zcta()
    con = duckdb.connect()
    states = [r[0] for r in con.execute(
        f"SELECT DISTINCT state_fips FROM read_parquet('{stage.as_posix()}') ORDER BY 1").fetchall()]
    for st in states:
        dest = out_dir / f"state={st}" / "residential.parquet"
        if dest.exists():
            continue
        df = con.execute(f"SELECT * FROM read_parquet('{stage.as_posix()}') WHERE state_fips = '{st}'").df()
        x, y = df.x.to_numpy(), df.y.to_numpy()
        h = hr.lookup(x, y) if st in CONUS_STATES else np.full(len(df), None, dtype=object)
        miss = pd.isna(h)
        if miss.any():
            h[miss] = _assign_polygon_ids(x[miss], y[miss], huc8, "huc8")
        df["huc8"] = h
        miss = df.zcta.isna()
        df["zcta_method"] = np.where(miss, "nearest_polygon", "block_relationship")
        if miss.any():
            df.loc[miss, "zcta"] = _assign_polygon_ids(df.x[miss].to_numpy(), df.y[miss].to_numpy(),
                                                       zcta, "zcta")
        dest.parent.mkdir(parents=True, exist_ok=True)
        df.sort_values(["huc8", "fd_id"]).to_parquet(dest, index=False, compression="zstd")
        log.info("state %s: %s structures, %d HUC8s, %d ZCTAs (%d via nearest)", st, f"{len(df):,}",
                 df.huc8.nunique(), df.zcta.nunique(), int(miss.sum()))
    return out_dir
