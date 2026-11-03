"""Site-by-site data inventory (the model behind the Availability tab).

For every RCCP study site: the nearest stations of each observed type, their
distance and record length, a Good / Fair / Poor rating (rules of the
nc_data_inventory_V1 page), and the same for every model / hindcast source.

Rating rules (V1)
    distance limits good / fair: water level 10 / 30 km, waves 25 / 60 km,
                                 wind 15 / 40 km, river flow 15 / 40 km
    record < 10 yr (< 20 yr for water level, the minimum for annual-maxima EVA) -> at most fair
    open-ocean waves at sheltered (sound / estuary / river) sites -> at most fair
    river flow is rated only at sites flagged river_relevant (else "not needed")
    overall = mean of the rated categories, rounded half up
Models use the same limits for the quantity they provide. WIS and GTSM output
points sit on the open coast, so they are capped at fair at sheltered sites;
CORA's ~500 m mesh resolves the sounds, so it is not capped.
"Overall incl. models" takes, per category, the better of observation and model.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd

import loaders
from loaders.base import haversine_km
from sites import load_sites

LIMITS = {"wl": (10, 30), "wave": (25, 60), "wind": (15, 40), "river": (15, 40)}
MIN_YEARS = {"wl": 20, "wave": 10, "wind": 10, "river": 10}
OBS_KINDS = [("wl", "Water level"), ("wave", "Waves"), ("wind", "Wind / met"), ("river", "River flow")]
MODEL_COLS = [("wis", "USACE WIS", "wave"), ("gtsm", "CoDEC / GTSM", "wl"), ("cora", "NOAA CORA", "wl"),
              ("era5", "ERA5", "wind"), ("conus404", "CONUS404", "wind")]
NA = -1
MODEL_ROW_LABELS = {"wis": "USACE WIS · waves", "codec_gtsm": "CoDEC / GTSM-ERA5 · tide + surge",
                    "cora": "NOAA CORA · water level + waves", "era5": "ERA5 · wind, MSLP",
                    "conus404": "CONUS404 · climate (4 km WRF)"}


def _yr(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    t = pd.Timestamp(v)
    return None if pd.isna(t) else t.year + (t.dayofyear - 1) / 365.25


def _now_year() -> float:
    d = dt.date.today()
    return d.year + (d.timetuple().tm_yday - 1) / 365.25


def _record_txt(s, e, now):
    if s is None:
        return "?"
    return f"{int(s)}–" + ("present" if e is None or e >= now - 0.2 else f"{int(e)}")


# --------------------------------------------------------------------------- #
# Station categories from the loaders' inventories
# --------------------------------------------------------------------------- #
def station_categories(get_stations, include_usgs_wl: bool = True) -> dict[str, pd.DataFrame]:
    """category -> DataFrame(id, name, lat, lon, s, e, years, record, src)."""
    now = _now_year()
    cats = {}

    def mk(df, s_col, e_col, src, name_fmt="{name}"):
        if df is None or df.empty or s_col not in df:
            return pd.DataFrame(columns=["id", "name", "lat", "lon", "s", "e", "years", "record", "src"])
        d = df[df[s_col].notna()].copy()
        s = d[s_col].map(_yr)
        # no end date (active station) or a value in the last 60 days -> runs to today
        e = [now if (_yr(v) is None or _yr(v) > now - 60 / 365) else _yr(v) for v in d[e_col]]
        out = pd.DataFrame({"id": d["station_id"].astype(str), "name": d["name"].astype(str),
                            "lat": d["lat"].astype(float), "lon": d["lon"].astype(float),
                            "s": s.to_numpy(), "e": e, "src": src})
        out = out.dropna(subset=["s"])
        out["years"] = (out["e"] - out["s"]).clip(lower=0).round(0).astype(int)
        out["record"] = [_record_txt(a, b, now) for a, b in zip(out["s"], out["e"])]
        return out.reset_index(drop=True)

    noaa = get_stations("noaa_coops")
    ndbc = get_stations("ndbc")
    usgs = get_stations("usgs_nwis")
    cats["tide"] = mk(noaa, "start", "end", "NOAA CO-OPS")
    cats["usgs_wl"] = mk(usgs, "wl_start", "wl_end", "USGS") if include_usgs_wl else mk(None, "", "", "")
    cats["wave"] = mk(ndbc, "wave_start", "wave_end", "NDBC")
    cats["wind"] = mk(ndbc, "wind_start", "wind_end", "NDBC")
    cats["river"] = mk(usgs, "q_start", "q_end", "USGS")
    cats["wis"] = mk(get_stations("wis"), "start", "end", "USACE WIS")
    cats["gtsm"] = mk(get_stations("codec_gtsm"), "start", "end", "CoDEC/GTSM")
    return cats


# --------------------------------------------------------------------------- #
# Ratings
# --------------------------------------------------------------------------- #
def score(kind: str, km: float, years: float, sheltered: bool, cap_sheltered: bool) -> int:
    good, fair = LIMITS[kind]
    s = 3 if km <= good else 2 if km <= fair else 1
    if years < MIN_YEARS[kind]:
        s = min(s, 2)
    if cap_sheltered and sheltered:
        s = min(s, 2)
    return s


def overall(scores) -> int:
    vals = [v for v in scores if v and v > 0]
    return int(math.floor(np.mean(vals) + 0.5)) if vals else 0


def _nearest(cat: pd.DataFrame, lat, lon, n=3) -> list[dict]:
    if cat is None or cat.empty:
        return []
    d = haversine_km(lat, lon, cat["lat"].to_numpy(), cat["lon"].to_numpy())
    idx = np.argsort(d)[:n]
    out = []
    for i in idx:
        r = cat.iloc[i]
        out.append({"id": r["id"], "label": f"{r['id']} – {r['name']}", "name": r["name"], "km": round(float(d[i]), 1),
                    "years": int(r["years"]), "start": int(r["s"]), "record": r["record"],
                    "lat": float(r["lat"]), "lon": float(r["lon"]), "src": r["src"]})
    return out


def build(get_stations, include_usgs_wl=True, overlap=None, min_active=1) -> dict:
    now = _now_year()
    cats = station_categories(get_stations, include_usgs_wl)
    wl_cat = pd.concat([cats["tide"], cats["usgs_wl"]], ignore_index=True)

    cora_nodes, cora_mesh = None, None
    try:
        from loaders import cora
        for mesh in ("native", "shoreline"):
            if cora.NODES_FILE[mesh].exists():
                cora_nodes, cora_mesh = cora.nodes(mesh), mesh
                break
    except Exception:
        pass

    results = []
    for site in load_sites().itertuples():
        r = {"num": int(site.num), "name": site.name, "title": site.title, "lat": site.lat, "lon": site.lon,
             "setting": site.setting, "note": site.note, "sheltered": bool(site.sheltered),
             "river_relevant": bool(site.river_relevant)}
        near = {"wl": _nearest(wl_cat, site.lat, site.lon), "wave": _nearest(cats["wave"], site.lat, site.lon),
                "wind": _nearest(cats["wind"], site.lat, site.lon), "river": _nearest(cats["river"], site.lat, site.lon)}
        sc = {}
        for k, _ in OBS_KINDS:
            r[k] = near[k]
            if not near[k]:
                sc[k] = NA
            elif k == "river" and not site.river_relevant:
                sc[k] = 0
            else:
                sc[k] = score(k, near[k][0]["km"], near[k][0]["years"], site.sheltered, cap_sheltered=(k == "wave"))
        # tide gauge usable for EVA: >= 20 yr within 40 km
        eva = [s for s in _nearest(wl_cat, site.lat, site.lon, n=10) if s["years"] >= 20 and s["km"] <= 40]
        r["tide_eva"] = eva[0] if eva else None

        # ---- models
        m = {}
        for key, cat_key, kind in (("wis", "wis", "wave"), ("gtsm", "gtsm", "wl")):
            nn = _nearest(cats[cat_key], site.lat, site.lon, n=1)
            if nn:
                p = nn[0]
                m[key] = {"id": p["id"], "km": p["km"], "lat": p["lat"], "lon": p["lon"], "txt": p["id"],
                          "score": score(kind, p["km"], 99, site.sheltered, cap_sheltered=True)}
            else:
                m[key] = {"score": NA, "txt": "n/a",
                          "why": "station list not built" if key == "gtsm" else "station list unavailable"}
        if cora_nodes is not None and len(cora_nodes):
            d = haversine_km(site.lat, site.lon, cora_nodes["lat"].to_numpy(), cora_nodes["lon"].to_numpy())
            i = int(np.argmin(d))
            nd = cora_nodes.iloc[i]
            m["cora"] = {"id": f"node {int(nd['node'])}", "km": round(float(d[i]), 2), "lat": float(nd["lat"]),
                         "lon": float(nd["lon"]), "txt": f"node {int(nd['node'])}", "mesh": cora_mesh,
                         "score": score("wl", float(d[i]), 99, site.sheltered, cap_sheltered=False)}
        else:
            m["cora"] = {"score": NA, "txt": "n/a", "why": "mesh nodes not cached (python -m loaders.cora --build)"}
        clat, clon = round(site.lat * 4) / 4, round(site.lon * 4) / 4
        km = float(haversine_km(site.lat, site.lon, clat, clon))
        m["era5"] = {"id": f"{clat:.2f}, {clon:.2f}", "km": round(km, 1), "lat": clat, "lon": clon,
                     "txt": "0.25° cell", "score": score("wind", km, 99, False, False)}
        m["conus404"] = {"id": "4 km cell", "km": 2.0, "txt": "4 km cell", "lat": None, "lon": None,
                         "score": score("wind", 2.0, 99, False, False)}
        r["models"] = m

        obs_scores = [sc["wl"], sc["wave"], sc["wind"]] + ([sc["river"]] if site.river_relevant else [])
        best = {
            "wl": max(sc["wl"], m["gtsm"]["score"], m["cora"]["score"]),
            "wave": max(sc["wave"], m["wis"]["score"], m["cora"]["score"]),   # CORA includes SWAN waves
            "wind": max(sc["wind"], m["era5"]["score"], m["conus404"]["score"]),
            "river": sc["river"],
        }
        r.update({f"{k}_score": v for k, v in sc.items()})
        r["overall"] = overall(obs_scores)
        r["overall_all"] = overall([best["wl"], best["wave"], best["wind"]]
                                   + ([best["river"]] if site.river_relevant else []))
        r["best"] = best
        results.append(r)

    # ---- stations drawn on the map
    serving = {s["id"] for r in results for k, _ in OBS_KINDS for s in r[k]}
    def pts(cat, kind):
        return [{"id": x.id, "name": x.name, "lat": x.lat, "lon": x.lon, "record": x.record,
                 "years": int(x.years), "src": x.src, "kind": kind, "key": x.id in serving}
                for x in cat.itertuples()]
    wave_ids = set(cats["wave"]["id"])
    stations = {
        "tide": pts(cats["tide"], "tide"),
        "usgs_wl": pts(cats["usgs_wl"], "usgs_wl"),
        "wave": pts(cats["wave"], "wave"),
        "met": pts(cats["wind"][~cats["wind"]["id"].isin(wave_ids)], "met"),
        "river": pts(cats["river"][~cats["river"]["id"].isin(set(cats["usgs_wl"]["id"]))], "river"),
        "wis": pts(cats["wis"], "wis"),
        "gtsm": pts(cats["gtsm"], "gtsm"),
    }

    # ---- coverage chart data
    y_now = int(now)
    def counts(cat):
        if cat.empty:
            return {}
        out = {}
        for y in range(int(cat["s"].min()), y_now + 1):
            c = int(((cat["s"] < y + 1) & (cat["e"] >= y)).sum())
            if c:
                out[y] = c
        return out
    groups = []
    for key, label, col, note in (("tide", "Tide gauges · NOAA", "tide", ""),
                                  ("usgs_wl", "Water level · USGS (NAVD88)", "usgswl", ""),
                                  ("wave", "Wave buoys · NDBC", "wave", ""),
                                  ("wind", "Wind / met · NDBC", "met", ""),
                                  ("river", "Stream gauges · USGS (discharge)", "river", "")):
        c = counts(cats[key])
        if c:
            groups.append({"key": key, "label": f"{label} ({len(cats[key])})", "n": len(cats[key]),
                           "counts": c, "col": col})
    models = []
    for key, mod in loaders.MODEL.items():
        cov = mod.COVERAGE
        res = cov["resolution"].split(",")[0]
        s0 = _yr(cov["start"])
        e0 = _yr(cov["end"]) + 1 / 365.25 if cov.get("end") else now
        core = cov.get("core")
        m = {"label": MODEL_ROW_LABELS.get(key, mod.SOURCE_NAME),
             "r": [s0, e0], "txt": f"{cov['start'][:7]} → {cov['end'][:7] if cov.get('end') else 'present'} · {res}"}
        if core:
            m["ext"] = [s0, e0]
            m["r"] = [_yr(core[0]), _yr(core[1]) + 1 / 365.25]
            m["txt"] = (f"{core[0][:4]}–{core[1][:4]} (ext. {cov['start'][:4]}–"
                        f"{cov['end'][:4] if cov.get('end') else 'present'}) · {res}")
        models.append(m)

    auto = None
    years = set(range(1800, y_now + 1))
    for m in models:
        years &= set(range(math.ceil(m["r"][0] - 0.01), math.floor(m["r"][1] + 0.01)))
    for g in groups:
        years &= {y for y, c in g["counts"].items() if c >= min_active}
    if years:
        auto = [min(years), max(years)]

    # ---- water-level record timeline (all NOAA + USGS WL gauges serving a site)
    wl_served = {}
    for r in results:
        if r["wl"]:
            wl_served.setdefault(r["wl"][0]["id"], []).append(r["num"])
    timeline = []
    for x in wl_cat.itertuples():
        if x.src == "USGS" and x.id not in wl_served:
            continue
        timeline.append({"id": x.id, "name": f"{x.name} ({x.id})" if x.src != "USGS" else f"USGS {x.id} {x.name[:28]}",
                         "s": float(x.s), "e": float(x.e), "years": int(x.years), "src": x.src,
                         "served": wl_served.get(x.id, [])})
    timeline.sort(key=lambda t: -t["years"])

    return {"results": results, "stations": stations, "groups": groups, "models": models,
            "overlap": overlap or auto, "overlap_auto": auto, "now": now,
            "timeline": timeline, "cora_mesh": cora_mesh, "include_usgs_wl": include_usgs_wl}


def matrix_frame(data: dict) -> pd.DataFrame:
    """Flat table of the site x data matrix (for CSV download)."""
    lbl = {3: "Good", 2: "Fair", 1: "Poor", 0: "Not needed", NA: "n/a"}
    rows = []
    for r in data["results"]:
        row = {"site": f"{r['num']}. {r['name']}"}
        for k, name in OBS_KINDS:
            s = r[k][0] if r[k] else None
            row[f"{name} station"] = s["id"] if s else ""
            row[f"{name} km"] = s["km"] if s else None
            row[f"{name} years"] = s["years"] if s else None
            row[f"{name} rating"] = lbl[r[f"{k}_score"]]
        row["Overall (obs)"] = lbl[r["overall"]]
        for key, name, _ in MODEL_COLS:
            mm = r["models"][key]
            row[f"{name}"] = f"{mm.get('txt', '')} {mm.get('km', '')} km".strip() if mm["score"] != NA else "n/a"
            row[f"{name} rating"] = lbl[mm["score"]]
        row["Overall incl. models"] = lbl[r["overall_all"]]
        rows.append(row)
    return pd.DataFrame(rows)
