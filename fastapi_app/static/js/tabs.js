/**
 * tabs.js - Tab switching with sliding gold indicator
 *
 * Works with the tab-bar / tab-panel pattern.
 * Uses [data-tab] on buttons and matching panel IDs.
 */
(function () {
  'use strict';

  var bar = document.querySelector('.tab-bar');
  if (!bar) return;

  var buttons   = Array.from(bar.querySelectorAll('.tab-btn'));
  var indicator = document.getElementById('tab-indicator');

  /* Position the indicator on the active button */
  function moveIndicator(btn) {
    if (!indicator) return;
    indicator.style.width = btn.offsetWidth + 'px';
    indicator.style.left  = btn.offsetLeft + 'px';
  }

  /* Activate a tab */
  function activate(btn) {
    var tabName = btn.dataset.tab;

    /* Update button states */
    buttons.forEach(function (b) {
      var isActive = b === btn;
      b.classList.toggle('active', isActive);
      b.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });

    /* Update panel visibility */
    document.querySelectorAll('.tab-panel').forEach(function (panel) {
      panel.classList.remove('active');
    });
    var target = document.getElementById('panel-' + tabName);
    if (target) {
      target.classList.add('active');
    }

    /* Slide the indicator */
    moveIndicator(btn);
  }

  /* Click handler */
  buttons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      activate(btn);
    });
  });

  /* Keyboard: left/right arrow between tabs */
  bar.addEventListener('keydown', function (e) {
    var idx = buttons.indexOf(document.activeElement);
    if (idx === -1) return;

    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault();
      var next = e.key === 'ArrowRight'
        ? (idx + 1) % buttons.length
        : (idx - 1 + buttons.length) % buttons.length;
      buttons[next].focus();
      activate(buttons[next]);
    }
  });

  /* Initial position */
  var active = bar.querySelector('.tab-btn.active');
  if (active) moveIndicator(active);
})();
