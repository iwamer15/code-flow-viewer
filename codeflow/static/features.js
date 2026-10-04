/* Code Flow Viewer - 表示状態の保存 / 書き出し / 全ファイル検索・置換 / ファイル操作 */
'use strict';

// ---------------------------------------------------------------------------
// 表示状態の保存 (フォルダごとに localStorage へ)
// ---------------------------------------------------------------------------
function viewKey() { return 'cf.view:' + (S.data?.root || ''); }
let restoring = false;
const saveViewState = debounce(() => {
  if (!S.data || restoring) return;
  const st = {
    view: S.view, entry: S.entry, depth: S.depth, dir: S.dir, showExt: S.showExt, scope: S.scope,
    expanded: [...S.expanded].slice(0, 200), collapsed: [...S.collapsed].slice(0, 500),
    modExpanded: S.modExpanded ? [...S.modExpanded].slice(0, 500) : null,
    chartSym: S.chartSym, tabs: S.tabs.map((t) => t.path).slice(0, 20), active: S.active,
  };
  try { localStorage.setItem(viewKey(), JSON.stringify(st)); } catch (e) { /* 無視 */ }
}, 400);

// scan() の中から呼ばれる (フォルダを読み込んだ直後、最初の描画の前)
function restoreViewState() {
  let st = null;
  try { st = JSON.parse(localStorage.getItem(viewKey()) || 'null'); } catch (e) { st = null; }
  if (!st) return;
  restoring = true;
  if (st.entry && S.sym[st.entry]) S.entry = st.entry;
  S.depth = Math.max(1, Math.min(30, +st.depth || 4));
  S.dir = st.dir === 'LR' ? 'LR' : 'TB';
  S.showExt = !!st.showExt;
  S.scope = st.scope || '';
  S.expanded = new Set((st.expanded || []).filter((id) => S.sym[id]));
  S.collapsed = new Set(st.collapsed || []);
  S.modExpanded = st.modExpanded ? new Set(st.modExpanded) : null;
  S.chartSym = st.chartSym && S.sym[st.chartSym] ? st.chartSym : null;
  S.view = ['flow', 'all', 'modules', 'chart'].includes(st.view) ? st.view : 'flow';
  if (S.view === 'chart' && !S.chartSym) S.view = 'flow';
  $('#depthInp').value = S.depth;
  $('#dirBtn').textContent = S.dir === 'TB' ? '↓ 縦' : '→ 横';
  $('#extChk').checked = S.showExt;
  $('#scopeInp').value = S.scope;
  $$('#viewSeg button').forEach((b) => b.classList.toggle('on', b.dataset.view === S.view));
  const tabs = (st.tabs || []).filter((p) => S.files.has(p));
  setTimeout(async () => {
    try {
      for (const p of tabs) await openFile(p);
      if (st.active && S.tabs.some((t) => t.path === st.active)) await openFile(st.active);
    } finally { restoring = false; saveViewState(); }
  }, 0);
}

// 描画やタブの変化のたびに保存 (app.js の関数を包む)
for (const name of ['render', 'renderTabs']) {
  const orig = window[name];
  window[name] = function (...args) { const r = orig.apply(this, args); saveViewState(); return r; };
}

// ---------------------------------------------------------------------------
// 書き出し (Mermaid / Markdown / HTML / SVG / PNG)
// ---------------------------------------------------------------------------
function exportTitle() {
  const s = S.sym[S.view === 'chart' ? S.chartSym : S.entry];
  if (S.view === 'chart' && s) return `${symLabel(s)} のフローチャート`;
  if (S.view === 'flow' && s) return `起動フロー: ${symLabel(s)} (${s.f})`;
  if (S.view === 'all') return '関数の呼び出し関係' + (S.scope ? ` (${S.scope})` : '');
  return 'モジュール依存' + (S.scope ? ` (${S.scope})` : '');
}
function exportName(ext) {
  const t = exportTitle().replace(/[\\/:*?"<>|\s()]+/g, '_').replace(/_+/g, '_').slice(0, 60);
  return `codeflow_${t}.${ext}`;
}

// いま表示している図 (Cytoscape) を Mermaid 記法に
function viewToMermaid() {
  const nodes = cy.nodes();
  if (!nodes.length) throw new Error('図が空です');
  const dir = S.rankDir || S.dir;
  const L = [`flowchart ${dir}`];
  const ids = new Map();
  const cls = { entry: [], ran: [], active: [], current: [], err: [], notrun: [] };
  nodes.forEach((n, i) => {
    const k = 'n' + i;
    ids.set(n.id(), k);
    const lines = String(n.data('label') || '').split('\n').filter((x) => !x.startsWith('＋') && !x.includes('クリックで展開'));
    const lab = `"${lines.map((x, j) => (j ? `<small>${mmText(x, 60)}</small>` : mmText(x, 60))).join('<br/>')}"`;
    const kind = n.data('kind');
    const shape = n.hasClass('ext') ? `(${lab})` : n.hasClass('dirnode') ? `[(${lab})]` : kind === 'toplevel' ? `([${lab}])` : kind === 'class' ? `{{${lab}}}` : `[${lab}]`;
    L.push(`  ${k}${shape}`);
    for (const c of Object.keys(cls)) if (n.hasClass(c)) cls[c].push(k);
  });
  cy.edges().forEach((e) => {
    const a = ids.get(e.source().id()), b = ids.get(e.target().id());
    if (!a || !b) return;
    const lab = e.data('label');
    const dotted = e.hasClass('ref') || e.hasClass('amb') || e.hasClass('back') || e.hasClass('extE');
    const arrow = dotted ? '-.->' : '-->';
    L.push(lab ? `  ${a} ${arrow}|"${mmText(lab, 12)}"| ${b}` : `  ${a} ${arrow} ${b}`);
  });
  const defs = {
    entry: 'stroke:#16a34a,stroke-width:3px', ran: 'stroke:#16a34a,stroke-width:2px',
    active: 'stroke:#f59e0b,stroke-width:3px', current: 'fill:#fde68a,stroke:#f59e0b,stroke-width:4px',
    err: 'fill:#fee2e2,stroke:#dc2626,stroke-width:4px', notrun: 'opacity:0.45',
  };
  for (const [c, list] of Object.entries(cls)) {
    if (!list.length) continue;
    L.push(`  classDef ${c} ${defs[c]}`);
    L.push(`  class ${list.join(',')} ${c}`);
  }
  return L.join('\n');
}
function currentMermaid() {
  if (S.view === 'chart') {
    if (!S.chartCode) throw new Error('フローチャートがまだありません');
    return S.chartCode;
  }
  if (cy.nodes().length > 400 && !confirm(`${cy.nodes().length} 個のノードがあります。Mermaid では見づらくなります。続けますか？\n(パスで絞り込むか、深さを下げるのがおすすめです)`)) throw new Error('中止しました');
  return viewToMermaid();
}
// 書き出し用の SVG (白背景)。ラベルは画面と同じ HTML 方式で描き、最後に XML として整形し直す
// (Mermaid の SVG テキスト方式は _ や * を Markdown として解釈して文字が変わってしまうため使わない)
async function exportSvg(code) {
  mermaid.initialize({ startOnLoad: false, theme: 'default', securityLevel: 'strict', htmlLabels: true, flowchart: { htmlLabels: true, curve: 'basis', useMaxWidth: false }, maxTextSize: 900000, maxEdges: 5000 });
  try {
    const { svg } = await mermaid.render('cfexp' + Date.now(), code);
    const el = new DOMParser().parseFromString(svg, 'text/html').querySelector('svg');
    el.style.backgroundColor = '#ffffff';
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + new XMLSerializer().serializeToString(el);
  } finally {
    document.querySelectorAll('[id^="dcfexp"]').forEach((x) => x.remove());
  }
}
function download(name, data, type) {
  const blob = data instanceof Blob ? data : new Blob([data], { type });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
function nowStr() {
  const d = new Date();
  const z = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())} ${z(d.getHours())}:${z(d.getMinutes())}`;
}
async function svgToPng(svg) {
  const doc = new DOMParser().parseFromString(svg.replace(/^<\?xml[^>]*>\s*/, ''), 'image/svg+xml').documentElement;
  const vb = (doc.getAttribute('viewBox') || '0 0 800 600').split(/[\s,]+/).map(Number);
  const w = vb[2] || 800, h = vb[3] || 600, k = Math.min(3, 8000 / Math.max(w, h));
  doc.setAttribute('width', w); doc.setAttribute('height', h);
  const url = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(new XMLSerializer().serializeToString(doc));
  const img = new Image();
  await new Promise((ok, ng) => { img.onload = ok; img.onerror = () => ng(new Error('画像に変換できませんでした')); img.src = url; });
  const c = document.createElement('canvas');
  c.width = Math.round(w * k); c.height = Math.round(h * k);
  const g = c.getContext('2d');
  g.fillStyle = '#ffffff'; g.fillRect(0, 0, c.width, c.height);
  g.drawImage(img, 0, 0, c.width, c.height);
  try {
    return await new Promise((ok, ng) => c.toBlob((b) => (b ? ok(b) : ng(new Error('PNG を作れませんでした'))), 'image/png'));
  } catch (e) {
    throw new Error('このブラウザでは PNG に変換できません。SVG で保存してください (' + e.message + ')');
  }
}

async function doExport(kind) {
  $('#exportMenu').classList.add('hidden');
  try {
    if (kind === 'png' && S.view !== 'chart') {
      download(exportName('png'), await (await fetch(cy.png({ full: true, scale: 2, bg: '#ffffff' }))).blob());
      return;
    }
    const code = currentMermaid();
    const title = exportTitle();
    if (kind === 'copy') {
      await navigator.clipboard.writeText('```mermaid\n' + code + '\n```\n');
      toast('Mermaid 記法をコピーしました (```mermaid 〜 ``` 付き)');
      return;
    }
    if (kind === 'md') {
      const md = `# ${title}\n\n- フォルダ: \`${S.data.root}\`\n- 作成: ${nowStr()} (Code Flow Viewer)\n\n\`\`\`mermaid\n${code}\n\`\`\`\n`;
      download(exportName('md'), md, 'text/markdown;charset=utf-8');
      return;
    }
    busy(true, '書き出し中…');
    const svg = await exportSvg(code);
    if (kind === 'svg') download(exportName('svg'), svg, 'image/svg+xml;charset=utf-8');
    if (kind === 'png') download(exportName('png'), await svgToPng(svg));
    if (kind === 'html') {
      const html = `<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(title)}</title>
<style>body{font-family:system-ui,-apple-system,"Segoe UI","Hiragino Sans",Meiryo,sans-serif;margin:24px;background:#fff;color:#1d2330}
h1{font-size:20px;margin:0 0 4px}.meta{color:#6b7486;font-size:13px;margin:0 0 16px}.fig{overflow:auto;border:1px solid #dde1e7;border-radius:8px;padding:12px}
.fig svg{max-width:100%;height:auto}details{margin-top:16px}pre{background:#f6f7f9;padding:12px;border-radius:6px;overflow:auto;font-size:12px}</style></head>
<body><h1>${esc(title)}</h1><p class="meta">${esc(S.data.root)} ・ ${nowStr()} ・ Code Flow Viewer で作成</p>
<div class="fig">${svg}</div>
<details><summary>Mermaid ソース (README や Obsidian に貼り付け可)</summary><pre>${esc(code)}</pre></details>
</body></html>`;
      download(exportName('html'), html, 'text/html;charset=utf-8');
    }
  } catch (e) {
    if (e.message !== '中止しました') toast('書き出しに失敗: ' + e.message, true, 5000);
  } finally {
    busy(false);
    if (S.view === 'chart') renderChart();   // 書き出し用の設定から画面用に戻す
  }
}
$('#exportBtn').addEventListener('click', (ev) => { ev.stopPropagation(); $('#exportMenu').classList.toggle('hidden'); });
$('#exportMenu').addEventListener('click', (ev) => { const b = ev.target.closest('[data-exp]'); if (b) doExport(b.dataset.exp); });
document.addEventListener('click', (ev) => {
  if (!ev.target.closest('#exportMenu')) $('#exportMenu').classList.add('hidden');
  if (!ev.target.closest('#ctxMenu')) $('#ctxMenu').classList.add('hidden');
});

// ---------------------------------------------------------------------------
// 全ファイル検索・置換
// ---------------------------------------------------------------------------
const SR = { last: null, seq: 0 };
['#sCase', '#sWord', '#sRe'].forEach((id) => $(id).addEventListener('click', () => { $(id).classList.toggle('on'); runSearch(); }));
function searchOpts() {
  return { q: $('#sq').value, case: $('#sCase').classList.contains('on'), word: $('#sWord').classList.contains('on'), regex: $('#sRe').classList.contains('on'), include: $('#sInc').value };
}
async function runSearch() {
  const o = searchOpts();
  const box = $('#sResults');
  if (!o.q || o.q.length < 2 || !S.data) { box.innerHTML = ''; $('#sInfo').textContent = o.q ? '2文字以上で検索します' : ''; SR.last = null; return; }
  const my = ++SR.seq;
  $('#sInfo').textContent = '検索中…';
  let r;
  try { r = await api('/api/search', { body: o }); } catch (e) { if (my === SR.seq) $('#sInfo').textContent = e.message; return; }
  if (my !== SR.seq) return;   // 古い検索結果は捨てる
  SR.last = { ...o, files: r.files };
  $('#sInfo').textContent = `${r.files.length} ファイル / ${r.total} 件${r.truncated ? ' (5000件で打ち切り)' : ''} · ${r.ms}ms`;
  box.innerHTML = r.files.map((f) => {
    const color = LANG_COLOR[S.files.get(f.p)?.lang] || '#888';
    return `<div class="sfile" data-file="${esc(f.p)}" title="${esc(f.p)}"><span class="dot" style="background:${color}"></span>${esc(base(f.p))} <span class="muted" style="font-weight:400;font-size:11px">${esc(f.p.includes('/') ? f.p.slice(0, f.p.lastIndexOf('/')) : '')}</span><span class="cnt">${f.m.length}</span></div>`
      + f.m.slice(0, 200).map((m) => `<div class="shit" data-file="${esc(f.p)}" data-l="${m.l}" data-c="${m.c}" data-n="${m.n}"><span class="ln">${m.l}</span><span>${m.cut ? '…' : ''}${esc(m.t.slice(0, m.s))}<mark>${esc(m.t.slice(m.s, m.s + m.n))}</mark>${esc(m.t.slice(m.s + m.n))}</span></div>`).join('');
  }).join('') || '<div class="ct-head">見つかりませんでした</div>';
}
$('#sq').addEventListener('input', debounce(runSearch, 400));
$('#sq').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') runSearch(); });
$('#sInc').addEventListener('input', debounce(runSearch, 500));
$('#sResults').addEventListener('click', async (ev) => {
  const h = ev.target.closest('.shit');
  if (h) {
    const line = +h.dataset.l, col = +h.dataset.c + 1, n = +h.dataset.n;
    await openFile(h.dataset.file, line, null, true);
    editor.setSelection(new monaco.Range(line, col, line, col + n));
    editor.revealRangeInCenter(new monaco.Range(line, col, line, col + n));
    editor.focus();
    return;
  }
  const f = ev.target.closest('.sfile');
  if (f) openFile(f.dataset.file);
});
$('#sRepAll').addEventListener('click', async () => {
  const o = searchOpts();
  if (!SR.last || SR.last.q !== o.q) await runSearch();
  if (!SR.last || !SR.last.files.length) { toast('置換する対象がありません'); return; }
  const rep = $('#sRep').value;
  const dirty = new Set(S.tabs.filter(isDirty).map((t) => t.path));
  const files = SR.last.files.map((f) => f.p).filter((p) => !dirty.has(p));
  const skipped = SR.last.files.length - files.length;
  const count = SR.last.files.filter((f) => !dirty.has(f.p)).reduce((t, f) => t + f.m.length, 0);
  if (!files.length) { toast('対象はすべて未保存のタブです。先に保存してください', true); return; }
  if (!confirm(`${files.length} ファイルの ${count} 件を「${rep}」に置換します。\n${skipped ? `(未保存のタブ ${skipped} ファイルは対象外)\n` : ''}保存前の内容は自動でバックアップされ、🕘 履歴から戻せます。`)) return;
  try {
    const r = await api('/api/replace', { body: { ...o, rep, files } });
    for (const c of r.changed) {
      const t = S.tabs.find((x) => x.path === c.p);
      if (t && !isDirty(t)) {
        const d = await api(`/api/file?path=${encodeURIComponent(c.p)}`);
        t.model.setValue(d.content); t.saved = t.model.getAlternativeVersionId(); t.mtime = d.mtime;
      }
    }
    toast(`${r.changed.length} ファイル / ${r.total} 件を置換しました`);
    await scan(null, { keepView: true });
    runSearch();
  } catch (e) { toast('置換に失敗: ' + e.message, true, 5000); }
});

// ---------------------------------------------------------------------------
// ファイルツリーの右クリックメニュー (名前変更・削除・新規・パスのコピー)
// ---------------------------------------------------------------------------
function showCtx(x, y, items) {
  const m = $('#ctxMenu');
  m.innerHTML = items.map((it, i) => (it === '-' ? '<hr>' : `<button data-i="${i}" class="${it.danger ? 'danger' : ''}">${esc(it.label)}</button>`)).join('');
  m.classList.remove('hidden');
  const r = m.getBoundingClientRect();
  m.style.left = Math.min(x, window.innerWidth - r.width - 8) + 'px';
  m.style.top = Math.min(y, window.innerHeight - r.height - 8) + 'px';
  m.onclick = (ev) => { const b = ev.target.closest('[data-i]'); if (!b) return; m.classList.add('hidden'); items[+b.dataset.i].run(); };
}
function tabsUnder(path) { return S.tabs.filter((t) => t.path === path || t.path.startsWith(path + '/')); }

async function renamePath(path) {
  const open = tabsUnder(path);
  if (open.some(isDirty)) { toast('未保存のタブがあります。保存してから名前を変更してください', true); return; }
  const to = prompt('新しいパス (読み込んだフォルダからの相対パス。フォルダ名を変えると移動になります)', path);
  if (!to || to === path) return;
  try {
    await api('/api/rename', { body: { path, to } });
    const reopen = open.map((t) => to + t.path.slice(path.length));
    for (const t of open) closeTab(t.path);
    await scan(null, { keepView: true });
    for (const p of reopen) await openFile(p);
    toast(`${path} → ${to}`);
  } catch (e) { toast(e.message, true, 5000); }
}
async function deletePath(path, isDir) {
  if (!confirm(`${isDir ? 'フォルダ' : 'ファイル'}「${path}」を削除します。\n(完全には消さず ~/.codeflow/trash/ に移動します)`)) return;
  try {
    const r = await api('/api/delete', { body: { path } });
    for (const t of tabsUnder(path)) { t.model.setValue(t.model.getValue()); t.saved = t.model.getAlternativeVersionId(); closeTab(t.path); }
    await scan(null, { keepView: true });
    toast('ゴミ箱へ移動しました: ' + r.trash, false, 5000);
  } catch (e) { toast(e.message, true, 5000); }
}
async function newFileIn(dir) {
  const p = prompt('作成するファイルのパス', dir ? dir + '/new_file.py' : 'new_file.py');
  if (!p) return;
  try { await api('/api/file', { body: { path: p, content: '', create: true } }); await scan(null, { keepView: true }); openFile(p); }
  catch (e) { toast(e.message, true); }
}
$('#fileTree').addEventListener('contextmenu', (ev) => {
  const t = ev.target.closest('.tnode');
  if (!t || (!t.dataset.file && !t.dataset.dir)) return;
  ev.preventDefault();
  const path = t.dataset.file || t.dataset.dir;
  const isDir = !!t.dataset.dir;
  const dir = isDir ? path : (path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '');
  showCtx(ev.clientX, ev.clientY, [
    ...(isDir ? [] : [{ label: '開く', run: () => openFile(path) }]),
    { label: 'ここに新しいファイル', run: () => newFileIn(dir) },
    { label: '名前の変更・移動…', run: () => renamePath(path) },
    { label: 'パスをコピー', run: () => navigator.clipboard.writeText(path).then(() => toast('コピーしました: ' + path)) },
    { label: '検索の対象をここに絞る', run: () => { $('#sInc').value = isDir ? path + '/' : path; $$('.tabs button').find((b) => b.dataset.side === 'search').click(); $('#sq').focus(); runSearch(); } },
    '-',
    { label: '削除 (ゴミ箱へ)', danger: true, run: () => deletePath(path, isDir) },
  ]);
});
// Ctrl/⌘+Shift+F で検索タブへ
window.addEventListener('keydown', (ev) => {
  if ((ev.ctrlKey || ev.metaKey) && ev.shiftKey && (ev.key === 'f' || ev.key === 'F')) {
    ev.preventDefault();
    $$('.tabs button').find((b) => b.dataset.side === 'search').click();
    const sel = editor?.getModel() && editor.getModel().getValueInRange(editor.getSelection());
    if (sel && !sel.includes('\n')) $('#sq').value = sel;
    $('#sq').focus(); $('#sq').select();
    runSearch();
  }
});
