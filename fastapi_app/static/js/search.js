/**
 * search.js - Search page interactions
 *
 * - Swap button 180-degree rotation animation
 * - Remember last search in localStorage
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'myciti_last_search';

  /* ---- Swap button rotation ---- */
  var swapBtn = document.getElementById('swap-btn');
  if (swapBtn) {
    swapBtn.addEventListener('click', function (e) {
      /* Animate rotation before navigating */
      swapBtn.classList.add('rotated');
    });
  }

  /* ---- Save / restore last search ---- */
  var form = document.getElementById('search-form');
  if (!form) return;

  /* Save current search to localStorage on submit */
  form.addEventListener('submit', function () {
    try {
      var data = {
        from_stop: form.querySelector('[name="from_stop"]').value,
        to_stop: form.querySelector('[name="to_stop"]').value,
        day_type: (form.querySelector('[name="day_type"]:checked') || {}).value || 'weekday'
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    } catch (e) {
      /* localStorage not available, silently ignore */
    }
  });

  /* Restore last search if form is empty */
  var fromNative = form.querySelector('select[name="from_stop"]');
  var toNative   = form.querySelector('select[name="to_stop"]');

  if (fromNative && !fromNative.value && toNative && !toNative.value) {
    try {
      var saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
      if (saved && saved.from_stop && saved.to_stop) {
        /* Populate native selects */
        fromNative.value = saved.from_stop;
        toNative.value   = saved.to_stop;

        /* Populate combobox inputs */
        var fromInput = form.querySelector('[data-combobox="from_stop"] .combobox-input');
        var toInput   = form.querySelector('[data-combobox="to_stop"] .combobox-input');
        if (fromInput) fromInput.value = saved.from_stop;
        if (toInput)   toInput.value   = saved.to_stop;

        /* Set day type radio */
        var radio = form.querySelector('[name="day_type"][value="' + saved.day_type + '"]');
        if (radio) radio.checked = true;
      }
    } catch (e) {
      /* Parsing error or no data, ignore */
    }
  }
})();
