"""Shared conventions for every data loader.

Every loader module (one per data source) exposes the same interface so the
dashboard can treat them generically:

    SOURCE_NAME : str               human-readable name
    KIND        : "point" | "gridded"
                  point   -> fixed stations/output points listed by list_stations()
                  gridded -> any lat/lon inside COVERAGE; get_data() takes
                             station_id = "lat,lon" and uses the nearest grid cell/node
    COVERAGE    : dict              {"start", "end", "resolution", "note"} for the inventory
    PRODUCTS    : dict[str, dict]   product key -> {"label": ..., ...}
    OPTIONS     : dict[str, list]   extra user choices (e.g. datum), may be empty

    list_stations() -> pd.DataFrame   columns = STATION_COLUMNS (may be empty for gridded)
    get_data(station_id, start, end, product, **options) -> pd.DataFrame
        index  : DatetimeIndex (UTC, named "time")
        columns: standard names below, so observed and model series can be paired
        attrs  : optional {"model_location": (lat, lon), "note": str}

    status() -> str | None   (optional) message shown in the UI, e.g. "API key missing"

Standard column names (units are part of the name):
    water_level_m  surge_m  tide_m
    hs_m  tp_s  tm_s  wave_dir_deg
    wind_speed_ms  wind_dir_deg  wind_gust_ms  u10_ms  v10_ms
    pressure_mb  air_temp_c  water_temp_c  precip_mm
    discharge_m3s  gage_height_m
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

from config import NC_BBOX

STATION_COLUMNS = [
    "source",       # e.g. "NOAA CO-OPS"
    "station_id",   # id used by the API
    "name",
    "lat",
    "lon",
    "start",        # first available date (Timestamp, UTC) or NaT
    "end",          # last available date (Timestamp, UTC) or NaT (NaT = ongoing)
    "status",       # "active" / "historical" / "model"
]

# Folder for station lists that are slow to build (commit these to GitHub)
# and for downloaded-data caches (ignored by git).
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "cache"


def empty_stations() -> pd.DataFrame:
    return pd.DataFrame(columns=STATION_COLUMNS)


def in_bbox(df: pd.DataFrame, bbox=NC_BBOX) -> pd.DataFrame:
    """Keep rows whose lat/lon fall inside bbox = (lon_min, lat_min, lon_max, lat_max)."""
    lon_min, lat_min, lon_max, lat_max = bbox
    mask = df["lon"].between(lon_min, lon_max) & df["lat"].between(lat_min, lat_max)
    return df.loc[mask].reset_index(drop=True)


def date_chunks(start, end, max_days: int):
    """Split [start, end] into consecutive windows no longer than max_days."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    cur = start
    while cur <= end:
        nxt = min(cur + pd.Timedelta(days=max_days - 1), end)
        yield cur, nxt
        cur = nxt + pd.Timedelta(days=1)


def month_starts(start, end):
    """First day of every month touched by [start, end]."""
    return pd.date_range(pd.Timestamp(start).to_period("M").to_timestamp(),
                         pd.Timestamp(end), freq="MS")


def parse_point(station_id: str) -> tuple[float, float]:
    """'35.2,-75.6' -> (35.2, -75.6)"""
    lat, lon = (float(v) for v in str(station_id).split(","))
    return lat, lon


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, np.asarray(lat2), np.asarray(lon2)))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 6371.0 * 2 * np.arcsin(np.sqrt(a))


def nearest(lat: float, lon: float, lats, lons) -> tuple[int, float]:
    """Index (into the flattened arrays) and distance in km of the closest point."""
    d = haversine_km(lat, lon, np.ravel(lats), np.ravel(lons))
    i = int(np.nanargmin(d))
    return i, float(d[i])


def wind_from_uv(u, v):
    """Speed and meteorological direction (degrees the wind blows FROM)."""
    speed = np.hypot(u, v)
    direction = (270.0 - np.degrees(np.arctan2(v, u))) % 360.0
    return speed, direction


def uv_from_wind(speed, direction_deg):
    rad = np.radians(direction_deg)
    return -speed * np.sin(rad), -speed * np.cos(rad)


def secret(name: str) -> str | None:
    """Read an API key from the environment (the app copies st.secrets into os.environ)."""
    return os.environ.get(name) or None


def finish(df: pd.DataFrame, start=None, end=None) -> pd.DataFrame:
    """Common tidy-up: UTC index named 'time', sorted, de-duplicated, clipped."""
    if df is None or df.empty:
        return pd.DataFrame()
    idx = pd.DatetimeIndex(df.index)
    df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df = df.sort_index()
    df = df[~df.index.duplicated()]
    if start is not None:
        df = df[df.index >= pd.Timestamp(start, tz="UTC")]
    if end is not None:
        df = df[df.index < pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)]
    df.index.name = "time"
    return df
