// 走勢圖（update.md 發現 42）：lib/chart.ts 的純邏輯（合併、換週期、均線、VWAP、場次水位、持倉停損線、設定還原）與 PriceChart 的控制項。
// 前端沒有 DOM 測試工具：hooks 換成替身、直接呼叫元件函式取得元素樹。由 tests/test_frontend_chart.py 執行。用法：node tests/frontend/chart_check.js
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react");
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "chart-check-")));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });
let failed = 0, passed = 0;
const ok = (c, m) => { if (c) { passed++; } else { failed++; console.log("FAIL:", m); } };
const eq = (a, b, m) => ok(JSON.stringify(a) === JSON.stringify(b), `${m}\n   got ${JSON.stringify(a)}\n   want ${JSON.stringify(b)}`);
const close = (a, b, m) => ok(a !== null && Math.abs(a - b) < 1e-9, `${m}（got ${a}, want ${b}）`);

const transpile = (src, out) => {
  const js = ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText;
  fs.writeFileSync(out, js);
  return require(out);
};
transpile(path.join(FRONT, "lib", "range.ts"), path.join(TMP, "range.js"));            // chart.ts 要用 range.ts 的 isBandKey
const C = transpile(path.join(FRONT, "lib", "chart.ts"), path.join(TMP, "chart.js"));

const bar = (t, o, h, l, c, v = 10, b = 6, s = 4, n = 5) => ({ t, o, h, l, c, v, b, s, n });
const T0 = 1_760_000_040;    // 任意分鐘起點（60 的倍數）

// ── mergeBars ──
{
  const base = [bar(T0, 1, 2, 0, 1), bar(T0 + 60, 1, 3, 1, 2)];
  const snapshot = JSON.stringify(base);
  const m = C.mergeBars(base, [bar(T0 + 60, 1, 5, 1, 4), bar(T0 + 120, 4, 4, 3, 3)], bar(T0 + 180, 3, 3, 3, 3));
  eq(m.map((b) => b.t), [T0, T0 + 60, T0 + 120, T0 + 180], "合併後依時間排序、同一分鐘不重複");
  eq(m[1].h, 5, "同一分鐘以新的為準");
  eq(JSON.stringify(base), snapshot, "不改動傳進來的陣列");
  const m2 = C.mergeBars(m, [], bar(T0 + 180, 3, 9, 3, 8));
  eq(m2[3].h, 9, "進行中那一根每次被取代");
  eq(C.mergeBars([], [], null), [], "沒有資料");
}
// ── aggregate ──
{
  const s = 1_760_000_100 - (1_760_000_100 % 300);       // 5 分鐘桶起點
  const one = [bar(s, 10, 12, 9, 11, 5, 3, 2, 2), bar(s + 60, 11, 15, 10, 14, 7, 4, 3, 3), bar(s + 120, 14, 14, 8, 9, 3, 1, 2, 1),
               bar(s + 300, 9, 10, 9, 10, 4, 2, 2, 1)];
  const five = C.aggregate(one, 5);
  eq(five.length, 2, "5 分 K 分成兩桶");
  eq([five[0].o, five[0].h, five[0].l, five[0].c, five[0].v, five[0].b, five[0].s, five[0].n], [10, 15, 8, 9, 15, 8, 7, 6], "開＝第一根開、高＝最大、低＝最小、收＝最後一根收、量與外內盤加總");
  eq(five[0].t, s, "桶的時間是 5 分鐘的倍數");
  eq(C.aggregate(one, 1), one, "1 分就是原樣");
  eq(JSON.stringify(one[0]), JSON.stringify(bar(s, 10, 12, 9, 11, 5, 3, 2, 2)), "不改動原本的棒");
}
// ── sma ──
{
  const m = C.sma([1, 2, 3, 4, 5], 3);
  eq(m, [null, null, 2, 3, 4], "SMA3");
  eq(C.sma([5], 3), [null], "不夠長都是 null");
}
// ── 場次、VWAP、水位 ──
{
  const a1 = bar(T0, 100, 103, 99, 102, 10), a2 = bar(T0 + 60, 102, 106, 101, 105, 30);
  const night = T0 + 60 + 4500;                          // 75 分鐘後＝下一個場次
  const b1 = bar(night, 110, 111, 109, 110, 20);
  eq(C.splitSessions([a1, a2, b1]).map((x) => x.length), [2, 1], "空檔 ≥ 1 小時就是新場次");
  const v = C.vwap([a1, a2, b1]);
  // 場次 1：第一根 (103+99+102)/3 = 101.333；第二根累計 (101.333*10 + (106+101+105)/3*30)/40 = (1013.33+3120)/40 = 103.333
  close(v[0], (103 + 99 + 102) / 3, "第一根 VWAP＝代表價");
  close(v[1], ((103 + 99 + 102) / 3 * 10 + (106 + 101 + 105) / 3 * 30) / 40, "第二根是累計加權");
  close(v[2], (111 + 109 + 110) / 3, "新場次從頭累計");
  eq(C.vwap([bar(T0, 1, 1, 1, 1, 0)]), [null], "沒有成交量就沒有 VWAP");
  const lv = C.sessionLevels([a1, a2, b1]);
  eq([lv.prevClose, lv.open, lv.high, lv.low], [105, 110, 111, 109], "昨收＝上一場次最後收盤；開高低是目前場次");
  const only = C.sessionLevels([a1, a2]);
  eq([only.prevClose, only.open, only.high, only.low], [null, 100, 106, 99], "只有一個場次沒有昨收");
  eq(C.sessionLevels([]).open, null, "沒有資料");
}
// ── positionLines ──
{
  const pos = [{ code: "TMFJ6", direction: "Buy", quantity: 2, price: 47000, last_price: 47010, pnl: 0, margin_original: 0 }];
  const w = [{ id: "1", contract: "TMF", direction: "Buy", quantity: 2, entry_price: 46990, stop_loss_pts: 40, take_profit_pts: 60, match_code: "TMF" }];
  const l = C.positionLines(pos, w);
  eq(l.map((x) => [x.kind, x.price]), [["entry", 47000], ["stop", 46950], ["take", 47050]], "多單：均價、停損＝監看進場價−點數、停利＝+點數");
  ok(l[0].title.includes("多") && l[0].title.includes("2"), "持倉線的標題有方向與口數");
  const sell = C.positionLines([{ ...pos[0], direction: "Sell" }], [{ ...w[0], direction: "Sell" }]);
  eq(sell.map((x) => [x.kind, x.price]), [["entry", 47000], ["stop", 47030], ["take", 46930]], "空單：停損在上、停利在下");
  eq(C.positionLines(pos, []).map((x) => x.kind), ["entry"], "沒有監看就只有持倉線");
  eq(C.positionLines(pos, [{ ...w[0], direction: "Sell" }]).map((x) => x.kind), ["entry"], "方向不同的監看不算");
  eq(C.positionLines(pos, [{ ...w[0], is_option: true }]).map((x) => x.kind), ["entry"], "選擇權監看不算");
  eq(C.positionLines([{ ...pos[0], code: "TXO20000J6" }], w), [], "不是期貨前綴不畫");
  eq(C.positionLines([{ ...pos[0], quantity: 0 }], w), [], "口數 0 不畫");
  eq(C.positionLines(null, null), [], "沒有資料");
  eq(C.positionLines(pos, [{ ...w[0], stop_loss_pts: 0, take_profit_pts: 0 }]).map((x) => x.kind), ["entry"], "沒設停損停利就不畫那兩條");
}
// ── parseToggles／sameBars ──
{
  const d = C.parseToggles(null);
  eq([d.interval, d.toggles.ma20, d.toggles.ma5], [1, true, false], "沒有存過：預設");
  const p = C.parseToggles(JSON.stringify({ interval: 5, toggles: { ma5: true, vwap: false, bogus: true } }));
  eq([p.interval, p.toggles.ma5, p.toggles.vwap, p.toggles.ma20], [5, true, false, true], "部分欄位：缺的用預設、不認得的忽略");
  eq(C.parseToggles("not json").interval, 1, "壞掉的 JSON 用預設");
  eq(C.parseToggles(JSON.stringify({ interval: 7 })).interval, 1, "不認得的週期用 1 分");
  eq(C.parseToggles(JSON.stringify({ toggles: { ma5: "yes" } })).toggles.ma5, false, "型別不對的欄位用預設");
  eq([d.toggles.band, d.bandKey, d.bandKind], [false, "15", "term"], "範圍帶預設關、15 分、落點");
  const pb = C.parseToggles(JSON.stringify({ toggles: { band: true }, bandKey: "close", bandKind: "path" }));
  eq([pb.toggles.band, pb.bandKey, pb.bandKind], [true, "close", "path"], "範圍帶的設定還原");
  const bad = C.parseToggles(JSON.stringify({ bandKey: "7", bandKind: "x", toggles: { band: "yes" } }));
  eq([bad.toggles.band, bad.bandKey, bad.bandKind], [false, "15", "term"], "壞掉的範圍帶設定用預設");
  eq(C.parseToggles(JSON.stringify({ interval: 5 })).bandKey, "15", "舊版存的設定（沒有範圍帶欄位）用預設");
  const a = [bar(T0, 1, 2, 0, 1)], b = [bar(T0, 1, 2, 0, 1)];
  ok(C.sameBars(a, b), "內容一樣");
  b[0].c = 9;
  ok(!C.sameBars(a, b), "最後一根不同");
  ok(!C.sameBars(a, []), "長度不同");
  ok(C.sameBars([], []), "兩個空的");
}

// ── 元件：控制項（hooks 換成替身）──
const calls = [];
const store = {};
global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };
const realApi = transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "real_api.js"));
fs.writeFileSync(path.join(TMP, "api_stub.js"), "module.exports = { api: { market: { bars: () => Promise.resolve({}), range: () => Promise.resolve({}) }, position: { list: () => Promise.resolve([]) }, order: { watches: () => Promise.resolve([]) } } };");
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) {
  if (req === "@/lib/api") return path.join(TMP, "api_stub.js");
  if (req === "@/lib/chart") return path.join(TMP, "chart.js");
  if (req === "@/lib/range") return path.join(TMP, "range.js");
  return orig.call(this, req, ...r);
};
const Comp = transpile(path.join(FRONT, "components", "PriceChart.tsx"), path.join(TMP, "panel.js")).default;
const NAMES = ["ready", "bars", "interval", "toggles", "positions", "watches", "err", "updatedAt", "legend", "bandKey", "bandKind", "range", "rangeErr"];
function render(forced = {}) {
  const sets = [];
  let n = 0;
  React.useState = (init) => { const i = n++; const v = Object.prototype.hasOwnProperty.call(forced, i) ? forced[i] : init; return [v, (x) => sets.push([NAMES[i], typeof x === "function" ? x(v) : x])]; };
  React.useRef = (init) => ({ current: init });
  React.useEffect = () => {};
  React.useCallback = (f) => f;
  React.useMemo = (f) => f();
  const tree = Comp({ active: true });
  return { tree, sets };
}
function find(el, pred, out = []) {
  if (el == null || typeof el === "string" || typeof el === "number") return out;
  if (Array.isArray(el)) { el.forEach((x) => find(x, pred, out)); return out; }
  if (pred(el)) out.push(el);
  if (el.props) find(el.props.children, pred, out);
  return out;
}
const text = (el) => (el == null || typeof el === "boolean") ? "" : typeof el === "string" || typeof el === "number" ? String(el)
  : Array.isArray(el) ? el.map(text).join("") : text(el.props && el.props.children);
{
  const { tree, sets } = render();
  const buttons = find(tree, (e) => e.type === "button");
  eq(buttons.length, 10, "3 個週期＋7 個線的開關（含範圍帶）");
  eq(buttons.find((b) => text(b).includes("範圍帶")).props["aria-pressed"], false, "範圍帶預設關");
  ok(!text(tree).includes("落點範圍"), "範圍帶關著就沒有選擇列與摘要");
  const byText = (t) => buttons.find((b) => text(b).includes(t));
  eq(byText("5 分").props["aria-pressed"], false, "預設 1 分，5 分沒按下");
  eq(buttons.find((b) => text(b).includes("1 分")).props["aria-pressed"], true, "1 分是按下的");
  eq(byText("MA20").props["aria-pressed"], true, "MA20 預設開");
  eq(byText("MA5").props["aria-pressed"], false, "MA5 預設關");
  byText("5 分").props.onClick();
  eq(sets.find((s) => s[0] === "interval"), ["interval", 5], "按 5 分會換週期");
  ok(JSON.parse(store.lh_chart_prefs).interval === 5, "換週期會記在 localStorage");
  byText("MA5").props.onClick();
  const t = sets.find((s) => s[0] === "toggles");
  eq(t[1].ma5, true, "按 MA5 會打開");
  ok(JSON.parse(store.lh_chart_prefs).toggles.ma5 === true, "開關也會記起來");
  ok(text(tree).includes("只顯示、不下單"), "有寫明只顯示");
}
{
  const bars = [bar(T0, 100, 103, 99, 102, 10, 6, 4, 3)];
  const { tree } = render({ 0: true, 1: bars });
  ok(text(tree).includes("102"), "有資料時顯示最新價");
  ok(!text(tree).includes("還沒有 K 棒資料"), "有資料就不顯示空狀態");
  const empty = render({ 0: true, 1: [] }).tree;
  ok(text(empty).includes("還沒有 K 棒資料"), "沒資料的空狀態說明原因");
  const bad = render({ 0: true, 1: [], 6: "連不上後端（K 棒暫停更新）" }).tree;
  ok(text(bad).includes("連不上後端"), "連不上時顯示原因");
}

// ── 範圍帶（update.md 發現 48）──
{
  const on = { ...C.DEFAULT_TOGGLES, band: true };
  const lv = (p, d, bad = false) => ({ p, term: { d, lo: 30000 - d, hi: 30000 + d }, path: { d: d * 1.5, lo: 30000 - d * 1.5, hi: 30000 + d * 1.5 }, unstable_term: bad, unstable_path: false });
  const band = { minutes: 15, noise: 16.8, few: false, levels: [lv(50, 10), lv(80, 20.2), lv(95, 34.3)] };
  const range = { price: 30000, stale: false, bands: { "15": band }, note: null };
  const { tree, sets } = render({ 3: on });
  const buttons = find(tree, (e) => e.type === "button");
  eq(buttons.length, 17, "開了範圍帶：多 5 個時間鈕＋2 個種類鈕");
  const bn = (t) => buttons.find((b) => text(b).trim() === t);
  eq([bn("15 分").props["aria-pressed"], bn("5 分").props["aria-pressed"]].length, 2, "時間鈕有 aria-pressed");
  const bandRow = buttons.filter((b) => ["5 分", "15 分", "30 分", "60 分", "到收盤"].includes(text(b).trim()));
  eq(bandRow.length, 7, "範圍帶的 5／15／30／60 分、到收盤，加上週期那一排同名的 5 分、15 分");
  ok(buttons.find((b) => text(b).trim() === "到收盤").props.disabled === true, "沒有資料時「到收盤」停用（只有日盤才有）");
  ok(buttons.find((b) => text(b).trim() === "落點").props["aria-pressed"] === true && buttons.find((b) => text(b).trim() === "期間").props["aria-pressed"] === false, "預設看落點範圍");
  ok(text(tree).includes("範圍帶載入中"), "還沒有資料時寫載入中");
  buttons.find((b) => text(b).trim() === "30 分" && b.props.title === undefined && b.props["aria-pressed"] !== undefined && b.props.disabled !== undefined).props.onClick();
  eq(sets.find((x) => x[0] === "bandKey"), ["bandKey", "30"], "按 30 分會換範圍帶的時間");
  ok(JSON.parse(store.lh_chart_prefs).bandKey === "30" && JSON.parse(store.lh_chart_prefs).toggles.band === true, "範圍帶的時間與開關記在 localStorage");
  buttons.find((b) => text(b).trim() === "期間").props.onClick();
  eq(sets.find((x) => x[0] === "bandKind"), ["bandKind", "path"], "按期間會換種類");
  ok(JSON.parse(store.lh_chart_prefs).bandKind === "path", "種類也記起來");
  const withData = render({ 3: on, 11: range }).tree;
  const t = text(withData);
  ok(t.includes("落點範圍（15 分鐘）") && t.includes("50% ±10 ") && t.includes("80% ±20.2") && t.includes("95% ±34.3"), "摘要：三個層級的距離");
  ok(t.includes(`80% ＝ ${29979} ~ ${30021}`.replace(/(\d{2})(\d{3})/g, "$1,$2")) , "摘要：80% 的價位範圍（下緣取整往下、上緣往上）");
  ok(t.includes("只說會走多遠、不說往哪個方向"), "摘要寫明不說方向");
  ok(buttons.length === 17 && find(withData, (e) => e.type === "button" && text(e).trim() === "到收盤")[0].props.disabled === true, "沒有到收盤的資料時仍停用");
  const withClose = render({ 3: on, 11: { ...range, bands: { ...range.bands, close: { ...band, minutes: 45, bucket: "c31_60" } } } }).tree;
  ok(find(withClose, (e) => e.type === "button" && text(e).trim() === "到收盤")[0].props.disabled === false, "有到收盤的資料就能按");
  const pathTxt = text(render({ 3: on, 10: "path", 11: range }).tree);
  ok(pathTxt.includes("期間範圍（15 分鐘）") && pathTxt.includes("50% ±15 "), "期間範圍用 path 的距離（±15＝10×1.5）");
  const stale = text(render({ 3: on, 11: { ...range, stale: true } }).tree);
  ok(stale.includes("最新成交價是舊的"), "價格是舊的要說");
  const none = text(render({ 3: on, 11: { ...range, bands: {}, note: "最近 30 分鐘成交太少" } }).tree);
  ok(none.includes("最近 30 分鐘成交太少") && !none.includes("±"), "沒有範圍時只寫原因、不寫數字");
  const err = text(render({ 3: on, 12: "範圍帶暫時抓不到（後端要重啟成新版才有這個端點）" }).tree);
  ok(err.includes("範圍帶暫時抓不到"), "抓不到時顯示原因");
  const few = text(render({ 3: on, 11: { ...range, bands: { "15": { ...band, few: true } } } }).tree);
  ok(few.includes("夜盤實測樣本少"), "夜盤樣本少要說");
}

console.log(`${passed} PASS, ${failed} FAIL`);
process.exit(failed ? 1 : 0);
