"""NOAA Coastal Ocean Reanalysis (CORA) — water level and waves on the ADCIRC mesh.

Source: NOAA Open Data Dissemination, public AWS bucket s3://noaa-nos-cora-pds
GEC domain (US East & Gulf coasts, Caribbean), hourly 1979-2022, ADCIRC+SWAN
unstructured triangular mesh (~500 m at the coast). Water levels relative to MSL.

Bucket layout since the 2026 restructure (README in the bucket):
    cora_gec/native_grid/water_levels/fort.63_[TIME].nc|zarr        all mesh nodes
    cora_gec/native_grid/waves/swan_HS.63_[TIME]_[map|timeseries].nc|zarr
    cora_gec/native_grid_shoreline/water_levels/.../NC.nc (+ NC_mm_*.zarr)
                                    ^ hourly water level at the mesh nodes on NC's shoreline
    CORA_intake.yml / CORA_intake_zarr3.yml                          intake catalogs
The .zarr stores are *virtual* (references into the NetCDF files).

Why several access paths
------------------------
NOAA notes that cloud access to GEC water levels on the full mesh is slow
(chunking is map-oriented). The NC *shoreline* file holds the same hourly
water levels for the nodes along NC's shoreline and is the fast path for our
study sites. Each product is opened with the first strategy that works:
    1. NC shoreline store (water level only)
    2. NOAA's intake catalog (what NOAA's own notebooks use)
    3. any *.zarr found by listing the bucket
    4. yearly NetCDF files read directly over S3 (h5netcdf)
`python -m loaders.cora --diagnose` lists what the bucket contains and which
strategy works on your machine — paste its output if CORA still fails.

The mesh is connected to the dashboard map: `map_layer()` draws the mesh nodes
(and triangles when cached) around the selected point, and get_data() uses the
node nearest to the requested lat/lon.
    python -m loaders.cora --build   # caches NC node coordinates in data/ (commit them)
"""

from __future__ import annotations

import re
import sys
import time
from functools import lru_cache

import numpy as np
import pandas as pd
import xarray as xr

from config import NC_BBOX
from loaders.base import DATA_DIR, empty_stations, finish, haversine_km, nearest, parse_point

SOURCE_NAME = "NOAA CORA"
KIND = "gridded"
COVERAGE = {"start": "1979-01-01", "end": "2022-12-31", "resolution": "ADCIRC+SWAN mesh (~500 m), hourly",
            "note": "Water level (rel. MSL), Hs, Tp, mean wave direction"}

BUCKET = "noaa-nos-cora-pds"
DOMAIN = "GEC"
DOMAIN_DIR = "cora_gec"
S3 = {"anon": True}

PRODUCTS = {
    "water_level": {"label": "Water level, rel. MSL (hourly)", "tag": "fort.63",  "var": "zeta",
                    "col": "water_level_m", "folder": "water_levels"},
    "hs":          {"label": "Significant wave height",        "tag": "swan_HS",  "var": "swan_HS",
                    "col": "hs_m", "folder": "waves"},
    "tp":          {"label": "Peak wave period",               "tag": "swan_TP",  "var": "swan_TP",
                    "col": "tp_s", "folder": "waves"},
    "dir":         {"label": "Mean wave direction",            "tag": "swan_DIR", "var": "swan_DIR",
                    "col": "wave_dir_deg", "folder": "waves"},
}
OPTIONS = {"mesh": ["auto", "shoreline nodes (fast)", "full mesh"]}

NODES_FILE = {"native": DATA_DIR / "cora_nodes_nc.csv.gz", "shoreline": DATA_DIR / "cora_shoreline_nodes_nc.csv.gz"}
ELEMS_FILE = DATA_DIR / "cora_elements_nc.npz"
SHORELINE_MAX_KM = 1.5          # "auto": use shoreline nodes when one is this close
LOG: list[str] = []             # what was tried (shown by --diagnose and in error messages)


def _log(msg: str):
    LOG.append(msg)
    if len(LOG) > 200:
        del LOG[:100]


# --------------------------------------------------------------------------- #
# Bucket discovery
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _fs():
    import s3fs
    return s3fs.S3FileSystem(anon=True)


@lru_cache(maxsize=32)
def _ls(path: str) -> tuple[str, ...]:
    try:
        return tuple(_fs().ls(path, detail=False))
    except Exception as e:
        _log(f"ls {path}: {type(e).__name__}: {e}")
        return ()


def _walk(path: str, depth: int = 3) -> list[str]:
    """List files/stores below path without descending into *.zarr (they hold millions of keys)."""
    out = []
    for p in _ls(path):
        base = p.rstrip("/").rsplit("/", 1)[-1]
        if base.endswith(".zarr") or "." in base:
            out.append(p.rstrip("/"))
        elif depth > 1:
            out.extend(_walk(p, depth - 1))
    return out


def _candidates(folder_path: str, pattern: str) -> list[str]:
    rx = re.compile(pattern)
    return sorted(p for p in _walk(folder_path) if rx.search(p.rsplit("/", 1)[-1]))


# --------------------------------------------------------------------------- #
# Openers
# --------------------------------------------------------------------------- #
def _open_zarr(path: str) -> xr.Dataset:
    url = path if path.startswith("s3://") else f"s3://{path}"
    errors = []
    try:
        return xr.open_zarr(url, storage_options=S3, chunks={}, consolidated=None)
    except Exception as e:
        errors.append(f"zarr: {type(e).__name__}: {e}")
    try:  # virtual (kerchunk-style) reference store
        import fsspec
        fs = fsspec.filesystem("reference", fo=url, remote_protocol="s3", remote_options=S3,
                               target_options=S3)
        return xr.open_dataset(fs.get_mapper(""), engine="zarr", chunks={},
                               backend_kwargs={"consolidated": False})
    except Exception as e:
        errors.append(f"reference: {type(e).__name__}: {e}")
    raise RuntimeError("; ".join(errors))


def _open_nc(path: str) -> xr.Dataset:
    f = _fs().open(path if not path.startswith("s3://") else path[5:], "rb",
                   block_size=4 * 2 ** 20, cache_type="blockcache")
    return xr.open_dataset(f, engine="h5netcdf", chunks={})


@lru_cache(maxsize=1)
def _catalog():
    import intake
    import zarr
    names = ["CORA_intake_zarr3.yml", "CORA_intake.yml"]
    if int(zarr.__version__.split(".")[0]) < 3:
        names.reverse()
    errs = []
    for n in names:
        try:
            cat = intake.open_catalog(f"s3://{BUCKET}/{n}", storage_options=S3)
            _log(f"intake catalog {n}: {len(list(cat))} entries")
            return cat
        except Exception as e:
            errs.append(f"{n}: {type(e).__name__}: {e}")
    raise RuntimeError("intake catalog: " + "; ".join(errs))


def _intake_entry(tag: str, chunk: str = "timeseries") -> str:
    entries = list(_catalog())
    t = tag.lower().replace(".63", "")
    ranked = [e for e in entries if DOMAIN.lower() in e.lower() and t in e.lower().replace(".63", "")]
    ranked.sort(key=lambda e: (chunk not in e.lower(), len(e)))
    if not ranked:
        raise KeyError(f"no intake entry for {DOMAIN} {tag} (entries: {entries[:12]}…)")
    return ranked[0]


# --------------------------------------------------------------------------- #
# Per-product dataset resolution (cached for the session)
# --------------------------------------------------------------------------- #
class Source:
    """An opened CORA dataset plus how to read it."""

    def __init__(self, kind, label, ds=None, files=None):
        self.kind, self.label, self.ds, self.files = kind, label, ds, files or {}

    def coords(self, var_hint):
        ds = self.ds if self.ds is not None else _open_nc(next(iter(self.files.values())))
        lon = _first(ds, ["x", "lon", "longitude", "node_lon", "nodes_lon"])
        lat = _first(ds, ["y", "lat", "latitude", "node_lat", "nodes_lat"])
        depth = _first(ds, ["depth", "bathymetry", "h"], required=False)
        var = _data_var(ds, var_hint)
        node_dim = ds[lon].dims[0]
        return ds, lon, lat, depth, var, node_dim


def _first(ds, names, required=True):
    for n in names:
        if n in ds.variables:
            return n
    if required:
        raise KeyError(f"none of {names} in dataset (has {list(ds.variables)[:20]})")
    return None


def _data_var(ds, hint):
    if hint in ds.data_vars:
        return hint
    cands = [v for v, da in ds.data_vars.items() if "time" in da.dims and da.ndim == 2]
    for key in (hint.lower(), "zeta", "water", "wl", "hs", "tp", "dir"):
        for v in cands:
            if key in v.lower():
                return v
    if cands:
        return cands[0]
    raise KeyError(f"no (time, node) variable like {hint} in {list(ds.data_vars)}")


@lru_cache(maxsize=8)
def _source(product: str, mesh: str) -> Source:
    p = PRODUCTS[product]
    errors = []
    base = f"{BUCKET}/{DOMAIN_DIR}"

    if mesh == "shoreline":
        if p["folder"] != "water_levels":
            raise RuntimeError("shoreline files contain water levels only")
        found = _candidates(f"{base}/native_grid_shoreline", r"^NC([._]|$)")
        _log(f"shoreline candidates: {found}")
        for path in sorted(found, key=lambda s: ("timeseries" not in s, not s.endswith(".zarr"))):
            try:
                ds = _open_zarr(path) if path.endswith(".zarr") else _open_nc(path)
                return Source("shoreline", path.rsplit("/", 1)[-1], ds)
            except Exception as e:
                errors.append(f"{path}: {e}")
        raise RuntimeError("NC shoreline file not usable. " + " | ".join(errors or ["none found"]))

    # 1. intake catalog
    try:
        name = _intake_entry(p["tag"])
        ds = _catalog()[name].to_dask()
        return Source("intake", name, ds)
    except Exception as e:
        errors.append(f"intake: {type(e).__name__}: {e}")
    # 2. zarr stores found by listing
    folder = f"{base}/native_grid/{p['folder']}"
    tag_rx = re.escape(p["tag"]).replace("TP", "TPS?")
    stores = _candidates(folder, rf"^{tag_rx}\.63.*\.zarr$")
    for path in sorted(stores, key=lambda s: ("timeseries" not in s, len(s))):
        try:
            return Source("zarr", path.rsplit("/", 1)[-1], _open_zarr(path))
        except Exception as e:
            errors.append(f"{path}: {e}")
    # 3. yearly NetCDF files
    ncs = _candidates(folder, rf"^{tag_rx}\.63.*\.nc$")
    if ncs:
        pref = [f for f in ncs if "timeseries" in f] or ncs
        files = {}
        for f in pref:
            m = re.search(r"_(\d{4})(?:-(\d{4}))?", f.rsplit("/", 1)[-1])
            if m:
                y0, y1 = int(m.group(1)), int(m.group(2) or m.group(1))
                files[(y0, y1)] = f
        if files:
            return Source("netcdf", f"{len(files)} NetCDF files", files=files)
    errors.append(f"listing {folder}: {len(stores)} zarr, {len(ncs)} nc")
    raise RuntimeError(f"CORA {product}: no access path worked. " + " | ".join(errors))


# --------------------------------------------------------------------------- #
# Node coordinates (cached in data/)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=2)
def nodes(mesh: str = "native") -> pd.DataFrame:
    """Mesh nodes inside the NC box: columns node, lat, lon, depth."""
    f = NODES_FILE[mesh]
    if f.exists():
        return pd.read_csv(f)
    src = _source("water_level", mesh) if mesh == "shoreline" else _source_any_native()
    ds, lon, lat, depth, _, node_dim = src.coords("zeta")
    x, y = np.asarray(ds[lon].values, float), np.asarray(ds[lat].values, float)
    x = np.where(x > 180, x - 360, x)
    lon0, lat0, lon1, lat1 = NC_BBOX
    idx = np.where((x >= lon0) & (x <= lon1) & (y >= lat0) & (y <= lat1))[0]
    df = pd.DataFrame({"node": idx, "lat": y[idx].round(6), "lon": x[idx].round(6)})
    if depth is not None:
        df["depth"] = np.asarray(ds[depth].values, float)[idx].round(2)
    DATA_DIR.mkdir(exist_ok=True)
    df.to_csv(f, index=False)
    if mesh == "native":
        _cache_elements(ds, set(idx.tolist()))
    return df


def _source_any_native() -> Source:
    errs = []
    for prod in ("water_level", "hs"):
        try:
            return _source(prod, "native")
        except Exception as e:
            errs.append(str(e))
    raise RuntimeError(" | ".join(errs))


def _cache_elements(ds, keep: set):
    """Triangles fully inside the NC box (for drawing the mesh). Optional."""
    name = _first(ds, ["element", "elements", "ele", "nv", "face_node_connectivity"], required=False)
    if not name:
        return
    try:
        el = np.asarray(ds[name].values)
        if el.shape[0] == 3 and el.shape[1] != 3:
            el = el.T
        el = el.astype(np.int64) - (1 if el.min() >= 1 else 0)   # ADCIRC is 1-based
        keep_arr = np.zeros(el.max() + 1, bool)
        keep_arr[list(keep)] = True
        sel = el[keep_arr[el].all(axis=1)]
        np.savez_compressed(ELEMS_FILE, elements=sel)
    except Exception as e:
        _log(f"elements not cached: {e}")


def _elements():
    if ELEMS_FILE.exists():
        return np.load(ELEMS_FILE)["elements"]
    return None


def nearest_node(lat: float, lon: float, mesh: str = "native"):
    nd = nodes(mesh)
    i, dist = nearest(lat, lon, nd["lat"].to_numpy(), nd["lon"].to_numpy())
    return nd.iloc[i], dist


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #
def list_stations():
    return empty_stations()


def status() -> str | None:
    if not NODES_FILE["native"].exists() and not NODES_FILE["shoreline"].exists():
        return ("CORA mesh nodes for NC are not cached yet: the first request reads the mesh "
                "coordinates from S3 (a minute or two). Run `python -m loaders.cora --build` once "
                "and commit the files in data/. Full-mesh water levels are slow to read from the "
                "cloud (NOAA known issue) — the NC shoreline nodes are much faster.")
    return None


def _choose_mesh(product: str, mesh_opt: str, lat: float, lon: float) -> str:
    if PRODUCTS[product]["folder"] != "water_levels":
        return "native"
    if mesh_opt.startswith("shoreline"):
        return "shoreline"
    if mesh_opt.startswith("full"):
        return "native"
    try:  # auto
        _, d = nearest_node(lat, lon, "shoreline")
        return "shoreline" if d <= SHORELINE_MAX_KM else "native"
    except Exception as e:
        _log(f"auto: shoreline unavailable ({e}); using full mesh")
        return "native"


def _read_point(src: Source, var_hint: str, node: int, start, end) -> pd.Series:
    t0, t1 = str(pd.Timestamp(start).date()), str((pd.Timestamp(end) + pd.Timedelta(days=1)).date())
    if src.kind != "netcdf":
        ds, *_rest = src.coords(var_hint)
        var, node_dim = _rest[3], _rest[4]
        da = ds[var].isel({node_dim: node}).sel(time=slice(t0, t1))
        return pd.Series(np.asarray(da.values, float), index=pd.to_datetime(da["time"].values))
    parts = []
    y0, y1 = pd.Timestamp(start).year, pd.Timestamp(end).year
    for (a, b), path in sorted(src.files.items()):
        if b < y0 or a > y1:
            continue
        ds = _open_nc(path)
        var, node_dim = _data_var(ds, var_hint), ds[_first(ds, ["x", "lon", "longitude"])].dims[0]
        da = ds[var].isel({node_dim: node}).sel(time=slice(t0, t1))
        parts.append(pd.Series(np.asarray(da.values, float), index=pd.to_datetime(da["time"].values)))
    return pd.concat(parts) if parts else pd.Series(dtype=float)


def get_data(station_id: str, start, end, product: str = "water_level", mesh: str = "auto",
             progress=None, **_) -> pd.DataFrame:
    p = PRODUCTS[product]
    lat, lon = parse_point(station_id)
    which = _choose_mesh(product, mesh, lat, lon)
    node, dist = nearest_node(lat, lon, which)
    src = _source(product, which)
    if progress:
        progress(0.2)
    t = time.time()
    s = _read_point(src, p["var"], int(node["node"]), start, end)
    vals = np.array(s.to_numpy(float), copy=True)
    vals[(vals < -999) | (vals > 1e3)] = np.nan          # dry nodes / fill values
    if p["col"] == "water_level_m" and ("mm" in src.label.lower()) and np.nanmax(np.abs(vals)) > 50:
        vals = vals / 1000.0                               # *_mm_* stores hold integer millimetres
    out = finish(pd.DataFrame({p["col"]: vals}, index=s.index), start, end)
    if progress:
        progress(1.0)
    depth = f", depth {node['depth']:.1f} m" if "depth" in node and pd.notna(node.get("depth")) else ""
    out.attrs["model_location"] = (float(node["lat"]), float(node["lon"]))
    out.attrs["note"] = (f"CORA {'shoreline' if which == 'shoreline' else 'mesh'} node {int(node['node'])} "
                         f"({node['lat']:.4f}, {node['lon']:.4f}{depth}), {dist:.2f} km from the requested "
                         f"point · read via {src.kind}: {src.label} in {time.time() - t:.0f}s")
    return out


# --------------------------------------------------------------------------- #
# Map layer: the mesh around the selected point
# --------------------------------------------------------------------------- #
def map_layer(lat: float, lon: float, radius_km: float = 6.0, max_nodes: int = 1500):
    import folium

    fg = folium.FeatureGroup(name="CORA mesh", show=True)
    notes = []
    for mesh, color in (("native", "#e377c2"), ("shoreline", "#1d5f8a")):
        try:
            nd = nodes(mesh)
        except Exception as e:
            notes.append(f"{mesh} nodes unavailable ({str(e)[:120]})")
            continue
        d = haversine_km(lat, lon, nd["lat"].to_numpy(), nd["lon"].to_numpy())
        near = nd.assign(d=d)[d <= radius_km]
        if len(near) > max_nodes:
            near = near.nsmallest(max_nodes, "d")
        if mesh == "native":
            el = _elements()
            if el is not None and len(near):
                ok = np.zeros(int(nd["node"].max()) + 1, bool)
                ok[near["node"].to_numpy()] = True
                tri = el[(el < ok.size).all(axis=1)]
                tri = tri[ok[tri].all(axis=1)]
                lon_of = dict(zip(nd["node"], nd["lon"]))
                lat_of = dict(zip(nd["node"], nd["lat"]))
                lines = [[[lon_of[a], lat_of[a]], [lon_of[b], lat_of[b]]] for t3 in tri[:4000].tolist()
                         for a, b in ((t3[0], t3[1]), (t3[1], t3[2]), (t3[2], t3[0]))]
                if lines:
                    folium.GeoJson({"type": "Feature", "properties": {},
                                    "geometry": {"type": "MultiLineString", "coordinates": lines}},
                                   style_function=lambda _: {"color": color, "weight": 0.6, "opacity": 0.6},
                                   interactive=False).add_to(fg)
        for r in near.itertuples():
            dep = f" · depth {r.depth:.1f} m" if hasattr(r, "depth") and pd.notna(r.depth) else ""
            folium.CircleMarker((r.lat, r.lon), radius=3 if mesh == "native" else 4, weight=1,
                                color=color, fill=True, fill_opacity=0.8,
                                tooltip=f"node|{mesh}:{r.node}|{r.lat},{r.lon}|{dep}").add_to(fg)
        if len(near):
            best = near.nsmallest(1, "d").iloc[0]
            dep = f", depth {best['depth']:.1f} m" if "depth" in best and pd.notna(best["depth"]) else ""
            notes.append(f"nearest {mesh} node {int(best['node'])}: {best['d']:.2f} km{dep}")
        else:
            notes.append(f"no {mesh} node within {radius_km:.0f} km")
    note = ("Pink = full mesh, blue = NC shoreline nodes; " + "; ".join(notes)) if notes else None
    return fg, note


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def diagnose():
    import zarr
    print(f"xarray {xr.__version__}, zarr {zarr.__version__}")
    try:
        import intake
        print(f"intake {intake.__version__}")
    except ImportError:
        print("intake not installed")
    print("\nBucket top level:", [p.rsplit('/', 1)[-1] for p in _ls(BUCKET)])
    for sub in ("native_grid", "native_grid/water_levels", "native_grid/waves",
                "native_grid_shoreline", "native_grid_shoreline/water_levels"):
        items = _ls(f"{BUCKET}/{DOMAIN_DIR}/{sub}")
        print(f"\n{DOMAIN_DIR}/{sub}: {len(items)} items")
        for p in items[:25]:
            print("   ", p.rsplit("/", 1)[-1])
    print("\nNC shoreline candidates:", _candidates(f"{BUCKET}/{DOMAIN_DIR}/native_grid_shoreline", r"^NC([._]|$)"))
    try:
        print("intake entries:", [e for e in _catalog() if DOMAIN.lower() in e.lower()])
    except Exception as e:
        print("intake catalog failed:", e)
    for prod, mesh in (("water_level", "shoreline"), ("water_level", "native"), ("hs", "native")):
        t = time.time()
        try:
            src = _source(prod, mesh)
            ds, lon, lat, depth, var, nd = src.coords(PRODUCTS[prod]["var"])
            print(f"\nOK  {prod}/{mesh}: {src.kind} {src.label} var={var} dim={nd} n={ds.sizes[nd]} "
                  f"({time.time() - t:.0f}s)")
        except Exception as e:
            print(f"\nFAIL {prod}/{mesh}: {e}")
    print("\nlog:", *LOG[-20:], sep="\n  ")


def build():
    for mesh in ("shoreline", "native"):
        try:
            nd = nodes(mesh)
            print(f"{mesh}: {len(nd)} nodes in NC box -> {NODES_FILE[mesh]}")
        except Exception as e:
            print(f"{mesh}: FAILED {e}")
    print("elements cached" if ELEMS_FILE.exists() else "elements not available (nodes only)")


if __name__ == "__main__":
    if "--diagnose" in sys.argv:
        diagnose()
    elif "--build" in sys.argv:
        build()
    else:
        print(get_data("34.7173,-76.6707", "2018-09-13", "2018-09-15"))  # Beaufort gauge
