// Pure helpers for the chat composer (issue #655). No imports and no DOM
// access at module load, so node can import this file directly in tests.

export const MAX_FILES = 5;
export const MAX_PER_FILE = 10 * 1024 * 1024;
export const ACCEPT = "image/*,application/pdf,text/plain";
export const STICK_THRESHOLD_PX = 40;

const IMAGE_EXT = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/gif": "gif",
  "image/webp": "webp",
  "image/bmp": "bmp",
  "image/svg+xml": "svg",
};

export const LOAD_OLDER_THRESHOLD_PX = 200;
// Page size for ``/api/chat?before=``; matches the server's SSE snapshot tail.
export const CHAT_PAGE_SIZE = 50;

// True when the scroll container is within ``threshold`` px of its top.
export function isNearTop(el, threshold = LOAD_OLDER_THRESHOLD_PX) {
  return el.scrollTop <= threshold;
}

// Stable identity for a server chat row, so overlapping snapshots and
// older pages fold into one list. Working-groups are keyed by turn.
export function chatRowKey(m) {
  if (m.variant === "working-group") return "wg:" + m.turn_id;
  return (m.from || "") + "|" + (m.at || "") + "|" + (m.text || "");
}

// Display text for a message/group timestamp: "3:44 PM" for today, with a
// "Nov 5" date prefix once the message is from a prior day. Returns "" for
// an unparsable/missing ``at`` so callers can skip rendering the <time>.
export function formatMessageTime(at, now = new Date()) {
  const t = Date.parse(at || "");
  if (Number.isNaN(t)) return "";
  const d = new Date(t);
  const time = d.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
  if (d.toDateString() === now.toDateString()) return time;
  return d.toLocaleDateString([], {month: "short", day: "numeric"}) + ", " + time;
}

// ``at`` of the earliest real (non working-group) message with a parsable
// timestamp, or null — the cursor for the next ``?before=`` fetch.
export function earliestAt(rows) {
  let best = null;
  let bestT = Infinity;
  for (const m of rows) {
    if (m.variant === "working-group") continue;
    const t = Date.parse(m.at || "");
    if (!Number.isNaN(t) && t < bestT) { bestT = t; best = m.at; }
  }
  return best;
}

// True when the scroll container is within ``threshold`` px of its bottom.
export function isNearBottom(el, threshold = STICK_THRESHOLD_PX) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
}

// Enter sends; Shift+Enter (newline) and IME composition fall through.
export function shouldSubmitOnKey(e) {
  if (e.key !== "Enter" || e.shiftKey) return false;
  if (e.isComposing || e.keyCode === 229) return false;
  return true;
}

// Image files carried by a ClipboardEvent's ``clipboardData``.
export function imageFilesFromClipboard(clipboardData) {
  const out = [];
  if (!clipboardData) return out;
  for (const item of Array.from(clipboardData.items || [])) {
    if (item.kind === "file" && String(item.type).startsWith("image/")) {
      const f = item.getAsFile();
      if (f) out.push(f);
    }
  }
  return out;
}

// Clipboard screenshots arrive as a generic "image.png"; give each a
// distinct name with an extension the server's MIME allowlist accepts.
export function attachmentName(file, index = 0, now = new Date()) {
  const type = String(file.type || "");
  const raw = String(file.name || "");
  const generic = !raw || /^image\.[a-z0-9]+$/i.test(raw);
  if (!generic) return raw;
  const ext = IMAGE_EXT[type] || (type.startsWith("image/") ? "png" : "bin");
  const stamp = now.toISOString().replace(/[-:]/g, "").replace(/\..*$/, "");
  return "pasted-" + stamp + (index ? "-" + index : "") + "." + ext;
}

// Returns an error string, or "" when ``file`` may be added to ``current``.
export function validateAttachment(file, currentCount) {
  if (currentCount >= MAX_FILES) return "At most " + MAX_FILES + " attachments.";
  if (file.size > MAX_PER_FILE) {
    return file.name + " is over " + MAX_PER_FILE / (1024 * 1024) + " MB.";
  }
  return "";
}

// base64 payload (no data: prefix) for the /api/chat ``files`` shape.
export function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] || "");
    r.onerror = () => reject(r.error || new Error("read failed"));
    r.readAsDataURL(file);
  });
}

// ── message actions: permalinks, reactions, replies (issue #668) ──────

export const QUICK_REACTS = ["👍", "❤️", "😂", "🎉", "👀"];
export const MORE_REACTS = ["🙏", "🔥", "✅", "❌", "🤔", "😮", "😢", "💯"];
export const HIGHLIGHT_MS = 1500;
export const REPLY_PREVIEW_CHARS = 80;
export const REPLY_QUOTE_CHARS = 300;

// DOM id / hash fragment for a message: ``msg-<at>``.
export function messageAnchorId(at) {
  return "msg-" + at;
}

// ``#msg-<at>`` → ``<at>`` (percent-decoded), or "" when not a message hash.
export function parseMessageHash(hash) {
  const m = /^#?msg-(.+)$/.exec(hash || "");
  if (!m) return "";
  try { return decodeURIComponent(m[1]); } catch (_) { return m[1]; }
}

export function permalinkFor(origin, at) {
  return origin + "/?p=chat#" + messageAnchorId(at);
}

// One-line plain-text preview for "In reply to: …" headers.
export function replyPreview(msg, limit = REPLY_PREVIEW_CHARS) {
  const text = String((msg && msg.text) || "").split(/\s+/).filter(Boolean).join(" ");
  if (!text) {
    return msg && ((msg.attachments && msg.attachments.length) || (msg.files && msg.files.length))
      ? "📎 attachment" : "";
  }
  return text.length > limit ? text.slice(0, limit - 1).trimEnd() + "…" : text;
}

// ``> ``-quoted text block (text only) that pre-fills the composer on Reply.
export function quoteForReply(msg, limit = REPLY_QUOTE_CHARS) {
  let text = String((msg && msg.text) || "").trim();
  if (!text) text = replyPreview(msg);
  if (text.length > limit) text = text.slice(0, limit - 1).trimEnd() + "…";
  return text.split("\n").map(l => ("> " + l).trimEnd()).join("\n") + "\n\n";
}

// ``{emoji: [{from, at}]}`` → ``[{emoji, count, mine}]`` in first-seen order.
export function summarizeReactions(reactions, me = "jason") {
  const out = [];
  for (const [emoji, users] of Object.entries(reactions || {})) {
    if (!Array.isArray(users) || !users.length) continue;
    out.push({emoji, count: users.length, mine: users.some(u => u && u.from === me)});
  }
  return out;
}

// Stable signature of a reactions map, for comparing local vs server state.
export function reactionsSignature(reactions) {
  return JSON.stringify(
    Object.keys(reactions || {}).sort()
      .map(e => [e, (reactions[e] || []).map(u => u.from).sort()])
      .filter(([, users]) => users.length),
  );
}

// Safe mini-markdown for chat bodies (issue #734): HTML-escapes first, then
// applies code spans, **bold**, "- " bullets and linkifies http(s) URLs,
// ``?p=`` paths and ``#NNN`` issue refs. Returns an HTML string.
export function chatTextToHtml(text, repo = "jcronq/alice") {
  const esc = s => s.replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const inline = seg => esc(seg)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/https?:\/\/[^\s<]+[^\s<.,;:!?)\]]/g,
      u => '<a href="' + u + '" target="_blank" rel="noopener">' + u + "</a>")
    .replace(/(^|[\s(])(\?p=[\w.\-]+)/g, '$1<a href="$2">$2</a>')
    .replace(/(^|[\s(])#(\d+)\b/g,
      (_, p, n) => p + '<a href="https://github.com/' + repo + "/issues/" + n +
        '" target="_blank" rel="noopener">#' + n + "</a>");
  return String(text || "").replace(/^([-*]) /gm, "• ").split(/(`[^`\n]+`)/)
    .map(part => /^`[^`\n]+`$/.test(part)
      ? "<code>" + esc(part.slice(1, -1)) + "</code>" : inline(part))
    .join("");
}
