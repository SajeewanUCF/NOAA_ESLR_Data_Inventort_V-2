"""NDBC wave buoys and C-MAN / NOS met stations (standard meteorological data).

Primary source: NDBC's own servers
    station list   https://www.ndbc.noaa.gov/data/stations/station_table.txt  (all stations, incl. retired)
                   https://www.ndbc.noaa.gov/activestations.xml               (currently reporting)
    data / records https://dods.ndbc.noaa.gov/thredds/dodsC/data/stdmet/{id}/{id}.ncml
                   (OPeNDAP aggregation of every yearly stdmet file of the station)
Backup: CoastWatch ERDDAP dataset cwwcNDBCMet (used only if NDBC's servers fail).
CoastWatch reorganised that dataset in 2026 (upper-case variables, per-row station
metadata); bbox-wide queries on it became too slow, which emptied the station list.

Station records (first/last report, and whether the station ever measured waves
or wind) come from a light, strided read of the THREDDS aggregation: ~500 samples
per variable spread over the whole record, not the full series.
Note: NDBC wind is measured at anemometer height (~4-5 m on 3-m buoys), not 10 m.

    python -m loaders.ndbc --build    # pre-builds data/ndbc_stations.csv (commit it: instant start-up)
"""

from __future__ import annotations

import io
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests

from config import NC_BBOX
from loaders.base import DATA_DIR, STATION_COLUMNS, date_chunks, empty_stations, finish

SOURCE_NAME = "NDBC buoys"
KIND = "point"
COVERAGE = {"start": "1970-02-26", "end": None, "resolution": "buoys / C-MAN, 10-60 min",
            "note": "Hs, periods, wave direction, wind, pressure, temperatures"}

STATION_TABLE = "https://www.ndbc.noaa.gov/data/stations/station_table.txt"
ACTIVE_XML = "https://www.ndbc.noaa.gov/activestations.xml"
THREDDS = "https://dods.ndbc.noaa.gov/thredds/dodsC/data/stdmet/{sid}/{sid}.ncml"
ERDDAP = "https://coastwatch.pfeg.noaa.gov/erddap"
DATASET = "cwwcNDBCMet"
STATION_FILE = DATA_DIR / "ndbc_stations.csv"
TIMEOUT = 60
EXTRA = ["wave_start", "wave_end", "wind_start", "wind_end"]

# THREDDS (NDBC netCDF) variable -> standard column; ERDDAP short names for the backup path
THREDDS_VARS = {
    "wave_height": "hs_m", "dominant_wpd": "tp_s", "average_wpd": "tm_s", "mean_wave_dir": "wave_dir_deg",
    "wind_spd": "wind_speed_ms", "wind_dir": "wind_dir_deg", "gust": "wind_gust_ms",
    "air_pressure": "pressure_mb", "air_temperature": "air_temp_c", "sea_surface_temperature": "water_temp_c",
}
ERDDAP_VARS = {
    "wvht": "hs_m", "dpd": "tp_s", "apd": "tm_s", "mwd": "wave_dir_deg",
    "wspd": "wind_speed_ms", "wd": "wind_dir_deg", "gst": "wind_gust_ms",
    "bar": "pressure_mb", "atmp": "air_temp_c", "wtmp": "water_temp_c",
}
# NDBC "missing" markers that are not always declared as _FillValue
MISSING_ABOVE = {"hs_m": 90, "tp_s": 90, "tm_s": 90, "wave_dir_deg": 900, "wind_speed_ms": 90,
                 "wind_dir_deg": 900, "wind_gust_ms": 90, "pressure_mb": 9000, "air_temp_c": 900,
                 "water_temp_c": 900}
PRODUCTS = {
    "waves": {"label": "Waves (Hs, Tp, Tm, direction)", "cols": ["hs_m", "tp_s", "tm_s", "wave_dir_deg"]},
    "met":   {"label": "Wind & pressure",               "cols": ["wind_speed_ms", "wind_dir_deg", "wind_gust_ms", "pressure_mb"]},
    "temp":  {"label": "Air & water temperature",       "cols": ["air_temp_c", "water_temp_c"]},
    "all":   {"label": "All standard met variables",    "cols": list(THREDDS_VARS.values())},
}
OPTIONS: dict = {}
LAST_ERRORS: list[str] = []


def _in_box(lat, lon) -> bool:
    lon0, lat0, lon1, lat1 = NC_BBOX
    return lon0 <= lon <= lon1 and lat0 <= lat <= lat1


# --------------------------------------------------------------------------- #
# Station list from NDBC
# --------------------------------------------------------------------------- #
_LOC = re.compile(r"([\d.]+)\s*([NS])\s+([\d.]+)\s*([EW])")


def _station_table() -> pd.DataFrame:
    r = requests.get(STATION_TABLE, timeout=TIMEOUT)
    r.raise_for_status()
    rows = []
    for line in r.text.splitlines():
        if not line or line.startswith("#"):
            continue
        f = line.split("|")
        if len(f) < 7:
            continue
        m = _LOC.search(f[6])
        if not m:
            continue
        lat = float(m.group(1)) * (1 if m.group(2) == "N" else -1)
        lon = float(m.group(3)) * (1 if m.group(4) == "E" else -1)
        if _in_box(lat, lon):
            rows.append({"station_id": f[0].strip().upper(), "name": f[4].strip() or f[0].strip(),
                         "lat": lat, "lon": lon, "type": f[2].strip()})
    return pd.DataFrame(rows)


def _active() -> pd.DataFrame:
    r = requests.get(ACTIVE_XML, timeout=TIMEOUT)
    r.raise_for_status()
    rows = []
    for s in ET.fromstring(r.content).iter("station"):
        try:
            lat, lon = float(s.get("lat")), float(s.get("lon"))
        except (TypeError, ValueError):
            continue
        if _in_box(lat, lon):
            rows.append({"station_id": s.get("id", "").upper(), "name": s.get("name") or s.get("id"),
                         "lat": lat, "lon": lon, "type": s.get("type", ""), "met": s.get("met", "")})
    return pd.DataFrame(rows)


def _nc_open(url: str):
    import netCDF4
    return netCDF4.Dataset(url)


def _record(sid: str) -> dict:
    """First/last time with data, and first/last valid wave height and wind speed (strided read)."""
    out = {"station_id": sid}
    try:
        with _nc_open(THREDDS.format(sid=sid.lower())) as nc:
            t = nc.variables["time"]
            n = len(t)
            if n == 0:
                return out
            stride = max(1, n // 500)
            tt = np.asarray(t[::stride], dtype="float64")
            idx = np.r_[np.arange(0, n, stride)]
            to_ts = lambda s: pd.to_datetime(float(s), unit="s", utc=True)
            out["start"], out["end"] = to_ts(t[0]), to_ts(t[n - 1])
            for var, tag, lim in (("wave_height", "wave", 90), ("wind_spd", "wind", 90)):
                if var not in nc.variables:
                    continue
                v = np.ma.filled(np.ma.asarray(nc.variables[var][::stride, ...]).astype("float64"), np.nan)
                v = v.reshape(len(idx), -1)[:, 0]
                ok = np.isfinite(v) & (v < lim)
                if ok.any():
                    out[f"{tag}_start"], out[f"{tag}_end"] = to_ts(tt[ok][0]), to_ts(tt[ok][-1])
    except Exception as e:  # no stdmet data for this station, or server hiccup
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _from_ndbc(workers: int = 8) -> pd.DataFrame:
    frames, errors = [], []
    for fn in (_station_table, _active):
        try:
            frames.append(fn())
        except Exception as e:
            errors.append(f"{fn.__name__}: {type(e).__name__}: {e}")
    frames = [f for f in frames if not f.empty]
    if not frames:
        raise RuntimeError("NDBC station lists unavailable (" + "; ".join(errors) + ")")
    active_ids = set(frames[-1]["station_id"]) if len(frames) == 2 else set()
    st = (pd.concat(frames, ignore_index=True)
          .drop_duplicates("station_id", keep="last").reset_index(drop=True))

    with ThreadPoolExecutor(workers) as ex:
        rec = pd.DataFrame(list(ex.map(_record, st["station_id"])))
    df = st.merge(rec, on="station_id", how="left")
    have = df["start"].notna() if "start" in df else pd.Series(False, index=df.index)
    if have.any():
        df = df[have].copy()          # keep stations that have standard met / wave data
    else:  # THREDDS unreachable: still show the stations, without dates
        LAST_ERRORS.append("THREDDS records unavailable: " + str(rec.get("error", pd.Series()).dropna().head(1).tolist()))
        df["start"] = pd.NaT
        df["end"] = pd.NaT
    for c in ["start", "end"] + EXTRA:
        df[c] = pd.to_datetime(df[c], utc=True) if c in df else pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    if not df["wave_start"].notna().any() and not df["wind_start"].notna().any():
        _heuristic_vars(df)
    recent = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=60)
    df["status"] = ["active" if (sid in active_ids or (pd.notna(e) and e >= recent)) else "historical"
                    for sid, e in zip(df["station_id"], df["end"])]
    for c in ("end", "wave_end", "wind_end"):
        df.loc[(df["status"] == "active") & (df[c].isna() | (df[c] >= recent)), c] = pd.NaT
    df["source"] = SOURCE_NAME
    return df[STATION_COLUMNS + EXTRA].reset_index(drop=True)


def _heuristic_vars(df):
    """No per-variable info: numeric ids are moored buoys (waves); every station reports wind."""
    buoy = df["station_id"].str.match(r"^\d")
    df.loc[buoy, "wave_start"], df.loc[buoy, "wave_end"] = df.loc[buoy, "start"], df.loc[buoy, "end"]
    df["wind_start"], df["wind_end"] = df["start"], df["end"]


def list_stations() -> pd.DataFrame:
    """Committed CSV (fast) -> NDBC servers -> CoastWatch ERDDAP. Raises if all fail."""
    LAST_ERRORS.clear()
    if STATION_FILE.exists():
        df = pd.read_csv(STATION_FILE, dtype={"station_id": str})
        for c in ["start", "end"] + EXTRA:
            df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
        df["source"] = SOURCE_NAME
        return df[STATION_COLUMNS + EXTRA]
    try:
        df = _from_ndbc()
        if not df.empty:
            return df
        LAST_ERRORS.append("NDBC: no stations in the NC box")
    except Exception as e:
        LAST_ERRORS.append(f"NDBC: {e}")
    try:
        df = _erddap_list_stations()
        if not df.empty:
            return df
        LAST_ERRORS.append("ERDDAP: no stations returned")
    except Exception as e:
        LAST_ERRORS.append(f"ERDDAP: {type(e).__name__}: {e}")
    raise RuntimeError("no NDBC station list could be loaded — " + " | ".join(LAST_ERRORS))


def build_station_list():
    df = _from_ndbc()
    DATA_DIR.mkdir(exist_ok=True)
    df.to_csv(STATION_FILE, index=False)
    print(f"{len(df)} NDBC stations in the NC box -> {STATION_FILE}")
    print(df[["station_id", "name", "start", "end", "wave_start", "wind_start", "status"]].to_string())


def status() -> str | None:
    return None


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def _clean(out: pd.DataFrame) -> pd.DataFrame:
    for c, lim in MISSING_ABOVE.items():
        if c in out:
            out[c] = out[c].where(out[c] < lim)
    return out


def _thredds_data(station_id: str, start, end, cols) -> pd.DataFrame:
    import xarray as xr
    url = THREDDS.format(sid=station_id.lower())
    with xr.open_dataset(url) as ds:
        names = [v for v, c in THREDDS_VARS.items() if c in cols and v in ds]
        t0 = pd.Timestamp(start).strftime("%Y-%m-%d")
        t1 = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        sub = ds[names].sel(time=slice(t0, t1)).load()
    out = pd.DataFrame({THREDDS_VARS[v]: np.asarray(sub[v].values, "float64").reshape(sub.sizes["time"], -1)[:, 0]
                        for v in names}, index=pd.to_datetime(sub["time"].values))
    return out


def get_data(station_id: str, start, end, product: str = "waves", progress=None, **_) -> pd.DataFrame:
    cols = PRODUCTS[product]["cols"]
    errors = []
    try:
        out = _thredds_data(station_id, start, end, cols)
        if progress:
            progress(1.0)
        out = finish(_clean(out), start, end)
        out.attrs["note"] = "Source: NDBC THREDDS stdmet"
        return out
    except Exception as e:
        errors.append(f"THREDDS: {type(e).__name__}: {e}")
    try:
        out = _erddap_get_data(station_id, start, end, cols, progress)
        out.attrs["note"] = "Source: CoastWatch ERDDAP (NDBC THREDDS failed)"
        return out
    except Exception as e:
        errors.append(f"ERDDAP: {type(e).__name__}: {e}")
    raise RuntimeError("; ".join(errors))


# --------------------------------------------------------------------------- #
# Backup: CoastWatch ERDDAP
# --------------------------------------------------------------------------- #
def _varnames() -> dict[str, str]:
    """lower-case name -> actual ERDDAP name (ERDDAP is case-sensitive; names changed case in 2026)."""
    r = requests.get(f"{ERDDAP}/info/{DATASET}/index.csv", timeout=TIMEOUT)
    r.raise_for_status()
    info = pd.read_csv(io.StringIO(r.text))
    names = info.loc[info["Row Type"] == "variable", "Variable Name"]
    return {n.lower(): n for n in names}


def _query(variables: list[str], constraints: list[str], timeout: int = TIMEOUT) -> pd.DataFrame:
    q = ",".join(variables) + "".join("&" + c for c in constraints)
    url = f"{ERDDAP}/tabledap/{DATASET}.csv?" + quote(q, safe="&=,")
    r = requests.get(url, timeout=timeout)
    if r.status_code == 404:          # ERDDAP answers 404 when nothing matches
        return pd.DataFrame()
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text), skiprows=[1])  # row 2 = units


def _erddap_list_stations() -> pd.DataFrame:
    vm = _varnames()
    st, lon, lat = vm["station"], vm["longitude"], vm["latitude"]
    lon0, lat0, lon1, lat1 = NC_BBOX
    box = [f"{lon}>={lon0}", f"{lon}<={lon1}", f"{lat}>={lat0}", f"{lat}<={lat1}"]
    df = _query([st, lon, lat], box + ["distinct()"])
    if df.empty:
        return empty_stations()
    df = df.rename(columns={st: "station_id", lon: "lon", lat: "lat"}).groupby("station_id", as_index=False).last()
    df["station_id"] = df["station_id"].astype(str).str.upper()
    df["name"] = "NDBC " + df["station_id"]
    for c in ["start", "end"] + EXTRA:
        df[c] = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    _heuristic_vars(df)
    df["status"] = "active"
    df["source"] = SOURCE_NAME
    return df[STATION_COLUMNS + EXTRA]


def _erddap_get_data(station_id, start, end, cols, progress=None) -> pd.DataFrame:
    vm = _varnames()
    wanted = [v for v, c in ERDDAP_VARS.items() if c in cols and v in vm]
    q_cols = [vm["time"]] + [vm[v] for v in wanted]
    chunks = list(date_chunks(start, end, 5 * 365))
    parts = []
    for i, (b, e) in enumerate(chunks):
        cons = [f'{vm["station"]}="{station_id}"',
                f'{vm["time"]}>={b:%Y-%m-%d}T00:00:00Z', f'{vm["time"]}<={e:%Y-%m-%d}T23:59:59Z']
        parts.append(_query(q_cols, cons, timeout=180))
        if progress:
            progress((i + 1) / len(chunks))
    parts = [p for p in parts if not p.empty]
    if not parts:
        return pd.DataFrame()
    raw = pd.concat(parts)
    raw.index = pd.to_datetime(raw.pop(vm["time"]), utc=True)
    raw.columns = [c.lower() for c in raw.columns]
    out = raw.rename(columns=ERDDAP_VARS).apply(pd.to_numeric, errors="coerce")
    return finish(_clean(out), start, end)


if __name__ == "__main__":
    if "--build" in sys.argv:
        build_station_list()
    else:
        print(list_stations())
        print(get_data("41025", "2023-08-01", "2023-08-03"))  # Diamond Shoals
