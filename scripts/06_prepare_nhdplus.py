"""Step 06 — NHDPlus V2.1 flowlines -> per-HUC8 GeoParquet (EPSG:5070).

Keeps COMID, REACHCODE, FTYPE, stream order, total drainage area, length, slope and the EROM
mean annual flow (QE_MA, converted to m3/s). The HUC-8 of a flowline is the first 8 digits
of its REACHCODE.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pyogrio  # noqa: E402
import shapely  # noqa: E402

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402

SEVENZIP = r"C:\Program Files\7-Zip\7z.exe"
CFS_TO_CMS = 0.0283168


def main() -> None:
    log = setup("06_prepare_nhdplus")
    src = path("raw", "nhdplus", "NHDPlusV21_NationalData_Seamless_Geodatabase_Lower48_07.7z")
    ext = path("raw", "nhdplus", "extracted", mkdir=True)
    gdbs = list(ext.rglob("*.gdb"))
    if not gdbs:
        log.info("extracting %s", src.name)
        subprocess.run([SEVENZIP, "x", "-y", f"-o{ext}", str(src)], check=True, capture_output=True)
        gdbs = list(ext.rglob("*.gdb"))
    gdb = gdbs[0]
    layers = [l[0] for l in pyogrio.list_layers(gdb)]
    log.info("gdb %s layers: %s", gdb.name, layers)
    lyr = "NHDFlowline_Network" if "NHDFlowline_Network" in layers else [l for l in layers if "Flowline" in l][0]
    info = pyogrio.read_info(gdb, layer=lyr)
    fields = list(info["fields"])
    want = ["COMID", "REACHCODE", "FTYPE", "StreamOrde", "TotDASqKM", "LENGTHKM", "SLOPE", "QE_MA",
            "Divergence", "Hydroseq", "DnHydroseq", "GNIS_NAME"]
    have = {f.lower(): f for f in fields}
    cols = [have[w.lower()] for w in want if w.lower() in have]
    log.info("reading %s (%d features) columns=%s", lyr, info["features"], cols)
    g = gpd.read_file(gdb, layer=lyr, columns=cols, engine="pyogrio")
    g.columns = [c.lower() if c != "geometry" else c for c in g.columns]
    g["geometry"] = shapely.force_2d(g.geometry.values)
    g = g.to_crs("EPSG:5070")
    g["huc8"] = g.reachcode.str[:8]
    g["length_m"] = g.lengthkm * 1000.0
    g["slope"] = np.where(g.slope > 0, g.slope, np.nan)
    if "qe_ma" in g:
        g["qe_ma_cms"] = np.where(g.qe_ma > 0, g.qe_ma * CFS_TO_CMS, np.nan)
    out = path("interim", "nhdplus", "flowlines_by_huc8", mkdir=True)
    for h, part in g.groupby("huc8"):
        part.drop(columns=["huc8"]).to_parquet(out / f"{h}.parquet", index=False)
    g.drop(columns="geometry").to_parquet(path("interim", "nhdplus") / "flowline_attributes.parquet", index=False)
    log.info("wrote %d HUC8 partitions (%d flowlines)", g.huc8.nunique(), len(g))


if __name__ == "__main__":
    main()
