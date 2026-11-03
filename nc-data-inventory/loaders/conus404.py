"""CONUS404 — 4 km WRF regional hydroclimate reanalysis (USGS/NCAR).

Source: USGS HyTEST cloud Zarr stores on the Open Storage Network (public):
    s3://hytest/conus404/conus404_hourly.zarr   (endpoint https://usgs.osn.mghpcc.org/)
    s3://hytest/conus404/conus404_daily.zarr
Coverage: water years 1980-2024 (1979-10-01 -> 2024-09-30).

WRF writes U10/V10 relative to its Lambert grid; they are rotated to
earth-relative using COSALPHA/SINALPHA when those fields are in the store.
Point values come from the grid cell nearest the requested point.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd
import xarray as xr

from config import NC_BBOX
from loaders.base import empty_stations, finish, haversine_km, nearest, parse_point, wind_from_uv

SOURCE_NAME = "CONUS404"
KIND = "gridded"
COVERAGE = {"start": "1979-10-01", "end": "2024-09-30", "resolution": "4 km WRF grid, hourly/daily",
            "note": "10 m wind, 2 m temperature, surface pressure, precipitation"}

OSN = {"anon": True, "client_kwargs": {"endpoint_url": "https://usgs.osn.mghpcc.org/"}}
STORES = {"hourly": "s3://hytest/conus404/conus404_hourly.zarr",
          "daily": "s3://hytest/conus404/conus404_daily.zarr"}

PRODUCTS = {
    "wind_hourly":   {"label": "10 m wind (hourly)",                 "store": "hourly", "vars": ["U10", "V10"]},
    "met_hourly":    {"label": "T2, surface pressure (hourly)",      "store": "hourly", "vars": ["T2", "PSFC"]},
    "precip_hourly": {"label": "Precipitation (hourly)",             "store": "hourly", "vars": ["PREC_ACC_NC"]},
    "daily":         {"label": "Daily wind / T2 / precip",           "store": "daily",
                      "vars": ["U10", "V10", "T2", "PSFC", "PREC_ACC_NC"]},
}
OPTIONS: dict = {}


@lru_cache(maxsize=2)
def _open(store: str) -> xr.Dataset:
    return xr.open_zarr(STORES[store], storage_options=OSN, chunks={})


@lru_cache(maxsize=2)
def _nc_grid(store: str):
    """(y, x) indices and lat/lon of grid cells inside the NC box (loaded once)."""
    ds = _open(store)
    lat = ds[next(n for n in ("lat", "XLAT", "latitude") if n in ds.variables)]
    lon = ds[next(n for n in ("lon", "XLONG", "longitude") if n in ds.variables)]
    if "time" in lat.dims:
        lat, lon = lat.isel(time=0), lon.isel(time=0)
    lat, lon = lat.values, lon.values
    lon0, lat0, lon1, lat1 = NC_BBOX
    yy, xx = np.where((lon >= lon0 - 0.5) & (lon <= lon1 + 0.5) & (lat >= lat0 - 0.5) & (lat <= lat1 + 0.5))
    ydim, xdim = ds["T2"].dims[-2:] if "T2" in ds else ("y", "x")
    return yy, xx, lat[yy, xx], lon[yy, xx], ydim, xdim


def list_stations():
    return empty_stations()


def get_data(station_id: str, start, end, product: str = "wind_hourly", progress=None, **_) -> pd.DataFrame:
    p = PRODUCTS[product]
    lat, lon = parse_point(station_id)
    ds = _open(p["store"])
    yy, xx, glat, glon, ydim, xdim = _nc_grid(p["store"])
    k, dist = nearest(lat, lon, glat, glon)
    cell = {ydim: int(yy[k]), xdim: int(xx[k])}
    t = slice(str(pd.Timestamp(start).date()), str((pd.Timestamp(end) + pd.Timedelta(days=1)).date()))

    names = [v for v in p["vars"] if v in ds]
    extra = [v for v in ("COSALPHA", "SINALPHA") if v in ds and {"U10", "V10"} & set(names)]
    pt = ds[names + extra].isel(cell)
    pt = pt.sel(time=t) if "time" in pt.dims else pt
    pt = pt.load()
    if progress:
        progress(1.0)

    out = pd.DataFrame(index=pd.to_datetime(pt["time"].values))
    if "U10" in pt and "V10" in pt:
        u, v = pt["U10"].values, pt["V10"].values
        if len(extra) == 2:  # grid-relative -> earth-relative
            ca, sa = np.ravel(pt["COSALPHA"].values)[0], np.ravel(pt["SINALPHA"].values)[0]
            u, v = u * ca - v * sa, v * ca + u * sa
        out["u10_ms"], out["v10_ms"] = u, v
        out["wind_speed_ms"], out["wind_dir_deg"] = wind_from_uv(u, v)
    if "T2" in pt:
        out["air_temp_c"] = pt["T2"].values - 273.15
    if "PSFC" in pt:
        out["pressure_mb"] = pt["PSFC"].values / 100.0   # surface (not MSL) pressure
    if "PREC_ACC_NC" in pt:
        out["precip_mm"] = pt["PREC_ACC_NC"].values
    out = finish(out, start, end)
    out.attrs["model_location"] = (float(glat[k]), float(glon[k]))
    out.attrs["note"] = f"CONUS404 cell {cell}, {dist:.2f} km from requested point"
    return out


def map_layer(lat: float, lon: float):
    """Outline of the 4-km CONUS404 cell nearest the point (and its neighbours)."""
    import folium
    yy, xx, glat, glon, *_ = _nc_grid("daily")
    k, dist = nearest(lat, lon, glat, glon)
    fg = folium.FeatureGroup(name="CONUS404 grid cell")
    d = haversine_km(lat, lon, glat, glon)
    for j in np.argsort(d)[:9]:
        h_lat, h_lon = 0.018, 0.018 / np.cos(np.radians(glat[j]))       # ~2 km half-width
        folium.Rectangle([(glat[j] - h_lat, glon[j] - h_lon), (glat[j] + h_lat, glon[j] + h_lon)],
                         color="#bcbd22", weight=2 if j == k else 0.7, fill=j == k, fill_opacity=0.15,
                         interactive=False).add_to(fg)
    return fg, f"CONUS404 cell centred at {glat[k]:.3f}, {glon[k]:.3f}, {dist:.1f} km from the point."


if __name__ == "__main__":
    print(get_data("35.2,-75.6", "2018-09-13", "2018-09-14"))
