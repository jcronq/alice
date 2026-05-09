/* Stage D review — one-keystroke labeling.
 *
 * Loads page config from #stage-d-config, listens for keys at the
 * document level (so focus doesn't have to be on a row), and POSTs
 * to /api/stage-d-label. After a successful label, refreshes the
 * rows + summary partials in place via fetch (we don't bounce the
 * whole page) and advances the active row to the next unlabeled.
 */
(function () {
  const cfg = (() => {
    const tag = document.getElementById('stage-d-config');
    if (!tag) return {};
    try { return JSON.parse(tag.textContent || '{}'); }
    catch (_) { return {}; }
  })();
  const KEY_MAP = cfg.key_map || {};
  const SKIP_KEYS = new Set(cfg.skip_keys || []);
  const STORAGE_KEY = 'stage-d-review/active-id';

  function $(id) { return document.getElementById(id); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function activeRow() {
    return document.querySelector('.stage-d-row-active') ||
           document.querySelector('.stage-d-row');
  }

  function setActive(row) {
    if (!row) return;
    $$('.stage-d-row-active').forEach(r => r.classList.remove('stage-d-row-active'));
    row.classList.add('stage-d-row-active');
    const det = row.querySelector('details.stage-d-synth');
    if (det) det.open = true;
    // Persist & scroll into view (smooth, only if not visible).
    try { localStorage.setItem(STORAGE_KEY, row.dataset.id || ''); } catch (_) {}
    const rect = row.getBoundingClientRect();
    if (rect.top < 60 || rect.bottom > window.innerHeight - 40) {
      row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }

  function nextUnlabeledFrom(row) {
    let n = row && row.nextElementSibling;
    while (n) {
      if (n.classList && n.classList.contains('stage-d-row') && n.dataset.unlabeled === '1') {
        return n;
      }
      n = n.nextElementSibling;
    }
    // Wrap to the first unlabeled if none found below.
    return document.querySelector('.stage-d-row[data-unlabeled="1"]');
  }

  function nextRow(row) {
    let n = row && row.nextElementSibling;
    while (n) {
      if (n.classList && n.classList.contains('stage-d-row')) return n;
      n = n.nextElementSibling;
    }
    return document.querySelector('.stage-d-row');
  }

  function toast(msg, kind) {
    const wrap = $('stage-d-toast-wrap');
    if (!wrap) return;
    const el = document.createElement('div');
    el.className = 'stage-d-toast' + (kind ? ' stage-d-toast-' + kind : '');
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(() => { el.classList.add('stage-d-toast-fade'); }, 1200);
    setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 1900);
  }

  function rowsRefreshURL(activeId) {
    const params = new URLSearchParams();
    if (cfg.since) params.set('since', cfg.since);
    if (cfg.days) params.set('days', cfg.days);
    if (cfg.status) params.set('status', cfg.status);
    if (activeId) params.set('focus', activeId);
    return '/stage-d-review/rows?' + params.toString();
  }
  function summaryRefreshURL() {
    const params = new URLSearchParams();
    if (cfg.since) params.set('since', cfg.since);
    if (cfg.days) params.set('days', cfg.days);
    return '/stage-d-review/summary?' + params.toString();
  }

  async function refreshAfterLabel(advanceFromId) {
    // Re-render rows partial first; then summary. Order matters because
    // we want to find the next-unlabeled inside the freshly-rendered
    // rows DOM.
    try {
      const r = await fetch(rowsRefreshURL(advanceFromId), { credentials: 'same-origin' });
      if (r.ok) {
        const html = await r.text();
        const wrap = $('stage-d-rows-wrap');
        if (wrap) wrap.innerHTML = html;
      }
    } catch (e) { /* best-effort */ }
    try {
      const r = await fetch(summaryRefreshURL(), { credentials: 'same-origin' });
      if (r.ok) {
        const html = await r.text();
        const wrap = $('stage-d-summary-wrap');
        if (wrap) wrap.innerHTML = html;
      }
    } catch (e) { /* best-effort */ }

    // The freshly-rendered partial sets data-active-id from focus=… ;
    // honor it. Then advance to next unlabeled if the focused row is
    // labeled (which it is, post-POST).
    const ol = $('stage-d-rows');
    if (!ol) return;
    let target = null;
    if (advanceFromId) {
      const just = ol.querySelector('[data-id="' + CSS.escape(advanceFromId) + '"]');
      target = just ? nextUnlabeledFrom(just) : null;
    }
    if (!target) target = ol.querySelector('.stage-d-row[data-unlabeled="1"]');
    if (!target) target = ol.querySelector('.stage-d-row');
    if (target) setActive(target);
  }

  async function postLabel(attemptId, label) {
    const body = new URLSearchParams();
    body.set('attempt_id', attemptId);
    body.set('label', label);
    const res = await fetch('/api/stage-d-label', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    });
    if (!res.ok) {
      let msg = 'label failed';
      try {
        const j = await res.json();
        if (j.error) msg = 'label failed: ' + j.error;
      } catch (_) {}
      toast(msg, 'err');
      return false;
    }
    return true;
  }

  async function applyLabel(label) {
    const row = activeRow();
    if (!row) return;
    const id = row.dataset.id;
    if (!id) return;
    const ok = await postLabel(id, label);
    if (!ok) return;
    toast('labeled ' + (label || '?') + ' · ' + (id.slice(-12)), 'ok');
    await refreshAfterLabel(id);
  }

  function skipToNext() {
    const row = activeRow();
    if (!row) return;
    const next = nextUnlabeledFrom(row) || nextRow(row);
    if (next) setActive(next);
  }

  // Click-to-label on the per-row buttons (mouse fallback for the
  // keystroke flow).
  document.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-stage-d-label]');
    if (!btn) return;
    ev.preventDefault();
    const id = btn.dataset.stageDAttempt;
    const label = btn.dataset.stageDLabel;
    if (!id || !label) return;
    // Make the clicked row active first so the toast / advance logic
    // is consistent with keystroke flow.
    const row = btn.closest('.stage-d-row');
    if (row) setActive(row);
    applyLabel(label);
  });

  // Click on a row body (not on an actionable element) makes it active.
  document.addEventListener('click', (ev) => {
    if (ev.target.closest('[data-stage-d-label]')) return;
    if (ev.target.closest('a, button, summary, input, select, textarea, kbd')) return;
    const row = ev.target.closest('.stage-d-row');
    if (row) setActive(row);
  });

  // Keystroke handler — only fire when the user isn't typing into a
  // form field.
  document.addEventListener('keydown', (ev) => {
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    const tag = (ev.target && ev.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
    if (ev.target && ev.target.isContentEditable) return;
    const k = ev.key;
    if (SKIP_KEYS.has(k)) {
      ev.preventDefault();
      skipToNext();
      return;
    }
    const lbl = KEY_MAP[k];
    if (!lbl) return;
    ev.preventDefault();
    applyLabel(lbl);
  });

  // On load: if localStorage has a remembered active id and it's still
  // present in the DOM, prefer it over the server-picked default.
  function init() {
    let stored = null;
    try { stored = localStorage.getItem(STORAGE_KEY); } catch (_) {}
    if (stored) {
      const el = document.querySelector('[data-id="' + CSS.escape(stored) + '"]');
      if (el) { setActive(el); return; }
    }
    const def = activeRow();
    if (def) setActive(def);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
