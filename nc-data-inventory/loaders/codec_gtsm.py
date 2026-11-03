"""CoDEC / GTSM-ERA5 — global tide and storm-surge reanalysis (Deltares).

Source: Copernicus Climate Data Store dataset "sis-water-level-change-timeseries-cmip6",
experiment "reanalysis" (GTSM forced by ERA5), 1950-2024, version 3.
    https://cds.climate.copernicus.eu/datasets/sis-water-level-change-timeseries-cmip6
Variables: total water level, storm surge residual, tidal elevation (relative to MSL).

Requirements
------------
A free CDS account and personal access token, accepted licence for this dataset,
and the token set as CDSAPI_KEY (environment variable or Streamlit secret).

How data are fetched
--------------------
The CDS delivers one global file per month (~43,000 output points), so each
month is downloaded once, cut down to the NC region and cached under
data/cache/gtsm/. Requests queue on the CDS side and can take minutes per
month: start with short periods, or pre-fetch with
    python -m loaders.codec_gtsm --prefetch 2018 2018
The station list is built from the first download and saved to
data/gtsm_stations.csv (commit it).
"""

from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from config import NC_BBOX
from loaders.base import (CACHE_DIR, DATA_DIR, STATION_COLUMNS, empty_stations, finish,
                          month_starts, secret)

SOURCE_NAME = "CoDEC / GTSM-ERA5"
KIND = "point"
COVERAGE = {"start": "1950-01-01", "end": "2024-12-31", "resolution": "GTSM output points (0.1° coast), 10-min/hourly",
            "core": ("1979-01-01", "2018-12-31"),   # original release; 1950-2024 = extended version
            "note": "Total water level, surge residual, tide (rel. MSL); needs CDS key"}

DATASET = "sis-water-level-change-timeseries-cmip6"
CDS_URL = "https://cds.climate.copernicus.eu/api"
VERSION = "v3"
STATION_FILE = DATA_DIR / "gtsm_stations.csv"
GTSM_CACHE = CACHE_DIR / "gtsm"

VARIABLES = {"total_water_level": "water_level_m", "storm_surge_residual": "surge_m",
             "tidal_elevation": "tide_m"}
FILE_VARS = {"waterlevel": "water_level_m", "surge": "surge_m", "tide": "tide_m"}

PRODUCTS = {
    "hourly":        {"label": "Water level, surge, tide (hourly)",  "agg": "hourly"},
    "10_min":        {"label": "Water level, surge, tide (10-min)",  "agg": "10_min"},
    "daily_maximum": {"label": "Daily maximum water level & surge",  "agg": "daily_maximum"},
}
OPTIONS: dict = {}


def status() -> str | None:
    if not secret("CDSAPI_KEY"):
        return ("CDSAPI_KEY not set. Create a free account at cds.climate.copernicus.eu, accept "
                f"the licence of '{DATASET}', and add your personal access token as the "
                "CDSAPI_KEY secret.")
    if not STATION_FILE.exists():
        return "GTSM station list not built yet: the first request downloads one month (a few minutes)."
    return None


def _client():
    import cdsapi
    return cdsapi.Client(url=CDS_URL, key=secret("CDSAPI_KEY"), quiet=True, progress=False)


def _station_coords(ds: xr.Dataset):
    lon = ds["station_x_coordinate"].values
    lat = ds["station_y_coordinate"].values
    ids = ds["stations"].values if "stations" in ds.coords else np.arange(ds.sizes["stations"])
    return ids, lat, lon


def _download_month(agg: str, month: pd.Timestamp) -> Path:
    """Download one global month, keep only NC-region stations, cache as NetCDF."""
    GTSM_CACHE.mkdir(parents=True, exist_ok=True)
    out = GTSM_CACHE / f"gtsm_{agg}_{month:%Y%m}.nc"
    if out.exists():
        return out
    variables = list(VARIABLES) if agg != "daily_maximum" else ["total_water_level", "storm_surge_residual"]
    with tempfile.TemporaryDirectory() as tmp:
        zpath = Path(tmp) / "gtsm.zip"
        _client().retrieve(DATASET, {
            "variable": variables, "experiment": "reanalysis", "temporal_aggregation": [agg],
            "year": [f"{month.year}"], "month": [f"{month:%m}"], "version": [VERSION],
            "format": "zip",
        }, str(zpath))
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmp)
        pieces = []
        for nc in sorted(Path(tmp).rglob("*.nc")):
            with xr.open_dataset(nc) as ds:
                ids, lat, lon = _station_coords(ds)
                lon0, lat0, lon1, lat1 = NC_BBOX
                keep = np.where((lon >= lon0) & (lon <= lon1) & (lat >= lat0) & (lat <= lat1))[0]
                sub = ds.isel(stations=keep).load()
                if "stations" not in sub.coords:
                    sub = sub.assign_coords(stations=ids[keep])
                pieces.append(sub)
        merged = xr.merge(pieces, compat="override")
        merged.to_netcdf(out)
    if not STATION_FILE.exists():
        _write_stations(merged)
    return out


def _write_stations(ds: xr.Dataset):
    ids, lat, lon = _station_coords(ds)
    DATA_DIR.mkdir(exist_ok=True)
    pd.DataFrame({"station_id": ids.astype(str), "lat": lat, "lon": lon}).to_csv(STATION_FILE, index=False)


def list_stations() -> pd.DataFrame:
    if not STATION_FILE.exists():
        if not secret("CDSAPI_KEY"):
            return empty_stations()
        try:
            _download_month("daily_maximum", pd.Timestamp("2018-09-01"))
        except Exception:
            return empty_stations()
    df = pd.read_csv(STATION_FILE, dtype={"station_id": str})
    df["name"] = "GTSM point " + df["station_id"]
    df["start"] = pd.Timestamp(COVERAGE["start"], tz="UTC")
    df["end"] = pd.Timestamp(COVERAGE["end"], tz="UTC")
    df["status"] = "model"
    df["source"] = SOURCE_NAME
    return df[STATION_COLUMNS]


def get_data(station_id: str, start, end, product: str = "hourly", progress=None, **_) -> pd.DataFrame:
    if not secret("CDSAPI_KEY"):
        raise RuntimeError(status())
    agg = PRODUCTS[product]["agg"]
    months = list(month_starts(start, end))
    parts = []
    for i, m in enumerate(months):
        path = _download_month(agg, m)
        with xr.open_dataset(path) as ds:
            ids = ds["stations"].values.astype(str)
            k = int(np.where(ids == str(station_id))[0][0])
            pt = ds.isel(stations=k)
            cols = {FILE_VARS[v]: pt[v].values for v in FILE_VARS if v in pt}
            parts.append(pd.DataFrame(cols, index=pd.to_datetime(pt["time"].values)))
        if progress:
            progress((i + 1) / len(months))
    out = finish(pd.concat(parts), start, end)
    out.attrs["model_location"] = (float(pt["station_y_coordinate"]), float(pt["station_x_coordinate"]))
    return out


if __name__ == "__main__":
    if "--prefetch" in sys.argv:
        y0, y1 = (int(a) for a in sys.argv[sys.argv.index("--prefetch") + 1:][:2])
        for m in month_starts(f"{y0}-01-01", f"{y1}-12-31"):
            print("fetching", m.strftime("%Y-%m"), flush=True)
            _download_month("hourly", m)
    else:
        print(list_stations())
