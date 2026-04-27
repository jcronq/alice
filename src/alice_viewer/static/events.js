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
  cli_turn_start: 'cli in',
  cli_turn_end: 'cli done',
  cli_send: 'cli sent',
  discord_turn_start: 'discord in',
  discord_turn_end: 'discord done',
  discord_send: 'discord sent',
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
  signal_turn_start: 'turn', signal_turn_end: 'turn', signal_send: 'turn',
  cli_turn_start: 'turn', cli_turn_end: 'turn', cli_send: 'turn',
  discord_turn_start: 'turn', discord_turn_end: 'turn', discord_send: 'turn',
  turn_log: 'turn',
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

function sendEventDetail(rec) {
  const d = rec.detail;
  return kvTable([
    ['recipient', escapeHtml(d.recipient || '—')],
    ['sender label', escapeHtml(d.sender_name || '—')],
    ['text length', d.text_len],
    ['chunks', d.chunk_count],
    ['attachments', d.attachment_count],
    ['emergency', d.emergency ? 'yes' : ''],
    ['bypassed quiet hours', d.bypassed_quiet ? 'yes' : ''],
  ]);
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
  signal_send: (rec) => sendEventDetail(rec),
  cli_send: (rec) => sendEventDetail(rec),
  discord_send: (rec) => sendEventDetail(rec),

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
/* Run modal — fetches /api/runs/{id} and renders the trace.            */
/* Used by the timeline row click handler and by the flow graph for    */
/* wake/turn nodes.                                                      */

async function openRunModal(runId) {
  openModal({
    titleHtml: '<span class="muted">loading…</span>',
    bodyHtml: '<div class="trace-empty">fetching trace…</div>',
  });
  try {
    const res = await fetch('/api/runs/' + encodeURIComponent(runId));
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      _renderRunError(err.error || 'not found');
      return;
    }
    const data = await res.json();
    _renderRun(data.run, data.events);
  } catch (err) {
    _renderRunError(String(err));
  }
}

function _renderRunError(msg) {
  document.querySelector('#app-modal .modal-title').innerHTML =
    '<span style="color:var(--err)">error</span>';
  document.querySelector('#app-modal .modal-body').innerHTML =
    '<div class="trace-empty">' + escapeHtml(msg) + '</div>';
}

function _renderRun(run, events) {
  const fam = run.kind === 'thinking-wake' ? 'tool'
            : run.kind === 'emergency-turn' ? 'emergency'
            : run.kind === 'surface-turn' ? 'artifact' : 'turn';
  const startStr = fmtTs(run.start_ts);
  const endStr = run.end_ts ? fmtTs(run.end_ts) : 'still running';
  const durStr = run.duration_ms != null ? (run.duration_ms / 1000).toFixed(1) + 's' : '—';
  const costStr = run.cost_usd != null ? '$' + Number(run.cost_usd).toFixed(4) : '—';

  document.querySelector('#app-modal .modal-title').innerHTML =
    '<span class="badge ' + escapeHtml(run.hemisphere) + '">' + escapeHtml(run.hemisphere) + '</span> ' +
    '<span class="kind-chip fam-' + fam + '">' + escapeHtml(run.kind) + '</span>';

  const detailRows = [
    ['run id', '<code>' + escapeHtml(run.run_id) + '</code>'],
    ['started', escapeHtml(startStr)],
    ['ended', escapeHtml(endStr)],
    ['duration', escapeHtml(durStr)],
    ['cost', escapeHtml(costStr)],
  ];
  if (run.model) detailRows.push(['model', '<code>' + escapeHtml(run.model) + '</code>']);
  if (run.sender_name) detailRows.push(['sender', escapeHtml(run.sender_name)]);
  if (run.tools && run.tools.length) {
    detailRows.push(['tools', run.tools.map(t => '<span class="chip tool">' + escapeHtml(t) + '</span>').join(' ')]);
  }
  if (run.error) {
    detailRows.push(['error', '<span style="color:var(--err)">' + escapeHtml(run.error) + '</span>']);
  }
  const detailHtml = '<div class="detail-grid">' +
    detailRows.map(([k, v]) => '<span class="k">' + k + '</span><span class="v">' + v + '</span>').join('') +
    '</div>';

  const summaryHtml = run.summary
    ? '<div class="summary-full">' + escapeHtml(run.summary) + '</div>'
    : '';

  const ctaHtml = '<p style="margin-top:14px"><a href="' + run.detail_url +
    '">open full detail page →</a></p>';

  const traceHtml = events && events.length
    ? '<h2>trace · ' + events.length + ' events</h2><div class="trace">' +
      events.map(_renderTraceRow).join('') + '</div>'
    : '<div class="trace-empty">No events captured for this run.</div>';

  document.querySelector('#app-modal .modal-body').innerHTML =
    summaryHtml + detailHtml + ctaHtml + traceHtml;
}

function _renderTraceRow(ev) {
  const fam = window.kindFamily(ev.kind);
  const label = window.humanizeKind(ev.kind);
  const ts = fmtTs(ev.ts).replace(/^.* /, '');
  const detail = ev.detail || {};

  const isRich =
    ev.kind === 'thinking' ||
    ev.kind === 'assistant_text' ||
    ev.kind === 'tool_use' ||
    ev.kind === 'system';

  if (!isRich) {
    return '<div class="trace-row compact fam-' + fam + '">' +
           '<span class="ts">' + escapeHtml(ts) + '</span>' +
           '<span class="kind">' + escapeHtml(label) + '</span>' +
           '<span class="summary">' + escapeHtml(ev.summary || '') + '</span>' +
           '</div>';
  }

  let body;
  if (ev.kind === 'thinking' || ev.kind === 'assistant_text') {
    const text = (detail.text || '').trim();
    body = text
      ? '<div class="trace-text">' + escapeHtml(text) + '</div>'
      : '<div class="trace-text muted">(empty)</div>';
  } else if (ev.kind === 'tool_use') {
    body = _renderTraceToolUse(detail);
  } else {
    body = _renderTraceSystem(detail);
  }

  const head = '<div class="trace-head">' +
               '<span class="ts">' + escapeHtml(ts) + '</span>' +
               '<span class="kind">' + escapeHtml(label) + '</span>' +
               (ev.kind === 'system' && detail.subtype
                 ? ' <span class="muted">· ' + escapeHtml(detail.subtype) + '</span>'
                 : '') +
               '</div>';
  return '<div class="trace-row rich fam-' + fam + '">' + head + body + '</div>';
}

function _renderTraceToolUse(detail) {
  const name = detail.name || '?';
  let input = detail.input;
  if (typeof input === 'string') {
    try { input = JSON.parse(input); }
    catch (e) {
      return '<div class="trace-text"><strong>' + escapeHtml(name) + '</strong>\n' +
             escapeHtml(String(input)) + '</div>';
    }
  }
  if (!input || typeof input !== 'object') {
    return '<div class="trace-text"><strong>' + escapeHtml(name) + '</strong></div>';
  }

  let primary = null;
  let primaryLabel = null;
  const secondaries = [];

  if (name === 'Bash') {
    primary = input.command;
    primaryLabel = 'command';
    if (input.description) secondaries.push(['', input.description]);
    if (input.timeout) secondaries.push(['timeout', input.timeout + 'ms']);
  } else if (['Read', 'Write', 'Edit', 'NotebookEdit'].includes(name)) {
    primary = input.file_path || input.notebook_path;
    primaryLabel = 'file';
    if (input.offset != null) secondaries.push(['offset', input.offset]);
    if (input.limit != null) secondaries.push(['limit', input.limit]);
    if (name === 'Edit') {
      if (input.old_string) secondaries.push(['old', _truncate(input.old_string, 200)]);
      if (input.new_string) secondaries.push(['new', _truncate(input.new_string, 200)]);
    }
    if (name === 'Write' && input.content) {
      secondaries.push(['content', _truncate(input.content, 400)]);
    }
  } else if (name === 'Grep') {
    primary = input.pattern;
    primaryLabel = 'pattern';
    if (input.path) secondaries.push(['path', input.path]);
    if (input.glob) secondaries.push(['glob', input.glob]);
    if (input.output_mode) secondaries.push(['mode', input.output_mode]);
  } else if (name === 'Glob') {
    primary = input.pattern;
    primaryLabel = 'pattern';
    if (input.path) secondaries.push(['path', input.path]);
  } else if (name === 'WebFetch') {
    primary = input.url;
    primaryLabel = 'url';
    if (input.prompt) secondaries.push(['prompt', _truncate(input.prompt, 300)]);
  } else if (name === 'WebSearch') {
    primary = input.query;
    primaryLabel = 'query';
  } else if (name.endsWith('__send_message') || name === 'send_message') {
    primary = input.message;
    primaryLabel = 'message';
    if (input.recipient) secondaries.push(['→', input.recipient]);
  } else if (name.endsWith('__append_note') || name === 'append_note') {
    primary = input.content || input.body || input.text;
    primaryLabel = 'note';
    if (input.tag) secondaries.push(['tag', input.tag]);
    if (input.title) secondaries.push(['title', input.title]);
  } else if (name.endsWith('__resolve_surface') || name === 'resolve_surface') {
    primary = input.action_taken;
    primaryLabel = 'action';
    if (input.id) secondaries.push(['surface', input.id]);
    if (input.verdict) secondaries.push(['verdict', input.verdict]);
  } else if (name.endsWith('__write_memory') || name === 'write_memory') {
    primary = input.content;
    primaryLabel = 'memory';
    if (input.path) secondaries.push(['path', input.path]);
  } else if (name.endsWith('__read_memory') || name === 'read_memory') {
    primary = input.pattern;
    primaryLabel = 'pattern';
  } else if (name.startsWith('mcp__')) {
    for (const [k, v] of Object.entries(input)) {
      if (typeof v === 'string' && v) {
        primary = v;
        primaryLabel = k;
        break;
      }
    }
    for (const [k, v] of Object.entries(input)) {
      if (k !== primaryLabel) {
        secondaries.push([k, typeof v === 'string' ? v : JSON.stringify(v)]);
      }
    }
  }

  if (primary == null) {
    return '<div class="trace-text"><strong>' + escapeHtml(name) + '</strong>\n' +
           escapeHtml(JSON.stringify(input, null, 2)) + '</div>';
  }

  const header = '<div class="trace-tool-header">' +
                 '<code>' + escapeHtml(name) + '</code>' +
                 (primaryLabel ? ' <span class="muted">' + escapeHtml(primaryLabel) + ':</span>' : '') +
                 '</div>';
  const primaryBlock = '<div class="trace-text">' + escapeHtml(String(primary)) + '</div>';
  const secondaryHtml = secondaries.length
    ? '<div class="trace-tool-secondary">' +
      secondaries.map(([k, v]) =>
        (k ? '<span class="muted">' + escapeHtml(k) + ':</span> ' : '') +
        escapeHtml(String(v))
      ).join(' · ') +
      '</div>'
    : '';
  return header + primaryBlock + secondaryHtml;
}

function _truncate(s, cap) {
  s = String(s || '');
  return s.length > cap ? s.substring(0, cap - 1) + '…' : s;
}

function _renderTraceSystem(detail) {
  const data = detail.data || {};
  const keys = Object.keys(data);
  if (!keys.length) {
    return '<div class="trace-text muted">(no data)</div>';
  }
  const rows = keys.map(k => {
    let v = data[k];
    if (Array.isArray(v)) {
      v = v.length === 0 ? '(none)' :
          v.length <= 8 ? v.join(', ') : v.slice(0, 8).join(', ') + ` …+${v.length - 8}`;
    } else if (v && typeof v === 'object') {
      v = JSON.stringify(v);
    }
    return '<span class="k">' + escapeHtml(k) + '</span>' +
           '<span class="v">' + escapeHtml(String(v == null ? '' : v)) + '</span>';
  }).join('');
  return '<div class="detail-grid trace-system-grid">' + rows + '</div>';
}

window.openRunModal = openRunModal;

/* ------------------------------------------------------------------ */
/* Helpers for graph pages.                                              */

window.openInteractionNodeModal = function (node) {
  // Wake/turn nodes get the same trace modal as the timeline rows.
  if (node.kind === 'wake' && node.id) {
    return openRunModal(node.id.replace('wake::', ''));
  }
  if (node.kind === 'turn' && node.id) {
    return openRunModal(node.id.replace('turn::', ''));
  }

  const fam = node.kind;
  const titleHtml = `<span class="kind-chip fam-${escapeHtml(fam)}">${escapeHtml(fam)}</span> ${escapeHtml(node.label || '')}`;
  const meta = node.meta || {};

  const rows = [
    ['node id', `<code>${escapeHtml(node.id || '')}</code>`],
    ['label', escapeHtml(node.label || '')],
    ['kind', escapeHtml(node.kind || '')],
  ];
  if (node.ts) rows.push(['ts', escapeHtml(fmtTs(node.ts))]);

  let body = kvTable(rows);

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

// Hover-focus for d3 force graphs: keep hovered node colored, fade 1-hop
// neighbors toward gray, fade 2-hop more, fade everything else nearly to
// background. Treats edges as undirected for BFS — the goal is to read
// structure, not flow direction. Uses the `.focus` event namespace so
// existing tooltip/click handlers on the same selection still fire.
//
// Implementation notes: we interpolate the *fill* of each <circle> toward
// a dark gray rather than using CSS `filter: grayscale()` — the latter is
// unreliable on SVG <g> across browsers and was producing no visible
// effect. Original fills are cached on the DOM node so we can restore
// them on mouseleave without re-deriving from the datum.
window.attachGraphFocus = function ({ nodeSel, linkSel, edges, idAccessor }) {
  const id = idAccessor || (n => n.id);
  const endId = (e, side) => {
    const v = e[side];
    return (v && typeof v === 'object') ? id(v) : v;
  };

  const adj = new Map();
  edges.forEach(e => {
    const s = endId(e, 'source');
    const t = endId(e, 'target');
    if (!adj.has(s)) adj.set(s, new Set());
    if (!adj.has(t)) adj.set(t, new Set());
    adj.get(s).add(t);
    adj.get(t).add(s);
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

  // Cache per-circle base fill so we can interpolate from it and restore
  // it cleanly on mouseleave.
  const circleSel = nodeSel.select('circle');
  circleSel.each(function () {
    const c = d3.select(this);
    if (c.property('__origFill') == null) {
      c.property('__origFill', c.attr('fill'));
    }
  });

  // distance 0: hovered. 1: direct neighbor. 2: neighbor's neighbor.
  // >=3 or unreachable: faded almost to background.
  //
  // Sharp cliffs > smooth gradient: at-a-glance "is this connected?" reads
  // better when 1-hop stays nearly full color and 2-hop drops hard. The
  // hovered node gets a white stroke halo so the focal point is unambiguous.
  const FADE = '#2a3140';
  const HALO = '#ffffff';
  const styleFor = (d) => {
    if (d === 0)   return { mix: 0.0,  opacity: 1.0  };
    if (d === 1)   return { mix: 0.15, opacity: 1.0  };
    if (d === 2)   return { mix: 0.75, opacity: 0.4  };
    return            { mix: 1.0,  opacity: 0.07 };
  };

  const apply = (startId) => {
    const dist = bfs(startId);
    nodeSel.style('opacity', n => {
      const d = dist.has(id(n)) ? dist.get(id(n)) : Infinity;
      return styleFor(d).opacity;
    });
    circleSel
      .style('fill', function (n) {
        const d = dist.has(id(n)) ? dist.get(id(n)) : Infinity;
        const s = styleFor(d);
        if (s.mix === 0) return null;
        const orig = d3.select(this).property('__origFill') || '#79849a';
        return d3.interpolateRgb(orig, FADE)(s.mix);
      })
      .style('stroke', n => (dist.get(id(n)) === 0 ? HALO : null))
      .style('stroke-width', n => (dist.get(id(n)) === 0 ? 3 : null));
    if (linkSel) {
      linkSel
        .style('stroke', e => {
          const ds = dist.has(endId(e, 'source')) ? dist.get(endId(e, 'source')) : Infinity;
          const dt = dist.has(endId(e, 'target')) ? dist.get(endId(e, 'target')) : Infinity;
          return Math.min(ds, dt) === 0 ? HALO : null;
        })
        .style('stroke-width', e => {
          const ds = dist.has(endId(e, 'source')) ? dist.get(endId(e, 'source')) : Infinity;
          const dt = dist.has(endId(e, 'target')) ? dist.get(endId(e, 'target')) : Infinity;
          return Math.min(ds, dt) === 0 ? 2 : null;
        })
        .style('stroke-opacity', e => {
          const ds = dist.has(endId(e, 'source')) ? dist.get(endId(e, 'source')) : Infinity;
          const dt = dist.has(endId(e, 'target')) ? dist.get(endId(e, 'target')) : Infinity;
          const m = Math.min(ds, dt);
          if (m === 0) return 1.0;
          if (m === 1) return 0.4;
          return 0.03;
        });
    }
  };

  const clear = () => {
    nodeSel.style('opacity', null);
    circleSel
      .style('fill', null)
      .style('stroke', null)
      .style('stroke-width', null);
    if (linkSel) {
      linkSel
        .style('stroke', null)
        .style('stroke-width', null)
        .style('stroke-opacity', null);
    }
  };

  nodeSel
    .on('mouseenter.focus', (e, d) => apply(id(d)))
    .on('mouseleave.focus', clear);
};

// Tuning panel for d3 force graphs — Obsidian-style sliders for the layout
// + visual knobs. Values persist per-`key` in localStorage so they survive
// re-renders (memory graph's ghost toggle) and page reloads.
//
// Caller passes live d3 selections + the simulation. Re-call after a
// re-render with the fresh references — the panel itself is preserved
// (we replace the DOM but read existing values back out of localStorage).
window.attachGraphTuning = function ({ key, container, simulation, linkSel, nodeSel, circleSel, defaults }) {
  if (!container || !simulation) return;

  // Inject panel CSS once per page.
  if (!document.getElementById('graph-tuning-style')) {
    const s = document.createElement('style');
    s.id = 'graph-tuning-style';
    s.textContent = `
      .graph-tuning {
        position: absolute; top: 14px; right: 14px; z-index: 5;
        background: var(--panel-2); border: 1px solid var(--border);
        border-radius: 6px; padding: 6px 10px; font-size: 11px;
        min-width: 220px; color: var(--text);
      }
      .graph-tuning > summary {
        cursor: pointer; user-select: none; color: var(--muted);
        list-style: none;
      }
      .graph-tuning > summary::-webkit-details-marker { display: none; }
      .graph-tuning[open] > summary { color: var(--text); margin-bottom: 6px; }
      .graph-tuning .row { display: block; margin-top: 8px; }
      .graph-tuning .row .lbl { color: var(--text); }
      .graph-tuning .row .v {
        color: var(--muted); float: right; font-family: var(--mono);
        font-size: 10px;
      }
      .graph-tuning .row input[type="range"] {
        width: 100%; margin: 2px 0 0; display: block;
      }
      .graph-tuning .reset {
        margin-top: 10px; background: transparent; border: 1px solid var(--border);
        color: var(--muted); padding: 3px 8px; border-radius: 4px;
        font-size: 10px; cursor: pointer;
      }
      .graph-tuning .reset:hover { color: var(--text); border-color: var(--text); }
    `;
    document.head.appendChild(s);
  }

  // Cache each circle's pre-scaling radius so the size multiplier is
  // composable with the per-graph radius logic (kind-based, in-degree, etc.).
  circleSel.each(function () {
    const c = d3.select(this);
    if (c.property('__baseR') == null) c.property('__baseR', +c.attr('r') || 6);
  });

  // Cache base link width for the multiplier.
  linkSel.each(function () {
    const l = d3.select(this);
    if (l.property('__baseW') == null) {
      l.property('__baseW', +l.attr('stroke-width') || 1);
    }
  });

  const storageKey = 'graph-tuning:' + key;
  const baseDefaults = {
    linkDistance: 80,
    linkStrength: 0.4,
    chargeStrength: -220,
    collideRadius: 18,
    nodeRadius: 1,
    linkWidth: 1,
  };
  const merged = { ...baseDefaults, ...(defaults || {}) };
  let values = { ...merged };
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
    if (saved && typeof saved === 'object') values = { ...values, ...saved };
  } catch (_) { /* ignore */ }

  // Spec for the sliders. step=0 means integer-format display.
  const knobs = [
    { name: 'linkDistance',   label: 'link distance',  min: 10,    max: 400, step: 1,    fmt: v => v.toFixed(0) },
    { name: 'linkStrength',   label: 'link strength',  min: 0,     max: 1,   step: 0.01, fmt: v => v.toFixed(2) },
    { name: 'chargeStrength', label: 'repel force',    min: -2000, max: 0,   step: 10,   fmt: v => v.toFixed(0) },
    { name: 'collideRadius',  label: 'collide radius', min: 0,     max: 60,  step: 1,    fmt: v => v.toFixed(0) },
    { name: 'nodeRadius',     label: 'node size ×',    min: 0.3,   max: 3,   step: 0.1,  fmt: v => v.toFixed(1) },
    { name: 'linkWidth',      label: 'link width ×',   min: 0.3,   max: 4,   step: 0.1,  fmt: v => v.toFixed(1) },
  ];

  // If a panel already exists in this container (re-render path), keep
  // it open/closed state and replace the body; otherwise build fresh.
  let panel = container.querySelector('.graph-tuning');
  const wasOpen = panel ? panel.open : false;
  if (panel) panel.remove();
  panel = document.createElement('details');
  panel.className = 'graph-tuning';
  panel.open = wasOpen;
  panel.innerHTML =
    '<summary>⚙ tuning</summary>' +
    knobs.map(k => `
      <label class="row">
        <span class="lbl">${k.label}</span>
        <span class="v" data-for="${k.name}">${k.fmt(values[k.name])}</span>
        <input type="range" data-tune="${k.name}"
               min="${k.min}" max="${k.max}" step="${k.step}"
               value="${values[k.name]}" />
      </label>
    `).join('') +
    '<button type="button" class="reset">reset to defaults</button>';
  container.appendChild(panel);

  const apply = () => {
    const linkF = simulation.force('link');
    if (linkF) linkF.distance(values.linkDistance).strength(values.linkStrength);
    const chargeF = simulation.force('charge');
    if (chargeF) chargeF.strength(values.chargeStrength);
    const collideF = simulation.force('collide');
    if (collideF) collideF.radius(values.collideRadius);
    circleSel.attr('r', function () {
      const base = d3.select(this).property('__baseR') || 6;
      return base * values.nodeRadius;
    });
    linkSel.attr('stroke-width', function () {
      const base = d3.select(this).property('__baseW') || 1;
      return base * values.linkWidth;
    });
    simulation.alpha(0.4).restart();
  };

  const persist = () => {
    try { localStorage.setItem(storageKey, JSON.stringify(values)); }
    catch (_) { /* quota / private mode — non-fatal */ }
  };

  panel.querySelectorAll('input[type="range"]').forEach(input => {
    const name = input.dataset.tune;
    const display = panel.querySelector(`.v[data-for="${name}"]`);
    const knob = knobs.find(k => k.name === name);
    input.addEventListener('input', () => {
      values[name] = +input.value;
      display.textContent = knob.fmt(values[name]);
      apply();
    });
    input.addEventListener('change', persist);
  });

  panel.querySelector('.reset').addEventListener('click', () => {
    values = { ...merged };
    panel.querySelectorAll('input[type="range"]').forEach(input => {
      const name = input.dataset.tune;
      input.value = values[name];
      const display = panel.querySelector(`.v[data-for="${name}"]`);
      const knob = knobs.find(k => k.name === name);
      display.textContent = knob.fmt(values[name]);
    });
    apply();
    try { localStorage.removeItem(storageKey); } catch (_) {}
  });

  // Apply once on attach so persisted values take effect immediately.
  apply();
};
