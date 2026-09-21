// config.js
//
// This is the one file you edit per-city. Everything else in the repo is
// generic — swap these values and the map, pipeline, and GitHub Action all
// follow along.

export const CONFIG = {
  // Basemap style (any MapLibre-compatible style URL works).
  MAP_STYLE: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',

  // TODO: set to your city's center [lng, lat].
  MAP_CENTER: [9.7386, 52.3744],
  MAP_ZOOM: 13,

  // Folder the map fetches trips.geojson / road_segments_averaged.json from.
  // './' is correct when they're generated into the repo root (the default —
  // see generate_trips_geojson.py and .github/workflows/generate-trips.yml).
  DATA_URL: './'
};

window.CONFIG = CONFIG;
