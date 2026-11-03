"""RCCP study sites (the 10 NC Nature-Based Solutions project sites).

Edit data/rccp_sites.csv to add, move or rename sites; every tab picks it up.
Columns:
    num, name, title, lat, lon, setting, note
    sheltered       True for sound / estuary / river sites (caps open-ocean wave ratings at fair)
    river_relevant  True where riverine / compound flooding matters (river-flow column is rated)
"""

from __future__ import annotations

from functools import lru_cache

import folium
import pandas as pd

from loaders.base import DATA_DIR, haversine_km

SITES_FILE = DATA_DIR / "rccp_sites.csv"
SITE_COLOR = "#c2453a"


@lru_cache(maxsize=1)
def load_sites() -> pd.DataFrame:
    if not SITES_FILE.exists():
        return pd.DataFrame(columns=["num", "name", "title", "lat", "lon", "setting",
                                     "sheltered", "river_relevant", "note"])
    df = pd.read_csv(SITES_FILE)
    for c in ("sheltered", "river_relevant"):
        df[c] = df[c].astype(str).str.lower().isin(["true", "1", "yes"])
    df["note"] = df["note"].fillna("")
    df["label"] = df["num"].astype(str) + ". " + df["name"]
    return df.sort_values("num").reset_index(drop=True)


def site_options() -> list[str]:
    return ["(none)"] + load_sites()["label"].tolist()


def get_site(label: str | None):
    s = load_sites()
    if not label or label == "(none)":
        return None
    hit = s[s["label"] == label]
    return None if hit.empty else hit.iloc[0]


def add_sites_layer(m: folium.Map, highlight: str | None = None) -> None:
    """Numbered red pins for every study site (own toggle in the layer control)."""
    fg = folium.FeatureGroup(name="RCCP study sites", show=True)
    for r in load_sites().itertuples():
        sel = r.label == highlight
        html = (f'<div style="width:22px;height:22px;border-radius:50%;background:{SITE_COLOR};'
                f'border:{3 if sel else 2}px solid {"#ffd400" if sel else "#fff"};color:#fff;'
                f'font:700 11px/18px sans-serif;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.4)">'
                f'{r.num}</div>')
        folium.Marker(
            (r.lat, r.lon), icon=folium.DivIcon(html=html, icon_size=(22, 22), icon_anchor=(11, 11)),
            tooltip=f"site|{r.num}|{r.name}",
            popup=folium.Popup(f"<b>{r.title}</b><br>{r.setting}", max_width=280),
        ).add_to(fg)
    fg.add_to(m)


def sort_by_site(stations: pd.DataFrame, site) -> pd.DataFrame:
    """Add dist_km to the site and sort nearest first (no-op without a site)."""
    if site is None or stations.empty:
        return stations
    out = stations.copy()
    out["dist_km"] = haversine_km(site["lat"], site["lon"], out["lat"].astype(float), out["lon"].astype(float))
    return out.sort_values("dist_km").reset_index(drop=True)
