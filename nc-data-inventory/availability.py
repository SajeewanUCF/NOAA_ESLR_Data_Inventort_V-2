"""Availability tab: V1-style site inventory built from all live sources.

Layout follows nc_data_inventory_V1: summary strip, zoomable coast map with the
study sites and every station / model output point, site detail panel, the
"Nearest station, distance and record length" matrix (observations + models),
the coverage-through-time chart and the water-level record timeline.

The page is rendered as an HTML component (templates/inventory.*) and takes its
colours from the Streamlit theme (light / dark / custom).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import inventory

TEMPLATES = Path(__file__).resolve().parent / "templates"
LAND_FILE = Path(__file__).resolve().parent / "data" / "nc_land.json"

# defaults matching Streamlit's built-in themes
THEME_DEFAULTS = {
    "light": {"bg": "#ffffff", "panel": "#ffffff", "ink": "#15232c", "line": "#d5dee3", "muted": "#5b6b75"},
    "dark":  {"bg": "#0e1117", "panel": "#161b26", "ink": "#e3ecf0", "line": "#2a3443", "muted": "#93a4ad"},
}


def theme() -> tuple[str, dict]:
    """('light'|'dark', css variable overrides) from the running app's theme."""
    kind = None
    try:
        kind = st.context.theme.type
    except Exception:
        pass
    kind = kind or st.get_option("theme.base") or "light"
    kind = "dark" if str(kind).lower() == "dark" else "light"
    v = dict(THEME_DEFAULTS[kind])
    custom = {"bg": "theme.backgroundColor", "panel": "theme.secondaryBackgroundColor",
              "ink": "theme.textColor", "accent": "theme.primaryColor"}
    for k, opt in custom.items():
        try:
            val = st.get_option(opt)
        except Exception:
            val = None
        if val:
            v[k] = val
    return kind, v


def _clean(o):
    """JSON-safe copy (NaN/inf -> null, numpy -> python)."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_clean(v) for v in o]
    if hasattr(o, "item"):
        o = o.item()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


def render_html(data: dict, kind: str, vars_: dict) -> str:
    html = (TEMPLATES / "inventory.html").read_text(encoding="utf-8")
    css = (TEMPLATES / "inventory.css").read_text(encoding="utf-8")
    js = (TEMPLATES / "inventory.js").read_text(encoding="utf-8")
    land = LAND_FILE.read_text(encoding="utf-8")
    tv = "".join(f"--{k}:{v};" for k, v in vars_.items())
    payload = json.dumps(_clean(data), allow_nan=False).replace("</", "<\\/")
    return (html.replace("__CSS__", css).replace("__THEME_VARS__", tv).replace("__THEME__", kind)
                .replace("__LAND__", land).replace("__DATA__", payload).replace("__JS__", js))


def _height(data: dict) -> int:
    n_sites = len(data["results"])
    n_tl = len(data["timeline"])
    cov_h = (18 + 30 + 30 + 14 + 34 + 44 * (len(data["groups"]) + len(data["models"]))) * 1.3
    return int(260 + 920 + 120 + 70 * n_sites + 160 + cov_h + 110 + (50 + 30 * n_tl) * 1.3 + 60)


@st.cache_data(ttl=6 * 3600, show_spinner="Building site inventory from all sources…")
def _build(_get_stations, include_usgs_wl: bool, overlap: tuple | None, min_active: int, _v: int = 2):
    return inventory.build(_get_stations, include_usgs_wl=include_usgs_wl,
                           overlap=list(overlap) if overlap else None, min_active=min_active)


def availability_tab(cached_stations):
    with st.expander("Inventory settings", expanded=False):
        c1, c2, c3 = st.columns([1.2, 1, 1.4])
        inc_wl = c1.toggle("Count USGS water-surface-elevation gauges as water-level stations", value=True,
                           key="av_usgswl")
        min_active = c2.number_input("Overlap: ≥ N active stations per observed type", 1, 50, 1, key="av_min")
        auto = c3.toggle("Automatic common-overlap period", value=True, key="av_auto")
        manual = None
        if not auto:
            manual = tuple(c3.slider("Overlap period", 1950, 2026, (2008, 2018), key="av_ov"))

    data = _build(cached_stations, inc_wl, manual, int(min_active))
    if not data["results"]:
        st.warning("No study sites found — add them to data/rccp_sites.csv.")
        return
    kind, vars_ = theme()
    html = render_html(data, kind, vars_)
    if hasattr(st, "iframe"):          # Streamlit ≥ 1.5x: sizes itself to the content
        st.iframe(html, height="content")
    else:                              # older Streamlit
        components.html(html, height=_height(data), scrolling=True)

    c1, c2 = st.columns([1, 3])
    c1.download_button("Download site × data matrix (CSV)", inventory.matrix_frame(data).to_csv(index=False).encode(),
                       file_name="nc_site_data_matrix.csv", mime="text/csv", key="av_dl")
    if data.get("overlap_auto"):
        c2.caption(f"Automatic common overlap of all models and every observed type: "
                   f"{data['overlap_auto'][0]}–{data['overlap_auto'][1]}.")
