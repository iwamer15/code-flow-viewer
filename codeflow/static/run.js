/* Code Flow Viewer - 実行パネル (実行・出力・進み具合の表示) */
'use strict';

const R = {
  seq: 0, timer: null, running: false, done: false, snap: null, traced: false,
  lines: [''], cr: false, seen: new Set(), ran: new Map(), stackIds: [], threadIds: [], errSid: null,
  symCache: new Map(), execDeco: [], errDeco: [], errDecoPath: null, execPath: null,
};
window.R = R;

// 起点の言語に合わせたコマンドの提案
const CMD_HINT = {
  javascript: (f) => `node ${f}`, typescript: (f) => `npx tsx ${f}`, go: () => 'go run .', rust: () => 'cargo run',
  java: (f) => `java ${f}`, shell: (f) => `bash ${f}`, powershell: (f) => `powershell -ExecutionPolicy Bypass -File ${f}`,
  ruby: (f) => `ruby ${f}`, php: (f) => `php ${f}`, lua: (f) => `lua ${f}`, r: (f) => `Rscript ${f}`, dart: (f) => `dart run ${f}`,
  c: (f) => `gcc ${f} -o app && ./app`, cpp: (f) => `g++ ${f} -o app && ./app`, kotlin: (f) => `kotlinc ${f} -include-runtime -d app.jar && java -jar app.jar`,
};

function runKey() { return 'cf.run:' + (S.data?.root || ''); }
function saveRunPrefs() {
  try {
    localStorage.setItem(runKey(), JSON.stringify({
      mode: $('#runMode').value, target: $('#runTarget').value, args: $('#runArgs').value, cmd: $('#runCmd').value,
      python: $('#runPython').value, cwd: $('#runCwd').value,
    }));
  } catch (e) { /* 無視 */ }
}
function loadRunPrefs() {
  try { return JSON.parse(localStorage.getItem(runKey()) || 'null'); } catch (e) { return null; }
}

// 読み込み・起点変更時に、実行対象の候補と既定値を用意する
async function prepareRun() {
  if (!S.data) return;
  const py = S.data.files.filter((f) => f.lang === 'python');
  const entryFiles = new Set(S.data.entries.filter((e) => e.score >= 30).map((e) => S.sym[e.id]?.f).filter((f) => f && f.endsWith('.py')));
  const ordered = [...entryFiles, ...py.map((f) => f.p).filter((p) => !entryFiles.has(p))];
  $('#pyFiles').innerHTML = ordered.slice(0, 500).map((p) => `<option value="${esc(p)}">`).join('');
  const prefs = loadRunPrefs();
  if (prefs) {
    $('#runMode').value = prefs.mode || 'python';
    $('#runTarget').value = prefs.target || '';
    $('#runArgs').value = prefs.args || '';
    $('#runCmd').value = prefs.cmd || '';
    $('#runCwd').value = prefs.cwd || '';
  }
  try {
    const cfg = await api('/api/run/config');
    if (!prefs?.python) $('#runPython').value = cfg.python || '';
    else $('#runPython').value = prefs.python;
    $('#runPython').title = cfg.venv ? 'プロジェクト内の仮想環境を自動で選びました' : 'このツールを動かしている Python';
  } catch (e) { /* 無視 */ }
  if (!prefs) suggestFromEntry();
  updateRunMode();
}
function suggestFromEntry() {
  const s = S.sym[S.entry]; if (!s) return;
  const lang = S.files.get(s.f)?.lang;
  if (lang === 'python') { $('#runMode').value = 'python'; $('#runTarget').value = s.f; }
  else if (CMD_HINT[lang]) {
    $('#runMode').value = 'command';
    const dir = s.f.includes('/') ? s.f.slice(0, s.f.lastIndexOf('/')) : '';
    const local = ['go', 'rust'].includes(lang) ? '' : base(s.f);
    $('#runCmd').value = CMD_HINT[lang](local || s.f);
    $('#runCwd').value = ['go', 'rust', 'javascript', 'typescript', 'java', 'c', 'cpp'].includes(lang) ? dir : '';
    if (lang === 'javascript' || lang === 'typescript') { $('#runCmd').value = CMD_HINT[lang](base(s.f)); }
  }
  updateRunMode();
}
function updateRunMode() {
  const m = $('#runMode').value;
  $$('#runPanel [data-run]').forEach((el) => el.classList.toggle('hidden', el.dataset.run !== m));
}

function toggleRunPanel(show) {
  const p = $('#runPanel');
  const on = show ?? p.classList.contains('hidden');
  p.classList.toggle('hidden', !on);
  $('#runSplit').classList.toggle('hidden', !on);
  if (cy) setTimeout(() => cy.resize(), 50);
}

// ---------------------------------------------------------------------------
// 実行
// ---------------------------------------------------------------------------
async function startRun() {
  if (!S.data) { toast('先にフォルダを読み込んでください', true); return; }
  const dirty = S.tabs.filter(isDirty);
  if (dirty.length && confirm(`未保存のファイルがあります (${dirty.map((t) => base(t.path)).join(', ')})。保存してから実行しますか？`)) {
    for (const t of dirty) { S.active = t.path; editor.setModel(t.model); await saveActive(); }
  }
  const mode = $('#runMode').value;
  const spec = { mode, cwd: $('#runCwd').value.trim() };
  if (mode === 'python') {
    const target = $('#runTarget').value.trim();
    if (!target) { toast('実行するファイルを入力してください', true); return; }
    const m = /^-m\s+(\S+)$/.exec(target);
    if (m) spec.module = m[1]; else spec.target = target;
    spec.args = $('#runArgs').value;
    spec.python = $('#runPython').value.trim();
  } else {
    spec.command = $('#runCmd').value;
  }
  saveRunPrefs();
  resetRun();
  try {
    const r = await api('/api/run/start', { body: spec });
    appendConsole(`$ ${r.cmd}\n`, true);
  } catch (e) { toast(e.message, true, 6000); return; }
  R.running = true; R.traced = mode === 'python';
  setRunButtons();
  clearInterval(R.timer);
  R.timer = setInterval(pollRun, 300);
  pollRun();
}

function resetRun() {
  Object.assign(R, { seq: 0, done: false, snap: null, lines: [''], cr: false, seen: new Set(), ran: new Map(), stackIds: [], threadIds: [], errSid: null });
  R.symCache.clear();
  $('#console').textContent = '';
  $('#hotTable').innerHTML = '';
  $('#runStack').innerHTML = '<span class="muted">—</span>';
  clearEditorRunDecos();
  if (cy) cy.elements().removeClass('ran notrun active current err activeE');
}

async function pollRun() {
  let s;
  try { s = await api(`/api/run/status?since=${R.seq}`); } catch (e) { return; }
  if (s.none) return;
  if (s.truncated) appendConsole('\n…(古い出力は省略しました)…\n');
  appendConsole(s.text);
  R.seq = s.seq;
  R.running = s.running;
  R.traced = s.traced;
  if (s.snapshot) {
    R.snap = s.snapshot;
    for (const fr of [...(R.snap.stack || []), ...(R.snap.threads || []).flat()]) {
      const sid = symForFrame(fr[0], fr[1], fr[2], fr[3]); if (sid) R.seen.add(sid);
    }
  }
  if (!s.running) {
    R.done = true;
    clearInterval(R.timer); R.timer = null;
  }
  const st = $('#runStatus');
  if (s.running) { st.textContent = `実行中… ${s.elapsed.toFixed(1)}秒`; st.className = 'live'; }
  else {
    st.textContent = `終了 (コード ${s.code}) ${s.elapsed.toFixed(1)}秒`;
    st.className = s.code === 0 ? 'ok' : 'bad';
    if (R.snap?.exc) st.textContent += ` — ${R.snap.exc.type}: ${R.snap.exc.msg}`;
  }
  setRunButtons();
  applyRunState();
  renderHot();
}

function setRunButtons() {
  $('#runBtn').disabled = R.running;
  $('#stopBtn').disabled = !R.running;
  $('#stdinInp').disabled = !R.running;
  $('#runToggle').textContent = R.running ? '● 実行中' : '▶ 実行';
  $('#runToggle').classList.toggle('primary', R.running);
}

// 出力 (\r で同じ行を書き換える進捗バーにも対応)
const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g;
function appendConsole(text, cmdLine = false) {
  if (!text) return;
  text = text.replace(ANSI, '').replace(/\r\n/g, '\n');
  text.split('\n').forEach((seg, i) => {
    if (i > 0) { R.lines.push(''); R.cr = false; }
    seg.split('\r').forEach((c, j) => {
      if (j > 0) R.cr = true;
      if (!c) return;
      const last = R.lines.length - 1;
      if (R.cr) { R.lines[last] = c; R.cr = false; } else R.lines[last] += c;
    });
  });
  if (R.lines.length > 3000) R.lines.splice(0, R.lines.length - 3000);
  const pre = $('#console');
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  pre.textContent = R.lines.join('\n');
  if (atBottom || cmdLine) pre.scrollTop = pre.scrollHeight;
}

// ---------------------------------------------------------------------------
// 実行情報 → 図の関数への対応付け
// ---------------------------------------------------------------------------
// トレーサから来る (ファイル, 先頭行, 関数名, 実行中の行) を解析結果の関数IDへ。
// デコレータ付き関数は先頭行がデコレータの行になるので「名前が同じで、先頭行以降で一番近い定義」を選ぶ。
function symForFrame(rel, first, name, lineno) {
  const key = `${rel}|${first}|${name}|${name === '<module>' ? lineno : ''}`;
  if (R.symCache.has(key)) return R.symCache.get(key);
  let res = null;
  const f = S.files.get(rel);
  if (f) {
    const syms = (f.sym || []).map((id) => [id, S.sym[id]]).filter(([, s]) => s);
    if (name === '<module>') {
      const guard = syms.find(([, s]) => s.q === '__main__' && lineno && s.s <= lineno && lineno <= s.e);
      const mod = guard || syms.find(([, s]) => s.q === '<module>') || syms.find(([, s]) => s.q === '__main__');
      res = mod ? mod[0] : null;
    } else {
      let best = null;
      for (const [id, s] of syms) {
        if (s.n === name && s.k !== 'class' && s.s >= first && (!best || s.s < S.sym[best].s)) best = id;
      }
      res = best;
    }
  }
  R.symCache.set(key, res);
  return res;
}

function applyRunState() {
  if (!R.snap) return;
  const ran = new Map();
  for (const [rel, first, name, calls, incl, self] of R.snap.fn || []) {
    const sid = symForFrame(rel, first, name, null); if (!sid) continue;
    const r = ran.get(sid) || { calls: 0, incl: 0, self: 0 };
    r.calls += calls; r.incl = Math.max(r.incl, incl); r.self += self;
    ran.set(sid, r);
  }
  for (const sid of R.seen) if (!ran.has(sid)) ran.set(sid, { calls: 0, incl: 0, self: 0 });
  R.ran = ran;
  R.stackIds = R.running ? (R.snap.stack || []).map((fr) => symForFrame(...fr)).filter(Boolean) : [];
  R.threadIds = R.running ? (R.snap.threads || []).flat().map((fr) => symForFrame(...fr)).filter(Boolean) : [];
  R.errSid = null;
  const exc = R.snap.exc;
  if (exc?.where) {
    const f = S.files.get(exc.where[0]);
    let best = null;
    for (const id of f?.sym || []) {
      const s = S.sym[id];
      if (s && s.k !== 'class' && s.s <= exc.where[1] && exc.where[1] <= s.e && (!best || s.s >= S.sym[best].s)) best = id;
    }
    R.errSid = best;
  }
  if (cy && S.view !== 'chart') {
    cy.batch(() => {
      cy.elements().removeClass('ran notrun active current err activeE');
      cy.nodes().forEach((n) => {
        const sid = n.data('sid');
        if (sid) {
          if (ran.has(sid)) n.addClass('ran');
          else if (R.done && R.traced && n.data('kind') !== 'class') n.addClass('notrun');
        } else if (n.data('file') && S.view === 'modules') {
          const f = S.files.get(n.data('file'));
          if ((f?.sym || []).some((id) => ran.has(id))) n.addClass('ran');
          else if (R.done && R.traced && f?.lang === 'python') n.addClass('notrun');
          if (R.stackIds.some((id) => S.sym[id]?.f === f?.p)) n.addClass('active');
        }
      });
      const chain = R.stackIds;
      chain.forEach((id) => cy.getElementById(id).addClass('active'));
      R.threadIds.forEach((id) => cy.getElementById(id).addClass('active'));
      for (let i = 1; i < chain.length; i++) cy.getElementById(`${chain[i - 1]}->${chain[i]}`).addClass('activeE');
      if (chain.length) cy.getElementById(chain[chain.length - 1]).addClass('current');
      if (R.errSid) cy.getElementById(R.errSid).addClass('err');
    });
  }
  // 呼び出しツリーの印を更新
  if (S.view === 'flow' && R.traced) {
    $$('#callTree .ct').forEach((el) => {
      const mark = el.querySelector('.rm');
      const html = runMark(el.dataset.sym);
      if (mark) mark.outerHTML = html || '<span class="rm"></span>';
      else el.children[1]?.insertAdjacentHTML('afterbegin', html || '<span class="rm"></span>');
    });
  }
  applyChartRun();
  applyEditorRun();
  renderLegend();
  if ($('#details').dataset.sid && !$('#details').classList.contains('hidden')) {
    const box = $('#details .runinfo');
    const html = runInfo($('#details').dataset.sid);
    if (box) box.outerHTML = html || '<div class="runinfo"></div>';
  }
}

function runMark(sid) {
  if (!R.snap || !R.traced) return '';
  if (sid === R.errSid) return '<span class="rm" style="color:var(--bad)">✖ </span>';
  if (R.stackIds.includes(sid)) return '<span class="rm" style="color:#f59e0b">▶ </span>';
  if (R.ran.has(sid)) return '<span class="rm" style="color:var(--ok)">✔ </span>';
  if (R.done) return '<span class="rm muted">· </span>';
  return '';
}
function runInfo(sid) {
  if (!R.snap || !R.traced) return '';
  const r = R.ran.get(sid);
  if (!r) return `<div class="runinfo sec">${R.done ? '今回の実行では呼ばれませんでした' : 'まだ呼ばれていません'}</div>`;
  return `<div class="runinfo sec" style="color:var(--text)">実行: ${r.calls ? r.calls + '回' : '実行済み'} · 累計 ${fmtMs(r.incl)} · 自身 ${fmtMs(r.self)}${R.stackIds.includes(sid) ? ' · <b style="color:#f59e0b">実行中</b>' : ''}</div>`;
}
function fmtMs(ms) { return ms >= 1000 ? (ms / 1000).toFixed(2) + 's' : Math.round(ms) + 'ms'; }

function renderHot() {
  if (!R.snap) return;
  if (!R.traced) { $('#hotTable').innerHTML = '<tr><td class="muted">コマンド実行では関数ごとの状況は表示されません（Python のみ対応）</td></tr>'; return; }
  const rows = [...R.ran.entries()].sort((a, b) => b[1].incl - a[1].incl || b[1].calls - a[1].calls).slice(0, 60);
  const max = Math.max(1, ...rows.map(([, r]) => r.incl));
  $('#hotTable').innerHTML = '<tr><th>関数</th><th>回数</th><th>累計</th><th>自身</th></tr>' + rows.map(([sid, r]) => {
    const s = S.sym[sid];
    return `<tr data-sym="${esc(sid)}" class="${R.stackIds.includes(sid) ? 'live' : ''}" title="${esc(s.f)}:${s.s}">`
      + `<td>${esc(symLabel(s))} <span class="muted">${esc(base(s.f))}</span></td><td>${r.calls || '-'}</td>`
      + `<td><span class="bar" style="width:${Math.round(40 * r.incl / max)}px"></span>${fmtMs(r.incl)}</td><td>${fmtMs(r.self)}</td></tr>`;
  }).join('');
  const st = R.snap.stack || [];
  $('#runStack').innerHTML = R.running && st.length
    ? st.map((fr, i) => `<div class="fr${i === st.length - 1 ? ' cur' : ''}" data-file="${esc(fr[0])}" data-line="${fr[3]}" style="padding-left:${i * 10}px">${i ? '└ ' : ''}${esc(fr[2])} <span class="muted">${esc(fr[0])}:${fr[3]}</span></div>`).join('')
    : (R.snap.exc ? `<div class="fr" style="color:var(--bad)" data-file="${esc(R.snap.exc.where?.[0] || '')}" data-line="${R.snap.exc.where?.[1] || ''}">✖ ${esc(R.snap.exc.type)}: ${esc(R.snap.exc.msg)}${R.snap.exc.where ? ` <span class="muted">${esc(R.snap.exc.where[0])}:${R.snap.exc.where[1]}</span>` : ''}</div>` : '<span class="muted">(停止中)</span>');
}

function nearestChartNode(line) {
  let best = null;
  for (const n of S.chartNodes) if (n.line <= line && (!best || n.line > best.line)) best = n;
  return best;
}
function applyChartRun() {
  if (S.view !== 'chart' || !S.chartNodes) return;
  S.chartNodes.forEach(({ g }) => g.classList.remove('cf-run', 'cf-err'));
  const sid = S.chartSym;
  const w = R.snap?.exc?.where;
  if (w && sid === R.errSid) { const n = nearestChartNode(w[1]); if (n) n.g.classList.add('cf-err'); }
  const i = R.stackIds.lastIndexOf(sid);
  if (i < 0) return;
  const fr = (R.snap.stack || []).filter((f) => symForFrame(...f))[i];
  if (!fr) return;
  const best = nearestChartNode(fr[3]);
  if (best) best.g.classList.add('cf-run');
}

function clearEditorRunDecos() {
  for (const t of S.tabs) {
    if (t.path === R.execPath) R.execDeco = t.model.deltaDecorations(R.execDeco, []);
    if (t.path === R.errDecoPath) R.errDeco = t.model.deltaDecorations(R.errDeco, []);
  }
  R.execDeco = []; R.errDeco = []; R.execPath = null; R.errDecoPath = null;
}
async function applyEditorRun() {
  if (!editor || !window.monaco) return;
  const st = R.running ? (R.snap?.stack || []) : [];
  const cur = st[st.length - 1];
  if (cur && $('#followChk').checked && S.active !== cur[0]) await openFile(cur[0]);
  for (const t of S.tabs) {
    if (t.path === R.execPath && (!cur || cur[0] !== t.path)) { R.execDeco = t.model.deltaDecorations(R.execDeco, []); R.execPath = null; }
  }
  if (cur) {
    const t = S.tabs.find((x) => x.path === cur[0]);
    if (t) {
      R.execDeco = t.model.deltaDecorations(R.execPath === t.path ? R.execDeco : [], [{
        range: new monaco.Range(cur[3], 1, cur[3], 1),
        options: { isWholeLine: true, className: 'cf-exec-line', glyphMarginClassName: 'cf-exec-glyph', glyphMarginHoverMessage: { value: `実行中: ${cur[2]}` } },
      }]);
      R.execPath = t.path;
      if ($('#followChk').checked && S.active === t.path) editor.revealLineInCenterIfOutsideViewport(cur[3]);
    }
  }
  const w = R.done && R.snap?.exc?.where;
  if (w) {
    const t = S.tabs.find((x) => x.path === w[0]);
    if (t) {
      R.errDeco = t.model.deltaDecorations(R.errDecoPath === t.path ? R.errDeco : [], [{ range: new monaco.Range(w[1], 1, w[1], 1), options: { isWholeLine: true, className: 'cf-err-line', glyphMarginHoverMessage: { value: `${R.snap.exc.type}: ${R.snap.exc.msg}` } } }]);
      R.errDecoPath = t.path;
    }
  }
}

// ---------------------------------------------------------------------------
// イベント
// ---------------------------------------------------------------------------
$('#runToggle').addEventListener('click', () => toggleRunPanel());
$('#runClose').addEventListener('click', () => toggleRunPanel(false));
$('#runBtn').addEventListener('click', startRun);
$('#stopBtn').addEventListener('click', () => api('/api/run/stop', { body: {} }).catch((e) => toast(e.message, true)));
$('#runMode').addEventListener('change', () => { updateRunMode(); saveRunPrefs(); });
$('#runOpts').addEventListener('click', () => $('#runOptsRow').classList.toggle('hidden'));
['#runTarget', '#runArgs', '#runCmd', '#runPython', '#runCwd'].forEach((sel) => {
  $(sel).addEventListener('change', saveRunPrefs);
  $(sel).addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && !R.running) startRun(); });
});
$('#stdinInp').addEventListener('keydown', async (ev) => {
  if (ev.key !== 'Enter') return;
  const text = ev.target.value; ev.target.value = '';
  appendConsole(text + '\n', true);
  await api('/api/run/input', { body: { text } }).catch((e) => toast(e.message, true));
});
$('#hotTable').addEventListener('click', (ev) => { const tr = ev.target.closest('[data-sym]'); if (tr) selectSym(tr.dataset.sym, { reveal: true }); });
$('#runStack').addEventListener('click', (ev) => {
  const fr = ev.target.closest('[data-file]'); if (fr && fr.dataset.file) openFile(fr.dataset.file, +fr.dataset.line, null, true);
});
$('#entrySel').addEventListener('change', () => { if (!loadRunPrefs()) suggestFromEntry(); });

// 実行パネルの高さ調整
$('#runSplit').addEventListener('mousedown', (ev) => {
  ev.preventDefault();
  const sp = $('#runSplit'), panel = $('#runPanel');
  sp.classList.add('drag');
  const startY = ev.clientY, startH = panel.getBoundingClientRect().height;
  const move = (e) => { panel.style.height = Math.max(120, Math.min(window.innerHeight - 200, startH - (e.clientY - startY))) + 'px'; if (cy) cy.resize(); };
  const up = () => { sp.classList.remove('drag'); window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
  window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
});

// 読み込み直後に実行設定を準備 (scan 完了を監視)
(function hookScan() {
  let lastRoot = null;
  setInterval(() => {
    if (S.data && S.data.root !== lastRoot) { lastRoot = S.data.root; prepareRun(); resetRun(); }
  }, 500);
})();
// ページを開いた時点で実行中のものがあれば追いかける
api('/api/run/status?since=0').then((s) => {
  if (s && s.running) { R.running = true; setRunButtons(); toggleRunPanel(true); R.timer = setInterval(pollRun, 300); }
}).catch(() => {});
