"""USACE Wave Information Study (WIS) hindcast — Atlantic.

Source: ERDC CHL THREDDS server, one NetCDF file per station per month:
    https://chldata.erdc.dren.mil/thredds/catalog/wis/Atlantic/catalog.html
    .../wis/Atlantic/ST63255/2013/WIS-ocean_waves_ST63255_201301.nc

Station list
------------
THREDDS has no station-location index, so the list is built once by crawling
the catalog and reading each station's lat/lon over OPeNDAP (a few minutes).
Run this once locally and commit the result so the app starts instantly:

    python -m loaders.wis --build      # writes data/wis_stations.csv
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests
import xarray as xr

from config import NC_BBOX
from loaders.base import DATA_DIR, STATION_COLUMNS, empty_stations, finish, in_bbox, month_starts

SOURCE_NAME = "USACE WIS"
KIND = "point"
COVERAGE = {"start": "1980-01-01", "end": "2024-12-31", "resolution": "hindcast output points, hourly",
            "note": "WAM hindcast, Atlantic; Hs, Tp, Tm, direction, wind"}

THREDDS = "https://chldata.erdc.dren.mil/thredds"
REGION = "Atlantic"
STATION_FILE = DATA_DIR / "wis_stations.csv"
TIMEOUT = 60
NS = {"t": "http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0",
      "xlink": "http://www.w3.org/1999/xlink"}

PRODUCTS = {
    "waves": {"label": "Waves (Hs, Tp, Tm, direction)", "cols": ["hs_m", "tp_s", "tm_s", "wave_dir_deg"]},
    "wind":  {"label": "Wind (model forcing)",           "cols": ["wind_speed_ms", "wind_dir_deg"]},
    "all":   {"label": "Waves + wind",                   "cols": None},
}
OPTIONS: dict = {}

# standard_name (preferred) or variable name -> column
STD_NAMES = {
    "sea_surface_wave_significant_height": "hs_m",
    "sea_surface_wave_period_at_variance_spectral_density_maximum": "tp_s",
    "sea_surface_wave_mean_period": "tm_s",
    "sea_surface_wave_mean_period_from_variance_spectral_density_first_frequency_moment": "tm_s",
    "sea_surface_wave_from_direction": "wave_dir_deg",
    "wind_speed": "wind_speed_ms",
    "wind_from_direction": "wind_dir_deg",
}
VAR_NAMES = {
    "waveHs": "hs_m", "waveTp": "tp_s", "waveTm": "tm_s", "waveTm1": "tm_s",
    "waveMeanDirection": "wave_dir_deg", "windSpeed": "wind_speed_ms", "windDirection": "wind_dir_deg",
}


# --------------------------------------------------------------------------- #
# Catalog crawling
# --------------------------------------------------------------------------- #
def _catalog(path: str) -> ET.Element:
    r = requests.get(f"{THREDDS}/catalog/wis/{path}/catalog.xml", timeout=TIMEOUT)
    r.raise_for_status()
    return ET.fromstring(r.content)


def _refs(root: ET.Element) -> list[str]:
    return [c.get(f"{{{NS['xlink']}}}title") or c.get("name")
            for c in root.iter(f"{{{NS['t']}}}catalogRef")]


def _files(root: ET.Element) -> list[str]:
    return sorted(d.get("urlPath") for d in root.iter(f"{{{NS['t']}}}dataset") if d.get("urlPath"))


def _station_info(sid: str) -> dict | None:
    try:
        years = sorted(int(y) for y in _refs(_catalog(f"{REGION}/{sid}")) if str(y).isdigit())
        if not years:
            return None
        files = _files(_catalog(f"{REGION}/{sid}/{years[-1]}"))
        with xr.open_dataset(f"{THREDDS}/dodsC/{files[0]}") as ds:
            lat = float(np.ravel(ds[_first(ds, ["latitude", "lat"])].values)[0])
            lon = float(np.ravel(ds[_first(ds, ["longitude", "lon"])].values)[0])
        last_files = [f.rsplit("_", 1)[-1][:6] for f in files]
        end = pd.Timestamp(last_files[-1] + "01") + pd.offsets.MonthEnd(0)
        return {"station_id": sid, "lat": lat, "lon": lon if lon <= 180 else lon - 360,
                "start": pd.Timestamp(f"{years[0]}-01-01"), "end": end}
    except Exception:
        return None


def _first(ds, names):
    for n in names:
        if n in ds.variables:
            return n
    raise KeyError(names)


def build_station_list(workers: int = 16, verbose: bool = True) -> pd.DataFrame:
    ids = [s for s in _refs(_catalog(REGION)) if s and s.startswith("ST")]
    if verbose:
        print(f"{len(ids)} WIS {REGION} stations; reading locations…")
    with ThreadPoolExecutor(workers) as ex:
        rows = [r for r in ex.map(_station_info, ids) if r]
    df = pd.DataFrame(rows)
    df = in_bbox(df, NC_BBOX)
    df["name"] = "WIS " + df["station_id"]
    DATA_DIR.mkdir(exist_ok=True)
    df.to_csv(STATION_FILE, index=False)
    if verbose:
        print(f"{len(df)} stations in the NC region -> {STATION_FILE}")
    return df


def list_stations() -> pd.DataFrame:
    if STATION_FILE.exists():
        df = pd.read_csv(STATION_FILE, dtype={"station_id": str})
    else:
        try:
            df = build_station_list(verbose=False)
        except Exception:
            return empty_stations()
    df["start"] = pd.to_datetime(df["start"], utc=True)
    df["end"] = pd.to_datetime(df["end"], utc=True)
    df["status"] = "model"
    df["source"] = SOURCE_NAME
    return df[STATION_COLUMNS]


def status() -> str | None:
    if not STATION_FILE.exists():
        return ("WIS station list not cached yet: the first load crawls the THREDDS catalog "
                "(several minutes). Run `python -m loaders.wis --build` once and commit "
                "data/wis_stations.csv.")
    return None


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def _month(sid: str, month: pd.Timestamp) -> pd.DataFrame:
    url = (f"{THREDDS}/dodsC/wis/{REGION}/{sid}/{month.year}/"
           f"WIS-ocean_waves_{sid}_{month:%Y%m}.nc")
    try:
        with xr.open_dataset(url) as ds:
            cols = {}
            for name, var in ds.data_vars.items():
                col = STD_NAMES.get(var.attrs.get("standard_name", "")) or VAR_NAMES.get(name)
                if col and col not in cols and var.ndim >= 1 and "time" in var.dims:
                    cols[col] = np.ravel(var.squeeze().values)
            return pd.DataFrame(cols, index=pd.to_datetime(ds["time"].values))
    except Exception:
        return pd.DataFrame()


def get_data(station_id: str, start, end, product: str = "waves", progress=None, **_) -> pd.DataFrame:
    months = list(month_starts(start, end))
    parts = []
    with ThreadPoolExecutor(8) as ex:
        for i, df in enumerate(ex.map(lambda m: _month(station_id, m), months)):
            parts.append(df)
            if progress:
                progress((i + 1) / len(months))
    parts = [p for p in parts if not p.empty]
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts)
    cols = PRODUCTS[product]["cols"]
    if cols:
        out = out[[c for c in cols if c in out]]
    return finish(out, start, end)


if __name__ == "__main__":
    if "--build" in sys.argv:
        build_station_list()
    else:
        print(list_stations())
        print(get_data("ST63183", "2018-09-10", "2018-09-16"))  # Hurricane Florence
