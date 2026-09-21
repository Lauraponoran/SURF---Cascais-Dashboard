#!/usr/bin/env python3
"""
road_segments_averaged.py

Reads trips.geojson (which merges both local CSV trips and remote Supabase trips)
and produces road_segments_averaged.json with averaged speed, road quality,
and composite scores per road segment.

Run AFTER generate_trips_geojson.py:
  python generate_trips_geojson.py
  python road_segments_averaged.py
"""

import json
import math
from pathlib import Path


# ── Geometry helpers ──────────────────────────────────────────────────────────

def haversine_distance(lon1, lat1, lon2, lat2):
    """Calculate distance between two points in meters."""
    R = 6_371_000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def calculate_bearing(lon1, lat1, lon2, lat2):
    """Calculate bearing between two points in degrees (0-360)."""
    dlon  = math.radians(lon2 - lon1)
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    y = math.sin(dlon) * math.cos(lat2r)
    x = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def are_segments_similar(coord1a, coord1b, coord2a, coord2b,
                          distance_threshold=50, bearing_threshold=15):
    """Return True if two segments share a similar midpoint and bearing."""
    mid1_lon = (coord1a[0] + coord1b[0]) / 2
    mid1_lat = (coord1a[1] + coord1b[1]) / 2
    mid2_lon = (coord2a[0] + coord2b[0]) / 2
    mid2_lat = (coord2a[1] + coord2b[1]) / 2

    if haversine_distance(mid1_lon, mid1_lat, mid2_lon, mid2_lat) > distance_threshold:
        return False

    b1 = calculate_bearing(coord1a[0], coord1a[1], coord1b[0], coord1b[1])
    b2 = calculate_bearing(coord2a[0], coord2a[1], coord2b[0], coord2b[1])
    diff = abs(b1 - b2)
    if diff > 180:
        diff = 360 - diff
    return diff < bearing_threshold


def merge_segments(segments_list):
    """Merge spatially similar segments into consolidated ones."""
    if not segments_list:
        return []

    merged = []
    used   = set()

    for i, seg1 in enumerate(segments_list):
        if i in used:
            continue
        group = [seg1]
        used.add(i)

        for j, seg2 in enumerate(segments_list):
            if j in used or j <= i:
                continue
            if are_segments_similar(
                seg1['coords'][0], seg1['coords'][1],
                seg2['coords'][0], seg2['coords'][1],
            ):
                group.append(seg2)
                used.add(j)

        all_speeds    = []
        all_qualities = []
        all_trips     = set()
        for seg in group:
            all_speeds.extend(seg['speeds'])
            all_qualities.extend(seg['qualities'])
            all_trips.update(seg['trips'])

        avg_lon1 = sum(s['coords'][0][0] for s in group) / len(group)
        avg_lat1 = sum(s['coords'][0][1] for s in group) / len(group)
        avg_lon2 = sum(s['coords'][1][0] for s in group) / len(group)
        avg_lat2 = sum(s['coords'][1][1] for s in group) / len(group)

        merged.append({
            'coords':    ([avg_lon1, avg_lat1], [avg_lon2, avg_lat2]),
            'speeds':    all_speeds,
            'qualities': all_qualities,
            'trips':     all_trips,
        })

    return merged


# ── Main processing ───────────────────────────────────────────────────────────

def process_trips_geojson(input_file="trips.geojson"):
    """
    Read trips.geojson (merged local + remote) and aggregate road segments.
    This replaces the old glob-based approach so all trips are included.
    """
    print(f"📂 Loading {input_file}…")
    try:
        data = json.loads(Path(input_file).read_text())
    except Exception as e:
        print(f"❌ Could not load {input_file}: {e}")
        return None

    features = data.get('features', [])
    print(f"   {len(features)} segments found")

    if not features:
        print("❌ No features in input file.")
        return None

    all_segments = []
    trip_ids     = set()

    for feature in features:
        if feature['geometry']['type'] != 'LineString':
            continue

        coords  = feature['geometry']['coordinates']
        props   = feature['properties']
        trip_id = props.get('trip_id', 'unknown')
        trip_ids.add(trip_id)

        speed   = props.get('Speed', props.get('speed', 0))
        quality = props.get('road_quality', 0)

        for i in range(len(coords) - 1):
            coord1 = coords[i]
            coord2 = coords[i + 1]

            dist = haversine_distance(coord1[0], coord1[1], coord2[0], coord2[1])
            if dist < 5:   # skip segments shorter than 5 m
                continue

            all_segments.append({
                'coords':    (coord1, coord2),
                'speeds':    [float(speed)],
                'qualities': [int(quality)] if quality and quality > 0 else [],
                'trips':     {trip_id},
            })

    print(f"   {len(all_segments)} raw segments from {len(trip_ids)} trips")
    print("🔀 Merging similar segments…")

    merged = merge_segments(all_segments)
    print(f"   Consolidated to {len(merged)} segments")

    # ── Build output features ─────────────────────────────────────────────────
    out_features = []

    for seg in merged:
        if len(seg['speeds']) < 2:
            continue

        avg_speed = sum(seg['speeds']) / len(seg['speeds'])
        min_speed = min(seg['speeds'])
        max_speed = max(seg['speeds'])

        avg_quality = (sum(seg['qualities']) / len(seg['qualities'])
                       if seg['qualities'] else 0)

        coord1, coord2 = seg['coords']
        distance = haversine_distance(coord1[0], coord1[1], coord2[0], coord2[1])

        # Composite score: higher = worse
        #   quality_score: 0 (perfect) → 100 (no road)
        #   speed_score:   0 (fast)    → 100 (stopped)
        speed_score   = max(0, 100 - (avg_speed * 4))
        quality_score = (avg_quality - 1) * 25 if avg_quality > 0 else 50
        composite     = (quality_score * 0.6) + (speed_score * 0.4)

        out_features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'LineString',
                'coordinates': [coord1, coord2],
            },
            'properties': {
                'avg_speed':         round(avg_speed,   2),
                'min_speed':         round(min_speed,   2),
                'max_speed':         round(max_speed,   2),
                'speed_variance':    round(max_speed - min_speed, 2),
                'avg_quality':       round(avg_quality, 2) if avg_quality > 0 else None,
                'observation_count': len(seg['speeds']),
                'trip_count':        len(seg['trips']),
                'distance_m':        round(distance, 2),
                'composite_score':   round(composite, 2),
                'trips':             list(seg['trips']),
            },
        })

    print(f"   {len(out_features)} final averaged segments created")

    if not out_features:
        print("❌ No output segments — check input data.")
        return None

    output = {'type': 'FeatureCollection', 'features': out_features}

    output_file = 'road_segments_averaged.json'
    Path(output_file).write_text(json.dumps(output))
    size_kb = Path(output_file).stat().st_size / 1024
    print(f"\n✅ Saved {output_file} ({size_kb:.0f} KB)")

    # ── Summary stats ─────────────────────────────────────────────────────────
    speeds     = [f['properties']['avg_speed']       for f in out_features]
    qualities  = [f['properties']['avg_quality']     for f in out_features
                  if f['properties']['avg_quality']]
    composites = [f['properties']['composite_score'] for f in out_features]

    print(f"\n📊 Statistics:")
    print(f"   Speed:     {min(speeds):.1f} – {max(speeds):.1f} km/h  (avg {sum(speeds)/len(speeds):.1f})")
    if qualities:
        print(f"   Quality:   {min(qualities):.1f} – {max(qualities):.1f}  (avg {sum(qualities)/len(qualities):.1f})")
    print(f"   Composite: {min(composites):.1f} – {max(composites):.1f}")

    return output


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    input_file = sys.argv[1] if len(sys.argv) > 1 else "trips.geojson"
    result = process_trips_geojson(input_file)
    if result:
        print("\n🎉 Done!")
    else:
        print("\n❌ Failed.")
        sys.exit(1)