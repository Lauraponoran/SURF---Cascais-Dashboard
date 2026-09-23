#!/usr/bin/env python3
"""
Standalone Cascais bike-trip animations using the same CARTO Dark Matter
basemap and the same sensor colour mapping as the dashboard.

Adapted from the original Marineterrein/Amsterdam version — see
config.js for this deployment's map center/zoom, which this script mirrors.

Outputs:
  cascais_trips_live.gif          - full route network always visible;
                                     dots continuously travel the routes,
                                     each looping on its own staggered
                                     cycle so the city is always moving.
  cascais_trips_live_flyover.gif  - the trips that make up the busiest
                                     corridor stitched into one continuous
                                     route; the camera flies along it start
                                     to end, holding one fixed heading the
                                     whole way (like a drone moving in a
                                     straight-ish direction of travel)
                                     instead of turning to track the
                                     route's bends (see
                                     stitch_trips_into_route(),
                                     build_combined_route() and
                                     compute_follow_camera()).
  cascais_trips_chronological.gif - trips drawn on one at a time, in the
                                     order they actually happened.
  cascais_trips_growth.gif        - trips drawn on one at a time, ordered
                                     to spread outward from the city centre
                                     (see compute_growth_order()).
 
Install:
  pip install playwright pillow
  playwright install chromium
 
Run:
  python make_cascais_animation.py trips.geojson
 
The browser needs internet access while rendering because MapLibre loads the
CARTO/OSM basemap tiles. The resulting GIFs are standalone files.
"""

import io
import json
import math
import os
import random
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# Visual / animation settings
# ---------------------------------------------------------------------------

WIDTH = 1100
HEIGHT = 720
FPS = 12

MAP_STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"
# Matches config.js's MAP_CENTER / MAP_ZOOM for this deployment.
MAP_CENTER = [-9.42147, 38.69681]
MAP_ZOOM = 13
# Tilted "flyover" camera instead of a flat top-down view. PITCH is degrees
# from straight-down (0 = top-down, 60 = MapLibre's practical max before the
# horizon gets ugly). BEARING rotates the camera (0 = north up); a non-zero
# bearing often reads better with a tilt since it avoids routes running
# straight toward/away from the camera. Tune both to taste.
MAP_PITCH = 50
MAP_BEARING = -20

# "Flyover" camera for cascais_trips_live_flyover.gif: instead of the fixed
# overview above, this camera FOLLOWS the route -- it starts at the stitched
# route's start and moves along it frame by frame, bearing continuously
# re-aimed a little way ahead so the camera looks where it's headed instead
# of straight down. This is deliberately not one MapLibre flyTo() animation:
# every other GIF in this script is built as discrete stepped frames from
# Python (the camera/route state is computed per-frame, then screenshotted),
# and this follows the same pattern so it behaves consistently under
# maybe_capture()'s skip-if-exists logic and the same frame-capture loop.
# 60 is MapLibre-gl's default max pitch -- going higher needs a custom
# maxPitch on the map, which isn't worth it here since 60 already reads as
# a proper horizon shot.
FLYOVER_PITCH = 60
FLYOVER_ZOOM = MAP_ZOOM + 0.5          # only slightly closer than the
                                         # overview -- a tight zoom turns
                                         # small position/bearing noise into
                                         # large on-screen motion, which is
                                         # exactly what reads as "sickening"
FLYOVER_TOTAL_SECONDS = 32.0            # fixed length of the fly-along GIF;
                                         # spreading the same route over more
                                         # time directly lowers how much the
                                         # frame has to move each step
FLYOVER_ACTIVE_CORE_WIDTH = 2.6         # the flown (already-traveled) line's
                                         # core width -- noticeably thicker
                                         # than the 1.45 other GIFs use, so
                                         # the route reads clearly at
                                         # flyover's closer zoom
FLYOVER_ACTIVE_UNDERLAY_WIDTH = 5.6     # matching glow underlay, widened to
                                         # scale with the thicker core
FLYOVER_ACTIVE_UNDERLAY_OPACITY = 0.34  # brighter glow than the 0.20 other
                                         # GIFs use, so the line pops against
                                         # the dark basemap
FLYOVER_MIN_VISIBLE_FRACTION = 0.02     # even at u=0 the camera should
                                         # already show a stub of bright
                                         # route -- partial_points(u=0)
                                         # returns a single point, which is
                                         # not a line and rendered nothing,
                                         # so the route was invisible for
                                         # the first several frames

# Same list and ordering as the actual dashboard's SENSOR_COLORS.
SENSOR_COLORS = [
    '#34CCCC','#FFCC33',"#0C58FD",'#CC5BAA',"#45CC33",
    '#FF7A3D','#88DDFF','#FFE066','#CC3355','#66FF99',
    '#AA88FF','#FF9966','#00CCFF','#FFB3DE','#44FFDD',
    '#FFAA00','#7BFFB3','#FF6680','#B3EEFF','#D4FF66',
]
DEFAULT_COLOR = '#34CCCC'

# Very short trips make poor animated paths; omit only the tiny ones.
MIN_LIVE_TRIP_DURATION_S = 8.0

# "Alive" mode: the route network is fully drawn from frame 0 (it never
# grows). Moving dots are capped per corridor -- at most a handful of trips
# on the same sensor's route are ever animated at once -- rather than trying
# to serialize every single historical trip with zero overlap, which blows
# up badly on a busy corridor (e.g. 40 trips needing 20-40 minutes to each
# get an overlap-free turn). The GIF's length stays fixed regardless of how
# much data you feed it, so file size and render time stay predictable.
LIVE_MIN_ANIMATION_S = 20.0        # slowest a dot can traverse its route, in seconds
LIVE_DURATION_SCALE = 1.0 / 100.0  # how much a trip's real duration stretches the animated time
LIVE_TOTAL_SECONDS = 48.0          # fixed length of the looping "alive" GIF
LIVE_MAX_CONCURRENT_PER_ROUTE = 4  # hard cap: this many dots, max, on one corridor at once
LIVE_ROUTE_GAP_S = 1.5             # buffer between two dots sharing the same lane
LIVE_BACKGROUND_OPACITY = 0.50     # opacity of the always-visible route network
LIVE_BRAKE_SPEED_RATIO = 0.18      # fraction of a trip's own top speed treated as "braking/stopped"

# Chronological draw-on animation. This is intentionally slower than before.
CHRONO_SECONDS_PER_TRIP = 0.30

# "Growth" draw-on animation: same crisp draw-on style as the chronological
# GIF, but ordered outward from the city centre instead of by real-world time.
# Same coordinates as config.js's MAP_CENTER -- nudge these if you want a
# different seed point (e.g. a specific depot or landmark instead).
ORIGIN_LON = -9.42147
ORIGIN_LAT = 38.69681
GROWTH_SECONDS_PER_TRIP = 0.30

# Many trips are the same rider doing the same commute on a different day,
# so without deduping, the growth GIF ends up re-drawing the same physical
# route back-to-back -- it reads as a stutter/lag rather than exploration.
# Only the growth animation dedupes (live/chronological still show every
# trip). Two trips are treated as the same route if their sampled points
# are, on average, within GROWTH_DEDUPE_METERS of each other.
GROWTH_DEDUPE_METERS = 25.0
GROWTH_DEDUPE_SAMPLES = 8

# Keep enough geometry for the route to feel like the actual street trace.
MAX_POINTS_PER_TRIP = 420


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def parse_time(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def load_trips(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    grouped = defaultdict(list)

    for feature in data.get("features", []):
        props = feature.get("properties", {})
        trip_id = props.get("trip_id")
        geom = feature.get("geometry", {})

        if not trip_id or geom.get("type") != "LineString":
            continue

        coords = geom.get("coordinates", [])
        if len(coords) >= 2:
            grouped[trip_id].append(feature)

    trips = []

    for trip_id, features in grouped.items():
        samples = []

        for feature in features:
            props = feature.get("properties", {})
            ts = props.get("timestamp")
            coords = feature["geometry"]["coordinates"]
            if not ts:
                continue

            t0 = parse_time(ts)
            dt = max(float(props.get("time_diff_s") or 0), 0.02)
            speed = float(props.get("Speed") or 0)

            samples.append((t0, coords[0][0], coords[0][1], speed))
            samples.append((t0 + dt, coords[-1][0], coords[-1][1], speed))

        if len(samples) < 2:
            continue

        samples.sort(key=lambda p: p[0])

        # Keep the final point at each timestamp.  This avoids zero-time jumps
        # while preserving the actual segment timing.
        clean = []
        for point in samples:
            if clean and point[0] == clean[-1][0]:
                clean[-1] = point
            else:
                clean.append(point)

        if len(clean) < 2:
            continue

        start = clean[0][0]
        end = clean[-1][0]
        duration = max(end - start, 0.001)

        # Downsample by TIME, not by point count. That preserves traffic-light
        # stops and other slower sections much better than uniform point picks.
        if len(clean) > MAX_POINTS_PER_TRIP:
            selected = [clean[0]]
            step = (len(clean) - 1) / (MAX_POINTS_PER_TRIP - 1)
            for i in range(1, MAX_POINTS_PER_TRIP - 1):
                selected.append(clean[round(i * step)])
            selected.append(clean[-1])
            clean = selected

        normalized = [
            ((t - start) / duration, lon, lat, speed)
            for t, lon, lat, speed in clean
        ]

        trips.append({
            "id": trip_id,
            "sensor": trip_id.split("_")[0],
            "start": start,
            "duration": duration,
            "points": normalized,
        })

    trips.sort(key=lambda t: t["start"])

    # EXACTLY mirror the dashboard's sensor mapping:
    # sorted sensor IDs -> SENSOR_COLORS by index.
    sensors = sorted({trip["sensor"] for trip in trips})
    sensor_color_map = {
        sensor: SENSOR_COLORS[i % len(SENSOR_COLORS)]
        for i, sensor in enumerate(sensors)
    }

    for trip in trips:
        trip["color"] = sensor_color_map.get(trip["sensor"], DEFAULT_COLOR)

    print(f"Loaded {len(trips)} trips from {len(sensors)} sensors.")
    print("Sensor colour map:")
    for sensor in sensors:
        print(f"  {sensor}: {sensor_color_map[sensor]}")

    durations = [trip["duration"] for trip in trips]
    if durations:
        print(
            f"Trip durations: {min(durations)/60:.1f} min min, "
            f"{sorted(durations)[len(durations)//2]/60:.1f} min median, "
            f"{max(durations)/60:.1f} min max."
        )

    return trips


# ---------------------------------------------------------------------------
# Browser / MapLibre
# ---------------------------------------------------------------------------

def make_html(trips):
    trips_json = json.dumps(trips, separators=(",", ":"))

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.css">
<script src="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.js"></script>
<style>
html, body {{
  margin: 0;
  padding: 0;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: #081019;
}}
#map {{ position: absolute; inset: 0; }}
#attrib {{
  display:none;
}}
</style>
</head>
<body>
<div id="map"></div>
<script>
const TRIPS = {trips_json};

const map = new maplibregl.Map({{
  container: 'map',
  style: {json.dumps(MAP_STYLE)},
  center: {json.dumps(MAP_CENTER)},
  zoom: {MAP_ZOOM},
  pitch: {MAP_PITCH},
  bearing: {MAP_BEARING},
  attributionControl: false,
  interactive: false,
  preserveDrawingBuffer: true,
  fadeDuration: 0
}});

window.__ready = false;
window.__layersReady = false;

function emptyCollection() {{ return {{type:'FeatureCollection',features:[]}}; }}

map.on('load', () => {{
  // NORMAL ROUTES: deliberately crisp. No line blur.
  map.addSource('animated-trips', {{type:'geojson', data:emptyCollection()}});

  map.addLayer({{
    id:'route-underlay', type:'line', source:'animated-trips',
    layout:{{'line-cap':'round','line-join':'round'}},
    paint:{{
      'line-color':['get','color'],
      'line-width':['get','underlayWidth'],
      'line-opacity':['get','underlayOpacity']
    }}
  }});

  map.addLayer({{
    id:'route-core', type:'line', source:'animated-trips',
    layout:{{'line-cap':'round','line-join':'round'}},
    paint:{{
      'line-color':['get','color'],
      'line-width':['get','coreWidth'],
      'line-opacity':['get','coreOpacity']
    }}
  }});

  // Separate pulse source so pulses do NOT get rendered twice by the route layers.
  map.addSource('animated-pulse', {{type:'geojson', data:emptyCollection()}});
  map.addLayer({{
    id:'route-pulse-underlay', type:'line', source:'animated-pulse',
    layout:{{'line-cap':'round','line-join':'round'}},
    paint:{{
      'line-color':['get','color'],
      'line-width':['get','pulseUnderlayWidth'],
      'line-opacity':['get','pulseUnderlayOpacity']
    }}
  }});
  map.addLayer({{
    id:'route-pulse-core', type:'line', source:'animated-pulse',
    layout:{{'line-cap':'round','line-join':'round'}},
    paint:{{
      'line-color':['get','color'],
      'line-width':['get','pulseCoreWidth'],
      'line-opacity':['get','pulseCoreOpacity']
    }}
  }});

  map.addSource('animated-heads', {{type:'geojson', data:emptyCollection()}});
  map.addLayer({{
    id:'trip-head-ring', type:'circle', source:'animated-heads',
    paint:{{
      'circle-color':['get','color'],
      'circle-radius':['get','ringRadius'],
      'circle-opacity':['get','ringOpacity'],
      'circle-stroke-color':['get','color'],
      'circle-stroke-width':1
    }}
  }});
  map.addLayer({{
    id:'trip-head-core', type:'circle', source:'animated-heads',
    paint:{{
      'circle-color':['get','color'],
      'circle-radius':['get','radius'],
      'circle-opacity':1
    }}
  }});

  window.__ready = true;
  setTimeout(() => {{ window.__layersReady = true; }}, 500);
}});

window.setFrame = function(routes, pulse, heads, camera) {{
  map.getSource('animated-trips').setData({{type:'FeatureCollection',features:routes}});
  map.getSource('animated-pulse').setData({{type:'FeatureCollection',features:pulse}});
  map.getSource('animated-heads').setData({{type:'FeatureCollection',features:heads}});
  // Only the fly-along camera passes this per frame; every other GIF's
  // camera is fixed for the whole capture and never calls setFrame with
  // one, so plain frames stay exactly as before.
  if (camera) {{ map.jumpTo(camera); }}
}};
window.isReady = () => window.__ready && window.__layersReady;
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def interpolate(points, u):
    if u <= points[0][0]:
        return points[0]
    if u >= points[-1][0]:
        return points[-1]

    lo, hi = 0, len(points) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if points[mid][0] < u:
            lo = mid
        else:
            hi = mid

    a = points[lo]
    b = points[hi]
    q = (u - a[0]) / max(b[0] - a[0], 1e-9)
    return (
        u,
        a[1] + q * (b[1] - a[1]),
        a[2] + q * (b[2] - a[2]),
        a[3] + q * (b[3] - a[3]),
    )


def partial_points(points, u):
    if u <= 0:
        return [points[0]]
    if u >= 1:
        return list(points)

    result = [points[0]]
    for i in range(1, len(points)):
        a = points[i - 1]
        b = points[i]
        if b[0] >= u:
            q = (u - a[0]) / max(b[0] - a[0], 1e-9)
            result.append((
                u,
                a[1] + q * (b[1] - a[1]),
                a[2] + q * (b[2] - a[2]),
                a[3] + q * (b[3] - a[3]),
            ))
            break
        result.append(b)
    return result


def reversed_points(points):
    # Same samples, walked from the other end, re-normalized so u still
    # runs 0 -> 1 in the new walking direction. Used by the growth mode so
    # a trip can be revealed starting from whichever endpoint is closest to
    # the already-grown network, instead of always from its recorded start.
    return [(1.0 - u, lon, lat, speed) for (u, lon, lat, speed) in reversed(points)]


def haversine_m(lon1, lat1, lon2, lat2):
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def bearing_deg(lon1, lat1, lon2, lat2):
    # Standard forward azimuth, degrees clockwise from north -- same
    # convention MapLibre uses for `bearing`, so the result can be fed to
    # the map directly with no conversion.
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def trip_geo_length_m(trip):
    pts = trip['points']
    total = 0.0
    for i in range(1, len(pts)):
        total += haversine_m(pts[i - 1][1], pts[i - 1][2], pts[i][1], pts[i][2])
    return total


def stitch_trips_into_route(trips):
    """Chain a set of trips end-to-end into one continuous path, on the
    assumption (per the user) that they're really legs of one bigger ride
    rather than unrelated commutes. Starts from the earliest-starting trip,
    then greedily attaches whichever remaining trip has an endpoint closest
    to the current chain's tail, flipping it if its END is the closer one.
    Simple nearest-neighbour chaining rather than compute_growth_order's
    distance-from-origin search: growth mode needed non-decreasing distance
    from a fixed origin so revealed trips never "jump backwards"; here there
    is no origin, just one path to assemble, so plain nearest-tail chaining
    is enough for the handful of legs this is meant for.

    Returns an ordered list of (trip, flip) tuples -- flip=True means that
    trip's points should be walked in reverse to continue the chain.
    """
    remaining = sorted(trips, key=lambda t: t['start'])
    first = remaining.pop(0)
    ordered = [(first, False)]
    tail_lon, tail_lat = first['points'][-1][1], first['points'][-1][2]

    while remaining:
        best_i, best_flip, best_d = None, False, None
        for i, t in enumerate(remaining):
            s, e = t['points'][0], t['points'][-1]
            d_start = haversine_m(tail_lon, tail_lat, s[1], s[2])
            d_end = haversine_m(tail_lon, tail_lat, e[1], e[2])
            d, flip = (d_start, False) if d_start <= d_end else (d_end, True)
            if best_d is None or d < best_d:
                best_i, best_flip, best_d = i, flip, d

        t = remaining.pop(best_i)
        ordered.append((t, best_flip))
        tail_lon, tail_lat = (
            (t['points'][0][1], t['points'][0][2]) if best_flip
            else (t['points'][-1][1], t['points'][-1][2])
        )

    return ordered


def build_combined_route(ordered_legs):
    """Turn stitch_trips_into_route()'s ordered legs into one trip-shaped
    dict: {'id', 'color', 'points'} where 'points' is a single list of
    (u, lon, lat, speed) tuples, u running 0->1 by CUMULATIVE REAL-WORLD
    DISTANCE across the whole stitched path -- not by each leg's original
    time-based u, which resets to 0 at the start of every leg and would
    make partial_points()/interpolate() jump backwards at every seam.
    Distance-based u also means a frame stepped uniformly through u moves
    the camera roughly the same real distance each step, giving a steadier
    glide than time-based pacing would (real ride speed varies a lot more
    than GPS sampling density does).
    """
    raw = []  # (lon, lat, speed), in stitched order, one leg after another
    for trip, flip in ordered_legs:
        pts = list(reversed(trip['points'])) if flip else trip['points']
        raw.extend((p[1], p[2], p[3]) for p in pts)

    cum = [0.0]
    for i in range(1, len(raw)):
        cum.append(cum[-1] + haversine_m(raw[i - 1][0], raw[i - 1][1], raw[i][0], raw[i][1]))
    total = cum[-1] or 1.0

    points = [(cum[i] / total, raw[i][0], raw[i][1], raw[i][2]) for i in range(len(raw))]
    leg_ids = "+".join(t['id'] for t, _ in ordered_legs)

    return {
        'id': f"combined[{leg_ids}]",
        # Colour of the earliest (first-stitched) leg, so the flyover reads
        # as the same corridor colour used elsewhere rather than an
        # unrelated one-off colour.
        'color': ordered_legs[0][0]['color'],
        'points': points,
    }


def ease_in_out(t):
    """Smoothstep (3t²-2t³): zero velocity at both t=0 and t=1. Used to
    turn the flyover's linear frame-time into route progress, so the
    camera ramps up to cruising speed and eases back down instead of
    starting/stopping at full speed instantly -- an instant-start was
    itself part of what made the ride feel jarring."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def compute_flyover_bearing(combined_points):
    """The ONE heading the drone camera holds for the entire flyover --
    the net direction from the route's start to its end. Computed once,
    not per frame: the camera is meant to move in a fixed, ominous
    direction while the bike's actual path wanders underneath it, not
    bank left/right to track every bend the rider took.
    """
    start = interpolate(combined_points, 0.0)
    end = interpolate(combined_points, 1.0)
    return bearing_deg(start[1], start[2], end[1], end[2])


def compute_follow_camera(combined_points, u, bearing, pitch=FLYOVER_PITCH, zoom=FLYOVER_ZOOM):
    """Camera state for one frame of the fly-along: centered on the route
    position at progress u (so it translates along the route same as
    before), but facing the fixed `bearing` passed in -- see
    compute_flyover_bearing() -- instead of re-aiming at the route's
    local heading each frame.
    """
    here = interpolate(combined_points, u)
    return {
        'center': [here[1], here[2]],
        'zoom': zoom,
        'pitch': pitch,
        'bearing': bearing,
    }


def compute_growth_order(trips, origin_lon, origin_lat):
    # Dijkstra-style growth: start from the origin, and at each step reveal
    # whichever not-yet-revealed trip has the smallest TOTAL distance
    # travelled from the origin -- the gap to whichever already-grown point
    # it connects to, plus its own length. Keying on total distance (rather
    # than just the gap to the frontier, which is what a plain Prim's-style
    # "nearest point" greedy does) keeps reveal order non-decreasing in
    # distance-from-origin, so growth reads as a steadily expanding
    # frontier. A pure nearest-gap greedy can let one long trip's cheap
    # first step fling the frontier far out, after which short trips much
    # closer to the origin get revealed later and look like a step back.
    #
    # O(n^2) in the number of trips (checked against every already-revealed
    # point each step): fine for a semester's worth of bike trips (low
    # hundreds), but this will get slow into the low thousands.
    lon_scale = math.cos(math.radians(origin_lat))
    meters_per_deg_lat = 111_320.0

    def meters(lon1, lat1, lon2, lat2):
        dlat_m = (lat1 - lat2) * meters_per_deg_lat
        dlon_m = (lon1 - lon2) * meters_per_deg_lat * lon_scale
        return math.hypot(dlon_m, dlat_m)

    endpoints = {}
    lengths = {}
    for trip in trips:
        pts = trip['points']
        s, e = pts[0], pts[-1]
        endpoints[trip['id']] = ((s[1], s[2]), (e[1], e[2]))
        length = 0.0
        for i in range(1, len(pts)):
            length += meters(pts[i - 1][1], pts[i - 1][2], pts[i][1], pts[i][2])
        lengths[trip['id']] = length

    remaining = list(trips)
    # revealed points: (lon, lat, cumulative_distance_from_origin_m)
    revealed = [(origin_lon, origin_lat, 0.0)]
    order = []

    while remaining:
        best_i, best_total, best_flip = None, None, False
        for i, trip in enumerate(remaining):
            (slon, slat), (elon, elat) = endpoints[trip['id']]
            length = lengths[trip['id']]
            start_best = min(rdist + meters(slon, slat, rlon, rlat) for rlon, rlat, rdist in revealed)
            end_best = min(rdist + meters(elon, elat, rlon, rlat) for rlon, rlat, rdist in revealed)
            total, flip = (start_best + length, False) if start_best <= end_best else (end_best + length, True)
            if best_total is None or total < best_total:
                best_i, best_total, best_flip = i, total, flip

        trip = remaining.pop(best_i)
        order.append((trip, best_flip))

        (slon, slat), (elon, elat) = endpoints[trip['id']]
        near_cum = best_total - lengths[trip['id']]
        far_cum = best_total
        if not best_flip:
            revealed.append((slon, slat, near_cum))
            revealed.append((elon, elat, far_cum))
        else:
            revealed.append((elon, elat, near_cum))
            revealed.append((slon, slat, far_cum))

    return order


def route_signature(trip, samples=GROWTH_DEDUPE_SAMPLES):
    # A handful of (lon, lat) points sampled evenly across the trip's
    # duration -- a coarse shape fingerprint used for distance comparison
    # in dedupe_for_growth(), not for exact matching.
    return [
        (interpolate(trip['points'], i / (samples - 1))[1],
         interpolate(trip['points'], i / (samples - 1))[2])
        for i in range(samples)
    ]


class _UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def dedupe_for_growth(trips, threshold_m=GROWTH_DEDUPE_METERS):
    # Union-find clustering on real-world distance (meters), not exact
    # coordinate matching -- two GPS traces of "the same" commute are
    # never bit-identical, so equality on rounded coordinates misses most
    # real repeats. O(n^2) comparisons, same order as compute_growth_order.
    meters_per_deg_lat = 111_320.0
    lon_scale = math.cos(math.radians(ORIGIN_LAT))

    def mean_dist_m(a, b):
        total = 0.0
        for (lon1, lat1), (lon2, lat2) in zip(a, b):
            dlat_m = (lat1 - lat2) * meters_per_deg_lat
            dlon_m = (lon1 - lon2) * meters_per_deg_lat * lon_scale
            total += math.hypot(dlon_m, dlat_m)
        return total / len(a)

    signatures = [route_signature(trip) for trip in trips]

    dsu = _UnionFind(len(trips))
    for i in range(len(trips)):
        for j in range(i + 1, len(trips)):
            if mean_dist_m(signatures[i], signatures[j]) <= threshold_m:
                dsu.union(i, j)

    groups = defaultdict(list)
    for i, trip in enumerate(trips):
        groups[dsu.find(i)].append(trip)

    kept = []
    dropped = 0
    for group in groups.values():
        # Keep the most detailed trace as the representative; arbitrary
        # tie-break among near-identical repeats.
        best = max(group, key=lambda t: len(t['points']))
        kept.append(best)
        dropped += len(group) - 1

    if dropped:
        print(f"Growth animation: folded {dropped} duplicate-route trip(s) "
              f"into {len(kept)} unique route(s) (within {threshold_m:.0f}m).")

    return kept


def pulse_segment(points, head_u, tail_width=0.045):
    # The moving pulse is a TRAIL behind the head, never ahead of it.
    # This keeps the dot visually anchored at the front of the active line.
    lo = max(points[0][0], head_u - tail_width)
    hi = min(points[-1][0], head_u)
    seg = [p for p in points if lo <= p[0] <= hi]

    if lo < head_u and lo > points[0][0]:
        seg.insert(0, interpolate(points, lo))
    if hi > points[0][0] and hi < points[-1][0]:
        seg.append(interpolate(points, hi))

    return seg if len(seg) >= 2 else []


def route_feature(trip, points, active=False, opacity=None, core_width=None, underlay_width=None, underlay_opacity=None):
    if len(points) < 2:
        return None

    return {
        'type':'Feature',
        'geometry':{
            'type':'LineString',
            'coordinates':[[p[1], p[2]] for p in points]
        },
        'properties':{
            'color':trip['color'],
            'underlayWidth': underlay_width if underlay_width is not None else (3.1 if active else 2.7),
            'underlayOpacity': underlay_opacity if underlay_opacity is not None else (0.20 if active else 0.12),
            'coreWidth': core_width if core_width is not None else (1.45 if active else 1.20),
            'coreOpacity':0.96 if opacity is None else opacity,
        }
    }


def pulse_feature(trip, points):
    if len(points) < 2:
        return None

    return {
        'type':'Feature',
        'geometry':{
            'type':'LineString',
            'coordinates':[[p[1], p[2]] for p in points]
        },
        'properties':{
            'color':trip['color'],
            'pulseUnderlayWidth':4.1,
            'pulseUnderlayOpacity':0.38,
            'pulseCoreWidth':1.9,
            'pulseCoreOpacity':1.0,
        }
    }


def head_feature(trip, p, stopped=False):
    return {
        'type':'Feature',
        'geometry':{'type':'Point','coordinates':[p[1],p[2]]},
        'properties':{
            'color':trip['color'],
            'radius':3.2 if not stopped else 3.8,
            'ringRadius':6.8 if not stopped else 9.2,
            'ringOpacity':0.28 if not stopped else 0.55,
        }
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def save_gif(frames, path):
    # Adaptive palette keeps the basemap reasonably detailed while retaining
    # strong sensor colours.
    paletted = [
        frame.convert('P', palette=Image.Palette.ADAPTIVE, colors=192)
        for frame in frames
    ]
    paletted[0].save(
        path,
        save_all=True,
        append_images=paletted[1:],
        duration=int(1000 / FPS),
        loop=0,
        optimize=False,
        disposal=2,
    )


def render(output_dir, geojson_path, force=False):
    trips = load_trips(geojson_path)
    if not trips:
        raise SystemExit('No usable trips found in the GeoJSON.')

    html = make_html(trips)

    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / 'animation.html'
        html_path.write_text(html, encoding='utf-8')

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()

            def new_page():
                p = browser.new_page(
                    viewport={'width':WIDTH,'height':HEIGHT},
                    device_scale_factor=1,
                )
                p.goto(html_path.as_uri(), wait_until='networkidle')
                p.wait_for_function('window.isReady()', timeout=30000)
                return p

            def capture(page, path, total_frames, builder, camera=None):
                if camera is not None:
                    # jumpTo is instant, but the tiles for the new
                    # center/zoom/pitch aren't loaded yet -- wait for the
                    # map's 'idle' event so the first captured frame isn't
                    # a half-loaded basemap. Used both for a one-time
                    # camera change (e.g. pre-loading tiles at the fly-
                    # along's starting point before the per-frame loop
                    # below starts moving in small steps) and for builders
                    # that don't move the camera at all.
                    page.evaluate(
                        "(cam) => new Promise((resolve) => { "
                        "map.once('idle', resolve); map.jumpTo(cam); })",
                        camera,
                    )

                frames = []
                for i in range(total_frames):
                    u = i / max(total_frames - 1, 1)
                    result = builder(u)
                    # Builders normally return (routes, pulse, heads). A
                    # builder that moves the camera every frame (currently
                    # just the fly-along) additionally returns a 4th
                    # element: that frame's camera state.
                    if len(result) == 4:
                        routes, pulse, heads, frame_camera = result
                    else:
                        routes, pulse, heads = result
                        frame_camera = None

                    page.evaluate(
                        "(data) => window.setFrame(data.routes, data.pulse, data.heads, data.camera)",
                        {'routes':routes, 'pulse':pulse, 'heads':heads, 'camera':frame_camera},
                    )
                    page.wait_for_timeout(int(1000 / FPS))

                    # A generous timeout here: a long capture run can leave the
                    # page under enough memory/GC pressure that a screenshot
                    # occasionally takes longer than Playwright's 30s default.
                    png = page.screenshot(type='png', timeout=60000)
                    frames.append(Image.open(io.BytesIO(png)).convert('RGB'))

                save_gif(frames, path)
                print(f"Wrote {path} ({os.path.getsize(path)/1e6:.1f} MB, {total_frames} frames)")

            def maybe_capture(path, total_frames, builder, camera=None):
                # Skip a GIF that's already on disk unless --force is passed,
                # so re-running the script while iterating on one animation
                # doesn't re-render the other one (which is slow: a fresh
                # page load + one screenshot per frame) for no reason.
                if path.exists() and not force:
                    print(f"Skipping {path.name} (already exists; pass --force to regenerate).")
                    return
                page = new_page()
                capture(page, path, total_frames, builder, camera=camera)
                page.close()

            # ---------------------------------------------------------------
            # LIVE / "ALIVE": the full route network is drawn from frame 0 and
            # never grows. On top of it, dots continuously travel the actual
            # routes -- each dot's progress through time is the real
            # recorded pacing (so it naturally slows/stops exactly where the
            # real ride did), and its trail length + stop ring additionally
            # react to the real recorded speed at that instant for a visible
            # "braking" cue.
            #
            # Moving dots are scheduled per corridor (sensor) into a fixed
            # number of "lanes" (LIVE_MAX_CONCURRENT_PER_ROUTE). Within a
            # lane, trips play back-to-back with a small gap, so a lane never
            # shows more than one dot at a time -- and a corridor never shows
            # more dots at once than it has lanes. Every trip on a corridor
            # IS eventually scheduled into some lane's queue and does get a
            # turn as that lane's cycle loops; what's bounded is how many
            # dots are visible on one corridor at the same instant, not
            # which trips ever get shown (the chronological GIF is a
            # different, non-looping record of the same completeness).
            # ---------------------------------------------------------------
            live_trips = [
                trip for trip in trips
                if trip['duration'] >= MIN_LIVE_TRIP_DURATION_S
            ]

            print(f"Live animation: {len(live_trips)} trips included; "
                  f"{len(trips) - len(live_trips)} very short trips omitted.")

            if not live_trips:
                raise RuntimeError('No trips long enough for the live animation.')

            for trip in live_trips:
                trip['live_duration'] = LIVE_MIN_ANIMATION_S + (trip['duration'] * LIVE_DURATION_SCALE)
                trip['max_speed'] = max((p[3] for p in trip['points']), default=0.0)

            route_groups = defaultdict(list)
            for trip in live_trips:
                route_groups[trip['sensor']].append(trip)

            # A lane's queue holds EVERY trip assigned to it -- no truncation.
            # Scheduling extra trips costs nothing (it's just bookkeeping: a
            # trip that isn't currently in its `live_duration` window simply
            # gets skipped in live_builder), so there's no reason to cut a
            # lane's queue short. Truncating used to mean each lane only ever
            # cycled through its first couple of trips, and the random phase
            # shift below only ever landed inside that same short opening
            # stretch -- so no matter how many times you regenerated, you'd
            # keep seeing the same small, chronologically-early subset (e.g.
            # a sensor's first trips of the day, which skews toward wherever
            # that sensor happens to start/end -- not a representative
            # sample of where it actually goes over the full day).
            #
            # Now the phase shift is drawn from the *entire* lane cycle
            # (sum of every trip's duration in that lane), so it can land
            # anywhere across a sensor's whole day, and every trip eventually
            # gets its turn as the cycle loops.
            scheduled_trips = []
            for sensor, group in route_groups.items():
                # Chronological order, so lanes fill up in the order the real
                # rides actually happened.
                group.sort(key=lambda t: t['start'])

                lanes = min(LIVE_MAX_CONCURRENT_PER_ROUTE, len(group))
                lane_queues = [[] for _ in range(lanes)]
                for i, trip in enumerate(group):
                    lane_queues[i % lanes].append(trip)

                for lane_index, queue in enumerate(lane_queues):
                    offset = 0.0
                    for trip in queue:
                        trip['start_offset'] = offset
                        offset += trip['live_duration'] + LIVE_ROUTE_GAP_S

                    lane_cycle_len = offset
                    # Per-lane phase shift so lanes/corridors desync from
                    # each other instead of all starting at u=0 together,
                    # and so which trips happen to be mid-animation at frame
                    # 0 varies across the lane's whole day rather than
                    # always being the queue's first entries.
                    phase_shift = (
                        random.Random(f"{sensor}_{lane_index}").uniform(0, lane_cycle_len)
                        if lane_cycle_len > 0 else 0.0
                    )
                    for trip in queue:
                        trip['cycle_len'] = lane_cycle_len
                        trip['start_offset'] = (trip['start_offset'] + phase_shift) % lane_cycle_len

                    scheduled_trips.extend(queue)

            print(f"Live animation: {len(scheduled_trips)} trips scheduled across "
                  f"{len(route_groups)} sensors (max {LIVE_MAX_CONCURRENT_PER_ROUTE} "
                  f"concurrent dot(s) per corridor). Every trip gets a turn in its lane's "
                  f"loop; a single {LIVE_TOTAL_SECONDS:.0f}s render just won't necessarily "
                  f"catch every lane mid-trip at once if a lane's full cycle runs longer "
                  f"than that.")

            live_frames = max(2, int(LIVE_TOTAL_SECONDS * FPS) + 1)

            def live_builder(u):
                routes, pulse, heads = [], [], []
                global_elapsed = u * LIVE_TOTAL_SECONDS

                # The network is always fully present -- this mode never
                # "grows"; only the moving dots animate on top of it.
                for trip in live_trips:
                    route = route_feature(trip, trip['points'], active=False, opacity=LIVE_BACKGROUND_OPACITY)
                    if route:
                        routes.append(route)

                for trip in scheduled_trips:
                    phase = (global_elapsed + trip['start_offset']) % trip['cycle_len']
                    if phase >= trip['live_duration']:
                        continue  # resting between repeats

                    progress = phase / trip['live_duration']
                    p = interpolate(trip['points'], progress)
                    speed = p[3]

                    # Real instantaneous speed (relative to this trip's own
                    # top speed) drives the trail length and the stop ring,
                    # so fast sections streak and braking/stopped sections
                    # pull in tight -- mirroring how the ride actually moved.
                    speed_ratio = 0.0 if trip['max_speed'] <= 0 else min(speed / trip['max_speed'], 1.0)
                    tail_width = 0.02 + 0.05 * speed_ratio
                    stopped = speed_ratio < LIVE_BRAKE_SPEED_RATIO

                    pulse_geom = pulse_segment(trip['points'], progress, tail_width=tail_width)
                    pulse_feature_obj = pulse_feature(trip, pulse_geom)
                    if pulse_feature_obj:
                        pulse.append(pulse_feature_obj)
                    heads.append(head_feature(trip, p, stopped=stopped))

                return routes, pulse, heads

            # ---------------------------------------------------------------
            # CHRONO: sorted by trip start time, one trip draws on at a time.
            # ---------------------------------------------------------------
            chrono_total = max(1, len(trips)) * CHRONO_SECONDS_PER_TRIP
            chrono_frames = max(2, int(chrono_total * FPS) + 1)

            def chronological_builder(u):
                # CHRONOLOGICAL MODE IS INTENTIONALLY DIFFERENT FROM LIVE MODE:
                # no moving dots and no travelling pulse. Each recorded trip is
                # simply drawn onto the city in chronological order and remains
                # visible once completed.
                routes, pulse, heads = [], [], []
                exact = u * len(trips)
                complete = min(int(exact), len(trips))
                frac = exact - complete

                # Keep all previously completed trips on the map.
                for i in range(complete):
                    trip = trips[i]
                    route = route_feature(trip, trip['points'], active=False, opacity=0.76)
                    if route:
                        routes.append(route)

                # Draw the next trip progressively. This is deliberately clean:
                # no head dot, no animated pulse, just a crisp line extending
                # along the actual route.
                if complete < len(trips):
                    trip = trips[complete]
                    reveal = min(1.0, frac)
                    visible = partial_points(trip['points'], reveal)
                    route = route_feature(trip, visible, active=True, opacity=0.98)
                    if route:
                        routes.append(route)

                return routes, pulse, heads

            # ---------------------------------------------------------------
            # GROWTH: same draw-on style as CHRONO, but ordered outward from
            # Marineterrein (ORIGIN_LON/ORIGIN_LAT) instead of by time -- see
            # compute_growth_order() for how the ordering is built.
            # ---------------------------------------------------------------
            growth_order = compute_growth_order(dedupe_for_growth(trips), ORIGIN_LON, ORIGIN_LAT)
            growth_total = max(1, len(growth_order)) * GROWTH_SECONDS_PER_TRIP
            growth_frames = max(2, int(growth_total * FPS) + 1)

            def growth_builder(u):
                routes, pulse, heads = [], [], []
                exact = u * len(growth_order)
                complete = min(int(exact), len(growth_order))
                frac = exact - complete

                for i in range(complete):
                    trip, _flip = growth_order[i]
                    route = route_feature(trip, trip['points'], active=False, opacity=0.76)
                    if route:
                        routes.append(route)

                if complete < len(growth_order):
                    trip, flip = growth_order[complete]
                    pts = reversed_points(trip['points']) if flip else trip['points']
                    reveal = min(1.0, frac)
                    visible = partial_points(pts, reveal)
                    route = route_feature(trip, visible, active=True, opacity=0.98)
                    if route:
                        routes.append(route)

                return routes, pulse, heads

            # ---------------------------------------------------------------
            # FLYOVER: the trips on the busiest corridor stitched into one
            # continuous route (per the user: several separate trip_ids can
            # really be legs of one bigger ride), with the camera flying
            # along it from start to end instead of sitting still.
            # ---------------------------------------------------------------
            flyover_sensor = max(
                route_groups,
                key=lambda s: (len(route_groups[s]), sum(trip_geo_length_m(t) for t in route_groups[s])),
            )
            flyover_legs = stitch_trips_into_route(route_groups[flyover_sensor])
            combined_route = build_combined_route(flyover_legs)

            print(f"Flyover route: sensor {flyover_sensor}, {len(flyover_legs)} leg(s) stitched "
                  f"({', '.join(t['id'] for t, _ in flyover_legs)}), "
                  f"{sum(trip_geo_length_m(t) for t, _ in flyover_legs)/1000:.2f} km total.")

            flyover_frames = max(2, int(FLYOVER_TOTAL_SECONDS * FPS) + 1)

            # Computed once for the whole flight -- see
            # compute_flyover_bearing(). The camera holds this heading for
            # every frame; it never turns to track the route's bends.
            flyover_bearing = compute_flyover_bearing(combined_route['points'])

            def flyover_builder(t):
                # t is linear frame-time (0->1); ease_in_out() turns that
                # into route progress u so the flight ramps up to cruising
                # speed and eases back down instead of starting/stopping
                # instantly.
                u = ease_in_out(t)

                # Full stitched route always faintly visible for context,
                # same LIVE_BACKGROUND_OPACITY treatment as the network in
                # live_builder -- so the viewer can see the whole coastline
                # shape while the camera is only partway along it.
                routes = []
                bg = route_feature(combined_route, combined_route['points'], active=False, opacity=LIVE_BACKGROUND_OPACITY)
                if bg:
                    routes.append(bg)

                traveled = partial_points(combined_route['points'], max(u, FLYOVER_MIN_VISIBLE_FRACTION))
                active = route_feature(
                    combined_route, traveled, active=True, opacity=0.98,
                    core_width=FLYOVER_ACTIVE_CORE_WIDTH,
                    underlay_width=FLYOVER_ACTIVE_UNDERLAY_WIDTH,
                    underlay_opacity=FLYOVER_ACTIVE_UNDERLAY_OPACITY,
                )
                if active:
                    routes.append(active)

                camera = compute_follow_camera(combined_route['points'], u, flyover_bearing)
                return routes, [], [], camera

            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            # Each capture gets its own fresh page (and the previous one is
            # closed first) so a long first capture can't leave the browser
            # short on memory or otherwise sluggish for the second one.
            # maybe_capture() additionally skips a GIF entirely if it's
            # already on disk (see its definition above), so re-running
            # while iterating on just one of the two is cheap.
            maybe_capture(
                output_dir / 'cascais_trips_live.gif',
                live_frames,
                live_builder,
            )

            # A different animation from cascais_trips_live.gif (not just a
            # different camera on the same builder): the route draws on
            # progressively as the camera flies along it, rather than the
            # full network being visible with dots looping over it -- see
            # flyover_builder() above. The `camera=` here just pre-loads
            # tiles at the starting position before the per-frame loop
            # starts moving in small steps; flyover_builder supplies the
            # actual per-frame camera itself.
            maybe_capture(
                output_dir / 'cascais_trips_live_flyover.gif',
                flyover_frames,
                flyover_builder,
                camera=compute_follow_camera(combined_route['points'], 0.0, flyover_bearing),
            )

            maybe_capture(
                output_dir / 'cascais_trips_chronological.gif',
                chrono_frames,
                chronological_builder,
            )

            maybe_capture(
                output_dir / 'cascais_trips_growth.gif',
                growth_frames,
                growth_builder,
            )

            browser.close()


if __name__ == '__main__':
    args = sys.argv[1:]
    force = '--force' in args
    args = [a for a in args if a != '--force']

    if len(args) != 1:
        raise SystemExit(
            'Usage: python make_cascais_animation.py trips.geojson [--force]\n'
            '  --force  regenerate both GIFs even if they already exist on disk'
        )

    # Write outputs beside the script when running locally on the Mac.
    render(Path(__file__).resolve().parent, args[0], force=force)
