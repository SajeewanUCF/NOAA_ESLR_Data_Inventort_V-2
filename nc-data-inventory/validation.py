"""Model-vs-observation comparison utilities (used by the Validation tab)."""

from __future__ import annotations

import numpy as np
import pandas as pd

LABELS = {
    "water_level_m": "Water level (m)", "surge_m": "Surge residual (m)", "tide_m": "Tide (m)",
    "hs_m": "Significant wave height Hs (m)", "tp_s": "Peak period Tp (s)", "tm_s": "Mean period Tm (s)",
    "wave_dir_deg": "Wave direction (°)", "wind_speed_ms": "Wind speed (m/s)",
    "wind_dir_deg": "Wind direction (°)", "wind_gust_ms": "Wind gust (m/s)",
    "u10_ms": "U wind (m/s)", "v10_ms": "V wind (m/s)", "pressure_mb": "Pressure (hPa)",
    "air_temp_c": "Air temperature (°C)", "water_temp_c": "Water temperature (°C)",
    "precip_mm": "Precipitation (mm)", "discharge_m3s": "Discharge (m³/s)",
    "gage_height_m": "Gage height (m)",
}


def label(col: str) -> str:
    return LABELS.get(col, col)


def is_direction(col: str) -> bool:
    return col.endswith("_deg")


def align(obs: pd.Series, mod: pd.Series, freq: str = "1h", demean: bool = False) -> pd.DataFrame:
    """Resample both series to a common step, keep overlapping times.

    Directions are averaged as vectors. demean=True removes each series' own
    mean (useful when observed and model water levels use different datums).
    """
    def rs(s: pd.Series) -> pd.Series:
        if is_direction(str(s.name)):
            r = np.radians(s)
            c, d = np.cos(r).resample(freq).mean(), np.sin(r).resample(freq).mean()
            return np.degrees(np.arctan2(d, c)) % 360
        return s.resample(freq).mean()

    df = pd.concat([rs(obs).rename("obs"), rs(mod).rename("model")], axis=1).dropna()
    if demean and not df.empty:
        df = df - df.mean()
    return df


def _err(df: pd.DataFrame, circular: bool) -> np.ndarray:
    e = df["model"].to_numpy() - df["obs"].to_numpy()
    return ((e + 180) % 360) - 180 if circular else e


def metrics(df: pd.DataFrame, circular: bool = False) -> dict:
    """Skill metrics on an aligned frame with columns 'obs' and 'model'."""
    if len(df) < 2:
        return {}
    o, m = df["obs"].to_numpy(), df["model"].to_numpy()
    err = _err(df, circular)
    rmse = float(np.sqrt(np.mean(err ** 2)))
    out = {"N": int(len(o)), "Bias": float(np.mean(err)), "RMSE": rmse,
           "MAE": float(np.mean(np.abs(err)))}
    if circular:
        return out
    denom = np.sum((np.abs(m - o.mean()) + np.abs(o - o.mean())) ** 2)
    r = float(np.corrcoef(o, m)[0, 1]) if o.std() > 0 and m.std() > 0 else np.nan
    out.update({
        "R": r,
        "Scatter index": rmse / float(np.mean(np.abs(o))) if np.mean(np.abs(o)) else np.nan,
        "Willmott d": float(1 - np.sum(err ** 2) / denom) if denom else np.nan,
        "Peak obs": float(o.max()), "Peak model": float(m.max()),
    })
    return out
