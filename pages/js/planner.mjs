/**
 * Browser timetable planner for the static Halte site.
 *
 * This is a direct port of journey.py, fastapi_app/services/service_day.py,
 * trip_window.py, uct_validity.py and the search/stop filtering in
 * fastapi_app/routes/search.py and stop.py. Times stay as zero-padded
 * "HH:MM:SS" strings so comparisons match the Python oracle, including
 * trips stored past 24:00. The clock is Africa/Johannesburg (UTC+2, no DST).
 * Never use the viewer's local timezone for a timetable decision.
 */

export const ZONE = "Africa/Johannesburg";
export const ZONE_OFFSET = "+02:00";

const WEEKDAY = "weekday";
const SATURDAY = "saturday";
const SUNDAY = "sunday";
const DAY_LABELS = {weekday: "weekday", saturday: "Saturday", sunday: "Sunday"};
const OPERATORS = new Set(["both", "myciti", "uct", "metrorail"]);
const DAY_TYPES = new Set([WEEKDAY, SATURDAY, SUNDAY]);

const MIN_TRANSFER_MIN = 3;
const MAX_WAIT_MIN = 45;
const MAX_LEG_MIN = 300;
const MAX_TOTAL_MIN = 420;
const MAX_HOPS = 4;
const OVERNIGHT_UNTIL_HOUR = 6;
const STALE_MAX_DAYS = 45;

const FIXED_HOLIDAYS = {
  "1-1": "New Year's Day",
  "3-21": "Human Rights Day",
  "4-27": "Freedom Day",
  "5-1": "Workers' Day",
  "6-16": "Youth Day",
  "8-9": "National Women's Day",
  "9-24": "Heritage Day",
  "12-16": "Day of Reconciliation",
  "12-25": "Christmas Day",
  "12-26": "Day of Goodwill",
};

const RAIL_SHORT = {
  "MR-S": "Southern",
  "MR-CF": "Cape Flats",
  "MR-C": "Central",
  "MR-N": "Northern",
  "MR-M": "Malmesbury",
};
const PRASA_URL = "https://www.prasa.com/train-schedules/";
const STALE_AFTER_DAYS = 180;

const WEEKEND = [SATURDAY, SUNDAY];
const ROSCOMMON_WEEKEND =
  "Routes 13/14 at weekends, 15:35–21:50: board and alight at Roscommon House.";
const UCT_NOTES = {
  UCT1: [[ [WEEKDAY], "Route 1: Claremont departures from 18:55 on weekdays leave from Roscommon House." ]],
  UCT2: [[ null, "Route 2: runs via Kramer after 13:25." ]],
  UCT6: [[ null, "Route 6: runs via Mowbray at 06:40 and 07:07." ]],
  UCT11: [[ null, "Route 11: only goes via South Stop from 12:25 to 17:40." ]],
  UCT13: [[ WEEKEND, ROSCOMMON_WEEKEND ]],
  UCT14: [[ WEEKEND, ROSCOMMON_WEEKEND ]],
  UCTOVN: [[ null, "Overnight library loop: UCT student or staff card, or a visitor's pass, needed." ]],
};

const MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

const HHMM = /^([01]?\d|2[0-3]):([0-5]\d)$/;

export function capeTownParts(date) {
  const fmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });
  const bag = {};
  for (const part of fmt.formatToParts(date)) bag[part.type] = part.value;
  let hour = Number(bag.hour);
  let day = Number(bag.day);
  let month = Number(bag.month);
  let year = Number(bag.year);
  if (hour === 24) hour = 0;
  const iso = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  const hms = `${String(hour).padStart(2, "0")}:${bag.minute}:${bag.second}`;
  return {year, month, day, hour, minute: Number(bag.minute), second: Number(bag.second), iso, hms};
}

export function parseInstant(value) {
  if (value instanceof Date) return value;
  if (!value) return new Date();
  return new Date(value);
}

function pad(n, w = 2) {
  return String(n).padStart(w, "0");
}

export function addDays(iso, n) {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + n);
  return dt.toISOString().slice(0, 10);
}

function weekdayMon0(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  const js = new Date(Date.UTC(y, m - 1, d)).getUTCDay();
  return (js + 6) % 7;
}

export function easterSunday(year) {
  const a = year % 19;
  const b = Math.floor(year / 100);
  const c = year % 100;
  const d = Math.floor(b / 4);
  const e = b % 4;
  const f = Math.floor((b + 8) / 25);
  const g = Math.floor((b - f + 1) / 3);
  const h = (19 * a + b - d - g + 15) % 30;
  const i = Math.floor(c / 4);
  const k = c % 4;
  const l = (32 + 2 * e + 2 * i - h - k) % 7;
  const m = Math.floor((a + 11 * h + 22 * l) / 451);
  const monthDay = h + l - 7 * m + 114;
  const month = Math.floor(monthDay / 31);
  const day = (monthDay % 31) + 1;
  return `${year}-${pad(month)}-${pad(day)}`;
}

export function publicHolidays(year, extra = []) {
  const holidays = {};
  for (const [key, name] of Object.entries(FIXED_HOLIDAYS)) {
    const [month, day] = key.split("-").map(Number);
    holidays[`${year}-${pad(month)}-${pad(day)}`] = name;
  }
  const easter = easterSunday(year);
  holidays[addDays(easter, -2)] = "Good Friday";
  holidays[addDays(easter, 1)] = "Family Day";
  for (const [day, name] of Object.entries({...holidays})) {
    if (weekdayMon0(day) === 6) {
      const monday = addDays(day, 1);
      if (!holidays[monday]) holidays[monday] = `${name} (observed)`;
    }
  }
  for (const item of extra || []) {
    if (String(item.date).startsWith(String(year))) holidays[item.date] = item.name;
  }
  return holidays;
}

export function holidayName(iso, extra = []) {
  const year = Number(iso.slice(0, 4));
  return publicHolidays(year, extra)[iso] || null;
}

export function dayTypeFor(iso, extra = []) {
  if (holidayName(iso, extra) || weekdayMon0(iso) === 6) return SUNDAY;
  if (weekdayMon0(iso) === 5) return SATURDAY;
  return WEEKDAY;
}

export function toMinutes(hms) {
  const [h, m] = String(hms).split(":");
  return Number(h) * 60 + Number(m);
}

export function clockTime(hms) {
  const raw = String(hms || "");
  const parts = raw.split(":");
  if (parts.length < 2 || !/^\d+$/.test(parts[0]) || !/^\d+$/.test(parts[1])) return raw.slice(0, 5);
  return `${pad(Number(parts[0]) % 24)}:${pad(Number(parts[1]))}`;
}

export function fmtDuration(minutes) {
  if (minutes >= 60) return `${Math.floor(minutes / 60)} h ${pad(minutes % 60)}`;
  return `${minutes} min`;
}

function cmp(a, b) {
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

function pairTimes(fromTimes, toTimes) {
  if (fromTimes.length === toTimes.length) {
    return fromTimes.map((dep, i) => [dep, toTimes[i]]);
  }
  const pairs = [];
  for (const dep of fromTimes) {
    let best = null;
    for (const t of toTimes) {
      if (t > dep && (best === null || t < best)) best = t;
    }
    if (best !== null) pairs.push([dep, best]);
  }
  return pairs;
}

export function indexDay(payload, routes) {
  const lines = [];
  const byStop = new Map();
  const byKey = new Map();
  for (const raw of payload.lines || []) {
    const meta = routes[raw.r] || {name: raw.r, operator: raw.o || "myciti", color: "#666666"};
    const stops = {};
    raw.s.forEach((name, i) => {
      const times = raw.t[i];
      stops[name] = [times[0], times];
    });
    const line = {
      routeId: raw.r,
      direction: raw.d,
      operator: raw.o || meta.operator,
      routeName: meta.name || raw.r,
      color: meta.color || "#666666",
      stops,
      order: raw.s.slice(),
    };
    lines.push(line);
    byKey.set(`${raw.r}\0${raw.d}`, line);
    for (const name of line.order) {
      if (!byStop.has(name)) byStop.set(name, []);
      byStop.get(name).push(line);
    }
  }
  return {lines, byStop, byKey};
}

export function createCatalog(meta, dayPayloads) {
  const days = {};
  for (const [day, payload] of Object.entries(dayPayloads)) {
    days[day] = indexDay(payload, meta.routes || {});
  }
  return {meta, days};
}

function linesFor(dayIndex, operator) {
  if (!dayIndex) return [];
  if (!operator || operator === "both") return dayIndex.lines;
  return dayIndex.lines.filter((line) => line.operator === operator);
}

function lineKey(line) {
  return [line.routeId, line.direction];
}

export function findConnections(dayIndex, fromStop, toStop, operator = "both") {
  const rows = [];
  for (const line of linesFor(dayIndex, operator)) {
    const from = line.stops[fromStop];
    const to = line.stops[toStop];
    if (!from || !to || !(from[0] < to[0])) continue;
    const minRide = String(line.routeId).startsWith("MR-") ? 1 : 5;
    for (const [dep, arr] of pairTimes(from[1], to[1])) {
      const minutes = toMinutes(arr) - toMinutes(dep);
      if (minutes >= minRide && minutes <= 300) {
        rows.push({
          route_id: line.routeId,
          route_name: line.routeName,
          direction: line.direction,
          dep,
          arr,
          duration_min: minutes,
          duration: fmtDuration(minutes),
          operator: line.operator,
        });
      }
    }
  }
  rows.sort((a, b) => cmp(a.dep, b.dep));
  return rows;
}

function legPairs(stops, s1, s2) {
  if (!(stops[s1][0] < stops[s2][0])) return [];
  const pairs = [];
  for (const [dep, arr] of pairTimes(stops[s1][1], stops[s2][1])) {
    const minutes = toMinutes(arr) - toMinutes(dep);
    if (minutes > 0 && minutes <= MAX_LEG_MIN) {
      pairs.push([dep, arr, toMinutes(dep), toMinutes(arr)]);
    }
  }
  return pairs;
}

function nextLeg(pairs, arrivedMin) {
  const lo = arrivedMin + MIN_TRANSFER_MIN;
  const hi = arrivedMin + MAX_WAIT_MIN;
  for (const pair of pairs) {
    if (pair[2] >= lo) return pair[2] <= hi ? pair : null;
  }
  return null;
}

function makeLeg(line, board, alight, pair) {
  return {
    route_id: line.routeId,
    direction: line.direction,
    board,
    alight,
    dep: pair[0],
    arr: pair[1],
    wait_min: 0,
  };
}

function itineraryRow(legs, routeNames) {
  const copy = legs.map((leg) => ({...leg}));
  for (let i = 1; i < copy.length; i += 1) {
    copy[i].wait_min = toMinutes(copy[i].dep) - toMinutes(copy[i - 1].arr);
  }
  for (const leg of copy) {
    leg.route_name = routeNames[leg.route_id] || leg.route_id;
  }
  const total = toMinutes(copy[copy.length - 1].arr) - toMinutes(copy[0].dep);
  return {
    dep: copy[0].dep,
    arr: copy[copy.length - 1].arr,
    duration_min: total,
    duration: fmtDuration(total),
    transfers: copy.length - 1,
    route_ids: copy.map((leg) => leg.route_id),
    via: copy.slice(1).map((leg) => leg.board),
    legs: copy,
  };
}

function oneTransferRows(linesFrom, linesTo, fromStop, toStop, routeNames) {
  const rows = [];
  for (const la of linesFrom) {
    for (const lb of linesTo) {
      if (la.routeId === lb.routeId) continue;
      const shared = [];
      for (const name of Object.keys(la.stops)) {
        if (!lb.stops[name] || name === fromStop || name === toStop) continue;
        if (!(la.stops[name][0] > la.stops[fromStop][0])) continue;
        if (!(lb.stops[name][0] < lb.stops[toStop][0])) continue;
        shared.push(name);
      }
      shared.sort((a, b) => cmp(la.stops[a][0], la.stops[b][0]));
      for (const hop of shared.slice(0, MAX_HOPS)) {
        const leg2 = legPairs(lb.stops, hop, toStop);
        if (!leg2.length) continue;
        for (const pair1 of legPairs(la.stops, fromStop, hop)) {
          const pair2 = nextLeg(leg2, pair1[3]);
          if (!pair2 || pair2[3] - pair1[2] > MAX_TOTAL_MIN) continue;
          rows.push(itineraryRow([
            makeLeg(la, fromStop, hop, pair1),
            makeLeg(lb, hop, toStop, pair2),
          ], routeNames));
        }
      }
    }
  }
  return rows;
}

function threeLegRows(la, lm, lb, fromStop, x1, x2, toStop, routeNames) {
  const mid = legPairs(lm.stops, x1, x2);
  const last = legPairs(lb.stops, x2, toStop);
  if (!mid.length || !last.length) return [];
  const rows = [];
  for (const pair1 of legPairs(la.stops, fromStop, x1)) {
    const pair2 = nextLeg(mid, pair1[3]);
    if (!pair2) continue;
    const pair3 = nextLeg(last, pair2[3]);
    if (!pair3 || pair3[3] - pair1[2] > MAX_TOTAL_MIN) continue;
    rows.push(itineraryRow([
      makeLeg(la, fromStop, x1, pair1),
      makeLeg(lm, x1, x2, pair2),
      makeLeg(lb, x2, toStop, pair3),
    ], routeNames));
  }
  return rows;
}

function twoTransferRows(lines, linesFrom, linesTo, fromStop, toStop, routeNames) {
  const rows = [];
  for (const la of linesFrom) {
    const xs1All = la.order.filter((name) => name !== fromStop && la.stops[name][0] > la.stops[fromStop][0]);
    for (const lm of lines) {
      if (lm.routeId === la.routeId) continue;
      const xs1 = xs1All.filter((name) => lm.stops[name] && name !== toStop);
      xs1.sort((a, b) => cmp(la.stops[a][0], la.stops[b][0]));
      const hops1 = xs1.slice(0, MAX_HOPS);
      if (!hops1.length) continue;
      for (const lb of linesTo) {
        if (lb.routeId === la.routeId || lb.routeId === lm.routeId) continue;
        const xs2All = [];
        for (const name of Object.keys(lb.stops)) {
          if (!lm.stops[name] || name === fromStop || name === toStop) continue;
          if (!(lb.stops[name][0] < lb.stops[toStop][0])) continue;
          xs2All.push(name);
        }
        if (!xs2All.length) continue;
        for (const x1 of hops1) {
          const xs2 = xs2All.filter((name) => lm.stops[name][0] > lm.stops[x1][0]);
          xs2.sort((a, b) => cmp(lm.stops[a][0], lm.stops[b][0]));
          for (const x2 of xs2.slice(0, MAX_HOPS)) {
            rows.push(...threeLegRows(la, lm, lb, fromStop, x1, x2, toStop, routeNames));
          }
        }
      }
    }
  }
  return rows;
}

function pareto(rows) {
  const sorted = rows.slice().sort((a, b) => cmp(a.dep, b.dep) || cmp(a.arr, b.arr) || (a.transfers - b.transfers));
  const best = new Map();
  for (const row of sorted) {
    if (!best.has(row.dep)) best.set(row.dep, row);
  }
  const kept = [];
  let bestArr = null;
  const deps = [...best.keys()].sort((a, b) => cmp(b, a));
  for (const dep of deps) {
    const row = best.get(dep);
    if (bestArr === null || row.arr < bestArr) {
      kept.push(row);
      bestArr = row.arr;
    }
  }
  kept.reverse();
  return kept;
}

export function findTransferConnections(dayIndex, fromStop, toStop, operator = "both", maxTransfers = 2) {
  const lines = linesFor(dayIndex, operator);
  const linesFrom = lines.filter((line) => line.stops[fromStop]);
  const linesTo = lines.filter((line) => line.stops[toStop]);
  if (!linesFrom.length || !linesTo.length || fromStop === toStop) return [];
  const routeNames = {};
  for (const line of lines) routeNames[line.routeId] = line.routeName;
  let rows = oneTransferRows(linesFrom, linesTo, fromStop, toStop, routeNames);
  if (!rows.length && maxTransfers >= 2) {
    rows = twoTransferRows(lines, linesFrom, linesTo, fromStop, toStop, routeNames);
  }
  return pareto(rows);
}

function routeLabel(routeId) {
  const num = routeId.toUpperCase().startsWith("UCT") ? routeId.slice(3) : routeId;
  return num.toUpperCase() === "OVN" ? "Overnight library loop" : `Route ${num}`;
}

export function expiredUct(meta, todayIso) {
  const expired = {};
  for (const [routeId, span] of Object.entries(meta.uct_validity || {})) {
    if (span.until && span.until < todayIso) expired[routeId] = span.until;
  }
  return expired;
}

export function expiryWarning(expired) {
  const ids = Object.keys(expired);
  if (!ids.length) return null;
  const last = ids.map((id) => expired[id]).sort().at(-1);
  const labels = ids.map(routeLabel).sort((a, b) => {
    const ar = a.startsWith("Route ") ? 0 : 1;
    const br = b.startsWith("Route ") ? 0 : 1;
    if (ar !== br) return ar - br;
    const an = /^\d+$/.test(a.slice(6)) ? Number(a.slice(6)) : 0;
    const bn = /^\d+$/.test(b.slice(6)) ? Number(b.slice(6)) : 0;
    if (an !== bn) return an - bn;
    return cmp(a, b);
  });
  const [y, m, d] = last.split("-").map(Number);
  const stamp = `${pad(d)} ${MONTHS_SHORT[m - 1]} ${y}`;
  return `UCT shuttle timetable expired on ${stamp} (${labels.join(", ")}). These times are no longer shown until UCT publishes a new timetable.`;
}

export function uctNotes(routeIds, dayType) {
  const ids = typeof routeIds === "string" ? [routeIds] : (routeIds || []);
  const out = [];
  for (const id of ids) {
    for (const [days, text] of UCT_NOTES[id] || []) {
      if (days === null || dayType == null || days.includes(dayType)) {
        if (!out.includes(text)) out.push(text);
      }
    }
  }
  return out;
}

export function badgeLabel(routeId) {
  return RAIL_SHORT[String(routeId || "")] || String(routeId || "");
}

function fmtLong(iso) {
  if (!iso) return null;
  const [y, m, d] = iso.split("-").map(Number);
  return `${d} ${MONTHS_LONG[m - 1]} ${y}`;
}

function daysBetween(later, earlier) {
  const [y1, m1, d1] = later.split("-").map(Number);
  const [y2, m2, d2] = earlier.split("-").map(Number);
  const a = Date.UTC(y1, m1 - 1, d1);
  const b = Date.UTC(y2, m2 - 1, d2);
  return Math.round((a - b) / 86400000);
}

export function metrorailNotice(meta, dayType, todayIso) {
  if (dayType === SUNDAY) {
    return {
      title: "No Metrorail trains on Sundays",
      body: "Metrorail doesn't run on Sundays. On public holidays PRASA runs a separate holiday timetable that Halte doesn't show yet, so check prasa.com before you travel.",
      url: PRASA_URL,
    };
  }
  if (dayType !== SATURDAY) return null;
  const rows = meta.rail_saturday || [];
  const old = rows.filter((row) => !row.valid_from || daysBetween(todayIso, row.valid_from) > STALE_AFTER_DAYS);
  if (!old.length) return null;
  const parts = old.map((row) => (row.valid_from ? `${row.name} (${fmtLong(row.valid_from)})` : `${row.name} (undated)`));
  return {
    title: "Some Saturday train times are old",
    body: `These Saturday sheets are the newest Halte has: ${parts.join(", ")}. PRASA may have changed them since, so check before you travel.`,
    url: PRASA_URL,
  };
}

export function servesRail(meta, names) {
  const rail = new Set(meta.rail_stops || []);
  return (names || []).some((name) => name && rail.has(name));
}

function parseWindow(when, at) {
  const mode = String(when || "now").trim().toLowerCase();
  if (mode !== "depart" && mode !== "arrive") return {mode: "now", at: null, atHhmm: ""};
  const match = HHMM.exec(String(at || "").trim());
  if (!match) return {mode: "now", at: null, atHhmm: ""};
  const hms = `${pad(Number(match[1]))}:${match[2]}:00`;
  return {mode, at: hms, atHhmm: hms.slice(0, 5)};
}

function dropExpiredDirect(rows, expired) {
  if (!expired || !Object.keys(expired).length) return rows;
  return rows.filter((row) => !expired[row.route_id]);
}

function dropExpiredTransfers(rows, expired) {
  if (!expired || !Object.keys(expired).length) return rows;
  return rows.filter((row) => row.route_ids.every((id) => !expired[id]));
}

function withOvernight(rows, dayIndex, fromStop, toStop, operator, expired, nowParts, yesterdayType) {
  const tagged = rows.map((row) => ({...row, service_day_type: null, overnight: false}));
  if (!yesterdayType) return tagged;
  const yFrom = `${pad(nowParts.hour + 24)}:${pad(nowParts.minute)}:${pad(nowParts.second)}`;
  let extra = dropExpiredDirect(findConnections(dayIndex, fromStop, toStop, operator), expired);
  extra = extra.filter((row) => row.dep >= yFrom).map((row) => ({
    ...row,
    service_day_type: yesterdayType,
    overnight: true,
  }));
  if (!extra.length) return tagged;
  if (!tagged.length) return extra;
  return extra.concat(tagged);
}

function inClockOrder(rows) {
  return rows.slice().sort((a, b) => {
    const am = toMinutes(a.dep) - (a.overnight ? 24 * 60 : 0);
    const bm = toMinutes(b.dep) - (b.overnight ? 24 * 60 : 0);
    return am - bm || cmp(a.dep, b.dep);
  });
}

function arrivingBy(rows, at, notBefore) {
  const keep = rows.filter((row) => row.arr <= at && row.dep >= notBefore);
  keep.sort((a, b) => cmp(b.dep, a.dep) || cmp(a.arr, b.arr));
  return keep;
}

function lastDepartureBy(rows, at) {
  let best = null;
  for (const row of rows) {
    if (row.arr <= at && (best === null || row.dep > best)) best = row.dep;
  }
  return best;
}

function legSequence(dayIndex, routeId, direction, board, alight) {
  const line = dayIndex && dayIndex.byKey.get(`${routeId}\0${direction}`);
  if (!line) return [];
  const i = line.order.indexOf(board);
  const j = line.order.indexOf(alight);
  if (i < 0 || j < 0 || i >= j) return [];
  return line.order.slice(i, j + 1);
}

function scrapedDate(meta) {
  const raw = meta.scraped_at;
  if (!raw) return null;
  return new Date(String(raw).replace(" ", "T").slice(0, 19) + ZONE_OFFSET);
}

export function previewOk(catalog, spec, nowDate) {
  const meta = catalog.meta;
  const scraped = scrapedDate(meta);
  if (!scraped) return false;
  const now = nowDate instanceof Date ? nowDate : parseInstant(nowDate);
  if (now - scraped > STALE_MAX_DAYS * 86400000) return false;
  const parts = capeTownParts(now);
  const extra = meta.extra_holidays || [];
  if (spec.day_type !== dayTypeFor(parts.iso, extra)) return false;
  const dayIndex = catalog.days[spec.day_type];
  const seq = legSequence(dayIndex, spec.route_id, spec.direction, spec.board, spec.alight);
  if (seq.length < 2) return false;
  const line = dayIndex.byKey.get(`${spec.route_id}\0${spec.direction}`);
  const boardTimes = (line.stops[spec.board] || [null, []])[1];
  if (!boardTimes.length) return false;
  const n = boardTimes.length;
  if (seq.some((stop) => (line.stops[stop] || [null, []])[1].length !== n)) return false;
  const depHm = String(spec.dep).slice(0, 5);
  const idx = boardTimes.findIndex((t) => t === spec.dep || t.slice(0, 5) === depHm || String(spec.dep).length === 5 && t.slice(0, 5) === spec.dep);
  if (idx < 0) return false;
  let arr = spec.arr;
  if (!arr) {
    const alightTimes = line.stops[spec.alight][1];
    arr = idx < alightTimes.length ? alightTimes[idx] : null;
  }
  if (arr) {
    const nowMin = parts.hour * 60 + parts.minute;
    if (nowMin > toMinutes(arr)) return false;
  }
  return true;
}

function projectDirect(rows, catalog, fromStop, toStop, operator, resultsDay, nowDate) {
  return rows.map((row) => {
    const rowDay = typeof row.service_day_type === "string" ? row.service_day_type : resultsDay;
    const dep = row.dep.slice(0, 5);
    const arr = row.arr.slice(0, 5);
    return {
      route_id: row.route_id,
      route_name: row.route_name,
      direction: row.direction,
      dep: row.dep,
      arr: row.arr,
      dep_hhmm: dep,
      arr_hhmm: arr,
      duration_min: row.duration_min,
      duration: row.duration,
      overnight: Boolean(row.overnight),
      service_day_type: rowDay,
      preview_ok: previewOk(catalog, {
        route_id: row.route_id,
        direction: row.direction,
        day_type: rowDay,
        board: fromStop,
        alight: toStop,
        dep,
        arr,
      }, nowDate),
      journey_href: journeyHrefDirect({
        fromStop, toStop, dayType: rowDay, routeId: row.route_id,
        direction: row.direction, dep, arr, operator,
      }),
    };
  });
}

function projectTransfers(rows, catalog, fromStop, toStop, operator, resultsDay, nowDate) {
  return rows.map((row) => {
    const first = row.legs[0];
    const dep = row.dep.slice(0, 5);
    const arr = row.arr.slice(0, 5);
    const preview = previewOk(catalog, {
      route_id: first.route_id,
      direction: first.direction,
      day_type: resultsDay,
      board: first.board,
      alight: first.alight,
      dep: String(first.dep).slice(0, 5),
      arr: String(first.arr).slice(0, 5),
    }, nowDate);
    return {
      ...row,
      dep_hhmm: dep,
      arr_hhmm: arr,
      num_transfers: row.route_ids.length - 1,
      preview_ok: preview,
      preview_route_id: first.route_id,
      preview_direction: first.direction,
      preview_board: first.board,
      preview_alight: first.alight,
      preview_dep: String(first.dep).slice(0, 5),
      journey_href: journeyHrefTransfer({
        fromStop, toStop, dayType: resultsDay, legs: row.legs, operator,
      }),
    };
  });
}

function qs(params) {
  const u = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value != null && value !== "") u.set(key, value);
  }
  return u.toString();
}

export function journeyHrefDirect(spec) {
  return `/journey?${qs({
    from_stop: spec.fromStop,
    to_stop: spec.toStop,
    day_type: spec.dayType,
    route_id: spec.routeId,
    direction: spec.direction,
    dep: spec.dep,
    arr: spec.arr,
    operator: OPERATORS.has(spec.operator) ? spec.operator : "both",
  })}`;
}

function bytesToB64Url(bytes) {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  const b64 = typeof btoa === "function" ? btoa(bin) : Buffer.from(bytes).toString("base64");
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

export function encodeLegs(legs) {
  const compact = legs.map((leg) => ({
    route_id: leg.route_id,
    direction: leg.direction || "",
    board: leg.board,
    alight: leg.alight,
    dep: String(leg.dep || "").slice(0, 8),
    arr: String(leg.arr || "").slice(0, 8),
  }));
  const json = JSON.stringify(compact);
  return bytesToB64Url(new TextEncoder().encode(json));
}

export function journeyHrefTransfer(spec) {
  return `/journey?${qs({
    from_stop: spec.fromStop,
    to_stop: spec.toStop,
    day_type: spec.dayType,
    legs: encodeLegs(spec.legs),
    operator: OPERATORS.has(spec.operator) ? spec.operator : "both",
  })}`;
}

function tabsFor(direct, transfers) {
  const source = direct.length ? direct : transfers;
  if (!source.length) return {best: [], fastest: [], all_day: []};
  return {
    best: source.slice(0, 8),
    fastest: source.slice().sort((a, b) => a.duration_min - b.duration_min).slice(0, 8),
    all_day: source,
  };
}

export function searchJourneys(catalog, query) {
  const meta = catalog.meta;
  const extra = meta.extra_holidays || [];
  const nowDate = parseInstant(query.now);
  const nowParts = capeTownParts(nowDate);
  let searchDate = query.search_date || "";
  let searchIso = null;
  if (searchDate) {
    if (/^\d{4}-\d{2}-\d{2}$/.test(searchDate)) searchIso = searchDate;
    else searchDate = "";
  }
  let dayType = query.day_type || null;
  if (searchIso && !dayType) dayType = dayTypeFor(searchIso, extra);
  if (!dayType) dayType = dayTypeFor(nowParts.iso, extra);
  if (!DAY_TYPES.has(dayType)) dayType = WEEKDAY;
  let operator = query.operator || "both";
  if (!OPERATORS.has(operator)) operator = "both";
  const window = parseWindow(query.when, query.at);
  const fromStop = (query.from_stop || "").trim();
  const toStop = (query.to_stop || "").trim();

  const expired = operator === "uct" || operator === "both" ? expiredUct(meta, nowParts.iso) : {};
  const uctWarning = Object.keys(expired).length ? expiryWarning(expired) : null;
  const railOn = operator === "metrorail" || (operator === "both" && servesRail(meta, [fromStop, toStop]));
  const railNotice = railOn ? metrorailNotice(meta, dayType, nowParts.iso) : null;

  if (!fromStop) {
    return {empty_form: true, day_type: dayType, operator, when: window.mode, at: window.atHhmm, search_date: searchDate, uct_warning: uctWarning, rail_notice: railNotice};
  }
  if (!toStop || toStop === fromStop) {
    return {
      redirect: `/stop/${encodeURIComponent(fromStop)}?day_type=${encodeURIComponent(dayType)}&operator=${encodeURIComponent(operator)}`,
      day_type: dayType,
      operator,
    };
  }

  const todayIso = searchIso || nowParts.iso;
  let live = dayType === dayTypeFor(nowParts.iso, extra) && (!searchIso || searchIso === nowParts.iso);
  let current = live ? nowParts.hms : "00:00:00";
  const onToday = live;
  const arriveFloor = current;
  if (window.mode !== "now") {
    live = false;
    current = window.mode === "depart" ? window.at : "00:00:00";
  }

  const todayIndex = catalog.days[dayType];
  let direct = dropExpiredDirect(findConnections(todayIndex, fromStop, toStop, operator), expired);
  if (live) {
    const yIso = addDays(nowParts.iso, -1);
    const yType = dayTypeFor(yIso, extra);
    const carry = nowParts.hour < OVERNIGHT_UNTIL_HOUR ? yType : null;
    direct = withOvernight(direct, catalog.days[yType], fromStop, toStop, operator, expired, nowParts, carry);
  } else {
    direct = direct.map((row) => ({...row, service_day_type: null, overnight: false}));
  }

  const tomorrowIso = addDays(nowParts.iso, 1);
  const tomorrowType = dayTypeFor(tomorrowIso, extra);
  let resultsDay = dayType;
  let showingNext = false;
  let arriveMissed = null;

  if (window.mode === "arrive") {
    if (onToday) arriveMissed = lastDepartureBy(direct, window.at);
    direct = arrivingBy(direct, window.at, arriveFloor);
  } else if (direct.length) {
    const upcoming = direct.filter((row) => row.dep >= current);
    if (upcoming.length) direct = inClockOrder(upcoming);
    else {
      if (tomorrowType !== dayType) {
        direct = dropExpiredDirect(findConnections(catalog.days[tomorrowType], fromStop, toStop, operator), expired)
          .map((row) => ({...row, service_day_type: null, overnight: false}));
      }
      direct = direct.slice(0, 10);
      if (direct.length) {
        showingNext = true;
        resultsDay = tomorrowType;
      }
    }
  }

  let transfers = [];
  if (!direct.length) {
    transfers = dropExpiredTransfers(findTransferConnections(todayIndex, fromStop, toStop, operator), expired);
    if (window.mode === "arrive") {
      if (onToday && arriveMissed == null) arriveMissed = lastDepartureBy(transfers, window.at);
      transfers = arrivingBy(transfers, window.at, arriveFloor);
    } else if (transfers.length) {
      const upcoming = transfers.filter((row) => row.dep >= current);
      if (upcoming.length) {
        transfers = upcoming.slice().sort((a, b) => cmp(a.dep, b.dep));
      } else {
        if (tomorrowType !== dayType) {
          transfers = dropExpiredTransfers(
            findTransferConnections(catalog.days[tomorrowType], fromStop, toStop, operator),
            expired,
          );
        }
        transfers = transfers.slice(0, 10);
        if (transfers.length) {
          showingNext = true;
          resultsDay = tomorrowType;
        }
      }
    }
  }

  const directCards = projectDirect(direct, catalog, fromStop, toStop, operator, resultsDay, nowDate);
  const transferCards = directCards.length ? [] : projectTransfers(transfers, catalog, fromStop, toStop, operator, resultsDay, nowDate);
  const has = directCards.length > 0 || transferCards.length > 0;
  return {
    empty_form: false,
    from_stop: fromStop,
    to_stop: toStop,
    day_type: dayType,
    results_day_type: resultsDay,
    operator,
    search_date: searchDate,
    when: window.mode,
    at: window.atHhmm,
    live,
    showing_next_day: showingNext,
    next_day_label: DAY_LABELS[resultsDay],
    next_day_holiday: showingNext ? holidayName(tomorrowIso, extra) : null,
    today_holiday: live ? holidayName(todayIso, extra) : null,
    current_time: searchDate ? "start of day" : current.slice(0, 5),
    arrive_missed: arriveMissed ? clockTime(arriveMissed) : null,
    has_results: has,
    direct: directCards,
    transfers: transferCards,
    tabs: tabsFor(directCards, transferCards),
    uct_warning: uctWarning,
    rail_notice: railNotice,
  };
}

function boardFrom(dayIndex, stopName, fromTime, operator, expired) {
  const lines = linesFor(dayIndex, operator).filter((line) => line.stops[stopName] && !expired[line.routeId]);
  lines.sort((a, b) => cmp(a.routeName, b.routeName) || cmp(a.direction, b.direction));
  const routes = lines.map((line) => ({
    route_id: line.routeId,
    route_name: line.routeName,
    direction: line.direction,
  }));
  const departures = {};
  for (const line of lines) {
    const upcoming = line.stops[stopName][1].filter((t) => t >= fromTime).slice(0, 10);
    if (!upcoming.length) continue;
    const key = `${line.routeId}_${line.direction}`;
    departures[key] = {
      route: {route_id: line.routeId, route_name: line.routeName, direction: line.direction},
      times: upcoming,
    };
  }
  return {routes, departures};
}

function mergeBoards(earlier, later) {
  const routes = earlier.routes.slice();
  const seen = new Set(routes.map((r) => `${r.route_id}\0${r.direction}`));
  for (const route of later.routes) {
    const key = `${route.route_id}\0${route.direction}`;
    if (!seen.has(key)) {
      seen.add(key);
      routes.push(route);
    }
  }
  const departures = {};
  const keys = Object.keys(earlier.departures).concat(
    Object.keys(later.departures).filter((key) => !earlier.departures[key]),
  );
  for (const key of keys) {
    const times = ((earlier.departures[key] || {}).times || []).concat((later.departures[key] || {}).times || []).slice(0, 10);
    const route = (earlier.departures[key] || later.departures[key]).route;
    departures[key] = {route, times};
  }
  return {routes, departures};
}

export function stopBoard(catalog, query) {
  const meta = catalog.meta;
  const extra = meta.extra_holidays || [];
  const nowDate = parseInstant(query.now);
  const nowParts = capeTownParts(nowDate);
  let dayType = query.day_type || dayTypeFor(nowParts.iso, extra);
  if (!DAY_TYPES.has(dayType)) dayType = WEEKDAY;
  let operator = query.operator || "both";
  if (!OPERATORS.has(operator)) operator = "both";
  const stopName = query.stop || query.stop_name || "";
  const expired = operator === "uct" || operator === "both" ? expiredUct(meta, nowParts.iso) : {};
  const live = dayType === dayTypeFor(nowParts.iso, extra);
  const fromTime = live ? nowParts.hms : "00:00:00";
  let board = boardFrom(catalog.days[dayType], stopName, fromTime, operator, expired);
  if (live && nowParts.hour < OVERNIGHT_UNTIL_HOUR) {
    const yType = dayTypeFor(addDays(nowParts.iso, -1), extra);
    const yFrom = `${pad(nowParts.hour + 24)}:${pad(nowParts.minute)}:${pad(nowParts.second)}`;
    const earlier = boardFrom(catalog.days[yType], stopName, yFrom, operator, expired);
    board = mergeBoards(earlier, board);
  }
  let boardDay = dayType;
  let nextDay = null;
  if (!board.routes.length) {
    return {
      stop_name: stopName,
      day_type: dayType,
      operator,
      live,
      routes: [],
      departures: {},
      error: `No routes found for stop '${stopName}' on ${dayType}s`,
      uct_warning: Object.keys(expired).length ? expiryWarning(expired) : null,
      rail_notice: (operator === "metrorail" || (operator === "both" && servesRail(meta, [stopName])))
        ? metrorailNotice(meta, dayType, nowParts.iso) : null,
      current_time: nowParts.hms.slice(0, 5),
      today_holiday: holidayName(nowParts.iso, extra),
    };
  }
  if (live && !Object.keys(board.departures).length) {
    const tomorrowIso = addDays(nowParts.iso, 1);
    const tomorrowType = dayTypeFor(tomorrowIso, extra);
    const tomorrow = boardFrom(catalog.days[tomorrowType], stopName, "00:00:00", operator, expired);
    if (Object.keys(tomorrow.departures).length) {
      board = tomorrow;
      boardDay = tomorrowType;
      nextDay = {
        day_type: tomorrowType,
        label: DAY_LABELS[tomorrowType],
        holiday: holidayName(tomorrowIso, extra),
      };
    }
  }
  const railOn = operator === "metrorail" || (operator === "both" && servesRail(meta, [stopName]));
  return {
    stop_name: stopName,
    day_type: dayType,
    board_day_type: boardDay,
    operator,
    live,
    routes: board.routes,
    departures: board.departures,
    error: null,
    next_day: nextDay,
    current_time: nowParts.hms.slice(0, 5),
    today_holiday: holidayName(nowParts.iso, extra),
    uct_warning: Object.keys(expired).length ? expiryWarning(expired) : null,
    rail_notice: railOn ? metrorailNotice(meta, boardDay, nowParts.iso) : null,
  };
}

const ROUTE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/;
const TIME_RE = /^\d{1,2}:\d{2}(:\d{2})?$/;

function cleanStop(name, field) {
  if (name == null || !String(name).trim()) throw new Error(`missing ${field}`);
  const value = String(name).trim();
  if (value.length > 120 || value.includes("\0") || value.includes("/") || value.includes("\\")) {
    throw new Error(`invalid ${field}`);
  }
  return value;
}

function cleanTime(value, field) {
  if (value == null || !String(value).trim()) throw new Error(`missing ${field}`);
  const raw = String(value).trim();
  if (!TIME_RE.test(raw)) throw new Error(`invalid ${field}`);
  return raw.length === 5 ? raw : raw.slice(0, 5);
}

export function decodeLegs(token) {
  if (!token || token.length > 4000) throw new Error("invalid legs");
  const padLen = (4 - (token.length % 4)) % 4;
  const b64 = token.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat(padLen);
  let raw;
  try {
    if (typeof atob === "function") {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
      raw = new TextDecoder().decode(bytes);
    } else {
      raw = Buffer.from(b64, "base64").toString("utf8");
    }
  } catch (err) {
    throw new Error("invalid legs");
  }
  let data;
  try {
    data = JSON.parse(raw);
  } catch (err) {
    throw new Error("invalid legs");
  }
  if (!Array.isArray(data) || !data.length || data.length > 4) throw new Error("invalid legs");
  return data.map((item) => {
    if (!item || typeof item !== "object") throw new Error("invalid legs");
    const routeId = String(item.route_id || "").trim();
    if (!ROUTE_RE.test(routeId)) throw new Error("invalid route_id");
    const direction = String(item.direction || "").trim();
    if (!direction || direction.length > 120 || direction.includes("\0")) throw new Error("invalid direction");
    return {
      route_id: routeId,
      direction,
      board: cleanStop(item.board, "board"),
      alight: cleanStop(item.alight, "alight"),
      dep: cleanTime(item.dep, "dep"),
      arr: cleanTime(item.arr, "arr"),
    };
  });
}

function fmtJourneyDuration(dep, arr) {
  let d = toMinutes(arr) - toMinutes(dep);
  if (d < 0) d += 24 * 60;
  const h = Math.floor(d / 60);
  const m = d % 60;
  if (h) return m ? `${h}h ${m}m` : `${h}h`;
  return `${m} min`;
}

export function journeyView(catalog, query, nowDate) {
  const meta = catalog.meta;
  const operator = OPERATORS.has(query.operator) ? query.operator : "both";
  let fromStop;
  let toStop;
  let dayType;
  try {
    fromStop = cleanStop(query.from_stop, "from_stop");
    toStop = cleanStop(query.to_stop, "to_stop");
    dayType = String(query.day_type || "").trim().toLowerCase();
    if (!DAY_TYPES.has(dayType)) throw new Error("invalid day_type");
  } catch (err) {
    return {error: "That journey link is invalid or incomplete.", status: 400};
  }
  const back = `/search?${qs({from_stop: fromStop, to_stop: toStop, day_type: dayType, operator})}`;
  try {
    if (query.legs) {
      const legs = decodeLegs(query.legs);
      if (legs[0].board !== fromStop || legs[legs.length - 1].alight !== toStop) throw new Error("legs mismatch");
      const legStops = [];
      const legColors = [];
      const routeIds = [];
      for (const leg of legs) {
        const stops = legSequence(catalog.days[dayType], leg.route_id, leg.direction, leg.board, leg.alight);
        if (stops.length < 2) {
          const miss = new Error("unknown journey");
          miss.status = 404;
          throw miss;
        }
        if (stops.length > 80) throw new Error("invalid journey");
        legStops.push(stops);
        const color = (meta.routes[leg.route_id] || {}).color || "#666";
        legColors.push(color);
        routeIds.push(leg.route_id);
      }
      const first = legs[0];
      return {
        error: null,
        kind: "transfer",
        from_stop: fromStop,
        to_stop: toStop,
        day_type: dayType,
        operator,
        dep: legs[0].dep,
        arr: legs[legs.length - 1].arr,
        duration: fmtJourneyDuration(legs[0].dep, legs[legs.length - 1].arr),
        route_ids: routeIds,
        legs,
        leg_stops: legStops,
        leg_colors: legColors,
        via: legs.slice(1).map((leg) => leg.board),
        preview_ok: previewOk(catalog, {
          route_id: first.route_id,
          direction: first.direction,
          day_type: dayType,
          board: first.board,
          alight: first.alight,
          dep: first.dep,
          arr: first.arr,
        }, nowDate || new Date()),
        preview_route_id: first.route_id,
        preview_direction: first.direction,
        preview_board: first.board,
        preview_alight: first.alight,
        preview_dep: first.dep,
        back_href: back,
      };
    }
    const routeId = String(query.route_id || "").trim();
    if (!ROUTE_RE.test(routeId)) throw new Error("invalid route_id");
    const direction = String(query.direction || "").trim();
    if (!direction || direction.length > 120 || direction.includes("\0")) throw new Error("invalid direction");
    const dep = cleanTime(query.dep, "dep");
    const arr = cleanTime(query.arr, "arr");
    const stops = legSequence(catalog.days[dayType], routeId, direction, fromStop, toStop);
    const line = catalog.days[dayType] && catalog.days[dayType].byKey.get(`${routeId}\0${direction}`);
    if (!line || !line.stops[fromStop] || stops.length < 2) {
      const miss = new Error("unknown journey");
      miss.status = 404;
      throw miss;
    }
    if (stops.length > 80) throw new Error("invalid journey");
    const route = meta.routes[routeId] || {};
    return {
      error: null,
      kind: "direct",
      from_stop: fromStop,
      to_stop: toStop,
      day_type: dayType,
      operator,
      route_id: routeId,
      route_name: route.name || routeId,
      direction,
      dep,
      arr,
      duration: fmtJourneyDuration(dep, arr),
      stops,
      route_color: route.color || "#666",
      route_ids: [routeId],
      preview_ok: previewOk(catalog, {
        route_id: routeId, direction, day_type: dayType, board: fromStop, alight: toStop, dep, arr,
      }, nowDate || new Date()),
      preview_route_id: routeId,
      preview_direction: direction,
      preview_board: fromStop,
      preview_alight: toStop,
      preview_dep: dep,
      back_href: back,
    };
  } catch (err) {
    const status = err.status || (String(err.message).includes("unknown") ? 404 : 400);
    return {
      error: status === 404 ? "We could not rebuild that journey from the timetable." : "That journey link is invalid or incomplete.",
      status,
    };
  }
}

export function legSequenceOf(catalog, dayType, routeId, direction, board, alight) {
  return legSequence(catalog.days[dayType], routeId, direction, board, alight);
}

export {lineKey};
