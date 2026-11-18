"""Step 00 — download static national inputs (resumable, idempotent).

Groups (select with --only):
  census   county list, ZCTA polygons (TIGER 2024), 2020 block->ZCTA relationship
  hydro    WBD national GDB, NHDPlus V2.1 national seamless (Lower 48)
  atlas14  NOAA Atlas 14 24-hour precipitation-frequency grids (AMS where published)
  coast    COAST-RP storm-tide return levels (global, CC-BY 4.0)
  cckp     CMIP6 0.25deg rx1day / rx5day ensemble climatologies & anomalies (World Bank CCKP)
  ddf      USACE (go-consequences) and Hazus/FAST depth-damage tables
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.http import download  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402

RPS_ATLAS14 = [2, 5, 10, 25, 50, 100, 200, 500, 1000]
ATLAS14_AMS = ["sw", "orb", "mw", "se", "ne", "tx", "inw", "pr", "hi", "ak"]
ATLAS14_PDS_ONLY = ["sa"]
CCKP = "https://wbg-cckp.s3.amazonaws.com/data/cmip6-x0.25"
FAST = "https://raw.githubusercontent.com/nhrap-hazus/FAST/main/Lookuptables"


def jobs(group: str) -> list[tuple[str, Path]]:
    raw = path("raw")
    J: list[tuple[str, Path]] = []
    if group == "census":
        J += [
            ("https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt",
             raw / "census" / "national_county2020.txt"),
            ("https://www2.census.gov/geo/tiger/TIGER2024/ZCTA520/tl_2024_us_zcta520.zip",
             raw / "census" / "tl_2024_us_zcta520.zip"),
            ("https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_tabblock20_natl.txt",
             raw / "census" / "tab20_zcta520_tabblock20_natl.txt"),
            ("https://www2.census.gov/geo/tiger/TIGER2024/STATE/tl_2024_us_state.zip",
             raw / "census" / "tl_2024_us_state.zip"),
        ]
    elif group == "hydro":
        J += [
            ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/WBD/National/GDB/WBD_National_GDB.zip",
             raw / "wbd" / "WBD_National_GDB.zip"),
            ("https://dmap-data-commons-ow.s3.amazonaws.com/NHDPlusV21/Data/NationalData/"
             "NHDPlusV21_NationalData_Seamless_Geodatabase_Lower48_07.7z",
             raw / "nhdplus" / "NHDPlusV21_NationalData_Seamless_Geodatabase_Lower48_07.7z"),
        ]
    elif group == "atlas14":
        base = "https://hdsc.nws.noaa.gov/pub/hdsc/data"
        for reg in ATLAS14_AMS:
            for rp in RPS_ATLAS14:
                f = f"{reg}{rp}yr24ha_ams.zip"
                J.append((f"{base}/{reg}/{f}", raw / "atlas14" / reg / f))
        for reg in ATLAS14_PDS_ONLY:
            for rp in RPS_ATLAS14:
                f = f"{reg}{rp}yr24ha.zip"
                J.append((f"{base}/{reg}/{f}", raw / "atlas14" / reg / f))
    elif group == "landcover":
        b = "https://www.mrlc.gov/downloads/sciweb1/shared/mrlc/data-bundles"
        for f in ("Annual_NLCD_LndCov_2024_CU_C1V1.zip", "Annual_NLCD_FctImp_2024_CU_C1V1.zip"):
            J.append((f"{b}/{f}", raw / "nlcd" / f))
    elif group == "coast":
        b = "https://data.4tu.nl/file/4e291b8f-a37e-4378-8ca6-954a44fdc8fb"
        J += [
            (f"{b}/c06c2f29-82d6-4439-8ee5-b694371bfb38", raw / "coast_rp" / "README.txt"),
            (f"{b}/26dcd904-2be5-457e-af95-7935a32c7466", raw / "coast_rp" / "COAST-RP.nc"),
            (f"{b}/1263247c-4427-40eb-b497-a79f72caa267", raw / "coast_rp" / "COAST-RP_TC.nc"),
            (f"{b}/e446e8bc-136c-42f2-9be5-6f35f0143c82", raw / "coast_rp" / "COAST-RP_ETC.nc"),
        ]
    elif group == "cckp":
        for var in ("rx1day", "rx5day"):
            for stat in ("median", "p10", "p90"):
                f = f"climatology-{var}-annual-mean_cmip6-x0.25_ensemble-all-historical_climatology_{stat}_1995-2014.nc"
                J.append((f"{CCKP}/{var}/ensemble-all-historical/{f}", raw / "cckp" / var / f))
                for ssp in ("ssp245", "ssp585"):
                    for win in ("2020-2039", "2040-2059", "2060-2079"):
                        f = (f"anomaly-{var}-annual-mean_cmip6-x0.25_ensemble-all-{ssp}"
                             f"_climatology_{stat}_{win}.nc")
                        J.append((f"{CCKP}/{var}/ensemble-all-{ssp}/{f}", raw / "cckp" / var / f))
    elif group == "ddf":
        for f in ("Building_DDF_Riverine_LUT_Hazus4p0.csv", "Building_DDF_CoastalA_LUT_Hazus4p0.csv",
                  "Building_DDF_CoastalV_LUT_Hazus4p0.csv", "Content_DDF_Riverine_LUT_Hazus4p0.csv",
                  "Content_DDF_CoastalA_LUT_Hazus4p0.csv", "Content_DDF_CoastalV_LUT_Hazus4p0.csv",
                  "flBldgStructDmgFn.csv", "flBldgContDmgFn.csv", "OccupancyTypes.csv",
                  "DefaultDDFAssignmentTables.xlsx", "README.txt"):
            J.append((f"{FAST}/{f}", raw / "ddf" / "hazus_fast" / f))
        J.append(("https://raw.githubusercontent.com/USACE/go-consequences/main/structures/occtypes.json",
                  raw / "ddf" / "usace" / "occtypes.json"))
    else:
        raise ValueError(group)
    return J


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=["census", "atlas14", "coast", "cckp", "ddf", "hydro"])
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    log = setup("00_download_static")
    todo = [j for g in args.only for j in jobs(g)]
    log.info("%d files queued (%s)", len(todo), ", ".join(args.only))
    failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(download, url, dest): (url, dest) for url, dest in todo}
        for fut in as_completed(futs):
            url, dest = futs[fut]
            try:
                fut.result()
                log.info("ok   %s", dest.relative_to(path("raw")))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.error("FAIL %s <- %s : %s", dest.name, url, exc)
    log.info("done, %d failed", failed)


if __name__ == "__main__":
    main()
