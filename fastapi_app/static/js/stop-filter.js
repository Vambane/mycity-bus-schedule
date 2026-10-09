/**
 * stop-filter.js - Route badge filtering on the stop departure board
 *
 * Click a route badge to show only that route's departures.
 * Click again to return to showing all routes.
 */
(function () {
  'use strict';

  var badges = document.querySelectorAll('.route-badge--filterable');
  var cards  = document.querySelectorAll('.departure-card');
  var status = document.getElementById('route-filter-status');

  if (!badges.length || !cards.length) return;

  var activeRoute = null;

  badges.forEach(function (badge) {
    badge.addEventListener('click', function () {
      var routeId = badge.getAttribute('data-route-id');

      if (activeRoute === routeId) {
        /* Same badge clicked again — clear filter */
        activeRoute = null;
        showAll();
      } else {
        /* New filter */
        activeRoute = routeId;
        filterTo(routeId);
      }
    });
  });

  function filterTo(routeId) {
    /* Dim non-matching badges */
    badges.forEach(function (b) {
      if (b.getAttribute('data-route-id') === routeId) {
        b.classList.remove('inactive');
      } else {
        b.classList.add('inactive');
      }
    });

    /* Show/hide departure cards */
    cards.forEach(function (card) {
      if (card.getAttribute('data-route-id') === routeId) {
        card.classList.remove('filtered-out');
      } else {
        card.classList.add('filtered-out');
      }
    });

    /* Update status text */
    if (status) {
      var name = '';
      badges.forEach(function (b) {
        if (b.getAttribute('data-route-id') === routeId) {
          name = b.textContent.trim();
        }
      });
      status.textContent = 'Showing ' + name;
    }
  }

  function showAll() {
    badges.forEach(function (b) { b.classList.remove('inactive'); });
    cards.forEach(function (c) { c.classList.remove('filtered-out'); });
    if (status) status.textContent = '';
  }
})();
