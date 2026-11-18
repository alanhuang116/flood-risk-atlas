# Flood Risk Atlas — US residential flood damage, climate-adjusted

Building-level flood damage for every residential building in the United States (USACE NSI 2026), from
**fluvial**, **pluvial** and **coastal** flooding, for present climate and **SSP2-4.5 / SSP5-8.5** at
**2030 / 2060**, aggregated to **5-digit ZIP codes** (2020 ZCTA). Metrics: average annual loss (AAL) and
conditional 1-in-100 / 1-in-1,000-year loss, in USD and as damage ratios. Built entirely from public data.

* **Web platform: <https://alanhuang116.github.io/flood-risk-atlas/>** (static site: MapLibre + PMTiles + ECharts,
  source in [platform/](platform/))
* Data downloads: [latest release](https://github.com/alanhuang116/flood-risk-atlas/releases/latest) (ZIP tables as
  CSV and Parquet, state and national summaries)
* Methodology: [docs/methodology.md](docs/methodology.md)
* Data dictionary: [docs/data_dictionary.csv](docs/data_dictionary.csv)

## Layout

```
config/settings.yaml        scenarios, return periods, paths, assumptions
src/floodrisk/
  exposure/                 NSI download, residential filter, ZCTA + HUC-8 assignment
  hazard/
    domain.py               HUC-8 grids, national VRTs (DEM, Atlas 14 + Atlas 2 fill, NLCD, soils)
    climate.py              CMIP6 precipitation change factors, IPCC AR6 sea-level rise
    fluvial/                NWM v3 annual maxima, LP3 flood frequency, HAND + rating curves
    pluvial/                GPU local-inertial rain-on-grid solver, curve-number losses
    coastal/                COAST-RP storm tide -> NAVD88, connected bathtub with attenuation
  vulnerability/ddf.py      USACE / Hazus depth-damage functions
  risk/                     losses, AAL integration, ZIP partial sums
scripts/                    numbered pipeline steps (see below)
platform/                   web app (index, methodology, data pages)
docs/                       methodology, data dictionary, release notes
.github/workflows/pages.yml deploys platform/ with the release data to GitHub Pages
data/                       (git-ignored) raw, interim, processed, outputs, release packages
```

## Environment

```bash
conda env create -f environment.yml          # conda-forge only (GDAL, rasterio, cupy, whitebox ...)
scripts/py.sh <script.py> [args]             # runs inside the env without activation
```
Hardware used: 32 cores, 64 GB RAM, NVIDIA RTX 4090 (CuPy for the pluvial solver).

## Pipeline

| Step | Command | What it does |
|---|---|---|
| 00 | `scripts/py.sh scripts/00_download_static.py` | census/ZCTA, WBD, NHDPlus, Atlas 14, COAST-RP, CMIP6 factors, DDFs, NLCD |
| 01 | `scripts/py.sh scripts/01_download_nsi.py --workers 4` | NSI 2026 for every county (streams the public API) |
| 02 | `scripts/py.sh scripts/02_nwm_annual_max.py` | streams NWM v3.0 retrospective (~100 GB) -> annual maxima per reach |
| 03 | `scripts/py.sh scripts/03_download_dem.py` | 3DEP 1 arc-second DEM tiles |
| 04 | `scripts/py.sh scripts/04_build_exposure.py` | residential buildings with ZCTA and HUC-8 |
| 04b | `scripts/py.sh scripts/04b_postfirm.py` | post-FIRM share of SFHA homes per block group + elevation classes (needs 12b) |
| 05 | `scripts/py.sh scripts/05_build_soils_hsg.py` | gNATSGO hydrologic soil groups (30 m) |
| 05b | `scripts/py.sh scripts/05b_build_precip_mosaic.py` | NOAA Atlas 14 + NOAA Atlas 2 gap fill (western WA/OR) |
| 06 | `scripts/py.sh scripts/06_prepare_nhdplus.py` | NHDPlus flowlines by HUC-8 |
| 06b | `scripts/py.sh scripts/06b_download_nld.py` | USACE National Levee Database: levee systems + leveed areas |
| 07 | `scripts/py.sh scripts/07_flood_frequency.py` | LP3 flood quantiles for 2.78 M reaches |
| 08 | `scripts/py.sh scripts/08_run_hazard.py --peril {coastal,fluvial,pluvial} --all` | building flood depths per HUC-8 |
| 09a | `scripts/py.sh scripts/09a_calibrate_fluvial.py --workers 8` | FEMA (CSI) calibration of every reach with FEMA data |
| 09 | `scripts/py.sh scripts/09_compute_risk.py --all --workers 8` | building losses, AAL, RP losses (defences, calibrated parameters) |
| 10 | `scripts/py.sh scripts/10_aggregate_zip.py` | ZIP product tables (`data/outputs/`) |
| 11 | `scripts/py.sh scripts/11_build_platform.py` | web data bundle (`platform/data/`) |
| 12 | `scripts/py.sh scripts/12_validate_nfip.py --counties ...` | validation against NFIP claims (ZIP level, selected counties) |
| 12a | `scripts/py.sh scripts/12a_download_nfip_national.py` | all NFIP claims by state (OpenFEMA) |
| 12b | `scripts/py.sh scripts/12b_download_nfip_policies.py` | NFIP policies in A/V zones, 2019 cross-section (OpenFEMA), for the post-FIRM elevation model |
| 12c | `scripts/py.sh scripts/12c_download_nfip_claims_postfirm.py` | SFHA claims with the post-FIRM indicator (check of the post-FIRM model) |
| 14 | `scripts/py.sh scripts/14_calibrate_nfip.py --grid` then `--fit --apply`, then `--regional --apply` | calibrate the five national parameters against NFIP claims (county level; `config/settings.yaml`), then the state calibration factors (`config/regional_factors.csv`) |
| 15 | `scripts/py.sh scripts/15_package_release.py --upload` | package the platform data and product tables, publish them as GitHub release `v<version>`, redeploy the site |

`bash scripts/run_national.sh` runs the national hazard chain (detached launch recommended; it holds a Windows
keep-awake request while running). `bash scripts/run_finalize.sh` then runs the calibration (14), the final
risk run (09 `--overwrite`), ZIP aggregation (10) and the platform bundle (11). `bash scripts/run_v11.sh`
rebuilds model v1.1 on the same hazard layers: post-FIRM inputs (12b, 04b), calibration grid v4 and fit, state
factors, then 09-11 with both defence scenarios. While `data/interim/PAUSE_RISK` exists, steps 09-11 are
skipped (`FLOODRISK_FORCE=1` overrides), so recalibration never publishes intermediate results.

## Results (v1.1)

110.7 million residential buildings in 33,110 ZIP codes. The present-climate AAL is **US$24.6 billion per year**:
fluvial 7.9, pluvial 15.6, coastal 2.0. By 2060 it rises to US$34.5 billion (+40%, SSP2-4.5) and US$36.6 billion
(+49%, SSP5-8.5) with defences at today's crest height, or US$33.4 / 35.4 billion (+36% / +44%) with defences raised
to keep their design standard (`defence_scenario`).

Version 1.1 adds post-FIRM elevation of SFHA homes (from 1.92 million NFIP policies), one calibration factor per
state (empirical Bayes over NOAA climate regions, validated on held-out loss years) and the raised-crest defence
scenario. The national calibration gives an insured-equivalent ratio of 0.89 (0.98 on held-out states) and a county
rank correlation of 0.75. With the state factors, 38 of 47 calibration states are within a factor of two of their
NFIP claims, against 20 with national parameters alone. See methodology §10. Version 1.0 outputs are archived in
`data/outputs/archive_v1.0/`.

Every step is resumable (existing outputs are skipped). Set `FLOODRISK_EXPOSURE_ROOT` to run a regional
subset (e.g. the Houston–Galveston pilot).

## Platform: preview and deployment

The site in `platform/` is fully static. Its data (`platform/data/`, written by step 11) is not in git. It is
published with each GitHub release, and the Pages workflow deploys the site together with that data.

```bash
scripts/py.sh scripts/serve_platform.py --port 8765     # local preview with HTTP Range support (PMTiles)
# open http://localhost:8765/
```

On a fresh clone, fetch the data of the latest release first:

```bash
gh release download --pattern platform-data.zip --pattern zcta.pmtiles --dir platform/data
python -m zipfile -e platform/data/platform-data.zip platform/data
```

To publish a new version:

1. Set `project.version` in `config/settings.yaml`.
2. Run steps 10–11.
3. Write release notes in `docs/releases/v<version>.md`.
4. Run `scripts/py.sh scripts/15_package_release.py --upload`.

Step 15 packages the data in `data/release/v<version>/`, publishes it as GitHub release `v<version>` and starts
`.github/workflows/pages.yml`. That workflow deploys `platform/` with the latest release's data to GitHub Pages.
It also runs on every push that changes `platform/` or the methodology.
