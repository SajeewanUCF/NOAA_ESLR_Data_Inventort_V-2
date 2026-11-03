"""USGS stream and tidal gauges.

Source: USGS Water Data OGC APIs (api.waterdata.usgs.gov), which replace the
legacy NWIS "waterservices" being decommissioned. Accessed through USGS's own
`dataretrieval` package (`dataretrieval.waterdata`), which handles paging and
retries and tracks API changes.

API key (optional but recommended): anonymous users get a low hourly request
limit. Get a free key at https://api.waterdata.usgs.gov/signup/ and set it as
API_USGS_PAT (environment variable or Streamlit secret).

All values are converted to SI: ft -> m, ft^3/s -> m^3/s.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import NC_BBOX
from loaders.base import STATION_COLUMNS, date_chunks, empty_stations, finish

SOURCE_NAME = "USGS gauges"
KIND = "point"
COVERAGE = {"start": "1880-01-01", "end": None, "resolution": "stream/tidal gauges, 5-15 min",
            "note": "Gage height, NAVD88 water surface, discharge, water temperature"}

FT = 0.3048
PRODUCTS = {
    "gage_height":  {"label": "Gage height (continuous)",                      "pcode": "00065", "daily": False},
    "wse_estuary":  {"label": "Water surface elevation NAVD88, estuary (cont.)", "pcode": "62620", "daily": False},
    "wse_stream":   {"label": "Water surface elevation NAVD88, stream (cont.)",  "pcode": "63160", "daily": False},
    "discharge":    {"label": "Discharge (continuous)",                         "pcode": "00060", "daily": False},
    "discharge_dv": {"label": "Discharge (daily mean)",                         "pcode": "00060", "daily": True},
    "water_temp":   {"label": "Water temperature (continuous)",                 "pcode": "00010", "daily": False},
}
OPTIONS: dict = {}

COLNAME = {"00065": "gage_height_m", "62620": "water_level_m", "63160": "water_level_m",
           "00060": "discharge_m3s", "00010": "water_temp_c"}


def _wd():
    from dataretrieval import waterdata  # imported lazily: slow import
    return waterdata


def _xy(geom):
    """geometry is a shapely Point (geopandas installed) or [lon, lat] list."""
    if geom is None or (isinstance(geom, float) and np.isnan(geom)):
        return np.nan, np.nan
    if hasattr(geom, "x"):
        return geom.y, geom.x
    return float(geom[1]), float(geom[0])


def list_stations() -> pd.DataFrame:
    wd = _wd()
    pcodes = sorted({p["pcode"] for p in PRODUCTS.values()})
    try:
        ts, _ = wd.get_time_series_metadata(bbox=list(NC_BBOX), parameter_code=pcodes)
    except Exception:
        return empty_stations()
    if ts is None or ts.empty:
        return empty_stations()

    ts = pd.DataFrame(ts)
    ts["begin"] = pd.to_datetime(ts["begin"], utc=True, errors="coerce")
    ts["end"] = pd.to_datetime(ts["end"], utc=True, errors="coerce")
    g = ts.groupby("monitoring_location_id")
    sites = pd.DataFrame({"start": g["begin"].min(), "end": g["end"].max(),
                          "params": g["parameter_code"].agg(lambda s: ",".join(sorted(set(s))))})

    # names + coordinates
    try:
        ml, _ = wd.get_monitoring_locations(bbox=list(NC_BBOX))
        ml = pd.DataFrame(ml)
        idcol = "monitoring_location_id" if "monitoring_location_id" in ml else "id"
        ml = ml.drop_duplicates(idcol).set_index(idcol)
        lat_lon = ml["geometry"].map(_xy)
        sites["lat"] = [lat_lon.get(i, (np.nan, np.nan))[0] for i in sites.index]
        sites["lon"] = [lat_lon.get(i, (np.nan, np.nan))[1] for i in sites.index]
        sites["name"] = ml["monitoring_location_name"].reindex(sites.index)
    except Exception:
        first = ts.drop_duplicates("monitoring_location_id").set_index("monitoring_location_id")
        lat_lon = first["geometry"].map(_xy)
        sites["lat"] = [v[0] for v in lat_lon.reindex(sites.index)]
        sites["lon"] = [v[1] for v in lat_lon.reindex(sites.index)]
        sites["name"] = sites.index

    sites = sites.dropna(subset=["lat", "lon"]).reset_index(names="station_id")
    recent = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=60)
    sites["status"] = np.where(sites["end"] >= recent, "active", "historical")
    sites.loc[sites["status"] == "active", "end"] = pd.NaT
    sites["name"] = sites["name"].fillna(sites["station_id"]) + " [" + sites["params"] + "]"
    sites["source"] = SOURCE_NAME

    # per-category records: discharge (river flow) and NAVD88 water-surface elevation (water level)
    for tag, codes in (("q", ["00060"]), ("wl", ["62620", "63160"])):
        sub = ts[ts["parameter_code"].isin(codes)].groupby("monitoring_location_id")
        sites[f"{tag}_start"] = sites["station_id"].map(sub["begin"].min())
        sites[f"{tag}_end"] = sites["station_id"].map(sub["end"].max())
    sites["params"] = sites["params"]
    return sites[STATION_COLUMNS + ["params", "q_start", "q_end", "wl_start", "wl_end"]]


def _to_si(values: pd.Series, units: pd.Series) -> pd.Series:
    u = units.astype(str).str.lower()
    v = pd.to_numeric(values, errors="coerce")
    v = v.where(~u.str.startswith("ft^3") & ~u.str.contains("ft3"), v * FT ** 3)
    v = v.where(~(u == "ft"), v * FT)
    v = v.where(~u.str.contains("deg f"), (v - 32) / 1.8)
    return v


def get_data(station_id: str, start, end, product: str = "gage_height", progress=None, **_) -> pd.DataFrame:
    wd = _wd()
    p = PRODUCTS[product]
    col = COLNAME[p["pcode"]]
    # continuous endpoint returns at most 3 years per query
    chunks = list(date_chunks(start, end, 365 * 3 - 1 if not p["daily"] else 365 * 20))
    parts = []
    for i, (b, e) in enumerate(chunks):
        interval = f"{b:%Y-%m-%d}T00:00:00Z/{e:%Y-%m-%d}T23:59:59Z"
        try:
            if p["daily"]:
                df, _ = wd.get_daily(monitoring_location_id=station_id, parameter_code=p["pcode"],
                                     statistic_id="00003", time=interval, skip_geometry=True)
            else:
                df, _ = wd.get_continuous(monitoring_location_id=station_id,
                                          parameter_code=p["pcode"], time=interval)
            if df is not None and not df.empty:
                parts.append(pd.DataFrame(df))
        except Exception:
            pass
        if progress:
            progress((i + 1) / len(chunks))
    if not parts:
        return pd.DataFrame()
    raw = pd.concat(parts)
    # a site can have several series for one parameter (e.g. two sensors): keep the first
    if "time_series_id" in raw:
        raw = raw[raw["time_series_id"] == raw["time_series_id"].iloc[0]]
    units = raw["unit_of_measure"] if "unit_of_measure" in raw else pd.Series("", index=raw.index)
    out = pd.DataFrame({col: _to_si(raw["value"], units).to_numpy()},
                       index=pd.to_datetime(raw["time"], utc=True))
    return finish(out)


if __name__ == "__main__":
    s = list_stations()
    print(s.head(), len(s))
    print(get_data("USGS-02084472", "2024-01-01", "2024-01-03", "wse_estuary"))
