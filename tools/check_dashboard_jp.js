/*
 * 日股工作台 · HTML 納品前チェック（4 ステップ）
 *
 * 使い方: node tools/check_dashboard_jp.js <dashboard.html>
 *
 *   1) 内聯 <script> の構文チェック（vm.Script でコンパイル）
 *   2) DOM スタブ上で実行 + **未知のコンテナ id 検出**
 *   3) window.__wb による境界冒煙（最早日/中間日/最新日/越界/セッション切替/ドロワー）
 *   4) 「フロントが描画した値」 vs 「埋め込みデータパック D」の自洽対拍
 *      → 日付に依存しないので、毎日の同期後でも回せる
 *
 * 終了コード: 0 = 全通過 / 1 = 失敗あり / 2 = 使い方エラー
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const file = process.argv[2];
if (!file) { console.error('使い方: node tools/check_dashboard_jp.js <html>'); process.exit(2); }
const html = fs.readFileSync(file, 'utf8');

/* ---------- ① id と内聯スクリプトの抽出 ---------- */
const ids = new Set();
for (const m of html.matchAll(/\bid="([^"]+)"/g)) { ids.add(m[1]); }
const inline = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
  .map((m) => m[1]);
console.log(`HTML ${(html.length / 1024).toFixed(1)} KB · コンテナ id ${ids.size} 個 · 内聯スクリプト ${inline.length} 段`);
const code = inline.join('\n;\n');

let syntaxOk = true;
try { new vm.Script(code, { filename: 'inline.js' }); }
catch (e) { syntaxOk = false; console.log('✗ 1/4 構文チェック失敗:', e.message); }
if (syntaxOk) { console.log('✓ 1/4 内聯スクリプトの構文チェック通過'); }

/* ---------- ② DOM スタブ ---------- */
const missing = [];
const hitIds = new Set();
function mkEl(id) {
  const el = {
    id, dataset: {}, style: {}, value: 0, disabled: false, options: [], _html: '',
    _text: '', className: '',
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, f) { const on = f === undefined ? !this._s.has(c) : !!f; on ? this._s.add(c) : this._s.delete(c); },
    },
    appendChild() {}, setAttribute() {}, getAttribute() { return null; },
    addEventListener() {}, removeEventListener() {},
    querySelector() { return mkEl('_q'); }, querySelectorAll() { return []; },
    closest() { return null; }, focus() {},
    get parentNode() { return mkEl('_parent'); },
  };
  Object.defineProperty(el, 'innerHTML', { get() { return el._html; }, set(v) { el._html = String(v); } });
  Object.defineProperty(el, 'textContent', { get() { return el._text; }, set(v) { el._text = String(v); } });
  return el;
}
const elCache = {};
/* sessSeg の .sg 子ボタン（data-sess を返すスタブ） */
const sgButtons = ['all', 'am'].map((s) => {
  const b = mkEl('_sg');
  b.getAttribute = (k) => (k === 'data-sess' ? s : null);
  b.classList.toggle('on', s === 'all');
  return b;
});
const doc = {
  getElementById(id) {
    if (!ids.has(id)) { missing.push(id); }
    hitIds.add(id);
    return elCache[id] || (elCache[id] = mkEl(id));
  },
  createElement() { return mkEl('_new'); },
  querySelector() { return mkEl('_sel'); },
  querySelectorAll() { return []; },
  addEventListener() {},
};
elCache['sessSeg'] = mkEl('sessSeg');
elCache['sessSeg'].querySelectorAll = () => sgButtons;

const chartStub = {
  _opts: [],
  setOption(o) { this._opts.push(o); },
  resize() {}, on() {}, dispose() {},
};
const sandbox = {
  console, JSON, Math, Date, Number, String, Boolean, Object, Array, Set, Map,
  parseFloat, parseInt, isNaN, isFinite, Intl, setTimeout, clearTimeout,
  document: doc,
  echarts: { init() { return chartStub; }, graphic: {} },
  getComputedStyle() { return { getPropertyValue() { return ''; } }; },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.addEventListener = () => {};
sandbox.devicePixelRatio = 1;

let execOk = true;
try {
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: 'inline.js' });
} catch (e) {
  execOk = false;
  console.log('✗ 2/4 DOM スタブ実行失敗:', e.message);
}
if (execOk) {
  console.log(`✓ 2/4 DOM スタブ実行通過（参照コンテナ ${hitIds.size}/${ids.size} 個）`);
  if (missing.length) {
    console.log(`✗ 存在しないコンテナ id を参照（${missing.length}）:`, [...new Set(missing)].join(', '));
  } else {
    console.log('✓     未知コンテナ id 検出通過');
  }
}

/* ---------- ③ __wb 境界冒煙 ---------- */
const wb = sandbox.window.__wb;
if (!wb || typeof wb.goTo !== 'function') {
  console.log('✗ 3/4 window.__wb が公開されていない');
  process.exit(1);
}
const N = wb.nDates - 1;
const probes = [
  ['最早日へ', () => { wb.goTo(0); return wb.state.date; }],
  ['中間日へ', () => { wb.goTo(Math.floor(N / 2)); return wb.state.date; }],
  ['最新日へ', () => { wb.goTo(N); return wb.state.date; }],
  ['越界保護(-1)', () => { wb.goTo(-1); return wb.state.date; }],
  ['越界保護(N+9)', () => { wb.goTo(N + 9); return wb.state.date; }],
  ['非数値保護(NaN)', () => { wb.goTo(NaN); return wb.state.date; }],
  ['セッション→前場引け', () => { wb.setSession('am'); return wb.state.sess; }],
  ['セッション→大引け', () => { wb.setSession('all'); return wb.state.sess; }],
  ['ドロワー開（先頭セクター）', () => wb.openInd(Object.keys(wb.data)[0])],
  ['ドロワー開（銀行）', () => wb.openInd('1631.T')],
  ['ドロワー閉', () => wb.closeDrawer()],
  ['全期間レンジ切替（1ヶ月）', () => { wb.openInd('1625.T'); return wb.state.range; }],
];
let pass = 0, fail = 0;
probes.forEach(([name, fn]) => {
  try { const r = fn(); console.log(`   · ${name} → ok (${r})`); pass++; }
  catch (e) { console.log(`   · ${name} → ✗ ${e.message}`); fail++; }
});
wb.closeDrawer();
console.log(`✓ 3/4 境界冒煙：${pass} 通過 / ${fail} 失敗`);

/* ---------- ④ 描画値 vs データパック 自洽対拍 ---------- */
const D = wb.D;
const num = (v, d) => (v === null || v === undefined) ? '—'
  : Number(v).toLocaleString('ja-JP', { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (v, d) => (v === null || v === undefined) ? '—'
  : ((v > 0 ? '+' : '') + num(v, d) + '%');
/* 億円金額の適応桁数（テンプレートの amtDigits/fmtAmt と同一定義） */
const amtD = (v) => {
  if (v === null || v === undefined) return 0;
  const a = Math.abs(v);
  return a >= 100 ? 1 : (a >= 1 ? 2 : 3);
};
const amt = (v) => num(v, amtD(v));

const nChecks = [];
const H = () => (elCache['hstat'] || {})._html || '';
const K = () => (elCache['kpi'] || {})._html || '';
const HEAT = () => (elCache['heat'] || {})._html || '';
const has = (s) => H().includes(s) || K().includes(s);

const LAST = N;
const LASTDATE = wb.dates[LAST];
wb.goTo(LAST); wb.setSession('all');

/* 指数 KPI：日経平均の終値と前日比がデータパックと一致するか */
const nk = wb.idxs['^N225'];
nChecks.push([`日経平均 終値 = データパック ${num(nk.c[LAST], 2)}`, has(num(nk.c[LAST], 2))]);
nChecks.push([`日経平均 前日比 = データパック ${pct(nk.p[LAST], 2)}`, has(pct(nk.p[LAST], 2))]);

/* ヒートマップ：先頭セクターの騰落率がタイルに出ているか */
const heatRows = Object.keys(D.ind).map((c) => ({ c, p: D.ind[c].p[LAST] }))
  .filter((r) => r.p !== null && r.p !== undefined)
  .sort((a, b) => b.p - a.p);
if (heatRows.length) {
  nChecks.push([`ヒートマップ首位 ${D.ind[heatRows[0].c].n} ${pct(heatRows[0].p, 2)}`,
    HEAT().includes(D.ind[heatRows[0].c].n) && HEAT().includes(pct(heatRows[0].p, 2))]);
}

/* 騰落レシオ：市場宽度がデータパックと整合するか */
const m = D.mkt[LASTDATE];
if (m) {
  nChecks.push([`騰落銘柄数（上がり ${m[0]} / 下がり ${m[1]}）`,
    (elCache['stats'] || {})._html.includes('>' + m[0] + '<')]);
  const r = m[1] ? ((m[0] / m[1]) * 100).toFixed(0) + '%' : '—';
  nChecks.push([`騰落レシオ ${r}`, (elCache['stats'] || {})._html.includes(r)]);
}

/* 売買代金ランキング：首位銘柄が表に出ているか */
const rk = wb.rank[LASTDATE];
if (rk && rk.length) {
  nChecks.push([`売買代金首位 ${rk[0][1]}（${amt(rk[0][5])} 億円）`,
    (elCache['rankWrap'] || {})._html.includes(rk[0][1])]);
}

/* 金額の適応桁数：1 億未満は小数 3 桁で表示されるか（「0 億円」再発防止） */
const subRows = Object.keys(D.ind)
  .map((c) => ({ c, a: D.ind[c].a[LAST] }))
  .filter((r) => r.a !== null && r.a !== undefined && Math.abs(r.a) < 1)
  .sort((x, y) => x.a - y.a);
if (subRows.length) {
  const t = amt(subRows[0].a) + ' 億円';
  nChecks.push([`1 億未満の売買代金が小数 3 桁表示（${t}）`, HEAT().includes(t)]);
}

/* 構造アサーション：グリッドのタイル数・カード数・テーブル行数 */
const cnt = (html, re) => (html.match(re) || []).length;
nChecks.push([`ヒートマップのタイル数 = ${Object.keys(D.ind).length}（17 業種）`,
  cnt(HEAT(), /class="heat"/g) === Object.keys(D.ind).length]);
nChecks.push(['指数 KPI カード数 = 5', cnt(K(), /class="kpi"/g) === 5]);
if (rk && rk.length) {
  nChecks.push([`売買代金テーブル行数 = ${rk.length}`,
    cnt((elCache['rankWrap'] || {})._html, /<tr>/g) === rk.length + 1]);
}
nChecks.push(['注目度リストの行数 = データありセクター数',
  cnt((elCache['attList'] || {})._html, /class="bi-row"/g) ===
  Object.keys(D.ind).filter((c) => D.ind[c].t[LAST] !== null).length]);

/* チャート：相対強度は 3 系列 × 17 業種、ドロワーは 2 系列（終値 + 売買代金） */
wb.goTo(LAST); wb.refreshAll();
const rsOpt = chartStub._opts.filter((o) => o.series && o.series.length === 3 &&
  o.yAxis && Array.isArray(o.yAxis) === false).pop();
if (rsOpt) {
  nChecks.push([`相対強度チャート 3 系列（5/20/60 日）`,
    rsOpt.series.length === 3]);
  nChecks.push([`相対強度チャートのデータ点数 = ${Object.keys(D.ind).length}`,
    rsOpt.series[0].data.length === Object.keys(D.ind).length]);
  nChecks.push(['相対強度チャートのカテゴリ = 17 業種',
    rsOpt.yAxis.data.length === Object.keys(D.ind).length]);
} else {
  nChecks.push(['相対強度チャートのオプションが渡っていない', false]);
}
wb.openInd('1625.T');
const dwOpt = chartStub._opts.filter((o) => Array.isArray(o.yAxis)).pop();
nChecks.push(['ドロワーチャート 2 系列（終値 + 売買代金）',
  !!dwOpt && dwOpt.series.length === 2 &&
  dwOpt.series[0].data.length === dwOpt.series[1].data.length]);
nChecks.push(['ドロワーの構成銘柄テーブルが描画される',
  ((elCache['dwStk'] || {})._html || '').includes('<table')]);
wb.closeDrawer();

/* 前場・後場：セッション切替でフロント値がデータパックと一致するか */
const ip = (D.intra.dates || []).indexOf(LASTDATE);
if (ip < 0) {
  nChecks.push([`前場後場·${LASTDATE} はデータなし（対拍スキップ）`, true]);
} else {
  const ia = D.intra.idx['^N225'] && D.intra.idx['^N225'][ip];
  wb.setSession('am');
  nChecks.push(['前場 セッション標記が状態条に出る',
    String((elCache['hDate'] || {}).textContent || '').includes('前場引け')]);
  if (ia && ia[0] !== null) {
    nChecks.push([`前場·日経平均 ${pct(ia[0], 2)}`, has(pct(ia[0], 2))]);
  }
  wb.setSession('all');
  nChecks.push(['大引けに戻すとセッション標記が消える',
    !String((elCache['hDate'] || {}).textContent || '').includes('前場引け')]);
}

/* 履歴日降級：前場データの無い日は自動で大引けに回落するか */
wb.goTo(0); wb.setSession('am');
const oldest = wb.dates[0];
const hasIntraOldest = (D.intra.dates || []).indexOf(oldest) >= 0;
const sessAfter = wb.state.sess;
nChecks.push([`履歴日 ${oldest} のセッション処理（前場データ${hasIntraOldest ? 'あり' : 'なし'} → ${sessAfter}）`,
  hasIntraOldest ? sessAfter === 'am' : sessAfter === 'all']);
wb.goTo(LAST); wb.setSession('all');

let nPass = 0, nFail = 0;
nChecks.forEach(([name, ok]) => {
  console.log(`   · ${name} → ${ok ? 'ok' : '✗ 数値不一致'}`);
  ok ? nPass++ : nFail++;
});
console.log(`✓ 4/4 描画値 vs データパック自洽対拍（最新日 ${LASTDATE}）：${nPass} 通過 / ${nFail} 失敗`);

const ok = syntaxOk && execOk && !missing.length && !fail && !nFail;
if (!ok) {
  console.log(`--- 未通過の内訳: 構文=${syntaxOk} DOMスタブ=${execOk} `
    + `未知id=${[...new Set(missing)].join(',') || 'なし'} 冒煙失敗=${fail} 数値失敗=${nFail}`);
}
console.log(ok ? '=== チェック結果: 全通過 ===' : '=== チェック結果: 未通過 ===');
process.exit(ok ? 0 : 1);
