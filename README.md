# Cycling Behaviour Dashboard

A bike sensor data visualization tool that turns GPS + accelerometer data
into an interactive map: speed coloring, road quality, and sudden-braking
hotspots. Built to be copy-pasted and re-pointed at a new city — see
[Adapting to a new city](#adapting-to-a-new-city) below.

## Overview

Trips come from **one place only: Supabase.** There is no manual/local
upload path — every trip must exist in your Supabase `trips` table
(with matching `raw_data`, `gnss`, and `data1` rows) to show up on the map.

```
Supabase DB ──► generate_trips_geojson.py ──► trips.geojson ──┐
                                                                 ├──► map
                              road_averaging.py ──► road_segments_averaged.json ─┘
```

`generate_trips_geojson.py` fetches every trip, reconstructs GPS + speed +
road quality + braking events, and writes `trips.geojson`.
`road_averaging.py` then aggregates that into per-road-segment averages.

This runs on a schedule via `.github/workflows/generate-trips.yml` — a
GitHub Action fetches from Supabase, regenerates both files, and commits
them automatically. In normal operation you never run anything by hand.

## Features

- **Speed-colored routes** — gradient or category mode
- **Road quality mapping** — 5-level scale from third-party/accelerometer data
- **Sudden braking detection + hotspots** — accumulated across trips into
  location-based clusters, sized by event count
- **Averaged road segments** — combined speed + quality score per segment
- **Click any route or sensor legend entry** to isolate it; click the map
  background to reset
- Single static-file rendering (`trips.geojson`) — no backend needed at runtime

## Project Structure

```
├── index.html                      # Page shell + manual/legend markup
├── app.js                          # Map logic and interactions
├── config.js                       # ⚙️ Per-city settings — edit this first
├── styles.css                      # Styling
│
├── generate_trips_geojson.py       # Fetch from Supabase → trips.geojson
├── road_averaging.py               # trips.geojson → road_segments_averaged.json
├── road_quality_calculator.py      # Road quality scoring module (used by the above)
├── master_pipeline.py              # Convenience wrapper: runs both, in order
│
├── .github/workflows/
│   └── generate-trips.yml          # Scheduled cloud fetch + auto-commit
├── .env.example                    # Copy to .env for local pipeline runs
└── trips.geojson                   # Map data (generated — not committed by you)
```

## Quick Start

### 1. Point it at your city

Edit `config.js`:

```javascript
export const CONFIG = {
  MAP_STYLE: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
  MAP_CENTER: [24.7536, 59.4370], // [lng, lat] — e.g. Tallinn
  MAP_ZOOM: 13,
  DATA_URL: './'
};
```

### 2. Connect your Supabase project

```bash
cp .env.example .env
# then fill in SUPABASE_HOST / PORT / DB / USER / PASSWORD
```

`generate_trips_geojson.py` and `road_averaging.py` expect the same
`trips` / `raw_data` / `gnss` / `data1` table shapes as the original
sensor pipeline. If your new city's sensors write a different schema,
that's the part you'll need to adapt — the reconstruction SQL lives at
the top of `generate_trips_geojson.py`.

**Python packages:**
```bash
pip install psycopg2-binary python-dotenv numpy
```

### 3. Generate the map data

```bash
python master_pipeline.py
```

This runs `generate_trips_geojson.py` then `road_averaging.py`, then
tells you to commit + push. Or just let the scheduled GitHub Action do it
(see below) — no manual step required day-to-day.

### 4. Automate it (GitHub Action)

`.github/workflows/generate-trips.yml` already runs this on a cron
schedule (every 6 hours) and commits the result if anything changed. To
use it in your fork:

1. Add repo secrets: `SUPABASE_HOST`, `SUPABASE_PORT`, `SUPABASE_DB`,
   `SUPABASE_USER`, `SUPABASE_PASSWORD` (Settings → Secrets and variables → Actions)
2. Adjust the cron schedule if you want a different cadence
3. That's it — trips appear on the map automatically as new sensor data lands in Supabase

### 5. Serve it

It's a static site. Any static host works (GitHub Pages, Netlify, S3, etc.)
— just serve `index.html` and the generated `.json`/`.geojson` files
alongside it.

## Configuration reference

| Setting | Where | Purpose |
|---|---|---|
| Map center/zoom/style | `config.js` | Per-city map defaults |
| Supabase credentials | `.env` (local) or repo Secrets (Action) | DB connection |
| `MAX_SPEED_KMH`, `TRIM_M`, `SPEED_SMOOTH_WIN` | `generate_trips_geojson.py` | GPS cleaning/smoothing |
| `BRAKING_DECEL_THRESHOLD_GPS_KMH_S` | `generate_trips_geojson.py` | Braking sensitivity (km/h lost per second) |
| `CELL_SIZE` | `app.js` → `buildBrakingHotspots()` | Braking hotspot cluster grid size |
| Cron schedule | `.github/workflows/generate-trips.yml` | How often to refresh data |

## Adapting to a new city

This repo was originally built for one pilot deployment (Marineterrein,
Amsterdam) and has since been stripped down to a reusable template:

- All city-specific UI (image banner, hardcoded site boundary overlay) has
  been removed
- Manual trip entry (local CSV upload) has been removed — Supabase is the
  only ingestion path, by design, so every deployment works the same way
- Non-essential UI (trip search bar, isochrone/travel-range layer, sensor
  leaderboard) has been removed to keep the base map minimal; add back
  whatever your deployment actually needs

To stand up a new city: fork, edit `config.js`, point `.env` / Action
secrets at that city's Supabase project, and push. If the sensor schema
differs from the original pilot's `trips`/`raw_data`/`gnss`/`data1`
tables, you'll need to adjust the SQL in `generate_trips_geojson.py`
accordingly — that's the one place the data model is assumed.

## Troubleshooting

### "Map is blank"
- Check that `trips.geojson` exists in the repo root and has been pushed
- Open the browser console and look for fetch errors
- Confirm `config.js` isn't pointing at the wrong `DATA_URL`

### "No trips fetched from Supabase"
- Check `.env` credentials (or the Action's repo secrets)
- Confirm the `trips` table actually has rows and the schema matches
  what `generate_trips_geojson.py` expects

### "Trip skipped due to timeout"
- The reconstruction query for that trip took longer than `STATEMENT_TIMEOUT`
  (default 30s) — increase it in `generate_trips_geojson.py`

### "Speed shows 0 or capped for some trips"
- GPS speed was null/erratic for that trip — check the raw `gnss` rows in Supabase

## Use Cases

- **Urban planning** — locate braking hotspots (dangerous junctions, poor
  sightlines, bad surfaces); plan bike lane improvements from real usage
- **Cycling safety** — flag road quality problem areas
- **Research** — compare speed/quality/braking across sensors and trips
