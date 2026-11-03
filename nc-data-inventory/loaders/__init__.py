"""Registry of data loaders.

The dashboard builds its tabs, map layers and validation menus from these
dicts, so adding a source = write a loader module + add one line here.
"""

from loaders import codec_gtsm, conus404, cora, era5, ndbc, noaa_coops, usgs_nwis, wis

# key -> module
OBSERVED = {
    "noaa_coops": noaa_coops,
    "ndbc": ndbc,
    "usgs_nwis": usgs_nwis,
}

MODEL = {
    "wis": wis,
    "codec_gtsm": codec_gtsm,
    "cora": cora,
    "era5": era5,
    "conus404": conus404,
}

ALL = OBSERVED | MODEL

# marker / coverage colour per source on the map
COLORS = {
    "noaa_coops": "#1f77b4",
    "ndbc": "#ff7f0e",
    "usgs_nwis": "#2ca02c",
    "wis": "#9467bd",
    "codec_gtsm": "#8c564b",
    "cora": "#e377c2",
    "era5": "#7f7f7f",
    "conus404": "#bcbd22",
}
