'use strict';
/*
 * Code Flow Viewer - Node.js 実行トレーサ
 *   node --require tracer_node.js <入口のファイル> [引数...]
 *
 * Node.js 標準の inspector を同じプロセス内で使う (追加インストール不要)。
 *   呼び出し回数 … V8 の Precise Coverage (callCount)
 *   時間・実行位置 … V8 の CPU プロファイラ (10ms 間隔のサンプリング)
 * 300ms ごとに状況を CODEFLOW_SNAPFILE (JSON) に書き出し、サーバがそれを読む。
 * メインスレッドが同期処理で塞がっている間は書き出せないので、表示はその処理が終わるまで止まる。
 */
const OUT = process.env.CODEFLOW_SNAPFILE;
if (OUT) start();

function start() {
  const inspector = require('inspector');
  const fs = require('fs');
  const path = require('path');
  const { fileURLToPath } = require('url');

  // Windows の短いパス名 (C:\Users\RUNNER~1\...) を正式な名前に直すため native 版を使う
  const realpath = fs.realpathSync.native || fs.realpathSync;
  const ROOT = realpath(process.env.CODEFLOW_ROOT || process.cwd());
  const COUNT = process.env.CODEFLOW_COUNT !== '0';
  const t0 = Date.now();
  const session = new inspector.Session();
  session.connect();

  // 同じスレッドのセッションは同期的に応答が返る
  function post(method, params) {
    let res = null, err = null;
    session.post(method, params || {}, (e, r) => { err = e; res = r; });
    if (err) throw err;
    return res;
  }

  const relCache = new Map();
  function relOf(u) {
    if (!u) return '';
    let r = relCache.get(u);
    if (r !== undefined) return r;
    r = '';
    try {
      const p = u.startsWith('file://') ? fileURLToPath(u) : (path.isAbsolute(u) ? u : '');
      if (p) {
        const rel = path.relative(ROOT, realpath(p));
        if (rel && !rel.startsWith('..') && !path.isAbsolute(rel)) {
          const parts = rel.split(path.sep);
          if (!parts.slice(0, -1).some((x) => x === 'node_modules' || x.startsWith('.'))) r = parts.join('/');
        }
      }
    } catch (e) { r = ''; }
    relCache.set(u, r);
    return r;
  }

  const lineStarts = new Map();
  function lineOf(u, offset) {
    let st = lineStarts.get(u);
    if (!st) {
      st = [0];
      try {
        const src = fs.readFileSync(u.startsWith('file://') ? fileURLToPath(u) : u, 'utf8');
        for (let i = 0; i < src.length; i++) if (src.charCodeAt(i) === 10) st.push(i + 1);
      } catch (e) { /* 無視 */ }
      lineStarts.set(u, st);
    }
    let lo = 0, hi = st.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (st[mid] <= offset) lo = mid; else hi = mid - 1; }
    return lo + 1;
  }
  // V8 の関数名 (exports.home / Foo.bar / get x) → 定義名
  function shortName(n) { n = String(n || ''); n = n.slice(n.lastIndexOf(' ') + 1); return n.slice(n.lastIndexOf('.') + 1); }

  const calls = new Map();   // key → 回数
  const incl = new Map();    // key → ms
  const selft = new Map();
  const keyInfo = new Map(); // key → [rel, line, name]
  let stack = [];
  let exc = null;

  function key(rel, line, name) {
    const k = `${rel}\u0000${line}\u0000${name}`;
    if (!keyInfo.has(k)) keyInfo.set(k, [rel, line, name]);
    return k;
  }

  post('Profiler.enable');
  post('Profiler.setSamplingInterval', { interval: 10000 });
  post('Profiler.start');
  if (COUNT) post('Profiler.startPreciseCoverage', { callCount: true, detailed: false });

  function collectCoverage() {
    if (!COUNT) return;
    const { result } = post('Profiler.takePreciseCoverage');   // 取得するたびにカウンタは 0 に戻る
    for (const script of result) {
      const rel = relOf(script.url);
      if (!rel) continue;
      for (const fn of script.functions) {
        const r = fn.ranges[0];
        if (!r || !r.count) continue;
        const top = r.startOffset === 0 && !fn.functionName;
        const name = top ? '<module>' : shortName(fn.functionName);
        if (!name) continue;   // 無名関数は図の関数に対応しない
        const k = key(rel, top ? 1 : lineOf(script.url, r.startOffset), name);
        calls.set(k, (calls.get(k) || 0) + r.count);
      }
    }
  }

  function collectProfile() {
    const { profile } = post('Profiler.stop');
    post('Profiler.start');
    const byId = new Map(profile.nodes.map((n) => [n.id, n]));
    const parent = new Map();
    for (const n of profile.nodes) for (const c of n.children || []) parent.set(c, n.id);
    const frameOf = new Map();   // node id → [rel, line, name] (プロジェクト外は null)
    const chainCache = new Map();
    function chain(id) {
      if (chainCache.has(id)) return chainCache.get(id);
      const out = [];
      let cur = id;
      while (cur !== undefined) {
        let f = frameOf.get(cur);
        if (f === undefined) {
          const cf = byId.get(cur).callFrame;
          const rel = relOf(cf.url);
          const name = cf.lineNumber === 0 && !cf.functionName ? '<module>' : shortName(cf.functionName);
          f = rel && name ? [rel, cf.lineNumber + 1, name] : null;
          frameOf.set(cur, f);
        }
        if (f) out.push(f);
        cur = parent.get(cur);
      }
      out.reverse();
      chainCache.set(id, out);
      return out;
    }
    const samples = profile.samples || [];
    const deltas = profile.timeDeltas || [];
    for (let i = 0; i < samples.length; i++) {
      const ms = (deltas[i] || 0) / 1000;
      const ch = chain(samples[i]);
      if (!ch.length) continue;
      const last = ch[ch.length - 1];
      const kl = key(last[0], last[1], last[2]);
      selft.set(kl, (selft.get(kl) || 0) + ms);
      for (const k of new Set(ch.map((f) => key(f[0], f[1], f[2])))) incl.set(k, (incl.get(k) || 0) + ms);
    }
    // 書き出しの瞬間は await 待ちで何も動いていないことが多いので、
    // 直前の区間で最後にプロジェクトのコードを実行していた位置を「実行中」とする
    let recent = [];
    for (let i = samples.length - 1; i >= 0; i--) {
      const ch = chain(samples[i]);
      if (ch.length) { recent = ch; break; }
    }
    if (recent.length || samples.length) stack = recent.map((f) => [f[0], f[1], f[2], f[1]]);
  }

  function write(done) {
    try { collectCoverage(); collectProfile(); } catch (e) { /* 計測の失敗で対象を止めない */ }
    const fn = [...keyInfo.entries()].map(([k, [rel, line, name]]) => [rel, line, name, calls.get(k) || 0,
      Math.round((incl.get(k) || 0) * 10) / 10, Math.round((selft.get(k) || 0) * 10) / 10]);
    const snap = { t: (Date.now() - t0) / 1000, stack: done ? [] : stack, threads: [], fn, done,
      how: COUNT ? 'node-inspector' : 'sampling', py: 'node ' + process.version };
    if (exc) snap.exc = exc;
    try {
      fs.writeFileSync(OUT + '.tmp', JSON.stringify(snap));
      fs.renameSync(OUT + '.tmp', OUT);
    } catch (e) { /* 無視 */ }
  }

  const timer = setInterval(() => write(false), 300);
  timer.unref();   // トレーサのせいでプロセスが終わらなくならないように
  process.on('uncaughtExceptionMonitor', (err) => {
    let where = null;
    for (const line of String(err && err.stack || '').split('\n')) {
      const m = /\(?((?:file:\/\/)?[^()\s]+):(\d+):(\d+)\)?\s*$/.exec(line.trim());
      if (!m) continue;
      const rel = relOf(m[1]);
      if (rel) { where = [rel, +m[2]]; break; }
    }
    exc = { type: (err && err.name) || 'Error', msg: String(err && err.message || err).slice(0, 500), where };
  });
  process.on('exit', () => write(true));
}
