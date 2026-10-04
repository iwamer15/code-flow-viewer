/* Code Flow Viewer - フロントエンド (ビルド不要の素の JS) */
'use strict';

// ---------------------------------------------------------------------------
// ライブラリ読み込み (vendor/ にあればオフラインで、無ければ CDN から)
// ---------------------------------------------------------------------------
const CDN = 'https://cdn.jsdelivr.net/npm/';
const LIBS = [
  ['vendor/cytoscape.min.js', CDN + 'cytoscape@3.30.2/dist/cytoscape.min.js'],
  ['vendor/dagre.min.js', CDN + 'dagre@0.8.5/dist/dagre.min.js'],
  ['vendor/cytoscape-dagre.js', CDN + 'cytoscape-dagre@2.5.0/cytoscape-dagre.js'],
  ['vendor/mermaid.min.js', CDN + 'mermaid@11.4.1/dist/mermaid.min.js'],
];
const MONACO_LOCAL = 'vendor/monaco/vs';
const MONACO_CDN = CDN + 'monaco-editor@0.52.2/min/vs';

function loadScript(src) {
  return new Promise((ok, ng) => {
    const s = document.createElement('script');
    s.src = src; s.onload = () => ok(src); s.onerror = () => { s.remove(); ng(new Error(src)); };
    document.head.appendChild(s);
  });
}
async function loadFirst(paths) {
  for (const p of paths) { try { return await loadScript(p); } catch (e) { /* 次へ */ } }
  throw new Error('ライブラリを読み込めません: ' + paths[paths.length - 1]);
}
async function exists(url) {
  try { const r = await fetch(url, { method: 'HEAD' }); return r.ok; } catch (e) { return false; }
}

// ---------------------------------------------------------------------------
// 状態
// ---------------------------------------------------------------------------
const S = {
  data: null, sym: {}, out: new Map(), inc: new Map(), files: new Map(),
  view: 'flow', entry: null, depth: 4, dir: 'TB', showExt: false, expanded: new Set(),
  selected: null, chartSym: null, chartStack: [], scope: '',
  tabs: [], active: null, models: new Map(), stamp: null, scanning: false, collapsed: new Set(),
};
let cy = null, editor = null, monacoReady = null, mermaidId = 0;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const base = (p) => p.split('/').pop();

const LANG_COLOR = {
  python: '#3572A5', javascript: '#e8c400', typescript: '#3178c6', java: '#b07219', kotlin: '#A97BFF',
  scala: '#c22d40', c: '#7a7a7a', cpp: '#f34b7d', csharp: '#178600', go: '#00ADD8', rust: '#dea584',
  swift: '#F05138', php: '#4F5D95', dart: '#00B4AB', r: '#198CE7', powershell: '#2b5797', ruby: '#a01e1e',
  lua: '#3b3bb3', shell: '#4eaa25',
};
const KIND_MARK = { function: 'ƒ', method: 'm', class: 'C', toplevel: '▶' };

function toast(msg, bad = false, ms = 2600) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'toast' + (bad ? ' bad' : '');
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.add('hidden'), ms);
}
function busy(on, text = '解析中…') { $('#busy').classList.toggle('hidden', !on); $('#busy span').textContent = text; }

async function api(path, opts = {}) {
  const r = await fetch(path, opts.body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(opts.body) } : {});
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok || j.error) { const err = new Error(j.error || r.statusText); err.status = r.status; err.data = j; throw err; }
  return j;
}

function isDark() {
  const t = document.documentElement.dataset.theme;
  if (t) return t === 'dark';
  return matchMedia('(prefers-color-scheme: dark)').matches;
}
function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
function hashHue(s) { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) | 0; return Math.abs(h) % 360; }
function fileColors(path) {
  const h = hashHue(path);
  return isDark() ? { bg: `hsl(${h}, 32%, 22%)`, bd: `hsl(${h}, 55%, 55%)` } : { bg: `hsl(${h}, 70%, 94%)`, bd: `hsl(${h}, 50%, 45%)` };
}
function symLabel(s) {
  if (s.k === 'toplevel') return `▶ ${base(s.f)} ${s.q === '__main__' ? '(__main__)' : '(起動)'}`;
  return s.q;
}

// ---------------------------------------------------------------------------
// スキャン
// ---------------------------------------------------------------------------
async function scan(root, { keepView = false } = {}) {
  if (S.scanning) return;
  S.scanning = true; busy(true);
  try {
    const d = await api('/api/scan', { body: root ? { root } : {} });
    ingest(d);
    if (!keepView || !S.sym[S.entry]) S.entry = S.data.entries[0]?.id || null;
    fillEntries();
    renderFileTree();
    render();
    const st = d.stats;
    const langs = Object.entries(st.by_lang).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k} ${v}`).join(' · ');
    $('#stats').textContent = `${st.analyzed}/${st.files} ファイル · 関数 ${st.symbols} · 呼び出し ${st.edges} · ${st.ms.total}ms (再解析 ${st.reparsed}) · ${langs}`;
    $('#stats').title = `走査 ${st.ms.walk}ms / 解析 ${st.ms.parse}ms / グラフ ${st.ms.graph}ms`;
    $('#empty').classList.add('hidden');
    try { localStorage.setItem('cf.root', d.root); } catch (e) { /* 無視 */ }
    S.stamp = (await api('/api/stamp')).stamp;
  } catch (e) {
    toast(e.message, true, 5000);
  } finally {
    S.scanning = false; busy(false);
  }
}

function ingest(d) {
  S.data = d; S.sym = d.symbols; S.files = new Map(d.files.map((f) => [f.p, f]));
  $('#rootInput').value = d.root;
  S.out = new Map(); S.inc = new Map();
  for (const [from, to, line, flag, count] of d.edges) {
    if (!S.out.has(from)) S.out.set(from, []);
    S.out.get(from).push({ to, line, flag, count });
    if (!S.inc.has(to)) S.inc.set(to, []);
    S.inc.get(to).push({ from, line, flag });
  }
  for (const l of S.out.values()) l.sort((a, b) => a.line - b.line);
  const dl = $('#symList');
  dl.innerHTML = Object.entries(S.sym).filter(([, s]) => s.k !== 'class').slice(0, 5000)
    .map(([id, s]) => `<option value="${esc(s.q)} — ${esc(s.f)}:${s.s}">`).join('');
}

function fillEntries() {
  const sel = $('#entrySel');
  const groups = { top: [], other: [] };
  for (const e of S.data.entries) {
    const s = S.sym[e.id]; if (!s) continue;
    (e.score >= 30 ? groups.top : groups.other).push(`<option value="${esc(e.id)}" title="${esc(e.why)}">${esc(symLabel(s))} — ${esc(s.f)}</option>`);
  }
  // 推定エントリー以外も選べるように
  sel.innerHTML = (groups.top.length ? `<optgroup label="推定エントリーポイント">${groups.top.join('')}</optgroup>` : '')
    + (groups.other.length ? `<optgroup label="呼び出し元のない関数">${groups.other.slice(0, 400).join('')}</optgroup>` : '');
  if (S.entry && S.sym[S.entry] && !sel.querySelector(`option[value="${CSS.escape(S.entry)}"]`)) {
    const s = S.sym[S.entry];
    sel.insertAdjacentHTML('afterbegin', `<option value="${esc(S.entry)}">${esc(symLabel(s))} — ${esc(s.f)}</option>`);
  }
  if (S.entry) sel.value = S.entry;
}

// ---------------------------------------------------------------------------
// ファイルツリー
// ---------------------------------------------------------------------------
function renderFileTree() {
  const box = $('#fileTree');
  if (!S.data) { box.innerHTML = ''; return; }
  const q = $('#fileFilter').value.trim().toLowerCase();
  const root = { dirs: new Map(), files: [] };
  for (const f of S.data.files) {
    let symHit = false;
    if (q) {
      const pathHit = f.p.toLowerCase().includes(q);
      symHit = (f.sym || []).some((id) => S.sym[id]?.q.toLowerCase().includes(q));
      if (!pathHit && !symHit) continue;
    }
    const parts = f.p.split('/');
    let node = root;
    for (const d of parts.slice(0, -1)) {
      if (!node.dirs.has(d)) node.dirs.set(d, { dirs: new Map(), files: [] });
      node = node.dirs.get(d);
    }
    node.files.push({ f, symHit });
  }
  const out = [];
  const walk = (node, path, depth) => {
    for (const [name, child] of [...node.dirs.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      const p = path ? `${path}/${name}` : name;
      const closed = S.collapsed.has(p) && !q;
      out.push(`<div class="tnode dir" data-dir="${esc(p)}" style="padding-left:${8 + depth * 12}px"><span class="tw">${closed ? '▸' : '▾'}</span><span class="nm">${esc(name)}</span></div>`);
      if (!closed) walk(child, p, depth + 1);
    }
    for (const { f, symHit } of node.files.sort((a, b) => a.f.p.localeCompare(b.f.p))) {
      const name = base(f.p);
      const syms = (f.sym || []).map((id) => S.sym[id]).filter((s) => s && s.k !== 'class');
      const open = (S.active === f.p) || symHit;
      const color = f.lang ? LANG_COLOR[f.lang] || '#888' : 'transparent';
      out.push(`<div class="tnode file${f.lang ? '' : ' dim'}${f.err ? ' err' : ''}${S.active === f.p ? ' on' : ''}" data-file="${esc(f.p)}" style="padding-left:${8 + depth * 12}px" title="${esc(f.err || f.p)}">`
        + `<span class="tw">${syms.length ? (open ? '▾' : '▸') : ''}</span><span class="dot" style="background:${color}"></span><span class="nm">${esc(name)}</span>`
        + `${syms.length ? `<span class="cnt">${syms.length}</span>` : ''}</div>`);
      if (open) {
        for (const s of syms.sort((a, b) => a.s - b.s)) {
          if (q && symHit && !s.q.toLowerCase().includes(q)) continue;
          const id = symId(s);
          out.push(`<div class="tnode sym${S.selected === id ? ' on' : ''}" data-sym="${esc(id)}" style="padding-left:${22 + depth * 12}px" title="${esc(s.q)} (${s.s}行目)"><span class="k">${KIND_MARK[s.k] || ''}</span><span class="nm">${esc(symLabel(s))}</span><span class="cnt">${s.s}</span></div>`);
        }
      }
    }
  };
  walk(root, '', 0);
  box.innerHTML = out.join('') || '<div class="ct-head">該当なし</div>';
}
function symId(s) { return `${s.f}::${s.q}@${s.s}`; }

$('#fileTree').addEventListener('click', (ev) => {
  const t = ev.target.closest('.tnode'); if (!t) return;
  if (t.dataset.dir) {
    const p = t.dataset.dir;
    S.collapsed.has(p) ? S.collapsed.delete(p) : S.collapsed.add(p);
    renderFileTree(); return;
  }
  if (t.dataset.file) { openFile(t.dataset.file); return; }
  if (t.dataset.sym) { selectSym(t.dataset.sym, { reveal: true, fromTree: true }); }
});
$('#fileFilter').addEventListener('input', debounce(renderFileTree, 150));

// ---------------------------------------------------------------------------
// グラフ (Cytoscape)
// ---------------------------------------------------------------------------
function initCy() {
  cy = cytoscape({
    container: $('#cy'), wheelSensitivity: 0.25, minZoom: 0.05, maxZoom: 3,
    boxSelectionEnabled: false, style: cyStyle(),
  });
  cy.on('tap', 'node', (ev) => {
    const n = ev.target;
    if (n.data('file') && !n.data('sid')) { openFile(n.data('file')); showFileDetails(n.data('file')); return; }
    if (n.data('sid')) selectSym(n.data('sid'), { reveal: true });
  });
  cy.on('dbltap', 'node', (ev) => {
    const sid = ev.target.data('sid'); if (!sid) return;
    if (S.view === 'flow') {
      S.expanded.has(sid) ? S.expanded.delete(sid) : S.expanded.add(sid);
      render();
    }
  });
  cy.on('cxttap', 'node', (ev) => { const sid = ev.target.data('sid'); if (sid) openChart(sid); });
  cy.on('tap', (ev) => { if (ev.target === cy) hideDetails(); });
  // ペインの大きさが変わったら描画領域を合わせ直す
  let lastW = 0;
  new ResizeObserver(debounce(() => {
    const w = $('#graphWrap').clientWidth;
    cy.resize();
    if (Math.abs(w - lastW) > 40 && cy.nodes().length) cy.fit(undefined, 30);
    lastW = w;
  }, 150)).observe($('#graphWrap'));
}

function cyStyle() {
  const txt = cssVar('--node-text'), edge = cssVar('--edge'), acc = cssVar('--accent'), warn = cssVar('--warn');
  return [
    { selector: 'node', style: {
      shape: 'round-rectangle', 'background-color': 'data(bg)', 'border-color': 'data(bd)', 'border-width': 1.5,
      label: 'data(label)', color: txt, 'font-size': 11, 'font-family': 'ui-monospace, Menlo, Consolas, monospace',
      'text-valign': 'center', 'text-halign': 'center', 'text-wrap': 'wrap', 'text-max-width': 260,
      width: 'label', height: 'label', padding: '9px',
    } },
    { selector: 'node[kind="toplevel"]', style: { shape: 'cut-rectangle', 'border-width': 2.5 } },
    { selector: 'node[kind="class"]', style: { shape: 'hexagon' } },
    { selector: 'node.entry', style: { 'border-color': cssVar('--ok'), 'border-width': 3 } },
    { selector: 'node.more', style: { 'border-style': 'dashed', 'border-width': 2 } },
    { selector: 'node.ext', style: { shape: 'round-rectangle', 'background-color': 'transparent', 'border-style': 'dotted', 'border-color': edge, color: cssVar('--muted'), 'font-size': 10, padding: '4px' } },
    { selector: 'node.file', style: { shape: 'round-rectangle', 'border-width': 2 } },
    // 実行状態 (▶ 実行 で付く)
    { selector: 'node.notrun', style: { opacity: 0.35 } },
    { selector: 'node.ran', style: { 'border-color': cssVar('--ok'), 'border-width': 3, opacity: 1 } },
    { selector: 'node.active', style: { 'border-color': '#f59e0b', 'border-width': 4 } },
    { selector: 'node.current', style: { 'border-color': '#f59e0b', 'border-width': 6, 'background-color': isDark() ? '#5c4513' : '#fde68a' } },
    { selector: 'node.err', style: { 'border-color': cssVar('--bad'), 'border-width': 6, 'background-color': isDark() ? '#4a1d1d' : '#fee2e2' } },
    { selector: 'node:selected, node.hl', style: { 'border-color': acc, 'border-width': 4, 'overlay-opacity': 0 } },
    { selector: 'node.faded', style: { opacity: 0.25 } },
    { selector: 'edge', style: {
      width: 1.6, 'line-color': edge, 'target-arrow-color': edge, 'target-arrow-shape': 'triangle', 'arrow-scale': 0.9,
      'curve-style': 'bezier', label: 'data(label)', 'font-size': 9, color: cssVar('--muted'),
      'text-background-color': cssVar('--bg'), 'text-background-opacity': 1, 'text-background-padding': '1px',
    } },
    { selector: 'edge.taxi', style: { 'curve-style': 'taxi', 'taxi-direction': 'auto', 'taxi-turn': '50%' } },
    { selector: 'edge.amb', style: { 'line-style': 'dashed' } },
    { selector: 'edge.ref', style: { 'line-style': 'dotted', 'line-color': '#a855f7', 'target-arrow-color': '#a855f7', width: 2 } },
    { selector: 'edge.back', style: { 'line-style': 'dashed', 'line-color': warn, 'target-arrow-color': warn, 'curve-style': 'unbundled-bezier', 'control-point-distances': 60 } },
    { selector: 'edge.extE', style: { 'line-style': 'dotted', width: 1, 'target-arrow-shape': 'none' } },
    { selector: 'edge.hl', style: { 'line-color': acc, 'target-arrow-color': acc, width: 3 } },
    { selector: 'edge.activeE', style: { 'line-color': '#f59e0b', 'target-arrow-color': '#f59e0b', width: 3.5, 'line-style': 'solid' } },
    { selector: 'edge.faded', style: { opacity: 0.15 } },
  ];
}

function symNode(sid, extra = {}) {
  const s = S.sym[sid];
  const c = fileColors(s.f);
  const loc = `${base(s.f)}:${s.s}`;
  return { group: 'nodes', data: { id: sid, sid, kind: s.k, file: s.f, label: `${symLabel(s)}\n${loc}`, bg: c.bg, bd: c.bd, ...extra } };
}

function buildFlow() {
  const els = [], seen = new Map(), more = new Map();
  if (!S.entry || !S.sym[S.entry]) return { els, info: '起点がありません' };
  const q = [[S.entry, 0]];
  seen.set(S.entry, 0);
  const edges = [];
  let capped = false;
  while (q.length) {
    const [sid, d] = q.shift();
    const outs = S.out.get(sid) || [];
    if (!(d < S.depth || S.expanded.has(sid))) { if (outs.length) more.set(sid, outs.length); continue; }
    outs.forEach((e, i) => {
      const back = seen.has(e.to) && seen.get(e.to) <= d;
      edges.push({ from: sid, to: e.to, ord: i + 1, flag: e.flag, back, count: e.count });
      if (!seen.has(e.to)) {
        if (seen.size >= 700) { capped = true; return; }
        seen.set(e.to, d + 1); q.push([e.to, d + 1]);
      }
    });
  }
  for (const [sid] of seen) {
    const n = symNode(sid);
    const cls = [];
    if (sid === S.entry) cls.push('entry');
    if (more.has(sid)) { cls.push('more'); n.data.label += `\n＋${more.get(sid)} (ダブルクリックで展開)`; }
    n.classes = cls.join(' ');
    els.push(n);
  }
  for (const e of edges) {
    if (!seen.has(e.to)) continue;
    const cls = [S.dir === 'TB' || S.dir === 'LR' ? 'taxi' : ''];
    if (e.flag === 1) cls.push('amb');
    if (e.flag === 2) cls.push('ref');
    if (e.back) cls.push('back');
    els.push({ group: 'edges', data: { id: `${e.from}->${e.to}`, source: e.from, target: e.to, label: String(e.ord) + (e.count > 1 ? `×${e.count}` : '') }, classes: cls.join(' ') });
  }
  if (S.showExt) addExternals(els, [...seen.keys()]);
  return { els, info: `${seen.size} 関数${capped ? ' (700件で打ち切り)' : ''}` };
}

function addExternals(els, sids) {
  for (const sid of sids) {
    const s = S.sym[sid];
    (s.x || []).slice(0, 8).forEach((name) => {
      const id = `ext:${sid}:${name}`;
      els.push({ group: 'nodes', data: { id, label: name + '()' }, classes: 'ext' });
      els.push({ group: 'edges', data: { id: id + ':e', source: sid, target: id, label: '' }, classes: 'extE' });
    });
  }
}

function inScope(path) { return !S.scope || path.toLowerCase().includes(S.scope.toLowerCase()); }

function buildAll() {
  const els = [], ids = new Set();
  const want = new Set();
  for (const [from, to] of S.data.edges) {
    const a = S.sym[from], b = S.sym[to];
    if (inScope(a.f) || inScope(b.f)) { want.add(from); want.add(to); }
  }
  let capped = false;
  for (const sid of want) {
    if (ids.size >= 1200) { capped = true; break; }
    ids.add(sid);
    const n = symNode(sid);
    if (!inScope(S.sym[sid].f)) n.classes = 'faded';
    els.push(n);
  }
  for (const [from, to, , flag] of S.data.edges) {
    if (!ids.has(from) || !ids.has(to)) continue;
    els.push({ group: 'edges', data: { id: `${from}->${to}`, source: from, target: to, label: '' }, classes: flag === 1 ? 'amb' : flag === 2 ? 'ref' : '' });
  }
  if (S.showExt) addExternals(els, [...ids]);
  return { els, info: `${ids.size} 関数${capped ? ' (1200件で打ち切り。パスで絞り込んでください)' : ''}` };
}

function buildModules() {
  const els = [], ids = new Set();
  const files = S.data.files.filter((f) => f.lang);
  const linked = new Set();
  for (const f of files) for (const t of f.imp || []) { if (inScope(f.p) || inScope(t)) { linked.add(f.p); linked.add(t); } }
  for (const f of files) {
    if (!linked.has(f.p) && !(S.scope && inScope(f.p))) continue;
    if (ids.size >= 900) break;
    ids.add(f.p);
    const color = LANG_COLOR[f.lang] || '#888';
    const dir = f.p.includes('/') ? f.p.slice(0, f.p.lastIndexOf('/')) + '/' : './';
    els.push({ group: 'nodes', data: { id: 'f:' + f.p, file: f.p, label: `${base(f.p)}\n${dir}`, bg: fileColors(f.p).bg, bd: color }, classes: 'file' + (inScope(f.p) ? '' : ' faded') });
  }
  for (const f of files) {
    if (!ids.has(f.p)) continue;
    for (const t of f.imp || []) {
      if (!ids.has(t)) continue;
      els.push({ group: 'edges', data: { id: `${f.p}=>${t}`, source: 'f:' + f.p, target: 'f:' + t, label: '' }, classes: 'taxi' });
    }
  }
  return { els, info: `${ids.size} ファイル (矢印: import する側 → される側)` };
}

function render() {
  if (!S.data || !cy) return;
  const isChart = S.view === 'chart';
  $('#cy').classList.toggle('hidden', isChart);
  $('#chartWrap').classList.toggle('hidden', !isChart);
  $('#callees').classList.toggle('hidden', !isChart);
  $$('.toolbar .grp').forEach((g) => g.classList.toggle('hidden', !g.dataset.show.split(' ').includes(S.view)));
  renderLegend();
  if (isChart) { renderChart(); return; }
  const { els, info } = S.view === 'flow' ? buildFlow() : S.view === 'all' ? buildAll() : buildModules();
  cy.startBatch();
  cy.elements().remove();
  cy.add(els);
  cy.endBatch();
  const rankDir = S.view === 'modules' ? (S.dir === 'TB' ? 'LR' : 'TB') : S.dir;
  cy.edges('.taxi').style('taxi-direction', rankDir === 'TB' ? 'downward' : 'rightward');
  if (cy.nodes().length > 250 || cy.edges().length > 350) fastLayered(rankDir);   // 大きいグラフは dagre だと数十秒かかる
  else cy.layout({ name: 'dagre', rankDir, nodeSep: 24, rankSep: S.view === 'all' ? 50 : 60, edgeSep: 8, animate: false, fit: true, padding: 30 }).run();
  if (cy.zoom() > 1.2) { cy.zoom(1.2); cy.center(); }
  if (S.selected && cy.getElementById(S.selected).length) highlight(S.selected, false);
  $('#legend').dataset.info = info;
  renderLegend();
  if (S.view === 'flow') renderCallTree();
  if (window.applyRunState) applyRunState();
}

// 大規模グラフ用の簡易階層レイアウト (閉路除去 → 最長路で段決め → 重心法で並べ替え)
function fastLayered(rankDir) {
  const nodes = cy.nodes(), ids = nodes.map((n) => n.id());
  const idx = new Map(ids.map((id, i) => [id, i]));
  const out = ids.map(() => []), inc = ids.map(() => []);
  cy.edges().forEach((e) => {
    const a = idx.get(e.source().id()), b = idx.get(e.target().id());
    if (a !== b && a !== undefined && b !== undefined) out[a].push(b);
  });
  // 1. DFS で後退辺を取り除いて DAG にする
  const state = new Uint8Array(ids.length), dag = ids.map(() => []);
  for (let r = 0; r < ids.length; r++) {
    if (state[r]) continue;
    const stack = [[r, 0]]; state[r] = 1;
    while (stack.length) {
      const top = stack[stack.length - 1];
      const [v, i] = top;
      if (i < out[v].length) {
        top[1]++;
        const w = out[v][i];
        if (state[w] === 0) { state[w] = 1; dag[v].push(w); stack.push([w, 0]); }
        else if (state[w] === 2) dag[v].push(w);
      } else { state[v] = 2; stack.pop(); }
    }
  }
  dag.forEach((ws, v) => ws.forEach((w) => inc[w].push(v)));
  // 2. 段 (呼び出し元より下)
  const rank = new Int32Array(ids.length), indeg = inc.map((l) => l.length), q = [];
  indeg.forEach((d, i) => { if (!d) q.push(i); });
  for (let h = 0; h < q.length; h++) {
    const v = q[h];
    for (const w of dag[v]) { rank[w] = Math.max(rank[w], rank[v] + 1); if (--indeg[w] === 0) q.push(w); }
  }
  const layers = [];
  ids.forEach((_, i) => { (layers[rank[i]] ||= []).push(i); });
  const pos = new Float64Array(ids.length);
  const fileOf = (i) => nodes[i].data('file') || '';
  layers.forEach((L) => { L.sort((a, b) => fileOf(a).localeCompare(fileOf(b))); L.forEach((v, k) => { pos[v] = k; }); });
  // 3. 重心法で交差を減らす (上下に数回)
  const bary = (v, nb) => nb.length ? nb.reduce((t, u) => t + pos[u], 0) / nb.length : pos[v];
  for (let it = 0; it < 6; it++) {
    const down = it % 2 === 0;
    const order = down ? layers : [...layers].reverse();
    for (const L of order) {
      const key = new Map(L.map((v) => [v, bary(v, down ? inc[v] : dag[v])]));
      L.sort((a, b) => key.get(a) - key.get(b));
      L.forEach((v, k) => { pos[v] = k; });
    }
  }
  // 4. 座標
  const horiz = rankDir === 'LR';
  const gap = 24, rgap = 70, positions = {};
  let off = 0;
  // 1段に並ぶ数が多すぎると極端に横長になるので、一定数ごとに折り返す
  const per = Math.max(12, Math.ceil(Math.sqrt(ids.length) * 1.6));
  const rows = [];
  for (const L of layers) { if (!L) continue; for (let i = 0; i < L.length; i += per) rows.push(L.slice(i, i + per)); }
  for (const L of rows) {
    const sizes = L.map((v) => nodes[v].layoutDimensions({ nodeDimensionsIncludeLabels: true }));
    const thick = Math.max(...sizes.map((d) => horiz ? d.w : d.h));
    const total = sizes.reduce((t, d) => t + (horiz ? d.h : d.w) + gap, 0);
    let c = -total / 2;
    L.forEach((v, k) => {
      const d = sizes[k], along = horiz ? d.h : d.w;
      positions[ids[v]] = horiz ? { x: off + thick / 2, y: c + along / 2 } : { x: c + along / 2, y: off + thick / 2 };
      c += along + gap;
    });
    off += thick + rgap;
  }
  cy.layout({ name: 'preset', positions: (n) => positions[n.id()], fit: true, padding: 30, animate: false }).run();
}

function renderLegend() {
  const lg = $('#legend');
  const info = lg.dataset.info || '';
  if (S.view === 'chart') {
    lg.innerHTML = '<span>◇ 分岐/ループ</span><span>▭ 処理</span><span>⊂⊃ return/終了</span><span class="muted">クリックで該当行へ</span>';
    return;
  }
  const parts = [`<span>${esc(info)}</span>`];
  if (S.view !== 'modules') {
    parts.push('<span><i style="border-color:var(--edge)"></i>呼び出し (数字=呼ぶ順)</span>',
      '<span><i style="border-color:#a855f7;border-top-style:dotted"></i>コールバック登録</span>',
      '<span><i style="border-color:var(--edge);border-top-style:dashed"></i>候補が複数</span>');
    if (S.view === 'flow') parts.push('<span><i style="border-color:var(--warn);border-top-style:dashed"></i>再帰・既出</span>', '<span class="muted">ダブルクリック: 展開 / 右クリック: フローチャート</span>');
  }
  if (window.R && R.snap) parts.push('<span><i style="border-color:var(--ok);border-top-width:3px"></i>実行済み</span>', '<span><i style="border-color:#f59e0b;border-top-width:4px"></i>実行中</span>', '<span class="muted">薄い=未実行</span>');
  lg.innerHTML = parts.join('');
}

$('#legend').addEventListener('click', () => {
  const lg = $('#legend');
  lg.classList.toggle('folded');
  try { localStorage.setItem('cf.legend', lg.classList.contains('folded') ? '0' : '1'); } catch (e) { /* 無視 */ }
});
try { if (localStorage.getItem('cf.legend') === '0') $('#legend').classList.add('folded'); } catch (e) { /* 無視 */ }

function highlight(sid, center = true) {
  if (!cy) return;
  cy.elements().removeClass('hl faded');
  const n = cy.getElementById(sid);
  if (!n.length) return;
  cy.$(':selected').unselect();
  n.select();
  const nb = n.closedNeighborhood();
  nb.edges().addClass('hl');
  if (center) cy.animate({ center: { eles: n }, duration: 250 });
}

// ---------------------------------------------------------------------------
// 呼び出しツリー (左ペイン)
// ---------------------------------------------------------------------------
function renderCallTree() {
  const box = $('#callTree');
  if (!S.entry || !S.sym[S.entry]) { box.innerHTML = '<div class="ct-head">起点がありません</div>'; return; }
  const rows = [];
  const seen = new Set();
  const maxRows = 1500;
  const walk = (sid, depth, ord, path) => {
    if (rows.length > maxRows) return;
    const s = S.sym[sid];
    const rep = seen.has(sid);
    const loop = path.has(sid);
    rows.push(`<div class="ct${S.selected === sid ? ' on' : ''}" data-sym="${esc(sid)}" style="padding-left:${8 + depth * 14}px">`
      + `<span class="ord">${ord}</span><span>${window.runMark ? runMark(sid) : ''}${esc(symLabel(s))}</span><span class="loc">${esc(base(s.f))}:${s.s}</span>`
      + (loop ? '<span class="rep">↺ 再帰</span>' : rep ? '<span class="rep">… 既出</span>' : '') + '</div>');
    if (rep || loop) return;
    seen.add(sid);
    if (depth >= S.depth + 4 && !S.expanded.has(sid)) return;
    const next = new Set(path); next.add(sid);
    (S.out.get(sid) || []).forEach((e, i) => walk(e.to, depth + 1, `${i + 1}.`, next));
  };
  walk(S.entry, 0, '●', new Set());
  const s = S.sym[S.entry];
  box.innerHTML = `<div class="ct-head">起点: <b>${esc(symLabel(s))}</b><br>${esc(s.f)} — 呼び出し順に表示</div>` + rows.join('')
    + (rows.length > maxRows ? '<div class="ct-head">…省略</div>' : '');
}
$('#callTree').addEventListener('click', (ev) => {
  const t = ev.target.closest('.ct'); if (t) selectSym(t.dataset.sym, { reveal: true });
});

// ---------------------------------------------------------------------------
// 選択・詳細カード
// ---------------------------------------------------------------------------
function selectSym(sid, { reveal = false } = {}) {
  const s = S.sym[sid]; if (!s) return;
  S.selected = sid;
  if (S.view === 'chart') { openChart(sid, { push: true }); }
  else highlight(sid);
  showDetails(sid);
  if (reveal) openFile(s.f, s.s, s.e);
  $$('#callTree .ct').forEach((el) => el.classList.toggle('on', el.dataset.sym === sid));
  $$('#fileTree .tnode.sym').forEach((el) => el.classList.toggle('on', el.dataset.sym === sid));
}

function showDetails(sid) {
  const s = S.sym[sid];
  const outs = S.out.get(sid) || [], ins = S.inc.get(sid) || [];
  const link = (id, prefix = '') => { const t = S.sym[id]; return `<span class="lnk" data-sym="${esc(id)}" title="${esc(t.f)}:${t.s}">${prefix}${esc(symLabel(t))} <span class="muted">${esc(base(t.f))}:${t.s}</span></span>`; };
  const card = $('#details');
  card.innerHTML = `<button class="x ghost" data-act="close">✕</button>
    <h3>${esc(symLabel(s))}</h3>
    <div class="loc">${esc(s.f)} : ${s.s}–${s.e}行 · ${({ function: '関数', method: 'メソッド', class: 'クラス', toplevel: 'トップレベル処理' })[s.k] || s.k}</div>
    ${window.runInfo ? runInfo(sid) : ''}
    <div class="row">
      <button data-act="chart">フローチャート</button>
      <button data-act="entry">ここを起点に</button>
      <button data-act="open">エディタで開く</button>
    </div>
    <div class="sec">呼び出し先 (${outs.length}) — 呼ぶ順</div>
    ${outs.slice(0, 40).map((e, i) => link(e.to, `${i + 1}. `)).join('') || '<span class="muted">なし</span>'}
    <div class="sec">呼び出し元 (${ins.length})</div>
    ${ins.slice(0, 40).map((e) => link(e.from, '← ')).join('') || '<span class="muted">なし (起点候補)</span>'}
    ${s.x?.length ? `<div class="sec">外部・未解決の呼び出し</div><div>${s.x.map((n) => `<span class="pill">${esc(n)}</span>`).join('')}</div>` : ''}`;
  card.classList.remove('hidden');
  card.dataset.sid = sid;
}
function showFileDetails(path) {
  const f = S.files.get(path); if (!f) return;
  const by = S.data.files.filter((g) => (g.imp || []).includes(path)).map((g) => g.p);
  const card = $('#details');
  const lnk = (p) => `<span class="lnk" data-file="${esc(p)}">${esc(p)}</span>`;
  card.innerHTML = `<button class="x ghost" data-act="close">✕</button><h3>${esc(base(path))}</h3><div class="loc">${esc(path)} · ${f.lang} · ${f.n}行</div>
    <div class="sec">import しているファイル (${(f.imp || []).length})</div>${(f.imp || []).map(lnk).join('') || '<span class="muted">なし</span>'}
    <div class="sec">このファイルを import しているファイル (${by.length})</div>${by.map(lnk).join('') || '<span class="muted">なし</span>'}`;
  card.classList.remove('hidden');
  delete card.dataset.sid;
}
function hideDetails() { $('#details').classList.add('hidden'); }

$('#details').addEventListener('click', (ev) => {
  const l = ev.target.closest('[data-sym]');
  if (l) { selectSym(l.dataset.sym, { reveal: true }); return; }
  const fl = ev.target.closest('[data-file]');
  if (fl) { openFile(fl.dataset.file); return; }
  const b = ev.target.closest('[data-act]'); if (!b) return;
  const sid = $('#details').dataset.sid;
  if (b.dataset.act === 'close') hideDetails();
  if (b.dataset.act === 'chart') openChart(sid);
  if (b.dataset.act === 'entry') setEntry(sid);
  if (b.dataset.act === 'open') { const s = S.sym[sid]; openFile(s.f, s.s, s.e); }
});

function setEntry(sid) {
  S.entry = sid; S.expanded.clear();
  fillEntries();
  setView('flow');
}

// ---------------------------------------------------------------------------
// 関数フローチャート (Mermaid)
// ---------------------------------------------------------------------------
function openChart(sid, { push = true } = {}) {
  if (!sid) return;
  if (push && S.chartSym && S.chartSym !== sid) S.chartStack.push(S.chartSym);
  S.chartSym = sid;
  S.selected = sid;
  if (S.view !== 'chart') setView('chart'); else renderChart();
}

function mmText(t, n = 70) {
  t = String(t ?? '').replace(/\s+/g, ' ').trim();
  if (t.length > n) t = t.slice(0, n - 1) + '…';
  return t.replace(/[^\p{L}\p{N} _.,:=+\-*/%!?@$^~'`]/gu, (c) => `#${c.codePointAt(0)};`);
}

function buildMermaid(items, title, calleeByName) {
  const L = ['flowchart TD'];
  const meta = {};
  let n = 0;
  const id = () => 'n' + (n++);
  const node = (shape, text, line, extra = {}) => {
    const k = id();
    const t = `"${text}"`;
    const sh = { box: `[${t}]`, dec: `{${t}}`, term: `([${t}])`, call: `[[${t}]]`, sub: `[/${t}/]`, hex: `{{${t}}}` }[shape];
    L.push(`${k}${sh}`);
    meta[k] = { line, ...extra };
    return k;
  };
  const link = (preds, to) => {
    for (const [p, lab] of preds) L.push(lab ? `${p} -->|"${mmText(lab, 24)}"| ${to}` : `${p} --> ${to}`);
  };
  const MAX = 260;
  const start = node('term', mmText('開始: ' + title, 60), null);
  L.push(`class ${start} cfStart`);
  const end = id();
  const exits = [];

  // 連続する「呼び出しの無い単純な文」は1つの箱にまとめる
  const compact = (list) => {
    const out = [];
    for (const it of list) {
      const last = out[out.length - 1];
      if (it.t === 'stmt' && !(it.c || []).length && last && last.t === 'stmt' && last.plain) {
        last.texts.push(it.x); continue;
      }
      if (it.t === 'stmt' && !(it.c || []).length) out.push({ ...it, plain: true, texts: [it.x] });
      else out.push(it);
    }
    return out;
  };

  const seq = (list, preds, ctx) => {
    for (const it of compact(list || [])) {
      if (!preds.length) break;
      if (n > MAX) { const k = node('box', '… (大きいので省略)', null); link(preds, k); return [[k, '']]; }
      preds = one(it, preds, ctx);
    }
    return preds;
  };
  const one = (it, preds, ctx) => {
    switch (it.t) {
      case 'stmt': {
        if (it.plain) {
          const txt = it.texts.length > 1 ? `${mmText(it.texts[0], 50)}<br/>… 他${it.texts.length - 1}行` : mmText(it.x);
          const k = node('box', txt, it.l); link(preds, k); return [[k, '']];
        }
        const callee = (it.c || []).map((c) => calleeByName[c]).find(Boolean);
        const k = node(callee ? 'call' : 'box', mmText(it.x), it.l, callee ? { callee } : {});
        if (callee) L.push(`class ${k} cfCall`);
        link(preds, k); return [[k, '']];
      }
      case 'if': {
        const d = node('dec', mmText(it.x, 50), it.l); link(preds, d);
        const a = seq(it.a, [[d, 'Yes']], ctx);
        const b = (it.b && it.b.length) ? seq(it.b, [[d, 'No']], ctx) : [[d, 'No']];
        return [...a, ...b];
      }
      case 'loop': {
        const d = node('dec', mmText(it.x, 50), it.l); L.push(`class ${d} cfLoop`); link(preds, d);
        const lc = { ...ctx, loop: d, breaks: [] };
        const body = seq(it.a, [[d, '繰り返す']], lc);
        link(body, d);
        return [[d, '終了'], ...lc.breaks];
      }
      case 'switch': {
        const d = node('hex', mmText(it.x, 50), it.l); link(preds, d);
        const sc = { ...ctx, sw: true, breaks: [] };
        let outs = [];
        for (const c of (it.cases || []).slice(0, 30)) outs = outs.concat(seq(c.a, [[d, c.x]], sc));
        return [...outs, ...sc.breaks];
      }
      case 'try': {
        const t = node('sub', 'try', it.l); link(preds, t);
        let outs = seq(it.a, [[t, '']], ctx);
        for (const h of it.h || []) outs = outs.concat(seq(h.a, [[t, '例外: ' + h.x]], ctx));
        if (it.e && it.e.length) outs = seq(it.e, outs, ctx);
        if (it.f && it.f.length) {
          const f = node('sub', 'finally', null); link(outs, f);
          outs = seq(it.f, [[f, '']], ctx);
        }
        return outs;
      }
      case 'group': return seq(it.a, preds, ctx);
      case 'return': {
        const callee = (it.c || []).map((c) => calleeByName[c]).find(Boolean);
        const k = node('term', mmText(it.x), it.l, callee ? { callee } : {}); L.push(`class ${k} cfRet`);
        link(preds, k); exits.push([k, '']); return [];
      }
      case 'break': {
        if (ctx.breaks) { ctx.breaks.push(...preds); return []; }
        return preds;
      }
      case 'continue': {
        if (ctx.loop) { link(preds, ctx.loop); return []; }
        return preds;
      }
      default: return preds;
    }
  };
  const last = seq(items, [[start, '']], {});
  L.push(`${end}(["終了"])`);
  L.push(`class ${end} cfStart`);
  link([...last, ...exits], end);
  const dark = isDark();
  L.push(`classDef cfStart fill:${dark ? '#1e3a2a' : '#dcfce7'},stroke:#16a34a`);
  L.push(`classDef cfCall fill:${dark ? '#1e2b4a' : '#dbe7ff'},stroke:#2563eb`);
  L.push(`classDef cfLoop fill:${dark ? '#3a2e12' : '#fef3c7'},stroke:#d97706`);
  L.push(`classDef cfRet fill:${dark ? '#3a1d1d' : '#fee2e2'},stroke:#dc2626`);
  return { code: L.join('\n'), meta };
}

let chartZoom = { k: 1, x: 0, y: 0 };
async function renderChart() {
  const sid = S.chartSym || S.selected || S.entry;
  const inner = $('#chartInner');
  $('#chartBack').disabled = !S.chartStack.length;
  if (!sid || !S.sym[sid]) { inner.innerHTML = '<p class="muted">関数を選択してください (ノードを右クリック / 詳細カードの「フローチャート」)</p>'; return; }
  S.chartSym = sid;
  const s = S.sym[sid];
  $('#chartTitle').textContent = `${symLabel(s)} — ${s.f}:${s.s}`;
  busy(true, 'フローチャート作成中…');
  try {
    const flow = await api(`/api/flow?path=${encodeURIComponent(s.f)}&line=${s.s}`);
    const calleeByName = {};
    for (const e of S.out.get(sid) || []) { const t = S.sym[e.to]; if (t && !calleeByName[t.n]) calleeByName[t.n] = e.to; }
    const { code, meta } = buildMermaid(flow.items, symLabel(s), calleeByName);
    mermaid.initialize({ startOnLoad: false, theme: isDark() ? 'dark' : 'default', securityLevel: 'strict', flowchart: { htmlLabels: true, curve: 'basis', useMaxWidth: false }, maxTextSize: 400000, maxEdges: 2000 });
    const { svg } = await mermaid.render('cfm' + (mermaidId++), code);
    inner.innerHTML = svg;
    S.chartNodes = [];
    inner.querySelectorAll('g.node').forEach((g) => {
      const m = /flowchart-(n\d+)-/.exec(g.id || '');
      const info = m && meta[m[1]];
      if (!info) return;
      if (info.line) S.chartNodes.push({ g, line: info.line });
      g.addEventListener('click', () => {
        inner.querySelectorAll('.cf-hl').forEach((x) => x.classList.remove('cf-hl'));
        g.classList.add('cf-hl');
        if (info.line) openFile(s.f, info.line, info.line, true);
      });
      if (info.callee) {
        g.addEventListener('dblclick', () => openChart(info.callee));
        g.querySelector('title')?.remove();
        g.insertAdjacentHTML('afterbegin', `<title>ダブルクリックで ${esc(S.sym[info.callee].q)} のフローチャートへ</title>`);
      }
    });
    chartZoom = { k: 1, x: 0, y: 0 };
    fitChart();
    if (window.applyChartRun) applyChartRun();
    // 呼び出し先へのショートカット
    const outs = S.out.get(sid) || [];
    $('#callees').innerHTML = `<span class="muted">この関数が呼ぶ関数:</span>` + (outs.length
      ? outs.map((e, i) => `<button data-sym="${esc(e.to)}" title="${esc(S.sym[e.to].f)}">${i + 1}. ${esc(symLabel(S.sym[e.to]))}</button>`).join('')
      : '<span class="muted">なし</span>') + '<span class="muted" style="margin-left:auto">青い二重枠=プロジェクト内の関数呼び出し (ダブルクリックで中へ)</span>';
  } catch (e) {
    inner.innerHTML = `<p style="color:var(--bad)">フローチャートを作成できませんでした: ${esc(e.message)}</p>`;
  } finally { busy(false); }
}
function applyChartZoom() { $('#chartInner').style.transform = `translate(${chartZoom.x}px, ${chartZoom.y}px) scale(${chartZoom.k})`; }
function fitChart() {
  const wrap = $('#chartWrap'), svg = $('#chartInner svg');
  if (!svg) return;
  const w = svg.getBoundingClientRect().width / chartZoom.k + 40, h = svg.getBoundingClientRect().height / chartZoom.k + 40;
  // 縦長のチャートは高さに合わせると文字が潰れるので、幅基準で合わせて縦はスクロール(ドラッグ)で見る
  const k = Math.min(1.1, Math.max(0.3, Math.min(wrap.clientWidth / w, Math.max(wrap.clientHeight / h, 0.6))));
  chartZoom = { k, x: Math.max(0, (wrap.clientWidth - w * k) / 2), y: 0 };
  applyChartZoom();
}
(function chartPanZoom() {
  const wrap = $('#chartWrap');
  wrap.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const r = wrap.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const f = Math.exp(-ev.deltaY * 0.0015);
    const k = Math.min(3, Math.max(0.08, chartZoom.k * f));
    chartZoom.x = mx - (mx - chartZoom.x) * (k / chartZoom.k);
    chartZoom.y = my - (my - chartZoom.y) * (k / chartZoom.k);
    chartZoom.k = k; applyChartZoom();
  }, { passive: false });
  let drag = null;
  wrap.addEventListener('mousedown', (ev) => { if (ev.button === 0) { drag = { x: ev.clientX - chartZoom.x, y: ev.clientY - chartZoom.y, moved: false }; wrap.classList.add('drag'); } });
  window.addEventListener('mousemove', (ev) => { if (!drag) return; chartZoom.x = ev.clientX - drag.x; chartZoom.y = ev.clientY - drag.y; applyChartZoom(); });
  window.addEventListener('mouseup', () => { drag = null; wrap.classList.remove('drag'); });
})();
$('#callees').addEventListener('click', (ev) => { const b = ev.target.closest('[data-sym]'); if (b) { openChart(b.dataset.sym); const s = S.sym[b.dataset.sym]; openFile(s.f, s.s, s.e); } });
$('#chartBack').addEventListener('click', () => { const prev = S.chartStack.pop(); if (prev) { S.chartSym = prev; renderChart(); } });

// ---------------------------------------------------------------------------
// エディタ (Monaco)
// ---------------------------------------------------------------------------
function initMonaco() {
  if (monacoReady) return monacoReady;
  monacoReady = (async () => {
    const vs = (await exists(MONACO_LOCAL + '/loader.js')) ? MONACO_LOCAL : MONACO_CDN;
    if (vs === MONACO_CDN) {
      // CDN 版はワーカーを同一オリジンから起動できないため data: URL 経由で読む
      window.MonacoEnvironment = { getWorkerUrl: () => `data:text/javascript;charset=utf-8,${encodeURIComponent(`self.MonacoEnvironment={baseUrl:'${MONACO_CDN.replace(/\/vs$/, '')}/'};importScripts('${MONACO_CDN}/base/worker/workerMain.js');`)}` };
    }
    await loadScript(vs + '/loader.js');
    window.require.config({ paths: { vs } });
    await new Promise((ok) => window.require(['vs/editor/editor.main'], ok));
    editor = monaco.editor.create($('#editor'), {
      automaticLayout: true, theme: isDark() ? 'vs-dark' : 'vs', fontSize: 13, minimap: { enabled: true, scale: 1 },
      scrollBeyondLastLine: false, model: null, renderWhitespace: 'selection', tabSize: 4, glyphMargin: true,
    });
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveActive());
    editor.onDidChangeCursorPosition(debounce(onCursor, 120));
    // 定義へジャンプ (Ctrl/⌘+クリック, F12)
    const provider = {
      provideDefinition(model, pos) {
        const w = model.getWordAtPosition(pos); if (!w) return null;
        const path = model.uri.path.replace(/^\//, '');
        const cands = Object.entries(S.sym).filter(([, s]) => s.n === w.word && s.k !== 'toplevel');
        if (!cands.length) return null;
        const pick = cands.find(([, s]) => s.f === path) || cands[0];
        const s = pick[1];
        if (s.f === path) return { uri: model.uri, range: new monaco.Range(s.s, 1, s.s, 1) };
        openFile(s.f, s.s, s.e);
        return null;
      },
    };
    const hover = {
      provideHover(model, pos) {
        const w = model.getWordAtPosition(pos); if (!w) return null;
        const cands = Object.entries(S.sym).filter(([, s]) => s.n === w.word && s.k !== 'toplevel').slice(0, 5);
        if (!cands.length) return null;
        return { contents: cands.map(([id, s]) => ({ value: `**${s.q}** — \`${s.f}:${s.s}\`  \n呼び出し元 ${(S.inc.get(id) || []).length} / 呼び出し先 ${(S.out.get(id) || []).length}` })) };
      },
    };
    for (const l of monaco.languages.getLanguages()) {
      monaco.languages.registerDefinitionProvider(l.id, provider);
      monaco.languages.registerHoverProvider(l.id, hover);
    }
    return editor;
  })();
  return monacoReady;
}

const decos = new Map();
async function openFile(path, line = null, endLine = null, onlyLine = false) {
  if (!path) return;
  const f = S.files.get(path);
  await initMonaco();
  let tab = S.tabs.find((t) => t.path === path);
  if (!tab) {
    let d;
    try { d = await api(`/api/file?path=${encodeURIComponent(path)}`); }
    catch (e) { toast(`${path}: ${e.message}`, true); return; }
    const uri = monaco.Uri.file('/' + path);
    const model = monaco.editor.getModel(uri) || monaco.editor.createModel(d.content, d.lang, uri);
    tab = { path, model, encoding: d.encoding, mtime: d.mtime, saved: model.getAlternativeVersionId(), view: null };
    model.onDidChangeContent(() => renderTabs());
    S.tabs.push(tab);
  }
  activate(tab);
  if (line) {
    editor.revealLineInCenter(line);
    editor.setPosition({ lineNumber: line, column: 1 });
    const old = decos.get(path) || [];
    const ds = [{ range: new monaco.Range(line, 1, line, 1), options: { isWholeLine: true, className: 'cf-line' } }];
    if (endLine && endLine > line && !onlyLine) ds.push({ range: new monaco.Range(line, 1, endLine, 1), options: { isWholeLine: true, className: 'cf-range' } });
    decos.set(path, tab.model.deltaDecorations(old, ds));
  }
  if (f) renderFileTreeActive(path);
  if (window.applyEditorRun) applyEditorRun();
}
function activate(tab) {
  if (S.active && S.active !== tab.path) {
    const cur = S.tabs.find((t) => t.path === S.active);
    if (cur) cur.view = editor.saveViewState();
  }
  S.active = tab.path;
  editor.setModel(tab.model);
  if (tab.view) editor.restoreViewState(tab.view);
  $('#editorEmpty').classList.add('hidden');
  renderTabs();
  onCursor();
}
function renderFileTreeActive(path) {
  $$('#fileTree .tnode.file').forEach((el) => el.classList.toggle('on', el.dataset.file === path));
}
function isDirty(t) { return t.model.getAlternativeVersionId() !== t.saved; }
function renderTabs() {
  $('#tabs').innerHTML = S.tabs.map((t) => `<div class="etab${t.path === S.active ? ' on' : ''}" data-path="${esc(t.path)}" title="${esc(t.path)}">`
    + `<span class="dot" style="background:${LANG_COLOR[S.files.get(t.path)?.lang] || '#888'}"></span>${esc(base(t.path))}`
    + `${t.conflict ? '<span class="warn" title="外部で変更されています。保存時に差分を確認します">⚠</span>' : ''}`
    + `${isDirty(t) ? '<span class="dirty">●</span>' : ''}<button class="close" data-close="${esc(t.path)}">✕</button></div>`).join('');
  const t = S.tabs.find((x) => x.path === S.active);
  $('#stRight').textContent = t ? `${t.encoding} · ${t.model.getLanguageId()}${isDirty(t) ? ' · 未保存' : ''}` : '';
}
$('#tabs').addEventListener('click', (ev) => {
  const c = ev.target.closest('[data-close]');
  if (c) { closeTab(c.dataset.close); return; }
  const t = ev.target.closest('.etab'); if (t) activate(S.tabs.find((x) => x.path === t.dataset.path));
});
function closeTab(path) {
  const i = S.tabs.findIndex((t) => t.path === path); if (i < 0) return;
  const t = S.tabs[i];
  if (isDirty(t) && !confirm(`${path} は未保存です。閉じますか？`)) return;
  t.model.dispose(); S.tabs.splice(i, 1);
  if (S.active === path) {
    S.active = null;
    const next = S.tabs[Math.max(0, i - 1)];
    if (next) activate(next); else { editor.setModel(null); $('#editorEmpty').classList.remove('hidden'); renderTabs(); $('#stLeft').textContent = ''; }
  } else renderTabs();
}
async function saveActive(force = false) {
  const t = S.tabs.find((x) => x.path === S.active); if (!t) return;
  try {
    const r = await api('/api/file', { body: { path: t.path, content: t.model.getValue(), encoding: t.encoding, base_mtime: t.mtime, force } });
    t.saved = t.model.getAlternativeVersionId(); t.mtime = r.mtime; t.conflict = false;
    renderTabs();
    toast(`保存しました: ${t.path}` + (r.backup ? ' (前の版はバックアップ済み)' : ''));
    await scan(null, { keepView: true });
  } catch (e) {
    if (e.status === 409 && e.data?.conflict) { showConflict(t, e.data); return; }
    toast('保存に失敗: ' + e.message, true, 5000);
  }
}

// ---------------------------------------------------------------------------
// 差分ダイアログ (保存時の衝突・バックアップからの復元)
// ---------------------------------------------------------------------------
let diffEd = null;
function openDiff({ title, note, original, modified, lang, editable = false, buttons = [], list = null }) {
  $('#diffTitle').textContent = title;
  $('#diffNote').innerHTML = note || '';
  $('#diffModal').classList.remove('hidden');
  $('#diffList').classList.toggle('hidden', !list);
  if (!diffEd) {
    diffEd = monaco.editor.createDiffEditor($('#diffEditor'), { automaticLayout: true, renderSideBySide: true, originalEditable: false, readOnly: false });
  }
  setDiffModels(original, modified, lang, editable);
  const foot = $('#diffBtns');
  foot.innerHTML = '';
  for (const b of [...buttons, { label: 'キャンセル' }]) {
    const el = document.createElement('button');
    el.textContent = b.label;
    if (b.primary) el.className = 'primary';
    el.addEventListener('click', async () => {
      const text = diffEd.getModel().modified.getValue();
      if (b.run && (await b.run(text)) === false) return;
      closeDiff();
    });
    foot.appendChild(el);
  }
}
function setDiffModels(original, modified, lang, editable) {
  const old = diffEd.getModel();
  diffEd.setModel({ original: monaco.editor.createModel(original ?? '', lang), modified: monaco.editor.createModel(modified ?? '', lang) });
  diffEd.updateOptions({ readOnly: !editable });
  if (old) { old.original.dispose(); old.modified.dispose(); }
}
function closeDiff() { $('#diffModal').classList.add('hidden'); }
$('#diffClose').addEventListener('click', closeDiff);

function showConflict(t, d) {
  const lang = t.model.getLanguageId();
  openDiff({
    title: `${t.path} は開いた後に外部で変更されています`,
    note: '<b>左</b>: ディスク上の内容（VS Code など外部での変更） / <b>右</b>: あなたの編集。右側は編集できるので、左の変更を取り込んでから保存できます。どちらで保存しても、上書きされる側は自動バックアップ（🕘 履歴）に残ります。',
    original: d.disk, modified: t.model.getValue(), lang, editable: true,
    buttons: [
      { label: '右側の内容で保存', primary: true, run: async (text) => { t.model.setValue(text); t.mtime = d.mtime; await saveActive(true); } },
      { label: 'ディスクの内容を読み込む（自分の編集を破棄）', run: () => {
        if (!confirm('エディタ上の未保存の編集を破棄して、ディスクの内容に置き換えます。よろしいですか？')) return false;
        t.model.setValue(d.disk); t.saved = t.model.getAlternativeVersionId(); t.mtime = d.mtime; t.conflict = false; renderTabs();
      } },
    ],
  });
}

async function openHistory() {
  const t = S.tabs.find((x) => x.path === S.active);
  if (!t) { toast('ファイルを開いてから使ってください'); return; }
  let items;
  try { items = (await api(`/api/backups?path=${encodeURIComponent(t.path)}`)).items; } catch (e) { toast(e.message, true); return; }
  if (!items.length) { toast('このファイルのバックアップはまだありません（このツールで保存すると作られます）'); return; }
  const lang = t.model.getLanguageId();
  let chosen = null;
  const pick = async (id) => {
    chosen = id;
    $$('#diffList div').forEach((el) => el.classList.toggle('on', el.dataset.id === id));
    const b = await api(`/api/backup?path=${encodeURIComponent(t.path)}&id=${encodeURIComponent(id)}`);
    chosen = { id, content: b.content };
    setDiffModels(b.content, t.model.getValue(), lang, false);
  };
  openDiff({
    title: `${t.path} の保存履歴`,
    note: '<b>左</b>: 選んだバックアップ / <b>右</b>: 現在のエディタの内容。このツールで保存するたびに、保存前の内容が自動で残ります（1ファイル30件まで）。',
    original: '', modified: t.model.getValue(), lang, list: true,
    buttons: [{ label: 'この版をエディタに戻す（未保存の状態になります）', primary: true, run: () => {
      if (!chosen?.content && chosen?.content !== '') return false;
      t.model.setValue(chosen.content); renderTabs(); toast('復元しました。内容を確認して保存してください');
    } }],
  });
  $('#diffList').innerHTML = items.map((x) => `<div data-id="${esc(x.id)}">${esc(x.time)}<br><span class="muted">${(x.size / 1024).toFixed(1)} KB</span></div>`).join('');
  $('#diffList').onclick = (ev) => { const el = ev.target.closest('[data-id]'); if (el) pick(el.dataset.id); };
  pick(items[0].id);
}
$('#histBtn').addEventListener('click', () => initMonaco().then(openHistory));
function onCursor() {
  const t = S.tabs.find((x) => x.path === S.active);
  if (!t || !editor.getPosition()) return;
  const line = editor.getPosition().lineNumber;
  const f = S.files.get(t.path);
  let best = null;
  for (const id of f?.sym || []) {
    const s = S.sym[id];
    if (s && s.k !== 'class' && s.k !== 'toplevel' && s.s <= line && line <= s.e && (!best || s.s >= S.sym[best].s)) best = id;
  }
  $('#stLeft').textContent = `${t.path} : ${line}行` + (best ? ` — ${S.sym[best].q}` : '');
  if (best && cy && S.view !== 'chart' && cy.getElementById(best).length) {
    cy.elements().removeClass('hl'); cy.getElementById(best).addClass('hl');
  }
}

// ---------------------------------------------------------------------------
// UI イベント
// ---------------------------------------------------------------------------
function setView(v) {
  S.view = v;
  $$('#viewSeg button').forEach((b) => b.classList.toggle('on', b.dataset.view === v));
  render();
}
$('#viewSeg').addEventListener('click', (ev) => { const b = ev.target.closest('button'); if (b) setView(b.dataset.view); });
$('#entrySel').addEventListener('change', (ev) => { S.entry = ev.target.value; S.expanded.clear(); render(); });
$('#depthInp').addEventListener('change', (ev) => { S.depth = Math.max(1, Math.min(30, +ev.target.value || 4)); render(); });
$('#scopeInp').addEventListener('input', debounce((ev) => { S.scope = ev.target.value.trim(); render(); }, 300));
$('#dirBtn').addEventListener('click', () => { S.dir = S.dir === 'TB' ? 'LR' : 'TB'; $('#dirBtn').textContent = S.dir === 'TB' ? '↓ 縦' : '→ 横'; render(); });
$('#extChk').addEventListener('change', (ev) => { S.showExt = ev.target.checked; render(); });
$('#fitBtn').addEventListener('click', () => cy && cy.fit(undefined, 30));
$('#pngBtn').addEventListener('click', () => {
  if (!cy) return;
  const a = document.createElement('a');
  a.href = cy.png({ full: true, scale: 2, bg: cssVar('--bg') });
  a.download = `codeflow-${S.view}.png`; a.click();
});
$('#searchInp').addEventListener('change', (ev) => {
  const v = ev.target.value; if (!v) return;
  const m = /^(.*) — (.*):(\d+)$/.exec(v);
  let sid = null;
  if (m) sid = Object.keys(S.sym).find((id) => S.sym[id].q === m[1] && S.sym[id].f === m[2] && S.sym[id].s === +m[3]);
  else sid = Object.keys(S.sym).find((id) => S.sym[id].q === v || S.sym[id].n === v);
  if (!sid) { toast('見つかりません: ' + v, true); return; }
  ev.target.value = '';
  if (S.view === 'flow' && cy && !cy.getElementById(sid).length) setEntry(sid);
  selectSym(sid, { reveal: true });
});
$$('.tabs button').forEach((b) => b.addEventListener('click', () => {
  $$('.tabs button').forEach((x) => x.classList.toggle('on', x === b));
  $$('#side [data-for]').forEach((el) => el.classList.toggle('hidden', el.dataset.for !== b.dataset.side));
}));
$('#loadBtn').addEventListener('click', () => { S.tabs.forEach((t) => t.model.dispose()); S.tabs = []; S.active = null; if (editor) editor.setModel(null); renderTabs(); scan($('#rootInput').value.trim()); });
$('#rootInput').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') $('#loadBtn').click(); });
$('#themeBtn').addEventListener('click', () => {
  document.documentElement.dataset.theme = isDark() ? 'light' : 'dark';
  try { localStorage.setItem('cf.theme', document.documentElement.dataset.theme); } catch (e) { /* 無視 */ }
  applyTheme();
});
function applyTheme() {
  if (cy) { cy.style(cyStyle()); render(); }
  if (window.monaco) monaco.editor.setTheme(isDark() ? 'vs-dark' : 'vs');
}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
$('#newFileBtn').addEventListener('click', async () => {
  const p = prompt('作成するファイルのパス (読み込んだフォルダからの相対パス)', 'new_file.py');
  if (!p) return;
  try { await api('/api/file', { body: { path: p, content: '', create: true } }); await scan(null, { keepView: true }); openFile(p); }
  catch (e) { toast(e.message, true); }
});
window.addEventListener('beforeunload', (ev) => { if (S.tabs.some(isDirty)) { ev.preventDefault(); ev.returnValue = ''; } });
window.addEventListener('keydown', (ev) => { if ((ev.ctrlKey || ev.metaKey) && ev.key === 's') { ev.preventDefault(); saveActive(); } });

// 分割バー
$$('.splitter').forEach((sp) => {
  sp.addEventListener('mousedown', (ev) => {
    ev.preventDefault(); sp.classList.add('drag');
    const target = sp.dataset.split === 'side' ? $('#side') : $('#editorPane');
    const startX = ev.clientX, startW = target.getBoundingClientRect().width;
    const move = (e) => {
      const dx = e.clientX - startX;
      const w = sp.dataset.split === 'side' ? startW + dx : startW - dx;
      target.style.width = Math.max(140, Math.min(window.innerWidth - 300, w)) + 'px';
      if (cy) cy.resize();
    };
    const up = () => { sp.classList.remove('drag'); window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); if (cy) cy.resize(); };
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
  });
});

// フォルダ選択ダイアログ
async function browse(path) {
  try {
    const d = await api('/api/browse' + (path ? `?path=${encodeURIComponent(path)}` : ''));
    $('#browsePath').value = d.path;
    $('#upBtn').dataset.path = d.parent || '';
    $('#upBtn').disabled = !d.parent;
    $('#drives').innerHTML = (d.drives || []).map((x) => `<button class="ghost" data-path="${esc(x)}">${esc(x)}</button>`).join('')
      + (d.home ? `<button class="ghost" data-path="${esc(d.home)}">🏠 ホーム</button>` : '');
    $('#dirList').innerHTML = d.dirs.map((x) => `<div data-name="${esc(x)}">📁 ${esc(x)}</div>`).join('') || '<div class="muted">サブフォルダなし</div>';
    if (d.error) toast(d.error, true);
  } catch (e) { toast(e.message, true); }
}
$('#browseBtn').addEventListener('click', () => { $('#modal').classList.remove('hidden'); browse($('#rootInput').value.trim()); });
$('#modalClose').addEventListener('click', () => $('#modal').classList.add('hidden'));
$('#upBtn').addEventListener('click', (ev) => browse(ev.target.dataset.path));
$('#drives').addEventListener('click', (ev) => { const b = ev.target.closest('[data-path]'); if (b) browse(b.dataset.path); });
$('#browsePath').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') browse(ev.target.value); });
$('#dirList').addEventListener('click', (ev) => {
  const d = ev.target.closest('[data-name]'); if (!d) return;
  const cur = $('#browsePath').value;
  const sep = cur.includes('\\') ? '\\' : '/';
  browse(cur.replace(/[\\/]$/, '') + sep + d.dataset.name);
});
$('#pickBtn').addEventListener('click', () => { $('#rootInput').value = $('#browsePath').value; $('#modal').classList.add('hidden'); $('#loadBtn').click(); });

// 外部エディタでの変更を検知
async function checkExternal() {
  if (!$('#autoReload').checked || !S.data || S.scanning || document.hidden) return;
  try {
    const { stamp } = await api('/api/stamp');
    if (stamp !== S.stamp) {
      S.stamp = stamp;
      for (const t of S.tabs) {
        const d = await api(`/api/file?path=${encodeURIComponent(t.path)}`).catch(() => null);
        if (isDirty(t)) { if (d && d.mtime !== t.mtime) t.conflict = true; continue; }
        if (d && d.mtime !== t.mtime && d.content !== t.model.getValue()) {
          t.model.setValue(d.content); t.saved = t.model.getAlternativeVersionId(); t.mtime = d.mtime;
        }
      }
      await scan(null, { keepView: true });
      renderTabs();
    }
  } catch (e) { /* サーバ停止中など */ }
}
setInterval(checkExternal, 3000);
document.addEventListener('visibilitychange', checkExternal);

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// ---------------------------------------------------------------------------
// 起動
// ---------------------------------------------------------------------------
(async function boot() {
  try { const th = localStorage.getItem('cf.theme'); if (th) document.documentElement.dataset.theme = th; } catch (e) { /* 無視 */ }
  busy(true, 'ライブラリ読み込み中…');
  try {
    for (const [local, cdn] of LIBS) await loadFirst([local, cdn]);
    if (window.cytoscapeDagre) cytoscape.use(window.cytoscapeDagre);
  } catch (e) {
    busy(false);
    toast(e.message + ' — ネットに繋がらない環境では `python -m codeflow.vendor` で事前取得してください', true, 15000);
    return;
  }
  initCy();
  initMonaco().catch((e) => toast('エディタの読み込みに失敗: ' + e.message, true, 8000));
  busy(false);
  const st = await api('/api/state').catch(() => ({}));
  let root = st.root;
  if (!root) { try { root = localStorage.getItem('cf.root'); } catch (e) { /* 無視 */ } }
  if (root) { $('#rootInput').value = root; scan(root); }
})();
