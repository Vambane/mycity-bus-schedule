/**
 * Static Halte pages: search, stop boards, journey schematics, map frame.
 * Timetable decisions go through planner.mjs (Africa/Johannesburg).
 */
import {
  addDays,
  badgeLabel,
  capeTownParts,
  clockTime,
  createCatalog,
  dayTypeFor,
  journeyView,
  searchJourneys,
  stopBoard,
  uctNotes,
} from "./planner.mjs";

const HISTORY_KEY = "myciti_search_history";
const MAX_HISTORY = 5;
const body = document.body;
const DATA = body.dataset.data;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[ch]));
}

function badgeClass(routeId) {
  const id = String(routeId || "");
  if (id.startsWith("UCT")) return "uct";
  if (id.startsWith("MR-")) return "rail";
  if (id.startsWith("T")) return "trunk";
  if (id.startsWith("D")) return "direct";
  return "area";
}

function qs(params) {
  const u = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value != null && value !== "") u.set(key, value);
  });
  return u.toString();
}

async function getJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} (${res.status})`);
  return res.json();
}

function extraOf(meta) {
  return meta.extra_holidays || [];
}

function neededDays(meta, requested) {
  const extra = extraOf(meta);
  const now = capeTownParts(new Date());
  const days = new Set([requested]);
  if (!requested) days.add(dayTypeFor(now.iso, extra));
  if (now.hour < 6) days.add(dayTypeFor(addDays(now.iso, -1), extra));
  days.add(dayTypeFor(addDays(now.iso, 1), extra));
  days.delete(undefined);
  return [...days];
}

async function loadCatalog(dayTypes) {
  const meta = await getJson(`${DATA}/meta.json`);
  const payloads = {};
  await Promise.all(dayTypes.map(async (day) => {
    payloads[day] = await getJson(`${DATA}/timetable/${day}.json`);
  }));
  return createCatalog(meta, payloads);
}

function alertBox(kind, title, text, href) {
  const link = href ? ` <a href="${esc(href)}" target="_blank" rel="noopener">PRASA train schedules</a>` : "";
  return `<div class="alert alert-${kind}" role="status"><div class="alert-title">${esc(title)}</div><p>${esc(text)}${link}</p></div>`;
}

function renderNotices(target, result) {
  if (!target) return;
  const bits = [];
  if (result.uct_warning) bits.push(alertBox("warning", "Timetable expired", result.uct_warning));
  if (result.rail_notice) {
    bits.push(alertBox("info", result.rail_notice.title, result.rail_notice.body, result.rail_notice.url));
  }
  target.innerHTML = bits.join("");
}

function cardTimes(dep, arr) {
  return `<div class="journey-endpoint"><div class="journey-time">${esc(clockTime(dep))}</div></div>
          <div class="journey-endpoint" style="text-align: right;"><div class="journey-time">${esc(clockTime(arr))}</div></div>`;
}

function directCard(card, result) {
  const notes = uctNotes(card.route_id, card.service_day_type).map((note) =>
    `<p class="journey-via journey-card-front route-note">${esc(note)}</p>`).join("");
  const preview = card.preview_ok ? `<a class="btn btn-secondary btn-trip-preview journey-card-control" href="/map?${esc(qs({
    preview: "1", route_id: card.route_id, direction: card.direction, day_type: card.service_day_type,
    board: result.from_stop, alight: result.to_stop, dep: card.dep_hhmm, arr: card.arr_hhmm,
  }))}">Preview this trip</a>` : "";
  const back = `/search?${qs({
    from_stop: result.to_stop, to_stop: result.from_stop, day_type: result.results_day_type, operator: result.operator,
  })}`;
  return `<div class="card card--interactive journey-card stagger-enter">
    <a class="journey-card-hit" href="${esc(card.journey_href)}" aria-label="View all stops for ${esc(result.from_stop)} to ${esc(result.to_stop)}"></a>
    <div class="card-header journey-card-front">
      <span class="route-badge ${badgeClass(card.route_id)}">${esc(card.route_name)}</span>
      <span class="journey-direction">${esc(card.direction)}</span>
      <a href="${esc(back)}" class="btn-reverse journey-card-control" title="Search return trip" aria-label="Search return trip">&#8693;</a>
    </div>
    ${notes}
    <div class="journey-viz journey-card-front">
      <div class="journey-endpoint">
        <div class="journey-time">${esc(clockTime(card.dep_hhmm))}</div>
        <div class="journey-stop-name" title="${esc(result.from_stop)}">${esc(result.from_stop)}</div>
      </div>
      <div class="journey-dot journey-dot--origin"></div>
      <div class="journey-line"></div>
      <div class="journey-duration">Direct &middot; ${esc(card.duration)}</div>
      <div class="journey-line"></div>
      <div class="journey-dot journey-dot--destination"></div>
      <div class="journey-endpoint" style="text-align: right;">
        <div class="journey-time">${esc(clockTime(card.arr_hhmm))}</div>
        <div class="journey-stop-name" title="${esc(result.to_stop)}">${esc(result.to_stop)}</div>
      </div>
    </div>
    <div class="journey-card-footer journey-card-front">
      <span class="journey-card-hint">View stops</span>
      ${preview}
    </div>
  </div>`;
}

function transferCard(card, result) {
  const chain = card.route_ids.map((id, i) => {
    const arrow = i < card.route_ids.length - 1 ? `<span class="route-chain-arrow" aria-hidden="true">&#8594;</span>` : "";
    return `<span class="route-badge ${badgeClass(id)}">${esc(badgeLabel(id))}</span>${arrow}`;
  }).join("");
  const notes = uctNotes(card.route_ids, result.results_day_type).map((note) =>
    `<p class="journey-via journey-card-front route-note">${esc(note)}</p>`).join("");
  const via = card.via && card.via.length ? `<div class="journey-via journey-card-front">Via ${esc(card.via.join(", "))}</div>` : "";
  const dots = (card.via || []).map(() => `<div class="journey-line journey-line-short"></div><div class="journey-dot journey-dot--transfer"></div>`).join("");
  const preview = card.preview_ok ? `<a class="btn btn-secondary btn-trip-preview journey-card-control" href="/map?${esc(qs({
    preview: "1", route_id: card.preview_route_id, direction: card.preview_direction, day_type: result.results_day_type,
    board: card.preview_board, alight: card.preview_alight, dep: card.preview_dep,
  }))}">Preview this trip</a>` : "";
  const back = `/search?${qs({
    from_stop: result.to_stop, to_stop: result.from_stop, day_type: result.results_day_type, operator: result.operator,
  })}`;
  const word = card.num_transfers === 1 ? "transfer" : "transfers";
  return `<div class="card card--interactive journey-card stagger-enter">
    <a class="journey-card-hit" href="${esc(card.journey_href)}" aria-label="View all stops for ${esc(result.from_stop)} to ${esc(result.to_stop)}"></a>
    <div class="card-header journey-card-front">
      <div class="route-chain">${chain}</div>
      <span class="badge badge-transfer">${card.num_transfers} ${word}</span>
      <a href="${esc(back)}" class="btn-reverse journey-card-control" title="Search return trip" aria-label="Search return trip">&#8693;</a>
    </div>
    ${notes}${via}
    <div class="journey-viz journey-card-front">
      <div class="journey-endpoint">
        <div class="journey-time">${esc(clockTime(card.dep_hhmm))}</div>
        <div class="journey-stop-name">${esc(result.from_stop)}</div>
      </div>
      <div class="journey-dot journey-dot--origin"></div>
      ${dots}
      <div class="journey-line"></div>
      <div class="journey-duration">${esc(card.duration)}</div>
      <div class="journey-line"></div>
      <div class="journey-dot journey-dot--destination"></div>
      <div class="journey-endpoint" style="text-align: right;">
        <div class="journey-time">${esc(clockTime(card.arr_hhmm))}</div>
        <div class="journey-stop-name">${esc(result.to_stop)}</div>
      </div>
    </div>
    <div class="journey-card-footer journey-card-front">
      <span class="journey-card-hint">View stops</span>
      ${preview}
    </div>
  </div>`;
}

function renderCard(card, result) {
  return card.route_ids ? transferCard(card, result) : directCard(card, result);
}

function resultsMeta(result) {
  if (result.when === "arrive") {
    return `Arriving by ${result.at}${result.search_date ? ` on ${result.search_date}` : ""}, latest departure first`;
  }
  if (result.when === "depart") {
    return `Departing from ${result.at}${result.search_date ? ` on ${result.search_date}` : ""}`;
  }
  if (result.search_date) return `Departures for ${result.search_date}`;
  if (result.showing_next_day) return "Tomorrow's first departures";
  if (!result.live) return "All departures";
  return `Departures from ${result.current_time}`;
}

function bindTabs(root) {
  const bar = root.querySelector(".tab-bar");
  if (!bar) return;
  const buttons = [...bar.querySelectorAll(".tab-btn")];
  const indicator = root.querySelector("#tab-indicator");
  function activate(btn) {
    buttons.forEach((item) => {
      const on = item === btn;
      item.classList.toggle("active", on);
      item.setAttribute("aria-selected", on ? "true" : "false");
    });
    root.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.remove("active"));
    const target = root.querySelector(`#panel-${btn.dataset.tab}`);
    if (target) target.classList.add("active");
    if (indicator) {
      indicator.style.width = `${btn.offsetWidth}px`;
      indicator.style.left = `${btn.offsetLeft}px`;
    }
    relativeTimes();
  }
  buttons.forEach((btn) => btn.addEventListener("click", () => activate(btn)));
  const active = bar.querySelector(".tab-btn.active");
  if (active) activate(active);
}

function renderResults(result) {
  const root = document.getElementById("results");
  renderNotices(document.getElementById("notices"), result);
  if (!result.has_results) {
    const arrive = result.when === "arrive";
    const missed = result.arrive_missed
      ? `The last bus that arrives at ${result.to_stop} by ${result.at} left at ${result.arrive_missed}.`
      : `No ${result.results_day_type} trip from ${result.from_stop} reaches ${result.to_stop} by ${result.at}.`;
    root.innerHTML = `<div class="empty-state">
      <div class="empty-state-icon" aria-hidden="true">&#128652;</div>
      <div class="empty-state-title">${arrive ? `Nothing gets you there by ${esc(result.at)}` : "No routes found"}</div>
      <p class="empty-state-text">${arrive ? esc(`${missed} Try a later time.`) : esc(`There are no direct or connecting routes between ${result.from_stop} and ${result.to_stop} on ${result.results_day_type}s.`)}</p>
    </div>`;
    return;
  }
  const holiday = result.today_holiday ? alertBox("info", result.today_holiday, "It's a public holiday, so Sunday timetables apply today.") : "";
  let next = "";
  if (result.showing_next_day) {
    const which = result.next_day_holiday
      ? `${result.next_day_holiday}, so the Sunday timetable`
      : `${result.next_day_label} timetable`;
    next = alertBox("info", "No more buses today", `The last bus has left. Showing tomorrow morning's departures (${which}).`);
  }
  const panels = ["best", "fastest", "all"].map((name) => {
    const key = name === "all" ? "all_day" : name;
    const cards = result.tabs[key] || [];
    const body = cards.length ? cards.map((card) => renderCard(card, result)).join("") : `<div class="empty-state"><div class="empty-state-title">No connections available</div></div>`;
    const active = name === "best" ? " active" : "";
    const selected = name === "best" ? "true" : "false";
    return {name, body, active, selected};
  });
  root.innerHTML = `
    ${holiday}${next}
    <div class="results-header">
      <div class="results-route">
        <h2 style="margin-bottom: 0;">${esc(result.from_stop)}</h2>
        <span class="results-arrow" aria-hidden="true">&#8594;</span>
        <h2 style="margin-bottom: 0;">${esc(result.to_stop)}</h2>
      </div>
      <p class="results-meta">${esc(resultsMeta(result))} &middot; ${esc(result.results_day_type[0].toUpperCase() + result.results_day_type.slice(1))} service</p>
    </div>
    <div class="tab-bar" role="tablist" aria-label="Result views">
      <span class="tab-indicator" id="tab-indicator"></span>
      <button class="tab-btn active" role="tab" id="btn-best" aria-controls="panel-best" aria-selected="true" data-tab="best">Best</button>
      <button class="tab-btn" role="tab" id="btn-fastest" aria-controls="panel-fastest" aria-selected="false" data-tab="fastest">Fastest</button>
      <button class="tab-btn" role="tab" id="btn-all" aria-controls="panel-all" aria-selected="false" data-tab="all">All Day</button>
    </div>
    ${panels.map((panel) => `<div class="tab-panel${panel.active}" id="panel-${panel.name}" role="tabpanel">${panel.body}</div>`).join("")}
  `;
  bindTabs(root);
  relativeTimes();
}

function relativeTimes() {
  const parts = capeTownParts(new Date());
  const nowMinutes = parts.hour * 60 + parts.minute;
  document.querySelectorAll(".journey-endpoint .journey-time, .departure-time, .metric-value").forEach((el) => {
    if (el.querySelector(".time-relative")) return;
    const endpoint = el.closest(".journey-endpoint");
    if (endpoint && endpoint.style.textAlign === "right") return;
    const match = el.textContent.trim().match(/^(\d{1,2}):(\d{2})/);
    if (!match) return;
    let diff = Number(match[1]) * 60 + Number(match[2]) - nowMinutes;
    if (diff < -12 * 60) diff += 24 * 60;
    if (diff < 0 || diff > 60) return;
    const span = document.createElement("span");
    span.className = "time-relative";
    if (diff <= 5) span.classList.add("time-relative--soon");
    span.textContent = diff === 0 ? "now" : `in ${diff} min`;
    el.appendChild(span);
  });
}

function fillCombo(root, names, value) {
  const list = root.querySelector(".combobox-list");
  const native = root.querySelector(".combobox-native");
  const input = root.querySelector(".combobox-input");
  list.replaceChildren();
  native.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = root.dataset.combobox === "to_stop" ? "Departure board" : "Select origin...";
  native.appendChild(placeholder);
  names.forEach((name) => {
    const li = document.createElement("li");
    li.className = "combobox-option";
    li.setAttribute("role", "option");
    li.dataset.value = name;
    li.textContent = name;
    if (name === value) li.setAttribute("aria-selected", "true");
    list.appendChild(li);
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    if (name === value) opt.selected = true;
    native.appendChild(opt);
  });
  const keep = names.includes(value);
  native.value = keep ? value : "";
  input.value = keep ? value : "";
}

function checked(name) {
  const el = document.querySelector(`[name="${name}"]:checked`);
  return el ? el.value : "";
}

function setRadio(name, value) {
  const el = document.querySelector(`[name="${name}"][value="${value}"]`);
  if (el) el.checked = true;
}

function readForm() {
  const form = document.getElementById("search-form");
  return {
    from_stop: form.querySelector('[name="from_stop"]').value,
    to_stop: form.querySelector('[name="to_stop"]').value,
    day_type: checked("day_type"),
    operator: checked("operator") || "both",
    when: checked("when") || "now",
    at: form.querySelector("#at").value,
    search_date: form.querySelector("#search_date").value,
  };
}

function applyForm(params, meta) {
  const extra = extraOf(meta);
  const now = capeTownParts(new Date());
  const day = params.get("day_type") || dayTypeFor(params.get("search_date") || now.iso, extra);
  setRadio("day_type", ["weekday", "saturday", "sunday"].includes(day) ? day : "weekday");
  const op = params.get("operator") || "both";
  setRadio("operator", ["both", "myciti", "uct", "metrorail"].includes(op) ? op : "both");
  const when = params.get("when") || "now";
  setRadio("when", ["now", "depart", "arrive"].includes(when) ? when : "now");
  const at = document.getElementById("at");
  if (at && params.get("at")) at.value = params.get("at");
  const date = document.getElementById("search_date");
  if (date && params.get("search_date")) date.value = params.get("search_date");
}

function loadHistory() {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
  } catch (err) {
    return [];
  }
}

function renderHistory() {
  const container = document.getElementById("recent-searches");
  if (!container) return;
  const history = loadHistory();
  if (!history.length) {
    container.style.display = "none";
    return;
  }
  container.style.display = "";
  container.replaceChildren();
  const label = document.createElement("span");
  label.className = "recent-label";
  label.textContent = "Recent";
  container.appendChild(label);
  history.forEach((item, index) => {
    const chip = document.createElement("a");
    chip.className = "recent-chip";
    chip.href = `/search?${qs({from_stop: item.from_stop, to_stop: item.to_stop, day_type: item.day_type || "weekday"})}`;
    const text = document.createElement("span");
    text.textContent = `${item.from_stop} → ${item.to_stop}`;
    chip.appendChild(text);
    const close = document.createElement("button");
    close.type = "button";
    close.className = "recent-chip-close";
    close.setAttribute("aria-label", "Remove from history");
    close.textContent = "×";
    close.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const next = loadHistory();
      next.splice(index, 1);
      localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
      renderHistory();
    });
    chip.appendChild(close);
    container.appendChild(chip);
  });
}

function pushHistory(from, to, dayType) {
  if (!from || !to) return;
  const history = loadHistory().filter((item) => !(item.from_stop === from && item.to_stop === to));
  history.unshift({from_stop: from, to_stop: to, day_type: dayType});
  localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(0, MAX_HISTORY)));
}

function wireLocate(stopsPayload) {
  const btn = document.getElementById("locate-btn");
  if (!btn || !navigator.geolocation) return;
  btn.hidden = false;
  const status = document.getElementById("locate-status");
  const privacy = document.getElementById("locate-privacy");
  btn.addEventListener("click", () => {
    if (privacy) privacy.hidden = false;
    btn.disabled = true;
    status.textContent = "Finding your location…";
    navigator.geolocation.getCurrentPosition((pos) => {
      const op = checked("operator") || "both";
      const list = (stopsPayload.locations || {})[op] || [];
      if (!list.length) {
        status.textContent = op === "uct"
          ? "UCT stops aren't on the map yet, so we can't find the nearest one. Type your stop instead."
          : "We couldn't match any stop to a location. Type your stop instead.";
        btn.disabled = false;
        return;
      }
      const here = list.map((stop) => ({...stop, m: distance(pos.coords.latitude, pos.coords.longitude, stop.lat, stop.lon)}))
        .sort((a, b) => a.m - b.m)
        .slice(0, 3);
      const root = document.querySelector('[data-combobox="from_stop"]');
      const names = (stopsPayload.names || {})[op] || [];
      fillCombo(root, names, here[0].name);
      status.textContent = `Nearest stop: ${here[0].name}, ${Math.round(here[0].m)} m. Location stayed on this device.`;
      btn.disabled = false;
    }, () => {
      status.textContent = "Couldn't get your location. Type your stop instead.";
      btn.disabled = false;
    }, {enableHighAccuracy: false, timeout: 10000, maximumAge: 60000});
  });
}

function distance(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad;
  const dLon = (lon2 - lon1) * rad;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.sqrt(a));
}

function wireSearch(stopsPayload, meta) {
  const params = new URLSearchParams(location.search);
  applyForm(params, meta);
  const op = checked("operator") || "both";
  const names = (stopsPayload.names || {})[op] || [];
  document.querySelectorAll("[data-combobox]").forEach((root) => {
    const field = root.dataset.combobox;
    fillCombo(root, names, params.get(field) || "");
    if (window.initCombobox) window.initCombobox(root);
  });
  document.querySelectorAll('input[name="operator"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      const next = (stopsPayload.names || {})[radio.value] || [];
      document.querySelectorAll("[data-combobox]").forEach((root) => {
        const native = root.querySelector(".combobox-native");
        fillCombo(root, next, native.value);
      });
    });
  });
  const form = document.getElementById("search-form");
  form.addEventListener("submit", () => {
    const data = readForm();
    pushHistory(data.from_stop, data.to_stop, data.day_type);
  });
  const swap = document.getElementById("swap-btn");
  if (swap) {
    const from = params.get("from_stop");
    const to = params.get("to_stop");
    if (from && to) swap.hidden = false;
    swap.addEventListener("click", () => {
      const data = readForm();
      const fromRoot = document.querySelector('[data-combobox="from_stop"]');
      const toRoot = document.querySelector('[data-combobox="to_stop"]');
      const list = (stopsPayload.names || {})[data.operator] || [];
      fillCombo(fromRoot, list, data.to_stop);
      fillCombo(toRoot, list, data.from_stop);
      form.requestSubmit();
    });
  }
  const time = document.getElementById("at");
  const nowRadio = document.getElementById("when-now");
  const departRadio = document.getElementById("when-depart");
  if (time && nowRadio && departRadio) {
    time.addEventListener("input", () => {
      if (time.value && nowRadio.checked) departRadio.checked = true;
    });
    document.querySelectorAll('[name="when"]').forEach((radio) => {
      radio.addEventListener("change", () => {
        if (radio.value === "now" || time.value) return;
        const parts = capeTownParts(new Date());
        const mins = Math.ceil((parts.hour * 60 + parts.minute + 1) / 15) * 15 % (24 * 60);
        time.value = `${String(Math.floor(mins / 60)).padStart(2, "0")}:${String(mins % 60).padStart(2, "0")}`;
        time.focus();
      });
    });
  }
  if (!params.get("from_stop")) renderHistory();
  wireLocate(stopsPayload);
}

async function runSearch(meta) {
  const params = new URLSearchParams(location.search);
  if (!params.get("from_stop")) {
    const extra = extraOf(meta);
    const now = capeTownParts(new Date());
    const day = params.get("day_type") || dayTypeFor(now.iso, extra);
    const op = params.get("operator") || "both";
    if (op === "metrorail" || op === "uct" || op === "both") {
      const catalog = await loadCatalog(neededDays(meta, day));
      const preview = searchJourneys(catalog, {
        from_stop: "", day_type: day, operator: op, now: new Date().toISOString(),
      });
      renderNotices(document.getElementById("notices"), preview);
    }
    return;
  }
  const root = document.getElementById("results");
  root.textContent = "Loading timetable…";
  const extra = extraOf(meta);
  const now = capeTownParts(new Date());
  const day = params.get("day_type") || dayTypeFor(params.get("search_date") || now.iso, extra);
  const catalog = await loadCatalog(neededDays(meta, day));
  const query = Object.fromEntries(params.entries());
  query.now = new Date().toISOString();
  const result = searchJourneys(catalog, query);
  if (result.redirect) {
    location.replace(result.redirect);
    return;
  }
  renderResults(result);
}

function stopName() {
  const params = new URLSearchParams(location.search);
  if (params.get("stop")) return params.get("stop");
  const path = decodeURIComponent(location.pathname);
  const match = path.match(/\/stop\/(.+?)\/?$/);
  if (match && match[1] !== "index.html") return match[1];
  return "";
}

function renderStop(board) {
  const root = document.getElementById("stop-root");
  const name = board.stop_name;
  const op = board.operator;
  const dayLink = (day, label) => {
    const current = board.day_type === day ? " active" : "";
    const aria = board.day_type === day ? " aria-current=\"page\"" : "";
    return `<a class="tab${current}" href="/stop/${encodeURIComponent(name)}?${qs({day_type: day, operator: op})}"${aria}>${label}</a>`;
  };
  const opLink = (value, label) => {
    const current = op === value ? " active" : "";
    return `<a class="tab${current}" href="/stop/${encodeURIComponent(name)}?${qs({day_type: board.day_type, operator: value})}">${label}</a>`;
  };
  let subtitle = `Upcoming departures from ${board.current_time}`;
  if (board.next_day) subtitle = "Tomorrow's first departures";
  else if (!board.live) subtitle = `All ${board.day_type} departures`;
  const holiday = board.today_holiday && board.live
    ? alertBox("info", board.today_holiday, "It's a public holiday, so Sunday timetables apply today.") : "";
  let next = "";
  if (board.next_day) {
    const which = board.next_day.holiday ? `${board.next_day.holiday}, so the Sunday timetable` : `${board.next_day.label} timetable`;
    next = alertBox("info", "No more buses today", `The last bus has left. Showing tomorrow's first departures (${which}).`);
  }
  const seen = new Set();
  const badges = board.routes.filter((route) => {
    if (seen.has(route.route_id)) return false;
    seen.add(route.route_id);
    return true;
  }).map((route) => `<button type="button" class="route-badge route-badge--filterable ${badgeClass(route.route_id)}" data-route-id="${esc(route.route_id)}">${esc(route.route_name)}</button>`).join("");
  const cards = Object.entries(board.departures).map(([, data]) => {
    const notes = uctNotes(data.route.route_id, board.board_day_type || board.day_type)
      .map((note) => `<p class="metric-subtitle route-note">${esc(note)}</p>`).join("");
    const items = data.times.map((time) => `<li class="departure-item"><div class="departure-info"><span class="departure-time time">${esc(clockTime(time.slice(0, 5)))}</span><span class="departure-direction">${esc(data.route.direction)}</span></div></li>`).join("");
    return `<div class="card mb-2 departure-card" data-route-id="${esc(data.route.route_id)}">
      <div class="card-header"><span class="route-badge ${badgeClass(data.route.route_id)}">${esc(data.route.route_name)}</span> <span class="journey-direction">${esc(data.route.direction)}</span></div>
      ${notes}
      <div class="metric-card"><div class="metric-label">Next Departure</div><div class="metric-value">${esc(clockTime(data.times[0].slice(0, 5)))}</div><div class="metric-subtitle">${esc(data.route.direction)}</div></div>
      <div class="card-body"><h4 style="margin-bottom: 1rem;">Upcoming Departures</h4><ul class="departure-list">${items}</ul></div>
    </div>`;
  }).join("");
  const empty = board.error
    ? `<div class="alert alert-danger">${esc(board.error)}</div>`
    : (cards || `<div class="empty-state"><div class="empty-state-title">No upcoming departures</div></div>`);
  root.innerHTML = `
    <section class="hero"><h1 class="hero-title">${esc(name || "Stop")}</h1><p class="hero-subtitle">${esc(subtitle)}</p></section>
    <div id="stop-notices"></div>
    ${holiday}${next}
    <div class="form-group"><span class="form-label">Service type</span><div class="tabs" role="group" aria-label="Service type">${dayLink("weekday", "Weekday")}${dayLink("saturday", "Saturday")}${dayLink("sunday", "Sunday")}</div></div>
    <div class="form-group"><span class="form-label">Network</span><div class="tabs" role="group" aria-label="Network">${opLink("both", "All")}${opLink("myciti", "MyCiTi")}${opLink("uct", "UCT")}${opLink("metrorail", "Metrorail")}</div></div>
    ${badges ? `<div class="card mb-3"><div class="card-header"><h3 class="card-title">Routes Serving This Stop</h3><span class="route-filter-status" id="route-filter-status"></span></div><div class="card-body"><div class="route-filter-bar">${badges}</div></div></div>` : ""}
    ${empty}
  `;
  renderNotices(document.getElementById("stop-notices"), board);
  let active = null;
  root.querySelectorAll(".route-badge--filterable").forEach((badge) => {
    badge.addEventListener("click", () => {
      const id = badge.getAttribute("data-route-id");
      active = active === id ? null : id;
      root.querySelectorAll(".departure-card").forEach((card) => {
        card.hidden = Boolean(active) && card.getAttribute("data-route-id") !== active;
      });
      root.querySelectorAll(".route-badge--filterable").forEach((item) => {
        item.classList.toggle("inactive", Boolean(active) && item.getAttribute("data-route-id") !== active);
      });
      const status = document.getElementById("route-filter-status");
      if (status) status.textContent = active ? `Showing ${active}` : "";
    });
  });
  document.title = `${name} - Halte`;
  relativeTimes();
}

function renderJourney(view) {
  const root = document.getElementById("journey-root");
  if (view.error) {
    root.innerHTML = `<section class="hero"><p class="hero-subtitle"><a href="/search">← Back to search</a></p><h1 class="hero-title">Journey</h1></section><div class="alert alert-warning" role="alert">${esc(view.error)}</div>`;
    return;
  }
  const chain = view.route_ids.map((id, i) => {
    const label = view.kind === "direct" && i === 0 ? view.route_name : badgeLabel(id);
    const arrow = i < view.route_ids.length - 1 ? `<span class="route-chain-arrow" aria-hidden="true">&#8594;</span>` : "";
    return `<span class="route-badge ${badgeClass(id)}">${esc(label)}</span>${arrow}`;
  }).join("");
  let rows = "";
  if (view.kind === "direct") {
    rows = view.stops.map((stop, i) => {
      const first = i === 0;
      const last = i === view.stops.length - 1;
      const seg = first ? "stop-line-segment--first" : (last ? "stop-line-segment--last" : "");
      const dot = first ? "stop-dot--origin" : (last ? "stop-dot--destination" : "stop-dot--intermediate");
      const time = first ? clockTime(view.dep) : (last ? clockTime(view.arr) : "");
      return `<div class="journey-stop-row"><div class="stop-line-segment ${seg}" style="--route-color: ${esc(view.route_color)}"></div><div class="stop-dot ${dot} stop-dot--page" style="--route-color: ${esc(view.route_color)}"></div><span class="stop-name stop-name--page">${esc(stop)}</span>${time ? `<span class="stop-time">${esc(time)}</span>` : ""}</div>`;
    }).join("");
  } else {
    view.leg_stops.forEach((stops, legIndex) => {
      const color = view.leg_colors[legIndex] || "#666";
      stops.forEach((stop, i) => {
        const first = i === 0;
        const last = i === stops.length - 1;
        const outerFirst = legIndex === 0;
        const outerLast = legIndex === view.leg_stops.length - 1;
        const seg = first && outerFirst ? "stop-line-segment--first" : (last && outerLast ? "stop-line-segment--last" : "");
        let dot = "stop-dot--intermediate";
        if (first && !outerFirst) dot = "stop-dot--transfer";
        else if (first && outerFirst) dot = "stop-dot--origin";
        else if (last && outerLast) dot = "stop-dot--destination";
        let time = "";
        if (first && outerFirst) time = clockTime(view.dep);
        else if (last && outerLast) time = clockTime(view.arr);
        else if (last && !outerLast) time = "change";
        rows += `<div class="journey-stop-row"><div class="stop-line-segment ${seg}" style="--route-color: ${esc(color)}"></div><div class="stop-dot ${dot} stop-dot--page" style="--route-color: ${esc(color)}"></div><span class="stop-name stop-name--page">${esc(stop)}</span>${time ? `<span class="stop-time${time === "change" ? " stop-time--transfer" : ""}">${esc(time)}</span>` : ""}</div>`;
      });
    });
  }
  const preview = view.preview_ok ? `<a class="btn btn-secondary btn-trip-preview" href="/map?${esc(qs({
    preview: "1", route_id: view.preview_route_id, direction: view.preview_direction, day_type: view.day_type,
    board: view.preview_board, alight: view.preview_alight, dep: view.preview_dep,
  }))}">Preview this trip</a>` : "";
  const via = view.via && view.via.length ? `<p class="journey-via">Via ${esc(view.via.join(", "))}</p>` : "";
  root.innerHTML = `
    <section class="hero hero--journey">
      <p class="journey-back"><a href="${esc(view.back_href)}">← Search results</a></p>
      <h1 class="hero-title">${esc(view.from_stop)} → ${esc(view.to_stop)}</h1>
      <p class="hero-subtitle">${esc(view.day_type[0].toUpperCase() + view.day_type.slice(1))} · ${esc(clockTime(view.dep))}–${esc(clockTime(view.arr))} · ${esc(view.duration)}</p>
      <div class="route-chain" style="margin-top: var(--sp-3);">${chain}</div>
      ${view.kind === "direct" ? `<p class="journey-direction" style="margin-top: var(--sp-2);">${esc(view.direction)}</p>` : ""}
      ${via}
    </section>
    <div class="card journey-schematic-card"><div class="card-header"><h2 style="margin:0; font-size: var(--text-md);">All stops</h2></div>
      <div class="journey-stops-schematic journey-stops-schematic--page">${rows}</div>
    </div>
    <div class="journey-page-actions">${preview}<a class="btn btn-ghost" href="/map">Network map</a></div>
  `;
  document.title = `${view.from_stop} → ${view.to_stop} - Halte`;
}

async function refreshLoadShedding() {
  const note = document.getElementById("ls-note");
  const badge = document.getElementById("ls-badge");
  let cfg = {endpoint: null};
  try {
    cfg = await getJson("/data/ls.json");
  } catch (err) {
    cfg = {endpoint: null};
  }
  const unavailable = "Load shedding is unavailable in this static copy. Journey times still work. A site operator can attach the optional Worker to show the national stage. Per-stop outage flags need block data, which this snapshot does not include.";
  if (!cfg.endpoint) {
    if (note && body.dataset.page !== "stop" && body.dataset.page !== "journey") {
      note.innerHTML = alertBox("info", "Load shedding unavailable", unavailable);
    }
    return;
  }
  try {
    const res = await fetch(cfg.endpoint);
    const payload = await res.json();
    if (!payload.available || payload.stage == null) throw new Error("unavailable");
    const stage = Number(payload.stage);
    badge.classList.remove("ls-badge--ok", "ls-badge--warn", "ls-badge--crit");
    badge.classList.add(stage === 0 ? "ls-badge--ok" : (stage <= 2 ? "ls-badge--warn" : "ls-badge--crit"));
    const label = `Stage ${stage} (auto)`;
    badge.title = label;
    badge.setAttribute("aria-label", label);
    badge.querySelector(".ls-badge__text").innerHTML = `Stage ${stage} <span class="text-tertiary ls-badge__src ls-badge__src--long">(auto)</span><span class="text-tertiary ls-badge__src ls-badge__src--short">&middot; auto</span>`;
    if (note) {
      note.innerHTML = alertBox("info", label, "National stage from the optional Worker. Per-stop outage flags need block data, which this snapshot does not include, so journeys are not marked affected.");
    }
  } catch (err) {
    if (note) note.innerHTML = alertBox("info", "Load shedding unavailable", unavailable);
  }
}

function wireMapFrame() {
  const frame = document.getElementById("map-frame");
  if (!frame) return;
  const params = new URLSearchParams(location.search);
  const src = new URL(frame.getAttribute("src"), location.origin);
  params.forEach((value, key) => src.searchParams.set(key, value));
  frame.src = `${src.pathname}${src.search}`;
}

async function main() {
  const page = body.dataset.page;
  const failures = document.getElementById("results") || document.getElementById("stop-root") || document.getElementById("journey-root");
  try {
    if (page === "search") {
      const meta = await getJson(`${DATA}/meta.json`);
      const stops = await getJson(`${DATA}/stops.json`);
      wireSearch(stops, meta);
      await runSearch(meta);
    } else if (page === "stop") {
      const meta = await getJson(`${DATA}/meta.json`);
      const params = new URLSearchParams(location.search);
      const extra = extraOf(meta);
      const now = capeTownParts(new Date());
      const day = params.get("day_type") || dayTypeFor(now.iso, extra);
      const catalog = await loadCatalog(neededDays(meta, day));
      renderStop(stopBoard(catalog, {
        stop: stopName(),
        day_type: params.get("day_type") || undefined,
        operator: params.get("operator") || "both",
        now: new Date().toISOString(),
      }));
    } else if (page === "journey") {
      const params = new URLSearchParams(location.search);
      const day = params.get("day_type") || "weekday";
      const catalog = await loadCatalog([day]);
      const query = Object.fromEntries(params.entries());
      renderJourney(journeyView(catalog, query, new Date()));
    } else if (page === "map") {
      wireMapFrame();
    }
    await refreshLoadShedding();
  } catch (err) {
    if (failures) {
      failures.innerHTML = alertBox("danger", "Could not load the timetable", "The static timetable files did not load. Refresh, or rebuild the site from the snapshot.");
    }
    console.error(err);
  }
}

main();
