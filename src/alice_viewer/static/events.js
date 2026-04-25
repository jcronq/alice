/* Shared kind metadata + modal infrastructure.
 *
 * Kept in lockstep with alice_viewer/labels.py. If you add an event kind on
 * the Python side, add it here too so live-tailed events and graph nodes get
 * the same humanized label and color family.
 */

const KIND_LABELS = {
  wake_start: 'wake start',
  wake_end: 'wake end',
  timeout: 'timeout',
  exception: 'exception',
  signal_turn_start: 'signal in',
  signal_turn_end: 'signal done',
  signal_send: 'signal sent',
  surface_dispatch: 'surface in',
  surface_turn_end: 'surface done',
  emergency_dispatch: 'emergency in',
  emergency_turn_end: 'emergency done',
  emergency_voiced: 'emergency voiced',
  emergency_downgraded: 'emergency downgrade',
  emergency_error: 'emergency error',
  emergency_no_recipient: 'emergency: no recipient',
  daemon_start: 'daemon start',
  daemon_ready: 'daemon ready',
  shutdown: 'shutdown',
  tool_use: 'tool call',
  user_message: 'tool result',
  assistant_text: 'reply',
  thinking: 'thought',
  assistant_error: 'assistant error',
  result: 'result',
  config_reload: 'config reload',
  quiet_queue_enter: 'queued (quiet hours)',
  quiet_queue_drain: 'queue drained',
  system: 'system',
  surface_pending: 'surface · pending',
  surface_resolved: 'surface · resolved',
  emergency_pending: 'emergency · pending',
  emergency_resolved: 'emergency · resolved',
  note_pending: 'note · pending',
  note_consumed: 'note · consumed',
  thought_written: 'thought · written',
  turn_log: 'signal turn (legacy)',
};

const KIND_FAMILIES = {
  tool_use: 'tool', user_message: 'tool',
  assistant_text: 'text', thinking: 'thought',
  result: 'result',
  wake_start: 'boundary', wake_end: 'boundary',
  daemon_start: 'boundary', daemon_ready: 'boundary', shutdown: 'boundary',
  surface_turn_end: 'boundary', emergency_turn_end: 'boundary',
  signal_turn_start: 'turn', signal_turn_end: 'turn', signal_send: 'turn', turn_log: 'turn',
  surface_dispatch: 'artifact', surface_pending: 'artifact', surface_resolved: 'artifact',
  thought_written: 'thought',
  note_pending: 'note', note_consumed: 'note',
  emergency_dispatch: 'emergency', emergency_voiced: 'emergency',
  emergency_pending: 'emergency', emergency_resolved: 'emergency',
  emergency_downgraded: 'emergency', emergency_no_recipient: 'emergency',
  timeout: 'error', exception: 'error', emergency_error: 'error', assistant_error: 'error',
  config_reload: 'meta', quiet_queue_enter: 'meta', quiet_queue_drain: 'meta', system: 'meta',
};

window.humanizeKind = (k) => KIND_LABELS[k] || (k || '').replace(/_/g, ' ');
window.kindFamily   = (k) => KIND_FAMILIES[k] || 'meta';

/* ------------------------------------------------------------------ */
/* Markdown rendering — marked + DOMPurify from CDN. Always sanitize. */

function renderMarkdown(text) {
  if (!text) return '';
  try {
    const html = window.marked.parse(String(text), { breaks: true, gfm: true });
    return window.DOMPurify.sanitize(html);
  } catch (e) {
    return '<pre>' + escapeHtml(text) + '</pre>';
  }
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function fmtTs(ts) {
  try { return new Date(ts * 1000).toLocaleString(); } catch (e) { return String(ts); }
}

function fmtDuration(ms) {
  if (!ms) return '—';
  if (ms < 1000) return ms + ' ms';
  return (ms / 1000).toFixed(2) + ' s';
}

function fmtUsd(n) {
  if (n == null) return '—';
  return '$' + Number(n).toFixed(4);
}

function jsonPre(obj) {
  const el = document.createElement('pre');
  el.textContent = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2);
  return el.outerHTML;
}

function tryParseJson(s) {
  if (typeof s !== 'string') return s;
  try { return JSON.parse(s); } catch (e) { return s; }
}

function chipsHtml(arr, cls = 'chip') {
  if (!arr || !arr.length) return '<span class="muted">—</span>';
  return arr.map(x => `<span class="${cls}">${escapeHtml(x)}</span>`).join(' ');
}

function kvTable(pairs) {
  const rows = pairs
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `<span class="k">${escapeHtml(k)}</span><span class="v">${v}</span>`)
    .join('');
  return `<div class="detail-grid">${rows}</div>`;
}

/* ------------------------------------------------------------------ */
/* Event-kind-specific modal renderers.                                */

const TOOL_INPUT_RENDERERS = {
  Bash: (input) => {
    const cmd = input.command || '';
    const desc = input.description ? `<div class="muted" style="margin-bottom:4px">${escapeHtml(input.description)}</div>` : '';
    const tmo = input.timeout ? `<div class="muted" style="margin-top:4px">timeout ${input.timeout}ms</div>` : '';
    const bg  = input.run_in_background ? `<div class="muted">run in background: yes</div>` : '';
    return desc + `<pre class="code-block">${escapeHtml(cmd)}</pre>` + tmo + bg;
  },
  Read: (input) => {
    const parts = [`<pre class="code-block">${escapeHtml(input.file_path || '')}</pre>`];
    const range = [];
    if (input.offset != null) range.push('offset ' + input.offset);
    if (input.limit != null) range.push('limit ' + input.limit);
    if (range.length) parts.push(`<div class="muted">${range.join(' · ')}</div>`);
    return parts.join('');
  },
  Write: (input) => {
    let out = `<pre class="code-block">${escapeHtml(input.file_path || '')}</pre>`;
    if (input.content) out += `<h2>content</h2><pre class="code-block">${escapeHtml(input.content)}</pre>`;
    return out;
  },
  Edit: (input) => {
    const flag = input.replace_all ? `<div class="muted">replace all</div>` : '';
    return `<pre class="code-block">${escapeHtml(input.file_path || '')}</pre>` + flag +
      `<h2>old</h2><pre class="code-block">${escapeHtml(input.old_string || '')}</pre>` +
      `<h2>new</h2><pre class="code-block">${escapeHtml(input.new_string || '')}</pre>`;
  },
  Grep: (input) => {
    const extras = [];
    if (input.path) extras.push(`path <code>${escapeHtml(input.path)}</code>`);
    if (input.glob) extras.push(`glob <code>${escapeHtml(input.glob)}</code>`);
    if (input.type) extras.push(`type ${escapeHtml(input.type)}`);
    if (input.output_mode) extras.push(`mode ${escapeHtml(input.output_mode)}`);
    const meta = extras.length ? `<div class="muted" style="margin-top:6px">${extras.join(' · ')}</div>` : '';
    return `<pre class="code-block">${escapeHtml(input.pattern || '')}</pre>` + meta;
  },
  Glob: (input) => {
    const p = input.path ? `<div class="muted" style="margin-top:6px">in <code>${escapeHtml(input.path)}</code></div>` : '';
    return `<pre class="code-block">${escapeHtml(input.pattern || '')}</pre>` + p;
  },
  WebFetch: (input) => {
    const url = input.url
      ? `<div><a href="${escapeHtml(input.url)}" target="_blank">${escapeHtml(input.url)}</a></div>`
      : '';
    const prompt = input.prompt ? `<h2>prompt</h2><pre class="code-block">${escapeHtml(input.prompt)}</pre>` : '';
    return url + prompt;
  },
};

const KIND_RENDERERS = {
  assistant_text: (rec) => renderMarkdown(rec.detail.text),
  thinking: (rec) =>
    `<div class="callout">internal reasoning — not visible to the user</div>` +
    renderMarkdown(rec.detail.text),
  assistant_error: (rec) =>
    `<div class="callout err">assistant error</div><pre class="code-block">${escapeHtml(rec.detail.error || JSON.stringify(rec.detail, null, 2))}</pre>`,

  tool_use: (rec) => {
    const d = rec.detail || {};
    const name = d.name || '?';
    const input = tryParseJson(d.input);
    const renderer = TOOL_INPUT_RENDERERS[name];
    const body = renderer && typeof input === 'object'
      ? renderer(input)
      : `<pre class="code-block">${escapeHtml(typeof input === 'string' ? input : JSON.stringify(input, null, 2))}</pre>`;
    const idSuffix = d.id
      ? `<span class="muted" style="font-size:10px;margin-left:10px">${escapeHtml(d.id)}</span>`
      : '';
    return `<div class="tool-name-bar"><code>${escapeHtml(name)}</code>${idSuffix}</div>` + body;
  },

  user_message: (rec) => {
    const content = rec.detail.content;
    const parsed = tryParseJson(content);
    // Content is typically an array of {type:'tool_result', content: '...'} blocks.
    if (Array.isArray(parsed)) {
      return parsed.map((b) => {
        const text = typeof b === 'string' ? b : (b.content || JSON.stringify(b, null, 2));
        return `<pre class="code-block">${escapeHtml(text)}</pre>`;
      }).join('');
    }
    return `<pre class="code-block">${escapeHtml(typeof parsed === 'string' ? parsed : JSON.stringify(parsed, null, 2))}</pre>`;
  },

  result: (rec) => {
    const d = rec.detail || {};
    const usage = d.usage || {};
    return kvTable([
      ['duration', fmtDuration(d.duration_ms)],
      ['cost', fmtUsd(d.total_cost_usd || d.cost_usd)],
      ['turns', d.num_turns],
      ['session', d.session_id ? `<code>${escapeHtml(d.session_id)}</code>` : '—'],
      ['is_error', d.is_error === true ? '<span style="color:var(--err)">yes</span>' : 'no'],
      ['result', d.result ? `<span>${escapeHtml(String(d.result))}</span>` : '—'],
    ]) +
    (Object.keys(usage).length
      ? '<h2>usage</h2>' + jsonPre(usage)
      : '');
  },

  wake_start: (rec) => {
    const d = rec.detail;
    return kvTable([
      ['model', `<code>${escapeHtml(d.model || '—')}</code>`],
      ['max_seconds', d.max_seconds === 0 ? 'unbounded' : d.max_seconds],
      ['cwd', `<code>${escapeHtml(d.cwd || '')}</code>`],
      ['prompt chars', d.prompt_chars],
      ['tools', chipsHtml(d.tools, 'chip tool')],
    ]);
  },
  wake_end: () => '<div class="muted">wake completed normally</div>',
  timeout: (rec) => `<div class="callout err">wake hit timeout</div>` + kvTable([
    ['max_seconds', rec.detail.max_seconds],
  ]),
  exception: (rec) => `<div class="callout err">exception in wake</div>` + kvTable([
    ['type', rec.detail.type],
    ['message', `<code>${escapeHtml(rec.detail.message || '')}</code>`],
  ]),

  signal_turn_start: (rec) => {
    const d = rec.detail;
    return kvTable([
      ['from', escapeHtml(d.sender_name || '—') + ' (' + escapeHtml(d.sender_number || '—') + ')'],
      ['quiet', d.quiet ? 'yes (quiet hours)' : 'no'],
      ['inbound chars', d.inbound_chars],
    ]) + '<h2>inbound</h2>' + renderMarkdown(d.inbound || '');
  },
  signal_turn_end: (rec) => {
    const d = rec.detail;
    return kvTable([
      ['sender', escapeHtml(d.sender_name || '—')],
      ['outbound chars', d.outbound_chars],
      ['duration', fmtDuration(d.duration_ms)],
      ['error', d.error ? `<span style="color:var(--err)">${escapeHtml(d.error)}</span>` : '—'],
    ]) + (d.outbound ? '<h2>outbound</h2>' + renderMarkdown(d.outbound) : '');
  },
  signal_send: (rec) => kvTable([
    ['recipient', escapeHtml(rec.detail.recipient || '—')],
    ['sender label', escapeHtml(rec.detail.sender_name || '—')],
    ['text length', rec.detail.text_len],
  ]),

  surface_dispatch: (rec) => {
    const d = rec.detail;
    return kvTable([
      ['surface id', `<code>${escapeHtml(d.surface_id || rec.correlation_id || '')}</code>`],
      ['chars', d.chars],
    ]) + '<h2>body</h2>' + renderMarkdown(d.body || '');
  },
  surface_turn_end: (rec) => kvTable([
    ['surface id', `<code>${escapeHtml(rec.detail.surface_id || '')}</code>`],
    ['duration', fmtDuration(rec.detail.duration_ms)],
    ['error', rec.detail.error ? `<span style="color:var(--err)">${escapeHtml(rec.detail.error)}</span>` : '—'],
  ]),

  emergency_dispatch: (rec) => {
    const d = rec.detail;
    return `<div class="callout err">EMERGENCY incoming</div>` + kvTable([
      ['emergency id', `<code>${escapeHtml(d.emergency_id || rec.correlation_id || '')}</code>`],
      ['chars', d.chars],
    ]) + '<h2>body</h2>' + renderMarkdown(d.body || '');
  },
  emergency_voiced: (rec) => {
    const d = rec.detail;
    return `<div class="callout err">voiced to user, bypassing quiet hours</div>` + kvTable([
      ['recipient', escapeHtml(d.recipient || '—')],
      ['text length', d.text_len],
    ]) + (d.text ? '<h2>voiced text</h2>' + renderMarkdown(d.text) : '');
  },
  emergency_downgraded: (rec) =>
    '<div class="callout">emergency downgraded — no evidence, Alice returned empty reply</div>' +
    kvTable([['emergency id', `<code>${escapeHtml(rec.detail.emergency_id || '')}</code>`]]),
  emergency_turn_end: (rec) => kvTable([
    ['emergency id', `<code>${escapeHtml(rec.detail.emergency_id || '')}</code>`],
    ['verdict', escapeHtml(rec.detail.verdict || '—')],
    ['duration', fmtDuration(rec.detail.duration_ms)],
  ]),

  quiet_queue_enter: (rec) => kvTable([
    ['recipient', escapeHtml(rec.detail.recipient || '—')],
    ['sender label', escapeHtml(rec.detail.sender_name || '—')],
    ['text length', rec.detail.text_len],
    ['queue size', rec.detail.queue_size],
  ]),
  quiet_queue_drain: (rec) => kvTable([
    ['count', rec.detail.count],
    ['reason', escapeHtml(rec.detail.reason || '—')],
  ]),

  config_reload: (rec) => kvTable([
    ['changed keys', chipsHtml(rec.detail.changes)],
  ]),
  daemon_start: (rec) => kvTable([
    ['model', `<code>${escapeHtml(rec.detail.model || '—')}</code>`],
    ['quiet hours', rec.detail.quiet_hours ? jsonPre(rec.detail.quiet_hours) : '—'],
  ]),
  daemon_ready: (rec) => kvTable([
    ['signal api', `<code>${escapeHtml(rec.detail.signal_api || '—')}</code>`],
  ]),
  shutdown: () => '<div class="muted">daemon shut down</div>',

  system: (rec) => kvTable([
    ['subtype', escapeHtml(rec.detail.subtype || '—')],
    ['data_keys', chipsHtml(rec.detail.data_keys)],
  ]),

  // Filesystem artifacts from inner/.
  surface_pending:   (rec) => renderArtifact(rec, 'surface', 'pending'),
  surface_resolved:  (rec) => renderArtifact(rec, 'surface', 'resolved'),
  emergency_pending: (rec) => renderArtifact(rec, 'emergency', 'pending'),
  emergency_resolved:(rec) => renderArtifact(rec, 'emergency', 'resolved'),
  note_pending:      (rec) => renderArtifact(rec, 'note', 'pending'),
  note_consumed:     (rec) => renderArtifact(rec, 'note', 'consumed'),
  thought_written:   (rec) => renderArtifact(rec, 'thought', 'written'),

  turn_log: (rec) => {
    const d = rec.detail;
    return kvTable([
      ['from', escapeHtml(d.sender_name || '—')],
      ['error', d.error ? `<span style="color:var(--err)">${escapeHtml(d.error)}</span>` : '—'],
    ]) +
    (d.inbound ? '<h2>inbound</h2>' + renderMarkdown(d.inbound) : '') +
    (d.outbound ? '<h2>outbound</h2>' + renderMarkdown(d.outbound) : '');
  },
};

function renderArtifact(rec, kind, status) {
  const d = rec.detail || {};
  const rows = [
    ['filename', `<code>${escapeHtml(d.filename || rec.correlation_id || '')}</code>`],
    ['path', d.path ? `<code>${escapeHtml(d.path)}</code>` : '—'],
    ['status', status],
  ];
  if (d.date) rows.push(['date', escapeHtml(d.date)]);
  let out = kvTable(rows);
  if (d.frontmatter && Object.keys(d.frontmatter).length) {
    out += '<h2>frontmatter</h2>' + kvTable(Object.entries(d.frontmatter).map(([k, v]) => [k, escapeHtml(String(v))]));
  }
  if (d.trailer && Object.keys(d.trailer).length) {
    out += '<h2>trailer</h2>' + kvTable(Object.entries(d.trailer).map(([k, v]) => [k, escapeHtml(String(v))]));
  }
  if (d.body) out += '<h2>body</h2>' + renderMarkdown(d.body);
  return out;
}

/* ------------------------------------------------------------------ */
/* Generic modal — used by timeline, flow graph, memory graph.          */

function ensureModal() {
  let backdrop = document.getElementById('app-modal');
  if (backdrop) return backdrop;
  backdrop = document.createElement('div');
  backdrop.id = 'app-modal';
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML = `
    <div class="modal-panel" role="dialog" aria-modal="true">
      <div class="modal-head">
        <h3 class="modal-title">—</h3>
        <button class="modal-close" aria-label="close">×</button>
      </div>
      <div class="modal-body"></div>
    </div>
  `;
  document.body.appendChild(backdrop);
  backdrop.querySelector('.modal-close').addEventListener('click', closeModal);
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });
  return backdrop;
}

function openModal({ titleHtml, bodyHtml }) {
  const backdrop = ensureModal();
  backdrop.querySelector('.modal-title').innerHTML = titleHtml;
  backdrop.querySelector('.modal-body').innerHTML = bodyHtml;
  backdrop.classList.add('open');
  document.body.style.overflow = 'hidden';
  backdrop.querySelector('.modal-body').scrollTop = 0;
}

function closeModal() {
  const backdrop = document.getElementById('app-modal');
  if (!backdrop) return;
  backdrop.classList.remove('open');
  document.body.style.overflow = '';
}

window.openModal = openModal;
window.closeModal = closeModal;

/* ------------------------------------------------------------------ */
/* High-level opener: pass a UnifiedEvent-shaped record.                */

function openEventModal(rec) {
  const hemi = rec.hemisphere || '—';
  const kind = rec.kind || '—';
  const fam = window.kindFamily(kind);
  const label = window.humanizeKind(kind);

  // Kind-family badge makes at-a-glance category obvious in the modal too.
  const titleHtml = `
    <span class="badge ${escapeHtml(hemi)}">${escapeHtml(hemi)}</span>
    <span class="kind-chip fam-${escapeHtml(fam)}">${escapeHtml(label)}</span>
    <span class="muted" style="font-size:11px;margin-left:8px">${escapeHtml(fmtTs(rec.ts))}</span>
  `;

  let cidRow = '';
  if (rec.correlation_id) {
    const id = escapeHtml(rec.correlation_id);
    let link = id;
    if (hemi === 'thinking') link = `<a href="/wakes/${encodeURIComponent(rec.correlation_id)}">${id}</a>`;
    else if (hemi === 'speaking') link = `<a href="/turns/${encodeURIComponent(rec.correlation_id)}">${id}</a>`;
    cidRow = `<div class="detail-grid" style="margin-bottom:10px"><span class="k">correlation</span><span class="v">${link}</span></div>`;
  }

  let body = '';
  const renderer = KIND_RENDERERS[kind];
  if (renderer) {
    try { body = renderer(rec); } catch (e) { console.error(e); body = '<pre>' + escapeHtml(String(e)) + '</pre>'; }
  }
  // Always include raw detail at the bottom as a collapsed escape hatch.
  body += `<details style="margin-top:18px"><summary class="muted">raw detail</summary><pre>${escapeHtml(JSON.stringify(rec.detail, null, 2))}</pre></details>`;

  openModal({
    titleHtml,
    bodyHtml: `<div class="summary-full">${escapeHtml(rec.summary || '')}</div>${cidRow}${body}`,
  });
}

window.openEventModal = openEventModal;

/* ------------------------------------------------------------------ */
/* Helpers for graph pages.                                              */

window.openInteractionNodeModal = function (node) {
  const fam = node.kind;
  const titleHtml = `<span class="kind-chip fam-${escapeHtml(fam)}">${escapeHtml(fam)}</span> ${escapeHtml(node.label || '')}`;
  const meta = node.meta || {};

  // For wake/turn nodes, the real data lives on dedicated pages — give a CTA.
  let cta = '';
  if (node.kind === 'wake' && node.id) {
    cta = `<p><a href="/wakes/${encodeURIComponent(node.id.replace('wake::', ''))}">open full wake trace →</a></p>`;
  } else if (node.kind === 'turn' && node.id) {
    cta = `<p><a href="/turns/${encodeURIComponent(node.id.replace('turn::', ''))}">open full turn trace →</a></p>`;
  }

  const rows = [
    ['node id', `<code>${escapeHtml(node.id || '')}</code>`],
    ['label', escapeHtml(node.label || '')],
    ['kind', escapeHtml(node.kind || '')],
  ];
  if (node.ts) rows.push(['ts', escapeHtml(fmtTs(node.ts))]);

  let body = cta + kvTable(rows);

  // Artifact kinds carry meta.body etc.; render rich content.
  if (meta.body) body += '<h2>body</h2>' + renderMarkdown(meta.body);
  if (meta.frontmatter && Object.keys(meta.frontmatter).length) {
    body += '<h2>frontmatter</h2>' + kvTable(Object.entries(meta.frontmatter).map(([k, v]) => [k, escapeHtml(String(v))]));
  }
  if (meta.trailer && Object.keys(meta.trailer).length) {
    body += '<h2>trailer</h2>' + kvTable(Object.entries(meta.trailer).map(([k, v]) => [k, escapeHtml(String(v))]));
  }
  if (meta.tools && meta.tools.length) {
    body += '<h2>tools used</h2>' + chipsHtml(meta.tools, 'chip tool');
  }

  body += `<details style="margin-top:18px"><summary class="muted">raw meta</summary><pre>${escapeHtml(JSON.stringify(meta, null, 2))}</pre></details>`;

  openModal({ titleHtml, bodyHtml: body });
};

window.openMemoryNodeModal = async function (node) {
  const titleHtml = `<span class="kind-chip fam-artifact">memory</span> ${escapeHtml(node.label || '')}`;
  openModal({ titleHtml, bodyHtml: '<div class="muted">loading…</div>' });

  try {
    const res = await fetch('/api/memory/note?id=' + encodeURIComponent(node.id));
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      document.querySelector('#app-modal .modal-body').innerHTML =
        '<div class="callout err">' + escapeHtml(err.error || 'not found') + '</div>' +
        kvTable([['id', `<code>${escapeHtml(node.id)}</code>`]]);
      return;
    }
    const data = await res.json();
    if (data.unresolved) {
      document.querySelector('#app-modal .modal-body').innerHTML =
        '<div class="callout">unresolved wikilink — no matching note file yet</div>' +
        kvTable([['referenced as', escapeHtml(data.label || '')]]);
      return;
    }
    const rows = [
      ['path', `<code>${escapeHtml(data.rel_path || '')}</code>`],
      ['size', (data.size || 0) + ' bytes'],
      ['in-degree', node.in_degree || 0],
    ];
    const html = kvTable(rows) + '<h2>content</h2>' + renderMarkdown(data.body);
    document.querySelector('#app-modal .modal-body').innerHTML = html;
  } catch (e) {
    console.error(e);
    document.querySelector('#app-modal .modal-body').innerHTML =
      '<div class="callout err">failed to load note: ' + escapeHtml(e.message) + '</div>';
  }
};

// Hover-focus for d3 force graphs: keep hovered node colored, desaturate
// 1-hop neighbors slightly, 2-hop more, fade everything else. Treats edges
// as undirected for BFS — the goal is investigating structure, not flow
// direction. Uses a `.focus` event namespace so existing tooltip/click
// handlers on the same selection still fire.
window.attachGraphFocus = function ({ nodeSel, linkSel, edges, idAccessor }) {
  const id = idAccessor || (n => n.id);
  const endId = (e, side) => {
    const v = e[side];
    return (v && typeof v === 'object') ? id(v) : v;
  };

  const adj = new Map();
  const ensure = k => {
    let s = adj.get(k);
    if (!s) { s = new Set(); adj.set(k, s); }
    return s;
  };
  edges.forEach(e => {
    const s = endId(e, 'source');
    const t = endId(e, 'target');
    ensure(s).add(t);
    ensure(t).add(s);
  });

  const bfs = (startId) => {
    const dist = new Map([[startId, 0]]);
    const queue = [startId];
    while (queue.length) {
      const cur = queue.shift();
      const d = dist.get(cur);
      const neighbors = adj.get(cur);
      if (!neighbors) continue;
      neighbors.forEach(n => {
        if (!dist.has(n)) {
          dist.set(n, d + 1);
          queue.push(n);
        }
      });
    }
    return dist;
  };

  // distance 0: hovered. 1: direct neighbor. 2: neighbor's neighbor.
  // >=3 or unreachable: fully faded.
  const styleFor = (d) => {
    if (d === 0)   return { gray: 0,    opacity: 1.0  };
    if (d === 1)   return { gray: 0.55, opacity: 0.85 };
    if (d === 2)   return { gray: 0.85, opacity: 0.5  };
    return            { gray: 1.0,  opacity: 0.15 };
  };

  const apply = (startId) => {
    const dist = bfs(startId);
    nodeSel
      .style('filter', n => {
        const d = dist.has(id(n)) ? dist.get(id(n)) : Infinity;
        return `grayscale(${styleFor(d).gray})`;
      })
      .style('opacity', n => {
        const d = dist.has(id(n)) ? dist.get(id(n)) : Infinity;
        return styleFor(d).opacity;
      });
    if (linkSel) {
      linkSel.style('stroke-opacity', e => {
        const ds = dist.has(endId(e, 'source')) ? dist.get(endId(e, 'source')) : Infinity;
        const dt = dist.has(endId(e, 'target')) ? dist.get(endId(e, 'target')) : Infinity;
        const m = Math.min(ds, dt);
        if (m === 0) return 0.95;
        if (m === 1) return 0.45;
        return 0.04;
      });
    }
  };

  const clear = () => {
    nodeSel.style('filter', null).style('opacity', null);
    if (linkSel) linkSel.style('stroke-opacity', null);
  };

  nodeSel
    .on('mouseenter.focus', (e, d) => apply(id(d)))
    .on('mouseleave.focus', clear);
};
