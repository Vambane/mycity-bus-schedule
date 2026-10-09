/**
 * search.js - Search page interactions
 *
 * - Swap button 180-degree rotation animation
 * - Recent searches history (last 5, stored in localStorage)
 * - Relative departure time labels ("in X min")
 */
(function () {
  'use strict';

  var HISTORY_KEY = 'myciti_search_history';
  var LEGACY_KEY  = 'myciti_last_search';
  var MAX_HISTORY = 5;

  /* ---- Swap button rotation ---- */
  var swapBtn = document.getElementById('swap-btn');
  if (swapBtn) {
    swapBtn.addEventListener('click', function () {
      swapBtn.classList.add('rotated');
    });
  }

  /* ---- Search history helpers ---- */

  function loadHistory() {
    try {
      var raw = localStorage.getItem(HISTORY_KEY);
      if (raw) return JSON.parse(raw);

      /* Migrate legacy single-search format */
      var legacy = localStorage.getItem(LEGACY_KEY);
      if (legacy) {
        var obj = JSON.parse(legacy);
        if (obj && obj.from_stop && obj.to_stop) {
          var migrated = [obj];
          localStorage.setItem(HISTORY_KEY, JSON.stringify(migrated));
          localStorage.removeItem(LEGACY_KEY);
          return migrated;
        }
      }
    } catch (e) { /* ignore */ }
    return [];
  }

  function saveHistory(history) {
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
    } catch (e) { /* ignore */ }
  }

  function pushSearch(from, to, dayType) {
    var history = loadHistory();
    /* Remove duplicate if it exists */
    history = history.filter(function (h) {
      return !(h.from_stop === from && h.to_stop === to);
    });
    /* Add to front */
    history.unshift({ from_stop: from, to_stop: to, day_type: dayType });
    /* Cap at MAX_HISTORY */
    if (history.length > MAX_HISTORY) history = history.slice(0, MAX_HISTORY);
    saveHistory(history);
  }

  function removeFromHistory(index) {
    var history = loadHistory();
    history.splice(index, 1);
    saveHistory(history);
    renderChips();
  }

  function renderChips() {
    var container = document.getElementById('recent-searches');
    if (!container) return;

    var history = loadHistory();
    if (history.length === 0) {
      container.style.display = 'none';
      return;
    }

    container.innerHTML = '<span class="recent-label">Recent</span>';
    container.style.display = '';

    history.forEach(function (item, i) {
      var chip = document.createElement('a');
      chip.className = 'recent-chip';
      chip.href = '/?from_stop=' + encodeURIComponent(item.from_stop) +
                  '&to_stop=' + encodeURIComponent(item.to_stop) +
                  '&day_type=' + encodeURIComponent(item.day_type || 'weekday');

      var text = document.createElement('span');
      text.textContent = item.from_stop + ' \u2192 ' + item.to_stop;
      chip.appendChild(text);

      var close = document.createElement('button');
      close.className = 'recent-chip-close';
      close.type = 'button';
      close.setAttribute('aria-label', 'Remove from history');
      close.innerHTML = '&times;';
      close.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        removeFromHistory(i);
      });
      chip.appendChild(close);

      container.appendChild(chip);
    });
  }

  /* ---- Form handling ---- */
  var form = document.getElementById('search-form');
  if (!form) return;

  /* Save search to history on submit */
  form.addEventListener('submit', function () {
    var from = form.querySelector('[name="from_stop"]').value;
    var to = form.querySelector('[name="to_stop"]').value;
    var dayType = (form.querySelector('[name="day_type"]:checked') || {}).value || 'weekday';
    if (from && to) pushSearch(from, to, dayType);
  });

  /* Restore last search if form is empty */
  var fromNative = form.querySelector('select[name="from_stop"]');
  var toNative   = form.querySelector('select[name="to_stop"]');

  if (fromNative && !fromNative.value && toNative && !toNative.value) {
    var history = loadHistory();
    if (history.length > 0) {
      var saved = history[0];
      fromNative.value = saved.from_stop;
      toNative.value   = saved.to_stop;

      var fromInput = form.querySelector('[data-combobox="from_stop"] .combobox-input');
      var toInput   = form.querySelector('[data-combobox="to_stop"] .combobox-input');
      if (fromInput) fromInput.value = saved.from_stop;
      if (toInput)   toInput.value   = saved.to_stop;

      var radio = form.querySelector('[name="day_type"][value="' + saved.day_type + '"]');
      if (radio) radio.checked = true;
    }

    /* Show chips only on the empty search page */
    renderChips();
  }
})();

/* ---- Relative departure times ---- */
(function () {
  'use strict';

  function computeRelativeTimes() {
    var now = new Date();
    var nowMinutes = now.getHours() * 60 + now.getMinutes();

    /* Journey cards: .journey-time elements (dep/arr times) */
    var journeyTimes = document.querySelectorAll('.journey-endpoint .journey-time');
    journeyTimes.forEach(function (el) {
      /* Only process departure times (first endpoint in each viz) */
      var endpoint = el.closest('.journey-endpoint');
      if (!endpoint || endpoint.style.textAlign === 'right') return;

      addRelativeLabel(el, nowMinutes);
    });

    /* Stop page: .departure-time elements */
    var depTimes = document.querySelectorAll('.departure-time');
    depTimes.forEach(function (el) {
      addRelativeLabel(el, nowMinutes);
    });

    /* Stop page: .metric-value (next departure highlight) */
    var metrics = document.querySelectorAll('.metric-value');
    metrics.forEach(function (el) {
      addRelativeLabel(el, nowMinutes);
    });
  }

  function addRelativeLabel(el, nowMinutes) {
    /* Skip if already processed */
    if (el.querySelector('.time-relative')) return;

    var text = el.textContent.trim();
    var match = text.match(/^(\d{1,2}):(\d{2})$/);
    if (!match) return;

    var depMinutes = parseInt(match[1], 10) * 60 + parseInt(match[2], 10);
    var diff = depMinutes - nowMinutes;

    /* Only show for departures in the next 60 minutes */
    if (diff < 0 || diff > 60) return;

    var span = document.createElement('span');
    span.className = 'time-relative';
    if (diff <= 5) span.classList.add('time-relative--soon');

    if (diff === 0) {
      span.textContent = 'now';
    } else {
      span.textContent = 'in ' + diff + ' min';
    }

    el.appendChild(span);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', computeRelativeTimes);
  } else {
    computeRelativeTimes();
  }
})();
