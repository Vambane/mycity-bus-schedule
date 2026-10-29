/**
 * nearest.js - "Use my location" on the search form.
 *
 * Asks the browser for the rider's position, downloads the list of stop
 * positions (/api/stops/locations), and picks the nearest stop *in the
 * browser*. The position is never sent to the server: that is the
 * promise made by the privacy line shown on first use.
 *
 * Fills From with the nearest stop and offers the next two as chips.
 * The button only appears when the browser supports geolocation.
 */
(function () {
  'use strict';

  var btn = document.getElementById('locate-btn');
  if (!btn || !('geolocation' in navigator)) return;

  var status = document.getElementById('locate-status');
  var privacy = document.getElementById('locate-privacy');
  var root = document.querySelector('[data-combobox="from_stop"]');
  var input = root && root.querySelector('.combobox-input');
  var native = root && root.querySelector('.combobox-native');
  if (!status || !input || !native) return;

  var WALK_M_PER_MIN = 80;   /* an easy walking pace */
  var DETOUR = 1.3;          /* streets are longer than a straight line */
  var cache = {};

  btn.hidden = false;

  function network() {
    var checked = document.querySelector('[name="operator"]:checked');
    return checked ? checked.value : 'both';
  }

  /* Great-circle distance in metres. */
  function distance(lat1, lon1, lat2, lon2) {
    var R = 6371000, rad = Math.PI / 180;
    var dLat = (lat2 - lat1) * rad, dLon = (lon2 - lon1) * rad;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(lat1 * rad) * Math.cos(lat2 * rad) *
            Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 2 * R * Math.asin(Math.sqrt(a));
  }

  function nearest(stops, lat, lon, n) {
    return stops
      .map(function (s) { return { name: s.name, m: distance(lat, lon, s.lat, s.lon) }; })
      .sort(function (a, b) { return a.m - b.m; })
      .slice(0, n);
  }

  function far(m) {
    return m < 1000 ? Math.round(m / 10) * 10 + ' m' : (m / 1000).toFixed(1) + ' km';
  }

  function walk(m) {
    return Math.max(1, Math.round(m * DETOUR / WALK_M_PER_MIN));
  }

  function choose(name) {
    input.value = name;
    native.value = name;
    root.querySelectorAll('.combobox-option').forEach(function (o) {
      if (o.dataset.value === name) o.setAttribute('aria-selected', 'true');
      else o.removeAttribute('aria-selected');
    });
    native.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function say(text) {
    status.textContent = '';
    if (text) {
      var p = document.createElement('p');
      p.textContent = text;
      status.appendChild(p);
    }
  }

  function showResult(found) {
    var first = found[0];
    choose(first.name);
    say('Nearest stop: ' + first.name + ', ' + far(first.m) +
        ' (about ' + walk(first.m) + ' min walk).');

    if (found.length > 1) {
      var row = document.createElement('div');
      row.className = 'locate-chips';
      var label = document.createElement('span');
      label.className = 'locate-chips__label';
      label.textContent = 'Also close:';
      row.appendChild(label);
      found.slice(1).forEach(function (s) {
        var chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'recent-chip';
        chip.textContent = s.name + ' · ' + far(s.m);
        chip.addEventListener('click', function () {
          choose(s.name);
          say('From set to ' + s.name + ', ' + far(s.m) +
              ' (about ' + walk(s.m) + ' min walk).');
        });
        row.appendChild(chip);
      });
      status.appendChild(row);
    }
  }

  function loadStops(op) {
    if (cache[op]) return Promise.resolve(cache[op]);
    return fetch('/api/stops/locations?operator=' + encodeURIComponent(op))
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) { cache[op] = data; return data; });
  }

  function located(pos) {
    var op = network();
    loadStops(op).then(function (data) {
      var found = nearest(data.stops || [], pos.coords.latitude, pos.coords.longitude, 3);
      if (!found.length) {
        say(op === 'uct'
          ? "UCT stops aren't on the map yet, so we can't find the nearest one. Type your stop instead."
          : "We couldn't match any stop to a location. Type your stop instead.");
        return;
      }
      showResult(found);
    }).catch(function () {
      say("Couldn't load the stop list. Check your connection, or type your stop.");
    }).then(done);
  }

  function failed(err) {
    say(err && err.code === 1
      ? 'Location is blocked for this site. Allow it in your browser settings, or type your stop.'
      : "Couldn't get your location. Try again, or type your stop.");
    done();
  }

  function done() {
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
  }

  btn.addEventListener('click', function () {
    if (privacy) privacy.hidden = false;   /* first ask: say where the location goes */
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    say('Finding your location…');
    navigator.geolocation.getCurrentPosition(located, failed, {
      enableHighAccuracy: false,
      timeout: 10000,
      maximumAge: 60000
    });
  });
})();
