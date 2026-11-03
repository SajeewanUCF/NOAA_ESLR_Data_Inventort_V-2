# North Carolina Coastal Data Inventory

Streamlit dashboard that inventories observed and model/hindcast coastal data
for the North Carolina coast and validates models against observations.

## Data sources

| Tab | Source | Access | Period | Key? |
|---|---|---|---|---|
| Observed | NOAA CO-OPS tide gauges | Tides & Currents data + metadata APIs | full record per gauge | no |
| Observed | NDBC buoys / C-MAN | NDBC station table + THREDDS stdmet (ERDDAP as backup) | 1970 → present | no |
| Observed | USGS stream & tidal gauges | USGS Water Data OGC API (`dataretrieval`) | full record per gauge | optional |
| Model | USACE WIS (Atlantic) | ERDC THREDDS / OPeNDAP | 1980 → 2024 | no |
| Model | CoDEC / GTSM-ERA5 | Copernicus CDS `sis-water-level-change-timeseries-cmip6` | 1950 → 2024 | **yes** |
| Model | NOAA CORA (GEC) | AWS `s3://noaa-nos-cora-pds` Zarr (intake catalog) | 1979 → 2022 | no |
| Model | ERA5 wind | Open-Meteo archive API (`models=era5`) | 1940 → present | no |
| Model | CONUS404 | USGS HyTEST Zarr on OSN | Oct 1979 → Sep 2024 | no |

All loaders return the same standard column names (`water_level_m`, `hs_m`,
`wind_speed_ms`, …) in UTC, so any observed series can be paired with any model
series in the **Validation** tab.

## Structure

```
nc-data-inventory/
├── app.py                 # dashboard: map, Observed / Model / Validation tabs
├── config.py              # map centre, NC bounding box, basemaps
├── validation.py          # alignment + skill metrics
├── availability.py        # Availability tab (renders templates/inventory.*)
├── inventory.py           # site × source matching and V1 rating rules
├── sites.py               # RCCP study sites (data/rccp_sites.csv)
├── templates/             # HTML/CSS/JS of the Availability tab (ported from nc_data_inventory_V1)
├── requirements.txt
├── .streamlit/secrets.toml.example
├── data/                  # rccp_sites.csv, nc_land.json, cached station lists / CORA mesh (commit);
│                          # data/cache is ignored
└── loaders/
    ├── __init__.py        # registry: OBSERVED and MODEL sources
    ├── base.py            # shared interface + helpers
    ├── noaa_coops.py  ndbc.py  usgs_nwis.py               # observed
    └── wis.py  codec_gtsm.py  cora.py  era5.py  conus404.py   # model / hindcast
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then paste your keys
streamlit run app.py
```

Quick check of any single source: `python -m loaders.ndbc` (same for every loader).

### One-time setup steps (then commit the CSVs in `data/`)

```bash
python -m loaders.wis --build            # WIS station locations -> data/wis_stations.csv (few minutes)
python -m loaders.ndbc --build           # NDBC stations + records -> data/ndbc_stations.csv (optional, faster start)
python -m loaders.cora --build           # CORA NC mesh nodes   -> data/cora_*.csv.gz
python -m loaders.codec_gtsm             # GTSM output points  -> data/gtsm_stations.csv (needs CDS key)
```

### API keys

* **CDSAPI_KEY** (required for CoDEC/GTSM): register at cds.climate.copernicus.eu,
  copy the personal access token from your profile, and accept the licence on the
  dataset's download page once.
* **API_USGS_PAT** (optional): free key from api.waterdata.usgs.gov/signup — raises
  the USGS request limit.

## Validation tab

Pick an observed station and a model source. Gridded models (CORA, ERA5, CONUS404)
are sampled at the cell/node nearest the station; point models (WIS, GTSM) let you
choose among the five nearest output points. Both series are resampled to a common
step and compared with **N, Bias, RMSE, MAE, R, Scatter index, Willmott d, peaks**
(directions use circular differences). Plots: time-series overlay, scatter vs 1:1,
model − observed. Paired series and metrics download as CSV.

Datum tip: CORA and GTSM water levels are relative to MSL, so choose **MSL** for
NOAA gauges (the validation tab defaults to it) or tick *Remove means*.

## RCCP study sites

The 10 project sites live in `data/rccp_sites.csv` (number, name, setting, note, and two flags:
`sheltered` = sound/estuary/river site, `river_relevant` = riverine or compound-flood hazard).
They appear on every map; the **Near study site** selector sorts stations by distance (or, in
point mode, moves the point to the site), and the Validation tab can sort stations the same way.

## Availability tab

A port of the `nc_data_inventory_V1` page, computed live from every source:
summary strip; zoomable coast map (drag, Ctrl/⌘ + scroll, buttons) with sites, all stations and
WIS/GTSM output points (legend items toggle layers); site panel with the nearest station of each
type, the nearest model point/node/cell and Good/Fair/Poor ratings; the **Nearest station,
distance and record length** matrix (observations + models, CSV download); the coverage chart
and the water-level record timeline. Colours follow the app theme (light/dark/custom).

Rating rules are those of V1 (see `inventory.py`); models use the same distance limits for the
quantity they provide. Model ratings describe coverage, not accuracy — use the Validation tab
for skill.

## NOAA CORA

CORA's bucket was restructured in 2026 (`cora_gec/...`, virtual Zarr stores, plus a per-state
**NC shoreline** water-level file). The loader tries, in order: the NC shoreline file (water
level), NOAA's intake catalog, any Zarr store found by listing the bucket, then the yearly
NetCDF files. Mesh option **auto** uses the shoreline nodes when one is within 1.5 km.

In point mode the mesh is drawn around the point (pink = full mesh nodes and triangles, blue =
shoreline nodes); click a node to select it exactly.

```bash
python -m loaders.cora --build      # cache NC mesh nodes (+ triangles) in data/ — commit them
python -m loaders.cora --diagnose   # what the bucket contains and which access path works
```

## Performance notes

* Every download is cached for 12 h; long records are fetched in chunks.
* GTSM: the CDS sends one global file per month, so each month is downloaded once,
  cut to the NC region and cached in `data/cache/gtsm/`. CDS queues can take minutes
  per month — pre-fetch with `python -m loaders.codec_gtsm --prefetch 2018 2018`.
* CORA full-mesh water levels are slow to read from the cloud (NOAA known issue); the NC
  shoreline nodes are much faster. Keep full-mesh requests to months.
* CONUS404 hourly: use the *daily* product for multi-year periods.
* NDBC wind is measured at anemometer height (~4–5 m), models give 10 m wind.

## Deploy (Streamlit Community Cloud)

1. Push this folder to GitHub (including `data/*.csv` once built).
2. share.streamlit.io → **New app** → repo, branch, `app.py`.
3. **Settings → Secrets**: paste the contents of your `secrets.toml`.
