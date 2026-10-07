/* global L */
const $ = (selector) => document.querySelector(selector);
const DAY_NAMES = { weekday: "Weekday", saturday: "Saturday", sunday: "Sunday / holiday" };
let network;
let map;
let routeLayers = [];
let streetLayers = [];
let schematicLayers = [];
let routeColors = {};
let allRoutes = [];
let activeCategory = null;
let mapMode = "street";
let activeResults = [];
let activeUpcoming = [];
let resultKind = "";
let selectedTab = "soon";
let toastTimer;

function html(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function todayType() {
  const day = new Date().getDay();
  return day === 0 ? "sunday" : day === 6 ? "saturday" : "weekday";
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2600);
}

function setLoading() {
  $("#results-area").innerHTML = '<div class="message-card"><span class="spinner"></span>Finding your next ride…</div>';
}

function setView(view) {
  document.querySelectorAll(".nav-link").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  $("#journey-view").classList.toggle("hidden", view !== "journey");
  $("#network-view").classList.toggle("hidden", view !== "network");
  if (view === "network") {
    $("#network-map").scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => map?.invalidateSize(), 220);
  }
}

function routeLabel(routeId) {
  const route = allRoutes.find((item) => item.id === routeId);
  return route && route.name.toLowerCase().startsWith(routeId.toLowerCase())
    ? route.name.slice(routeId.length).replace(/^\s*[–-]?\s*/, "")
    : route?.name || "";
}

function makeMap(mapData) {
  if (!window.L) {
    $("#coverage-label").textContent = "Map unavailable";
    return;
  }
  map = L.map("network-map", {
    zoomControl: false,
    scrollWheelZoom: false,
    preferCanvas: true,
  });
  L.control.zoom({ position: "bottomleft" }).addTo(map);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
    maxZoom: 19,
  }).addTo(map);
  network = mapData;
  allRoutes = mapData.routes;
  allRoutes.forEach((route) => { routeColors[route.id] = route.color; });

  const paths = mapData.paths || {};
  const routesWithStreet = new Set();
  Object.entries(paths).forEach(([routeId, lines]) => {
    const route = allRoutes.find((item) => item.id.toUpperCase() === routeId.toUpperCase());
    if (!route) return;
    routesWithStreet.add(route.id);
    lines.forEach((line) => {
      const layer = L.polyline(line, {
        color: route.color, weight: 3, opacity: .75, lineCap: "round", lineJoin: "round",
      });
      layer.routeId = route.id;
      routeLayers.push(layer);
      streetLayers.push(layer);
      layer.addTo(map);
    });
  });

  const stopCoordinates = new Map(mapData.nodes.map((stop) => [stop.id, [stop.lat, stop.lon]]));
  mapData.links.forEach((link) => {
    const from = stopCoordinates.get(link.source);
    const to = stopCoordinates.get(link.target);
    const route = allRoutes.find((item) => item.id === link.route);
    if (!from || !to || !route) return;
    const layer = L.polyline([from, to], {
      color: route.color, weight: 3, opacity: .75, lineCap: "round",
      dashArray: routesWithStreet.has(route.id) ? null : "5 5",
    });
    layer.routeId = route.id;
    routeLayers.push(layer);
    schematicLayers.push(layer);
    if (!routesWithStreet.has(route.id)) {
      layer.setStyle({ opacity: .6 });
      streetLayers.push(layer);
    }
    layer.addTo(map);
  });
  setMapMode("street");

  const markers = L.layerGroup();
  mapData.nodes.forEach((stop) => {
    const icon = L.divIcon({
      className: "",
      html: '<div class="stop-marker"></div>',
      iconSize: [9, 9],
      iconAnchor: [4, 4],
    });
    const marker = L.marker([stop.lat, stop.lon], { icon, title: stop.id, keyboard: true });
    marker.bindTooltip(html(stop.id), { direction: "top", offset: [0, -5] });
    marker.on("click", () => {
      $("#origin").value = stop.id;
      $("#destination").focus();
      setView("journey");
      $("#origin").scrollIntoView({ behavior: "smooth", block: "center" });
      showToast(`${stop.id} added as your starting stop`);
    });
    markers.addLayer(marker);
  });
  markers.addTo(map);

  const bounds = L.latLngBounds(mapData.nodes.map((stop) => [stop.lat, stop.lon]));
  map.fitBounds(bounds, { padding: [28, 28] });
  $("#coverage-label").textContent = `${mapData.coverage.located} stops · ${allRoutes.length} routes`;
  renderLegend();
}

function setMapMode(mode) {
  mapMode = mode;
  document.querySelectorAll(".map-style").forEach((button) => {
    button.classList.toggle("active", button.dataset.mapStyle === mode);
  });
  routeLayers.forEach((layer) => {
    const route = allRoutes.find((item) => item.id === layer.routeId);
    const modeVisible = mode === "street"
      ? streetLayers.includes(layer)
      : schematicLayers.includes(layer);
    const visible = modeVisible && (!activeCategory || route?.category === activeCategory);
    layer.setStyle({ opacity: visible ? .78 : .08, weight: visible ? 4 : 2 });
  });
}

function renderLegend() {
  const categories = [
    { key: "Trunk", label: "Trunk", color: "#e65461" },
    { key: "Direct", label: "Direct", color: "#4494b6" },
    { key: "Area", label: "Area", color: "#85a66c" },
  ];
  $("#route-legend").innerHTML = categories.map((category) => `
    <button class="legend-chip ${activeCategory === category.key ? "selected" : ""}" data-category="${category.key}">
      <span class="legend-mark" style="--legend-color:${category.color}"></span>${category.label}
    </button>`).join("");
  $("#route-legend").querySelectorAll(".legend-chip").forEach((button) => {
    button.addEventListener("click", () => {
      activeCategory = activeCategory === button.dataset.category ? null : button.dataset.category;
      routeLayers.forEach((layer) => {
        const route = allRoutes.find((item) => item.id === layer.routeId);
        const modeVisible = mapMode === "street"
          ? streetLayers.includes(layer)
          : schematicLayers.includes(layer);
        const visible = modeVisible && (!activeCategory || route?.category === activeCategory);
        layer.setStyle({ opacity: visible ? .78 : .08, weight: visible ? 4 : 2 });
      });
      renderLegend();
    });
  });
}

function createResultsHeader(title, subtitle, tabs = true) {
  return `<div class="result-header">
    <div><div class="result-title">${html(title)}</div><div class="result-subtitle">${html(subtitle)}</div></div>
    ${tabs ? `<div class="result-tabs">
      <button class="result-tab ${selectedTab === "soon" ? "active" : ""}" data-tab="soon">Next buses</button>
      <button class="result-tab ${selectedTab === "fast" ? "active" : ""}" data-tab="fast">Fastest</button>
      <button class="result-tab ${selectedTab === "all" ? "active" : ""}" data-tab="all">All day</button>
    </div>` : ""}
  </div>`;
}

function bindTabs() {
  document.querySelectorAll(".result-tab").forEach((button) => {
    button.addEventListener("click", () => {
      selectedTab = button.dataset.tab;
      renderJourneyResults();
    });
  });
}

function renderJourneyCard(row, badge = "") {
  const transfers = Number(row.transfers || 0);
  const routeIds = row.route_ids || [row.route_id];
  const via = (row.via || []).join(", ");
  const routePills = routeIds.map((id) => `<span class="route-pill" style="--route-color:${html(routeColors[id] || "#176b50")}">${html(id)}</span>`).join('<span class="caption-divider">→</span>');
  const middleText = transfers ? `${transfers} ${transfers === 1 ? "change" : "changes"}` : "Direct";
  const locationText = transfers ? `via ${html(via)}` : html(routeLabel(row.route_id));
  return `<article class="journey-card">
    <div class="route-info">
      ${badge ? `<div class="journey-badges"><span class="badge">${html(badge)}</span></div>` : ""}
      <div class="route-line">${routePills}</div>
      <div class="route-description">${locationText || "&nbsp;"}</div>
    </div>
    <div><div class="time">${html(row.dep.slice(0, 5))}</div><div class="time-label">${html(row.legs?.[0]?.board || $("#origin").value)}</div></div>
    <div class="journey-middle"><div class="journey-track"><span>○</span><span class="track-line"></span><span class="track-label">${middleText}</span><span class="track-line"></span><span>○</span></div><div class="duration">${html(row.duration)}</div></div>
    <div><div class="time">${html(row.arr.slice(0, 5))}</div><div class="time-label">${html(row.legs?.at(-1)?.alight || $("#destination").value)}</div></div>
    <div>${transfers ? `<div class="route-description">${(row.legs || []).map((leg) => `${html(leg.route_id)} ${html(leg.dep.slice(0, 5))}`).join(" · ")}</div>` : ""}</div>
  </article>`;
}

function renderJourneyResults() {
  const area = $("#results-area");
  if (resultKind === "none") {
    area.innerHTML = `<div class="message-card"><span class="message-icon">↗</span>No service found for this pair of stops on ${html(DAY_NAMES[$("#day-type").value])}. Try nearby stops or another day.</div>`;
    return;
  }
  const transferNotice = resultKind === "transfer"
    ? '<div class="message-card" style="margin-bottom:14px"><span class="message-icon">↗</span>No direct service for this trip. These timetable-based connections include a transfer and are suggestions, not guaranteed connections.</div>'
    : "";
  let rows;
  if (selectedTab === "all") rows = activeResults;
  else if (selectedTab === "fast") rows = [...activeUpcoming].sort((a, b) => a.duration_min - b.duration_min || a.dep.localeCompare(b.dep));
  else rows = activeUpcoming;
  if (selectedTab !== "all") rows = rows.slice(0, 8);
  const title = `${$("#origin").value}  →  ${$("#destination").value}`;
  const subtitle = `${rows.length ? `${rows.length} ${selectedTab === "all" ? "scheduled" : "upcoming"} connection${rows.length === 1 ? "" : "s"}` : "No more departures today"} · ${DAY_NAMES[$("#day-type").value]} · Cape Town time`;
  const cards = rows.map((row, index) => renderJourneyCard(row, selectedTab === "fast" && index === 0 ? "Fastest" : selectedTab === "soon" && index === 0 ? "Next ride" : "")).join("");
  area.innerHTML = transferNotice + createResultsHeader(title, subtitle) + (cards || '<div class="message-card"><span class="message-icon">◷</span>No more buses today. Choose “All day” to see the full timetable.</div>');
  bindTabs();
}

function renderStopBoard(data) {
  const area = $("#results-area");
  const routeNames = data.routes.map((route) => route.id).join(" · ");
  if (!data.departures.length) {
    area.innerHTML = createResultsHeader(data.stop, `Served by ${routeNames || "no listed routes"} · Current time ${data.now}`, false)
      + '<div class="message-card"><span class="message-icon">◷</span>No more buses today for this day type. Try a different day.</div>';
    return;
  }
  const groups = new Map();
  data.departures.forEach((departure) => {
    const key = `${departure.route_id}|${departure.direction}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(departure);
  });
  const board = [...groups.values()].map((departures) => {
    const first = departures[0];
    const colors = routeColors[first.route_id] || "#176b50";
    return `<div class="board-group">
      <div class="board-group-head"><strong><span class="route-pill" style="--route-color:${html(colors)}">${html(first.route_id)}</span> &nbsp;${html(first.route_name)}</strong><span>${html(first.direction)}</span></div>
      <div class="departure-list">${departures.slice(0, 10).map((departure) => `<div class="departure-item"><div class="departure-time">${html(departure.departure_time.slice(0, 5))}</div><div class="departure-direction">${html(departure.direction)}</div></div>`).join("")}</div>
    </div>`;
  }).join("");
  area.innerHTML = createResultsHeader(data.stop, `Served by ${routeNames || "listed routes"} · Upcoming departures after ${data.now}`, false) + board;
}

async function submitSearch(event) {
  event.preventDefault();
  const origin = $("#origin").value.trim();
  const destination = $("#destination").value.trim();
  if (!origin) return showToast("Choose a starting stop first");
  setLoading();
  selectedTab = "soon";
  try {
    if (!destination) {
      const params = new URLSearchParams({ stop: origin, day: $("#day-type").value });
      const response = await fetch(`/api/stop?${params}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not load departures.");
      renderStopBoard(data);
      return;
    }
    const params = new URLSearchParams({ from: origin, to: destination, day: $("#day-type").value });
    const response = await fetch(`/api/journey?${params}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not plan this trip.");
    resultKind = data.kind;
    activeResults = data.results;
    activeUpcoming = data.upcoming;
    renderJourneyResults();
  } catch (error) {
    $("#results-area").innerHTML = `<div class="message-card"><span class="message-icon">!</span>${html(error.message)}</div>`;
  }
}

async function init() {
  $("#day-type").value = todayType();
  $("#clock-label").textContent = `Cape Town · ${new Intl.DateTimeFormat("en-ZA", { timeZone: "Africa/Johannesburg", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date())}`;
  $("#search-form").addEventListener("submit", submitSearch);
  $("#swap-stops").addEventListener("click", () => {
    const origin = $("#origin").value;
    $("#origin").value = $("#destination").value;
    $("#destination").value = origin;
  });
  document.querySelectorAll(".nav-link").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  $("#map-expand").addEventListener("click", () => {
    setView("network");
    document.querySelector(".map-shell").classList.toggle("expanded");
    setTimeout(() => map?.invalidateSize(), 250);
  });
  document.querySelectorAll(".map-style").forEach((button) => {
    button.addEventListener("click", () => setMapMode(button.dataset.mapStyle));
  });
  try {
    const response = await fetch("/api/bootstrap");
    if (!response.ok) throw new Error("Website data could not be loaded.");
    const data = await response.json();
    $("#day-type").value = data.dayType;
    const list = $("#stops-list");
    data.stops.forEach((stop) => {
      const option = document.createElement("option");
      option.value = stop;
      list.append(option);
    });
    if (data.updated) $("#updated-label").textContent = `Timetable snapshot · updated ${data.updated} UTC`;
    makeMap(data.network);
  } catch (error) {
    $("#coverage-label").textContent = "Network data unavailable";
    $("#results-area").innerHTML = `<div class="message-card"><span class="message-icon">!</span>${html(error.message)} Refresh the page to try again.</div>`;
  }
}

init();
