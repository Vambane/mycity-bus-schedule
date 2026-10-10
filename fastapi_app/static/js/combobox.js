/**
 * combobox.js - Accessible searchable combobox
 *
 * Wraps each [data-combobox] element into a type-to-filter dropdown.
 * Keyboard: ArrowDown/Up navigate, Enter selects, Escape closes.
 * Syncs selected value to the hidden native <select> for form submission.
 */
(function () {
  'use strict';

  document.querySelectorAll('[data-combobox]').forEach(initCombobox);

  /* Expose globally so search.js can re-init after operator changes */
  window.initCombobox = initCombobox;

  function initCombobox(root) {
    /* Guard: skip if already initialised (prevents double-binding) */
    if (root._comboboxInit) return;
    root._comboboxInit = true;

    var input   = root.querySelector('.combobox-input');
    var list    = root.querySelector('.combobox-list');
    var native  = root.querySelector('.combobox-native');
    var chevron = root.querySelector('.combobox-chevron');
    var highlighted = -1;

    /* Live DOM query — always returns the current set of options,
       so after search.js rebuilds the list for a new operator,
       existing event listeners still work correctly. */
    function getOptions() {
      return Array.from(list.querySelectorAll('.combobox-option'));
    }

    /* --- Sync initial value --- */
    if (native.value) {
      input.value = native.value;
    }

    /* --- Filter logic --- */
    function filter(query) {
      var q = query.toLowerCase().trim();
      var visible = 0;

      getOptions().forEach(function (opt) {
        var match = !q || opt.textContent.trim().toLowerCase().indexOf(q) !== -1;
        opt.style.display = match ? '' : 'none';
        opt.removeAttribute('aria-selected');
        opt.classList.remove('highlighted');
        if (match) visible++;
      });

      /* Show empty message if nothing matches */
      var empty = list.querySelector('.combobox-empty');
      if (visible === 0) {
        if (!empty) {
          empty = document.createElement('li');
          empty.className = 'combobox-empty';
          empty.textContent = 'No stops found';
          list.appendChild(empty);
        }
        empty.style.display = '';
      } else if (empty) {
        empty.style.display = 'none';
      }

      highlighted = -1;
    }

    /* --- Open / close --- */
    function open() {
      root.classList.add('open');
      input.setAttribute('aria-expanded', 'true');
      filter(input.value);
    }

    function close() {
      root.classList.remove('open');
      input.setAttribute('aria-expanded', 'false');
      highlighted = -1;
    }

    /* --- Select a value --- */
    function select(opt) {
      var value = opt.dataset.value;
      input.value = value;
      native.value = value;

      /* Mark selected */
      getOptions().forEach(function (o) { o.removeAttribute('aria-selected'); });
      opt.setAttribute('aria-selected', 'true');

      close();
    }

    /* --- Keyboard navigation --- */
    function getVisible() {
      return getOptions().filter(function (o) { return o.style.display !== 'none'; });
    }

    function highlightIndex(idx) {
      var visible = getVisible();
      if (visible.length === 0) return;

      /* Wrap */
      if (idx < 0) idx = visible.length - 1;
      if (idx >= visible.length) idx = 0;

      visible.forEach(function (o) { o.classList.remove('highlighted'); });
      visible[idx].classList.add('highlighted');
      visible[idx].scrollIntoView({ block: 'nearest' });
      highlighted = idx;
    }

    /* --- Events --- */
    input.addEventListener('focus', open);

    input.addEventListener('input', function () {
      open();
      filter(input.value);
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (!root.classList.contains('open')) open();
        highlightIndex(highlighted + 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        highlightIndex(highlighted - 1);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        var visible = getVisible();
        if (highlighted >= 0 && visible[highlighted]) {
          select(visible[highlighted]);
        } else if (visible.length === 1) {
          select(visible[0]);
        }
      } else if (e.key === 'Escape') {
        close();
        input.blur();
      }
    });

    /* Click an option */
    list.addEventListener('click', function (e) {
      var opt = e.target.closest('.combobox-option');
      if (opt) select(opt);
    });

    /* Close on outside click */
    document.addEventListener('click', function (e) {
      if (!root.contains(e.target)) {
        /* If user typed a partial match, try to resolve it */
        if (root.classList.contains('open')) {
          var match = getOptions().find(function (o) {
            return o.textContent.trim().toLowerCase() === input.value.toLowerCase().trim();
          });
          if (match) {
            select(match);
          } else if (!native.value) {
            input.value = '';
          }
        }
        close();
      }
    });
  }
})();
