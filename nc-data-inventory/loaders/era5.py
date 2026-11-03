"""ERA5 reanalysis — 10 m wind and mean sea-level pressure.

Source: Open-Meteo Historical Weather API (ERA5, 0.25 deg, hourly, 1940 -> ~5 days ago)
    https://archive-api.open-meteo.com/v1/archive?models=era5
Open-Meteo serves ERA5 as point time series, which is far faster than
downloading global fields from the Copernicus CDS for a single location.
No API key is needed for non-commercial use.
"""

from __future__ import annotations

import pandas as pd
import requests

from loaders.base import date_chunks, empty_stations, finish, parse_point, uv_from_wind

SOURCE_NAME = "ERA5 (wind)"
KIND = "gridded"
COVERAGE = {"start": "1940-01-01", "end": None, "resolution": "0.25° grid, hourly",
            "note": "10 m wind speed/direction/gusts, MSL pressure (via Open-Meteo)"}

URL = "https://archive-api.open-meteo.com/v1/archive"
TIMEOUT = 120
PRODUCTS = {"wind": {"label": "10 m wind + MSL pressure (hourly)"}}
OPTIONS = {"model": ["era5", "era5_seamless"]}

RENAME = {"wind_speed_10m": "wind_speed_ms", "wind_direction_10m": "wind_dir_deg",
          "wind_gusts_10m": "wind_gust_ms", "pressure_msl": "pressure_mb"}


def list_stations():
    return empty_stations()


def get_data(station_id: str, start, end, product: str = "wind", model: str = "era5",
             progress=None, **_) -> pd.DataFrame:
    lat, lon = parse_point(station_id)
    chunks = list(date_chunks(start, end, 10 * 365))
    parts, loc = [], None
    for i, (b, e) in enumerate(chunks):
        r = requests.get(URL, timeout=TIMEOUT, params={
            "latitude": lat, "longitude": lon,
            "start_date": f"{b:%Y-%m-%d}", "end_date": f"{e:%Y-%m-%d}",
            "hourly": ",".join(RENAME), "models": model,
            "wind_speed_unit": "ms", "timezone": "GMT",
        })
        r.raise_for_status()
        js = r.json()
        loc = (js.get("latitude"), js.get("longitude"))
        h = js.get("hourly", {})
        if h.get("time"):
            df = pd.DataFrame({RENAME[k]: h.get(k) for k in RENAME},
                              index=pd.to_datetime(h["time"], utc=True))
            parts.append(df)
        if progress:
            progress((i + 1) / len(chunks))
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts).apply(pd.to_numeric, errors="coerce")
    out["u10_ms"], out["v10_ms"] = uv_from_wind(out["wind_speed_ms"], out["wind_dir_deg"])
    out = finish(out)
    out.attrs["model_location"] = loc
    out.attrs["note"] = f"ERA5 grid cell centred at {loc[0]:.3f}, {loc[1]:.3f}"
    return out


def map_layer(lat: float, lon: float):
    """Outline of the 0.25° ERA5 cell that will be used for this point."""
    import folium
    clat, clon = round(lat * 4) / 4, round(lon * 4) / 4
    fg = folium.FeatureGroup(name="ERA5 grid cell")
    for dy in (-0.25, 0, 0.25):          # neighbouring cells, faint
        for dx in (-0.25, 0, 0.25):
            c = (clat + dy, clon + dx)
            folium.Rectangle([(c[0] - 0.125, c[1] - 0.125), (c[0] + 0.125, c[1] + 0.125)],
                             color="#7f7f7f", weight=2 if (dx, dy) == (0, 0) else 0.7,
                             fill=(dx, dy) == (0, 0), fill_opacity=0.12, interactive=False).add_to(fg)
    return fg, f"ERA5 cell centred at {clat:.2f}, {clon:.2f} (0.25° ≈ 28 × 23 km)."


if __name__ == "__main__":
    print(get_data("35.2,-75.5", "2018-09-13", "2018-09-15"))
