'use strict';
/* ARM operations dashboard. No frameworks and no HTML parsing: every API string is
   untrusted (disc labels/titles come from physical discs) and goes in via
   textContent only. */

/* ---------- pure helpers (exported for tests) ---------- */

function fmtDuration(sec) {
  if (sec == null || !isFinite(sec) || sec < 0) return '–';
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return h + ' h ' + String(m).padStart(2, '0') + ' m';
  if (m) return m + ' m ' + String(s).padStart(2, '0') + ' s';
  return s + ' s';
}

/* minutes:seconds with unbounded minutes, e.g. 5999 -> "99:59" */
function fmtMinSec(sec) {
  if (sec == null || !isFinite(sec) || sec < 0) return '–';
  sec = Math.round(sec);
  return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
}

function relTime(nowSec, thenSec) {
  if (thenSec == null || !isFinite(thenSec)) return '–';
  const d = Math.max(0, Math.round(nowSec - thenSec));
  if (d < 5) return 'just now';
  if (d < 60) return d + ' s ago';
  if (d < 3600) return Math.floor(d / 60) + ' min ago';
  if (d < 86400) return Math.floor(d / 3600) + ' h ago';
  return Math.floor(d / 86400) + ' d ago';
}

function fmtAge(sec) {
  return sec == null || !isFinite(sec) ? '–' : relTime(sec, 0);
}

function fmtRate(mbps) {
  if (mbps == null || !isFinite(mbps)) return '–';
  return mbps.toFixed(mbps >= 100 ? 0 : 1) + ' MB/s';
}

function fmtGb(x) {
  if (x == null || !isFinite(x)) return '–';
  return x.toFixed(x >= 100 ? 0 : 1);
}

function fmtPct(x) {
  if (x == null || !isFinite(x)) return '–';
  return Math.max(0, Math.min(100, Math.round(x))) + '%';
}

function clampPct(x) {
  return x == null || !isFinite(x) ? 0 : Math.max(0, Math.min(100, x));
}

const SEV_ORDER = { critical: 0, warning: 1, info: 2 };
function sortIssues(list) {
  return list.slice().sort((a, b) =>
    (SEV_ORDER[a.severity] ?? 3) - (SEV_ORDER[b.severity] ?? 3) || (a.since ?? 0) - (b.since ?? 0));
}

function longestTitle(scan) {
  const t = scan && Array.isArray(scan.titles) ? scan.titles : [];
  let best = null;
  for (const x of t) if (best == null || (x.duration_s || 0) > best) best = x.duration_s || 0;
  return best;
}

/* "SAYANYTHING" / "SAY_ANYTHING_1989" -> text box prefill */
function prettyLabel(label) {
  return String(label || '').replace(/[_.]+/g, ' ').replace(/\s+/g, ' ').trim();
}

const STICKER = { ok: 'NEW RELEASE', warn: 'DUE', bad: 'LATE', run: 'NOW PLAYING', off: 'ON FILE' };
function stickerText(state) { return STICKER[stateLevel(state)]; }

function stateLevel(state) {
  const s = String(state || '').toLowerCase();
  if (/fail|error|reject|stuck/.test(s)) return 'bad';
  if (/await|wait|approval|need|pending|stale/.test(s)) return 'warn';
  if (/done|complete|success|imported|finished/.test(s)) return 'ok';
  if (/rip|transfer|copy|scan|verify|encod|process|running/.test(s)) return 'run';
  return 'off';
}

function parseYear(v) {
  const n = parseInt(String(v == null ? '' : v).trim(), 10);
  return n >= 1880 && n <= 2200 ? n : null;
}

function matchesRuntime(runtimeMin, longestSec, slackMin = 6) {
  if (runtimeMin == null || longestSec == null || !longestSec) return false;
  return Math.abs(runtimeMin - longestSec / 60) <= slackMin;
}

function eventKey(e) { return [e.ts, e.kind, e.fp, e.text].join('|'); }

function backoffDelay(base, fails, cap = 60000) {
  return Math.min(cap, base * Math.pow(2, Math.min(fails, 6)));
}

if (typeof module !== 'undefined') {
  module.exports = { fmtDuration, fmtMinSec, relTime, fmtAge, fmtRate, fmtGb, fmtPct, clampPct, sortIssues,
    longestTitle, prettyLabel, stateLevel, stickerText, parseYear, matchesRuntime, eventKey, backoffDelay };
}

/* ---------- browser app ---------- */
if (typeof document !== 'undefined') (function () {

const $ = (id) => document.getElementById(id);
const st = {
  csrf: null, skew: 0, issues: [], jobs: [], events: [], evKeys: new Set(), evSince: 0,
  filter: 'all', status: null, controllers: new Set(), tasks: [], sigs: {},
  panels: new Map(), hints: new Map(), authed: false,
};

function nowSec() { return Date.now() / 1000 + st.skew; }

/* tiny element builder: only textContent / setAttribute, never HTML parsing */
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  if (props) for (const k of Object.keys(props)) {
    const v = props[k];
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}
function clear(e) { while (e.firstChild) e.removeChild(e.firstChild); return e; }
function sameSig(key, data) { const s = JSON.stringify(data); if (st.sigs[key] === s) return true; st.sigs[key] = s; return false; }

/* ---------- API ---------- */
class ApiError extends Error { constructor(status, msg, headers) { super(msg); this.status = status; this.headers = headers; } }

async function api(method, path, body, opts = {}) {
  const ctl = new AbortController();
  st.controllers.add(ctl);
  try {
    const init = { method, credentials: 'same-origin', signal: ctl.signal, headers: { Accept: 'application/json' } };
    if (method !== 'GET') {
      init.headers['Content-Type'] = 'application/json';
      if (st.csrf) init.headers['X-CSRF-Token'] = st.csrf;
      init.body = JSON.stringify(body || {});
    }
    const r = await fetch(path, init);
    let data = null;
    try { data = await r.json(); } catch (_) { /* non-JSON */ }
    if (r.status === 401 && !opts.noAuthRedirect) { showLogin(); throw new ApiError(401, 'Signed out', r.headers); }
    if (!r.ok) throw new ApiError(r.status, (data && (data.error || data.message)) || ('HTTP ' + r.status), r.headers);
    return data;
  } finally { st.controllers.delete(ctl); }
}
function abortAll() { for (const c of st.controllers) c.abort(); st.controllers.clear(); }

/* ---------- toasts / modal / drawer ---------- */
function toast(msg, isErr) {
  const t = h('div', { class: 'toast' + (isErr ? ' error' : ''), text: msg });
  $('toasts').append(t);
  setTimeout(() => t.remove(), isErr ? 8000 : 4500);
}

let trapStack = [];
function openLayer(node, drawer, onClose) {
  const ov = $('overlay');
  const prev = document.activeElement;
  clear(ov).append(node);
  ov.classList.toggle('drawer-mode', !!drawer);
  ov.classList.remove('hidden');
  const layer = { node, onClose, prev };
  trapStack.push(layer);
  const first = node.querySelector('[data-autofocus]') || node.querySelector('button, input, [href]');
  if (first) first.focus();
  return layer;
}
function closeLayer(result) {
  const layer = trapStack.pop();
  if (!layer) return;
  const ov = $('overlay');
  ov.classList.add('hidden'); clear(ov);
  if (layer.prev && layer.prev.focus && document.contains(layer.prev)) layer.prev.focus();
  if (layer.onClose) layer.onClose(result);
}
document.addEventListener('keydown', (e) => {
  const layer = trapStack[trapStack.length - 1];
  if (!layer) return;
  if (e.key === 'Escape') { e.preventDefault(); closeLayer(false); return; }
  if (e.key === 'Tab') {
    const f = [...layer.node.querySelectorAll('button, input, a[href], [tabindex]:not([tabindex="-1"])')].filter((x) => !x.disabled && x.offsetParent !== null);
    if (!f.length) return;
    const a = f[0], z = f[f.length - 1];
    if (e.shiftKey && document.activeElement === a) { e.preventDefault(); z.focus(); }
    else if (!e.shiftKey && document.activeElement === z) { e.preventDefault(); a.focus(); }
    else if (!layer.node.contains(document.activeElement)) { e.preventDefault(); a.focus(); }
  }
});
$('overlay').addEventListener('mousedown', (e) => { if (e.target === $('overlay')) closeLayer(false); });

function confirmModal(message, okLabel) {
  return new Promise((resolve) => {
    const m = h('div', { class: 'modal', role: 'alertdialog', 'aria-modal': 'true', 'aria-labelledby': 'cm-t' },
      h('h3', { id: 'cm-t', text: 'Please confirm' }),
      h('p', { text: message }),
      h('div', { class: 'actions' },
        h('button', { type: 'button', class: 'btn', 'data-autofocus': '1', text: 'Cancel', onclick: () => closeLayer(false) }),
        h('button', { type: 'button', class: 'btn primary', text: okLabel || 'Confirm', onclick: () => closeLayer(true) })));
    openLayer(m, false, (r) => resolve(!!r));
  });
}

/* ---------- login / session ---------- */
function showLogin() {
  stopPolling(); abortAll(); st.authed = false; st.csrf = null;
  while (trapStack.length) closeLayer(false);
  $('app').classList.add('hidden'); $('offline').classList.add('hidden');
  $('login-view').classList.remove('hidden');
  $('login-token').value = '';
  $('login-token').focus();
}
function showApp() {
  st.authed = true;
  $('login-view').classList.add('hidden'); $('app').classList.remove('hidden');
  startPolling();
}
$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('login-error'); err.textContent = '';
  const btn = $('login-submit'); btn.disabled = true;
  try {
    const r = await api('POST', '/api/login', { token: $('login-token').value }, { noAuthRedirect: true });
    st.csrf = r.csrf; showApp();
  } catch (x) {
    if (x.status === 429) {
      const ra = x.headers && x.headers.get && x.headers.get('Retry-After');
      err.textContent = 'Too many attempts. ' + (ra ? 'Try again in ' + ra + ' s.' : 'Please wait a moment and try again.');
    } else if (x.status === 401 || x.status === 403) err.textContent = 'That token was not accepted.';
    else err.textContent = 'Could not sign in: ' + x.message;
  } finally { btn.disabled = false; }
});
$('logout').addEventListener('click', async () => {
  const csrf = st.csrf;
  stopPolling(); abortAll();
  try { st.csrf = csrf; await api('POST', '/api/logout', {}, { noAuthRedirect: true }); } catch (_) { /* ignore */ }
  showLogin();
});

/* ---------- theme ---------- */
function applyTheme(t) { if (t) document.documentElement.setAttribute('data-theme', t); else document.documentElement.removeAttribute('data-theme'); }
try { applyTheme(localStorage.getItem('arm-theme')); } catch (_) { /* storage unavailable */ }
try { const tq = new URLSearchParams(location.search).get('theme'); if (tq === 'dark' || tq === 'light') applyTheme(tq); } catch (_) { /* ignore */ }
$('theme').addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme')
    || (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const next = cur === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  try { localStorage.setItem('arm-theme', next); } catch (_) { /* ignore */ }
});

/* ---------- chips ---------- */
function chip(level, label, value, extra, title) {
  const ic = { ok: '✓', warn: '!', bad: '✕', off: '–' }[level];
  const li = h('li', { class: 'chip ' + level, title: title || null },
    h('span', { class: 'ic', 'aria-hidden': 'true', text: ic }),
    h('span', { text: label + ': ' + value }));
  if (extra) li.append(extra);
  return li;
}
function renderChips(s) {
  const ul = clear($('chips'));
  const svc = s.service || {}, drv = s.drive || {}, shr = s.share || {}, scr = s.scratch || {}, sv = s.servers || {};
  ul.append(chip(svc.active ? 'ok' : 'bad', 'Service', svc.active ? 'running' : (svc.state || 'down')));
  const tray = drv.tray || 'unknown';
  ul.append(chip(tray === 'unknown' ? 'warn' : 'ok', 'Drive', tray + (drv.disc_present ? ', disc present' : ', no disc')));
  ul.append(chip(shr.ok ? 'ok' : 'bad', 'Share', shr.ok ? 'reachable' : 'unreachable', null,
    shr.checked ? 'checked ' + relTime(nowSec(), shr.checked) : null));
  const used = scr.used_pct;
  const bar = h('span', { class: 'minibar', 'aria-hidden': 'true' }, h('span'));
  bar.firstChild.style.width = clampPct(used) + '%';
  ul.append(chip(used == null ? 'off' : used >= 90 ? 'bad' : used >= 80 ? 'warn' : 'ok', 'Scratch',
    used == null ? 'unknown' : fmtPct(used) + ' used', bar,
    scr.total_gb != null ? fmtGb(scr.free_gb) + ' GB free of ' + fmtGb(scr.total_gb) + ' GB; rips ' + fmtGb(scr.rips_gb) + ' GB' : null));
  for (const [k, name] of [['plex', 'Plex'], ['jellyfin', 'Jellyfin']]) {
    const x = sv[k] || {};
    ul.append(!x.configured ? chip('off', name, 'not configured') : chip(x.reachable ? 'ok' : 'bad', name, x.reachable ? 'reachable' : 'unreachable'));
  }
  ul.append((sv.tmdb || {}).configured ? chip('ok', 'TMDb', 'configured') : chip('warn', 'TMDb', 'not configured'));
  const sl = sv.slack || {};
  ul.append(sl.webhook || sl.bot_approvals
    ? chip('ok', 'Slack', sl.webhook && sl.bot_approvals ? 'alerts + approvals' : sl.webhook ? 'alerts only' : 'approvals only')
    : chip('off', 'Slack', 'off'));
}

/* ---------- now processing ---------- */
function renderNow(s) {
  const c = s.current;
  if (sameSig('now', c ? [c.fp, c.state, c.phase, Math.round(c.percent || 0), c.stale, c.title] : [s.drive]) ) return;
  const box = clear($('now'));
  if (!c) {
    const drv = s.drive || {};
    box.append(h('p', { class: 'pad muted', text: 'Idle — insert a disc.' + (drv.tray ? ' Tray: ' + drv.tray + (drv.disc_present ? ', disc present.' : '.') : '') }));
    return;
  }
  const pct = clampPct(c.percent);
  const bar = h('div', { class: 'bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(Math.round(pct)), 'aria-label': 'Progress' }, h('span'));
  bar.firstChild.style.width = pct + '%';
  const reel = () => h('span', { class: 'reel', 'aria-hidden': 'true' });
  const ctr = (label, value) => h('div', { class: 'counter' }, h('span', { class: 'c-label', text: label }), h('span', { class: 'c-val', text: value }));
  box.append(
    h('div', { class: 'now-title', text: c.title || 'Unknown title' }),
    h('div', { class: 'now-phase', text: 'Phase: ' + (c.phase || c.state || '–') }),
    h('div', { class: 'deck' + (c.stale ? '' : ' live') }, reel(), bar, reel()),
    h('div', { class: 'counters num' },
      ctr('Done', fmtPct(c.percent)), ctr('Rate', fmtRate(c.rate_mbps)),
      ctr('Time left', c.eta_s == null ? '–' : fmtDuration(c.eta_s)),
      ctr('Progress', fmtGb(c.done_gb) + ' / ' + fmtGb(c.total_gb) + ' GB')));
  if (c.stale) box.append(h('div', { class: 'stale', role: 'status', text: 'Warning: progress has not updated recently. The process may be stuck.' }));
}

/* ---------- issues ---------- */
async function postAction(fp, a, extraBody) {
  try {
    const r = await api('POST', a.path, extraBody || a.body || {});
    toast(r && r.message ? r.message : 'Done');
    refreshAll();
    return { ok: true };
  } catch (e) { if (e.status !== 401) toast(e.message, true); refreshAll(); return { ok: false, message: e.message }; }
}
async function runAction(issue, a, btn) {
  if (a.needs_input === 'identify') { toggleIdentify(issue, a); return; }
  if (a.confirm) { if (!(await confirmModal(typeof a.confirm === 'string' ? a.confirm : a.label + '?', a.label))) return; }
  btn.disabled = true;
  await postAction(issue.fp, a);
}

function renderIssues(list) {
  const root = $('issues');
  const sorted = sortIssues(list);
  $('attn-count').textContent = sorted.length ? String(sorted.length) : '';
  if (sameSig('issues', sorted)) { refreshSince(); return; }
  /* detach live identify panels so their input state survives a re-render */
  for (const p of st.panels.values()) if (p.el.parentNode) p.el.parentNode.removeChild(p.el);
  clear(root);
  if (!sorted.length) {
    root.append(h('div', { class: 'panel empty' }, h('span', { 'aria-hidden': 'true', text: '✓' }), h('span', { text: 'All clear — nothing needs your attention.' })));
    st.panels.clear(); return;
  }
  const live = new Set();
  for (const is of sorted) {
    const card = h('article', { class: 'card ' + is.severity, 'aria-labelledby': 'is-' + cssId(is.id) });
    card.append(h('div', { class: 'card-head' },
      h('span', { class: 'sev ' + is.severity, text: is.severity }),
      h('h3', { class: 'card-title', id: 'is-' + cssId(is.id), text: is.title })));
    if (is.detail) card.append(h('p', { class: 'card-detail', text: is.detail }));
    card.append(h('p', { class: 'card-since muted since', 'data-since': String(is.since ?? ''), text: is.since ? 'since ' + relTime(nowSec(), is.since) : '' }));
    const acts = h('div', { class: 'actions' });
    for (const a of is.actions || []) {
      const b = h('button', { type: 'button', class: 'btn' + (a.needs_input === 'identify' ? ' primary' : a.confirm ? ' danger' : ''), text: a.label });
      b.addEventListener('click', () => runAction(is, a, b));
      acts.append(b);
    }
    card.append(acts);
    const p = st.panels.get(is.id);
    if (p) { card.append(p.el); live.add(is.id); }
    root.append(card);
  }
  for (const k of [...st.panels.keys()]) if (!live.has(k)) st.panels.delete(k);
}
function cssId(s) { return String(s).replace(/[^A-Za-z0-9_-]/g, '_'); }
function refreshSince() {
  for (const e of document.querySelectorAll('.since')) { const t = parseFloat(e.dataset.since); if (t) e.textContent = 'since ' + relTime(nowSec(), t); }
}

/* ---------- identify panel ---------- */
function toggleIdentify(issue, action) {
  const ex = st.panels.get(issue.id);
  if (ex) { ex.el.remove(); st.panels.delete(issue.id); return; }
  const card = [...document.querySelectorAll('.card')].find((c) => c.getAttribute('aria-labelledby') === 'is-' + cssId(issue.id));
  const p = buildIdentify(issue, action);
  st.panels.set(issue.id, p);
  if (card) card.append(p.el);
  const q = p.el.querySelector('input'); if (q) q.focus();
}

function buildIdentify(issue, action) {
  const fp = issue.fp;
  const S = { picked: null, manual: false, longest: null, searching: false };
  const q = h('input', { type: 'text', id: 'iq-' + cssId(issue.id), 'aria-label': 'Movie title to search', placeholder: 'Title' });
  const yr = h('input', { id: 'iy-' + cssId(issue.id), type: 'text', inputmode: 'numeric', maxlength: '4', 'aria-label': 'Year (optional)', placeholder: 'Year' });
  const searchBtn = h('button', { type: 'button', class: 'btn', text: 'Search TMDb' });
  const hint = h('p', { class: 'hint', text: 'Looking up disc details…' });
  const status = h('p', { class: 'hint', role: 'status' });
  const results = h('ul', { class: 'results', 'aria-label': 'Search results' });
  const picked = h('p', { class: 'picked' });
  const mt = h('input', { type: 'text', 'aria-label': 'Your own title', placeholder: 'Title' });
  const my = h('input', { type: 'text', inputmode: 'numeric', maxlength: '4', 'aria-label': 'Your own year', placeholder: 'Year' });
  const manual = h('div', { class: 'row2 hidden' }, mt, my);
  const toggle = h('button', { type: 'button', class: 'link', text: 'Use my own title' });
  const resume = h('button', { type: 'button', class: 'btn primary', text: 'Resume', disabled: true });
  const cancel = h('button', { type: 'button', class: 'btn', text: 'Close' });
  const el = h('div', { class: 'ident', role: 'group', 'aria-label': 'Identify disc' },
    h('div', { class: 'row' },
      h('div', {}, h('label', { for: q.id, text: 'Search for the movie' }), q),
      h('div', {}, h('label', { for: yr.id, text: 'Year' }), yr),
      searchBtn),
    hint, status, results, picked, toggle, manual,
    h('div', { class: 'actions' }, resume, cancel));

  function refreshResume() {
    if (S.manual) resume.disabled = !mt.value.trim() || !parseYear(my.value);
    else resume.disabled = !S.picked;
  }
  function renderResults(list) {
    clear(results);
    if (!list.length) { status.textContent = 'No matches. Try fewer words, or use your own title.'; return; }
    status.textContent = list.length + ' result' + (list.length === 1 ? '' : 's') + '. Choose the right one.';
    for (const r of list) {
      const match = matchesRuntime(r.runtime, S.longest);
      const b = h('button', { type: 'button', class: 'result', 'aria-pressed': 'false' },
        h('span', { class: 'r-title', text: r.title + (r.year ? ' (' + r.year + ')' : '') }),
        h('span', { class: 'r-meta num' }, (r.runtime ? r.runtime + ' min' : 'runtime unknown') + (r.original_language ? ' · ' + r.original_language : ''),
          match ? h('span', { class: 'match', text: ' · matches longest title' }) : null),
        r.overview ? h('span', { class: 'r-over', text: r.overview, title: r.overview }) : null);
      b.addEventListener('click', () => {
        S.picked = r;
        for (const x of results.querySelectorAll('.result')) x.setAttribute('aria-pressed', x === b ? 'true' : 'false');
        picked.textContent = 'Selected: ' + r.title + (r.year ? ' (' + r.year + ')' : '') + ' · TMDb ' + r.tmdb_id;
        refreshResume();
      });
      results.append(h('li', {}, b));
    }
  }
  async function search() {
    const text = q.value.trim(); if (!text) { q.focus(); return; }
    searchBtn.disabled = true; status.textContent = 'Searching…';
    try {
      const y = parseYear(yr.value);
      const list = await api('GET', '/api/tmdb/search?q=' + encodeURIComponent(text) + (y ? '&year=' + y : ''));
      S.picked = null; picked.textContent = ''; refreshResume();
      renderResults(Array.isArray(list) ? list : []);
    } catch (e) { if (e.status !== 401) status.textContent = 'Search failed: ' + e.message; }
    finally { searchBtn.disabled = false; }
  }
  searchBtn.addEventListener('click', search);
  for (const i of [q, yr]) i.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); search(); } });
  toggle.addEventListener('click', () => {
    S.manual = !S.manual;
    manual.classList.toggle('hidden', !S.manual);
    toggle.textContent = S.manual ? 'Pick from search results instead' : 'Use my own title';
    if (S.manual && !mt.value) mt.value = q.value;
    if (S.manual && !my.value && parseYear(yr.value)) my.value = String(parseYear(yr.value));
    refreshResume();
  });
  mt.addEventListener('input', refreshResume);
  my.addEventListener('input', refreshResume);
  cancel.addEventListener('click', () => { el.remove(); st.panels.delete(issue.id); });
  resume.addEventListener('click', async () => {
    let body;
    if (S.manual) body = { title: mt.value.trim(), year: parseYear(my.value), tmdb_id: null };
    else if (S.picked) body = { title: S.picked.title, year: S.picked.year ? parseInt(S.picked.year, 10) : null, tmdb_id: S.picked.tmdb_id };
    else return;
    resume.disabled = true;
    const r = await postAction(fp, action, body);
    if (r.ok) { el.remove(); st.panels.delete(issue.id); return; }
    /* refused (bad title/year ...): keep the panel and what was typed, say why right here */
    status.textContent = 'Not accepted: ' + r.message;
    refreshResume();
  });

  /* prefill + duration hint from the job detail */
  (async () => {
    let label = '';
    try {
      const d = await api('GET', '/api/jobs/' + encodeURIComponent(fp));
      label = (d.scan && (d.scan.label || d.scan.disc_name)) || d.label || '';
      S.longest = longestTitle(d.scan);
      hint.textContent = S.longest
        ? 'Disc label: ' + (d.label || label || '–') + ' · longest title ' + fmtMinSec(S.longest) + ' — pick the movie with a similar runtime.'
        : 'Disc label: ' + (d.label || label || '–') + '. No title list available.';
    } catch (e) { hint.textContent = 'Could not load disc details.'; }
    if (!q.value) q.value = prettyLabel(label);
    if (q.value) search();
  })();
  return { el };
}

/* ---------- events ---------- */
function isWarn(e) { return e.level === 'warning' || e.level === 'error'; }
function renderEvents() {
  const ol = clear($('events'));
  const list = st.events.filter((e) => st.filter === 'all' || isWarn(e));
  if (!list.length) { ol.append(h('li', { class: 'ev-empty', text: st.filter === 'all' ? 'No activity yet.' : 'No warnings or errors.' })); return; }
  const now = nowSec();
  for (const e of list) {
    const ic = { info: 'i', warning: '!', error: '✕' }[e.level] || 'i';
    ol.append(h('li', { class: 'ev-' + (e.level || 'info') },
      h('span', { class: 'ev-ic', 'aria-label': e.level || 'info', title: e.level || 'info', text: ic }),
      h('span', { class: 'ev-text', text: e.text }),
      h('time', { class: 'ev-time num', title: new Date(e.ts * 1000).toLocaleString(), datetime: new Date(e.ts * 1000).toISOString(), text: relTime(now, e.ts) })));
  }
}
for (const b of document.querySelectorAll('.filter')) b.addEventListener('click', () => {
  st.filter = b.dataset.filter;
  for (const x of document.querySelectorAll('.filter')) x.setAttribute('aria-pressed', x === b ? 'true' : 'false');
  renderEvents();
});
async function pollEvents() {
  const first = st.evSince === 0;
  const list = await api('GET', '/api/events?limit=' + (first ? 100 : 200) + (first ? '' : '&since=' + encodeURIComponent(st.evSince)));
  let added = false;
  for (const e of Array.isArray(list) ? list : []) {
    const k = eventKey(e); if (st.evKeys.has(k)) continue;
    st.evKeys.add(k); st.events.unshift(e); added = true;
    if (e.ts > st.evSince) st.evSince = e.ts;
  }
  st.events.sort((a, b) => b.ts - a.ts);
  if (st.events.length > 300) { for (const e of st.events.splice(300)) st.evKeys.delete(eventKey(e)); }
  if (added || first) renderEvents(); else refreshTimes();
}
function refreshTimes() {
  const now = nowSec();
  for (const t of document.querySelectorAll('.ev-time')) { const d = Date.parse(t.getAttribute('datetime')); if (d) t.textContent = relTime(now, d / 1000); }
}

/* ---------- jobs / history ---------- */
function badge(state) {
  return h('span', { class: 'badge ' + stateLevel(state) }, h('b', { text: stickerText(state) }), h('small', { text: String(state || '–').replace(/_/g, ' ') }));
}
function renderJobs(list) {
  const body = clear($('jobs-body'));
  $('jobs-empty').classList.toggle('hidden', list.length > 0);
  $('jobs').classList.toggle('hidden', list.length === 0);
  for (const j of list) {
    const open = () => openDetail(j.fp);
    const tr = h('tr', { onclick: (e) => { if (!e.target.closest('button')) open(); } },
      h('td', { class: 'c-title', 'data-label': 'Title' }, h('button', { type: 'button', class: 'rowbtn', text: j.title || j.label || j.fp, onclick: open })),
      h('td', { 'data-label': 'State' }, badge(j.state)),
      h('td', { 'data-label': 'Action', text: j.action || '–' }),
      h('td', { 'data-label': 'Age', class: 'num', text: j.age_s == null ? '–' : fmtAge(j.age_s) }),
      h('td', { 'data-label': 'Message', text: j.message || '' }));
    body.append(tr);
  }
}

async function openDetail(fp) {
  const shell = h('aside', { class: 'drawer', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Job detail' }, h('p', { class: 'muted', text: 'Loading…' }));
  openLayer(shell, true);
  let d;
  try { d = await api('GET', '/api/jobs/' + encodeURIComponent(fp)); }
  catch (e) { clear(shell).append(drawerHead('Job detail'), h('p', { class: 'form-error', text: e.message })); return; }
  clear(shell);
  shell.append(drawerHead(d.title || d.label || fp));
  shell.append(h('p', {}, badge(d.state), ' ', d.message || ''));
  const kv = h('dl', { class: 'kv' });
  const add = (k, v) => { if (v) kv.append(h('dt', { text: k }), h('dd', { text: String(v) })); };
  add('Label', d.label); add('Identity', d.identity && (typeof d.identity === 'string' ? d.identity : JSON.stringify(d.identity)));
  add('Action', d.action); add('Target folder', d.target_folder); add('Existing', d.existing);
  add('Fingerprint', d.fp); add('Created', d.created ? new Date(d.created * 1000).toLocaleString() : '');
  if (d.paths && typeof d.paths === 'object') for (const k of Object.keys(d.paths)) add(k, d.paths[k]);
  shell.append(h('h3', { text: 'Details' }), kv);

  const hist = d.history || [];
  shell.append(h('h3', { text: 'Timeline' }));
  const tl = h('ol', { class: 'timeline' });
  for (const [ts, state, msg] of hist) tl.append(h('li', {}, h('b', { text: String(state).replace(/_/g, ' ') }), ' ', h('span', { class: 'muted num', title: new Date(ts * 1000).toLocaleString(), text: relTime(nowSec(), ts) }), msg ? h('div', { text: msg }) : null));
  shell.append(hist.length ? tl : h('p', { class: 'muted', text: 'No history recorded.' }));

  const plan = d.plan || {};
  const reasons = plan.reasons || [], warns = (plan.warnings || []).concat(d.warnings || []);
  if (reasons.length || warns.length) {
    shell.append(h('h3', { text: 'Plan' }));
    if (reasons.length) shell.append(h('ul', {}, reasons.map((r) => h('li', { text: String(r) }))));
    if (warns.length) shell.append(h('p', { class: 'muted', text: 'Warnings' }), h('ul', {}, warns.map((r) => h('li', { text: String(r) }))));
  }
  const titles = d.scan && d.scan.titles;
  if (titles && titles.length) {
    shell.append(h('h3', { text: 'Disc titles' }));
    const t = h('table', { class: 'titles num' }, h('thead', {}, h('tr', {}, ['#', 'Length', 'Size', 'Ch.', 'Audio', 'Subs', ''].map((x) => h('th', { scope: 'col', text: x })))));
    const tb = h('tbody');
    for (const x of titles) tb.append(h('tr', { class: x.is_selected ? 'sel' : null },
      h('td', { text: x.index }), h('td', { text: fmtMinSec(x.duration_s) }), h('td', { text: fmtGb(x.size_gb) + ' GB' }), h('td', { text: x.chapters ?? '' }),
      h('td', { text: (x.audio_langs || []).join(', ') }), h('td', { text: (x.sub_langs || []).join(', ') }), h('td', { text: x.is_selected ? 'selected' : '' })));
    t.append(tb); shell.append(t);
  }
  const f = shell.querySelector('.drawer-head button'); if (f) f.focus();
}
function drawerHead(title) {
  return h('div', { class: 'drawer-head' }, h('h2', { text: title }), h('button', { type: 'button', class: 'btn small', 'data-autofocus': '1', text: 'Close', onclick: () => closeLayer(false) }));
}

/* ---------- polling ---------- */
function setUpdated() { $('updated').textContent = 'Updated ' + new Date().toLocaleTimeString(); }
function conn() {
  const bad = st.tasks.some((t) => t.fails > 0);
  const b = $('offline');
  b.classList.toggle('hidden', !bad);
  if (bad) b.textContent = 'Connection lost — retrying. Data shown may be out of date.';
}
function makeTask(name, base, fn) {
  const t = { name, base, fn, fails: 0, timer: null, running: false, stopped: false };
  t.run = async () => {
    if (t.stopped || t.running) return;
    if (document.hidden) { t.schedule(); return; }
    t.running = true;
    try { await fn(); t.fails = 0; setUpdated(); }
    catch (e) { if (e.name === 'AbortError' || e.status === 401) { t.running = false; return; } t.fails++; }
    t.running = false; conn(); t.schedule();
  };
  t.schedule = () => { clearTimeout(t.timer); if (!t.stopped) t.timer = setTimeout(t.run, backoffDelay(t.base, t.fails)); };
  return t;
}
async function pollStatus() {
  const [s, issues] = await Promise.all([api('GET', '/api/status'), api('GET', '/api/issues')]);
  if (s.now) st.skew = s.now - Date.now() / 1000;
  st.status = s;
  renderChips(s); renderNow(s); renderIssues(Array.isArray(issues) ? issues : []);
}
async function pollJobs() { const l = await api('GET', '/api/jobs'); st.jobs = Array.isArray(l) ? l : []; renderJobs(st.jobs); }
function startPolling() {
  stopPolling();
  st.evSince = 0; st.events = []; st.evKeys = new Set(); st.sigs = {};
  st.tasks = [makeTask('status', 4000, pollStatus), makeTask('events', 4000, pollEvents), makeTask('jobs', 10000, pollJobs)];
  for (const t of st.tasks) { t.stopped = false; t.run(); }
}
function stopPolling() { for (const t of st.tasks) { t.stopped = true; clearTimeout(t.timer); } st.tasks = []; }
function refreshAll() { for (const t of st.tasks) { clearTimeout(t.timer); t.run(); } }
document.addEventListener('visibilitychange', () => { if (!document.hidden && st.authed) refreshAll(); });
$('refresh').addEventListener('click', refreshAll);

/* ---------- boot ---------- */
(async function boot() {
  try {
    const s = await api('GET', '/api/session', null, { noAuthRedirect: true });
    if (s && s.authenticated) { st.csrf = s.csrf; showApp(); } else showLogin();
  } catch (e) { showLogin(); $('login-error').textContent = 'Cannot reach the server.'; }
})();

})();
