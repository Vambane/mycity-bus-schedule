/**
 * Timetable ghost position for the static map.
 * Port of fastapi_app/services/trip_preview.py and journey_map._street_slice.
 * Clock: Africa/Johannesburg. Caption is fixed and must not claim live GPS.
 */
import {capeTownParts, clockTime, parseInstant, toMinutes} from "./planner.mjs";

const CAPTION = "Estimated from timetable — not live GPS.";
const OPTIONAL = "Planning aid only. Confirm on MyCiTi / UCT before you travel.";
const OUTSIDE = "Outside trip window — showing scheduled start/end";
const TOL_SQ = 0.005 ** 2;

function muteColor(hex) {
  let raw = String(hex || "#666666").replace("#", "");
  if (raw.length !== 6) raw = "666666";
  const mix = (i) => {
    const c = parseInt(raw.slice(i, i + 2), 16);
    return Math.round(c * 0.45 + 128 * 0.55);
  };
  const r = mix(0);
  const g = mix(2);
  const b = mix(4);
  return `#${r.toString(16).padStart(2, "0")}${g.toString(16).padStart(2, "0")}${b.toString(16).padStart(2, "0")}`;
}

function pathLength(path) {
  let total = 0;
  for (let i = 1; i < path.length; i += 1) {
    const a = path[i - 1];
    const b = path[i];
    const k = Math.cos(a[0] * Math.PI / 180);
    total += Math.hypot(b[0] - a[0], (b[1] - a[1]) * k);
  }
  return total;
}

function pointAlong(path, fraction) {
  if (!path.length) return [0, 0];
  if (path.length === 1 || fraction <= 0) return path[0].slice();
  if (fraction >= 1) return path[path.length - 1].slice();
  const target = pathLength(path) * fraction;
  let walked = 0;
  for (let i = 1; i < path.length; i += 1) {
    const a = path[i - 1];
    const b = path[i];
    const k = Math.cos(a[0] * Math.PI / 180);
    const seg = Math.hypot(b[0] - a[0], (b[1] - a[1]) * k);
    if (walked + seg >= target && seg > 0) {
      const t = (target - walked) / seg;
      return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
    }
    walked += seg;
  }
  return path[path.length - 1].slice();
}

function nearestIndex(path, point) {
  const k = Math.cos(point[0] * Math.PI / 180);
  let bestI = 0;
  let bestD = Infinity;
  for (let i = 0; i < path.length; i += 1) {
    const [lat, lon] = path[i];
    const d = (lat - point[0]) ** 2 + ((lon - point[1]) * k) ** 2;
    if (d < bestD) {
      bestI = i;
      bestD = d;
    }
  }
  return [bestI, bestD];
}

export function streetSlice(paths, boardPt, alightPt) {
  let bestScore = Infinity;
  let best = null;
  for (const path of paths || []) {
    const [i, di] = nearestIndex(path, boardPt);
    const [j, dj] = nearestIndex(path, alightPt);
    if (Math.max(di, dj) > TOL_SQ) continue;
    if (di + dj < bestScore) {
      bestScore = di + dj;
      best = [path, i, j];
    }
  }
  if (!best) return null;
  const [path, i, j] = best;
  if (i === j) return null;
  let segment = path.slice(Math.min(i, j), Math.max(i, j) + 1);
  if (j < i) segment = segment.slice().reverse();
  return segment;
}

function normTime(value) {
  const raw = String(value || "").trim();
  if (raw.length === 5 && raw[2] === ":") return `${raw}:00`;
  return raw;
}

/**
 * Build the ghost payload. `line` is an indexed timetable line (or null).
 * `coords` maps stop name to [lat, lon]. `paths` is the route's street geometry.
 */
export function buildPreview({line, coords, paths, routeColor, spec, now}) {
  const base = {
    eligible: false,
    hide_reason: "no_geometry",
    caption: CAPTION,
    caption_optional: OPTIONAL,
    clock_zone: "Africa/Johannesburg",
  };
  if (!line) return base;
  const seq = [];
  const i0 = line.order.indexOf(spec.board);
  const i1 = line.order.indexOf(spec.alight);
  if (i0 < 0 || i1 <= i0) return base;
  for (let i = i0; i <= i1; i += 1) seq.push(line.order[i]);
  const boardTimes = line.stops[spec.board][1];
  const n = boardTimes.length;
  if (!n || seq.some((stop) => line.stops[stop][1].length !== n)) {
    return {...base, hide_reason: "soft"};
  }
  const depN = normTime(spec.dep);
  const depHm = depN.slice(0, 5);
  const idx = boardTimes.findIndex((t) => normTime(t) === depN || normTime(t).slice(0, 5) === depHm);
  if (idx < 0) return base;
  const timed = seq.map((name) => {
    const point = coords[name];
    const row = {name, time: normTime(line.stops[name][1][idx])};
    if (point) {
      row.lat = Math.round(point[0] * 1e5) / 1e5;
      row.lon = Math.round(point[1] * 1e5) / 1e5;
    }
    return row;
  });
  const schematic = timed.filter((row) => row.lat != null).map((row) => [row.lat, row.lon]);
  if (schematic.length < 2) return base;
  const street = streetSlice(paths, schematic[0], schematic[schematic.length - 1]);
  const path = street || schematic;
  const parts = capeTownParts(parseInstant(now));
  const nowMin = parts.hour * 60 + parts.minute;
  const startMin = toMinutes(timed[0].time);
  const endMin = toMinutes(timed[timed.length - 1].time);
  let fraction = 0;
  let outside = false;
  if (nowMin < startMin) {
    fraction = 0;
    outside = true;
  } else if (nowMin >= endMin) {
    fraction = 1;
    outside = true;
  } else {
    for (let i = 0; i < timed.length - 1; i += 1) {
      const a = toMinutes(timed[i].time);
      const b = toMinutes(timed[i + 1].time);
      if (nowMin < a) break;
      if (nowMin <= b && b > a) {
        const local = (nowMin - a) / (b - a);
        const fa = i / (timed.length - 1);
        const fb = (i + 1) / (timed.length - 1);
        fraction = fa + local * (fb - fa);
        break;
      }
      fraction = (i + 1) / (timed.length - 1);
    }
  }
  const ghost = pointAlong(path, fraction);
  const captions = [CAPTION, OPTIONAL];
  if (outside) captions.unshift(OUTSIDE);
  return {
    ...base,
    eligible: true,
    hide_reason: null,
    route_id: spec.route_id,
    direction: spec.direction,
    day_type: spec.day_type,
    board: spec.board,
    alight: spec.alight,
    dep: clockTime(normTime(spec.dep)),
    arr: clockTime(timed[timed.length - 1].time),
    route_color: routeColor || "#666666",
    ghost_color: muteColor(routeColor),
    ghost_opacity: 0.45,
    geometry_source: street ? "street" : "schematic",
    path,
    stops: timed,
    ghost: {lat: Math.round(ghost[0] * 1e5) / 1e5, lon: Math.round(ghost[1] * 1e5) / 1e5},
    outside_window: outside,
    captions,
    now: `${String(parts.hour).padStart(2, "0")}:${String(parts.minute).padStart(2, "0")}`,
  };
}

const cache = {};

async function loadJson(url) {
  if (cache[url]) return cache[url];
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  cache[url] = data;
  return data;
}

/**
 * Browser entry used by the map iframe. Reads HALTE_DATA (data directory URL).
 */
export async function buildFromQuery(graph, params) {
  const root = window.HALTE_DATA || "/data";
  const day = params.get("day_type");
  const [meta, dayPayload] = await Promise.all([
    loadJson(`${root}/meta.json`),
    loadJson(`${root}/timetable/${day}.json`),
  ]);
  const routeId = params.get("route_id");
  const direction = params.get("direction");
  const raw = (dayPayload.lines || []).find((line) => line.r === routeId && line.d === direction);
  let indexed = null;
  if (raw) {
    const stops = {};
    raw.s.forEach((name, i) => {
      stops[name] = [raw.t[i][0], raw.t[i]];
    });
    indexed = {order: raw.s, stops};
  }
  const coords = {};
  for (const node of graph.nodes || []) coords[node.id] = [node.lat, node.lon];
  const route = (meta.routes || {})[routeId] || {};
  return buildPreview({
    line: indexed,
    coords,
    paths: (graph.paths || {})[String(routeId || "").toUpperCase()] || [],
    routeColor: route.color,
    spec: {
      route_id: routeId,
      direction,
      day_type: day,
      board: params.get("board"),
      alight: params.get("alight"),
      dep: params.get("dep"),
      arr: params.get("arr"),
    },
    now: new Date(),
  });
}

if (typeof window !== "undefined") {
  window.HaltePreview = {buildFromQuery, buildPreview};
  if (typeof window.__haltePreviewResolve === "function") window.__haltePreviewResolve();
}
