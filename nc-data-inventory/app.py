"""North Carolina Coastal Data Inventory — Streamlit dashboard.

Run locally:   streamlit run app.py
"""

from __future__ import annotations

import datetime as dt
import os

import folium
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from streamlit_folium import st_folium

import config
import loaders
from loaders.base import haversine_km
from availability import availability_tab
from sites import add_sites_layer, get_site, site_options, sort_by_site
from validation import align, is_direction, label, metrics

st.set_page_config(page_title=config.APP_TITLE, page_icon="🌊", layout="wide")

# API keys: Streamlit secrets -> environment (loaders read os.environ)
for _k in ("CDSAPI_KEY", "API_USGS_PAT"):
    try:
        if _k in st.secrets and not os.environ.get(_k):
            os.environ[_k] = str(st.secrets[_k])
    except Exception:  # no secrets.toml
        pass

TODAY = dt.date.today()


# --------------------------------------------------------------------------- #
# Cached wrappers around the loaders
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=24 * 3600, show_spinner="Loading station list…")
def cached_stations(key: str) -> pd.DataFrame:
    df = loaders.ALL[key].list_stations()
    df = df.copy()
    df["key"] = key
    return df


def station_list(key: str, warn: bool = True) -> pd.DataFrame:
    """Station list of one source; a failing source gives a warning, not a broken page.
    Failures are not cached, so the next rerun tries again."""
    try:
        df = cached_stations(key)
        mod = loaders.ALL[key]
        status_fn = getattr(mod, "status", None)
        if warn and df.empty and mod.KIND == "point" and not (status_fn and status_fn()):
            st.warning(f"{mod.SOURCE_NAME}: the server returned no stations for the NC region. "
                       "Use 'Clear cached data' in the sidebar to retry.")
        return df
    except Exception as e:
        if warn:
            st.warning(f"{loaders.ALL[key].SOURCE_NAME}: station list could not be loaded "
                       f"({type(e).__name__}: {str(e)[:200]}). Other sources are unaffected; "
                       "use 'Clear cached data' in the sidebar to retry.")
        return pd.DataFrame(columns=["source", "station_id", "name", "lat", "lon", "start", "end",
                                     "status", "key"])


@st.cache_data(ttl=12 * 3600, show_spinner=False, max_entries=64)
def cached_data(key: str, station_id: str, start, end, product: str, opts: tuple) -> pd.DataFrame:
    return loaders.ALL[key].get_data(station_id, start, end, product, **dict(opts))


def fetch(key, station_id, start, end, product, opts) -> pd.DataFrame | None:
    """cached_data with a spinner and a readable error instead of a traceback."""
    mod = loaders.ALL[key]
    with st.spinner(f"Downloading {mod.SOURCE_NAME} … (long periods can take minutes)"):
        try:
            return cached_data(key, str(station_id), pd.Timestamp(start), pd.Timestamp(end),
                               product, tuple(sorted(opts.items())))
        except Exception as e:  # network / API / key problems
            st.error(f"{mod.SOURCE_NAME}: {e}")
            return None


def coverage_dates(mod, row=None) -> tuple[dt.date, dt.date]:
    if row is not None and pd.notna(row.get("start")):
        s = row["start"].date()
    else:
        s = pd.Timestamp(mod.COVERAGE["start"]).date()
    if row is not None and pd.notna(row.get("end")):
        e = row["end"].date()
    elif mod.COVERAGE.get("end"):
        e = pd.Timestamp(mod.COVERAGE["end"]).date()
    else:
        e = TODAY
    return s, min(e, TODAY)


def status_messages(keys):
    for k in keys:
        fn = getattr(loaders.ALL[k], "status", None)
        msg = fn() if fn else None
        if msg:
            st.info(f"**{loaders.ALL[k].SOURCE_NAME}** — {msg}")


# --------------------------------------------------------------------------- #
# Map
# --------------------------------------------------------------------------- #
def make_map(stations: pd.DataFrame, basemap: str, point=None, overlays=(), site_label=None) -> folium.Map:
    tiles, attr = config.BASEMAPS[basemap]
    m = folium.Map(location=config.MAP_CENTER, zoom_start=config.MAP_ZOOM,
                   tiles=tiles, attr=attr, control_scale=True)
    lon0, lat0, lon1, lat1 = config.NC_BBOX
    folium.Rectangle([(lat0, lon0), (lat1, lon1)], color="#444", weight=1,
                     fill=False, dash_array="5,5", tooltip="Study region").add_to(m)

    for key, grp in stations.groupby("key"):
        mod = loaders.ALL[key]
        fg = folium.FeatureGroup(name=f"{mod.SOURCE_NAME} ({len(grp)})")
        for r in grp.itertuples():
            start = r.start.strftime("%Y-%m-%d") if pd.notna(r.start) else "?"
            end = r.end.strftime("%Y-%m-%d") if pd.notna(r.end) else "present"
            folium.CircleMarker(
                location=(r.lat, r.lon), radius=5 if r.status == "model" else 6, weight=1,
                color="black", fill=True, fill_opacity=0.9 if r.status != "historical" else 0.4,
                fill_color=loaders.COLORS.get(key, "#333"),
                tooltip=f"{key}|{r.station_id}|{r.name}",   # parsed back on click
                popup=folium.Popup(f"<b>{r.name}</b><br>{mod.SOURCE_NAME} {r.station_id}"
                                   f"<br>{start} → {end}", max_width=260),
            ).add_to(fg)
        fg.add_to(m)
    for layer in overlays:
        layer.add_to(m)
    add_sites_layer(m, highlight=site_label)
    if point is not None:
        folium.Marker(point, tooltip="Selected point",
                      icon=folium.Icon(color="red", icon="crosshairs", prefix="fa")).add_to(m)
        m.location = list(point)
    folium.LayerControl(collapsed=False).add_to(m)
    return m


def clicked_station(map_state):
    tip = (map_state or {}).get("last_object_clicked_tooltip")
    if tip and tip.count("|") >= 2:
        key, sid, _ = tip.split("|", 2)
        return key, sid
    return None


def plot_frame(df: pd.DataFrame, title: str):
    cols = list(df.columns)
    fig = make_subplots(rows=len(cols), cols=1, shared_xaxes=True, vertical_spacing=0.03)
    for i, c in enumerate(cols, start=1):
        mode = "markers" if is_direction(c) else "lines"
        fig.add_trace(go.Scattergl(x=df.index, y=df[c], mode=mode, name=label(c),
                                   marker={"size": 3}, line={"width": 1}), row=i, col=1)
        fig.update_yaxes(title_text=label(c), row=i, col=1, title_font={"size": 11})
    fig.update_layout(height=max(320, 220 * len(cols)), title=title, showlegend=False,
                      margin={"l": 60, "r": 20, "t": 50, "b": 40})
    fig.update_xaxes(title_text="Time (UTC)", row=len(cols), col=1)
    st.plotly_chart(fig, width="stretch")


# --------------------------------------------------------------------------- #
# Data tab (Observed or Model)
# --------------------------------------------------------------------------- #
def data_tab(group: str, registry: dict, basemap: str):
    labels = {k: m.SOURCE_NAME for k, m in registry.items()}
    chosen = st.multiselect("Data sources", list(registry), default=list(registry),
                            format_func=labels.get, key=f"src_{group}")
    if not chosen:
        return
    status_messages(chosen)

    point_keys = [k for k in chosen if registry[k].KIND == "point"]
    grid_keys = [k for k in chosen if registry[k].KIND == "gridded"]
    frames = [station_list(k) for k in point_keys]
    stations = (pd.concat([f for f in frames if not f.empty], ignore_index=True)
                if any(not f.empty for f in frames) else pd.DataFrame(columns=["key"]))

    modes = (["Station"] if not stations.empty else []) + (["Point on map (gridded)"] if grid_keys else [])
    if not modes:
        st.warning("No stations available for the selected sources.")
        return

    # ---- apply clicks queued on the previous run (before any widget is created)
    for k in ("site", "lat", "lon", "mode"):
        pend = st.session_state.pop(f"_pending_{k}_{group}", None)
        if pend is not None:
            st.session_state[f"{k}_{group}"] = pend

    c0, c1, c2 = st.columns([2, 2, 2])
    mode = c0.radio("Select data by", modes, horizontal=True, key=f"mode_{group}")
    site_label = c1.selectbox("Near study site", site_options(), key=f"site_{group}",
                              help="Sorts stations by distance to the site; in point mode moves the point to the site.")
    site = get_site(site_label)
    point_mode = mode.startswith("Point")
    gkey = c2.selectbox("Gridded source", grid_keys, format_func=labels.get, key=f"gsrc_{group}") if point_mode else None

    pkey_lat, pkey_lon = f"lat_{group}", f"lon_{group}"
    st.session_state.setdefault(pkey_lat, 35.20)
    st.session_state.setdefault(pkey_lon, -75.60)
    if site is not None and st.session_state.get(f"_prevsite_{group}") != site_label:
        st.session_state[f"_prevsite_{group}"] = site_label
        if point_mode:
            st.session_state[pkey_lat], st.session_state[pkey_lon] = round(site["lat"], 4), round(site["lon"], 4)
    point = (st.session_state[pkey_lat], st.session_state[pkey_lon]) if point_mode else None

    # gridded source can draw its grid / mesh around the point
    overlays, overlay_note = [], None
    if point_mode and hasattr(registry[gkey], "map_layer"):
        try:
            layer, overlay_note = registry[gkey].map_layer(*point)
            overlays = [layer] if layer is not None else []
        except Exception as e:  # never let an overlay break the page
            overlay_note = f"Grid overlay unavailable: {e}"
    if site is not None and not stations.empty:
        stations = sort_by_site(stations, site)

    col_map, col_inv = st.columns([3, 2])
    with col_map:
        state = st_folium(make_map(stations, basemap, point, overlays, site_label), height=560,
                          use_container_width=True, key=f"map_{group}",
                          returned_objects=["last_object_clicked_tooltip", "last_clicked"])
        if point_mode:
            st.caption("Click anywhere on the map (or on a mesh node / study site) to set the point. "
                       + (overlay_note or ""))
    with col_inv:
        if not stations.empty:
            st.markdown(f"**Station inventory — {len(stations)} stations**"
                        + (f" (sorted by distance to site {site['num']})" if site is not None else ""))
            show = stations.assign(
                source=stations["key"].map(labels),
                start=pd.to_datetime(stations["start"], utc=True).dt.strftime("%Y-%m-%d"),
                end=pd.to_datetime(stations["end"], utc=True).dt.strftime("%Y-%m-%d").fillna("present"),
            )
            cols = ["source", "station_id", "name", "start", "end", "status"]
            if "dist_km" in show:
                show["km"] = show["dist_km"].round(1)
                cols = ["km"] + cols
            st.dataframe(show[cols], height=330 if grid_keys else 520, hide_index=True, width="stretch")
        if grid_keys:
            st.markdown("**Gridded datasets** (whole region)")
            st.dataframe(pd.DataFrame([{
                "source": registry[k].SOURCE_NAME,
                "start": registry[k].COVERAGE["start"],
                "end": registry[k].COVERAGE.get("end") or "present",
                "resolution": registry[k].COVERAGE["resolution"],
                "variables": registry[k].COVERAGE["note"],
            } for k in grid_keys]), hide_index=True, width="stretch")

    # ---- map clicks: site pin, mesh node, station, or empty map (point mode)
    tip = (state or {}).get("last_object_clicked_tooltip")
    lc = (state or {}).get("last_clicked")
    lc_r = (round(lc["lat"], 4), round(lc["lng"], 4)) if lc else None
    new_tip = tip and tip != st.session_state.get(f"_prevtip_{group}")
    new_lc = lc_r and lc_r != st.session_state.get(f"_prevclick_{group}")
    st.session_state[f"_prevtip_{group}"], st.session_state[f"_prevclick_{group}"] = tip, lc_r
    if new_tip and tip.startswith("site|"):
        num = int(tip.split("|")[1])
        lab = next((o for o in site_options() if o.startswith(f"{num}. ")), None)
        if lab and lab != site_label:
            st.session_state[f"_pending_site_{group}"] = lab
            st.rerun()
    elif new_tip and tip.startswith("node|"):
        lat_s, lon_s = tip.split("|")[2].split(",")
        st.session_state[f"_pending_lat_{group}"] = round(float(lat_s), 5)
        st.session_state[f"_pending_lon_{group}"] = round(float(lon_s), 5)
        st.rerun()
    elif point_mode and new_lc and not new_tip:
        st.session_state[f"_pending_lat_{group}"], st.session_state[f"_pending_lon_{group}"] = lc_r
        st.rerun()

    st.divider()
    if mode == "Station":
        stations["label"] = (stations["key"].map(labels) + " · " + stations["station_id"].astype(str)
                             + " · " + stations["name"].astype(str))
        if "dist_km" in stations:
            stations["label"] = stations["label"] + " · " + stations["dist_km"].round(1).astype(str) + " km"
        hit = clicked_station(state)
        idx = 0
        if hit:
            match = stations.index[(stations["key"] == hit[0]) & (stations["station_id"].astype(str) == hit[1])]
            idx = int(match[0]) if len(match) else 0
        row = stations.loc[st.selectbox("Station (or click one on the map)", stations.index, index=idx,
                                        format_func=stations["label"].get, key=f"stn_{group}_{site_label}")]
        key, station_id, title = row["key"], row["station_id"], row["label"]
        rec_start, rec_end = coverage_dates(registry[key], row)
    else:
        key = gkey
        c1, c2 = st.columns(2)
        lat = c1.number_input("Latitude", -90.0, 90.0, step=0.001, format="%.5f", key=pkey_lat)
        lon = c2.number_input("Longitude", -180.0, 180.0, step=0.001, format="%.5f", key=pkey_lon)
        station_id = f"{lat:.5f},{lon:.5f}"
        title = f"{labels[key]} · {lat:.4f}, {lon:.4f}"
        rec_start, rec_end = coverage_dates(registry[key])

    mod = registry[key]
    c1, c2, c3, c4 = st.columns(4)
    product = c1.selectbox("Product", list(mod.PRODUCTS),
                           format_func=lambda p: mod.PRODUCTS[p]["label"], key=f"prod_{group}_{key}")
    opts = {name: c2.selectbox(name.capitalize(), choices, key=f"opt_{name}_{group}_{key}")
            for name, choices in getattr(mod, "OPTIONS", {}).items()}
    default_start = max(rec_start, rec_end - dt.timedelta(days=config.DEFAULT_WINDOW_DAYS))
    start = c3.date_input("Start", default_start, min_value=rec_start, max_value=rec_end, key=f"s_{group}_{key}")
    end = c4.date_input("End", rec_end, min_value=rec_start, max_value=rec_end, key=f"e_{group}_{key}")
    st.caption(f"Available: {rec_start} → {rec_end}. Long periods download in chunks and are cached.")

    if st.button("Load data", type="primary", key=f"go_{group}"):
        df = fetch(key, station_id, start, end, product, opts)
        if df is not None:
            st.session_state[f"df_{group}"] = (title, df)

    if f"df_{group}" in st.session_state:
        title, df = st.session_state[f"df_{group}"]
        if df.empty:
            st.warning("No data returned for this request (try another period, product or option).")
        else:
            if df.attrs.get("note"):
                st.caption(df.attrs["note"])
            plot_frame(df, title)
            st.download_button("Download CSV", df.to_csv().encode(), mime="text/csv",
                               file_name=f"{title.replace(' · ', '_').replace(', ', '_')}.csv",
                               key=f"dl_{group}")


# --------------------------------------------------------------------------- #
# Validation tab
# --------------------------------------------------------------------------- #
PAIRING_HINTS = """
| Quantity | Observed | Model / hindcast |
|---|---|---|
| Water level | NOAA CO-OPS (datum **MSL**), USGS NAVD88 elevation | CORA, CoDEC/GTSM (both rel. MSL) |
| Tide | NOAA CO-OPS *Tide predictions* | CoDEC/GTSM tide |
| Waves (Hs, Tp, Tm, dir) | NDBC | WIS, CORA |
| Wind | NDBC, NOAA CO-OPS wind | ERA5, CONUS404, WIS forcing |
"""


def validation_tab(basemap: str):
    st.markdown("Compare a model/hindcast series with an observation **at the observed station's "
                "location**: gridded models use the nearest cell/node, point models the nearest output point.")
    with st.expander("Which sources can be compared?"):
        st.markdown(PAIRING_HINTS)

    obs_labels = {k: m.SOURCE_NAME for k, m in loaders.OBSERVED.items()}
    vsite_label = st.selectbox("Study site (optional)", site_options(), key="v_site",
                               help="Sorts observed stations by distance to the site (nearest first).")
    vsite = get_site(vsite_label)
    mod_labels = {k: m.SOURCE_NAME for k, m in loaders.MODEL.items()}
    left, right = st.columns(2)

    # ---- observed side ----
    with left:
        st.subheader("Observed")
        okey = st.selectbox("Source", list(obs_labels), format_func=obs_labels.get, key="v_okey")
        omod = loaders.OBSERVED[okey]
        ost = station_list(okey)
        if ost.empty:
            st.warning("No stations available.")
            return
        ost = ost.reset_index(drop=True)
        ost = sort_by_site(ost, vsite)
        ost["label"] = ost["station_id"].astype(str) + " · " + ost["name"].astype(str)
        if "dist_km" in ost:
            ost["label"] = ost["label"] + " · " + ost["dist_km"].round(1).astype(str) + " km from site"
        orow = ost.loc[st.selectbox("Station", ost.index, format_func=ost["label"].get,
                                    key=f"v_ostn_{vsite_label}")]
        oprod = st.selectbox("Product", list(omod.PRODUCTS), key=f"v_oprod_{okey}",
                             format_func=lambda p: omod.PRODUCTS[p]["label"])
        oopts = {n: st.selectbox(n.capitalize(), c, key=f"v_oopt_{n}_{okey}",
                                 index=c.index("MSL") if "MSL" in c else 0)
                 for n, c in getattr(omod, "OPTIONS", {}).items()}

    # ---- model side ----
    with right:
        st.subheader("Model / hindcast")
        mkey = st.selectbox("Source", list(mod_labels), format_func=mod_labels.get, key="v_mkey")
        mmod = loaders.MODEL[mkey]
        status_messages([mkey])
        mprod = st.selectbox("Product", list(mmod.PRODUCTS), key=f"v_mprod_{mkey}",
                             format_func=lambda p: mmod.PRODUCTS[p]["label"])
        mopts = {n: st.selectbox(n.capitalize(), c, key=f"v_mopt_{n}_{mkey}")
                 for n, c in getattr(mmod, "OPTIONS", {}).items()}
        if mmod.KIND == "gridded":
            mstation = f"{orow['lat']:.4f},{orow['lon']:.4f}"
            st.caption(f"Model sampled at the grid cell/node nearest {orow['lat']:.3f}, {orow['lon']:.3f}.")
            mrow = None
        else:
            mst = station_list(mkey)
            if mst.empty:
                st.warning("Model output points are not available (see message above).")
                return
            mst = mst.assign(dist_km=haversine_km(orow["lat"], orow["lon"], mst["lat"], mst["lon"]))
            mst = mst.nsmallest(5, "dist_km").reset_index(drop=True)
            pick = st.selectbox("Model output point (5 nearest)", mst.index, key=f"v_mpt_{mkey}",
                                format_func=lambda i: f"{mst.at[i, 'station_id']} · {mst.at[i, 'dist_km']:.1f} km")
            mrow = mst.loc[pick]
            mstation = mrow["station_id"]

    # ---- period & options ----
    o0, o1 = coverage_dates(omod, orow)
    m0, m1 = coverage_dates(mmod, mrow)
    p0, p1 = max(o0, m0), min(o1, m1)
    if p0 > p1:
        st.error(f"No overlapping period: observed {o0}→{o1}, model {m0}→{m1}.")
        return
    c1, c2, c3, c4 = st.columns(4)
    start = c1.date_input("Start", max(p0, p1 - dt.timedelta(days=30)), min_value=p0, max_value=p1, key="v_s")
    end = c2.date_input("End", p1, min_value=p0, max_value=p1, key="v_e")
    freq = c3.selectbox("Compare at", ["1h", "3h", "6h", "1D"], key="v_freq")
    demean = c4.checkbox("Remove means (datum offset)", value=False, key="v_demean",
                         help="Subtracts each series' own mean before comparing — use when datums differ.")
    st.caption(f"Overlapping record: {p0} → {p1}")

    if st.button("Run validation", type="primary", key="v_go"):
        odf = fetch(okey, orow["station_id"], start, end, oprod, oopts)
        mdf = fetch(mkey, mstation, start, end, mprod, mopts)
        if odf is not None and mdf is not None:
            st.session_state["v_data"] = (odf, mdf, orow["label"], f"{mod_labels[mkey]} {mstation}")

    if "v_data" not in st.session_state:
        return
    odf, mdf, oname, mname = st.session_state["v_data"]
    if odf.empty or mdf.empty:
        st.warning(f"No data: observed {len(odf)} rows, model {len(mdf)} rows.")
        return
    if mdf.attrs.get("note"):
        st.caption(mdf.attrs["note"])

    common = [c for c in odf.columns if c in mdf.columns]
    v1, v2 = st.columns(2)
    ocol = v1.selectbox("Observed variable", list(odf.columns), format_func=label,
                        index=list(odf.columns).index(common[0]) if common else 0,
                        key=f"v_ocol_{'_'.join(odf.columns)}")
    mcols = list(mdf.columns)
    mcol = v2.selectbox("Model variable", mcols, format_func=label,
                        index=mcols.index(ocol) if ocol in mcols else 0,
                        key=f"v_mcol_{ocol}_{'_'.join(mcols)}")
    if ocol != mcol:
        st.warning(f"Comparing different quantities: {label(ocol)} vs {label(mcol)}.")

    circ = is_direction(ocol)
    df = align(odf[ocol], mdf[mcol], freq=freq, demean=demean and not circ)
    if len(df) < 2:
        st.error("The two series do not overlap in time.")
        return
    stats = metrics(df, circular=circ)

    cols = st.columns(len(stats))
    for c, (k, v) in zip(cols, stats.items()):
        c.metric(k, f"{v:,.0f}" if k == "N" else f"{v:.3f}")

    t1, t2 = st.columns([3, 2])
    with t1:
        fig = go.Figure()
        fig.add_trace(go.Scattergl(x=df.index, y=df["obs"], name=f"Observed — {oname}", line={"width": 1.2}))
        fig.add_trace(go.Scattergl(x=df.index, y=df["model"], name=f"Model — {mname}", line={"width": 1.2}))
        fig.update_layout(height=420, yaxis_title=label(ocol), xaxis_title="Time (UTC)",
                          legend={"orientation": "h", "y": -0.2}, margin={"t": 30})
        st.plotly_chart(fig, width="stretch")
    with t2:
        lo, hi = float(np.nanmin(df.values)), float(np.nanmax(df.values))
        fig = go.Figure()
        fig.add_trace(go.Scattergl(x=df["obs"], y=df["model"], mode="markers",
                                   marker={"size": 4, "opacity": 0.5}, name="pairs"))
        fig.add_trace(go.Scatter(x=[lo, hi], y=[lo, hi], mode="lines", name="1:1",
                                 line={"dash": "dash", "color": "gray"}))
        fig.update_layout(height=420, xaxis_title=f"Observed {label(ocol)}",
                          yaxis_title=f"Model {label(mcol)}", showlegend=False, margin={"t": 30})
        st.plotly_chart(fig, width="stretch")

    err = df["model"] - df["obs"]
    if circ:
        err = ((err + 180) % 360) - 180
    fig = go.Figure(go.Scattergl(x=df.index, y=err, line={"width": 1}, name="model − obs"))
    fig.add_hline(y=0, line_dash="dash", line_color="gray")
    fig.update_layout(height=250, yaxis_title="Model − Observed", margin={"t": 20})
    st.plotly_chart(fig, width="stretch")

    out = df.rename(columns={"obs": f"obs_{ocol}", "model": f"model_{mcol}"})
    st.download_button("Download paired series (CSV)", out.to_csv().encode(),
                       file_name="validation_pairs.csv", mime="text/csv", key="v_dl")
    st.download_button("Download metrics (CSV)", pd.Series(stats).to_csv().encode(),
                       file_name="validation_metrics.csv", mime="text/csv", key="v_dl2")


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
st.title(config.APP_TITLE)
with st.sidebar:
    st.header("Settings")
    basemap = st.radio("Basemap", list(config.BASEMAPS))
    st.caption("Region (lon/lat bbox): " + ", ".join(str(v) for v in config.NC_BBOX))
    st.markdown("**API keys**")
    st.caption(("✅" if os.environ.get("CDSAPI_KEY") else "⚠️") + " CDSAPI_KEY (CoDEC/GTSM)")
    st.caption(("✅" if os.environ.get("API_USGS_PAT") else "➖") + " API_USGS_PAT (USGS, optional)")
    if st.button("Clear cached data"):
        st.cache_data.clear()

tab_obs, tab_mod, tab_val, tab_av = st.tabs(
    ["📡 Observed data", "🖥️ Model / hindcast data", "✅ Validation", "📅 Availability"])
with tab_obs:
    data_tab("Observed", loaders.OBSERVED, basemap)
with tab_mod:
    data_tab("Model", loaders.MODEL, basemap)
with tab_val:
    validation_tab(basemap)
with tab_av:
    availability_tab(lambda k: station_list(k, warn=False))
