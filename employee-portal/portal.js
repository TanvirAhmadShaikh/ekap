/* ── Helpers ─────────────────────────────────────────────────────────────────── */
const q   = id => document.getElementById(id);
const esc = s  => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
// JS-serialize a value for use as an inline onclick argument inside a single-quoted
// HTML attribute — JSON.stringify handles JS-string escaping, esc() handles HTML-attribute
// escaping, and the extra replace covers stray apostrophes JSON.stringify leaves unescaped.
const attrJson = v => esc(JSON.stringify(v)).replace(/'/g, '&#39;');
const fmt = s  => s ? new Date(s).toLocaleDateString('en-AU',{year:'numeric',month:'short',day:'numeric'}) : '';

async function apiFetch(url) {
  const res = await fetch(url);
  if (!res.ok) { const b = await res.json().catch(()=>({detail:res.statusText})); throw new Error(b.detail); }
  return res.json();
}

function toast(msg, type='info') {
  const t = document.createElement('div');
  t.className = `toast toast-${type}`; t.textContent = msg;
  q('toasts').appendChild(t);
  requestAnimationFrame(() => requestAnimationFrame(() => t.classList.add('show')));
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 280); }, 3200);
}

/* ── File type icons ────────────────────────────────────────────────────────── */
const FILE_ICONS = {
  '.pdf': '📕', '.docx': '📘', '.doc': '📘',
  '.xlsx': '📗', '.xls': '📗', '.csv': '📊',
  '.pptx': '📙', '.ppt': '📙', '.txt': '📄',
  '.md': '📝', '.html': '🌐',
  '.jpg': '🖼', '.jpeg': '🖼', '.png': '🖼',
  '.tiff': '🖼', '.bmp': '🖼', '.webp': '🖼',
};
const fileIcon = t => FILE_ICONS[t] || '📄';

/* ── State ──────────────────────────────────────────────────────────────────── */
let _docs    = [];
let _folders = [];
let _active  = null;  // active folder_id or null (= All)
let _query   = '';
let _searchResults = null; // null = not searching; array = server search results (corpus-wide)
let _searching = false;
let _previewToken = 0;

/* ── Load data ──────────────────────────────────────────────────────────────── */
async function loadAll() {
  const [fData, dData] = await Promise.all([
    apiFetch('/api/folders'),
    apiFetch('/api/documents?lifecycle_state=published&limit=200'),
  ]);
  _folders = fData.folders || [];
  _docs    = dData.documents || [];
}

/* ── Folder tree ────────────────────────────────────────────────────────────── */
function renderFolderTree(nodes, depth = 0) {
  if (!nodes.length) return '';
  return `<ul class="folder-list">${nodes.map(f => `
    <li class="folder-node">
      <button class="folder-btn ${_active===f.folder_id?'active':''}"
              style="padding-left:${14+depth*16}px"
              onclick="selectFolder('${f.folder_id}')">
        <span class="ficon">📁</span>${esc(f.name)}
      </button>
      ${f.children?.length ? renderFolderTree(f.children, depth+1) : ''}
    </li>`).join('')}</ul>`;
}

function renderSidebar() {
  q('folder-tree').innerHTML = `
    <button class="all-btn ${_active===null?'active':''}" onclick="selectFolder(null)">
      📚 All Documents
    </button>
    ${renderFolderTree(_folders)}`;
}

/* ── Documents ──────────────────────────────────────────────────────────────── */
// While a search query is active, results come from the server (real corpus
// search over title/owner/full document content — see performSearch) rather
// than filtering the already-loaded folder listing, so search covers every
// published document regardless of folder or how many are loaded locally.
function visibleDocs() {
  if (_query) return _searchResults || [];
  let docs = _docs;
  if (_active) docs = docs.filter(d => d.folder_id === _active);
  return docs;
}

async function performSearch(query) {
  _searching = true;
  renderMain();
  try {
    const data = await apiFetch(`/api/documents?q=${encodeURIComponent(query)}&lifecycle_state=published&limit=100`);
    _searchResults = data.documents || [];
  } catch(e) {
    _searchResults = [];
    toast(e.message, 'error');
  }
  _searching = false;
  renderMain();
}

function renderDocGrid(docs) {
  if (!docs.length) return `<div class="empty">
    <div class="empty-icon">📭</div>
    <p>${_query ? `No results for "${esc(_query)}"` : 'No documents in this folder.'}</p>
  </div>`;
  return `<div class="doc-grid">${docs.map(d => `
    <div class="doc-card" onclick='openPreview(${attrJson(d.document_id)},${attrJson(d.title)},${attrJson({
      classification: d.classification,
      file_type: d.file_type,
      department: d.department,
      owner: d.owner,
      created_at: d.created_at,
    })})'>
      <div class="doc-card-icon">${fileIcon(d.file_type)}</div>
      <div class="doc-card-title">${esc(d.title)}</div>
      <div class="doc-card-meta">
        ${d.department ? `<span>${esc(d.department)}</span>` : ''}
        ${d.file_type  ? `<span>${esc(d.file_type)}</span>` : ''}
        ${d.created_at ? `<span>${fmt(d.created_at)}</span>` : ''}
        <span class="doc-card-badge pub">Published</span>
      </div>
    </div>`).join('')}</div>`;
}

function renderMain() {
  const docs = visibleDocs();
  const title = _query
    ? `Search results for "${esc(_query)}" (${_searching ? '…' : docs.length})`
    : _active
      ? (findFolder(_folders, _active)?.name || 'Documents') + ` (${docs.length})`
      : `All Documents (${docs.length})`;

  q('main-content').innerHTML = `
    <div class="section-title">${title}</div>
    ${_searching ? '<div class="loading-wrap"><div class="spinner"></div></div>' : renderDocGrid(docs)}`;
}

function findFolder(nodes, id) {
  for (const f of nodes) {
    if (f.folder_id === id) return f;
    if (f.children?.length) { const r = findFolder(f.children, id); if (r) return r; }
  }
  return null;
}

/* ── Interactions ───────────────────────────────────────────────────────────── */
function selectFolder(id) {
  _active = id;
  _query  = '';
  _searchResults = null;
  const input = q('search-input');
  if (input) input.value = '';
  renderSidebar();
  renderMain();
}

let _st;
function handleSearch(v) {
  clearTimeout(_st);
  const query = v.trim();
  _st = setTimeout(() => {
    _query = query;
    if (!query) { _searchResults = null; renderMain(); }
    else performSearch(query);
  }, 250);
}

/* ── Preview ────────────────────────────────────────────────────────────────── */
// pageNum only actually jumps for PDFs — PDF page numbers come from the real
// page objects at ingestion time. Other formats (docx/txt/md) assign
// page_number from synthetic "N paragraphs/lines per page" counts that don't
// correspond to the preview endpoint's own (character-count-based) pagination,
// so a jump there would silently land in the wrong place — safer to just open
// at the top for those than fake precision.
async function openPreview(docId, title, meta = {}, pageNum = null) {
  const myToken = ++_previewToken;

  // Always start a freshly-opened preview in standard view.
  q('preview-modal').classList.remove('fullscreen');
  q('preview-fullscreen-btn').textContent = 'Full Screen';

  q('preview-title').textContent = title;
  q('preview-meta').textContent = [
    meta.classification, meta.file_type, meta.department, meta.owner ? `by ${meta.owner}` : ''
  ].filter(Boolean).join(' · ');
  q('preview-dl').href = `/api/documents/${docId}/download`;
  q('preview-body').innerHTML = '<div class="loading-wrap"><div class="spinner"></div></div>';
  q('preview-modal').classList.remove('hidden');

  try {
    const res = await fetch(`/api/documents/${docId}/preview`);
    if (myToken !== _previewToken) return; // user moved on to another document
    if (!res.ok) throw new Error((await res.json().catch(()=>({detail:'Preview unavailable'}))).detail);
    const ct = res.headers.get('content-type') || '';

    if (ct.includes('text/html')) {
      const totalPages   = parseInt(res.headers.get('X-Total-Pages') || '0', 10);
      const previewPages = parseInt(res.headers.get('X-Preview-Pages') || '0', 10);
      const partial      = totalPages > previewPages;
      const html = await res.text();
      q('preview-body').innerHTML = partial
        ? `<div class="preview-partial-note">Showing first ${previewPages} of ${totalPages} pages — loading the rest in the background…</div>`
        : '';
      q('preview-body').appendChild(makeSandboxedHtmlIframe(html));
      if (partial) loadFullPreviewInBackground(docId, myToken, async fullRes => {
        const container = q('preview-body');
        const oldFrame = container?.querySelector('iframe');
        if (container && oldFrame) await swapHtmlSeamless(container, oldFrame, await fullRes.text());
      });
    } else if (ct.includes('application/pdf')) {
      const totalPages   = parseInt(res.headers.get('X-Total-Pages') || '0', 10);
      const previewPages = parseInt(res.headers.get('X-Preview-Pages') || '0', 10);
      const partial      = totalPages > previewPages;
      // If the target page isn't in this batch yet (still loading in the
      // background), don't jump into a truncated PDF that doesn't contain it —
      // wait and jump once the full-document swap-in below lands instead.
      const pageReady = !pageNum || pageNum <= previewPages;
      const frag      = pageReady && pageNum ? `#page=${pageNum}` : '';
      const url = URL.createObjectURL(await res.blob()) + frag;
      q('preview-body').innerHTML = `<iframe src="${url}"></iframe>` + (partial
        ? `<div class="preview-partial-note">Showing first ${previewPages} of ${totalPages} pages — loading the rest in the background…</div>`
        : '');
      if (partial) loadFullPreviewInBackground(docId, myToken, async fullRes => {
        const container = q('preview-body');
        const oldIframe = container?.querySelector('iframe');
        if (!container || !oldIframe) return;
        const url = URL.createObjectURL(await fullRes.blob()) + (pageNum ? `#page=${pageNum}` : '');
        await swapIframeSeamless(container, oldIframe, url);
      });
    } else if (ct.startsWith('image/')) {
      const url = URL.createObjectURL(await res.blob());
      q('preview-body').innerHTML = `<div style="text-align:center"><img src="${url}"></div>`;
    } else {
      const text = await res.text();
      q('preview-body').innerHTML = `<pre class="preview-text">${esc(text)}</pre>`;
    }
  } catch(e) {
    if (myToken !== _previewToken) return;
    q('preview-body').innerHTML = `<div class="empty">
      <div class="empty-icon">⚠</div><p>${esc(e.message)}</p><br>
      <a class="btn btn-outline" href="/api/documents/${docId}/download" download>⬇ Download</a>
    </div>`;
  }
}

// Crossfade a new iframe in over the old one instead of reassigning .src, which
// would blank the frame while the new PDF loads. The old one is only removed
// once the new one has actually finished loading and faded fully into view.
function swapIframeSeamless(container, oldIframe, newUrl) {
  return new Promise(resolve => {
    const newIframe = document.createElement('iframe');
    newIframe.className = 'preview-fade';
    newIframe.src = newUrl;
    container.appendChild(newIframe);
    newIframe.onload = () => {
      requestAnimationFrame(() => newIframe.classList.add('preview-fade-in'));
      setTimeout(() => {
        const oldUrl = oldIframe.src;
        oldIframe.remove();
        if (oldUrl.startsWith('blob:')) URL.revokeObjectURL(oldUrl);
        newIframe.classList.remove('preview-fade', 'preview-fade-in');
        resolve();
      }, 260);
    };
  });
}

// Same idea for HTML previews (docx/xlsx/pptx/md/html/txt/csv) — crossfade the
// fully-rendered replacement in. Rendered inside a fully sandboxed iframe (see
// makeSandboxedHtmlIframe) rather than innerHTML, so a malicious uploaded
// document can't run script in the app's own origin.
function swapHtmlSeamless(container, oldFrame, newHtml) {
  return new Promise(resolve => {
    const newFrame = makeSandboxedHtmlIframe(newHtml);
    newFrame.classList.add('preview-fade');
    container.insertBefore(newFrame, oldFrame.nextSibling);
    requestAnimationFrame(() => newFrame.classList.add('preview-fade-in'));
    setTimeout(() => {
      oldFrame.remove();
      newFrame.classList.remove('preview-fade', 'preview-fade-in');
      resolve();
    }, 260);
  });
}

// Renders untrusted document-derived HTML (docx/xlsx/pptx/md/html/txt/csv
// conversions) inside a maximally-sandboxed iframe — no scripts, no
// same-origin, no forms/popups — so embedded <script>, onerror=, onload=,
// javascript: URLs etc. in an uploaded document can never execute against
// the portal's own session/cookies/tokens.
function makeSandboxedHtmlIframe(html) {
  const iframe = document.createElement('iframe');
  iframe.sandbox = '';
  iframe.srcdoc = html;
  return iframe;
}

// Large documents (any type) preview with just the first few "pages"; fetch the
// rest in the background in a single request and swap it in once ready. A single
// swap (rather than several growing batches) avoids the preview visibly
// reloading/flashing multiple times — the backend is fast enough now that one
// background fetch is no slower than several small ones would have been.
async function loadFullPreviewInBackground(docId, myToken, apply) {
  try {
    const res = await fetch(`/api/documents/${docId}/preview?full=true`);
    if (!res.ok || myToken !== _previewToken) return;
    await apply(res);
    if (myToken === _previewToken) q('preview-body')?.querySelector('.preview-partial-note')?.remove();
  } catch(e) { /* full document fetch failed — the partial preview stays usable */ }
}

function closePreview() {
  q('preview-modal').classList.add('hidden');
  q('preview-modal').classList.remove('fullscreen');
  q('preview-body').innerHTML = '';
}

function togglePreviewFullscreen() {
  const modal = q('preview-modal-inner');
  const isFullscreen = q('preview-modal').classList.toggle('fullscreen');
  // A manual drag-resize sets inline width/height on the element, which beats
  // any stylesheet rule — including .fullscreen's width:100vw/height:100vh —
  // so without this, Full Screen would silently no-op after a resize. Stash
  // the resized size while fullscreen is on, then restore it on exit instead
  // of snapping back to the default size.
  if (isFullscreen) {
    modal.dataset.prevWidth  = modal.style.width;
    modal.dataset.prevHeight = modal.style.height;
    modal.style.width  = '';
    modal.style.height = '';
  } else {
    modal.style.width  = modal.dataset.prevWidth  || '';
    modal.style.height = modal.dataset.prevHeight || '';
  }
  q('preview-fullscreen-btn').textContent = isFullscreen ? 'Exit Full Screen' : 'Full Screen';
}

// Dragging the modal's native resize handle fires a resize event on (almost)
// every pixel — reflowing/repainting the preview iframe's full content is
// what made it feel slow/glitchy. Adding "resizing" hides the iframe (see
// CSS) for the duration; the 150ms silence-timeout after the last event is
// what "the drag has settled" looks like via ResizeObserver, since it has no
// native start/end signal of its own.
function setupPreviewResizeOptimization() {
  const modal = q('preview-modal-inner');
  if (!modal || typeof ResizeObserver === 'undefined') return;
  let settleTimer = null;
  new ResizeObserver(() => {
    modal.classList.add('resizing');
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => modal.classList.remove('resizing'), 150);
  }).observe(modal);
}

/* ── Chat panel ─────────────────────────────────────────────────────────────── */
let _chatOpen    = false;
let _chatHistory = [];   // [{role,content}]
let _chatBusy    = false;
let _chatConfig  = { show_stats: false, show_timing: false };   // cached GET /api/llm/config

// Turns "[Source N ...]" markers in a streamed reply into clickable spans that
// open the real source document, using the citations array from ekap_stats.
function linkifyCitations(text, citByNum) {
  return esc(text).replace(/\[Source (\d+)[^\]]*\]/g, (match, num) => {
    const c = citByNum[num];
    if (!c) return match;
    const meta = attrJson({ file_type: c.page_number ? `Page ${c.page_number}` : '', department: c.section || '' });
    return `<span class="chat-citation-ref" onclick='openPreview(${attrJson(c.document_id)},${attrJson(c.document_title)},${meta},${attrJson(c.page_number)})'>${match}</span>`;
  });
}

function toggleChat() {
  _chatOpen = !_chatOpen;
  q('chat-panel').classList.toggle('open', _chatOpen);
  q('layout').classList.toggle('chat-open', _chatOpen);
  q('chat-toggle-btn').classList.toggle('chat-open', _chatOpen);
  if (_chatOpen) setTimeout(() => q('chat-input').focus(), 320);
}

function newChat() {
  _chatHistory = [];
  q('chat-messages').innerHTML = `
    <div class="chat-welcome">
      <div class="chat-welcome-icon">⬡</div>
      <p class="chat-welcome-title">How can I help you?</p>
      <p class="chat-welcome-sub">Ask anything — I'll search the knowledge base and answer with sources.</p>
      <div class="chat-suggestions">
        <button class="chat-suggestion" onclick="sendSuggestion(this)">What policies are available?</button>
        <button class="chat-suggestion" onclick="sendSuggestion(this)">Summarise recent engineering docs</button>
        <button class="chat-suggestion" onclick="sendSuggestion(this)">What are the onboarding steps?</button>
      </div>
    </div>`;
}

function sendSuggestion(btn) {
  q('chat-input').value = btn.textContent;
  sendMessage();
}

function handleChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function formatDuration(ms) {
  if (ms >= 1000) {
    const s = ms / 1000;
    return `${s.toFixed(s < 10 ? 2 : 1)} sec`;
  }
  return `${ms < 1 ? ms.toFixed(3) : Math.round(ms)} ms`;
}

function scrollBottom() {
  const msgs = q('chat-messages');
  msgs.scrollTop = msgs.scrollHeight;
}

function appendBubble(role, text = '') {
  // Remove welcome screen on first message
  const welcome = q('chat-messages').querySelector('.chat-welcome');
  if (welcome) welcome.remove();

  const wrap = document.createElement('div');
  wrap.className = `chat-msg chat-msg-${role}`;
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble';
  bubble.textContent = text;
  wrap.appendChild(bubble);
  q('chat-messages').appendChild(wrap);
  scrollBottom();
  return { wrap, bubble };
}

function showThinking() {
  const el = document.createElement('div');
  el.className = 'chat-msg chat-msg-assistant';
  el.id = 'chat-thinking';
  el.innerHTML = `<div class="chat-thinking">
    <div class="chat-dot"></div><div class="chat-dot"></div><div class="chat-dot"></div>
  </div>`;
  q('chat-messages').appendChild(el);
  scrollBottom();
}

function removeThinking() {
  const el = q('chat-thinking');
  if (el) el.remove();
}

async function sendMessage() {
  if (_chatBusy) return;
  const input = q('chat-input');
  const text  = input.value.trim();
  if (!text) return;

  input.value = ''; input.style.height = 'auto';
  _chatBusy = true;
  q('chat-send').disabled = true;

  appendBubble('user', text);
  _chatHistory.push({ role: 'user', content: text });

  showThinking();
  const startTime = performance.now();

  try {
    const res = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'ekap-llm',
        messages: _chatHistory,
        stream: true,
      }),
    });

    removeThinking();

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const { wrap, bubble } = appendBubble('assistant');
    bubble.classList.add('streaming');
    let fullText = '';

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let   buf     = '';
    let   stats   = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();                        // keep incomplete line

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (raw === '[DONE]') continue;
        try {
          const chunk = JSON.parse(raw);
          if (chunk.ekap_stats) { stats = chunk.ekap_stats; continue; }
          const delta = chunk.choices?.[0]?.delta?.content;
          if (delta) { fullText += delta; bubble.textContent = fullText; scrollBottom(); }
        } catch {}
      }
    }

    bubble.classList.remove('streaming');
    _chatHistory.push({ role: 'assistant', content: fullText });

    // Real citation data (document_id/title/page) lets the inline [Source N]
    // markers open the actual source document instead of being static text.
    const citByNum = {};
    for (const c of stats?.citations || []) citByNum[c.source_num] = c;
    bubble.innerHTML = linkifyCitations(fullText, citByNum);

    if (_chatConfig.show_stats) {
      const statParts = stats
        ? [stats.strategy || 'no retrieval', `${stats.chunks} chunk${stats.chunks === 1 ? '' : 's'}`, stats.model].filter(Boolean)
        : [];
      if (statParts.length) {
        const statLine = document.createElement('div');
        statLine.className = 'chat-timing';
        statLine.textContent = `(${statParts.join(' · ')})`;
        wrap.appendChild(statLine);
      }

      const GPU_LABEL = { gpu: '⚙ Using GPU', 'gpu-partial': '⚙ Using GPU (partial offload)', cpu: '⚙ Using CPU' };
      const gpuLabel = GPU_LABEL[stats?.gpu];
      if (gpuLabel) {
        const gpuLine = document.createElement('div');
        gpuLine.className = 'chat-timing';
        gpuLine.textContent = gpuLabel;
        wrap.appendChild(gpuLine);
      }
    }

    if (_chatConfig.show_timing) {
      const timing = document.createElement('div');
      timing.className = 'chat-timing';
      timing.textContent = `⏱ ${formatDuration(performance.now() - startTime)}`;
      wrap.appendChild(timing);
    }

    if (_chatConfig.show_stats || _chatConfig.show_timing) scrollBottom();

  } catch(e) {
    removeThinking();
    const err = document.createElement('div');
    err.className = 'chat-error';
    err.textContent = `⚠ ${e.message}`;
    q('chat-messages').appendChild(err);
    scrollBottom();
  } finally {
    _chatBusy = false;
    q('chat-send').disabled = false;
    q('chat-input').focus();
  }
}

/* ── Init ───────────────────────────────────────────────────────────────────── */
async function init() {
  setupPreviewResizeOptimization();
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      if (!q('preview-modal').classList.contains('hidden')) { closePreview(); return; }
      if (_chatOpen) toggleChat();
    }
  });
  try {
    await loadAll();
    renderSidebar();
    renderMain();
  } catch(e) {
    q('main-content').innerHTML = `<div class="empty">
      <div class="empty-icon">⚠</div><p>Could not load documents: ${esc(e.message)}</p>
    </div>`;
    toast(e.message, 'error');
  }
  loadChatModel();
  loadBranding();
}

async function loadBranding() {
  try {
    const { company_name } = await apiFetch('/api/branding');
    const el = q('topbar-company-name');
    if (el && company_name) el.textContent = company_name;
  } catch(e) { /* topbar just keeps showing "EKAP" if this fails */ }
}

async function loadChatModel() {
  try {
    const config = await apiFetch('/api/llm/config');
    _chatConfig = config;
    q('chat-model-badge').textContent = (config.show_model_name && config.model) ? ` · ${config.model}` : '';
  } catch(e) { /* badge just stays empty if this fails */ }
}

init();
