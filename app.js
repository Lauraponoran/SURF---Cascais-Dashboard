// app.js
// Bike Sensor Data Visualization

import { CONFIG } from './config.js';

console.log('🚀 Starting bike visualization...');

const map = new mapboxgl.Map({
  container: 'map',
  style: CONFIG.MAP_STYLE,
  center: CONFIG.MAP_CENTER,
  zoom: CONFIG.MAP_ZOOM
});

window.map = map;

// ─── State ────────────────────────────────────────────────────────────────────
let tripIds = [];
let speedMode = 'gradient';
let showSpeedColors = false;
let showRoadQuality = false;
let selectedTrip = null;
let currentPopup = null;
let showAveragedSegments = false;
let averagedSegmentMode = 'composite';
let activeFilter = null;
let showBraking = false;
let showCrashes = false;
let tripDates = {};        // trip_id -> 'YYYY-MM-DD'
let selectedSensorFilters = new Set();
let selectedDateFrom = '';
let selectedDateTo   = '';

// ─── Sensor colours ───────────────────────────────────────────────────────────
const SENSOR_COLORS = [
  '#34CCCC','#FFCC33','#5B8FFF','#CC5BAA','#33CCAA',
  '#FF7A3D','#88DDFF','#FFE066','#CC3355','#66FF99',
  '#AA88FF','#FF9966','#00CCFF','#FFB3DE','#44FFDD',
  '#FFAA00','#7BFFB3','#FF6680','#B3EEFF','#D4FF66',
];
const DEFAULT_COLOR = '#34CCCC';
const sensorColorMap = {};

function buildSensorColorMap(ids) {
  const sensors = [...new Set(ids.map(id => id.split('_')[0]))].sort();
  sensors.forEach((s, i) => { sensorColorMap[s] = SENSOR_COLORS[i % SENSOR_COLORS.length]; });
  console.log('🎨 Sensor colour map:', sensorColorMap);
}

function getSensorColor(tripId) {
  const sensor = tripId.split('_')[0];
  return sensorColorMap[sensor] || DEFAULT_COLOR;
}

function getFirstLabelLayerId() {
  const layers = map.getStyle().layers;
  for (const layer of layers) {
    if (layer.type === 'symbol') return layer.id;
  }
  return undefined;
}

// ─── Colour expressions ───────────────────────────────────────────────────────
function getSpeedColorExpression(mode) {
  const v = ['to-number', ['coalesce', ['get', 'Speed'], ['get', 'speed'], 0]];
  if (mode === 'gradient') {
    return ['interpolate', ['linear'], v, 0,'#808080', 2,'#DC2626', 5,'#F97316', 10,'#FACC15', 15,'#22C55E', 20,'#3B82F6', 25,'#bb06d7'];
  }
  return ['step', v, '#808080', 2,'#DC2626', 5,'#F97316', 10,'#FACC15', 15,'#22C55E', 20,'#3B82F6', 25,'#bb06d7'];
}

function getRoadQualityColorExpression() {
  return ['match', ['get', 'road_quality'], 1,'#22C55E', 2,'#84CC16', 3,'#FACC15', 4,'#F97316', 5,'#DC2626', '#808080'];
}

function getSensorColorExpression() {
  const fallback = DEFAULT_COLOR;
  const pairs = tripIds.flatMap(id => [id, getSensorColor(id)]);
  if (pairs.length === 0) return fallback;
  return ['match', ['get', 'trip_id'], ...pairs, fallback];
}

function getAveragedSpeedColorExpression() {
  return ['interpolate', ['linear'], ['get', 'avg_speed'], 0,'#DC2626', 5,'#F97316', 10,'#FACC15', 15,'#22C55E', 20,'#3B82F6', 25,'#bb06d7'];
}
function getAveragedQualityColorExpression() {
  return ['interpolate', ['linear'], ['get', 'avg_quality'], 1,'#22C55E', 2,'#84CC16', 3,'#FACC15', 4,'#F97316', 5,'#DC2626'];
}
function getCompositeScoreColorExpression() {
  return ['interpolate', ['linear'], ['get', 'composite_score'], 0,'#22C55E', 25,'#84CC16', 50,'#FACC15', 75,'#F97316', 100,'#DC2626'];
}

function getQualityLabel(q) {
  if (q <= 1.5) return 'Perfect';
  if (q <= 2.5) return 'Normal';
  if (q <= 3.5) return 'Outdated';
  if (q <= 4.5) return 'Bad';
  return 'No road';
}
function getCompositeLabel(s) {
  if (s < 20) return 'Excellent';
  if (s < 40) return 'Good';
  if (s < 60) return 'Moderate';
  if (s < 80) return 'Poor';
  return 'Critical';
}

// ─── Hotspot colour (concentration only) ─────────────────────────────────────
function getHotspotColorExpression() {
  return [
    'interpolate', ['linear'],
    ['to-number', ['get', 'count']],
    1,  '#FFF176',
    3,  '#FF9800',
    8,  '#D32F2F',
    20, '#9C27B0',
  ];
}

// ─── Braking filter helpers ───────────────────────────────────────────────────
// When a trip (or sensor's set of trips) is selected while braking is active,
// filter hotspots down to just those trips AND grid them at finer resolution —
// cheap once we're only gridding a handful of trips instead of the whole
// dataset, and it gets rid of the patchy/blocky look on an isolated trip.
// When deselected, restore the full dataset at the coarser (RAM-friendly) grid.
function applyBrakingTripFilter(tripIdOrIds) {
  if (!map.getSource('braking-hotspots')) return;

  const source = map.getSource('trips');
  const allFeatures = source?._data?.features || [];

  if (tripIdOrIds) {
    const ids = new Set(Array.isArray(tripIdOrIds) ? tripIdOrIds : [tripIdOrIds]);
    const tripFeatures = allFeatures.filter(f => ids.has(f.properties.trip_id));
    const filtered = buildBrakingHotspots(tripFeatures, BRAKING_CELL_SIZE_ISOLATED);
    map.getSource('braking-hotspots').setData(filtered);
  } else {
    // Restore all hotspots at the coarser full-dataset grid
    const all = buildBrakingHotspots(allFeatures, BRAKING_CELL_SIZE_FULL);
    map.getSource('braking-hotspots').setData(all);
  }
}

// ─── Data loading ─────────────────────────────────────────────────────────────
async function loadTripsGeoJSON() {
  const loadingEl = document.getElementById('loadingIndicator');
  if (loadingEl) loadingEl.style.display = 'block';
  try {
    const r = await fetch('./trips.geojson');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const geojson = await r.json();
    console.log(`✅ Loaded trips.geojson — ${geojson.features?.length ?? 0} segments`);
    return geojson;
  } catch (err) {
    console.error('❌ Could not load trips.geojson:', err);
    return { type: 'FeatureCollection', features: [] };
  } finally {
    if (loadingEl) loadingEl.style.display = 'none';
  }
}

async function loadAveragedSegments() {
  const paths = ['./road_segments_averaged.json', `${CONFIG.DATA_URL}road_segments_averaged.json`];
  for (const path of paths) {
    try {
      const r = await fetch(path);
      if (r.ok) {
        const data = await r.json();
        console.log(`✅ Loaded ${data.features.length} averaged segments`);
        return data;
      }
    } catch {}
  }
  console.error('❌ Could not load averaged segments');
  return null;
}

function formatDuration(s) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h} h ${m} min` : `${m} min`;
}

function hasAccordionFilter() {
  const sensors = Object.keys(sensorColorMap);
  return selectedSensorFilters.size !== sensors.length || !!selectedDateFrom || !!selectedDateTo;
}

function updateResetButtonVisibility() {
  const active = showSpeedColors || showRoadQuality || showAveragedSegments || showBraking || showCrashes || !!activeFilter || !!selectedTrip || hasAccordionFilter();
  document.getElementById('resetButton').style.display = active ? 'block' : 'none';
}

function currentColorExpression() {
  if (showSpeedColors) return getSpeedColorExpression(speedMode);
  if (showRoadQuality) return getRoadQualityColorExpression();
  return getSensorColorExpression();
}

function getSelectedTripIds() {
  if (!activeFilter) return null;
  return Array.isArray(activeFilter) ? activeFilter : [activeFilter];
}

function applyTripFilter(filterTripId) {
  activeFilter = filterTripId;
  if (!map.getLayer('trips-layer')) return;

  if (filterTripId) {
    let highlightColor;
    if (showSpeedColors) {
      highlightColor = getSpeedColorExpression(speedMode);
    } else if (showRoadQuality) {
      highlightColor = getRoadQualityColorExpression();
    } else {
      highlightColor = '#FF69B4';
    }
    map.setPaintProperty('trips-layer', 'line-color', [
      'case',
      ['==', ['get', 'trip_id'], filterTripId],
      highlightColor,
      'rgba(0,0,0,0)'
    ]);
    map.setPaintProperty('trips-layer', 'line-opacity', 1);
    map.setPaintProperty('trips-layer', 'line-width', [
      'case', ['==', ['get', 'trip_id'], filterTripId], 4, 0
    ]);
  } else {
    map.setPaintProperty('trips-layer', 'line-color', currentColorExpression());
    map.setPaintProperty('trips-layer', 'line-opacity', 0.7);
    map.setPaintProperty('trips-layer', 'line-width', 3);
  }

  // If braking is active, sync hotspot filter to the selected trip
  if (showBraking) applyBrakingTripFilter(filterTripId);
}

function applyGroupFilter(matchingIds) {
  activeFilter = matchingIds;
  if (!map.getLayer('trips-layer')) return;
  const set = new Set(matchingIds);

  let highlightColor;
  if (showSpeedColors) {
    highlightColor = getSpeedColorExpression(speedMode);
  } else if (showRoadQuality) {
    highlightColor = getRoadQualityColorExpression();
  } else {
    highlightColor = '#FF69B4';
  }

  map.setPaintProperty('trips-layer', 'line-color', [
    'case',
    ['in', ['get', 'trip_id'], ['literal', [...set]]],
    highlightColor,
    'rgba(0,0,0,0)'
  ]);
  map.setPaintProperty('trips-layer', 'line-opacity', 1);
  map.setPaintProperty('trips-layer', 'line-width', [
    'case', ['in', ['get', 'trip_id'], ['literal', [...set]]], 4, 0
  ]);
}

// ─── Selection ────────────────────────────────────────────────────────────────
function resetSelection() {
  selectedTrip         = null;
  activeFilter         = null;
  showSpeedColors      = false;
  showRoadQuality      = false;
  showAveragedSegments = false;
  showBraking          = false;
  showCrashes          = false;

  if (currentPopup) { currentPopup.remove(); currentPopup = null; }
  applyTripFilter(null);

  ['speedColorsCheckbox','roadQualityCheckbox','averagedSegmentsCheckbox','brakingCheckbox','crashCheckbox'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.checked = false;
  });

  document.getElementById('speedLegend').style.display            = 'none';
  document.getElementById('speedModeGroup').style.display         = 'none';
  document.getElementById('roadQualityLegend').style.display      = 'none';
  document.getElementById('averagedSegmentsLegend').style.display = 'none';
  document.getElementById('averagedModeGroup').style.display      = 'none';
  const brakingLegend = document.getElementById('brakingLegend');
  if (brakingLegend) brakingLegend.style.display = 'none';
  const crashLegend = document.getElementById('crashLegend');
  if (crashLegend) crashLegend.style.display = 'none';

  if (map.getLayer('averaged-segments'))
    map.setLayoutProperty('averaged-segments', 'visibility', 'none');
  if (map.getLayer('braking-hotspots-halo'))
    map.setLayoutProperty('braking-hotspots-halo', 'visibility', 'none');
  if (map.getLayer('braking-hotspots-dot'))
    map.setLayoutProperty('braking-hotspots-dot', 'visibility', 'none');
  if (map.getLayer('crash-events-halo'))
    map.setLayoutProperty('crash-events-halo', 'visibility', 'none');
  if (map.getLayer('crash-events-dot'))
    map.setLayoutProperty('crash-events-dot', 'visibility', 'none');

  if (map.getLayer('trips-layer')) {
    map.setLayoutProperty('trips-layer', 'visibility', 'visible');
    map.setPaintProperty('trips-layer', 'line-color', getSensorColorExpression());
    map.setPaintProperty('trips-layer', 'line-opacity', 0.7);
    map.setPaintProperty('trips-layer', 'line-width', 3);
  }

  document.getElementById('selectedTripRow').style.display  = 'none';
  document.getElementById('statTripRow').style.display      = 'flex';
  document.getElementById('statDistanceRow').style.display  = 'flex';
  document.getElementById('statAvgSpeedRow').style.display  = 'flex';
  document.getElementById('statTotalTimeRow').style.display = 'flex';

  updateResetButtonVisibility();
  setTimeout(updateLegendPositions, 50);
  updateStatsVisibility();
}

// Deselect a trip/sensor highlight without touching the other filter
// checkboxes (speed/quality/etc stay as they were).
function clearFilter() {
  selectedTrip = null;
  activeFilter = null;
  if (currentPopup) { currentPopup.remove(); currentPopup = null; }
  applyTripFilter(null);

  // Restore all braking hotspots when deselecting
  if (showBraking) applyBrakingTripFilter(null);

  document.getElementById('selectedTripRow').style.display  = 'none';
  document.getElementById('statTripRow').style.display      = 'flex';
  document.getElementById('statDistanceRow').style.display  = 'flex';
  document.getElementById('statAvgSpeedRow').style.display  = 'flex';
  document.getElementById('statTotalTimeRow').style.display = 'flex';

  updateResetButtonVisibility();
}

function showSelection(tripId) {
  document.getElementById('statTripRow').style.display      = 'none';
  document.getElementById('statDistanceRow').style.display  = 'none';
  document.getElementById('statAvgSpeedRow').style.display  = 'none';
  document.getElementById('statTotalTimeRow').style.display = 'none';
  document.getElementById('selectedTripRow').style.display  = 'flex';
  const name = tripId.replace(/_/g, ' ').replace(/processed/gi, '').replace(/clean/gi, '').trim();
  document.getElementById('selectedTrip').textContent = name;
  updateResetButtonVisibility();
}

// Highlight every trip belonging to one sensor (triggered by clicking its
// swatch in the sensor legend).
function highlightSensor(sensor) {
  if (!sensor) { resetSelection(); return; }
  const q       = sensor.toLowerCase().trim();
  const matches = tripIds.filter(id => id.toLowerCase().includes(q));

  if (matches.length === 0) return false;

  if (matches.length === 1) {
    selectedTrip = matches[0];
    applyTripFilter(matches[0]);
    showSelection(matches[0]);
  } else {
    selectedTrip = null;
    applyGroupFilter(matches);
    if (showBraking) applyBrakingTripFilter(matches);
    document.getElementById('statTripRow').style.display      = 'none';
    document.getElementById('statDistanceRow').style.display  = 'none';
    document.getElementById('statAvgSpeedRow').style.display  = 'none';
    document.getElementById('statTotalTimeRow').style.display = 'none';
    document.getElementById('selectedTripRow').style.display  = 'flex';
    document.getElementById('selectedTrip').textContent = `${sensor.toUpperCase()} — ${matches.length} trips`;
    updateResetButtonVisibility();
  }

  try {
    const features = map.querySourceFeatures('trips', {
      filter: ['in', ['get', 'trip_id'], ['literal', matches]]
    });
    if (features.length > 0) {
      const bbox = turf.bbox({ type: 'FeatureCollection', features });
      map.fitBounds(bbox, { padding: 50, duration: 1000 });
    }
  } catch (err) {
    console.error('Zoom error:', err);
  }

  return true;
}

// ─── Averaged segments ────────────────────────────────────────────────────────
function updateAveragedSegmentColors() {
  if (!map.getLayer('averaged-segments')) return;
  const exprs = {
    speed:     getAveragedSpeedColorExpression(),
    quality:   getAveragedQualityColorExpression(),
    composite: getCompositeScoreColorExpression(),
  };
  map.setPaintProperty('averaged-segments', 'circle-color', exprs[averagedSegmentMode]);
}

async function setupAveragedSegments(labelLayerId) {
  const data = await loadAveragedSegments();
  if (!data) return;

  const pointFeatures = data.features.map(f => {
    const coords = f.geometry.coordinates;
    const midLng = coords.reduce((s, c) => s + c[0], 0) / coords.length;
    const midLat = coords.reduce((s, c) => s + c[1], 0) / coords.length;
    return { type: 'Feature', geometry: { type: 'Point', coordinates: [midLng, midLat] }, properties: f.properties };
  });

  map.addSource('averaged-segments', { type: 'geojson', data: { type: 'FeatureCollection', features: pointFeatures } });
  map.addLayer({
    id: 'averaged-segments', type: 'circle', source: 'averaged-segments',
    layout: { visibility: 'none' },
    paint: {
      'circle-color':           getCompositeScoreColorExpression(),
      'circle-radius':          ['interpolate', ['linear'], ['zoom'], 10, 18, 13, 28, 16, 45],
      'circle-blur':            1.2,
      'circle-opacity':         0.6,
      'circle-pitch-alignment': 'map',
    }
  }, labelLayerId);

  map.on('click', 'averaged-segments', (e) => {
    e.preventDefault();
    if (e.originalEvent) e.originalEvent.stopPropagation();
    const p = e.features[0].properties;
    const qualityText = p.avg_quality
      ? `🛣️ Avg Quality: ${p.avg_quality} (${getQualityLabel(p.avg_quality)})`
      : '🛣️ Quality: No data';
    new mapboxgl.Popup().setLngLat(e.lngLat).setHTML(`
      <strong>📊 Averaged Road Segment</strong><br>
      🚴 Avg Speed: ${p.avg_speed} km/h<br>
      📈 Speed Range: ${p.min_speed} - ${p.max_speed} km/h<br>
      ${qualityText}<br>
      📏 Distance: ${p.distance_m}m<br>
      🎯 Composite Score: ${p.composite_score} (${getCompositeLabel(p.composite_score)})<br>
      📍 Observations: ${p.observation_count}<br>
      🚲 From ${p.trip_count} trips
    `).addTo(map);
  });
  map.on('mouseenter', 'averaged-segments', () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'averaged-segments', () => { map.getCanvas().style.cursor = ''; });

  console.log('✅ Averaged segments layer added');
}

// ─── Braking hotspot accumulation ────────────────────────────────────────────
// Grid ("tile") size in degrees. Coarser = fewer cells = lighter on RAM when
// gridding the whole dataset; finer = smoother/more precise, cheap once we've
// already narrowed down to a single trip or sensor's worth of points.
const BRAKING_CELL_SIZE_FULL     = 0.0002;   // ~22m — used for all trips at once
const BRAKING_CELL_SIZE_ISOLATED = 0.00004;  // ~4.5m — used once a trip/sensor is selected

function buildBrakingHotspots(features, cellSize = BRAKING_CELL_SIZE_FULL) {
  const CELL_SIZE = cellSize;
  const grid = new Map();

  for (const f of features) {
    if (!f.properties.is_braking) continue;

    const coords = f.geometry.coordinates;
    const mid    = coords[Math.floor(coords.length / 2)] || coords[0];
    const [lng, lat] = mid;

    const cellLng = Math.round(lng / CELL_SIZE) * CELL_SIZE;
    const cellLat = Math.round(lat / CELL_SIZE) * CELL_SIZE;
    // 6 decimals keeps the dedup key finer than either grid size above so
    // adjacent cells never collide when CELL_SIZE shrinks for isolated views.
    const key     = `${cellLng.toFixed(6)},${cellLat.toFixed(6)}`;

    if (!grid.has(key)) {
      grid.set(key, {
        lng: cellLng, lat: cellLat,
        count: 0, totalIntensity: 0, maxIntensity: 0,
        trips: new Set(),
      });
    }

    const cell = grid.get(key);
    cell.count++;
    cell.totalIntensity += f.properties.braking_intensity || 0;
    cell.maxIntensity    = Math.max(cell.maxIntensity, f.properties.braking_intensity || 0);
    cell.trips.add(f.properties.trip_id);
  }

  const hotspotFeatures = [...grid.values()].map(cell => ({
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [cell.lng, cell.lat] },
    properties: {
      count:         cell.count,
      avg_intensity: parseFloat((cell.totalIntensity / cell.count).toFixed(1)),
      max_intensity: parseFloat(cell.maxIntensity.toFixed(1)),
      trip_count:    cell.trips.size,
    },
  }));

  console.log(`🛑 Built ${hotspotFeatures.length} braking hotspot cells`);
  return { type: 'FeatureCollection', features: hotspotFeatures };
}

// ─── Crash / fall events (ported from Marineterrein-Commutes) ─────────────
// Severity (colour), type (glyph artwork), and outcome (white ring) are
// each baked directly into the marker's icon — see CRASH_SEVERITY_COLORS,
// CRASH_GLYPHS, and drawCrashIcon() below.

function getCrashTypeLabel(properties) {
  if (properties.crash_type) return properties.crash_type;

  // speed_at_impact_kmh can be null when there's no wheel-diameter data to
  // estimate from. Number(null) === 0, not NaN, so without this explicit
  // null check a missing-data event would silently fall through the
  // Number.isFinite/speed<=1 branch below and get misread as a
  // "Stationary Fall".
  const rawSpeed = properties.preimpact_speed_kmh ?? properties.speed_at_impact_kmh;
  if (rawSpeed == null) return 'Unclassified';

  const speed = Number(rawSpeed);
  if (!Number.isFinite(speed)) return 'Unclassified';
  if (speed <= 1) return 'Stationary Fall';
  if (speed <= 10) return 'Low-Speed Fall';
  return 'High-Speed Fall';
}

function getCrashOutcomeLabel(properties) {
  if (properties.crash_outcome) return properties.crash_outcome;

  if (properties.unresolved === true || properties.unresolved === 'true') {
    return 'Unresolved';
  }

  if (properties.came_to_stop === true || properties.came_to_stop === 'true') {
    return 'Resolved';
  }

  return 'Unclassified';
}

function buildCrashFeatures(features) {
  const crashFeatures = (features || [])
    .filter(f =>
      f?.geometry?.type === 'Point' &&
      f?.properties?.event_type === 'crash'
    )
    .map(f => {
      const crash_type = getCrashTypeLabel(f.properties || {});
      const crash_outcome = getCrashOutcomeLabel(f.properties || {});
      const severity = f.properties?.severity || 'Minor';
      return {
        ...f,
        properties: {
          ...f.properties,
          crash_type,
          crash_outcome,
          crash_icon_id: crashIconId(
            crash_type,
            severity,
            crash_outcome === 'Unresolved'
          )
        }
      };
    });
  console.log(`🚨 Found ${crashFeatures.length} crash marker(s)`);
  return { type: 'FeatureCollection', features: crashFeatures };
}

// Crash marker icons, drawn on canvas rather than rendered as map-font
// text/circle layers — the vector basemap's fonts don't cover the glyph
// shapes used here (◆ ▲ ● ■ fall outside standard Latin ranges).
const CRASH_GLYPHS = {
  // Arrow swoosh
  'Stationary Fall': {
    type: 'path',
    d: 'M43.3,141.6s19.3-35.5,53.1-44.3c33.8-8.9,70.4,14,70.4,14l5.5-18.8c.1-.5.5-.8,1-1,.8-.3,1.6.1,1.9.9l18.3,53.7c0,.1,0,.2,0,.3,0,.8-.5,1.6-1.3,1.6l-53.3,5c-.4,0-.9-.1-1.2-.4-.6-.6-.6-1.5,0-2.1l15.3-16s-30.5-23.2-59.1-17.9c-28.6,5.3-50.6,24.9-50.6,24.9Z'
  },
  // Wifi-style signal arcs
  'Low-Speed Fall': {
    type: 'path',
    d: 'M187.9,145.5c-17-38.7-62.1-60.5-103-49.8-1.5.4-2.3,2-1.7,3.4l3.1,7.8c.6,1.4,2.2,2.2,3.6,1.8,33.6-8.6,70.5,9.2,84.6,40.9.6,1.4,2.2,2.1,3.7,1.7l8.1-2.4c1.5-.5,2.3-2,1.6-3.5h0ZM161.3,153.8c-11.4-24.5-40-38.3-66.3-32.1-1.6.4-2.4,2-1.8,3.4l3.1,7.9c.5,1.4,2.1,2.2,3.5,1.9,19-4.3,39.6,5.7,48,23.2.6,1.3,2.2,2,3.7,1.6l8.1-2.4c1.5-.5,2.3-2.1,1.6-3.5Z'
  },
  // Zigzag impact spike — drawn stroked (source is a polyline), not filled
  'High-Speed Fall': {
    type: 'polyline',
    points: [[68.5,167.8],[50.8,139.7],[79.9,139.7],[79.9,91.7],[123.1,138.1],[142.7,76.4],[167.5,127.2],[209.1,106.8],[209.1,141.8]],
    strokeWidth: 19.8
  },
  // Triangle with a question mark
  'Unclassified': {
    type: 'path',
    d: 'M184.2,161.2l-51-100.3c-2.8-5.6-7.5-5.6-10.3,0l-51,100.3c-2.8,5.6,0,10.1,6.4,10.1h99.6c6.3,0,9.2-4.5,6.4-10.1ZM126.3,163.1c-4.9,0-8.2-3.5-8.2-8.3,0-4.9,3.4-8.4,8.2-8.4,4.9,0,8.1,3.4,8.2,8.4,0,4.8-3.2,8.3-8.2,8.3ZM137.4,129c-3.3,3.7-4.7,7.3-4.7,11.4v1.6s-12.2,0-12.2,0v-2.3c-.4-4.7,1.2-9.5,5.3-14.4,3-3.5,5.3-6.5,5.3-9.6s-2.1-5.4-6.7-5.5c-3.1,0-6.7,1-9.1,2.7l-3.1-10c3.3-2,8.8-3.8,15.3-3.8,12.1,0,17.6,6.7,17.6,14.3s-4.4,11.6-7.9,15.4Z'
  }
};
const CRASH_GLYPH_VIEWBOX = 256; // all path/point data above is in this coordinate space

const CRASH_SEVERITY_COLORS = {
  'Minor':  '#ffea00',
  'Hard':   '#ff9100',
  'Severe': '#ff1744'
};

function crashColorForSeverity(severity) {
  return CRASH_SEVERITY_COLORS[severity] || CRASH_SEVERITY_COLORS.Minor;
}

// Small inline SVG version of a crash-type glyph, for the legend — reuses
// the same path/polyline data as drawCrashIcon() so the legend and the map
// markers always agree on what each glyph looks like.
function crashGlyphSvg(crashType, color = '#cfcfcf') {
  const glyph = CRASH_GLYPHS[crashType] || CRASH_GLYPHS['Unclassified'];
  const inner = glyph.type === 'polyline'
    ? `<polyline points="${glyph.points.map(p => p.join(',')).join(' ')}" fill="none" stroke="${color}" stroke-width="${glyph.strokeWidth}" stroke-linejoin="round" stroke-linecap="round"/>`
    : `<path d="${glyph.d}" fill="${color}"/>`;
  return `<svg viewBox="0 0 ${CRASH_GLYPH_VIEWBOX} ${CRASH_GLYPH_VIEWBOX}" width="16" height="16">${inner}</svg>`;
}

// Icon id encodes all three baked-in dimensions so distinct combinations
// each get their own registered image.
function crashIconId(crashType, severity, unresolved) {
  const safeType = (crashType || 'Unclassified').replace(/[^a-zA-Z0-9]+/g, '_');
  return `crash-icon-${safeType}-${severity}-${unresolved ? 'ring' : 'plain'}`;
}

// Renders one crash-type glyph as a round badge: dark disc, a severity-
// coloured ring, the glyph artwork in that same colour, and — for
// unresolved crashes — an extra white ring outside it.
function drawCrashIcon(crashType, color, unresolved, size = 40) {
  const glyph = CRASH_GLYPHS[crashType] || CRASH_GLYPHS['Unclassified'];
  const canvas = document.createElement('canvas');
  const dpr = window.devicePixelRatio || 1;
  canvas.width = size * dpr;
  canvas.height = size * dpr;
  const ctx = canvas.getContext('2d');

  const scale = (size * dpr) / CRASH_GLYPH_VIEWBOX;
  ctx.scale(scale, scale);

  const cx = CRASH_GLYPH_VIEWBOX / 2;
  const cy = CRASH_GLYPH_VIEWBOX / 2;
  const discRadius = 118;

  ctx.beginPath();
  ctx.arc(cx, cy, discRadius, 0, Math.PI * 2);
  ctx.fillStyle = '#3a3838';
  ctx.fill();

  if (unresolved) {
    ctx.beginPath();
    ctx.arc(cx, cy, discRadius + 6, 0, Math.PI * 2);
    ctx.lineWidth = 8;
    ctx.strokeStyle = '#ffffff';
    ctx.stroke();
  }

  ctx.beginPath();
  ctx.arc(cx, cy, discRadius - 5, 0, Math.PI * 2);
  ctx.lineWidth = 10;
  ctx.strokeStyle = color;
  ctx.stroke();

  ctx.fillStyle = color;
  ctx.strokeStyle = color;
  if (glyph.type === 'polyline') {
    ctx.beginPath();
    glyph.points.forEach(([x, y], i) => {
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineWidth = glyph.strokeWidth;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();
  } else {
    ctx.fill(new Path2D(glyph.d));
  }

  return { width: canvas.width, height: canvas.height, data: ctx.getImageData(0, 0, canvas.width, canvas.height).data };
}

function ensureCrashIcons(crashFeatures) {
  const seen = new Set();
  (crashFeatures || []).forEach(f => {
    const p = f.properties || {};
    const id = p.crash_icon_id;
    if (!id || seen.has(id) || map.hasImage(id)) return;
    seen.add(id);
    const severity = p.severity || 'Minor';
    const unresolved = p.crash_outcome === 'Unresolved';
    map.addImage(id, drawCrashIcon(p.crash_type, crashColorForSeverity(severity), unresolved), { pixelRatio: window.devicePixelRatio || 1 });
  });
}

function setupCrashLayer(geojson, labelLayerId) {
  const crashData = buildCrashFeatures(geojson.features || []);

  ensureCrashIcons(crashData.features);

  map.addSource('crash-events', {
    type: 'geojson',
    data: crashData
  });

  // Invisible tap-target circle — the visible marker is the icon below.
  map.addLayer({
    id: 'crash-events-halo',
    type: 'circle',
    source: 'crash-events',
    layout: {
      visibility: 'none'
    },
    paint: {
      'circle-color': '#000000',
      'circle-opacity': 0,
      'circle-radius': [
        'interpolate', ['linear'], ['zoom'],
        10, 9,
        14, 13,
        17, 18
      ],
      'circle-pitch-alignment': 'map'
    }
  }, labelLayerId);

  // The actual crash marker: severity (colour), type (glyph), outcome
  // (white ring) all baked into a single raster icon (see drawCrashIcon).
  map.addLayer({
    id: 'crash-events-dot',
    type: 'symbol',
    source: 'crash-events',
    layout: {
      visibility: 'none',
      'icon-image': ['get', 'crash_icon_id'],
      'icon-size': [
        'interpolate', ['linear'], ['zoom'],
        10, 0.48,
        14, 0.68,
        17, 0.95
      ],
      'icon-allow-overlap': true,
      'icon-ignore-placement': true
    },
    paint: {
      'icon-opacity': 1
    }
  }, labelLayerId);

  function showCrashPopup(e) {
    e.preventDefault();
    if (e.originalEvent) {
      e.originalEvent.stopPropagation();
    }

    if (currentPopup) { currentPopup.remove(); }

    const p = e.features[0].properties;

    const speedLine = p.speed_at_impact_kmh != null
      ? `🚴 Speed at impact: ${p.speed_at_impact_kmh} km/h`
      : `🚴 Speed at impact: unknown`;

    const crashType = getCrashTypeLabel(p);
    const crashOutcome = getCrashOutcomeLabel(p);

    const recoveryLine =
      p.unresolved === true || p.unresolved === 'true'
        ? `⚠️ <strong>Wheel never turned again</strong>`
        : p.came_to_stop === true || p.came_to_stop === 'true'
          ? `🧍 Came to a stop, moving again after ${p.recovery_time_s}s`
          : `↪️ Kept moving — no stop detected nearby`;

    currentPopup = new mapboxgl.Popup()
      .setLngLat(e.lngLat)
      .setHTML(`
        <strong>🚨 ${p.severity} Impact</strong><br>
        💥 Peak force: ${p.peak_g}g<br>
        ⚡ Onset: ${p.suddenness_s}s to peak<br>
        🚲 Classification: ${crashType}<br>
        📍 Outcome: ${crashOutcome}<br>
        ${speedLine}<br>
        ${recoveryLine}<br>
        🕐 ${p.time_str || 'time unknown'} · trip ${p.trip_id}
      `)
      .addTo(map);
  }

  map.on('click', 'crash-events-halo', showCrashPopup);
  map.on('click', 'crash-events-dot',  showCrashPopup);

  map.on('mouseenter', 'crash-events-halo', () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'crash-events-halo', () => { map.getCanvas().style.cursor = ''; });
  map.on('mouseenter', 'crash-events-dot',  () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'crash-events-dot',  () => { map.getCanvas().style.cursor = ''; });

  console.log('✅ Crash/fall layer added');
}

function setupCrashControls() {
  const cb = document.getElementById('crashCheckbox');
  if (!cb) return;

  cb.addEventListener('change', (e) => {
    showCrashes = e.target.checked;

    const legend = document.getElementById('crashLegend');
    const visibility = showCrashes ? 'visible' : 'none';

    if (map.getLayer('crash-events-halo')) {
      map.setLayoutProperty('crash-events-halo', 'visibility', visibility);
    }
    if (map.getLayer('crash-events-dot')) {
      map.setLayoutProperty('crash-events-dot', 'visibility', visibility);
    }
    if (legend) {
      legend.style.display = showCrashes ? 'block' : 'none';
    }

    updateResetButtonVisibility();
    setTimeout(updateLegendPositions, 50);
    updateStatsVisibility();
  });
}

// Crash / fall legend — three dimensions (intensity / classification /
// outcome), each shown as a collapsible category, ported from
// Marineterrein-Commutes.
const CRASH_LEGEND_CATEGORIES = [
  {
    id: 'intensity',
    label: 'Intensity',
    hint: 'Circle colour = impact severity',
    rows: [
      { swatch: `<div class="cl-swatch" style="background:#ffea00;"></div>`, label: 'Minor' },
      { swatch: `<div class="cl-swatch" style="background:#ff9100;"></div>`, label: 'Hard' },
      { swatch: `<div class="cl-swatch" style="background:#ff1744;"></div>`, label: 'Severe' },
    ]
  },
  {
    id: 'classification',
    label: 'Classification',
    hint: 'Symbol icon = type of event',
    rows: [
      { swatch: `<div class="cl-swatch cl-swatch--glyph">${crashGlyphSvg('Stationary Fall')}</div>`, label: 'Stationary Fall' },
      { swatch: `<div class="cl-swatch cl-swatch--glyph">${crashGlyphSvg('Low-Speed Fall')}</div>`, label: 'Low-Speed Fall' },
      { swatch: `<div class="cl-swatch cl-swatch--glyph">${crashGlyphSvg('High-Speed Fall')}</div>`, label: 'High-Speed Fall' },
      { swatch: `<div class="cl-swatch cl-swatch--glyph">${crashGlyphSvg('Unclassified')}</div>`, label: 'Unclassified' },
    ]
  },
  {
    id: 'outcome',
    label: 'Outcome',
    hint: 'White ring = rider did not recover',
    rows: [
      { swatch: `<div class="cl-swatch cl-swatch--outcome"></div>`, label: 'Resolved' },
      { swatch: `<div class="cl-swatch cl-swatch--outcome unresolved"></div>`, label: 'Unresolved (white ring)' },
    ]
  },
];

function renderCrashLegend() {
  const legend = document.getElementById('crashLegend');
  if (!legend) return;

  legend.innerHTML = `
    <strong>CRASHES &amp; FALLS</strong>
    <p class="cl-sub">Tap a category to see what its colours &amp; symbols mean.</p>
    ${CRASH_LEGEND_CATEGORIES.map((cat, i) => `
      <button type="button" class="cl-cat" data-cat="${cat.id}" aria-expanded="false">
        <span class="cl-cat-name">${cat.label}</span>
        <span class="cl-chevron">▶</span>
      </button>
      <div class="cl-panel" data-panel="${cat.id}" data-open="false">
        <p class="cl-sub" style="margin:0 0 6px;">${cat.hint}</p>
        ${cat.rows.map(r => `
          <div class="cl-row">
            ${r.swatch}
            <span class="cl-row-label">${r.label}</span>
          </div>
        `).join('')}
      </div>
    `).join('')}
  `;

  // Accordion behaviour: opening one category closes the others so the
  // panel stays short rather than growing with every click.
  legend.querySelectorAll('.cl-cat').forEach(btn => {
    btn.addEventListener('click', () => {
      const catId = btn.dataset.cat;
      const isOpen = btn.getAttribute('aria-expanded') === 'true';

      legend.querySelectorAll('.cl-cat').forEach(b => b.setAttribute('aria-expanded', 'false'));
      legend.querySelectorAll('.cl-panel').forEach(p => p.setAttribute('data-open', 'false'));

      if (!isOpen) {
        btn.setAttribute('aria-expanded', 'true');
        const panel = legend.querySelector(`.cl-panel[data-panel="${catId}"]`);
        if (panel) panel.setAttribute('data-open', 'true');
      }
    });
  });
}

function setupBrakingLayer(geojson, labelLayerId) {
  const hotspotData = buildBrakingHotspots(geojson.features || []);

  map.addSource('braking-hotspots', { type: 'geojson', data: hotspotData });

  // Outer glow
  map.addLayer({
    id: 'braking-hotspots-halo',
    type: 'circle',
    source: 'braking-hotspots',
    layout: { visibility: 'none' },
    paint: {
      'circle-color': getHotspotColorExpression(),
      'circle-radius': [
        'interpolate', ['linear'], ['zoom'],
        10, ['interpolate', ['linear'], ['get', 'count'],  1, 12,  5, 20, 15, 34],
        14, ['interpolate', ['linear'], ['get', 'count'],  1, 20,  5, 32, 15, 52],
        17, ['interpolate', ['linear'], ['get', 'count'],  1, 30,  5, 50, 15, 75],
      ],
      'circle-blur':            1.2,
      'circle-opacity':         0.35,
      'circle-pitch-alignment': 'map',
    },
  }, labelLayerId);

  // Solid dot — no stroke
  map.addLayer({
    id: 'braking-hotspots-dot',
    type: 'circle',
    source: 'braking-hotspots',
    layout: { visibility: 'none' },
    paint: {
      'circle-color': getHotspotColorExpression(),
      'circle-radius': [
        'interpolate', ['linear'], ['zoom'],
        10, ['interpolate', ['linear'], ['get', 'count'],  1,  4,  5,  8, 15, 14],
        14, ['interpolate', ['linear'], ['get', 'count'],  1,  7,  5, 13, 15, 20],
        17, ['interpolate', ['linear'], ['get', 'count'],  1, 11,  5, 18, 15, 28],
      ],
      'circle-opacity':         0.85,
      'circle-pitch-alignment': 'map',
    },
  }, labelLayerId);

  map.on('click', 'braking-hotspots-dot', (e) => {
    e.preventDefault();
    if (e.originalEvent) e.originalEvent.stopPropagation();
    const p = e.features[0].properties;
    let severity = 'Low';
    if (p.avg_intensity >= 15) severity = 'Emergency';
    else if (p.avg_intensity >= 5)   severity = 'Hard';
    else if (p.avg_intensity >= 2.5) severity = 'Firm';
    new mapboxgl.Popup()
      .setLngLat(e.lngLat)
      .setHTML(`
        <strong>🔴 Braking Hotspot</strong><br>
        📍 Events here: <strong>${p.count}</strong><br>
        ⚡ Avg deceleration: ${p.avg_intensity} km/h/s<br>
        🏎️ Peak deceleration: ${p.max_intensity} km/h/s<br>
        🚲 Across ${p.trip_count} trip(s)
      `)
      .addTo(map);
  });

  map.on('mouseenter', 'braking-hotspots-dot', () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'braking-hotspots-dot', () => { map.getCanvas().style.cursor = ''; });

  console.log('✅ Braking hotspot layer added');
}

// ─── Map load ─────────────────────────────────────────────────────────────────
map.on('error', e => console.error('❌ Map error:', e));

map.on('load', async () => {
  console.log('✅ Map loaded');

  const labelLayerId = getFirstLabelLayerId();
  console.log(`📌 Inserting layers before basemap layer: "${labelLayerId}"`);

  try {
    const geojson = await loadTripsGeoJSON();

    tripIds = [...new Set((geojson.features || []).map(f => f.properties.trip_id).filter(Boolean))].sort();
    console.log(`📊 ${tripIds.length} unique trips loaded`);

    buildSensorColorMap(tripIds);
    buildTripDateMap(geojson.features || []);
    renderFilterPanel();

    map.addSource('trips', {
      type: 'geojson',
      data: geojson,
      attribution: 'Bike sensor data',
    });

    map.addLayer({
      id: 'trips-layer',
      type: 'line',
      source: 'trips',
      layout: {
        'line-cap':  'round',
        'line-join': 'round',
      },
      paint: {
        'line-color':   getSensorColorExpression(),
        'line-width':   3,
        'line-opacity': 0.7,
      }
    }, labelLayerId);

    map.on('click', 'trips-layer', async (e) => {
      e.preventDefault();
      if (e.originalEvent) e.originalEvent.stopPropagation();
      if (currentPopup) { currentPopup.remove(); }

      const props       = e.features[0].properties;
      const tripId      = props.trip_id;
      const speed       = parseFloat(props.Speed || props.speed || 0);
      const roadQuality = parseInt(props.road_quality || 0);

      selectedTrip = tripId;
      applyTripFilter(tripId); // also filters braking hotspots if showBraking
      showSelection(tripId);

      const allFeats   = map.getSource('trips')?._data?.features || [];
      const tripFeats  = allFeats.filter(f => f.properties.trip_id === tripId);
      const geoDistKm  = (tripFeats.reduce((s, f) => s + (f.properties.gps_distance_m || 0), 0) / 1000).toFixed(2);
      const geoTime    = tripFeats.reduce((s, f) => s + (f.properties.time_diff_s || 0), 0);
      const geoSpeeds  = tripFeats.map(f => f.properties.Speed || 0).filter(s => s > 0);
      const geoAvgSpd  = geoSpeeds.length ? (geoSpeeds.reduce((a, b) => a + b, 0) / geoSpeeds.length).toFixed(1) : '—';
      const geoMaxSpd  = geoSpeeds.length ? Math.max(...geoSpeeds).toFixed(1) : '—';
      const geoBraking = tripFeats.filter(f => f.properties.is_braking).length;

      const distanceKm = geoDistKm;
      const avgSpeed   = geoAvgSpd;
      const maxSpeed   = geoMaxSpd;
      const duration   = formatDuration(Math.round(geoTime));
      const rideDate   = getTripDate(tripId);

      const qualityLabels = { 0:'Unknown', 1:'Perfect', 2:'Normal', 3:'Outdated', 4:'Bad', 5:'No road' };
      const popupName  = tripId.replace(/_/g, ' ').trim();
      const brakingLine = geoBraking > 0 ? `<br>🛑 Braking events: ${geoBraking}` : '';
      const dateLine    = rideDate ? `<br>📅 Date: ${formatDateDMY(rideDate)}` : '';

      currentPopup = new mapboxgl.Popup()
        .setLngLat(e.lngLat)
        .setHTML(`
          <strong>${popupName}</strong><br>
          🚴 Speed at point: ${speed} km/h<br>
          🛣️ Road quality: ${roadQuality} (${qualityLabels[roadQuality] || 'Unknown'})<br>
          📊 Average speed: ${avgSpeed} km/h<br>
          🏁 Max speed: ${maxSpeed} km/h<br>
          📍 Total distance: ${distanceKm} km<br>
          ⏱️ Duration: ${duration}${brakingLine}${dateLine}
        `)
        .addTo(map);
    });

    map.on('mouseenter', 'trips-layer', () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', 'trips-layer', () => { map.getCanvas().style.cursor = ''; });

    map.on('click', e => {
      if (!e.defaultPrevented) {
        if (activeFilter) clearFilter();
        else if (selectedTrip) {
          // If braking is active, deselecting restores all hotspots
          if (showBraking) applyBrakingTripFilter(null);
          resetSelection();
        }
      }
    });

    setupBrakingLayer(geojson, labelLayerId);
    setupCrashLayer(geojson, labelLayerId);
    await setupAveragedSegments(labelLayerId);

    setupControls();
    updateStatsFromMetadata();
    renderSensorLegend();
    renderCrashLegend();
    updateStatsVisibility();

  } catch (err) {
    console.error('❌ Error loading trips:', err);
  }
});

// ─── UI helpers ───────────────────────────────────────────────────────────────
function isFilteredMode() { return showSpeedColors || showRoadQuality || showAveragedSegments || showBraking || showCrashes || !!activeFilter; }

function updateStatsVisibility() {
  const statsEl = document.getElementById('stats');
  if (statsEl) statsEl.style.display = (window.innerWidth <= 768 && isFilteredMode()) ? 'none' : 'block';
  const sensorLegend = document.getElementById('sensorLegend');
  if (sensorLegend) sensorLegend.style.display = isFilteredMode() ? 'none' : 'block';
}

const sensorLegendEl = document.getElementById('sensorLegend');
sensorLegendEl.addEventListener('scroll', () => {
  sensorLegendEl.classList.toggle('is-scrolled', sensorLegendEl.scrollTop > 0);
});

window.addEventListener('resize', updateStatsVisibility);

function updateLegendPositions() {
  const order   = ['averagedSegmentsLegend','speedLegend','roadQualityLegend','brakingLegend','crashLegend','sensorLegend'];
  const visible = order.map(id => document.getElementById(id)).filter(el => el && el.style.display === 'block');
  const mobile  = window.matchMedia('(max-width: 768px)').matches;
  updateStatsVisibility();

  if (mobile) {
    let b = 10;
    visible.forEach(el => { el.style.right = '10px'; el.style.bottom = `${b}px`; b += (el.offsetHeight || 150) + 8; });
  } else {
    let r = 10;
    visible.forEach(el => { el.style.bottom = '10px'; el.style.right = `${r}px`; r += (el.offsetWidth || 220) + 10; });
  }
}

function setupAveragedSegmentControls() {
  const cb = document.getElementById('averagedSegmentsCheckbox');
  if (cb) {
    cb.addEventListener('change', e => {
      showAveragedSegments = e.target.checked;
      const modeGroup    = document.getElementById('averagedModeGroup');
      const legend       = document.getElementById('averagedSegmentsLegend');
      const sensorLegend = document.getElementById('sensorLegend');

      if (showAveragedSegments) {
        if (map.getLayer('averaged-segments')) map.setLayoutProperty('averaged-segments', 'visibility', 'visible');
        if (modeGroup)    modeGroup.style.display    = 'flex';
        if (legend)       legend.style.display       = 'block';
        if (sensorLegend) sensorLegend.style.display = 'none';
        if (map.getLayer('trips-layer')) map.setLayoutProperty('trips-layer', 'visibility', 'none');
        updateAveragedSegmentColors();
      } else {
        if (map.getLayer('averaged-segments')) map.setLayoutProperty('averaged-segments', 'visibility', 'none');
        if (modeGroup)    modeGroup.style.display    = 'none';
        if (legend)       legend.style.display       = 'none';
        if (sensorLegend) sensorLegend.style.display = 'block';
        if (map.getLayer('trips-layer')) map.setLayoutProperty('trips-layer', 'visibility', 'visible');
      }
      updateResetButtonVisibility();
      setTimeout(updateLegendPositions, 50);
      updateStatsVisibility();
    });
  }
  document.querySelectorAll('input[name="averagedMode"]').forEach(r => {
    r.addEventListener('change', e => {
      averagedSegmentMode = e.target.value;
      if (showAveragedSegments) updateAveragedSegmentColors();
    });
  });
}

function setupBrakingControls() {
  const cb = document.getElementById('brakingCheckbox');
  if (!cb) return;

  cb.addEventListener('change', e => {
    showBraking = e.target.checked;
    const legend     = document.getElementById('brakingLegend');
    const visibility = showBraking ? 'visible' : 'none';

    if (map.getLayer('braking-hotspots-halo')) map.setLayoutProperty('braking-hotspots-halo', 'visibility', visibility);
    if (map.getLayer('braking-hotspots-dot'))  map.setLayoutProperty('braking-hotspots-dot',  'visibility', visibility);

    if (legend) legend.style.display = showBraking ? 'block' : 'none';

    // When enabling braking, if a trip is already selected filter immediately
    if (showBraking && selectedTrip) applyBrakingTripFilter(selectedTrip);
    // When disabling, restore full hotspot data
    if (!showBraking) applyBrakingTripFilter(null);

    updateResetButtonVisibility();
    setTimeout(updateLegendPositions, 50);
    updateStatsVisibility();
  });
}

function refreshTripLayerColor() {
  if (!map.getLayer('trips-layer')) return;

  const selectedIds = getSelectedTripIds();

  if (!selectedIds) {
    map.setPaintProperty('trips-layer', 'line-color', currentColorExpression());
    map.setPaintProperty('trips-layer', 'line-opacity', 0.7);
    map.setPaintProperty('trips-layer', 'line-width', 3);
    return;
  }

  const baseExpr = currentColorExpression();
  const isSingle = selectedIds.length === 1;

  map.setPaintProperty('trips-layer', 'line-color', [
    'case',
    isSingle
      ? ['==', ['get', 'trip_id'], selectedIds[0]]
      : ['in', ['get', 'trip_id'], ['literal', selectedIds]],
    baseExpr,
    'rgba(0,0,0,0)'
  ]);
  map.setPaintProperty('trips-layer', 'line-opacity', 1);
  map.setPaintProperty('trips-layer', 'line-width', [
    'case',
    isSingle
      ? ['==', ['get', 'trip_id'], selectedIds[0]]
      : ['in', ['get', 'trip_id'], ['literal', selectedIds]],
    4,
    0
  ]);
}

function setupControls() {
  const resetBtn = document.getElementById('resetButton');
  if (resetBtn) resetBtn.addEventListener('click', () => {
    resetSelection();
    resetAccordionFilters();
  });

  const speedCb = document.getElementById('speedColorsCheckbox');
  if (speedCb) {
    speedCb.addEventListener('change', e => {
      showSpeedColors = e.target.checked;
      if (showSpeedColors && showRoadQuality) {
        showRoadQuality = false;
        document.getElementById('roadQualityCheckbox').checked     = false;
        document.getElementById('roadQualityLegend').style.display = 'none';
      }
      const legend    = document.getElementById('speedLegend');
      const modeGroup = document.getElementById('speedModeGroup');
      if (showSpeedColors) {
        if (legend)    legend.style.display    = 'block';
        if (modeGroup) modeGroup.style.display = 'flex';
      } else {
        if (legend)    legend.style.display    = 'none';
        if (modeGroup) modeGroup.style.display = 'none';
      }
      refreshTripLayerColor();
      updateResetButtonVisibility();
      setTimeout(updateLegendPositions, 50);
      updateStatsVisibility();
    });
  }

  const qualityCb = document.getElementById('roadQualityCheckbox');
  if (qualityCb) {
    qualityCb.addEventListener('change', e => {
      showRoadQuality = e.target.checked;
      if (showRoadQuality && showSpeedColors) {
        showSpeedColors = false;
        document.getElementById('speedColorsCheckbox').checked    = false;
        document.getElementById('speedLegend').style.display      = 'none';
        document.getElementById('speedModeGroup').style.display   = 'none';
      }
      const legend = document.getElementById('roadQualityLegend');
      if (showRoadQuality) {
        if (legend) legend.style.display = 'block';
      } else {
        if (legend) legend.style.display = 'none';
      }
      refreshTripLayerColor();
      updateResetButtonVisibility();
      updateLegendPositions();
      updateStatsVisibility();
    });
  }

  document.querySelectorAll('input[name="speedMode"]').forEach(r => {
    r.addEventListener('change', e => {
      speedMode = e.target.value;
      if (showSpeedColors) refreshTripLayerColor();
    });
  });

  setupAveragedSegmentControls();
  setupBrakingControls();
  setupCrashControls();
}

function updateStatsFromMetadata() {
  const source      = map.getSource('trips');
  const allFeatures = source?._data?.features || [];

  const tripStats = {};
  for (const f of allFeatures) {
    const tid   = f.properties.trip_id;
    const dist  = f.properties.gps_distance_m || 0;
    const time  = f.properties.time_diff_s    || 0;
    const speed = f.properties.Speed          || 0;

    if (!tripStats[tid]) tripStats[tid] = { dist: 0, time: 0, speeds: [] };
    tripStats[tid].dist += dist;
    tripStats[tid].time += time;
    if (speed > 0) tripStats[tid].speeds.push(speed);
  }

  let totalDist = 0, totalTime = 0, allSpeeds = [];
  for (const t of Object.values(tripStats)) {
    totalDist += t.dist;
    totalTime += t.time;
    allSpeeds.push(...t.speeds);
  }

  const avgSpeed = allSpeeds.length
    ? (allSpeeds.reduce((a, b) => a + b, 0) / allSpeeds.length).toFixed(1)
    : '—';

  document.getElementById('statTrips').textContent     = tripIds.length;
  document.getElementById('statDistance').textContent  = `${(totalDist / 1000).toFixed(1)} km`;
  document.getElementById('statAvgSpeed').textContent  = `${avgSpeed} km/h`;
  document.getElementById('statTotalTime').textContent = formatDuration(Math.round(totalTime));
}

// ─── Trip dates ───────────────────────────────────────────────────────────────
// Uses each trip's first available "timestamp" property (added by
// generate_trips_geojson.py). Older trips.geojson files generated before that
// change won't have this field, so trips just won't appear in the date map/dropdown.
function buildTripDateMap(features) {
  tripDates = {};
  for (const f of features) {
    const tid = f.properties.trip_id;
    const ts  = f.properties.timestamp;
    if (!tid || !ts || tripDates[tid]) continue;
    tripDates[tid] = ts.slice(0, 10); // 'YYYY-MM-DD'
  }
}

function getTripDate(tripId) {
  return tripDates[tripId] || null;
}

// Converts an internal 'YYYY-MM-DD' date string to display format 'DD/MM/YYYY'.
function formatDateDMY(isoDate) {
  if (!isoDate) return null;
  const [y, m, d] = isoDate.split('-');
  return `${d}/${m}/${y}`;
}

// ─── Sensor + date filter panel ────────────────────────────────────────────────
// This panel uses map.setFilter() on trips-layer to show/hide whole trips
// while leaving the layer's paint (sensor colours, speed colours, etc.)
// completely untouched — so filtered trips keep their real colours instead
// of turning pink, and this filter is independent of the click-to-select /
// click-off-to-clear "activeFilter" system used by the bottom-right sensor
// legend and route clicks.
function isoDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function applyBaseTripSetFilter(matches) {
  if (!map.getLayer('trips-layer')) return;
  map.setFilter('trips-layer', matches === null ? null : ['in', ['get', 'trip_id'], ['literal', matches]]);
}

function updateFilterMatchCount(count) {
  const el = document.getElementById('filterMatchCount');
  if (!el) return;
  if (count === null) {
    el.textContent = '';
  } else if (count === 0) {
    el.textContent = 'No trips match these filters';
  } else {
    el.textContent = `Showing ${count} of ${tripIds.length} trips`;
  }
}

function renderFilterPanel() {
  const toggleBtn  = document.getElementById('filterAccordionToggle');
  const content    = document.getElementById('filterAccordionContent');
  const accordion  = toggleBtn ? toggleBtn.closest('.filter-accordion') : null;
  const listEl     = document.getElementById('filterSensorList');
  const allCb      = document.getElementById('filterSensorAll');
  const fromInput  = document.getElementById('dateFilterFrom');
  const toInput    = document.getElementById('dateFilterTo');
  if (!toggleBtn || !content || !listEl || !allCb || !fromInput || !toInput) return;

  // Accordion open/close
  const chevron = toggleBtn.querySelector('.filter-accordion-chevron');
  toggleBtn.onclick = () => {
    content.hidden = !content.hidden;
    if (accordion) accordion.classList.toggle('open', !content.hidden);
    if (chevron) chevron.textContent = content.hidden ? '⌄' : '⌃';
  };

  // Sensors default to "all selected" = no filtering
  const sensors = Object.keys(sensorColorMap).sort();
  selectedSensorFilters = new Set(sensors);

  listEl.innerHTML = sensors.map(s => `
    <label class="filter-sensor-item">
      <input type="checkbox" class="filterSensorCheckbox" value="${s}" checked>
      <div class="speed-color-box" style="background:${sensorColorMap[s]};"></div>
      <span>${s}</span>
    </label>`).join('');

  const sensorCbs = [...listEl.querySelectorAll('.filterSensorCheckbox')];

  function syncAllCheckbox() {
    if (selectedSensorFilters.size === sensors.length) {
      allCb.checked = true; allCb.indeterminate = false;
    } else if (selectedSensorFilters.size === 0) {
      allCb.checked = false; allCb.indeterminate = false;
    } else {
      allCb.checked = false; allCb.indeterminate = true;
    }
  }

  sensorCbs.forEach(cb => {
    cb.addEventListener('change', () => {
      if (cb.checked) selectedSensorFilters.add(cb.value);
      else selectedSensorFilters.delete(cb.value);
      syncAllCheckbox();
      applyDropdownFilters();
    });
  });

  allCb.addEventListener('change', () => {
    const checkAll = allCb.checked;
    sensorCbs.forEach(cb => { cb.checked = checkAll; });
    selectedSensorFilters = new Set(checkAll ? sensors : []);
    allCb.indeterminate = false;
    applyDropdownFilters();
  });

  // Dates — auto-hide the whole "Dates" section if trips.geojson has no
  // timestamps yet (needs a pipeline regeneration)
  const dates = [...new Set(Object.values(tripDates))].filter(Boolean).sort();
  const dateSection = fromInput.closest('.filter-section');
  if (dates.length) {
    fromInput.min = toInput.min = dates[0];
    fromInput.max = toInput.max = dates[dates.length - 1];
    if (dateSection) dateSection.style.display = '';
  } else if (dateSection) {
    dateSection.style.display = 'none';
  }

  fromInput.addEventListener('change', () => {
    selectedDateFrom = fromInput.value;
    setActivePresetButton(null);
    applyDropdownFilters();
  });
  toInput.addEventListener('change', () => {
    selectedDateTo = toInput.value;
    setActivePresetButton(null);
    applyDropdownFilters();
  });

  document.querySelectorAll('.filter-preset-btn').forEach(btn => {
    btn.addEventListener('click', () => applyDatePreset(btn.dataset.preset));
  });
}

function setActivePresetButton(preset) {
  document.querySelectorAll('.filter-preset-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.preset === preset);
  });
}

function applyDatePreset(preset) {
  const today = new Date();
  let from = '', to = '';

  if (preset === 'today') {
    from = to = isoDate(today);
  } else if (preset === 'yesterday') {
    const y = new Date(today); y.setDate(y.getDate() - 1);
    from = to = isoDate(y);
  } else if (preset === 'last7') {
    const start = new Date(today); start.setDate(start.getDate() - 6);
    from = isoDate(start); to = isoDate(today);
  } else if (preset === 'last30') {
    const start = new Date(today); start.setDate(start.getDate() - 29);
    from = isoDate(start); to = isoDate(today);
  } // 'all' (or anything else) leaves from/to as ''

  selectedDateFrom = from;
  selectedDateTo   = to;

  const fromInput = document.getElementById('dateFilterFrom');
  const toInput   = document.getElementById('dateFilterTo');
  if (fromInput) fromInput.value = from;
  if (toInput)   toInput.value   = to;

  setActivePresetButton(preset);
  applyDropdownFilters();
}

function applyDropdownFilters() {
  const sensors = Object.keys(sensorColorMap);
  const noSensorFilter = selectedSensorFilters.size === sensors.length;
  const noDateFilter    = !selectedDateFrom && !selectedDateTo;

  if (noSensorFilter && noDateFilter) {
    applyBaseTripSetFilter(null);
    updateFilterMatchCount(null);
    updateResetButtonVisibility();
    return;
  }

  let matches = tripIds;
  if (!noSensorFilter) matches = matches.filter(id => selectedSensorFilters.has(id.split('_')[0]));
  if (!noDateFilter) {
    matches = matches.filter(id => {
      const d = tripDates[id];
      if (!d) return false;
      if (selectedDateFrom && d < selectedDateFrom) return false;
      if (selectedDateTo   && d > selectedDateTo)   return false;
      return true;
    });
  }

  applyBaseTripSetFilter(matches);
  updateFilterMatchCount(matches.length);
  updateResetButtonVisibility();
}

function resetAccordionFilters() {
  selectedSensorFilters = new Set(Object.keys(sensorColorMap));
  selectedDateFrom = '';
  selectedDateTo   = '';

  const allCb = document.getElementById('filterSensorAll');
  if (allCb) { allCb.checked = true; allCb.indeterminate = false; }
  document.querySelectorAll('.filterSensorCheckbox').forEach(cb => { cb.checked = true; });

  const fromInput = document.getElementById('dateFilterFrom');
  const toInput   = document.getElementById('dateFilterTo');
  if (fromInput) fromInput.value = '';
  if (toInput)   toInput.value   = '';
  setActivePresetButton(null);

  applyBaseTripSetFilter(null);
  updateFilterMatchCount(null);
  updateResetButtonVisibility();
}

function renderSensorLegend() {
  const legend = document.getElementById('sensorLegend');
  if (!legend) return;
  legend.innerHTML = `<h4>Sensors</h4>` + Object.entries(sensorColorMap).map(([s, c]) => `
    <div class="speed-legend-item sensor-legend-item" data-sensor="${s}" style="cursor:pointer;" title="Click to highlight ${s}">
      <div class="speed-color-box" style="background:${c};"></div>
      <span>${s}</span>
    </div>`).join('');

  legend.querySelectorAll('.sensor-legend-item').forEach(item => {
    item.addEventListener('click', () => {
      highlightSensor(item.dataset.sensor);
    });
  });

  legend.style.display = 'block';
  updateLegendPositions();
}