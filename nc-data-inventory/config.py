"""Global settings for the NC Coast Data Inventory dashboard."""

APP_TITLE = "North Carolina Coastal Data Inventory"
APP_ID = "NC_Coast_Data_Inventory"  # sent to APIs that ask for an application name

# Map view centred on the NC coastline (Outer Banks / Pamlico Sound)
MAP_CENTER = (35.25, -76.60)
MAP_ZOOM = 7

# Region of interest: (lon_min, lat_min, lon_max, lat_max)
# Covers the whole NC coast plus a margin into VA/SC and offshore.
NC_BBOX = (-79.2, 33.4, -74.5, 37.0)

# Default request window when a user first opens a station (days back from today)
DEFAULT_WINDOW_DAYS = 30

# Basemap tiles
BASEMAPS = {
    "Esri Ocean": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}",
        "Tiles &copy; Esri, GEBCO, NOAA, National Geographic, DeLorme, HERE, Geonames.org",
    ),
    "Esri Imagery": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "Tiles &copy; Esri",
    ),
    "OpenStreetMap": ("OpenStreetMap", None),
}
