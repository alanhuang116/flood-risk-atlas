# US Residential Flood Risk — Climate-Adjusted · Methodology

**Product version 1.1.0** (national, NFIP-calibrated with state factors) · Exposure: USACE National Structure Inventory 2026 · Prices: USD, August-2025 price level

This document describes how the product estimates flood damage to residential buildings in the United States from **fluvial (river)**, **pluvial (rainfall)** and **coastal (storm-tide)** flooding. It covers present climate and two emission scenarios (**SSP2-4.5** and **SSP5-8.5**) at **2030** and **2060**, and reports results by **5-digit ZIP code**. It explains the hazard modelling, the damage-calculation units, how results are aggregated to ZIP codes, and the key assumptions and limitations.

---

## 1. Product summary

| Item | Specification |
|---|---|
| Perils | Fluvial (rivers with drainage area ≥ 50 km²), pluvial (rain-on-grid, including small streams), coastal (storm tide + sea-level rise); plus a combined estimate |
| Exposure unit | Individual residential structure (≈ building), NSI 2026 Base |
| Residential definition | Hazus occupancy classes RES1 (single-family), RES2 (manufactured housing), RES3A–RES3F (multi-family) |
| Spatial resolution of results | 5-digit ZIP code, represented by the 2020 Census ZIP Code Tabulation Area (ZCTA) |
| Damage metrics | Average annual loss (AAL); conditional loss for the 1-in-100 and 1-in-1,000-year event; each in USD and as a damage ratio (% of replacement value) |
| Scenarios | Present climate (labelled 2020); SSP2-4.5 and SSP5-8.5 |
| Time horizons | 2030 (climate of 2020–2039) and 2060 (climate of 2040–2079) |
| Return periods modelled | 2, 5, 10, 25, 50, 100, 250, 500, 1,000 years |
| Monetary unit | USD at the NSI price level (August 2025), full replacement cost of structure + contents |
| Computation year field | `hazard_year` (2020, 2030, 2060) on every record, plus `scenario_id`, `ssp`, `exposure_vintage`, `price_level` |
| Coverage | 48 contiguous states + DC: 111 million residential buildings in 33,000+ ZIP codes (99.5% of US residential buildings) |
| Flood defences | FEMA levee zones, USACE National Levee Database, implicit coastal defences; two scenarios for 2030/2060: fixed crest (default) and raised crest (§4.6) |
| Building elevation | Post-FIRM homes in the SFHA elevated to the base flood elevation, from 1.92 million NFIP policies (§6.3) |
| Calibration | Five national parameters and one factor per state, calibrated against 2.4 million NFIP residential claims (1980–2024); cross-validation by state and by loss year (§10.2, §10.3) |
| Present-day national AAL | US$24.6 billion per year (fluvial 7.9, pluvial 15.6, coastal 2.0); +40% (SSP2-4.5) and +49% (SSP5-8.5) by 2060 with fixed defence crests, +36% and +44% with raised crests (§10.4) |

---

## 2. Input data

| Component | Dataset | Provider | Notes |
|---|---|---|---|
| Buildings | National Structure Inventory (NSI) 2026 Base | USACE HEC | Point per structure; occupancy, foundation type and height, stories, area, depreciated and full replacement value, contents value, ground elevation (USGS 10 m), FEMA flood zone |
| ZIP geography | 2020 ZCTA polygons (TIGER/Line 2024); 2020 block → ZCTA relationship file | US Census Bureau | Every 2020 census block belongs to at most one ZCTA |
| Watersheds | Watershed Boundary Dataset, HUC-8 | USGS | Hazard processing units |
| Terrain | 3DEP 1 arc-second DEM (≈30 m), NAVD88 | USGS | 1,161 tiles for CONUS |
| River network | NHDPlus V2.1 national seamless | EPA / USGS | Flowlines with drainage area, slope, length, EROM mean annual flow |
| River flows | National Water Model v3.0 retrospective, 1979–2023, hourly | NOAA OWP | 2.78 million reaches; annual maxima extracted for water years 1980–2022 |
| Design rainfall | NOAA Atlas 14 precipitation-frequency estimates (24-hour, annual maximum series) | NOAA HDSC | All CONUS volumes; 2- to 1,000-year |
| Land cover / imperviousness | Annual NLCD 2024, Collection 1.1 | USGS / MRLC | Curve numbers, roughness, urban drainage |
| Soils | gNATSGO hydrologic soil group (dominant condition) | USDA NRCS | Curve numbers |
| Storm tide | COAST-RP (Dullaart et al., 2021) | VU Amsterdam / Deltares (GTSM) | Return levels 1–1,000 years incl. synthetic tropical cyclones (STORM) |
| Tidal datums | Station datums (MSL, NAVD88), NTDE 1983–2001 | NOAA CO-OPS | MSL → NAVD88 conversion |
| Sea-level rise | IPCC AR6 regional projections, medium confidence, total RSL incl. vertical land motion | IPCC / NASA (Garner et al., 2021) | Median, by decade |
| Extreme-precipitation change | CMIP6 multi-model ensemble, 0.25°, rx1day and rx5day climatologies and anomalies | World Bank Climate Change Knowledge Portal | Ensemble median; reference 1995–2014 |
| Damage functions | USACE default occupancy types (EGM 01-03; HEC-FIA; FEMA coastal PFRA); Hazus 4.0 coastal tables | USACE (go-consequences); FEMA (FAST) | Depth relative to first floor |
| Validation | NFIP redacted claims (v3) and policies (v3); FEMA flood zones and base flood elevations (in NSI) | FEMA / OpenFEMA | See §10 |

---

## 3. Exposure: residential building inventory

### 3.1 Source and residential identification
The NSI 2026 Base is a nationally consistent, modelled inventory that places each structure using parcel data, address geocodes and building footprints (Microsoft, FEMA USA Structures, NGA). It assigns a Hazus occupancy code to each structure. We keep structures whose damage category is residential and whose occupancy is:

* **RES1** — single-family dwellings. The NSI subtype gives stories and basement status, e.g. `RES1-2SWB` = two stories with basement.
* **RES2** — manufactured (mobile) homes.
* **RES3A–RES3F** — multi-family buildings, from duplexes (RES3A) to 50+ units (RES3F).

Hotels (RES4), dormitories (RES5) and nursing homes (RES6) are not housing and are excluded. Structures queried under two Connecticut county schemes (the 2022 planning-region change) are de-duplicated on the NSI structure identifier.

### 3.2 Attributes used
| Attribute | Use |
|---|---|
| `occtype`, `num_story`, `found_type` | Selects the depth-damage function |
| `found_ht` (ft) | First-floor height above ground; depth above first floor = flood depth − `found_ht` |
| `grnd_elv_m` (USGS 10 m, NAVD88) | Building ground elevation, used to convert water-surface elevation to depth at the building |
| `fullrep` | Full (undepreciated) replacement cost of the structure — **primary value basis** |
| `val_struct`, `val_cont` | Depreciated structure and contents value (contents = occupancy-specific ratio × structure; e.g. 0.5 for RES1) |
| `resunits` | Housing units (reported per ZIP) |
| `firmzone`, `zone_sub` | FEMA flood zone: SFHA membership (post-FIRM elevation, §6.3; NFIP calibration, §10.2), coastal damage-function selection (V zones, Coastal A), validation |
| `static_bfe` (ft), `ground_elv` (ft) | Base flood elevation where the FIRM gives one for the whole zone (coastal, lake and ponding zones); BFE above ground for post-FIRM homes (§6.3) |
| `cbfips` | Census block: ZIP assignment (§3.3) and block group for the post-FIRM share (§6.3) |

**FEMA zone codes.** Zones are matched on exact codes: A zones are A, AE, AH, AO and A1–A30; V zones are V, VE and V1–V30; the SFHA adds A99 and AR (levee zones). The NSI labels land without a flood map "AREA NOT INCLUDED" (0.78 million homes, e.g. most of Portland, Oregon), "D" or leaves the field empty; these homes are treated as unmapped. (Version 1.0 used a prefix test that counted "AREA NOT INCLUDED" as an A zone; this affected the NFIP calibration in a few counties and the FEMA-zone statistics, and is corrected in version 1.1.)

**Value basis.** The damage ratio is applied to the full replacement cost of the structure (`fullrep`) and of the contents. Contents full replacement cost is defined as `val_cont × fullrep / val_struct`, which keeps the NSI's occupancy-specific contents ratio. Depreciated values are carried in the outputs (`value_struct_depr_usd`) for users who need them. All values are in USD at the NSI price level (August 2025). No inflation adjustment is applied for 2030 or 2060.

**Static exposure.** The building stock is held at its 2026 state for every horizon, so differences between horizons reflect hazard change only. The product does not model population growth, new development, demolition or future adaptation of homes (e.g. elevating homes). Elevation that already exists because of the NFIP building rules is modelled (§6.3).

### 3.3 ZIP assignment
Each structure carries its 2020 census block (`cbfips`). The block is mapped to its ZCTA using the Census 2020 block → ZCTA relationship file, an exact, non-spatial join. The few structures in blocks without a ZCTA (water or unpopulated blocks) are assigned to the nearest ZCTA polygon; the field `zcta_method` records which method was used.

---

## 4. Hazard modelling

### 4.1 Common framework
* **Processing unit:** USGS HUC-8 watershed (≈2,100 in CONUS), on a 30 m grid in NAD83 / CONUS Albers (EPSG:5070). Grid cells are aligned nationally.
* **Hazard variable:** maximum flood depth at the building for each return period T ∈ {2, 5, 10, 25, 50, 100, 250, 500, 1,000} years, for each scenario-year.
* **Building-level depth:** hydraulics run at 30 m. Depth at a building is computed from the modelled water-surface elevation minus the building's own ground elevation from the NSI (USGS 10 m DEM). This downscaling step recovers building-scale terrain detail lost at 30 m.

### 4.2 Fluvial (river) flooding
**Rivers modelled.** NHDPlus V2.1 flowlines with total drainage area ≥ 50 km². Smaller streams are represented by the pluvial model.

**Flood frequency.**
1. Hourly NWM v3.0 retrospective discharge (forced by AORC, 1979–2023) is reduced to water-year annual maxima for all 2.78 million reaches. Complete water years 1980–2022 (43 years) are used.
2. A log-Pearson Type III distribution is fitted per reach following Bulletin 17B logic:
   * zero-flow years are handled by conditional probability;
   * low outliers are screened with the Grubbs-Beck test;
   * station skew is weighted with a regional (HUC-4 median) skew.
3. The fit gives discharge quantiles Q_T for T = 2 … 1,000 years. Estimates beyond ~100 years are extrapolations of a 43-year record (§11).

**Inundation (HAND).** For each HUC-8:
1. Rivers are burned into the DEM. Depressions are breached (WhiteboxTools complete breaching, Lindsay 2016, with pit filling), and D8 flow directions are derived.
2. Each grid cell is traced downstream to the first river cell it reaches. This gives its **height above nearest drainage**, HAND = z(cell) − z(river cell), and the river reach (NHDPlus COMID) it drains to.
3. A **synthetic rating curve** is built for each reach from its HAND catchment geometry. For stage H:

   V(H) = Σ_{HAND<H} (H − HAND)·A,  B(H) = Σ_{HAND<H} A·√(1+s²)

   Q(H) = (V/L)·R^{2/3}·S^{1/2} / n,  with R = V/B,

   where A is the cell area, s the cell slope, L the reach length, S the reach slope (NHDPlus) and n = 0.06.
4. **Channel capacity.** A 30 m DEM represents the channel only above the water surface at survey time. Narrow or engineered channels are largely invisible to it. The in-channel capacity Q_cap is therefore removed before converting discharge to stage: H_T = SRC⁻¹((Q_T − Q_cap)/k).
   * Q_cap(T_c) is the reach's own T_c-year flow. T_c rises log-linearly with the developed fraction of the reach's HAND catchment (Annual NLCD 2024 classes 22–24): T_c = T_n for natural channels (≤ 10% developed) and T_c = T_u for urban channels (≥ 60% developed; these are engineered: lined, straightened, often with flood-control works).
   * Two capacities are used. The **rating anchor** (T_n = 2 years ≈ bankfull, T_u = 10 years) fixes k against FEMA (step 5). The **effective capacity** (T_n, T_u calibrated against NFIP claims, §10.2) is the one subtracted from the flow. Where it exceeds the anchor, every flood stage is lowered smoothly while k stays fixed. The effective capacity also absorbs local flood-control works and biases of the 30 m rating curves and NWM peaks, so it is not a surveyed channel capacity. (In version 1.0 it also absorbed the elevation of post-FIRM homes, which version 1.1 models explicitly, §6.3.)
5. **Calibration to FEMA flood zones.** The conveyance multiplier k is calibrated per reach against FEMA's 1%-annual-chance Special Flood Hazard Area, using the residential buildings that drain to the reach:
   * Buildings are sorted by HAND. The 100-year stage H* is the one that maximises the Critical Success Index CSI = TP/(TP+FP+FN) between "modelled flooded" (HAND_b < H*) and "inside an A-zone".
   * k = (Q₁₀₀ − Q_cap(anchor)) / Q_SRC(H*), clipped to [0.1, 100]. Anchoring k at the physical capacity, rather than at the calibrated one, matters. If k were recomputed for the calibrated capacity, Q₁₀₀ − Q_cap would shrink to zero as T_u approaches 100 years: k collapses, the upper tail and climate response are amplified, and at T_u = 100 urban rivers would never flood.
   * Excluded from calibration: V zones, buildings flooded by the coastal model, and unmapped panels. Levee-protected X zones count as outside the SFHA.
   * Reach classes: **CSI-calibrated** (≥ 25 eligible buildings, ≥ 1 in the SFHA — a reach with one or two SFHA homes among hundreds is a low-risk reach, not an unknown one); **no-SFHA** (≥ 5 eligible buildings, none in the SFHA: the stage is set just below the lowest building); **regional fallback** (all other reaches, e.g. unmapped panels): the geometric median k of CSI-calibrated reaches in the same HUC-2 region and drainage-area class (< 150, 150–500, 500–1,500, 1,500–5,000, 5,000–15,000, 15,000–50,000, > 50,000 km²; national class median where a regional class has < 20 reaches). k rises strongly with river size (national class medians ≈ 0.1 for the smallest streams to ≈ 1.5 for large rivers), so a single watershed-wide fallback would overstate flooding along large rivers by up to an order of magnitude.
   * Q_SRC(H*) is stored per reach (step 09a).
   * The rating curve is thereby anchored to FEMA's detailed studies: with the anchor capacity the 100-year extent reproduces the SFHA. Other return periods follow the NWM frequency curve and the rating-curve shape. The calibrated effective capacity then lowers the stages wherever NFIP claims show smaller losses than the FEMA-anchored curve implies.
6. Depth at a building = max(0, H_T − HAND_b), where HAND_b = building ground elevation − elevation of its drainage river cell.
7. **Reaches without routed NWM flow.** These are lake flow paths, minor distributaries and masked reaches, about 7% of all reaches. They take the quantiles of the nearest downstream reach with flow, scaled by (drainage-area ratio)^0.7; minor distributaries take 50%.

**Climate adjustment.** Q_T is scaled by the ensemble-median relative change in mean annual-maximum 5-day precipitation (rx5day, CMIP6) at the building's location. This assumes flood peaks scale proportionally with multi-day precipitation extremes (elasticity 1).

**Levees.** River (and coastal) defences are applied in the risk step (§4.6).

### 4.3 Pluvial (rainfall) flooding
**Rainfall.** 24-hour design depths from NOAA Atlas 14 (annual maximum series; the partial-duration series converted where AMS is not published), distributed in time with the NRCS Type II hyetograph. Atlas 14 does not cover western Washington and Oregon (to be replaced by NOAA Atlas 15); there the current NWS standard, **NOAA Atlas 2** (digitised 2- and 100-year 24-hour grids), is used: 5–50-year depths from the Atlas 2 interpolation equations, partial-duration values converted to annual series (× 0.88, 0.96, 0.99 for 2, 5, 10 years), and 200–1,000-year depths as the 100-year depth times the median Atlas 14 ratio of the neighbouring Interior-Northwest and California volumes (1.10, 1.24, 1.35). This fills 144 watersheds holding 4.2 million homes. Eleven rainfall runs per watershed: T = 2, 5, 10, 25, 50, 100, 200, 500, 1,000 years, plus 1.15× and 1.30× the 1,000-year depth to cover climate-intensified extremes without extrapolation.

**Losses.**
* Infiltration and interception: SCS curve number evaluated cell-by-cell on cumulative rainfall. Curve numbers come from TR-55, by Annual NLCD 2024 land cover × gNATSGO hydrologic soil group. Dual groups take the drained class, except in wetlands; missing soil data defaults to C (D in dense urban land).
* **Urban drainage:** in developed cells (NLCD 22–24) the storm-sewer network is assumed to remove the runoff of the 5-year, 24-hour storm at every time step. Sewers therefore cope with storms up to their design standard, and only the excess floods.
* **Effective drainage standard.** The 5-year standard is built into the GPU runs. The product uses a calibrated effective standard T_d (§10.2), which also absorbs storage and conveyance that a 30–90 m grid cannot resolve (curbs, roadside ditches, detention ponds). For T_d > 5 the building's depth–rainfall curve is evaluated at P − (P_Td − P_5), with P_T the building's Atlas design depths; depths fall to zero at zero effective rainfall.

**Hydraulics.** A GPU solver of the 2D local-inertial shallow-water equations (Bates et al., 2010; de Almeida et al., 2012) with:
* semi-implicit Manning friction (n from land cover; Kalyanapu et al., 2009);
* Froude-limited fluxes and an adaptive CFL time step;
* free-outfall boundaries.

NHDPlus flowlines are lowered by 2 m in the DEM so that water passes road embankments through culverts and bridges. Open water (NLCD class 11: sea, bays, lakes, wide rivers) is treated as an absorbing sink, so runoff reaching it leaves the model. This prevents rain from accumulating artificially on enclosed water bodies, and flooding from large rivers is handled by the fluvial model. Each run simulates the 24-hour storm plus 6 hours of drainage and records the maximum depth per cell. The solver conserves mass to 99.85% in closed-basin tests.

**Depth at buildings.** Cells with maximum depth < 0.10 m are treated as dry (sheet flow). Otherwise depth_b = max(0, z_cell + h_max − z_building).

**Climate adjustment.** The design rainfall at the building is scaled by the ensemble-median relative change in mean annual-maximum 1-day precipitation (rx1day, CMIP6). The depth for the scaled rainfall is interpolated linearly between the eleven runs. The 250-year rainfall is interpolated in log(T) between the 200- and 500-year Atlas 14 depths. The 0.10 m wet/dry threshold is re-applied after interpolation.

**Resolution.** Watersheds with ≥ 60 residential buildings per km² or ≥ 150,000 buildings run at 30 m: 212 HUC-8s holding 58% of US homes, including all large metropolitan areas. Other watersheds run at 60 m (≥ 4 buildings per km²) or 90 m. Building depths always use the building's own 10 m ground elevation.

### 4.4 Coastal flooding
**Extreme water levels.** Storm-tide return levels (1–1,000 years) come from COAST-RP. It combines the Global Tide and Surge Model (GTSM) driven by 10,000 years of synthetic tropical cyclones (STORM) and by ERA5 extratropical storms (1979–2017). Levels are converted from mean sea level to NAVD88 using the MSL–NAVD88 offset at the nearest NOAA CO-OPS station with both datums (within 150 km).

**Sea-level rise.** The IPCC AR6 median total relative sea-level change (including vertical land motion) at the nearest AR6 location is added for 2030 or 2060. The present-climate scenario uses AR6 2020.

**Inundation.** A hydraulically connected "bathtub" with inland attenuation:

WL(cell) = storm tide_T + SLR − α·d(cell),  α = 0.25 m/km,

where d is the distance to the sea. A cell floods if its elevation is below WL and it is 8-connected to sea seeds. Sea seeds are NLCD open-water or no-data cells at ≤ 1 m elevation forming water bodies that reach the domain edge or exceed 20 km². Depth at a building = WL − building ground elevation.

**Wave effects.** Buildings in FEMA V zones use coastal V-zone (high-wave) damage functions. Buildings in Coastal A zones use moderate-wave functions. Other coastal flooding uses salt-water still-water functions.

### 4.5 Combined hazard
For every building, scenario and return period, the combined depth is the **maximum** of the fluvial, pluvial and coastal depths. Loss is computed with the damage function of the peril giving that maximum. This treats co-occurring perils as fully dependent within a return period, so the combined AAL is typically lower than the sum of the single-peril AALs (§11).

### 4.6 Flood defences (levees, dikes, pumped areas)
Fluvial and coastal depths are reduced behind flood defences in the risk step; pluvial flooding is not (interior drainage). Each building receives a defence standard T_p (years), the highest of:
* **FEMA:** FIRM zone X "area with reduced flood risk due to levee" (accredited levee) or A99 (federal flood-protection system) → 100 years.
* **USACE National Levee Database** (public NLD web API, 6,061 levee systems, 6,040 leveed-area polygons): the system's overtopping annual exceedance probability where reported (38% of leveed areas, T_p = 1/AEP); otherwise 100 years for FEMA-accredited or provisionally accredited systems and 25 years for other systems.
* **Implicit coastal defences:** a home that today's 2-year storm tide would flood ≥ 0.5 m deep (bathtub model) cannot exist without dikes, sea walls or pumps; it receives T_p = 100 years for coastal flooding. This safety net covers defended areas missing from both sources.

The defence is modelled as a crest: the building stays dry while the depth does not exceed the crest depth, and floods as if undefended beyond that (overtopping / breach). Two defence scenarios are delivered for 2030 and 2060 (field `defence_scenario`):

* **Fixed crest** (`fixed-crest`, default): the crest stays at the present-day depth of the T_p event. Sea-level rise and heavier rainfall erode the protection level — a 100-year levee may protect only against the 20-year flood by 2060. This is the no-adaptation case.
* **Raised crest** (`raised-crest`): the crest is raised with the hazard so that every defence keeps its design standard T_p in every scenario (the crest is the scenario's own T_p-event depth). Defended homes still flood in events beyond the standard, and those events are deeper in a warmer climate, so their losses still grow, but much less. Costs and feasibility of raising defences are not modelled.

Present-climate results are identical in both. Undefended homes and pluvial flooding are not affected.

---

## 5. Climate scenarios and time horizons

| Scenario ID | SSP | hazard_year | Climate window | Sea level |
|---|---|---|---|---|
| `baseline` | present | 2020 | Observational record (NWM 1979–2022; Atlas 14) | AR6 2020 |
| `ssp245_2030` | SSP2-4.5 | 2030 | 2020–2039 | AR6 2030 |
| `ssp245_2060` | SSP2-4.5 | 2060 | mean of 2040–2059 and 2060–2079 | AR6 2060 |
| `ssp585_2030` | SSP5-8.5 | 2030 | 2020–2039 | AR6 2030 |
| `ssp585_2060` | SSP5-8.5 | 2060 | mean of 2040–2059 and 2060–2079 | AR6 2060 |

Precipitation change factors are relative to the CMIP6 reference period 1995–2014. AR6 sea-level values are relative to 1995–2014. The baseline hazard is treated as representative of present-day (2020) climate without further adjustment. `hazard_year` on every output record identifies the year for which damage was computed.

---

## 6. Damage calculation

### 6.1 Depth–damage functions
For each building the depth above the first finished floor is

d_ff (ft) = flood depth at building (ft) − first-floor height `found_ht` (ft),

and the structure and contents damage ratios are read from the depth–damage function for the building's occupancy type, as summarised below.

**Riverine and pluvial (fresh water)**
* RES1: EGM 01-03 by stories and basement. Pier and pile foundations use pier-specific curves where available.
* RES2, RES3: HEC-FIA (Galveston) curves.

**Coastal**

| Zone | RES1 | RES2, RES3 |
|---|---|---|
| Still water | FEMA coastal PFRA "depth + salinity" | Riverine curve (no coastal variant) |
| Coastal A (moderate wave) | FEMA coastal PFRA | Hazus 4.0 coastal A curves (BCAR 2011 for ≤2 stories; USACE Chicago for taller buildings) |
| V zone (high wave) | FEMA coastal PFRA | Hazus 4.0 coastal V curves (same sources) |

The central value of each curve's uncertainty distribution is used. Loss is zero when the flood depth at the building is zero.

**Water below the first floor.** The curves assign damage below the first floor (negative d_ff: basements, crawlspaces; up to 15–25% of value for basement homes). That damage is only counted once the ground-level depth reaches a threshold h_b, because shallow water around a building — and the depth of a 30–90 m model cell — does not reach basement openings or crawlspace vents. h_b is calibrated against NFIP claims (§10.2). Above the first floor the curves apply unchanged.

### 6.2 Loss and units
Loss_b = DR_struct(d_ff) × V_struct + DR_cont(d_ff) × V_cont

* V_struct = full replacement cost of the structure (`fullrep`).
* V_cont = full replacement cost of contents.
* Units: USD, NSI price level (August 2025).
* **Damage ratio** = loss / (V_struct + V_cont).

### 6.3 Post-FIRM elevation (NFIP building rules)
Since a community joined the NFIP's regular programme, new homes in the SFHA (and substantially improved ones) must have their **lowest floor — basement included — at or above the base flood elevation** (BFE; 44 CFR 60.3). In V zones the lowest horizontal structural member must be above the BFE, on an open pile or column foundation. Homes built after the community's first flood map are called *post-FIRM*. The NSI does not reflect this: its first-floor heights are national defaults by foundation type and are the same inside and outside the SFHA (slab 0.7 ft, crawlspace and basement 2 ft). The model therefore treats post-FIRM elevation explicitly.

**Data.** OpenFEMA NFIP policies (v3): all residential policy terms in A and V zones that took effect in 2019, the last full year before Risk Rating 2.0, while post-FIRM SFHA policies were still rated from elevation certificates (1.92 million policies: 1.87 million in A zones and 48,724 in V zones policies; script 12b). Each policy carries its census block group, rated zone, post-FIRM indicator and the difference between its lowest floor and the BFE.

**Share of post-FIRM homes, p_post.** For each census block group, p_post is the share of post-FIRM policies. Sparse block groups are pooled with their county, counties with their state and states with the nation, each with 10 pseudo-policies of the next level: p = (k + 10·p_parent) / (n + 10). Every SFHA home (A, AE, AH, AO, V, VE; not A99, AR or unmapped land) receives the p_post of its block group. Nationally 53% of the insured SFHA homes are post-FIRM. 66% of the 91,896 block groups with SFHA homes have at least one policy, and the mean p_post over the 4.12 million SFHA homes is 0.48. 86% of post-FIRM A-zone policies report no basement or below-floor enclosure (pre-FIRM: 65%).

**Elevation of post-FIRM homes.** The difference e = lowest floor − BFE of post-FIRM policies is summarised by three equally likely classes centred at its 1/6, 1/2 and 5/6 quantiles:

| Zone | Policies with e | Classes e (ft) | Mean | Below today's BFE |
|---|---|---|---|---|
| A zones | 847,730 | 0, 1, 4 | 1.8 | 11.6% |
| V zones | 26,387 | −4, 3, 6 | 1.8 | 23.8% |

Homes below today's BFE are kept: many were built to an earlier map whose BFE was later raised, or did not comply.

**First-floor height.** A post-FIRM home has its first floor at h_k = max(found_ht, d_BFE + e_k) above ground for class k, where d_BFE is the BFE above the building's ground:
* d_BFE = static BFE − ground elevation (NSI) where the FIRM gives a static BFE for the zone (coastal, lake and ponding zones; 45% of SFHA homes);
* otherwise the model's own 100-year depth at the building at fixed reference settings: fluvial with the FEMA-anchored rating curves (§4.2), pluvial at the 5-year drain standard of the GPU runs, and coastal storm tide, all undefended. The reference does not depend on the NFIP-calibrated parameters, so the BFE does not move during calibration.

Post-FIRM homes have no basement (RES1 `…WB` curves become `…NB`), and post-FIRM V-zone homes use pier/pile curves.

**Loss.** For an SFHA home the loss at every depth is the expectation over both states: L = (1 − p_post) · L(NSI first floor) + p_post · ⅓ Σ_k L(h_k). AAL and return-period losses are linear in L, so every metric is the expected value. The product reports the expected number of post-FIRM homes per ZIP (`n_bldg_postfirm`).

**What is not covered.** Pre-FIRM homes that were later elevated (mitigation, substantial improvement), freeboard rules outside the SFHA, and elevation that local codes require in zone X are not modelled. The policy sample represents insured SFHA homes; mortgaged and newer homes are likely over-represented.

---

## 7. Risk metrics

**Return-period (conditional) loss.** L_T is the building loss when the T-year flood occurs at that building, reported for T = 100 and 1,000 years. Loss-exceedance curves at all nine return periods are available per ZIP in the platform.

**Average annual loss.** AAL is the expected loss per year — the integral of loss over annual exceedance probability p = 1/T:

AAL = ∫₀¹ L(p) dp ≈ Σ_k ½ (L_{T_k} + L_{T_{k+1}})(1/T_k − 1/T_{k+1}) + L_{1000}·(1/1000)

* No damage is assumed for events more frequent than the 2-year flood.
* The loss beyond the 1,000-year event is held constant at L_1000.
* **Behind flood defences** the loss curve has a jump where the defence fails (§4.6). The failure probability p_f is found where the undefended depth curve crosses the crest depth (linear in p between return periods), and the AAL is integrated exactly with nodes on both sides of p_f: L(p) = 0 for p > p_f, undefended loss for p ≤ p_f. A small rise of the hazard therefore lowers the protection level smoothly instead of switching whole return-period bands on or off.

AAL is computed per building, per peril and combined, for every scenario-year.

---

## 8. ZIP-code aggregation

1. **Geography.** USPS ZIP codes are delivery routes, not areas. The product uses the Census **2020 ZCTA**, the standard areal approximation of residential 5-digit ZIP codes. ZIP codes used only for PO boxes or single organisations have no ZCTA and contain no residential buildings.
2. **Assignment.** Each building is assigned to exactly one ZCTA (§3.3), so the national total equals the sum over ZIP codes.
3. **Sums.** For each ZIP × defence scenario × scenario-year × peril:
   * AAL_ZIP = Σ_b AAL_b
   * L_T,ZIP = Σ_b L_T,b
   * value = Σ_b (V_struct + V_cont)
   * plus counts of buildings, housing units and buildings with depth > 0 in the 100-year event.
4. **Ratios.** Value-weighted: AAL ratio = AAL_ZIP / value_ZIP; L_T ratio = L_T,ZIP / value_ZIP.
5. **Interpretation of ZIP return-period losses.** L_T,ZIP is the sum of local T-year losses, which assumes every building experiences its own T-year flood at the same time. It is a conditional, spatially coincident measure for comparing ZIP codes, **not** the loss of a single T-year event across the ZIP. A true event loss would be equal or lower.

---

## 9. Output data dictionary (long format, one row per ZIP × defence scenario × scenario-year × peril)

| Field | Description | Unit |
|---|---|---|
| `zip5` | 5-digit ZIP code (2020 ZCTA) | — |
| `defence_scenario` | fixed-crest (default) or raised-crest (§4.6); present-climate rows are identical in both | — |
| `scenario_id` | baseline, ssp245_2030, ssp245_2060, ssp585_2030, ssp585_2060 | — |
| `ssp` | present, ssp245, ssp585 | — |
| `hazard_year` | Year for which damage is computed (2020, 2030, 2060) | year |
| `peril` | fluvial, pluvial, coastal, combined | — |
| `state_fips` | State of the majority of the ZIP's buildings | — |
| `n_res_buildings` | Residential structures (RES1–RES3) | count |
| `n_housing_units` | Housing units in those structures | count |
| `value_struct_usd`, `value_cont_usd`, `value_total_usd` | Full replacement cost of structures, contents, total | USD (Aug-2025) |
| `value_struct_depr_usd` | Depreciated structure value | USD (Aug-2025) |
| `aal_usd` | Average annual loss | USD/year |
| `aal_ratio` | AAL / total value | fraction per year |
| `l100_usd`, `l100_ratio` | Conditional 1-in-100-year loss and ratio | USD, fraction |
| `l1000_usd`, `l1000_ratio` | Conditional 1-in-1,000-year loss and ratio | USD, fraction |
| `n_bldg_flooded_100yr`, `pct_bldg_flooded_100yr` | Buildings with flood depth > 0 at 1-in-100 | count, fraction |
| `n_bldg_defended` | Buildings behind a flood defence for river or coastal flooding (§4.6) | count |
| `n_bldg_postfirm` | Expected number of post-FIRM, elevated SFHA buildings (§6.3) | count |
| `exposure_vintage`, `price_level`, `model_version` | Provenance | — |

---

## 10. Validation and calibration

The model is checked and calibrated against data it was not built from:

1. **NFIP claims (OpenFEMA, v3).** About 2.4 million residential claims from 1980–2024 are used: by ZIP code in the development pilot (§10.1), by county for the national calibration (§10.2) and by state and loss year for the state factors (§10.3). The claims record river overflow, rainfall accumulation and tidal flooding separately. Insurance penetration is accounted for by flood zone.
2. **FEMA flood zones (in NSI).** The share of homes with a 100-year depth > 0 is compared inside and outside the Special Flood Hazard Area (§10.4).
3. **FEMA base flood elevations.** The modelled 100-year coastal water levels are compared with the static BFEs of coastal V and A zones, a sanity check on storm tide and attenuation (§10.4).
4. **National totals and climate change.** These are compared with published estimates (§10.4).
5. **Post-FIRM claims.** The post-FIRM share of SFHA claims is compared with the model's (§10.2).

### 10.1 Development pilot — Houston–Galveston (8 counties, 2.13 million residential buildings, 240 ZIP codes)

The pilot was run during development, before flood defences, the below-first-floor threshold and the national calibration (§10.2) were added. Its numbers describe that development model. The v1.1 results for the same ZIP codes follow the table.

| Quantity | Value |
|---|---|
| Modelled AAL, combined, present climate | US$3.72 billion per year (fluvial 2.52, pluvial 1.29, coastal 0.14) |
| NFIP residential claims, building + contents damage 1980–2024, CPI-adjusted to 2025 | US$18.8 billion total (Harvey 9.5, Ike 2.7, Allison 1.8); US$0.42 billion per year |
| Claims ÷ current county NFIP penetration (10–42%) | ≈ US$2.3 billion per year. Using historical penetration (≈11% in Harris County before 2017) gives ≈ US$3.5 billion per year |
| Spearman rank correlation, ZIP-level AAL vs penetration-adjusted claims | 0.62 |
| Coastal 100-year flooded share by FEMA zone | V zones 95%, A zones 30%, X zones 2% |
| Fluvial 100-year flooded share | SFHA 60%, non-SFHA 7% (after FEMA calibration) |
| Climate change, combined AAL | +9–10% (2030), +18% (SSP2-4.5, 2060), +24% (SSP5-8.5, 2060); coastal +94–106% by 2060 |

During development the fluvial component was overestimated by a factor of about 2.5. It was corrected by (i) calibrating rating curves to FEMA flood zones, (ii) adding the missing in-channel capacity and (iii) re-applying the pluvial wet/dry threshold after interpolation. The modelled total then lay within the range obtained by dividing claims by penetration. The peril split is uncertain because NFIP cause-of-loss codes do not separate river and rainfall flooding reliably.

Dividing claims by an average penetration rate overstates the uninsured loss, because insured homes are the riskier ones. The national calibration therefore compares insured-equivalent losses instead (§10.2).

**v1.1, same 240 ZIP codes.**
* **AAL.** The combined AAL is US$1.30 billion per year: fluvial 0.77, pluvial 0.50, coastal 0.12 (v1.0: 0.64). The increase comes from the higher national vulnerability factor and the Texas state factor (§10.2, §10.3).
* **Claims.** With national parameters only, the insured-equivalent model loss is 0.42 times the eight counties' claims of US$0.42 billion per year. With the Texas factor of 1.62 it is 0.69 (US$0.29 billion per year). Hurricane Harvey accounts for 51% of those claims; without 2017 the ratio would be about 1.4.
* **Ranking.** The ZIP-level rank correlation with penetration-adjusted claims is 0.63 (unchanged; development model 0.62).
* **Climate change.** With fixed defence crests, the combined AAL rises by 15% in 2030, 26% under SSP2-4.5 in 2060 and 33% under SSP5-8.5 in 2060.

### 10.2 National calibration against NFIP claims

**Why calibrate.** Before calibration, the national model's insured-equivalent losses were 2.6 times NFIP claims on the fully computed counties. Its national total, US$93 billion per year, was about three times published estimates. Part of the excess came from missing flood defences, from shallow water below the first floor and from the elevation of post-FIRM homes. These are now modelled explicitly (§4.6, §6.1, §6.3). The remaining excess is removed by calibrating five national parameters; state factors follow in §10.3.

**Data.**
* **Claims.** NFIP redacted claims (OpenFEMA, v3), downloaded for every state: 2.36 million residential claims with year of loss 1980–2024. The building and contents damage assessed by the adjuster (not the amount paid) is CPI-adjusted to 2025 dollars and divided by 45 years. Nationally this is US$136 billion, or US$3.0 billion per year.
* **Penetration.** OpenFEMA NFIP residential penetration rates by county, separately inside and outside the SFHA (policies in force ÷ residential structures).
* **Model.** Baseline AAL per county, split into homes inside and outside the SFHA (exact zone codes, §3.2), for every parameter combination. It is computed with the full risk engine, including flood defences, post-FIRM elevation and the exact AAL behind defences (step 14, `--grid`).

**Target: insured-equivalent losses.** Insured homes are riskier than average: purchase is mandatory in the SFHA, and buyers outside it select themselves. Dividing claims by a county-average penetration rate would therefore overstate the uninsured loss. Instead, the model is weighted by zone-specific penetration:

model_ins = AAL(SFHA homes) × penetration_SFHA + AAL(other homes) × penetration_non-SFHA,

and compared with the county's annual claims.

**Counties.** 2,708 counties in 46 states and DC, each with ≥ 1,000 residential buildings and ≥ 99% of them computed. They carry US$2.17 billion per year of claims (72% of the national total). Two states are excluded:
* **Louisiana** (27% of national claims). Its claims are dominated by the 2005 failures of the New Orleans defences, before today's Hurricane and Storm Damage Risk Reduction System was built (reported separately below).
* **Connecticut.** Its claims are coded to the legacy counties, but the exposure uses the 2022 planning regions.

**Parameters.** The nine (T_n, T_u) pairs are (2, 10), (2, 25), (2, 50), (2, 100), (5, 25), (5, 50), (5, 100), (10, 50) and (10, 100) years. With the values of T_d and h_b this gives 72 combinations, and each has its own optimal λ.

| Parameter | Meaning | Values tested | Selected |
|---|---|---|---|
| T_n | effective channel capacity, natural channels (§4.2) | 2, 5, 10 years | **10 years** |
| T_u | effective channel capacity, urban channels (§4.2) | 10, 25, 50, 100 years | **100 years** |
| T_d | effective storm-drain standard (§4.3) | 5, 10, 25, 50 years | **25 years** |
| h_b | ground depth before below-first-floor damage counts (§6.1) | 0, 0.3 m | **0 m** |
| λ | vulnerability factor multiplying all depth–damage ratios | closed-form optimum | **0.92** (v1.0: 0.62) |

**Objective.** With r = Σ model_ins / Σ claims over a set of counties:

J = (ln r_all)² + ⅕ Σ_q (ln r_q)² + (0.5/3) Σ_c (ln r_c)²

* **r_q** are the ratios in quintiles of the county SFHA share. The strata are defined by the share of homes in the SFHA, not by the claims. Stratifying by realised claims would build regression to the mean into the objective: counties that happened to have a large flood would always look under-predicted.
* **r_c** compare each peril with its NFIP cause of loss: fluvial with river and alluvial-fan overflow, pluvial with accumulation of rainfall, and coastal with tidal overflow. These terms carry half weight, because cause codes are only indicative.
* **λ** multiplies every ratio, so its optimum is closed-form: ln λ* = −Σ w ln r / Σ w.

**Cross-validation.** The states, sorted by FIPS code, are split alternately into two halves of 24 and 23. λ is fitted on one half and J evaluated on the other, in both directions. The selected combination has the lowest mean validation loss. Its final λ is fitted on all calibration states.

**Result.**

| Statistic | Value |
|---|---|
| Cross-validation loss of the selected set (runners-up: h_b = 0.3 m 0.253; T_u = 50 years 0.265) | 0.223 |
| Insured-equivalent ÷ claims, all calibration counties (λ = 1 → λ = 0.92) | 0.98 → **0.89** |
| Same for the validation states, with λ from the training states | 0.98 |
| By SFHA-share quintile, lowest → highest | 0.86 · 1.65 · 1.53 · 0.91 · 0.78 |
| By cause of loss: river · rainfall · tidal | 1.42 · 0.83 · 0.98 |
| County rank correlation (Spearman), model vs claims | 0.75 (validation states 0.77) |
| Modelled AAL, all homes in the calibration counties | US$26.0 billion per year (insured-equivalent US$1.94 billion vs claims US$2.17 billion) |

**What post-FIRM elevation changed.**
* **At fixed parameters** it lowers the AAL of SFHA homes by about 38% and the national AAL by 10%: coastal −34%, fluvial −13%, pluvial −5%.
* **Calibration level.** Insured homes are concentrated in the SFHA. The insured-equivalent loss at λ = 1 therefore falls from 1.54 to 0.98 times the claims, and λ rises from 0.62 to 0.92.
* **Homes outside the SFHA.** v1.0 had scaled these down together with the overstated SFHA losses. They now rise with λ, which is why the national total is higher than in v1.0 (§10.4).

**Channel capacity: still at the limit of the grid.** The table shows the best result for each capacity pair.

| (T_n, T_u) | Best T_d | λ | CV loss | River-claims ratio |
|---|---|---|---|---|
| (2, 10) — physical rating anchor | 10 | 0.41 | 0.570 | 2.42 |
| (2, 100) | 10 | 0.51 | 0.416 | 1.88 |
| (5, 50) | 25 | 0.75 | 0.318 | 1.89 |
| (5, 100) | 25 | 0.80 | 0.276 | 1.72 |
| (10, 50) | 25 | 0.86 | 0.265 | 1.63 |
| **(10, 100)** | **25** | **0.92** | **0.223** | **1.42** |

Version 1.0 expected that modelling post-FIRM elevation would let the urban capacity fall back towards its physical value. It does not.
* **Cross-validation.** The loss still falls steadily towards the corner of the grid. That corner was set in advance as the plausible limit and was not extended.
* **Physical anchor.** With FEMA-anchored rating curves and the physical capacity, insured-equivalent river losses would still be 2.4 times river claims.
* **Interpretation.** The claims show few losses below the 10-year flow of natural rivers and the 100-year flow of urban channels for reasons other than post-FIRM elevation. The likely ones are local flood-control works (reservoirs, channel improvements, detention basins) and biases of NWM peak flows and of the 30 m rating curves. The effective capacity is a calibrated value, not a channel survey.
* **Consequence.** As in v1.0, the 100-year river flood leaves SFHA homes along fully urban channels dry. 14.6% of A-zone homes are wet from river flooding at 1-in-100 (§10.4).

**Balance across strata.** The national fit is less balanced than v1.0's: its cross-validation loss is 0.223, against 0.158.
* With post-FIRM homes elevated, one national level under-predicts the counties with the largest SFHA shares (0.78) and over-predicts those with low-to-middle shares (1.65 and 1.53).
* v1.0's better balance came with a lower λ, which also under-scaled homes outside the SFHA.
* Applying the state factors (§10.3) narrows the range of stratum ratios from 0.78–1.65 to 0.82–1.36.

**Check of the post-FIRM model against claims.** NFIP claims record whether the damaged building is post-FIRM.
* **Observed.** In A zones, post-FIRM homes produced 28% of the residential claim dollars of 1980–2024 (24% without Louisiana) and 28–32% of those since 2000. They hold 53% of today's policies. Per insured home, their losses are about a third of those of pre-FIRM homes.
* **Model.** The test sample is the 40 HUC-8s with the most SFHA homes (1.8 million homes). There, post-FIRM homes carry 23% of the insured-equivalent A-zone loss, against 27–31% of the claims in the same counties. Per home, their AAL is 23% of that of pre-FIRM homes.
* **Reading.** The direction and size of the effect agree, but the model's post-FIRM homes are about a quarter safer than observed. The gap is largest where storm surges far exceeded the BFE:
  * Sandy in Ocean County, NJ (post-FIRM share: model 11%, claims 34%) and Nassau County, NY (1% vs 12%);
  * Katrina in Harrison County, MS (18% vs 62%).

  In Harris, Galveston and Lee counties and in Jefferson Parish the shares agree within a few points.

**Vulnerability factor.** With post-FIRM elevation explicit, λ = 0.92 scales the depth–damage functions only slightly. The low v1.0 value (0.62) partly stood in for the missing elevation of insured SFHA homes.

**Robustness.**
* **High-penetration subset.** Using only the 320 counties with ≥ 2% residential NFIP penetration, where claims are most representative, selects (T_n, T_u, T_d, h_b) = (10, 100, 5, 0) with λ = 0.79. The capacities agree with the full fit. The drainage standard is not robustly identified, because these counties are dominated by a few coastal catastrophes (cross-validation loss 0.44).
* **Near-optimal sets.** Across the six best combinations, the modelled AAL of all homes in the calibration counties ranges from US$23.3 to 29.9 billion per year. The river-claims ratio ranges from 1.11 to 1.72.

**By state, national parameters only** (insured-equivalent model loss, US$ million per year):

| State | Counties | Claims | Model | Ratio |
|---|---|---|---|---|
| Texas | 202 | 543 | 302 | 0.56 |
| Florida | 67 | 521 | 522 | 1.00 |
| New Jersey | 21 | 185 | 125 | 0.67 |
| New York | 62 | 171 | 87 | 0.51 |
| Mississippi | 79 | 149 | 22 | 0.15 |
| North Carolina | 100 | 148 | 77 | 0.52 |
| South Carolina | 46 | 55 | 93 | 1.70 |
| Alabama | 63 | 39 | 19 | 0.49 |
| Pennsylvania | 67 | 38 | 85 | 2.27 |
| Virginia | 129 | 36 | 32 | 0.90 |
| California | 56 | 33 | 164 | 4.92 |
| Missouri | 102 | 27 | 14 | 0.51 |

20 of the 47 jurisdictions are within a factor of two of their claims, and together they hold 83% of the calibration claims.
* **Under-predicted states.** The model is low where one extreme event dominates 45 years of claims: Hurricane Harvey is 48% of Texas claims (2017), Katrina 70% of Mississippi's (2005) and Sandy 71% of New York's (2012).
* **Over-predicted states.** The model is high in California (4.9), Georgia (3.5), Pennsylvania (2.3) and the Appalachian states of Kentucky, West Virginia and Tennessee (2.1–2.6).
  * The 45-year record holds few large floods in these states.
  * Most of their modelled loss is pluvial, where the rain-on-grid model is least reliable. Southern California's engineered flood-control system probably exceeds the national effective drainage standard.
  * Narrow Appalachian valleys, which carry small-stream flooding in the pluvial model, are coarsely resolved at 60 m. That grid covers 89% of West Virginia's homes and about 60% of those in Kentucky and Tennessee.

A 45-year claims record is a single sample of a heavy-tailed process. State ratios between 0.5 and 2 are therefore not by themselves evidence of bias. Ratios outside that range indicate regional biases that national parameters cannot remove; they are the subject of the state factors (§10.3).

**Louisiana (excluded from the fit).**
* **Comparison.** With national parameters, the modelled insured-equivalent loss is US$304 million per year, against claims of US$814 million per year (ratio 0.37). With Louisiana's state factor (its region's effect, 1.23) the ratio is 0.46. The modelled AAL of all Louisiana homes is US$1.34 billion per year.
* **Why the gap is expected.** 70% of Louisiana's 1980–2024 claims are from 2005, when the New Orleans levees and floodwalls failed below their design level. The model represents today's rebuilt 100-year system and does not model breaches below the design level (§11).

### 10.3 State calibration factors

**Why.** National parameters cannot remove regional biases. With them alone, 20 of the 47 calibration states are within a factor of two of their claims (§10.2). The model is high where pluvial losses dominate, such as California and the Appalachian states, and low where single catastrophes dominate the claims record, such as Texas, New York and Mississippi. Version 1.1 adds one calibration factor per state, applied on top of the national calibration.

**Method.**
* **Raw signal.** For state s, δ_s = ln(claims_s / model_ins,s) with the nationally calibrated model (λ = 0.92).
* **Sampling noise.** A 45-year claims average is a noisy estimate of a heavy-tailed process. Its variance σ_s² is estimated by resampling the 45 loss years with replacement (4,000 bootstrap samples). A record dominated by one catastrophe, such as Harvey, Katrina or Sandy, therefore gets a large σ_s; a long record of many floods gets a small one.
* **Hierarchy.** States are grouped into NOAA's nine climate regions (Karl & Koss 1984). The model is δ_s = a_region + b_s + noise, with a ~ N(0, τ_R²), b ~ N(0, τ_S²) and noise ~ N(0, σ_s²). The hyper-parameters τ_R and τ_S are estimated by maximum marginal likelihood. The factor is the posterior mean, c_s = exp(E[a + b | δ]): a state with a noisy record is shrunk towards its region and the region towards the national model.
* **National level kept.** The factors are rescaled by a common κ so that the model total of the calibration states does not change. Shrinkage is asymmetric: under-predicted states are mostly single-catastrophe records (strong shrinkage), while over-predicted states are often records without a rare event (weak shrinkage). Without κ the factors would pull the national level down. The national level stays with the national calibration (§10.2); the factors move losses between states.
* **Peril groups.** One factor per state for all perils was compared with separate inland and coastal factors (coastal = tidal claims). One factor validated better (below) and is used.
* **Bounds and gaps.** Factors are bounded to 0.25–4. Louisiana and Connecticut, which have no calibration counties, receive their region's effect.

**Validation: split years.** The factors are estimated on odd loss years and scored on even years, and vice versa. The score is the error in ln(claims / model) of every state's held-out half:

| Method | RMSE of log ratio | Model-weighted RMSE | Median absolute log ratio |
|---|---|---|---|
| National parameters only | 0.92 | 0.90 | 0.77 |
| Unshrunk state ratios (inland and coastal) | 0.89 | 0.99 | 0.58 |
| **Shrunk, one factor per state (used)** | **0.76** | **0.82** | **0.53** |
| Shrunk, separate inland and coastal factors | 0.79 | 0.87 | 0.54 |

Unshrunk state ratios do worse than the national model on the model-weighted score: they chase single events. Shrunk factors cut the error by 18% (unweighted) and 9% (model-weighted), so the factors capture persistent regional differences rather than single events. One factor per state validated better than separate inland and coastal factors.

**Result.** The estimated spread of true state deviations is τ_S = 0.65 within regions and τ_R = 0.38 between regions (natural-log scale), and the rescaling factor is κ = 1.06. Only Oregon reaches a bound (0.25).

| State | Region | Claims (US$ M/yr) | Model, national only | Ratio | Factor | Ratio with factor |
|---|---|---|---|---|---|---|
| Texas | South | 543 | 302 | 0.56 | 1.62 | 0.90 |
| Florida | Southeast | 521 | 522 | 1.00 | 1.04 | 1.04 |
| New Jersey | Northeast | 185 | 125 | 0.67 | 1.09 | 0.73 |
| New York | Northeast | 171 | 87 | 0.51 | 1.17 | 0.60 |
| Mississippi | South | 149 | 22 | 0.15 | 2.39 | 0.36 |
| North Carolina | Southeast | 148 | 77 | 0.52 | 1.42 | 0.74 |
| South Carolina | Southeast | 55 | 93 | 1.70 | 0.80 | 1.37 |
| Alabama | Southeast | 39 | 19 | 0.49 | 1.67 | 0.83 |
| Pennsylvania | Northeast | 38 | 85 | 2.27 | 0.52 | 1.18 |
| Virginia | Southeast | 36 | 32 | 0.90 | 1.12 | 1.01 |
| California | West | 33 | 164 | 4.92 | 0.26 | 1.29 |
| Missouri | Ohio Valley | 27 | 14 | 0.51 | 1.76 | 0.89 |
| Georgia | Southeast | 18 | 62 | 3.49 | 0.37 | 1.29 |
| Kentucky | Ohio Valley | 13 | 35 | 2.64 | 0.45 | 1.19 |
| Tennessee | Ohio Valley | 12 | 25 | 2.09 | 0.60 | 1.26 |
| West Virginia | Ohio Valley | 11 | 30 | 2.59 | 0.45 | 1.17 |

* **Fit.** With the factors, 38 of the 47 jurisdictions are within a factor of two of their claims, holding 93% of the claims, against 20 (83%) with national parameters alone. The county rank correlation rises from 0.751 to 0.759, and the national ratio stays at 0.89. The factors apply to all perils and every scenario; the full list is in `config/regional_factors.csv`.
* **Over-predicted states.** California's factor is 0.26, Georgia's 0.37, Kentucky's and West Virginia's 0.45, Tennessee's 0.60 and Pennsylvania's 0.52.
* **Under-predicted states.** Texas's factor is 1.62, North Carolina's 1.42, Alabama's 1.67 and Mississippi's 2.39.
* **Shrinkage.** Single-catastrophe records are shrunk strongly. Mississippi's claims are 6.7 times the model and New York's 2.0 times, but their factors are 2.39 and 1.17. Both therefore stay below their claims (0.36 and 0.60), as intended: a 45-year record dominated by one event is weak evidence of the long-run mean.
* **States without calibration counties.** Louisiana and Connecticut receive the region effects of the South (1.23) and the Northeast (0.81).

**Interpretation.** A state factor corrects everything that differs between the model and the claims in that state. This includes regional hazard-model bias (pluvial resolution, drainage standards, flood-control works), building practice (basements, elevation, freeboard rules), and the history of insurance penetration. It is a statistical correction, not a physical parameter. It applies to every scenario and horizon, and it changes at state borders.

### 10.4 National results (v1.1)

**Coverage.** The product covers 110.7 million residential buildings in 33,110 ZIP codes. Their structures and contents have a replacement value of US$76.5 trillion. 4.7 million of these buildings are behind flood defences (§4.6), and an expected 1.95 million are post-FIRM homes in the SFHA (§6.3).

**Present climate.**

| Peril | AAL (US$ bn/yr) | AAL ratio | Homes wet, 1-in-100 | ΣL₁₀₀ (US$ bn) | ΣL₁,₀₀₀ (US$ bn) |
|---|---|---|---|---|---|
| Fluvial | 7.91 | 0.010% | 1.22 million | 138 | 1,114 |
| Pluvial | 15.58 | 0.020% | 2.84 million | 193 | 659 |
| Coastal | 1.98 | 0.003% | 0.55 million | 27 | 272 |
| **Combined** | **24.63** | **0.032%** | **4.35 million** | **349** | **1,862** |

ΣL_T is the national sum of local T-year losses. It assumes every home has its own T-year flood at the same time (§8), so it is a measure of concentration, not the loss of a single event. The combined values are below the sum of the perils because the deepest peril is taken per home (§4.5).

**Change from v1.0.** The present-climate total rises from US$20.74 billion to US$24.63 billion (+19%). Three changes interact:
* **Post-FIRM elevation** lowers the losses of SFHA homes (§10.2).
* **The higher national vulnerability factor** (0.92 instead of 0.62) raises the losses of all other homes.
* **The state factors** move losses towards the Gulf and Plains states and away from California, the Southwest and Appalachia (§10.3).

The largest state changes:
* **Higher:** Texas US$1.76 → 3.70 billion, New York 1.04 → 1.71, Florida 1.40 → 1.60, Louisiana 0.95 → 1.34, Mississippi 0.31 → 0.84.
* **Lower:** California 2.47 → 0.91, Kentucky 0.99 → 0.62, West Virginia 0.97 → 0.60, Pennsylvania 0.92 → 0.68, Oregon 0.42 → 0.14.

The hazard is unchanged, so the counts of wet homes are the same as in v1.0.

**Climate change, fixed defence crests** (AAL in US$ billion per year, change against present climate):

| Peril | Present | SSP2-4.5 2030 | SSP2-4.5 2060 | SSP5-8.5 2030 | SSP5-8.5 2060 |
|---|---|---|---|---|---|
| Fluvial | 7.91 | 9.43 (+19%) | 10.29 (+30%) | 9.44 (+19%) | 10.83 (+37%) |
| Pluvial | 15.58 | 18.46 (+19%) | 20.29 (+30%) | 18.20 (+17%) | 21.33 (+37%) |
| Coastal | 1.98 | 2.56 (+29%) | 5.25 (+165%) | 2.59 (+31%) | 5.84 (+194%) |
| **Combined** | **24.63** | **29.37 (+19%)** | **34.54 (+40%)** | **29.17 (+18%)** | **36.64 (+49%)** |

* **2030.** The two scenarios barely differ in 2030. The 2030 change is already +18–19% because the CMIP6 factors are relative to 1995–2014, so they span about 25 years of warming.
* **Amplification.** River and rainfall losses rise three to four times faster than the change factors.
  * The CMIP6 ensemble-median change in rx1day at homes has a median of +5% in 2030, +9% in 2060 under SSP2-4.5 and +11% in 2060 under SSP5-8.5. The rx5day changes are +5%, +8% and +10%.
  * Losses begin only above the calibrated channel capacities and drainage standard, so a small rise in extremes produces a several-times larger relative rise in AAL (§11, item 7).

**Defence scenarios** (combined AAL, US$ billion per year, change against present climate):

| Defences | SSP2-4.5 2030 | SSP2-4.5 2060 | SSP5-8.5 2030 | SSP5-8.5 2060 |
|---|---|---|---|---|
| Fixed crest (default) | 29.37 (+19%) | 34.54 (+40%) | 29.17 (+18%) | 36.64 (+49%) |
| Raised crest | 29.07 (+18%) | 33.43 (+36%) | 28.85 (+17%) | 35.37 (+44%) |
| Coastal only: fixed → raised | 2.56 → 2.35 | 5.25 → 4.23 | 2.59 → 2.37 | 5.84 → 4.68 |

* **Defended homes.** The 4.7 million defended homes (4% of all homes) have an AAL of US$1.24 billion per year today. Under SSP5-8.5 in 2060 it rises to US$2.91 billion with fixed crests and to US$1.64 billion with raised crests. Their coastal AAL rises from US$0.38 billion to 1.64 and 0.47 billion respectively. With fixed crests they account for 14% of the national increase and a third of the coastal increase.
* **The most affected ZIP codes** lie on low land entirely behind levees. The storm tide there has a narrow distribution, so a few decimetres of sea-level rise turn today's 100-year crest level into a frequent event under fixed crests. Under SSP5-8.5 in 2060:

  | ZIP | Place | Today (US$ M/yr) | Fixed crest | Raised crest |
  |---|---|---|---|---|
  | 94404 | Foster City, CA | 1.8 | 109 | 3.3 |
  | 94065 | Redwood Shores, CA | 1.3 | 76 | 2.1 |
  | 70380 | Morgan City, LA | 37 | 136 | 49 |

* **What raising crests does not remove.** Undefended homes, pluvial flooding and events beyond the design standard still grow, so the national climate signal changes little (+44% instead of +49%). Actual adaptation will lie between the two scenarios and differ from place to place. Costs and feasibility of raising defences are not modelled.

**Comparison with published estimates.**

| Source | Scope | Present AAL | Change |
|---|---|---|---|
| This product, v1.1 | residential buildings, 48 states + DC, 2025 USD | US$24.6 billion | +40% (SSP2-4.5) / +49% (SSP5-8.5) by 2060, fixed crests; about +33% by 2050 (SSP2-4.5, interpolated) |
| First Street Foundation (2021) | residential properties | ≈ US$20 billion (2021 USD; ≈ US$24 billion in 2025 USD) | +61% by 2051 (US$32.2 billion) |
| Wing et al. (2022) | all buildings, 2021 USD | US$32.1 billion (2020) | +26.4% by 2050 (RCP4.5, climate change only) |
| NFIP claims, 1980–2024 | insured residential buildings and contents, 2025 USD | US$3.0 billion of claims per year | — |

At a common price level the present-day national total agrees with First Street's residential estimate. It is plausible against Wing et al.'s figure for all building types, most of which is residential. The climate signal lies between the two studies.

**Geography.**
* **Leading states** (present climate, combined): Texas US$3.70 billion (15% of the national AAL), New York 1.71, Florida 1.60, Louisiana 1.34, North Carolina 1.20, Missouri 1.16, Virginia 1.03, California 0.91, Mississippi 0.84, Alabama 0.82.
* **Concentration.** The top 1% of ZIP codes carry 20% of the AAL. The median ZIP AAL ratio is 0.024% per year, the 90th percentile 0.19% and the 99th percentile 0.94%. 285 ZIP codes exceed 1% per year.
* **Regional calibration.** These state totals include the state factors (§10.3). Mississippi, New York and Louisiana remain below their claims, by design, because their records are dominated by single catastrophes.

**FEMA flood zones** (share of homes with 100-year depth > 0, present climate, after defences; exact zone codes, §3.2):

| FEMA zone | Homes | Fluvial | Pluvial | Coastal | Combined |
|---|---|---|---|---|---|
| A zones (A, AE, AH, AO, A1–A30) | 4.03 million | 14.6% | 11.5% | 11.1% | 33.1% |
| V zones (coastal high hazard) | 0.07 million | 9.3% | 2.4% | 42.8% | 46.2% |
| X (outside the SFHA) | 97.5 million | 0.4% | 2.1% | 0.0% | 2.5% |
| X, protected by an accredited levee | 1.46 million | 0.0% | 6.0% | 0.0% | 6.0% |
| A99 and AR (levee zones in the SFHA) | 0.04 million | 0.0% | 4.8% | 0.0% | 4.8% |
| Unmapped (D, "area not included", none) | 7.56 million | 2.9% | 3.0% | 0.3% | 5.7% |

* **Inside the SFHA.** By FEMA's definition every SFHA home lies inside the 100-year floodplain. The model wets 33% of A-zone homes. (v1.0 reported 29%, because it counted "AREA NOT INCLUDED" as an A zone, §3.2.) There are two reasons for the gap:
  * the calibrated effective channel capacity lowers 100-year river stages below the FEMA-anchored ones, wherever NFIP claims show smaller losses (§10.2; the FEMA-anchored development model wetted 60% of SFHA homes by river flooding alone);
  * many SFHA homes stand on ground slightly above the flood level in the 10 m elevation data.

  Post-FIRM elevation does not change these depths; it changes the damage once a home is wet.
* **Total.** 4.35 million homes are wet at 1-in-100, similar to the 4.1 million homes in A and V zones. Two thirds of them are outside the SFHA, mostly with shallow pluvial flooding in zone X.

**Coastal water levels against FEMA BFEs.** The check uses coastal-flooded homes with a static BFE:
* **A zones** (439,707 homes): the modelled 100-year still-water level is a median 2.1 ft below the BFE (interquartile range −3.4 to −0.4 ft).
* **V zones** (32,062 homes): it is a median 6.5 ft below the BFE (−8.1 to −5.2 ft).

BFEs include wave crests: up to about 3 ft in A zones and more in V zones. The modelled still-water levels sit below them by about the expected wave contribution.

---

## 11. Key assumptions and limitations

1. **Model resolution.** Hydraulics run at 30 m. Building-scale terrain enters only through the building's ground elevation. Micro-topography, curbs and building footprints are not resolved.
2. **HAND method.** HAND is less reliable in very flat terrain, at confluences, under backwater from tides or large rivers, and where flow is controlled by structures (dams, bridges, levees). Rating curves are calibrated to FEMA flood-zone extents at the 100-year level, not to stream gauges; where FEMA maps are outdated or missing, a regional drainage-area-class calibration is used (§4.2). The channel-capacity return periods are effective, NFIP-calibrated values (§10.2); they also absorb biases of the 30 m rating curves and of NWM peak flows, and should not be read as surveyed channel capacities.
3. **Flood frequency.** River flows come from a model simulation (NWM v3.0, not gauge observations) and inherit its biases. 250- to 1,000-year quantiles are extrapolations of 43 years of data.
4. **Pluvial.** Storm drainage is represented by a uniform effective standard (§4.3, NFIP-calibrated), not by pipe hydraulics. A single 24-hour Type II storm shape is used nationally. Western Washington and Oregon use NOAA Atlas 2 (1973) design rainfall because NOAA Atlas 14 does not cover them.
5. **Coastal.** A static, connected bathtub with uniform inland attenuation is used. There are no dynamic surge, wave run-up, overtopping or erosion processes; wave effects enter only through V and Coastal A damage functions. Great Lakes water levels are not modelled in this release.
6. **Flood defences.** Defences come from FEMA levee zones, the USACE National Levee Database and an implicit-coastal-defence rule (§4.6). They are modelled as crests that either hold or fail completely (failure floods the protected area to the undefended depth, as in Hazus); breach probabilities below the design level, interior drainage capacity and overtopping volumes are not modelled. Two bounding defence scenarios are delivered: fixed crests (no adaptation), which makes the climate signal in defended areas such as New Orleans and San Francisco Bay large, and crests raised to keep their standard, with no cost or feasibility constraint. Actual adaptation will lie between them and differ by place.
7. **Climate.** Precipitation change factors come from the CMIP6 ensemble median of mean annual maxima and are applied uniformly across return periods. Fluvial flows are assumed to scale with rx5day (elasticity 1). Changes in snowmelt, antecedent soil moisture, storm frequency and tropical-cyclone climatology are not represented beyond sea-level rise. Because losses start only above calibrated channel and drainage capacities, a few-percent rise in extreme rainfall produces a several-times larger relative rise in AAL; the resulting climate signal is higher than in some published national studies (§10.4). The present-climate baseline reflects the climate of the hazard data (NOAA Atlas 14, NWM 1979–2023), close to the CMIP6 reference period 1995–2014 against which change factors are computed.
8. **Exposure.** The building stock is fixed at 2026. First-floor heights are NSI national defaults by foundation type, except that post-FIRM homes in the SFHA are elevated to the BFE (§6.3). Whether an individual home is post-FIRM is not known; it is post-FIRM with the probability observed for insured SFHA homes in its block group, and losses are expected values over both states. Pre-FIRM homes that were elevated later, and elevation required by local codes outside the SFHA, are not modelled. NSI values are modelled, not appraised.
9. **Vulnerability.** Deterministic, central depth–damage functions are used, scaled by a national vulnerability factor and a state calibration factor, both calibrated to NFIP claims (§10.2, §10.3). The state factors are statistical corrections that absorb regional hazard-model biases as well as differences in building practice and insurance take-up; they are held constant across scenarios and change at state borders. Flood duration, velocity and contamination are not considered, except through the coastal curves.
10. **Spatial coincidence.** ZIP-level return-period losses are sums of local return-period losses (§8), not event losses.
11. **Calibration target.** NFIP claims (1980–2024) are one realisation of the hazard dominated by a few catastrophes, cover only insured homes, and carry imperfect cause-of-loss codes. Calibration therefore uses insured-equivalent losses, strata defined by FEMA flood-zone shares rather than by the claims themselves, five national parameters, and state factors shrunk according to how much each state's record is dominated by single years. Penetration rates are those of 2026 and are applied to the whole 45-year record.
12. **Coverage.** The product covers the 48 contiguous states and DC (99.5% of US residential buildings). Alaska, Hawaii and Puerto Rico are excluded (no Annual NLCD land cover and no COAST-RP/HAND coverage in this release).
13. **Uncertainty.** The product reports central estimates. The CMIP6 ensemble 10th–90th percentile and AR6 likely ranges are available as sensitivity extensions.

---

## 12. Reproducibility

The pipeline is fully scripted (`scripts/00_*.py` … `scripts/14_*.py`) and uses only public data:

| Step | Script | Output |
|---|---|---|
| 00 | download static inputs | census, WBD, NHDPlus, Atlas 14, COAST-RP, CCKP, DDFs, NLCD |
| 01 | NSI county download | raw NSI 2026 (≈3,240 counties) |
| 02 | NWM v3.0 annual maxima | 2.78 M reaches × 45 water years |
| 03 | 3DEP 1″ DEM | 1,161 tiles |
| 04 | residential exposure | buildings with ZCTA and HUC-8 |
| 04b | post-FIRM inputs | post-FIRM share per block group, elevation classes (§6.3) |
| 05 | soils | national hydrologic soil group raster |
| 05b | design-rainfall mosaic | Atlas 14 + NOAA Atlas 2 gap fill (western WA/OR) |
| 06 | NHDPlus preparation | flowlines by HUC-8 |
| 06b | USACE National Levee Database | levee systems and leveed areas (public NLD web API) |
| 07 | flood frequency | LP3 quantiles per reach |
| 08 | hazard (fluvial / pluvial / coastal) | building depths per HUC-8 |
| 09a | FEMA rating-curve calibration | CSI-optimal 100-year stage per reach |
| 09 | risk | building AAL / RP losses; ZIP partial sums |
| 10 | aggregation | ZIP product tables |
| 11 | platform bundle | web data |
| 12a | NFIP claims download | OpenFEMA NFIP claims v3 by state |
| 12b | NFIP policies download | OpenFEMA NFIP policies v3, A/V zones, 2019 |
| 12c | NFIP claims with post-FIRM indicator | check of the post-FIRM model (§10.2) |
| 12 | NFIP validation (pilot) | ZIP-level comparison |
| 14 | NFIP calibration | parameter grid by county × SFHA, cross-validated fit (§10.2); state factors (§10.3) |
| 15 | release | GitHub release with the product tables and platform data; Pages deployment |

`scripts/run_national.sh` runs steps 08–11 for all HUC-8s; `scripts/run_finalize.sh` runs the calibration (14), the final risk run (09) and steps 10–11; `scripts/run_v11.sh` rebuilds version 1.1 (12b, 04b, 14 with state factors, 09–11) on the same hazard layers.

Code, documentation and release notes are at <https://github.com/alanhuang116/flood-risk-atlas>. Each version's product tables and platform data are attached to the GitHub release of that version (`v1.1.0` for this document). The platform is served from that data at <https://alanhuang116.github.io/flood-risk-atlas/>.

---

## 13. Version history

**1.1.0** (November 2026)
* **Post-FIRM elevation (§6.3).** Homes built in the SFHA after the community's first flood map have no basement and their first floor at the BFE plus an observed elevation class. Shares and elevations come from 1.92 million NFIP policies. In v1.0 this effect was absorbed by the calibrated channel capacity.
* **State calibration factors (§10.3).** One factor per state on top of the national calibration, with empirical-Bayes shrinkage over NOAA climate regions and validation on held-out loss years.
* **Raised-crest defence scenario (§4.6).** Every result for 2030 and 2060 is also delivered with defences raised to keep their design standard (`defence_scenario`).
* **Zone codes (§3.2).** FEMA zones are matched on exact codes; v1.0 counted "AREA NOT INCLUDED" (unmapped land) as an A zone.
* **Recalibration.** The national parameters were re-fitted with these changes (§10.2). The present-day national AAL rises from US$20.7 billion to 24.6 billion per year (§10.4).

**1.0.0** (November 2026) — first national release: national NFIP calibration of five parameters, flood defences, below-first-floor threshold.

---

## 14. References

* Bates, P. D., Horritt, M. S., & Fewtrell, T. J. (2010). A simple inertial formulation of the shallow water equations for efficient two-dimensional flood inundation modelling. *Journal of Hydrology*, 387, 33–45.
* de Almeida, G. A. M., Bates, P., Freer, J. E., & Souvignet, M. (2012). Improving the stability of a simple formulation of the shallow water equations for 2-D flood modeling. *Water Resources Research*, 48.
* Dullaart, J. C. M., et al. (2021). Accounting for tropical cyclones more than doubles the global population exposed to low-probability coastal flooding. *Communications Earth & Environment*, 2, 135.
* Efron, B., & Tibshirani, R. J. (1993). *An Introduction to the Bootstrap*. Chapman & Hall, New York.
* Federal Emergency Management Agency. Code of Federal Regulations, Title 44, §60.3: Floodplain management criteria for flood-prone areas.
* First Street Foundation (2021). *The Cost of Climate: America's Growing Flood Risk*. First Street Foundation, New York.
* Garner, G. G., et al. (2021). IPCC AR6 Sea-Level Rise Projections. Zenodo, doi:10.5281/zenodo.6382554.
* Interagency Advisory Committee on Water Data (1982). Guidelines for Determining Flood Flow Frequency, Bulletin 17B.
* Kalyanapu, A. J., Burian, S. J., & McPherson, T. N. (2009). Effect of land use-based surface roughness on hydrologic model output. *Journal of Spatial Hydrology*, 9(2).
* Karl, T. R., & Koss, W. J. (1984). Regional and national monthly, seasonal, and annual temperature weighted by area, 1895–1983. *Historical Climatology Series* 4-3, NOAA National Climatic Data Center, Asheville, NC.
* Morris, C. N. (1983). Parametric empirical Bayes inference: theory and applications. *Journal of the American Statistical Association*, 78(381), 47–55.
* Nobre, A. D., et al. (2011). Height Above the Nearest Drainage — a hydrologically relevant new terrain model. *Journal of Hydrology*, 404, 13–29.
* USACE (2003). Economic Guidance Memorandum 04-01 / EGM 01-03: Generic depth-damage relationships for residential structures.
* USDA NRCS (1986). Urban Hydrology for Small Watersheds, TR-55.
* Wing, O. E. J., Pinter, N., Bates, P. D., & Kousky, C. (2020). New insights into US flood vulnerability revealed from flood insurance big data. *Nature Communications*, 11, 1444.
* Wing, O. E. J., et al. (2022). Inequitable patterns of US flood risk in the Anthropocene. *Nature Climate Change*, 12, 156–162.
* Zheng, X., Tarboton, D. G., Maidment, D. R., Liu, Y. Y., & Passalacqua, P. (2018). River channel geometry and rating curve estimation using height above the nearest drainage. *JAWRA*, 54(4).
