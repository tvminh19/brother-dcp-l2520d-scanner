/* Scanner UI — frontend logic */
'use strict';

const $ = (id) => document.getElementById(id);
const state = { settings: null, doc: { pages: [] }, poll: null, lb: -1 };
let tpl;

/* ---------------- api helpers ---------------- */
async function api(url, body, method) {
  const opts = { method: method || (body ? 'POST' : 'GET') };
  if (body) { opts.headers = { 'Content-Type': 'application/json' }; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  let d = {};
  try { d = await r.json(); } catch (e) { /* non-json */ }
  if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = msg;
  $('toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 4200);
}

function setStatus(el, text, kind) {
  el.className = 'status ' + (kind || 'idle');
  el.textContent = text;
}

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
  return (bytes / 1048576).toFixed(2) + ' MB';
}

/* ---------------- boot / settings ---------------- */
async function loadSettings() {
  state.settings = await api('/api/settings');
  const s = state.settings;

  $('resolution').innerHTML = s.dpi.map(d =>
    `<option value="${d}"${d === 300 ? ' selected' : ''}>${d} DPI${d === 200 ? ' (fast)' : d === 600 ? ' (sharp, slow)' : ''}</option>`).join('');
  $('colorMode').innerHTML = s.color_modes.map(c =>
    `<option value="${c.id}">${c.label}</option>`).join('');
  $('pageSize').innerHTML = s.page_sizes.map(p =>
    `<option value="${p}"${p === 'a4' ? ' selected' : ''}>${p.toUpperCase()}</option>`).join('');

  const fOpts = s.filters.map(f => `<option value="${f.id}">${f.label}</option>`).join('');
  $('autoFilter').innerHTML = fOpts;
  $('autoFilter').value = 'none';
  $('bulkFilter').innerHTML = s.filters.filter(f => f.id !== 'none')
    .map(f => `<option value="${f.id}">${f.label}</option>`).join('');

  $('ocrLang').innerHTML = s.ocr_langs.map(l =>
    `<option value="${l.id}">${l.label}</option>`).join('');
}

/* ---------------- device presence ---------------- */
async function checkDevice() {
  try {
    const d = await api('/api/device');
    $('devDot').className = 'dot ' + (d.present ? 'on' : 'off');
    $('devText').textContent = d.present ? 'connected' : 'not found';
  } catch (e) {
    $('devDot').className = 'dot off';
    $('devText').textContent = 'connection error';
  }
}

/* ---------------- scanning ---------------- */
async function startScan() {
  const btn = $('scanBtn');
  if (btn.disabled) return;
  btn.disabled = true;
  $('cancelBtn').classList.remove('hidden');
  $('bar').style.width = '0%';
  setStatus($('status'), '⏳ Scanning… (keep the scanner still)', 'busy');

  try {
    const body = {
      resolution: parseInt($('resolution').value, 10),
      colorMode: $('colorMode').value,
      pageSize: $('pageSize').value,
      filter: $('autoFilter').value,
    };
    const r = await api('/api/scan', body);
    if (r.status === 'busy') { toast(r.error, 'warn'); endScan(); return; }
    state.poll = setInterval(pollStatus, 1200);
  } catch (e) {
    toast('Could not start scan: ' + e.message, 'err');
    endScan();
  }
}

function endScan() {
  if (state.poll) { clearInterval(state.poll); state.poll = null; }
  $('scanBtn').disabled = false;
  $('cancelBtn').classList.add('hidden');
  $('bar').style.width = '0%';
}

async function pollStatus() {
  let s;
  try { s = await api('/api/status'); } catch (e) { return; }

  if (s.state === 'scanning') {
    const stage = s.stage ? ' · ' + s.stage : '';
    setStatus($('status'), `⏳ Scanning… ${s.progress}%${stage}`, 'busy');
    $('bar').style.width = s.progress + '%';
    return;
  }

  if (s.state === 'done') {
    endScan();
    setStatus($('status'), '✅ Added 1 page', 'done');
    if (s.blank) toast('This page looks nearly blank — check the paper', 'warn');
    else toast('New page added', 'ok');
    await loadDoc();
    return;
  }

  if (s.state === 'error') {
    endScan();
    setStatus($('status'), '❌ ' + s.error, 'error');
    toast(s.error, 'err');
  }
}

async function cancelScan() {
  await api('/api/scan/cancel', {});
  setStatus($('status'), 'Stopping…', 'busy');
}

/* ---------------- document ---------------- */
async function loadDoc() {
  const d = await api('/api/doc');
  state.doc = d;
  renderDoc();
  if (!$('docName').value) $('docName').placeholder = 'Document ' + d.id;
}

function renderDoc() {
  const wrap = $('pages');
  const pages = state.doc.pages || [];
  $('pageCount').textContent = pages.length;
  $('empty').classList.toggle('hidden', pages.length > 0);
  wrap.innerHTML = '';

  pages.forEach((p, idx) => {
    const node = tpl.content.firstElementChild.cloneNode(true);
    node.dataset.id = p.id;
    node.dataset.idx = idx;

    node.querySelector('.pnum').textContent = 'Trang ' + p.n;

    const flags = node.querySelector('.pflags');
    let fh = '';
    if (p.rot || p.fine_rot) fh += `<span class="flag">${Math.round(p.rot + p.fine_rot)}°</span>`;
    if (p.filter && p.filter !== 'none') fh += `<span class="flag">${p.filter_label}</span>`;
    if (p.blank) fh += '<span class="flag warn">blank?</span>';
    flags.innerHTML = fh;

    const img = node.querySelector('.pthumb');
    img.src = p.thumb;
    img.addEventListener('click', (e) => { e.stopPropagation(); openLb(idx); });

    const sel = node.querySelector('.pfilter');
    sel.innerHTML = state.settings.filters.map(f =>
      `<option value="${f.id}"${f.id === p.filter ? ' selected' : ''}>${f.label}</option>`).join('');
    sel.addEventListener('click', e => e.stopPropagation());
    sel.addEventListener('change', async () => {
      try {
        await api(`/api/page/${p.id}/filter`, { filter: sel.value });
        toast('Filter applied: ' + sel.options[sel.selectedIndex].text, 'ok');
        await loadDoc();
      } catch (err) { toast(err.message, 'err'); }
    });

    const meta = p.meta || {};
    node.querySelector('.pmeta').textContent =
      `${p.w}×${p.h}px · ${meta.resolution || '?'}dpi · ${p.ts || ''}`;

    node.addEventListener('click', () => openLb(idx));
    node.querySelectorAll('[data-act]').forEach(b =>
      b.addEventListener('click', (e) => {
        e.stopPropagation();
        pageAction(p.id, b.dataset.act);
      }));

    attachDrag(node);
    wrap.appendChild(node);
  });
}

async function pageAction(pid, act) {
  try {
    if (act === 'rotL') await api(`/api/page/${pid}/rotate`, { delta: -90 });
    if (act === 'rotR') await api(`/api/page/${pid}/rotate`, { delta: 90 });
    if (act === 'deskew') {
      const r = await api(`/api/page/${pid}/deskew`, {});
      toast(Math.abs(r.angle) < 0.05 ? 'Page is already straight' : `Straightened by ${r.angle.toFixed(2)}°`);
    }
    if (act === 'dup') { await api(`/api/page/${pid}/duplicate`, {}); toast('Page duplicated', 'ok'); }
    if (act === 'del') {
      if (!confirm('Delete this page?')) return;
      await api(`/api/page/${pid}/delete`, {});
      toast('Page deleted');
    }
    await loadDoc();
    if (act === 'del' || act === 'dup') refreshLb();
  } catch (e) { toast(e.message, 'err'); }
}

async function bulkFilter() {
  const f = $('bulkFilter').value;
  const pages = state.doc.pages || [];
  if (!pages.length) return toast('No pages yet', 'warn');
  for (const p of pages) await api(`/api/page/${p.id}/filter`, { filter: f });
  toast(`Filtered ${pages.length} page(s)`, 'ok');
  await loadDoc();
}

async function bulkRotate() {
  const pages = state.doc.pages || [];
  if (!pages.length) return toast('No pages yet', 'warn');
  for (const p of pages) await api(`/api/page/${p.id}/rotate`, { delta: 90 });
  toast(`Rotated ${pages.length} page(s)`, 'ok');
  await loadDoc();
}

async function clearAll() {
  if (!(state.doc.pages || []).length) return;
  if (!confirm('Delete every page in this document?')) return;
  await api('/api/doc/clear', {});
  toast('Document cleared');
  await loadDoc();
}

/* ---------------- export ---------------- */
async function exportNow() {
  const pages = state.doc.pages || [];
  if (!pages.length) return toast('No pages to export', 'warn');

  const btn = $('exportBtn');
  btn.disabled = true;
  const fmt = $('fmt').value;
  const ocrOn = fmt === 'pdf' && $('ocr').checked;
  setStatus($('exportStatus'), ocrOn ? '⏳ Exporting + OCR (may take a while)…' : '⏳ Exporting…', 'busy');

  try {
    const r = await api('/api/export', {
      format: fmt,
      name: $('docName').value.trim(),
      mode: $('outMode').value || null,
      quality: parseInt($('quality').value, 10),
      ocr: ocrOn,
      lang: $('ocrLang').value,
      target: $('target').value,
    });
    setStatus($('exportStatus'), `✅ ${r.file} (${fmtSize(r.size)})`, 'done');
    toast(`Exported ${r.pages} page(s) · ${fmtSize(r.size)}`, 'ok');
    await loadExports();
    window.location.href = r.url;
  } catch (e) {
    setStatus($('exportStatus'), '❌ ' + e.message, 'error');
    toast('Export failed: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

async function loadExports() {
  const list = await api('/api/exports');
  const wrap = $('exportList');
  if (!list.length) { wrap.innerHTML = '<span class="muted">No files yet</span>'; return; }
  wrap.innerHTML = list.slice(0, 25).map(f => `
    <div class="fileitem">
      <span class="fname" title="${f.name}">${f.name}</span>
      <span class="muted">${fmtSize(f.size)}</span>
      <a href="${f.url}">⬇</a>
    </div>`).join('');
}

/* ---------------- lightbox ---------------- */
function openLb(idx) {
  const pages = state.doc.pages || [];
  if (idx < 0 || idx >= pages.length) return;
  state.lb = idx;
  $('lbImg').src = `/api/page/${pages[idx].id}/view.png?v=${Date.now()}`;
  $('lbInfo').textContent = `Trang ${idx + 1}/${pages.length} — ${pages[idx].filter_label}`;
  $('lightbox').classList.remove('hidden');
}

function closeLb() { $('lightbox').classList.add('hidden'); state.lb = -1; }
function lbStep(d) {
  const n = (state.doc.pages || []).length;
  if (!n) return;
  openLb((state.lb + d + n) % n);
}
function refreshLb() {
  if (state.lb >= 0) {
    const n = (state.doc.pages || []).length;
    if (!n) return closeLb();
    openLb(Math.min(state.lb, n - 1));
  }
}

/* ---------------- drag & drop reorder ---------------- */
let dragId = null;
function attachDrag(node) {
  node.addEventListener('dragstart', (e) => {
    dragId = node.dataset.id;
    node.classList.add('dragging');
  });
  node.addEventListener('dragend', () => {
    node.classList.remove('dragging');
    document.querySelectorAll('.page.drop').forEach(p => p.classList.remove('drop'));
  });
  node.addEventListener('dragover', (e) => { e.preventDefault(); node.classList.add('drop'); });
  node.addEventListener('dragleave', () => node.classList.remove('drop'));
  node.addEventListener('drop', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    node.classList.remove('drop');
    const targetId = node.dataset.id;
    if (!dragId || dragId === targetId) return;

    const order = (state.doc.pages || []).map(p => p.id).filter(i => i !== dragId);
    const at = order.indexOf(targetId);
    order.splice(at, 0, dragId);
    try {
      await api('/api/doc/reorder', { order });
      await loadDoc();
    } catch (err) { toast(err.message, 'err'); }
  });
}

/* ---------------- init ---------------- */
function bindUi() {
  $('scanBtn').addEventListener('click', startScan);
  $('cancelBtn').addEventListener('click', cancelScan);
  $('exportBtn').addEventListener('click', exportNow);
  $('bulkFilterBtn').addEventListener('click', bulkFilter);
  $('bulkRotateBtn').addEventListener('click', bulkRotate);
  $('clearBtn').addEventListener('click', clearAll);
  $('lbClose').addEventListener('click', closeLb);
  $('lbPrev').addEventListener('click', () => lbStep(-1));
  $('lbNext').addEventListener('click', () => lbStep(1));
  $('lightbox').addEventListener('click', (e) => { if (e.target.id === 'lightbox') closeLb(); });

  $('quality').addEventListener('input', () => { $('qualityVal').textContent = $('quality').value; });

  $('fmt').addEventListener('change', () => {
    const isPdf = $('fmt').value === 'pdf';
    $('ocrWrap').classList.toggle('hidden', !isPdf);
    $('ocrLangWrap').classList.toggle('hidden', !isPdf || !$('ocr').checked);
    $('targetWrap').classList.toggle('hidden', !isPdf);
    $('imgNote').classList.toggle('hidden', isPdf);
  });
  $('ocr').addEventListener('change', () => {
    $('ocrLangWrap').classList.toggle('hidden', !$('ocr').checked);
  });

  $('docName').addEventListener('change', () => {
    const v = $('docName').value.trim();
    state.doc.name = v;
    fetch('/api/doc/name', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: v }),
    }).catch(() => {});
  });

  document.addEventListener('keydown', (e) => {
    const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName);
    if (e.key === 'Escape') return closeLb();
    if (state.lb >= 0 && e.key === 'ArrowLeft') return lbStep(-1);
    if (state.lb >= 0 && e.key === 'ArrowRight') return lbStep(1);
    if (typing) return;
    if (e.code === 'Space') { e.preventDefault(); startScan(); }
  });
}

async function boot() {
  tpl = $('pageTpl');
  bindUi();
  try { await loadSettings(); } catch (e) { toast('Could not load settings: ' + e.message, 'err'); }
  await checkDevice();
  setInterval(checkDevice, 15000);
  try { await loadDoc(); } catch (e) { toast('Could not load document: ' + e.message, 'err'); }
  try { await loadExports(); } catch (e) { /* ignore */ }
  try {
    const s = await api('/api/status');
    if (s.state === 'scanning') { $('scanBtn').disabled = true; state.poll = setInterval(pollStatus, 1200); }
  } catch (e) { /* ignore */ }
}

boot();
