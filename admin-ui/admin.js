/* ── Helpers ─────────────────────────────────────────────────────────────────── */
const q  = id => document.getElementById(id);
const esc = s  => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const fmt = s  => s ? new Date(s).toLocaleDateString('en-AU',{year:'numeric',month:'short',day:'numeric'}) : '—';
const fmtDateTime = s => s ? new Date(s).toLocaleString('en-AU',{year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
// JS-serialize a value for use as an inline onclick argument inside a single-quoted
// HTML attribute — JSON.stringify handles JS-string escaping, esc() handles HTML-attribute
// escaping, and the extra replace covers stray apostrophes JSON.stringify leaves unescaped.
const attrJson = v => esc(JSON.stringify(v)).replace(/'/g, '&#39;');

async function apiFetch(url, opts = {}) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({ detail: res.statusText }));
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

/* ── Toast ──────────────────────────────────────────────────────────────────── */
function toast(msg, type = 'info') {
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  q('toasts').appendChild(t);
  requestAnimationFrame(() => requestAnimationFrame(() => t.classList.add('show')));
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 3500);
}

/* ── Processing progress ────────────────────────────────────────────────────── */
const PROC_STAGES = {
  ocr_converting:     { pct:  5, label: 'Converting scanned pages (OCR)' },
  extracting:         { pct: 15, label: 'Extracting text' },
  sectioning:         { pct: 28, label: 'Structuring sections' },
  embedding_sections: { pct: 42, label: 'Embedding sections' },
  indexing_sections:  { pct: 56, label: 'Indexing sections' },
  embedding_chunks:   { pct: 75, label: 'Embedding content' },
  indexing_chunks:    { pct: 90, label: 'Indexing content' },
  finalizing:         { pct: 96, label: 'Finalising' },
};

function renderStatus(doc) {
  if (doc.status !== 'processing') return stBadge(doc.status);

  const stage   = doc.processing_stage || 'extracting';
  const info    = PROC_STAGES[stage] || { pct: 10, label: 'Processing' };
  const { pct, label } = info;

  let timeStr = '';
  if (doc.processing_started_at) {
    const elapsed = Math.max(1, Math.round((Date.now() - new Date(doc.processing_started_at).getTime()) / 1000));
    if (pct > 0 && pct < 100) {
      const remaining = Math.round(elapsed * (100 - pct) / pct);
      if (remaining <= 3)      timeStr = 'almost done';
      else if (remaining < 60) timeStr = `~${remaining}s left`;
      else                     timeStr = `~${Math.ceil(remaining/60)}m left`;
    }
  }

  return `<div class="proc-wrap">
    <div class="proc-bar-track">
      <div class="proc-bar-fill" style="width:${pct}%"></div>
    </div>
    <div class="proc-meta">
      <span class="proc-stage">${esc(label)}</span>
      <span style="display:flex;align-items:center;gap:6px">
        ${timeStr ? `<span class="proc-time">${esc(timeStr)}</span>` : ''}
        <button class="btn btn-xs btn-danger"
          onclick="cancelProcessing('${esc(doc.document_id)}','${esc(doc.title)}')">✕ Cancel</button>
      </span>
    </div>
  </div>`;
}

let _pollTimer      = null;
let _processingIds  = new Set();
let _previewToken   = 0;

function startProcessingPoll(docs) {
  clearTimeout(_pollTimer);
  _processingIds = new Set(
    docs.filter(d => d.status === 'processing' || d.status === 'pending').map(d => d.document_id)
  );
  if (_processingIds.size) _pollTimer = setTimeout(_pollStatuses, 3000);
}

async function _pollStatuses() {
  clearTimeout(_pollTimer);
  if (!_processingIds.size) return;
  try {
    const data = await apiFetch('/api/documents?limit=200');
    let anyRunning = false;
    for (const doc of (data.documents || [])) {
      if (!_processingIds.has(doc.document_id)) continue;
      const cell = document.getElementById(`proc-${doc.document_id}`);
      if (!cell) { _processingIds.delete(doc.document_id); continue; }
      if (doc.status === 'processing' || doc.status === 'pending') {
        cell.innerHTML = renderStatus(doc);
        anyRunning = true;
      } else {
        cell.innerHTML = stBadge(doc.status);
        _processingIds.delete(doc.document_id);
      }
    }
    if (anyRunning) _pollTimer = setTimeout(_pollStatuses, 3000);
  } catch {
    _pollTimer = setTimeout(_pollStatuses, 5000);
  }
}

/* ── Badge helpers ──────────────────────────────────────────────────────────── */
const LC_BADGE = {
  draft: 'badge-draft', review: 'badge-review', approved: 'badge-approved',
  published: 'badge-published', archived: 'badge-archived',
};
const ST_BADGE = {
  pending: 'badge-pending', processing: 'badge-processing',
  completed: 'badge-completed', failed: 'badge-failed',
};
const badge = (txt, cls) => `<span class="badge ${cls || 'badge-outline'}">${esc(txt)}</span>`;
const lcBadge = s => badge(s || 'draft', LC_BADGE[s] || 'badge-draft');
const stBadge = s => badge(s || '—', ST_BADGE[s] || 'badge-outline');

/* ── Workflow actions per state ─────────────────────────────────────────────── */
const WF_ACTIONS = {
  draft:     [['submit',    'Submit for Review', 'btn-blue']],
  review:    [['approve',   'Approve',           'btn-success'],
              ['reject',    'Reject',            'btn-danger'],
              ['recall',    'Recall',            'btn-ghost btn-sm']],
  approved:  [['publish',   'Publish',           'btn-success'],
              ['recall',    'Recall',            'btn-ghost btn-sm']],
  published: [['archive',   'Archive',           'btn-warning']],
  archived:  [['republish', 'Republish',         'btn-blue']],
};

function wfButtons(doc) {
  const actions = WF_ACTIONS[doc.lifecycle_state] || [];
  return actions.map(([action, label, cls]) =>
    `<button class="btn btn-xs ${cls}"
       onclick="wfAction('${esc(doc.document_id)}','${action}','${esc(doc.title)}')">
       ${label}</button>`
  ).join('');
}

/* ── Folders (cached flat list) ─────────────────────────────────────────────── */
let _folders = [];

async function loadFolders() {
  try {
    const d = await apiFetch('/api/folders');
    _folders = d.folders || [];
  } catch { _folders = []; }
}

function flatFolders(nodes = _folders, depth = 0) {
  const out = [];
  for (const f of nodes) {
    out.push({ ...f, _d: depth });
    if (f.children?.length) out.push(...flatFolders(f.children, depth + 1));
  }
  return out;
}

function folderOptions(selectedId = '') {
  return flatFolders().map(f =>
    `<option value="${f.folder_id}" ${f.folder_id === selectedId ? 'selected' : ''}>
       ${'—'.repeat(f._d)} ${esc(f.name)}</option>`
  ).join('');
}

/* ── Router ─────────────────────────────────────────────────────────────────── */
const PAGES = {
  '/dashboard': pageDashboard,
  '/documents': pageDocuments,
  '/folders':   pageFolders,
  '/queue':     pageQueue,
  '/trash':     pageTrash,
  '/retention': pageRetention,
  '/audit-log': pageAuditLog,
  '/settings':  pageSettings,
  '/gpu-setup': pageGpuSetup,
};

async function router() {
  clearTimeout(_pollTimer);
  clearTimeout(_settingsTimer);
  const hash = location.hash.slice(1) || '/dashboard';
  const [path] = hash.split('?');
  document.querySelectorAll('.nav-item').forEach(a =>
    a.classList.toggle('active', a.dataset.page === path.slice(1)));
  const fn = PAGES[path];
  if (!fn) return;
  q('content').innerHTML = '<div class="spinner-wrap"><div class="spinner"></div></div>';
  try { await fn(); }
  catch (e) { q('content').innerHTML = `<div class="empty"><p>⚠ ${esc(e.message)}</p></div>`; }
}

/* ── Dashboard ──────────────────────────────────────────────────────────────── */
async function pageDashboard() {
  const [all, review, pub, trashData, recent] = await Promise.all([
    apiFetch('/api/documents?limit=1'),
    apiFetch('/api/documents?lifecycle_state=review&limit=1'),
    apiFetch('/api/documents?lifecycle_state=published&limit=1'),
    apiFetch('/api/documents/trash'),
    apiFetch('/api/documents?limit=10'),
  ]);
  updateQueueBadge(review.total);
  q('content').innerHTML = `
    <div class="page-header">
      <h1>Dashboard</h1>
      <button class="btn btn-primary" onclick="openUpload()">+ Upload Document</button>
    </div>
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-val">${all.total}</div>
        <div class="stat-label">Total Documents</div>
      </div>
      <div class="stat-card yellow">
        <div class="stat-val">${review.total}</div>
        <div class="stat-label">Pending Approval</div>
      </div>
      <div class="stat-card green">
        <div class="stat-val">${pub.total}</div>
        <div class="stat-label">Published</div>
      </div>
      <div class="stat-card slate">
        <div class="stat-val">${trashData.trash?.length || 0}</div>
        <div class="stat-label">In Trash</div>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h2>Recent Documents</h2>
        <a class="btn btn-ghost btn-sm" href="#/documents">View all →</a>
      </div>
      ${renderTable(recent.documents || [], false)}
    </div>`;
  startProcessingPoll(recent.documents || []);
}

/* ── Documents ──────────────────────────────────────────────────────────────── */
async function pageDocuments() {
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  const lc     = params.get('lc')     || '';
  const folder = params.get('folder') || '';
  const q_str  = params.get('q')      || '';

  const qs = new URLSearchParams({ limit: 100 });
  if (lc)     qs.set('lifecycle_state', lc);
  if (folder) qs.set('folder_id', folder);
  if (q_str)  qs.set('q', q_str);

  await loadFolders();
  const data = await apiFetch(`/api/documents?${qs}`);
  const docs = data.documents || [];

  q('content').innerHTML = `
    <div class="page-header">
      <h1>Documents <span style="color:var(--c-muted);font-size:14px;font-weight:400">${data.total} total</span></h1>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="btn btn-ghost btn-sm" id="doc-refresh-btn"
                onclick="refreshDocList(this)" title="Refresh list">↺ Refresh</button>
        <button class="btn btn-primary" onclick="openUpload()">+ Upload</button>
      </div>
    </div>
    <div class="card filters">
      <div class="filter-row">
        <input class="filter-input" placeholder="Search title, owner, or document content…" value="${esc(q_str)}"
               id="doc-q" oninput="debouncedSearch(this.value)">
        <select class="filter-select" onchange="setFilter('lc',this.value)">
          <option value="" ${!lc?'selected':''}>All states</option>
          ${['draft','review','approved','published','archived'].map(s =>
            `<option value="${s}" ${lc===s?'selected':''}>${s.charAt(0).toUpperCase()+s.slice(1)}</option>`
          ).join('')}
        </select>
        <select class="filter-select" onchange="setFilter('folder',this.value)">
          <option value="" ${!folder?'selected':''}>All folders</option>
          ${folderOptions(folder)}
        </select>
      </div>
    </div>
    <div class="card">${renderTable(docs, true)}</div>`;
  startProcessingPoll(docs);
}

async function refreshDocList(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '↻'; }
  await pageDocuments();
}

function setFilter(key, val) {
  const p = new URLSearchParams(location.hash.split('?')[1] || '');
  if (val) p.set(key, val); else p.delete(key);
  location.hash = '/documents?' + p.toString();
}

let _st;
function debouncedSearch(v) {
  clearTimeout(_st);
  _st = setTimeout(() => setFilter('q', v), 280);
}

function renderTable(docs, withActions) {
  if (!docs.length) return '<div class="empty"><div class="empty-icon">📭</div><p>No documents found.</p></div>';
  return `<div class="table-wrap"><table class="data-table"><thead><tr>
    <th>Title</th><th>Owner</th><th>Dept</th><th>Class</th>
    <th>Type</th><th>Version</th><th>State</th><th>Status</th><th>Created</th>
    ${withActions ? '<th>Actions</th>' : ''}
  </tr></thead><tbody>
  ${docs.map(d => `<tr>
    <td><a class="link" href="javascript:void(0)"
       onclick='openPreview(${attrJson(d.document_id)},${attrJson(d.title)},${attrJson(d)})'>${esc(d.title)}</a></td>
    <td>${esc(d.owner||'—')}</td>
    <td>${esc(d.department||'—')}</td>
    <td>${badge(d.classification,'badge-outline')}</td>
    <td>${esc(d.file_type||'—')}</td>
    <td><a class="link" href="javascript:void(0)" title="View audit trail"
       onclick='openAuditTrail(${attrJson(d.document_id)},${attrJson(d.title)})'>v${esc(d.current_version || 1)}</a></td>
    <td>${lcBadge(d.lifecycle_state)}${d.legal_hold ? ` <span class="badge" style="background:#fef3c7;color:#92400e" title="${esc(d.legal_hold_reason||'')}">⚖ HOLD</span>` : ''}</td>
    <td id="proc-${d.document_id}">${renderStatus(d)}</td>
    <td>${fmtDateTime(d.created_at)}</td>
    ${withActions ? `<td class="actions">
      ${wfButtons(d)}
      ${d.status === 'failed' ? `<button class="btn btn-xs btn-blue"
        onclick="reindexDoc('${esc(d.document_id)}','${esc(d.title)}')">↺ Reindex</button>` : ''}
      <button class="btn btn-xs btn-ghost"
        onclick='openVersionUpload(${attrJson(d.document_id)},${attrJson(d.title)})'>↑ New Version</button>
      <button class="btn btn-xs btn-ghost"
        onclick='openPermissions(${attrJson(d.document_id)},${attrJson(d.title)})'>🔐 Access</button>
      ${d.legal_hold
        ? `<button class="btn btn-xs btn-warning"
             onclick='releaseLegalHold(${attrJson(d.document_id)},${attrJson(d.title)})'>⚖ Release Hold</button>`
        : `<button class="btn btn-xs btn-ghost"
             onclick='setLegalHold(${attrJson(d.document_id)},${attrJson(d.title)})'>⚖ Legal Hold</button>`}
      <button class="btn btn-xs btn-danger"
        onclick="doDelete('${esc(d.document_id)}','${esc(d.title)}')">Delete</button>
    </td>` : ''}
  </tr>`).join('')}
  </tbody></table></div>`;
}

/* ── Folders ────────────────────────────────────────────────────────────────── */
async function pageFolders() {
  await loadFolders();
  q('content').innerHTML = `
    <div class="page-header">
      <h1>Folders</h1>
      <button class="btn btn-primary" onclick="createFolder()">+ New Folder</button>
    </div>
    <div class="card">
      ${_folders.length
        ? renderFolderTree(_folders)
        : '<div class="empty"><div class="empty-icon">📁</div><p>No folders yet.</p></div>'}
    </div>`;
}

function renderFolderTree(nodes, depth = 0) {
  return `<ul class="folder-tree">${nodes.map(f => `
    <li class="folder-item" style="padding-left:${20 + depth*24}px">
      <span class="folder-icon">📁</span>
      <span class="folder-name">${esc(f.name)}</span>
      ${f.department ? `<span class="folder-dept">${esc(f.department)}</span>` : ''}
      <div class="folder-actions">
        <a class="btn btn-xs btn-ghost" href="#/documents?folder=${f.folder_id}">View Docs</a>
        <button class="btn btn-xs btn-ghost"
          onclick='renameFolder("${f.folder_id}","${esc(f.name)}")'>Rename</button>
        <button class="btn btn-xs btn-danger"
          onclick='deleteFolder("${f.folder_id}","${esc(f.name)}")'>Delete</button>
      </div>
      ${f.children?.length ? `<div class="folder-children">${renderFolderTree(f.children, depth+1)}</div>` : ''}
    </li>`).join('')}</ul>`;
}

async function createFolder() {
  const name = prompt('Folder name:'); if (!name?.trim()) return;
  const dept = prompt('Department (optional):') || '';
  try {
    await apiFetch('/api/folders', {
      method: 'POST',
      body: JSON.stringify({ name: name.trim(), owner: 'admin', department: dept }),
    });
    toast('Folder created', 'success');
    pageFolders();
  } catch(e) { toast(e.message, 'error'); }
}

async function renameFolder(id, old_name) {
  const name = prompt('New name:', old_name); if (!name?.trim() || name === old_name) return;
  try {
    await apiFetch(`/api/folders/${id}`, { method: 'PUT', body: JSON.stringify({ name: name.trim() }) });
    toast('Folder renamed', 'success'); pageFolders();
  } catch(e) { toast(e.message, 'error'); }
}

async function deleteFolder(id, name) {
  if (!confirm(`Delete folder "${name}"?\nDocuments inside will be moved to root.`)) return;
  try {
    await apiFetch(`/api/folders/${id}`, { method: 'DELETE' });
    toast('Folder deleted', 'success'); pageFolders();
  } catch(e) { toast(e.message, 'error'); }
}

/* ── Approval Queue ─────────────────────────────────────────────────────────── */
async function pageQueue() {
  const data = await apiFetch('/api/workflow/pending');
  const docs = data.pending || [];
  updateQueueBadge(docs.length);
  q('content').innerHTML = `
    <div class="page-header"><h1>Approval Queue</h1></div>
    <div class="card">
      ${!docs.length
        ? '<div class="empty"><div class="empty-icon">✅</div><p>No documents awaiting approval.</p></div>'
        : `<div class="table-wrap"><table class="data-table"><thead><tr>
            <th>Title</th><th>Owner</th><th>Department</th><th>Classification</th><th>Type</th><th>Submitted</th><th>Actions</th>
          </tr></thead><tbody>
          ${docs.map(d => `<tr>
            <td><a class="link" href="javascript:void(0)"
               onclick='openPreview(${attrJson(d.document_id)},${attrJson(d.title)},${attrJson(d)})'>${esc(d.title)}</a></td>
            <td>${esc(d.owner||'—')}</td>
            <td>${esc(d.department||'—')}</td>
            <td>${badge(d.classification,'badge-outline')}</td>
            <td>${esc(d.file_type||'—')}</td>
            <td>${fmt(d.updated_at)}</td>
            <td class="actions">
              <button class="btn btn-xs btn-success"
                onclick="wfAction('${esc(d.document_id)}','approve','${esc(d.title)}')">Approve</button>
              <button class="btn btn-xs btn-danger"
                onclick="wfAction('${esc(d.document_id)}','reject','${esc(d.title)}')">Reject</button>
            </td>
          </tr>`).join('')}
          </tbody></table></div>`}
    </div>`;
}

/* ── Trash ──────────────────────────────────────────────────────────────────── */
async function pageTrash() {
  const data = await apiFetch('/api/documents/trash');
  const docs = data.trash || [];
  q('content').innerHTML = `
    <div class="page-header"><h1>Trash</h1></div>
    <div class="card">
      ${!docs.length
        ? '<div class="empty"><div class="empty-icon">🗑</div><p>Trash is empty.</p></div>'
        : `<div class="table-wrap"><table class="data-table"><thead><tr>
            <th>Title</th><th>Owner</th><th>Type</th><th>Deleted</th><th>Actions</th>
          </tr></thead><tbody>
          ${docs.map(d => `<tr>
            <td>${esc(d.title)}</td>
            <td>${esc(d.owner||'—')}</td>
            <td>${esc(d.file_type||'—')}</td>
            <td>${fmt(d.deleted_at)}</td>
            <td class="actions">
              <button class="btn btn-xs btn-success"
                onclick="undelete('${esc(d.document_id)}','${esc(d.title)}')">Restore</button>
            </td>
          </tr>`).join('')}
          </tbody></table></div>`}
    </div>`;
}

/* ── Retention policy ───────────────────────────────────────────────────────── */
async function pageRetention() {
  const data = await apiFetch('/api/retention-policies');
  const policies = data.policies || [];

  q('content').innerHTML = `
    <div class="page-header">
      <h1>Retention Policy</h1>
      <button class="btn btn-primary btn-sm" onclick="runRetentionSweepNow()">▶ Run Sweep Now</button>
    </div>
    <p class="llm-note" style="margin-bottom:16px">
      Documents past their retention period are archived (or moved to Trash, per the action below)
      the next time the background sweep runs — hourly by default. A document under
      <strong>Legal Hold</strong> is always skipped, no matter how far past retention it is.
      The clock starts when a document is published, using its classification's policy at that time.
    </p>
    <div class="card">
      <div class="table-wrap"><table class="data-table"><thead><tr>
        <th>Classification</th><th>Retention (days)</th><th>When it elapses</th><th>Last updated</th><th></th>
      </tr></thead><tbody>
        ${policies.map(p => `
          <tr>
            <td><strong>${esc(p.classification)}</strong></td>
            <td><input class="input" type="number" min="1" style="width:100px"
                  id="ret-days-${esc(p.classification)}" value="${p.retention_days}"></td>
            <td>
              <select class="filter-select" id="ret-action-${esc(p.classification)}">
                <option value="archive" ${p.action === 'archive' ? 'selected' : ''}>Archive</option>
                <option value="delete"  ${p.action === 'delete'  ? 'selected' : ''}>Move to Trash</option>
              </select>
            </td>
            <td>${fmtDateTime(p.updated_at)}</td>
            <td><button class="btn btn-xs btn-primary"
                  onclick="saveRetentionPolicy('${esc(p.classification)}')">Save</button></td>
          </tr>`).join('')}
      </tbody></table></div>
    </div>`;
}

async function saveRetentionPolicy(classification) {
  const days   = parseInt(q(`ret-days-${classification}`).value, 10);
  const action = q(`ret-action-${classification}`).value;
  if (!days || days < 1) { toast('Enter a valid number of days', 'error'); return; }
  try {
    await apiFetch(`/api/retention-policies/${encodeURIComponent(classification)}`, {
      method: 'PUT',
      body: JSON.stringify({ retention_days: days, action }),
    });
    toast(`${classification} retention policy updated`, 'success');
    pageRetention();
  } catch(e) { toast(e.message, 'error'); }
}

async function runRetentionSweepNow() {
  try {
    const d = await apiFetch('/api/retention/run-now', { method: 'POST' });
    toast(`Sweep complete — archived ${d.archived}, moved to trash ${d.deleted}`, 'success');
  } catch(e) { toast(e.message, 'error'); }
}

/* ── Audit log (tamper-evident hash chain) ─────────────────────────────────── */
// <input type="datetime-local"> values have no timezone info ("2026-07-01T00:00");
// sent as-is, Postgres interprets them in the DB's own timezone rather than the
// browser's, silently shifting the window. new Date(local) parses a bare
// date-time string as local time per the ECMAScript spec, so converting through
// it and back out via toISOString() gives the correct UTC instant to send.
function localDateTimeToUTC(local) {
  if (!local) return '';
  const d = new Date(local);
  return isNaN(d) ? '' : d.toISOString();
}

async function pageAuditLog(offset = 0, start = '', end = '') {
  const qs = new URLSearchParams({ limit: 50, offset });
  const startUtc = localDateTimeToUTC(start);
  const endUtc   = localDateTimeToUTC(end);
  if (startUtc) qs.set('start', startUtc);
  if (endUtc)   qs.set('end', endUtc);

  const [verify, data] = await Promise.all([
    apiFetch('/api/audit-log/verify'),
    apiFetch(`/api/audit-log?${qs}`),
  ]);
  const entries = data.entries || [];

  q('content').innerHTML = `
    <div class="page-header">
      <h1>Audit Log <span style="color:var(--c-muted);font-size:14px;font-weight:400">${data.total} entries</span></h1>
      <button class="btn btn-ghost btn-sm" onclick="pageAuditLog(${offset},${attrJson(start)},${attrJson(end)})">↺ Re-verify</button>
    </div>
    <div class="card" style="padding:14px 18px;margin-bottom:16px;display:flex;align-items:center;gap:10px;
         background:${verify.valid ? '#f0fdf4' : '#fef2f2'};border-left:4px solid ${verify.valid ? 'var(--c-success)' : 'var(--c-danger)'}">
      <span style="font-size:20px">${verify.valid ? '✓' : '⚠'}</span>
      <div>
        <div style="font-weight:700;font-size:13.5px;color:${verify.valid ? '#166534' : '#991b1b'}">
          ${verify.valid ? 'Chain intact — no tampering detected' : 'Tampering detected'}
        </div>
        <div style="font-size:12px;color:var(--c-muted)">
          ${verify.checked} entries checked${verify.valid ? '' : ` — first broken entry: #${verify.first_broken_id}`}
        </div>
      </div>
    </div>
    <div class="card filters">
      <div class="filter-row" style="align-items:flex-end">
        <div class="field">
          <label class="label">From</label>
          <input class="input" type="datetime-local" id="audit-start" value="${esc(start)}">
        </div>
        <div class="field">
          <label class="label">To</label>
          <input class="input" type="datetime-local" id="audit-end" value="${esc(end)}">
        </div>
        <button class="btn btn-ghost btn-sm" onclick="applyAuditLogRange()">Apply Range</button>
        <button class="btn btn-ghost btn-sm" onclick="pageAuditLog()">Clear</button>
        <button class="btn btn-primary btn-sm" style="margin-left:auto" onclick="exportAuditLog()">⬇ Export CSV</button>
      </div>
    </div>
    <div class="card">
      <div class="table-wrap"><table class="data-table"><thead><tr>
        <th>#</th><th>Time</th><th>User</th><th>Event</th><th>Resource</th><th>Details</th>
      </tr></thead><tbody>
        ${!entries.length ? '' : entries.map(e => `
          <tr>
            <td>${e.id}</td>
            <td>${fmtDateTime(e.timestamp)}</td>
            <td>${esc(e.user_id || '—')}</td>
            <td><span class="badge badge-outline">${esc(e.event_type)}</span></td>
            <td>${e.resource_id ? `<code style="font-size:11px">${esc(e.resource_id.slice(0, 8))}…</code>` : '—'}</td>
            <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11.5px;color:var(--c-muted)"
                title="${esc(JSON.stringify(e.details))}">${esc(JSON.stringify(e.details))}</td>
          </tr>`).join('')}
      </tbody></table></div>
      ${!entries.length ? '<div class="empty"><div class="empty-icon">🔗</div><p>No audit entries in range.</p></div>' : `
      <div style="display:flex;justify-content:space-between;padding:12px 16px">
        <button class="btn btn-ghost btn-sm" ${offset === 0 ? 'disabled' : ''}
          onclick="pageAuditLog(${Math.max(0, offset - 50)},${attrJson(start)},${attrJson(end)})">← Newer</button>
        <button class="btn btn-ghost btn-sm" ${offset + 50 >= data.total ? 'disabled' : ''}
          onclick="pageAuditLog(${offset + 50},${attrJson(start)},${attrJson(end)})">Older →</button>
      </div>`}
    </div>`;
}

function applyAuditLogRange() {
  const start = q('audit-start').value;
  const end   = q('audit-end').value;
  pageAuditLog(0, start, end);
}

function exportAuditLog() {
  const startUtc = localDateTimeToUTC(q('audit-start')?.value || '');
  const endUtc   = localDateTimeToUTC(q('audit-end')?.value || '');
  const qs = new URLSearchParams();
  if (startUtc) qs.set('start', startUtc);
  if (endUtc)   qs.set('end', endUtc);
  window.location = `/api/audit-log/export?${qs}`;
}

/* ── Upload modal ───────────────────────────────────────────────────────────── */
async function openUpload() {
  await loadFolders();
  q('f-folder').innerHTML = `<option value="">None</option>${folderOptions()}`;
  q('upload-modal').classList.remove('hidden');
  q('f-file').focus();
}

function closeUpload() {
  q('upload-modal').classList.add('hidden');
  q('upload-form').reset();
}

async function submitUpload(e) {
  e.preventDefault();
  const file = q('f-file').files[0]; if (!file) return;
  await doUpload(file, false);
}

async function doUpload(file, force) {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('title', q('f-title').value || file.name.replace(/\.[^.]+$/, ''));
  fd.append('owner', q('f-owner').value || 'admin');
  fd.append('department', q('f-dept').value);
  fd.append('classification', q('f-class').value);
  fd.append('tags', q('f-tags').value);
  const fid = q('f-folder').value;
  if (fid) fd.append('folder_id', fid);
  if (force) fd.append('force', 'true');

  const btn = q('upload-btn');
  btn.disabled = true; btn.textContent = 'Uploading…';
  try {
    const res = await fetch('/api/documents/upload', { method: 'POST', body: fd });
    if (res.status === 409) {
      const body = await res.json().catch(() => ({}));
      showDuplicateDialog(body.detail || {}, file);
      return;
    }
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    if (!res.ok) throw new Error(body.detail);
    toast(`"${body.filename}" queued for processing`, 'success');
    closeUpload();
    router();
  } catch(e) { toast(e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = 'Upload'; }
}

/* ── Possible-duplicate dialog ──────────────────────────────────────────────── */
let _pendingUploadFile = null;
let _duplicateMatch = null;

function showDuplicateDialog(detail, file) {
  _pendingUploadFile = file;
  _duplicateMatch = detail;
  q('upload-modal').classList.add('hidden'); // one overlay visible at a time
  q('duplicate-message').textContent = detail.message || 'A similar document already exists.';
  q('duplicate-modal').classList.remove('hidden');
}

function closeDuplicateModal() {
  // Cancel abandons this upload attempt entirely rather than trying to restore
  // the form underneath — simplest, least surprising behavior.
  q('duplicate-modal').classList.add('hidden');
  _pendingUploadFile = null;
  _duplicateMatch = null;
  q('upload-form').reset();
}

function useAsNewVersion() {
  const match = _duplicateMatch;
  const file  = _pendingUploadFile;
  closeDuplicateModal();
  if (match?.existing_document_id) {
    openVersionUpload(match.existing_document_id, match.existing_title || 'Document');
    if (file) {
      const dt = new DataTransfer();
      dt.items.add(file);
      q('v-file').files = dt.files;
    }
  }
}

async function forceUploadAnyway() {
  const file = _pendingUploadFile;
  q('duplicate-modal').classList.add('hidden');
  q('upload-modal').classList.remove('hidden'); // so "Uploading…" is visible during the retry
  if (file) await doUpload(file, true);
}

/* ── Upload new version modal ──────────────────────────────────────────────── */
let _versionDocId = null;

function openVersionUpload(docId, title) {
  _versionDocId = docId;
  q('version-doc-title').textContent = title;
  q('version-modal').classList.remove('hidden');
  q('v-file').focus();
}

function closeVersionModal() {
  q('version-modal').classList.add('hidden');
  q('version-form').reset();
  _versionDocId = null;
}

async function submitVersionUpload(e) {
  e.preventDefault();
  const file = q('v-file').files[0]; if (!file || !_versionDocId) return;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('change_note', q('v-note').value);

  const btn = q('version-btn');
  btn.disabled = true; btn.textContent = 'Uploading…';
  try {
    const res = await fetch(`/api/documents/${_versionDocId}/versions/upload`, { method: 'POST', body: fd });
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    if (!res.ok) throw new Error(body.detail);
    toast(`Version ${body.version} queued for processing`, 'success');
    closeVersionModal();
    router();
  } catch(e) { toast(e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = 'Upload Version'; }
}

/* ── Keycloak user search (autocomplete for the access-grant form) ───────────── */
let _userSearchTimer;

function handleUserSearch(q) {
  clearTimeout(_userSearchTimer);
  _userSearchTimer = setTimeout(() => runUserSearch(q), 250);
}

async function runUserSearch(query) {
  const list = q('perm-user-suggestions');
  try {
    const data  = await apiFetch(`/api/keycloak/users?q=${encodeURIComponent(query)}`);
    const users = data.users || [];
    list.innerHTML = !users.length
      ? '<div class="autocomplete-empty">No matching users</div>'
      : users.map(u => `
        <div class="autocomplete-item" onclick='selectUser(${attrJson(u.username)})'>
          <span class="autocomplete-item-name">${esc(u.username)}${u.name ? ` — ${esc(u.name)}` : ''}</span>
          ${u.email ? `<span class="autocomplete-item-sub">${esc(u.email)}</span>` : ''}
        </div>`).join('');
  } catch(e) {
    list.innerHTML = `<div class="autocomplete-empty">⚠ ${esc(e.message)}</div>`;
  }
  list.classList.remove('hidden');
}

function selectUser(username) {
  q('perm-user-id').value = username;
  q('perm-user-suggestions').classList.add('hidden');
}

/* ── Document access grants ────────────────────────────────────────────────── */
let _permDocId = null;

async function openPermissions(docId, title) {
  _permDocId = docId;
  q('perm-doc-title').textContent = title;
  q('perm-user-id').value = '';
  q('perm-list').innerHTML = '<div class="spinner-wrap"><div class="spinner"></div></div>';
  q('permissions-modal').classList.remove('hidden');
  await refreshPermissions();
  q('perm-user-id').focus();
}

async function refreshPermissions() {
  try {
    const data = await apiFetch(`/api/documents/${_permDocId}/permissions`);
    const grants = data.permissions || [];
    q('perm-list').innerHTML = !grants.length
      ? '<div class="empty"><div class="empty-icon">🔐</div><p>No explicit grants — access is classification-only.</p></div>'
      : grants.map(g => `
        <div class="llm-model-row">
          <div class="llm-model-info">
            <span class="llm-model-name">${esc(g.user_id)}</span>
            <span class="llm-model-size">granted by ${esc(g.granted_by||'—')} · ${fmtDateTime(g.granted_at)}</span>
          </div>
          <button class="btn btn-xs btn-danger"
            onclick='revokePermission(${attrJson(g.user_id)})'>Revoke</button>
        </div>`).join('');
  } catch(e) {
    q('perm-list').innerHTML = `<div class="empty"><div class="empty-icon">⚠</div><p>${esc(e.message)}</p></div>`;
  }
}

async function submitPermissionGrant(e) {
  e.preventDefault();
  const userId = q('perm-user-id').value.trim();
  if (!userId || !_permDocId) return;
  const btn = q('perm-grant-btn');
  btn.disabled = true; btn.textContent = 'Granting…';
  try {
    await apiFetch(`/api/documents/${_permDocId}/permissions`, {
      method: 'POST',
      body: JSON.stringify({ user_id: userId }),
    });
    toast(`Access granted to "${userId}"`, 'success');
    q('perm-user-id').value = '';
    await refreshPermissions();
  } catch(e) { toast(e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = 'Grant Access'; }
}

async function revokePermission(userId) {
  if (!confirm(`Revoke "${userId}"'s access to this document?`)) return;
  try {
    await apiFetch(`/api/documents/${_permDocId}/permissions/${encodeURIComponent(userId)}`, { method: 'DELETE' });
    toast(`Access revoked for "${userId}"`, 'success');
    await refreshPermissions();
  } catch(e) { toast(e.message, 'error'); }
}

function closePermissionsModal() {
  q('permissions-modal').classList.add('hidden');
  q('perm-list').innerHTML = '';
  q('perm-user-suggestions').classList.add('hidden');
  _permDocId = null;
}

/* ── Preview modal ──────────────────────────────────────────────────────────── */
async function openPreview(docId, title, meta = {}) {
  const myToken = ++_previewToken;

  q('preview-title').textContent = title;
  q('preview-meta').textContent = [
    meta.classification, meta.file_type, meta.lifecycle_state
  ].filter(Boolean).join(' · ');
  q('preview-download').href = `/api/documents/${docId}/download`;
  q('preview-download').download = title;
  q('preview-body').innerHTML = '<div class="spinner-wrap"><div class="spinner"></div></div>';
  q('preview-modal').classList.remove('hidden');

  try {
    const res = await fetch(`/api/documents/${docId}/preview`);
    if (myToken !== _previewToken) return; // user moved on to another document
    if (!res.ok) throw new Error((await res.json().catch(() => ({detail:'Preview failed'}))).detail);
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
      const url = URL.createObjectURL(await res.blob());
      q('preview-body').innerHTML = `<iframe src="${url}"></iframe>` + (partial
        ? `<div class="preview-partial-note">Showing first ${previewPages} of ${totalPages} pages — loading the rest in the background…</div>`
        : '');
      if (partial) loadFullPreviewInBackground(docId, myToken, async fullRes => {
        const container = q('preview-body');
        const oldIframe = container?.querySelector('iframe');
        if (!container || !oldIframe) return;
        const url = URL.createObjectURL(await fullRes.blob());
        await swapIframeSeamless(container, oldIframe, url);
      });
    } else if (ct.startsWith('image/')) {
      const url = URL.createObjectURL(await res.blob());
      q('preview-body').innerHTML = `<div style="padding:20px;text-align:center"><img src="${url}"></div>`;
    } else {
      const text = await res.text();
      q('preview-body').innerHTML = `<pre class="preview-text">${esc(text)}</pre>`;
    }
  } catch(e) {
    if (myToken !== _previewToken) return;
    q('preview-body').innerHTML = `<div class="empty">
      <div class="empty-icon">⚠</div>
      <p>${esc(e.message)}</p>
      <br><a class="btn btn-primary" href="/api/documents/${docId}/download" download>
        ⬇ Download Instead</a>
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
// the admin portal's own session/cookies/tokens.
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
  q('preview-body').innerHTML = '';
}

/* ── Audit trail (versions + workflow transitions, merged) ────────────────────── */
let _auditDocId = null;

async function openAuditTrail(docId, title) {
  _auditDocId = docId;
  q('audit-title').textContent = `Audit Trail — ${title}`;
  q('audit-body').innerHTML = '<div class="spinner-wrap"><div class="spinner"></div></div>';
  q('audit-modal').classList.remove('hidden');

  try {
    const [versions, workflow] = await Promise.all([
      apiFetch(`/api/documents/${docId}/versions`),
      apiFetch(`/api/documents/${docId}/workflow`),
    ]);
    // Manager-only — a 403 here shouldn't break the rest of the audit trail.
    let accessLog = { access_log: [] };
    try { accessLog = await apiFetch(`/api/documents/${docId}/access-log`); } catch(e) {}

    const versionList = versions.versions || []; // API returns newest-first
    const events = [
      ...versionList.map(v => ({
        ts: v.created_at, icon: '📄',
        title: `Version ${v.version_number} uploaded`,
        by: v.uploaded_by, note: v.change_note,
        meta: [v.file_type, fmtBytes(v.file_size)].filter(Boolean).join(' · '),
      })),
      ...(workflow.history || []).map(h => ({
        ts: h.created_at, icon: '🔄',
        title: h.from_state ? `${h.from_state} → ${h.to_state}` : `Created (${h.to_state})`,
        by: h.username || h.user_id, note: h.comment,
      })),
      ...(accessLog.access_log || []).map(a => ({
        ts: a.timestamp, icon: a.event_type === 'DOCUMENT_DOWNLOADED' ? '⬇' : '👁',
        title: a.event_type === 'DOCUMENT_DOWNLOADED' ? 'Downloaded' : 'Viewed',
        by: a.user_id,
      })),
    ].sort((a, b) => new Date(b.ts) - new Date(a.ts));

    const compareBlock = versionList.length < 2 ? '' : `
      <div class="version-compare">
        <span class="version-compare-label">Compare:</span>
        <select class="filter-select" id="diff-from">
          ${versionList.map(v => `<option value="${v.version_number}">v${v.version_number}</option>`).join('')}
        </select>
        <span>→</span>
        <select class="filter-select" id="diff-to">
          ${versionList.map(v => `<option value="${v.version_number}">v${v.version_number}</option>`).join('')}
        </select>
        <button class="btn btn-xs btn-primary" onclick="runVersionDiff()">Compare</button>
      </div>
      <div id="version-diff-result"></div>`;

    q('audit-body').innerHTML = compareBlock + (!events.length
      ? '<div class="empty"><div class="empty-icon">🕒</div><p>No history recorded yet.</p></div>'
      : `<div class="audit-timeline">${events.map(e => `
          <div class="audit-item">
            <span class="audit-icon">${e.icon}</span>
            <div class="audit-item-body">
              <div class="audit-item-head">
                <span class="audit-item-title">${esc(e.title)}</span>
                <span class="audit-item-time">${fmtDateTime(e.ts)}</span>
              </div>
              <div class="audit-item-meta">
                ${e.by ? `by ${esc(e.by)}` : ''}${e.meta ? ` · ${esc(e.meta)}` : ''}
              </div>
              ${e.note ? `<div class="audit-item-note">${esc(e.note)}</div>` : ''}
            </div>
          </div>`).join('')}</div>`);

    if (versionList.length >= 2) {
      q('diff-to').value   = versionList[0].version_number;
      q('diff-from').value = versionList[1].version_number;
    }
  } catch(e) {
    q('audit-body').innerHTML = `<div class="empty"><div class="empty-icon">⚠</div><p>${esc(e.message)}</p></div>`;
  }
}

async function runVersionDiff() {
  const from = q('diff-from').value;
  const to   = q('diff-to').value;
  const result = q('version-diff-result');
  if (from === to) {
    result.innerHTML = '<div class="notif-empty">Pick two different versions to compare.</div>';
    return;
  }
  result.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div></div>';
  try {
    const d = await apiFetch(`/api/documents/${_auditDocId}/versions/diff?from=${from}&to=${to}`);
    result.innerHTML = !d.lines.length
      ? '<div class="notif-empty">No text differences between these versions.</div>'
      : `<div class="diff-summary">+${d.added_lines} added &nbsp; −${d.removed_lines} removed</div>
         <div class="diff-view">${d.lines.map(l => {
           const prefix = l.type === 'added' ? '+' : l.type === 'removed' ? '−' : l.type === 'hunk' ? '' : ' ';
           return `<div class="diff-line diff-${l.type}">${esc(prefix)}${esc(l.text)}</div>`;
         }).join('')}</div>`;
  } catch(e) {
    result.innerHTML = `<div class="notif-empty">⚠ ${esc(e.message)}</div>`;
  }
}

function closeAuditModal() {
  q('audit-modal').classList.add('hidden');
  q('audit-body').innerHTML = '';
  _auditDocId = null;
}

/* ── Workflow action (with comment modal) ───────────────────────────────────── */
let _wfResolve;

function wfAction(docId, action, title) {
  const labels = { approve:'Approve', reject:'Reject', publish:'Publish',
                   archive:'Archive', recall:'Recall', republish:'Republish',
                   submit:'Submit for Review' };
  q('wf-title').textContent = `${labels[action] || action}: "${title}"`;
  q('wf-comment').value = '';
  q('wf-modal').classList.remove('hidden');
  q('wf-comment').focus();

  const confirmBtn = q('wf-confirm');
  confirmBtn.onclick = async () => {
    const comment = q('wf-comment').value.trim();
    closeWfModal();
    try {
      const res = await apiFetch(`/api/documents/${docId}/${action}`, {
        method: 'POST',
        body: JSON.stringify({ comment }),
      });
      toast(`Document moved to "${res.to_state}"`, 'success');
      router();
    } catch(e) { toast(e.message, 'error'); }
  };
}

function closeWfModal() {
  q('wf-modal').classList.add('hidden');
}

/* ── Cancel / Reindex ───────────────────────────────────────────────────────── */
async function cancelProcessing(id, title) {
  if (!confirm(`Cancel processing of "${title}"?\nThe document will be marked as failed. You can reindex it later.`)) return;
  try {
    await apiFetch(`/api/documents/${id}/cancel-processing`, { method: 'POST' });
    toast(`Processing cancelled for "${title}"`, 'info');
    router();
  } catch(e) { toast(e.message, 'error'); }
}

async function reindexDoc(id, title) {
  try {
    await apiFetch('/api/documents/reindex', { method: 'POST', body: JSON.stringify({ document_id: id }) });
    toast(`"${title}" queued for reprocessing`, 'success');
    router();
  } catch(e) { toast(e.message, 'error'); }
}

/* ── Delete / Undelete ──────────────────────────────────────────────────────── */
async function doDelete(id, title) {
  if (!confirm(`Move "${title}" to trash?`)) return;
  try {
    await apiFetch(`/api/documents/${id}`, { method: 'DELETE' });
    toast(`"${title}" moved to trash`, 'success');
    router();
  } catch(e) { toast(e.message, 'error'); }
}

async function setLegalHold(id, title) {
  const reason = prompt(`Reason for placing "${title}" under legal hold:`);
  if (!reason?.trim()) return;
  try {
    await apiFetch(`/api/documents/${id}/legal-hold`, {
      method: 'POST',
      body: JSON.stringify({ reason: reason.trim() }),
    });
    toast(`"${title}" placed under legal hold`, 'success');
    router();
  } catch(e) { toast(e.message, 'error'); }
}

async function releaseLegalHold(id, title) {
  if (!confirm(`Release the legal hold on "${title}"? It becomes eligible for retention actions again.`)) return;
  try {
    await apiFetch(`/api/documents/${id}/legal-hold`, { method: 'DELETE' });
    toast(`Legal hold released for "${title}"`, 'success');
    router();
  } catch(e) { toast(e.message, 'error'); }
}

async function undelete(id, title) {
  try {
    await apiFetch(`/api/documents/${id}/undelete`, { method: 'POST' });
    toast(`"${title}" restored`, 'success');
    pageTrash();
  } catch(e) { toast(e.message, 'error'); }
}

/* ── LLM Settings ───────────────────────────────────────────────────────────── */
let _settingsTimer = null;

// quantBase = the real Ollama tag a quant suffix actually resolves against for
// this model — verified directly against the registry (2026-07-06). Plain
// suffix-appending to `name` fails for every one of these; most need
// "-instruct-" inserted, and phi3.5/deepseek-r1 use their own naming entirely.
// quantLevels (optional) = which of QUANT_LEVELS' values this model actually
// publishes — omit to allow all of them. Every model here has all 5 except
// deepseek-r1, which only publishes q4_K_M/q8_0/fp16 (verified against the
// registry; q4_0 and q5_K_M 404 for this specific model).
const POPULAR_MODELS = [
  { name: 'llama3.2:1b',      label: 'Llama 3.2 1B',     size: '0.8 GB', note: 'Fastest',       quantBase: 'llama3.2:1b-instruct' },
  { name: 'llama3.2:3b',      label: 'Llama 3.2 3B',     size: '2.0 GB', note: 'Recommended',    quantBase: 'llama3.2:3b-instruct' },
  { name: 'phi3.5',           label: 'Phi-3.5 Mini',     size: '2.2 GB', note: 'Best reasoning', quantBase: 'phi3.5:3.8b-mini-instruct' },
  { name: 'gemma2:2b',        label: 'Gemma 2 2B',       size: '1.6 GB', note: '',               quantBase: 'gemma2:2b-instruct' },
  { name: 'qwen2.5:1.5b',     label: 'Qwen 2.5 1.5B',   size: '1.0 GB', note: '',                quantBase: 'qwen2.5:1.5b-instruct' },
  { name: 'qwen2.5:3b',       label: 'Qwen 2.5 3B',      size: '2.0 GB', note: '',               quantBase: 'qwen2.5:3b-instruct' },
  { name: 'mistral:7b',       label: 'Mistral 7B',       size: '4.1 GB', note: 'Higher quality', quantBase: 'mistral:7b-instruct' },
  { name: 'deepseek-r1:1.5b', label: 'DeepSeek-R1 1.5B', size: '1.1 GB', note: 'Reasoning',      quantBase: 'deepseek-r1:1.5b-qwen-distill',
    quantLevels: ['q4_K_M', 'q8_0', 'fp16'] },
];

// Ollama tags encode quantization as a suffix (e.g. llama3.2:3b-instruct-q4_0).
// Not every model publishes every level — an unknown tag just fails the pull.
const QUANT_LEVELS = [
  { value: '',        label: 'Default (as tagged)' },
  { value: 'q4_0',    label: 'Q4_0 — smallest, fastest' },
  { value: 'q4_K_M',  label: 'Q4_K_M — balanced (recommended)' },
  { value: 'q5_K_M',  label: 'Q5_K_M — higher quality' },
  { value: 'q8_0',    label: 'Q8_0 — larger, near-lossless' },
  { value: 'fp16',    label: 'FP16 — full precision, largest' },
];

function fmtBytes(b) {
  if (!b) return '';
  const gb = b / 1e9;
  return gb >= 1 ? gb.toFixed(1) + ' GB' : Math.round(b / 1e6) + ' MB';
}

function renderPullingRow(p) {
  if (p.status === 'error') {
    return `<div class="llm-model-row">
      <div class="llm-model-info">
        <span class="llm-model-name">${esc(p.name)}</span>
        <span class="llm-model-size" style="color:var(--c-danger)">⚠ ${esc(p.error || 'Pull failed')}</span>
      </div>
      <span style="display:flex;gap:6px">
        <button class="btn btn-sm btn-primary" onclick="fillPullInput('${esc(p.name)}')">Retry</button>
        <button class="btn btn-sm btn-danger" onclick="cancelPull('${esc(p.name)}')">Remove</button>
      </span>
    </div>`;
  }
  const pct   = p.percent ?? 0;
  const stage = (p.status || 'starting').replace(/^pulling /, 'downloading ');
  return `<div class="llm-model-row" style="flex-direction:column;align-items:stretch;gap:6px">
    <div class="llm-model-info">
      <span class="llm-model-name">${esc(p.name)}</span>
      ${p.model_size ? `<span class="llm-model-size">${fmtBytes(p.model_size)}</span>` : ''}
      <span class="badge badge-processing">Pulling</span>
    </div>
    <div class="proc-wrap" style="min-width:0">
      <div class="proc-bar-track">
        <div class="proc-bar-fill" style="width:${pct}%"></div>
      </div>
      <div class="proc-meta">
        <span class="proc-stage">${esc(stage)}</span>
        <span style="display:flex;align-items:center;gap:6px">
          <span class="proc-time">${p.percent != null ? pct.toFixed(0) + '%' : ''}</span>
          <button class="btn btn-xs btn-danger" onclick="cancelPull('${esc(p.name)}')">✕ Stop</button>
        </span>
      </div>
    </div>
  </div>`;
}

function modelRowHTML(m, active) {
  return `<div class="llm-model-row ${m.name === active ? 'llm-model-row-active' : ''}">
    <div class="llm-model-info">
      <span class="llm-model-name">${esc(m.name)}${m.quantization ? ` <span class="llm-model-quant">(${esc(m.quantization)})</span>` : ''}</span>
      ${fmtBytes(m.size) ? `<span class="llm-model-size">${fmtBytes(m.size)}</span>` : ''}
    </div>
    <div style="display:flex;gap:6px;align-items:center">
      ${m.name === active
        ? `<span class="badge badge-published">Active</span>`
        : `<button class="btn btn-sm btn-primary"
             onclick="activateModel('${esc(m.name)}')">Activate</button>`}
      <button class="btn btn-sm btn-danger" title="Remove model" ${m.name === active ? 'disabled' : ''}
        onclick="removeModel('${esc(m.name)}')">Remove</button>
    </div>
  </div>`;
}

function modelsListBodyHTML(modelsData) {
  const models  = modelsData.models  || [];
  const pulling = modelsData.pulling || [];
  const active  = modelsData.active;
  // Active model always first, with a visual break before the rest — so it
  // doesn't get lost among whatever order Ollama happens to return the list in.
  const activeModel = models.find(m => m.name === active);
  const otherModels = models.filter(m => m.name !== active);
  return `
    ${modelsData.error ? `<div style="padding:12px 20px;font-size:12.5px;color:var(--c-danger)">
      ⚠ Could not reach Ollama: ${esc(modelsData.error)}
    </div>` : ''}
    ${!models.length && !pulling.length
      ? '<div class="empty"><div class="empty-icon">🤖</div><p>No models installed. Pull one below.</p></div>'
      : `<div style="padding:4px 12px">
          ${activeModel ? modelRowHTML(activeModel, active) : ''}
          ${activeModel && (otherModels.length || pulling.length) ? '<div class="llm-model-separator"></div>' : ''}
          ${otherModels.map(m => modelRowHTML(m, active)).join('')}
          ${pulling.map(p => renderPullingRow(p)).join('')}
        </div>`}`;
}

// Poll only while the Settings page is still the one showing, so a background
// pull never yanks the user back here after they've navigated elsewhere.
function scheduleModelsPoll() {
  clearTimeout(_settingsTimer);
  _settingsTimer = setTimeout(refreshModelsList, 1200);
}

async function refreshModelsList() {
  if ((location.hash.split('?')[0] || '#/dashboard') !== '#/settings') return;
  const body = q('models-list-body');
  if (!body) return;
  let modelsData;
  try {
    modelsData = await apiFetch('/api/llm/models');
  } catch {
    scheduleModelsPoll();
    return;
  }
  body.innerHTML = modelsListBodyHTML(modelsData);
  if ((modelsData.pulling || []).length) scheduleModelsPoll();
}

function gpuStatusRowHTML(config) {
  const badges = {
    'gpu':         { cls: 'badge-completed', label: '🟢 GPU' },
    'gpu-partial': { cls: 'badge-processing', label: '🟡 Partial GPU' },
    'cpu':         { cls: 'badge-failed', label: '🔴 CPU only' },
    'unknown':     { cls: 'badge-outline', label: '⚪ Unknown' },
  };
  const b = badges[config.gpu] || badges.unknown;
  return `
    <div class="gpu-status-row">
      <span class="badge ${b.cls}">${b.label}</span>
      <span style="font-size:13px;color:var(--c-muted)">for the active model's last inference</span>
    </div>`;
}

async function pageSettings() {
  clearTimeout(_settingsTimer);
  let config, modelsData;
  try {
    [config, modelsData] = await Promise.all([
      apiFetch('/api/llm/config'),
      apiFetch('/api/llm/models'),
    ]);
  } catch(e) {
    q('content').innerHTML = `<div class="empty"><p>⚠ ${esc(e.message)}</p></div>`;
    return;
  }

  const pulling = modelsData.pulling || [];
  const active  = config.model;
  const matchesEnv = active === config.env_model;

  q('content').innerHTML = `
    <div class="page-header"><h1>LLM Settings</h1></div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><h2>Active Model</h2></div>
      <div style="padding:16px 20px">
        <div class="llm-active-row">
          <span class="llm-dot"></span>
          <span class="llm-active-name">${esc(active)}</span>
          ${matchesEnv
            ? `<span class="badge badge-published" style="margin-left:8px">Matches .env</span>`
            : `<span class="badge badge-review" style="margin-left:8px">Runtime override</span>`}
        </div>
        <p class="llm-note">
          Changes here take effect immediately but reset on container restart.
          To make permanent, set <code>OLLAMA_MODEL=${esc(active)}</code> in your <code>.env</code>
          and run <code>docker compose up -d retrieval-service</code>.
        </p>
        <div class="llm-advanced-header" onclick="toggleChatDisplaySettings()">
          <span id="chat-display-caret" class="llm-advanced-caret">▸</span> Chat display settings (Click to expand/collapse)
        </div>
        <div id="chat-display-settings" class="hidden">
          <div class="llm-toggle-row">
            <label class="switch">
              <input type="checkbox" id="show-model-toggle"
                     ${config.show_model_name ? 'checked' : ''}
                     onchange="toggleShowModelName(this.checked)">
              <span class="switch-slider"></span>
            </label>
            <div>
              <div class="llm-toggle-label">Show model name in chat header</div>
              <div class="llm-toggle-sub">
                When on, the Employee Portal's AI Assistant panel shows the active model name
                (e.g. "${esc(active)}") next to "Answers from the knowledge base".
              </div>
            </div>
          </div>
          <div class="llm-toggle-row">
            <label class="switch">
              <input type="checkbox" id="show-stats-toggle"
                     ${config.show_stats ? 'checked' : ''}
                     onchange="toggleShowStats(this.checked)">
              <span class="switch-slider"></span>
            </label>
            <div>
              <div class="llm-toggle-label">Show response stats</div>
              <div class="llm-toggle-sub">
                When on, each reply in the Employee Portal shows retrieval strategy, chunk count,
                model, and GPU/CPU underneath it.
              </div>
            </div>
          </div>
          <div class="llm-toggle-row">
            <label class="switch">
              <input type="checkbox" id="show-timing-toggle"
                     ${config.show_timing ? 'checked' : ''}
                     onchange="toggleShowTiming(this.checked)">
              <span class="switch-slider"></span>
            </label>
            <div>
              <div class="llm-toggle-label">Show time taken</div>
              <div class="llm-toggle-sub">
                When on, each reply in the Employee Portal shows how long it took to respond.
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-head">
        <h2>Installed Models</h2>
        <button class="btn btn-ghost btn-sm" onclick="pageSettings()">↺ Refresh</button>
      </div>
      <div id="models-list-body">${modelsListBodyHTML(modelsData)}</div>
    </div>

    <div class="card">
      <div class="card-head"><h2>Pull New Model</h2></div>
      <div style="padding:16px 20px;display:flex;flex-direction:column;gap:14px">
        <div>
          <p style="font-size:13px;color:var(--c-muted);margin-bottom:10px">
            Click a model to pre-fill the name, then hit Pull:
          </p>
          <div class="llm-chips">
            ${POPULAR_MODELS.map(m => `
              <button class="llm-chip" onclick="fillPullInput('${esc(m.name)}')">
                <span class="llm-chip-name">${esc(m.label)}</span>
                <span class="llm-chip-meta">${esc(m.size)}${m.note ? ' · ' + esc(m.note) : ''}</span>
              </button>`).join('')}
          </div>
        </div>
        <div style="display:flex;gap:8px">
          <input class="input" id="pull-input" placeholder="e.g. llama3.2:3b" style="flex:1" oninput="updateQuantOptions()">
          <select class="input" id="pull-quant" style="flex:0 0 auto;width:auto">
            ${QUANT_LEVELS.map(o => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('')}
          </select>
          <button class="btn btn-primary" id="pull-btn" onclick="doPull()">Pull Model</button>
        </div>
        <p style="font-size:12px;color:var(--c-muted)">
          The pull runs on the host Ollama instance. First download may take several minutes depending on model size.
          The list above refreshes automatically. For the models above, the quantization dropdown only offers levels
          verified to exist for that model. For anything else you type, quantization appends a guessed tag suffix —
          smaller/faster but lower quality — which may not exist for every model; check the exact tag on
          <a href="https://ollama.com/library" target="_blank" rel="noopener">ollama.com/library</a> if a pull fails
          with "manifest does not exist".
        </p>
      </div>
    </div>`;

  if (pulling.length) scheduleModelsPoll();
}

/* ── Hardware / GPU setup ───────────────────────────────────────────────────── */
async function pageGpuSetup() {
  let config, history;
  try {
    [config, history] = await Promise.all([
      apiFetch('/api/llm/config'),
      apiFetch('/api/llm/gpu-diagnostics'),
    ]);
  } catch(e) {
    q('content').innerHTML = `<div class="empty"><p>⚠ ${esc(e.message)}</p></div>`;
    return;
  }
  const entries = history.entries || [];

  q('content').innerHTML = `
    <div class="page-header"><h1>Hardware &amp; GPU Setup</h1></div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><h2>Detected from inside the stack</h2></div>
      <div style="padding:16px 20px">
        ${gpuStatusRowHTML(config)}
        <p class="gpu-status-detail">
          This reflects the <b>${esc(config.backend === 'vllm' ? 'vLLM' : 'Ollama')}</b> backend
          (<code>${esc(config.base_url)}</code>) for the currently active model. Because Docker
          isolates containers from the real host, EKAP can only report what the LLM backend
          itself sees — it can't inspect the host's OS or GPU driver directly. Use the script
          below on the actual host for a full check.
        </p>
        ${config.gpu === 'cpu' || config.gpu === 'unknown' ? `
        <div class="gpu-warning-box">
          ⚠ The active model does not appear to be using a GPU. If this machine has one,
          run the setup-check script below <b>on the machine that hosts Docker/Ollama</b> —
          it detects your OS and GPU driver setup and prints the exact commands to fix it
          (or confirms CPU-only is expected/correct for this hardware).
        </div>` : ''}
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><h2>Run the setup-check script</h2></div>
      <div style="padding:16px 20px">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <a class="btn btn-ghost btn-sm" href="docs/gpu-setup-check.sh" download>⬇ Download setup-check script</a>
          <span class="gpu-status-detail" style="margin:0">
            <code>chmod +x gpu-setup-check.sh && ./gpu-setup-check.sh</code>
          </span>
        </div>
        <p class="gpu-status-detail">
          On a headless server without a browser, fetch it directly instead:
          <code>curl -o gpu-setup-check.sh ${esc(location.origin)}/admin/docs/gpu-setup-check.sh</code>
        </p>
        <p class="gpu-status-detail">
          Then paste the output below and save it — visible here to any admin, with who ran it and when.
        </p>
        <textarea class="input" id="gpu-diag-input" rows="8"
          style="font-family:monospace;font-size:12px;width:100%;resize:vertical"
          placeholder="Paste the script's output here…"></textarea>
        <div style="margin-top:10px">
          <button class="btn btn-primary btn-sm" id="gpu-diag-save-btn" onclick="submitGpuDiagnostics()">Save Results</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>History</h2></div>
      ${!entries.length ? '<div class="empty"><div class="empty-icon">🖥</div><p>No results submitted yet.</p></div>' : `
      <div style="padding:8px 20px 16px">
        ${entries.map((e, i) => `
          <div style="padding:12px 0;${i > 0 ? 'border-top:1px solid var(--c-border)' : ''}">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:10px">
              <div>
                <b style="font-size:13px">${esc(e.submitted_by)}</b>
                <span class="gpu-status-detail" style="margin:0 0 0 8px">${fmtDateTime(e.submitted_at)}</span>
              </div>
              <button class="btn btn-ghost btn-sm" onclick="toggleDiagOutput('${esc(e.id)}')">View</button>
            </div>
            <pre id="diag-output-${esc(e.id)}" class="diag-output hidden">${esc(e.output)}</pre>
          </div>`).join('')}
      </div>`}
    </div>`;
}

function toggleDiagOutput(id) {
  q(`diag-output-${id}`).classList.toggle('hidden');
}

async function submitGpuDiagnostics() {
  const output = q('gpu-diag-input').value.trim();
  if (!output) { toast('Paste the script output first', 'error'); return; }
  const btn = q('gpu-diag-save-btn');
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    await apiFetch('/api/llm/gpu-diagnostics', { method: 'POST', body: JSON.stringify({ output }) });
    toast('Results saved', 'success');
    pageGpuSetup();
  } catch(e) {
    toast(e.message, 'error');
    btn.disabled = false; btn.textContent = 'Save Results';
  }
}

// Collapsed by default every time the Settings page loads — the toggles below
// are occasional/debug-facing, not something an admin needs open every visit.
function toggleChatDisplaySettings() {
  q('chat-display-settings').classList.toggle('hidden');
  const caret = q('chat-display-caret');
  caret.textContent = caret.textContent === '▸' ? '▾' : '▸';
}

function fillPullInput(name) {
  q('pull-input').value = name;
  q('pull-input').focus();
  updateQuantOptions();
}

// Restricts the quant dropdown to levels actually published for the currently
// typed/selected model (see POPULAR_MODELS.quantLevels); shows all of them for
// anything not in the curated list, since we don't know its real availability.
function updateQuantOptions() {
  const select = q('pull-quant');
  if (!select) return;
  const rawName = (q('pull-input')?.value || '').trim();
  const known    = POPULAR_MODELS.find(m => m.name === rawName);
  const allowed  = known?.quantLevels || null;
  const current  = select.value;
  select.innerHTML = QUANT_LEVELS
    .filter(o => !o.value || !allowed || allowed.includes(o.value))
    .map(o => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('');
  if ([...select.options].some(o => o.value === current)) select.value = current;
}

async function activateModel(name) {
  try {
    await apiFetch('/api/llm/config', {
      method: 'POST',
      body: JSON.stringify({ model: name }),
    });
    toast(`Active model switched to "${name}"`, 'success');
    pageSettings();
  } catch(e) { toast(e.message, 'error'); }
}

async function removeModel(name) {
  if (!confirm(`Remove model "${name}"? It will be deleted from disk — pull it again to use it later.`)) return;
  try {
    await apiFetch(`/api/llm/models/${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast(`Removed "${name}"`, 'success');
    pageSettings();
  } catch(e) { toast(e.message, 'error'); }
}

async function cancelPull(name) {
  try {
    const res = await apiFetch('/api/llm/pull/cancel', {
      method: 'POST',
      body: JSON.stringify({ model: name }),
    });
    toast(res.status === 'dismissed' ? `Removed "${name}"` : `Cancelling pull of "${name}"…`, 'info');
    pageSettings();
  } catch(e) { toast(e.message, 'error'); }
}

async function toggleShowModelName(checked) {
  const toggle = q('show-model-toggle');
  toggle.disabled = true;
  try {
    await apiFetch('/api/llm/config', {
      method: 'POST',
      body: JSON.stringify({ show_model_name: checked }),
    });
    toast(checked ? 'Model name will be shown in chat header' : 'Model name hidden in chat header', 'success');
  } catch(e) {
    toggle.checked = !checked;
    toast(e.message, 'error');
  } finally {
    toggle.disabled = false;
  }
}

async function toggleShowStats(checked) {
  const toggle = q('show-stats-toggle');
  toggle.disabled = true;
  try {
    await apiFetch('/api/llm/config', {
      method: 'POST',
      body: JSON.stringify({ show_stats: checked }),
    });
    toast(checked ? 'Response stats will be shown in chat' : 'Response stats hidden in chat', 'success');
  } catch(e) {
    toggle.checked = !checked;
    toast(e.message, 'error');
  } finally {
    toggle.disabled = false;
  }
}

async function toggleShowTiming(checked) {
  const toggle = q('show-timing-toggle');
  toggle.disabled = true;
  try {
    await apiFetch('/api/llm/config', {
      method: 'POST',
      body: JSON.stringify({ show_timing: checked }),
    });
    toast(checked ? 'Time taken will be shown in chat' : 'Time taken hidden in chat', 'success');
  } catch(e) {
    toggle.checked = !checked;
    toast(e.message, 'error');
  } finally {
    toggle.disabled = false;
  }
}

// Fallback for names not in POPULAR_MODELS (which carries verified exact tags
// instead — see quantBase above). Most Ollama instruct/chat models tag their
// quantized variants as "...-instruct-<quant>", not a bare "-<quant>" suffix
// (verified: 0/8 popular models resolved with a bare suffix, 5/7 resolved with
// "-instruct-" inserted) — so that's the best generic guess, not a guarantee.
function buildPullName(name, quant) {
  if (!quant) return name;
  if (!name.includes(':')) return `${name}:instruct-${quant}`;
  return name.endsWith('-instruct') ? `${name}-${quant}` : `${name}-instruct-${quant}`;
}

async function doPull() {
  const rawName = (q('pull-input').value || '').trim();
  if (!rawName) { toast('Enter a model name first', 'error'); return; }
  const quant = q('pull-quant').value;
  const known = POPULAR_MODELS.find(m => m.name === rawName);
  // Known chip models carry a verified-exact quantBase — suffix it directly,
  // don't run it back through the guessing heuristic (it doesn't fit all of
  // them, e.g. deepseek-r1's tag doesn't end in "-instruct").
  const name = (quant && known?.quantBase)
    ? `${known.quantBase}-${quant}`
    : buildPullName(rawName, quant);
  const btn = q('pull-btn');
  btn.disabled = true; btn.textContent = 'Starting…';
  try {
    await apiFetch('/api/llm/pull', {
      method: 'POST',
      body: JSON.stringify({ model: name }),
    });
    toast(`Pulling "${name}" — this may take a few minutes`, 'info');
    q('pull-input').value = '';
    setTimeout(pageSettings, 1500);
  } catch(e) { toast(e.message, 'error'); }
  finally { btn.disabled = false; btn.textContent = 'Pull Model'; }
}

/* ── Queue badge ────────────────────────────────────────────────────────────── */
function updateQueueBadge(n) {
  const b = q('queue-badge');
  if (n > 0) { b.textContent = n; b.classList.remove('hidden'); }
  else b.classList.add('hidden');
}

async function refreshQueueBadge() {
  try {
    const d = await apiFetch('/api/documents?lifecycle_state=review&limit=1');
    updateQueueBadge(d.total);
  } catch {}
}

/* ── Notifications ──────────────────────────────────────────────────────────── */
async function refreshNotifBadge() {
  try {
    const d = await apiFetch('/api/notifications/unread-count');
    const b = q('notif-badge');
    if (d.unread > 0) { b.textContent = d.unread; b.classList.remove('hidden'); }
    else b.classList.add('hidden');
  } catch {}
}

function toggleNotifPanel() {
  const panel = q('notif-panel');
  const opening = panel.classList.contains('hidden');
  panel.classList.toggle('hidden');
  if (opening) loadNotifications();
}

async function loadNotifications() {
  const list = q('notif-list');
  list.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div></div>';
  try {
    const d = await apiFetch('/api/notifications?limit=20');
    const items = d.notifications || [];
    list.innerHTML = !items.length
      ? '<div class="notif-empty">You\'re all caught up.</div>'
      : items.map(n => `
        <div class="notif-item ${n.read_at ? '' : 'unread'}" onclick='markNotificationRead(${attrJson(n.notification_id)})'>
          <div class="notif-item-msg">${esc(n.message)}</div>
          <div class="notif-item-time">${fmtDateTime(n.created_at)}</div>
        </div>`).join('');
  } catch(e) {
    list.innerHTML = `<div class="notif-empty">⚠ ${esc(e.message)}</div>`;
  }
}

async function markNotificationRead(id) {
  try {
    await apiFetch(`/api/notifications/${id}/read`, { method: 'POST' });
    await Promise.all([loadNotifications(), refreshNotifBadge()]);
  } catch(e) { toast(e.message, 'error'); }
}

async function markAllNotificationsRead() {
  try {
    await apiFetch('/api/notifications/read-all', { method: 'POST' });
    await Promise.all([loadNotifications(), refreshNotifBadge()]);
  } catch(e) { toast(e.message, 'error'); }
}

/* ── Init ───────────────────────────────────────────────────────────────────── */
function init() {
  if (!location.hash) location.hash = '#/dashboard';
  q('upload-form').addEventListener('submit', submitUpload);
  q('version-form').addEventListener('submit', submitVersionUpload);
  q('perm-form').addEventListener('submit', submitPermissionGrant);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      closeUpload(); closePreview(); closeWfModal(); closeVersionModal();
      closeAuditModal(); closePermissionsModal(); closeDuplicateModal();
      q('notif-panel').classList.add('hidden');
    }
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('.autocomplete')) q('perm-user-suggestions')?.classList.add('hidden');
    if (!e.target.closest('.notif-wrap')) q('notif-panel')?.classList.add('hidden');
  });
  window.addEventListener('hashchange', router);
  router();
  setInterval(refreshQueueBadge, 30000);
  refreshNotifBadge();
  setInterval(refreshNotifBadge, 30000);
}

init();
