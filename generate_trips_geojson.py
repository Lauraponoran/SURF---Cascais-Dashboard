#!/usr/bin/env python3
"""
generate_trips_geojson.py

Builds trips.geojson entirely from Supabase — this is the ONLY way trips
get onto the map. There is no manual/local CSV entry point; every trip
must exist in the `trips` table and be reachable via `raw_data` / `gnss` /
`data1`, exactly as the sensor pipeline writes it.

Supabase trips use gnss.speed directly (already in km/h) with a 5-point
rolling average to smooth GPS noise. Road quality is calculated from
decoded acc_y via road_quality_calculator.py. Braking is detected from
consecutive speed deltas (see BRAKING_DECEL_THRESHOLD_GPS_KMH_S below).

Run manually:      python generate_trips_geojson.py
Run automatically:  see .github/workflows/generate-trips.yml (scheduled)
"""

import bisect
import json
import math
import os
from datetime import timedelta
from pathlib import Path

import numpy as np
import psycopg2
from dotenv import load_dotenv

load_dotenv()

try:
    from road_quality_calculator import calculate_road_quality
    ROAD_QUALITY_AVAILABLE = True
except ImportError:
    print("⚠️  road_quality_calculator.py not found — road_quality will be 0")
    ROAD_QUALITY_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────

OUTPUT_FILE        = "trips.geojson"
MAX_SPEED_KMH      = 40
MAX_GPS_JUMP_M     = 1000
TRIM_M             = 100
INITIAL_DAYS       = None   # None = all trips; set e.g. 90 to limit
STATEMENT_TIMEOUT  = "30s"
SPEED_SMOOTH_WIN   = 5      # rolling average window for gnss speed

# Braking detection — GNSS speed is pre-smoothed, so peak decels are lower
# than raw wheel-rotation data would show.
BRAKING_INTENSITY_CAP_KMH_S       = 50   # hard ceiling to suppress data artefacts
BRAKING_DECEL_THRESHOLD_GPS_KMH_S = 2.0  # tune up to 2.5 to reduce false positives

SPEED_JUMP_THRESHOLD_KMH      = 20   # implausible inter-segment speed change → reset
MIN_SEGMENT_TIME_S             = 0.5  # GPS fixes are ~1 s apart; below this is noise

# ── DB connection ─────────────────────────────────────────────────────────────

def get_connection():
    conn = psycopg2.connect(
        host            = os.environ["SUPABASE_HOST"],
        port            = int(os.environ.get("SUPABASE_PORT", 6543)),
        dbname          = os.environ["SUPABASE_DB"],
        user            = os.environ["SUPABASE_USER"],
        password        = os.environ["SUPABASE_PASSWORD"],
        sslmode         = "require",
        connect_timeout = 30,
    )
    conn.autocommit = True
    return conn

# ── Trip list ─────────────────────────────────────────────────────────────────

TRIPS_QUERY = """
select id, trip_start, trip_end, system_id, wheel_diam
from public.trips
{where}
order by trip_start desc
"""

DEFAULT_WHEEL_DIAM_INCH = 28.0

def fetch_trips(cur):
    where = ""
    if INITIAL_DAYS:
        where = f"where trip_start >= now() - interval '{INITIAL_DAYS} days'"
    cur.execute(TRIPS_QUERY.format(where=where))
    return cur.fetchall()

# ── Queries ───────────────────────────────────────────────────────────────────

# 1. Decoded acc_y from raw_data blobs (for road quality)
RAW_QUERY = """
with params as (
    select %(trip_id)s::int as trip_id
),
marker_bounds as (
    select
        p.trip_id,
        (select d1.samples from public.data1 d1
         where d1.trip_id = p.trip_id and d1.marker = 9
         order by d1.samples limit 1) as start_sample,
        (select d1.samples from public.data1 d1
         where d1.trip_id = p.trip_id and d1.marker = 10
         order by d1.samples limit 1) as end_sample
    from params p
),
-- Parse each raw_data blob exactly once. Without MATERIALIZED, Postgres is
-- free to inline this subquery and re-run string_to_array/convert_from
-- once per generate_series value below (20x per row) instead of once per
-- row -- see csv_export_query.txt for the original diagnosis/benchmark.
parsed as materialized (
    select rd.samples,
           string_to_array(
               replace(replace(convert_from(rd.data, 'UTF8'), '[', ''), ']', ''),
               ','
           ) as vals
    from public.raw_data rd
    join marker_bounds mb on mb.trip_id = rd.trip_id
    where rd.trip_id = (select trip_id from params)
      and rd.samples >= mb.start_sample
      and rd.samples - 9 <= mb.end_sample
),
x as (
    select
        rd.samples - 9 + gs.i as output_samples,
        trim(vals[gs.i * 4 + 1])::integer as acc_low,
        trim(vals[gs.i * 4 + 2])::integer as acc_high
    from parsed rd
    cross join generate_series(0, 9) as gs(i)
)
select
    x.output_samples as samples,
    round((
        case
            when (x.acc_low + x.acc_high * 256) >= 32768
                then (x.acc_low + x.acc_high * 256) - 65536
            else (x.acc_low + x.acc_high * 256)
        end
    ) / 1024.0, 3) as acc_y
from x
join marker_bounds mb on true
where x.output_samples >= mb.start_sample
  and x.output_samples <= mb.end_sample
order by x.output_samples
"""

# 2. GNSS points with speed (already in km/h)
GNSS_QUERY = """
select latitude, longitude, speed, "timestamp"
from public.gnss
where trip_id = %(trip_id)s
  and latitude  is not null
  and longitude is not null
order by "timestamp"
"""

# 3. data1 anchors for sample→timestamp mapping (road quality lookup)
#    h_rot is needed for crash detection's wheel-rotation checks (was the
#    wheel turning before impact, speed-at-impact estimate).
DATA1_QUERY = """
select samples, "timestamp", h_rot
from public.data1
where trip_id = %(trip_id)s
order by samples
"""

def fetch_trip_data(cur, trip_id):
    cur.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")

    cur.execute(RAW_QUERY, {"trip_id": trip_id})
    raw_rows = cur.fetchall()
    raw_cols = [d[0] for d in cur.description]

    cur.execute(GNSS_QUERY, {"trip_id": trip_id})
    gnss_rows = cur.fetchall()
    gnss_cols = [d[0] for d in cur.description]

    cur.execute(DATA1_QUERY, {"trip_id": trip_id})
    d1_rows = cur.fetchall()
    d1_cols = [d[0] for d in cur.description]

    return raw_rows, raw_cols, gnss_rows, gnss_cols, d1_rows, d1_cols

# ── Road quality ──────────────────────────────────────────────────────────────

def compute_road_quality_lookup(raw_rows, raw_cols, d1_rows, d1_cols):
    """
    Build a timestamp → road_quality lookup using decoded acc_y samples.
    Maps each raw sample to a timestamp via data1 anchors, then returns
    a function: timestamp → quality score (1-5) or 0 if unavailable.
    """
    if not ROAD_QUALITY_AVAILABLE or not raw_rows or len(raw_rows) < 200:
        return None

    raw = [dict(zip(raw_cols, r)) for r in raw_rows]
    d1  = [dict(zip(d1_cols,  r)) for r in d1_rows]

    if not d1:
        return None

    d1_samples = [r["samples"]   for r in d1]
    d1_ts      = [r["timestamp"] for r in d1]

    def interp_ts(sample_idx):
        pos = bisect.bisect_left(d1_samples, sample_idx)
        if pos == 0:
            anchor = 0
        elif pos >= len(d1_samples):
            anchor = len(d1_samples) - 1
        else:
            before, after = pos - 1, pos
            anchor = before if abs(d1_samples[before] - sample_idx) <= abs(d1_samples[after] - sample_idx) else after
        diff = sample_idx - d1_samples[anchor]
        return d1_ts[anchor] + timedelta(milliseconds=diff * 20)

    acc_y = np.array([float(r["acc_y"] or 0) for r in raw])
    try:
        rq_data = calculate_road_quality(acc_y, window_size=100, overlap=0.5)
    except Exception as e:
        print(f"    ⚠️  Road quality failed: {e}")
        return None

    quality_scores = rq_data["road_quality"]
    time_windows   = rq_data["time_windows"]
    window_ts      = [interp_ts(int(w)) for w in time_windows]

    def lookup(ts):
        if not window_ts:
            return 0
        diffs = [abs((t - ts).total_seconds()) for t in window_ts]
        return int(quality_scores[diffs.index(min(diffs))])

    return lookup

# ── Braking detection (API path) ──────────────────────────────────────────────

def calculate_braking_intensity(prev_speed_kmh, curr_speed_kmh, time_diff_s):
    """
    Return deceleration rate (km/h/s), capped at BRAKING_INTENSITY_CAP_KMH_S.
    Returns 0.0 when accelerating, holding speed, or time_diff is too small.
    """
    if time_diff_s is None or time_diff_s < MIN_SEGMENT_TIME_S:
        return 0.0
    delta = prev_speed_kmh - curr_speed_kmh  # positive = deceleration
    if delta <= 0:
        return 0.0
    return round(min(delta / time_diff_s, BRAKING_INTENSITY_CAP_KMH_S), 2)

# ── Geometry helpers ──────────────────────────────────────────────────────────

def haversine(a, b):
    R = 6_371_000
    lat1 = math.radians(float(a["latitude"]))
    lat2 = math.radians(float(b["latitude"]))
    dlon = math.radians(float(b["longitude"]) - float(a["longitude"]))
    dlat = lat2 - lat1
    x = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))

# ── Crash/fall detection (API path) — ported from Marineterrein-Commutes ────
CRASH_IMPACT_THRESHOLD_G      = 6.0
CRASH_CLUSTER_GAP_S           = 1.0
CRASH_STALL_THRESHOLD_S       = 1.0
CRASH_STOP_SEARCH_WINDOW_S    = 3.0
# See Marineterrein-Commutes generate_trips_geojson.py for the full history
# behind these thresholds (calibrated against confirmed real falls there).
CRASH_SPEED_LOOKBACK_MAX_S    = 10.0
CRASH_MAX_RECOVERY_S          = 120.0
CRASH_SETTLE_WINDOW_S         = CRASH_STOP_SEARCH_WINDOW_S
CRASH_SETTLE_DURATION_S       = 1.0
CRASH_SETTLE_MAX_RANGE_G      = 3.0
CRASH_GPS_STILL_MAX_SPEED_KMH = 5.5
CRASH_COORD_STALL_RADIUS_M    = 5.0
CRASH_BASELINE_WINDOW_S       = 3.0
CRASH_BASELINE_DELTA_MIN_G    = 0.0
CRASH_BURST_GAP_S             = CRASH_SETTLE_WINDOW_S

CRASH_SEVERITY_BANDS = [
    (6.0,  8.0,  "Minor"),
    (8.0,  11.0, "Hard"),
    (11.0, None, "Severe"),
]

# speed_at_impact_kmh comes from a wheel-rotation estimate, not GPS. An
# estimate at or above this cap drops the whole crash event rather than
# being clamped and reported as a fake, exact "40 km/h" — see
# Marineterrein-Commutes for the full rationale.
CRASH_SPEED_CAP_KMH = 40


def _crash_severity(peak_g):
    mag = abs(peak_g)
    for low, high, label in CRASH_SEVERITY_BANDS:
        if mag >= low and (high is None or mag < high):
            return label
    return "Minor"


def _acc_y_settles_after_impact(points, spike_idx, end_idx, n, t_diff):
    """
    True if a post-impact acc_y window:
      1. stays within CRASH_SETTLE_MAX_RANGE_G for >= CRASH_SETTLE_DURATION_S
      2. has a mean shifted from the pre-impact baseline by at least
         CRASH_BASELINE_DELTA_MIN_G
    """
    pre_samples = []
    k = spike_idx - 1

    while k >= 0 and t_diff(points[k], points[spike_idx]) <= CRASH_BASELINE_WINDOW_S:
        pre_samples.append(points[k]["acc_y"])
        k -= 1

    if not pre_samples:
        return False

    baseline_mean = sum(pre_samples) / len(pre_samples)

    win_start = end_idx
    k = end_idx

    while k < n:
        while (
            win_start < k
            and t_diff(points[win_start], points[k]) > CRASH_SETTLE_DURATION_S
        ):
            win_start += 1

        window_duration = t_diff(points[win_start], points[k])

        if (
            win_start < k
            and window_duration >= CRASH_SETTLE_DURATION_S * 0.9
        ):
            window = points[win_start:k + 1]
            vals = [p["acc_y"] for p in window]
            value_range = max(vals) - min(vals)

            if value_range <= CRASH_SETTLE_MAX_RANGE_G:
                settled_mean = sum(vals) / len(vals)
                delta = abs(settled_mean - baseline_mean)

                if delta >= CRASH_BASELINE_DELTA_MIN_G:
                    return True

        if t_diff(points[end_idx], points[k]) > CRASH_SETTLE_WINDOW_S:
            break

        k += 1

    return False

def _gps_stops_after_impact(gnss, gnss_ts, onset_ts):
    """
    True if, within CRASH_SETTLE_WINDOW_S of onset_ts, GPS confirms the bike is stationary via:
      1. Reported speed <= CRASH_GPS_STILL_MAX_SPEED_KMH for at least CRASH_SETTLE_DURATION_S.
      2. Consecutive coordinate spread (haversine) <= CRASH_COORD_STALL_RADIUS_M over that window.
    """
    if not gnss_ts or onset_ts is None:
        return False

    pos = bisect.bisect_left(gnss_ts, onset_ts)
    idx = pos
    settle_fixes = []

    while idx < len(gnss) and (gnss[idx]["timestamp"] - onset_ts).total_seconds() <= CRASH_SETTLE_WINDOW_S:
        settle_fixes.append(gnss[idx])
        idx += 1

    if len(settle_fixes) < 2:
        return False

    run_start_ts = None
    valid_speed_window = False

    for fix in settle_fixes:
        speed = float(fix["speed"] or 0)
        if speed <= CRASH_GPS_STILL_MAX_SPEED_KMH:
            if run_start_ts is None:
                run_start_ts = fix["timestamp"]
            elif (fix["timestamp"] - run_start_ts).total_seconds() >= CRASH_SETTLE_DURATION_S:
                valid_speed_window = True
                break
        else:
            run_start_ts = None

    if not valid_speed_window:
        return False

    max_dist = 0.0
    for i in range(len(settle_fixes)):
        for j in range(i + 1, len(settle_fixes)):
            d = haversine(settle_fixes[i], settle_fixes[j])
            if d > max_dist:
                max_dist = d

    return max_dist <= CRASH_COORD_STALL_RADIUS_M


def detect_crash_events_api(raw_rows, raw_cols, d1_rows, d1_cols,
                             gnss_rows, gnss_cols, trip_id, wheel_diam_mm):
    """
    Detect crash/fall events for a Supabase-fetched trip. See
    Marineterrein-Commutes generate_trips_geojson.py for the full two-pass
    algorithm writeup (cluster impact spikes into "throws", group throws
    into bursts, gate once per burst on settle/GPS-stop/was-moving).
    """
    if not raw_rows or not d1_rows:
        return []

    raw = sorted(
        (dict(zip(raw_cols, r)) for r in raw_rows),
        key=lambda r: r["samples"]
    )
    d1 = sorted(
        (dict(zip(d1_cols, r)) for r in d1_rows),
        key=lambda r: r["samples"]
    )
    gnss = [dict(zip(gnss_cols, r)) for r in gnss_rows] if gnss_rows else []

    d1_samples = [r["samples"] for r in d1]
    gnss_ts    = [r["timestamp"] for r in gnss] if gnss else []

    def nearest_d1(sample_idx):
        pos = bisect.bisect_left(d1_samples, sample_idx)
        if pos == 0:
            return d1[0]
        if pos >= len(d1_samples):
            return d1[-1]
        before, after = d1[pos - 1], d1[pos]
        return before if abs(before["samples"] - sample_idx) <= abs(after["samples"] - sample_idx) else after

    def nearest_gnss(ts):
        if not gnss_ts or ts is None:
            return None
        pos = bisect.bisect_left(gnss_ts, ts)
        if pos == 0:
            return gnss[0]
        if pos >= len(gnss_ts):
            return gnss[-1]
        before, after = gnss[pos - 1], gnss[pos]
        return before if abs((before["timestamp"] - ts).total_seconds()) <= abs((after["timestamp"] - ts).total_seconds()) else after

    points = []
    for r in raw:
        anchor = nearest_d1(r["samples"])
        points.append({
            "samples": r["samples"],
            "acc_y":   float(r["acc_y"] or 0),
            "time":    anchor["timestamp"],
            "hrot":    anchor.get("h_rot") or 0,
        })

    if len(points) < 3:
        return []

    def t_diff(a, b):
        sample_estimate = (b["samples"] - a["samples"]) * 0.02  # 50Hz fallback
        if a["time"] and b["time"]:
            delta = (b["time"] - a["time"]).total_seconds()
            if a["samples"] == b["samples"]:
                return delta
            if delta != 0:
                return max(delta, sample_estimate)
        return sample_estimate

    wheel_circumference_m = (wheel_diam_mm / 1000) * math.pi if wheel_diam_mm else None

    def wheel_rotation_lookback(idx):
        """
        Walk back from points[idx] until hitting a data1 anchor with a
        DIFFERENT h_rot reading, capped at CRASH_SPEED_LOOKBACK_MAX_S.
        Returns (hrot_diff, lb_time_s).
        """
        lb_idx = idx
        while (
            lb_idx > 0
            and points[lb_idx]["hrot"] == points[idx]["hrot"]
            and t_diff(points[lb_idx], points[idx]) < CRASH_SPEED_LOOKBACK_MAX_S
        ):
            lb_idx -= 1
        hrot_diff = points[idx]["hrot"] - points[lb_idx]["hrot"]
        lb_time_s = t_diff(points[lb_idx], points[idx])
        return hrot_diff, lb_time_s

    # ── Pass 1: cluster raw over-threshold samples into discrete throws ────
    throws = []
    i, n = 0, len(points)
    while i < n:
        if abs(points[i]["acc_y"]) >= CRASH_IMPACT_THRESHOLD_G:
            start = i
            end = i
            j = i + 1
            while j < n:
                if t_diff(points[end], points[j]) > CRASH_CLUSTER_GAP_S:
                    break
                if abs(points[j]["acc_y"]) >= CRASH_IMPACT_THRESHOLD_G:
                    end = j
                j += 1

            peak_idx = max(range(start, end + 1), key=lambda k: abs(points[k]["acc_y"]))
            throws.append({
                "start":    start,
                "end":      end,
                "peak_idx": peak_idx,
                "peak_g":   points[peak_idx]["acc_y"],
                "onset_ts": points[start]["time"],
            })
            i = end + 1
        else:
            i += 1

    if not throws:
        return []

    # ── Pass 2: group throws into bursts, gate once per burst ──────────────
    bursts = [[throws[0]]]
    for throw in throws[1:]:
        prev_end = bursts[-1][-1]["end"]
        if t_diff(points[prev_end], points[throw["start"]]) <= CRASH_BURST_GAP_S:
            bursts[-1].append(throw)
        else:
            bursts.append([throw])

    events = []
    for burst in bursts:
        first_throw, last_throw = burst[0], burst[-1]

        acc_ok = _acc_y_settles_after_impact(
            points, first_throw["start"], last_throw["end"], n, t_diff
        )
        gps_ok = _gps_stops_after_impact(gnss, gnss_ts, last_throw["onset_ts"])

        moving_ok = True
        if wheel_circumference_m:
            hrot_diff_pre, _ = wheel_rotation_lookback(first_throw["start"])
            moving_ok = hrot_diff_pre > 0

        if not (acc_ok and gps_ok and moving_ok):
            continue

        for throw in burst:
            start, end   = throw["start"], throw["end"]
            peak_idx     = throw["peak_idx"]
            peak_g       = throw["peak_g"]
            onset_ts     = throw["onset_ts"]

            b_idx = start
            while b_idx > max(0, start - 25) and abs(points[b_idx]["acc_y"]) > 1.0:
                b_idx -= 1
            suddenness_s = round(t_diff(points[b_idx], points[peak_idx]), 2)

            speed_kmh = None
            if wheel_circumference_m:
                hrot_diff, lb_time_s = wheel_rotation_lookback(start)
                if hrot_diff > 0 and lb_time_s and lb_time_s >= 0.02:
                    revolutions   = hrot_diff / 2.0
                    distance_m    = revolutions * wheel_circumference_m
                    raw_speed_kmh = (distance_m / lb_time_s) * 3.6
                    if raw_speed_kmh >= CRASH_SPEED_CAP_KMH:
                        continue
                    speed_kmh = round(raw_speed_kmh, 1)

            came_to_stop = False
            recovery_time_s = None
            unresolved = False
            cursor = end
            while cursor < n - 1 and t_diff(points[end], points[cursor]) <= CRASH_STOP_SEARCH_WINDOW_S:
                hrot_now = points[cursor]["hrot"]
                k = cursor + 1
                while k < n and points[k]["hrot"] == hrot_now:
                    k += 1
                if k >= n:
                    unresolved = True
                    break
                gap = t_diff(points[cursor], points[k])
                if gap >= CRASH_STALL_THRESHOLD_S:
                    came_to_stop = True
                    recovery_time_s = round(gap, 2)
                    break
                cursor = k

            fix = nearest_gnss(onset_ts)
            if fix is None:
                continue

            if not came_to_stop or recovery_time_s > CRASH_MAX_RECOVERY_S:
                continue

            events.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(fix["longitude"]), float(fix["latitude"])],
                },
                "properties": {
                    "event_type":           "crash",
                    "trip_id":              trip_id,
                    "peak_g":               round(peak_g, 2),
                    "severity":             _crash_severity(peak_g),
                    "suddenness_s":         suddenness_s,
                    "speed_at_impact_kmh":  speed_kmh,
                    "came_to_stop":         came_to_stop,
                    "recovery_time_s":      recovery_time_s,
                    "unresolved":           unresolved,
                    "time_str":             onset_ts.strftime("%H:%M:%S") if onset_ts else None,
                    "location_approximate": True,
                },
            })

    return events


def privacy_trim(rows):
    if len(rows) < 2:
        return rows
    cum, start_idx = 0, 0
    for k in range(1, len(rows)):
        d = haversine(rows[k - 1], rows[k])
        if d > 500:
            continue
        cum += d
        if cum >= TRIM_M:
            start_idx = k
            break
    cum, end_idx = 0, len(rows) - 1
    for k in range(len(rows) - 1, 0, -1):
        d = haversine(rows[k], rows[k - 1])
        if d > 500:
            continue
        cum += d
        if cum >= TRIM_M:
            end_idx = k
            break
    return rows[start_idx:end_idx] if start_idx < end_idx else rows

# ── Feature builder ───────────────────────────────────────────────────────────

def rows_to_features(gnss_rows, gnss_cols, raw_rows, raw_cols,
                     d1_rows, d1_cols, trip_id, db_trip_id, wheel_diam_mm):
    """
    One LineString per consecutive gnss point pair.
    Speed   = smoothed gnss.speed (already in km/h), capped at MAX_SPEED_KMH.
    Quality = from decoded acc_y via timestamp lookup.
    Braking = detected from consecutive speed deltas using GPS-appropriate threshold.
              GPS speed is pre-smoothed so peak decels are lower (~2-3 km/h/s max).
    """
    if not gnss_rows:
        return []

    gnss    = [dict(zip(gnss_cols, r)) for r in gnss_rows]
    trimmed = privacy_trim(gnss)
    if len(trimmed) < 2:
        return []

    # 5-point rolling average to smooth GPS speed noise
    raw_speeds = [min(float(r["speed"] or 0), MAX_SPEED_KMH) for r in trimmed]
    smoothed   = []
    w          = SPEED_SMOOTH_WIN
    for k in range(len(raw_speeds)):
        start = max(0, k - w // 2)
        end   = min(len(raw_speeds), k + w // 2 + 1)
        smoothed.append(sum(raw_speeds[start:end]) / (end - start))

    quality_lookup = compute_road_quality_lookup(raw_rows, raw_cols, d1_rows, d1_cols)

    features       = []
    prev_speed_kmh = None  # rolling speed for braking detection

    for i in range(len(trimmed) - 1):
        a = trimmed[i]
        b = trimmed[i + 1]

        dist = haversine(a, b)
        if dist > MAX_GPS_JUMP_M or dist == 0:
            # GPS jump — treat as discontinuity in the speed series
            prev_speed_kmh = None
            continue

        speed_kmh   = min((smoothed[i] + smoothed[i + 1]) / 2, MAX_SPEED_KMH)
        time_diff_s = (b["timestamp"] - a["timestamp"]).total_seconds() \
                      if a["timestamp"] and b["timestamp"] else None
        road_quality = quality_lookup(a["timestamp"]) \
                       if quality_lookup and a["timestamp"] else 0

        # Braking detection — use GPS-appropriate threshold (smoothed speed
        # caps real decels at ~3 km/h/s, far below the CSV threshold of 40)
        braking_intensity = 0.0
        if prev_speed_kmh is not None and time_diff_s is not None:
            if abs(speed_kmh - prev_speed_kmh) > SPEED_JUMP_THRESHOLD_KMH:
                # Implausible speed jump — reset series, don't flag as braking
                prev_speed_kmh = speed_kmh
            else:
                braking_intensity = calculate_braking_intensity(
                    prev_speed_kmh, speed_kmh, time_diff_s
                )

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [float(a["longitude"]), float(a["latitude"])],
                    [float(b["longitude"]), float(b["latitude"])],
                ],
            },
            "properties": {
                "trip_id":           trip_id,
                "db_trip_id":        db_trip_id,
                "timestamp":         a["timestamp"].isoformat() if a["timestamp"] else None,
                "Speed":             round(speed_kmh, 1),
                "marker":            0,
                "Acc Y (g)":         0,
                "road_quality":      road_quality,
                "hrot_diff":         0,
                "sample_diff":       0,
                "time_diff_s":       round(time_diff_s, 3) if time_diff_s is not None else None,
                "gps_distance_m":    round(dist, 1),
                "wheel_diameter_mm": wheel_diam_mm,
                "braking_intensity": braking_intensity,
                "is_braking":        braking_intensity >= BRAKING_DECEL_THRESHOLD_GPS_KMH_S,
            },
        })

        prev_speed_kmh = speed_kmh

    return features

def make_trip_id(system_id, db_trip_id):
    hex_slug = format(int(system_id) & 0xFFFFFFFFFFFFFFFF, 'X')[-5:]
    return f"{hex_slug}_Trip{db_trip_id}"

# ── Fetch all trips from Supabase ───────────────────────────────────────────

def load_remote_trips():
    print("\n🔌 Connecting to Supabase…")
    try:
        conn = get_connection()
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return []

    features = []
    skipped  = []
    cur      = conn.cursor()
    trips    = fetch_trips(cur)
    print(f"📋 Found {len(trips)} trips in Supabase")

    for trip_row in trips:
        db_id, trip_start, trip_end, system_id, wheel_diam = trip_row
        trip_id = make_trip_id(system_id, db_id)

        try:
            wheel_diam_mm = float(wheel_diam) * 25.4 if wheel_diam else DEFAULT_WHEEL_DIAM_INCH * 25.4
        except (TypeError, ValueError):
            wheel_diam_mm = DEFAULT_WHEEL_DIAM_INCH * 25.4

        try:
            raw_rows, raw_cols, gnss_rows, gnss_cols, d1_rows, d1_cols = fetch_trip_data(cur, db_id)

            if not gnss_rows:
                print(f"  ⚠️  Trip {db_id} ({trip_id}): no gnss rows — skipping")
                continue

            new_feats = rows_to_features(
                gnss_rows, gnss_cols,
                raw_rows,  raw_cols,
                d1_rows,   d1_cols,
                trip_id, db_id, wheel_diam_mm
            )

            braking_count = sum(1 for f in new_feats if f["properties"]["is_braking"])

            crash_feats = detect_crash_events_api(
                raw_rows, raw_cols, d1_rows, d1_cols,
                gnss_rows, gnss_cols, trip_id, wheel_diam_mm
            )
            new_feats.extend(crash_feats)

            features.extend(new_feats)
            print(f"  ✅ {trip_id}: {len(gnss_rows)} gnss pts → {len(new_feats)} segments"
                  + (f" | 🛑 {braking_count} braking events" if braking_count else "")
                  + (f" | 🚨 {len(crash_feats)} crash events" if crash_feats else ""))

        except Exception as e:
            print(f"  ❌ {trip_id}: {e}")
            skipped.append(trip_id)

    cur.close()
    conn.close()

    if skipped:
        print(f"\n⚠️  {len(skipped)} trip(s) skipped:")
        for t in skipped:
            print(f"   {t}")

    return features

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    all_features = load_remote_trips()

    geojson = {"type": "FeatureCollection", "features": all_features}
    with open(OUTPUT_FILE, "w") as f:
        json.dump(geojson, f, separators=(",", ":"))

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    trip_ids = set(f['properties']['trip_id'] for f in all_features)
    print(f"\n✅ Written {OUTPUT_FILE}")
    print(f"   Trips        : {len(trip_ids)}")
    print(f"   Total segments: {len(all_features)} ({size_kb:.0f} KB)")

    # Braking summary
    total_braking = sum(1 for f in all_features if f['properties'].get('is_braking'))
    print(f"   Total braking events: {total_braking}")

    # Crash summary
    total_crashes = sum(1 for f in all_features if f['properties'].get('event_type') == 'crash')
    print(f"   Total crash/fall events: {total_crashes}")

if __name__ == "__main__":
    main()
