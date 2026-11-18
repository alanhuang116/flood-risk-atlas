"""Step 06b — USACE National Levee Database (NLD): levee systems and leveed areas.

Uses the public NLD web API (https://levees.sec.usace.army.mil, the same endpoints as the
website's search and map):
  systems   : /api-local/systems/query?in=@STATE:<state name>
  geometry  : /api-local/geometries/query?type=leveedArea&systemId=<id>&format=geo&props=true&coll=true
  attributes: /api-local/leveed-areas/<leveed area id>   (FEMA accreditation, overtopping AEP, ...)
Outputs (resumable via per-system JSON cache):
  data/raw/nld/systems.parquet
  data/raw/nld/leveed_areas.parquet  (GeoParquet, EPSG:4326)
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.http import session  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402

API = "https://levees.sec.usace.army.mil/api-local/"
STATES = ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
          "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas",
          "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
          "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
          "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
          "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
          "West Virginia", "Wisconsin", "Wyoming", "Puerto Rico"]


def get_json(url: str, **params):
    r = session().get(url, params=params, timeout=180)
    r.raise_for_status()
    return r.json()


def systems_for(state: str) -> list[dict]:
    return get_json(API + "systems/query", **{"in": f"@STATE:{state}"})


def fetch_system(sid: int, cache: Path) -> dict:
    fp = cache / f"{sid}.json"
    if fp.exists():
        return json.loads(fp.read_text(encoding="utf-8"))
    fc = get_json(API + "geometries/query", type="leveedArea", systemId=sid, format="geo", props="true", coll="true")
    feats = fc.get("features", []) if isinstance(fc, dict) else []
    for f in feats:
        la_id = f.get("properties", {}).get("id")
        if la_id is not None:
            try:
                f["properties"]["attrs"] = get_json(API + f"leveed-areas/{la_id}")
            except Exception:  # noqa: BLE001
                f["properties"]["attrs"] = {}
    out = {"systemId": sid, "features": feats}
    fp.write_text(json.dumps(out), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    log = setup("06b_download_nld")
    od = path("raw", "nld", mkdir=True)
    cache = path("raw", "nld", "systems_json", mkdir=True)

    rows = []
    with ThreadPoolExecutor(args.workers) as ex:
        for st, res in zip(STATES, ex.map(systems_for, STATES)):
            log.info("%s: %d systems", st, len(res))
            rows += res
    sysdf = pd.DataFrame(rows).drop_duplicates("systemId")
    keep = [c for c in sysdf.columns if sysdf[c].map(lambda v: not isinstance(v, (list, dict))).all()]
    sysdf[keep].to_parquet(od / "systems.parquet", index=False)
    log.info("%d unique levee systems", len(sysdf))

    import geopandas as gpd
    from shapely.geometry import shape
    recs = []
    with ThreadPoolExecutor(args.workers) as ex:
        for k, res in enumerate(ex.map(lambda s: fetch_system(int(s), cache), sysdf.systemId), 1):
            for f in res["features"]:
                p, a = f.get("properties", {}), f.get("properties", {}).get("attrs", {}) or {}
                try:
                    g = shape(f["geometry"])
                except Exception:  # noqa: BLE001
                    continue
                recs.append({"leveed_area_id": p.get("id"), "systemId": res["systemId"], "name": p.get("name"),
                             "fema_accreditation": a.get("femaAccreditationRating"),
                             "overtopping_aep": a.get("overtoppingAep"), "buildings": a.get("buildings"),
                             "area_sqmi": a.get("areaSquareMiles"), "source": a.get("leveedAreaSource"),
                             "geometry": g})
            if k % 500 == 0:
                log.info("geometries: %d / %d systems", k, len(sysdf))
    gdf = gpd.GeoDataFrame(recs, geometry="geometry", crs="EPSG:4326")
    gdf["geometry"] = gdf.geometry.force_2d() if hasattr(gdf.geometry, "force_2d") else gdf.geometry
    gdf.to_parquet(od / "leveed_areas.parquet", index=False)
    log.info("leveed areas: %d polygons; accreditation: %s", len(gdf),
             gdf.fema_accreditation.value_counts(dropna=False).to_dict())


if __name__ == "__main__":
    main()
