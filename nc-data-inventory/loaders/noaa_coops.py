"""NOAA CO-OPS tide gauges (Tides & Currents).

APIs used
---------
Metadata : https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/
Data     : https://api.tidesandcurrents.noaa.gov/api/prod/datagetter

Request-length limits on the data API (enforced by NOAA):
    6-minute water level / predictions : 31 days per request
    hourly_height                      : 365 days per request
    high_low                           : 365 days per request
    daily_mean / monthly_mean          : 3650 days per request
Longer periods are fetched in chunks and concatenated, so the full record of a
station can be pulled (the oldest NC records start in the 1930s-1970s).
"""

from __future__ import annotations

import pandas as pd
import requests

from config import APP_ID
from loaders.base import STATION_COLUMNS, date_chunks, empty_stations, in_bbox

SOURCE_NAME = "NOAA CO-OPS"
KIND = "point"
COVERAGE = {"start": "1846-01-01", "end": None, "resolution": "tide gauges, 6-min / hourly",
            "note": "Verified water levels; datum selectable"}
MDAPI = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi"
DATAAPI = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
TIMEOUT = 60

PRODUCTS = {
    "water_level":   {"label": "Water level (6-min, verified)", "units": "m", "max_days": 31},
    "hourly_height": {"label": "Hourly height (verified)",       "units": "m", "max_days": 365},
    "high_low":      {"label": "High/low water (verified)",      "units": "m", "max_days": 365},
    "monthly_mean":  {"label": "Monthly mean water level",       "units": "m", "max_days": 3650},
    "predictions":   {"label": "Tide predictions (hourly)",      "units": "m", "max_days": 365},
    "wind":          {"label": "Wind (6-min)",                    "units": "m/s", "max_days": 31},
    "air_pressure":  {"label": "Air pressure (6-min)",            "units": "mb", "max_days": 31},
}

OPTIONS = {"datum": ["NAVD", "MSL", "MHHW", "MLLW", "STND"]}


# --------------------------------------------------------------------------- #
# Station list
# --------------------------------------------------------------------------- #
def _station_details(station_id: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return (first, last) dates of record from the station 'details' endpoint."""
    try:
        r = requests.get(f"{MDAPI}/stations/{station_id}/details.json", timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        start = pd.to_datetime(d.get("origyear") or d.get("established"), utc=True, errors="coerce")
        end = pd.to_datetime(d.get("removed"), utc=True, errors="coerce")
        return start, end
    except Exception:
        return pd.NaT, pd.NaT


def list_stations(with_dates: bool = True) -> pd.DataFrame:
    """All CO-OPS water-level stations inside the NC bounding box.

    Includes historic (decommissioned) stations so the inventory shows every
    period that ever existed, not only the currently active gauges.
    """
    frames = []
    for kind, status in (("waterlevels", "active"), ("historicwl", "historical")):
        try:
            r = requests.get(f"{MDAPI}/stations.json", params={"type": kind}, timeout=TIMEOUT)
            r.raise_for_status()
        except Exception:
            continue
        st = pd.DataFrame(r.json().get("stations", []))
        if st.empty:
            continue
        frames.append(pd.DataFrame({
            "source": SOURCE_NAME,
            "station_id": st["id"].astype(str),
            "name": st["name"],
            "lat": st["lat"].astype(float),
            "lon": st["lng"].astype(float),
            "status": status,
        }))
    if not frames:
        return empty_stations()

    df = in_bbox(pd.concat(frames, ignore_index=True))
    # a station can appear in both lists; keep the 'active' row
    df = df.sort_values("status").drop_duplicates("station_id").reset_index(drop=True)

    if with_dates:
        dates = df["station_id"].map(_station_details)
        df["start"] = [d[0] for d in dates]
        df["end"] = [d[1] if s == "historical" else pd.NaT for d, s in zip(dates, df["status"])]
    else:
        df["start"], df["end"] = pd.NaT, pd.NaT
    return df[STATION_COLUMNS]


# --------------------------------------------------------------------------- #
# Time series
# --------------------------------------------------------------------------- #
def _fetch_chunk(station_id, product, begin, end, datum) -> pd.DataFrame:
    params = {
        "station": station_id,
        "product": product,
        "begin_date": begin.strftime("%Y%m%d"),
        "end_date": end.strftime("%Y%m%d"),
        "units": "metric",
        "time_zone": "gmt",
        "format": "json",
        "application": APP_ID,
    }
    if product in ("water_level", "hourly_height", "high_low", "monthly_mean", "predictions"):
        params["datum"] = datum
    if product == "predictions":
        params["interval"] = "h"

    r = requests.get(DATAAPI, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    js = r.json()
    if "error" in js:  # e.g. "No data was found" for a gap in the record
        return pd.DataFrame()

    rows = js.get("data") or js.get("predictions") or []
    if not rows:
        return pd.DataFrame()
    raw = pd.DataFrame(rows)

    if product == "monthly_mean":
        t = pd.to_datetime(raw["year"].astype(str) + "-" + raw["month"].astype(str) + "-01", utc=True)
        out = pd.DataFrame({"msl_m": pd.to_numeric(raw["MSL"], errors="coerce").to_numpy()}, index=t)
    elif product == "wind":
        t = pd.to_datetime(raw["t"], utc=True)
        out = pd.DataFrame({
            "wind_speed_ms": pd.to_numeric(raw["s"], errors="coerce").to_numpy(),
            "wind_dir_deg": pd.to_numeric(raw["d"], errors="coerce").to_numpy(),
            "wind_gust_ms": pd.to_numeric(raw["g"], errors="coerce").to_numpy(),
        }, index=t)
    elif product == "air_pressure":
        t = pd.to_datetime(raw["t"], utc=True)
        out = pd.DataFrame({"pressure_mb": pd.to_numeric(raw["v"], errors="coerce").to_numpy()}, index=t)
    elif product == "predictions":
        t = pd.to_datetime(raw["t"], utc=True)
        out = pd.DataFrame({"tide_m": pd.to_numeric(raw["v"], errors="coerce").to_numpy()}, index=t)
    else:
        t = pd.to_datetime(raw["t"], utc=True)
        out = pd.DataFrame({"water_level_m": pd.to_numeric(raw["v"], errors="coerce").to_numpy()}, index=t)
    return out


def get_data(station_id: str, start, end, product: str = "hourly_height",
             datum: str = "NAVD", progress=None, **_) -> pd.DataFrame:
    """Download a product for any period, chunking to respect NOAA limits.

    progress: optional callable(fraction: float) for a Streamlit progress bar.
    Note: not every station has NAVD; if the result is empty try datum="MSL" or "STND".
    """
    max_days = PRODUCTS[product]["max_days"]
    chunks = list(date_chunks(start, end, max_days))
    parts = []
    for i, (b, e) in enumerate(chunks):
        try:
            parts.append(_fetch_chunk(station_id, product, b, e, datum))
        except requests.RequestException:
            pass  # skip a failed chunk rather than lose the whole request
        if progress:
            progress((i + 1) / len(chunks))

    parts = [p for p in parts if not p.empty]
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts).sort_index()
    df = df[~df.index.duplicated()]
    df.index.name = "time"
    return df


if __name__ == "__main__":  # quick manual test:  python -m loaders.noaa_coops
    s = list_stations(with_dates=False)
    print(s)
    print(get_data("8656483", "2024-01-01", "2024-01-03"))  # Beaufort, NC
