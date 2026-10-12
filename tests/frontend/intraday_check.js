// 盤中即時面板新增的三張卡片（買賣掛單／均價與基差／時段脈絡）：純函式的格式、各種資料形狀下的畫面（日盤／夜盤／舊後端／資料壞掉）。
// 卡片沒有 hooks，直接 react-dom/server 的 renderToStaticMarkup；整個面板（LiveStatePanel）用 hooks 替身灌入資料。
// 由 tests/test_frontend_intraday.py 執行。用法：node tests/frontend/intraday_check.js（需要 frontend/node_modules）
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "intraday-check-"));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });

const transpile = (src, out) => {
  const js = ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText;
  fs.writeFileSync(out, js);
  return require(out);
};

// ── 模組：lib/intraday（真的）、lib/api（替身：面板會呼叫 api.market.live，這裡不會真的執行）、IntradayCards（真的）、lucide-react（替身）──
const H = transpile(path.join(FRONT, "lib", "intraday.ts"), path.join(TMP, "intraday.js"));
global.__api_stub = { api: { market: { live: () => Promise.resolve(null) } } };
fs.writeFileSync(path.join(TMP, "api_stub.js"), "module.exports = global.__api_stub;");
fs.writeFileSync(path.join(TMP, "lucide_stub.js"), "module.exports = new Proxy({}, { get: () => () => null });");
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) {
  if (req === "@/lib/api") return path.join(TMP, "api_stub.js");
  if (req === "@/lib/intraday") return path.join(TMP, "intraday.js");
  if (req === "lucide-react") return path.join(TMP, "lucide_stub.js");
  if (req === "./IntradayCards") return path.join(TMP, "cards.js");
  return orig.call(this, req, ...r);
};
const Cards = transpile(path.join(FRONT, "components", "IntradayCards.tsx"), path.join(TMP, "cards.js"));
const Panel = transpile(path.join(FRONT, "components", "LiveStatePanel.tsx"), path.join(TMP, "panel.js")).default;

let fail = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + extra)); if (!ok) fail++; };
const html = (el) => renderToStaticMarkup(el).replace(/<[^>]+>/g, "").replace(/&gt;/g, ">").replace(/&lt;/g, "<").replace(/&amp;/g, "&");   // 只留文字
const raw = (el) => renderToStaticMarkup(el);
const e = React.createElement;

// ── 純函式 ──
check("signed：正負號與零", H.signed(5) === "+5" && H.signed(-3) === "−3" && H.signed(0) === "0" && H.signed(-0.04, 1) === "0.0" && H.signed(-63.54, 1) === "−63.5");
check("signed：千分位與缺值", H.signed(1234.5, 1) === "+1,234.5" && H.signed(null) === "—" && H.signed(NaN) === "—" && H.signed(undefined) === "—");
check("price：千分位、最多 1 位小數、缺值", H.price(49353.320758) === "49,353.3" && H.price(49301.96, 2) === "49,301.96" && H.price(100) === "100" && H.price(null) === "—");
check("pct", H.pct(0.512) === "51%" && H.pct(0.5, 1) === "50.0%" && H.pct(null) === "—" && H.pct(NaN) === "—");
{
  const sums = [0, 0.005, 0.495, 0.505, 0.512, 0.5, 0.999, 1].every((x) => { const [a, b] = H.pctPair(x); return parseInt(a) + parseInt(b) === 100; });
  check("pctPair：外盤＋內盤永遠剛好 100%（各自四捨五入會變 51+50）", sums && H.pctPair(0.505)[0] === "51%" && H.pctPair(0.505)[1] === "49%"
    && H.pctPair(null)[0] === "—" && H.pctPair(NaN)[1] === "—" && H.pctPair(1.7)[0] === "100%" && H.pctPair(-0.2)[1] === "100%");
}
check("imbalanceText：均衡帶 ±5%", H.imbalanceText(0.04) === "均衡" && H.imbalanceText(-0.049) === "均衡" && H.imbalanceText(0.05) === "買方較厚 5%");
check("imbalanceText：方向與缺值", H.imbalanceText(-0.124) === "賣方較厚 12%" && H.imbalanceText(0.6) === "買方較厚 60%" && H.imbalanceText(null) === "—");
check("minutesText", H.minutesText(45) === "45 分" && H.minutesText(60) === "1 小時" && H.minutesText(205) === "3 小時 25 分" && H.minutesText(-5) === "0 分" && H.minutesText(null) === "—");
check("depthShare", H.depthShare(30, 10) === 0.75 && H.depthShare(0, 0) === null && H.depthShare(null, 5) === null);
{
  const hs = H.profileHeights([1, 2, 4]);
  check("profileHeights：以最大值為滿格、有下限", hs[2] === 1 && hs[1] === 0.5 && hs[0] === 0.25 && H.profileHeights([0, 0]).every((x) => x === 0.08) && H.profileHeights([]).length === 0 && H.profileHeights(undefined).length === 0);
}

// ── 測試資料（欄位與後端 snapshot 一致）──
const profile = Array.from({ length: 20 }, (_, i) => 1.8 - i * 0.06);
const base = (over = {}) => ({
  ready: true, as_of: 0, prefix: "TMF", last: 49300, last_age: 0.4, flow: {}, high: 49420, low: 49280, range: 140, avg_range: 525.1,
  range_ratio: 0.27, range_label: "清淡", session_day: "2026-10-12", since: "08:45:01", partial: false, in_session: true,
  pre: { date: null, state: null, hint: null, want: 0, want_reason: "" }, flow_dir: 0, coherence: null, coherence_text: "", range_source: "own", session: "day",
  thresholds: { flow_up: 0.6, flow_down: 0.4, flow_margin: 0.025, big_move: 1.5, quiet: 0.6 },
  book: { bid1: 49290, ask1: 49293, bv1: 5, av1: 7, bv5: 30, av5: 52, spread: 3, imb5: -0.268, spread_avg: 2.4, imb5_avg: -0.1, n: 120, window_sec: 30, age: 0.3 },
  session_flow: { inside: 8198, outside: 8374, share: 0.505, session: "day" },
  vwap: { price: 49353.320758, diff: -53.3, session: "day" },
  basis: { spot: 49301.96, value: -1.96, valid: true },
  ex_range: { high: 49420, low: 49280, mismatch: 0 },
  ctx: { session: "day", ready: true, elapsed: 95, settle_day: false, mins_to_close: 205, close_hhmm: "13:45", r20: 525.1, n_days: 1607,
         bucket: { label: "10:15~10:30", mult: 1.09, idx: 6 }, profile,
         proj: { c: 60, early: 140, expect: { lo: 160, mid: 210, hi: 280 }, done: 25, done_ratio: 0.12, n: 1587 } },
  ...over,
});

// ── 買賣掛單 ──
{
  const t = html(e(Cards.BookCard, { st: base() }));
  check("掛單卡：價差、平均、買賣量、失衡、第一檔", t.includes("3 點") && t.includes("近 30 秒平均 2.4") && t.includes("買 30 口") && t.includes("賣 52 口")
    && t.includes("賣方較厚 27%") && t.includes("近 30 秒 賣方較厚 10%") && t.includes("買一 49,290 × 5") && t.includes("賣一 49,293 × 7"), t);
  check("掛單卡：交易所累計內外盤", t.includes("日盤累計（交易所）") && t.includes("外盤 51% · 內盤 49%") && t.includes("外盤 8,374 口 / 內盤 8,198 口"), t);
  check("掛單卡：新鮮資料不淡化", !raw(e(Cards.BookCard, { st: base() })).includes("opacity-50"));
  const old = html(e(Cards.BookCard, { st: base({ book: { ...base().book, age: 42 } }) }));
  check("掛單卡：資料超過 10 秒標示幾秒前並淡化", old.includes("42 秒前") && raw(e(Cards.BookCard, { st: base({ book: { ...base().book, age: 42 } }) })).includes("opacity-50"));
  const locked = html(e(Cards.BookCard, { st: base({ book: { ...base().book, spread: null, spread_avg: null } }) }));
  check("掛單卡：價差無效時顯示破折號、不顯示 null", locked.includes("—") && !locked.includes("null") && !locked.includes("undefined") && !locked.includes("NaN"), locked);
  const none = html(e(Cards.BookCard, { st: base({ book: null, session_flow: null }) }));
  check("掛單卡：舊後端（沒有五檔）顯示尚無", none.includes("尚無五檔資料") && !none.includes("累計"));
  const bare = base(); delete bare.book; delete bare.session_flow;
  check("掛單卡：欄位根本不存在也不會壞", html(e(Cards.BookCard, { st: bare })).includes("尚無五檔資料"));
  const night = html(e(Cards.BookCard, { st: base({ session_flow: { inside: 10, outside: 30, share: 0.75, session: "night" } }) }));
  check("掛單卡：夜盤累計的標籤", night.includes("夜盤累計（交易所）") && night.includes("外盤 75%"));
  const zero = html(e(Cards.BookCard, { st: base({ session_flow: { inside: 0, outside: 0, share: null, session: "day" } }) }));
  check("掛單卡：時段剛開始（累計 0）不顯示 NaN", zero.includes("外盤 — · 內盤 —") && !zero.includes("NaN"), zero);
}

// ── 均價與基差 ──
{
  const t = html(e(Cards.VwapCard, { st: base() }));
  check("均價卡：交易所均價、與現價差、基差", t.includes("49,353.3") && t.includes("日盤均價（交易所）") && t.includes("現價 −53.3 點") && t.includes("基差 −2.0 點") && t.includes("現貨 49,301.96"), t);
  const night = html(e(Cards.VwapCard, { st: base({ basis: { spot: 49301.96, value: -12, valid: false }, vwap: { price: 49353.3, diff: -63.3, session: "night" } }) }));
  check("均價卡：夜盤不顯示基差數字", night.includes("夜盤均價") && night.includes("現貨只在 09:00~13:30 更新") && !night.includes("−12"), night);
  check("均價卡：高低一致時不顯示警告", !t.includes("不一致"));
  const mm = html(e(Cards.VwapCard, { st: base({ ex_range: { high: 1, low: 1, mismatch: 3 } }) }));
  check("均價卡：高低和交易所對不上時顯示次數", mm.includes("不一致 3 次"));
  const none = html(e(Cards.VwapCard, { st: base({ vwap: null, basis: null, ex_range: null }) }));
  check("均價卡：沒有資料顯示尚無", none.includes("尚無資料") && !none.includes("null"));
}

// ── 時段脈絡 ──
{
  const t = html(e(Cards.ContextCard, { st: base() }));
  check("脈絡卡：時段與倍數", t.includes("現在 10:15~10:30") && t.includes("1.09×"), t);
  check("脈絡卡：距收盤", t.includes("距日盤收盤 3 小時 25 分（13:45）"), t);
  check("脈絡卡：預期振幅與區間", t.includes("之後預期振幅約 210 點") && t.includes("68% 區間 160~280"), t);
  check("脈絡卡：依據與已走", t.includes("依開盤後 60 分振幅 140 點") && t.includes("09:45 後已走 25 點") && t.includes("預期中位數的 12%"), t);
  check("脈絡卡：誠實標示不確定性與用途", t.includes("±40%") && t.includes("不是目標價") && t.includes("525 點"), t);
  const bars = (raw(e(Cards.ContextCard, { st: base() })).match(/flex-1 rounded-sm/g) || []).length;
  const lit = (raw(e(Cards.ContextCard, { st: base() })).match(/background:#00e676/g) || []).length;
  check("脈絡卡：20 格直條、只有現在那格亮", bars === 20 && lit === 1, `${bars} ${lit}`);
  const c30 = html(e(Cards.ContextCard, { st: base({ ctx: { ...base().ctx, proj: { ...base().ctx.proj, c: 30 } } }) }));
  check("脈絡卡：30 分模型標 09:15", c30.includes("開盤後 30 分振幅") && c30.includes("09:15 後已走"));
  const settle = html(e(Cards.ContextCard, { st: base({ ctx: { ...base().ctx, settle_day: true, close_hhmm: "13:30", mins_to_close: 150 } }) }));
  check("脈絡卡：結算日提醒近月合約 13:30 收", settle.includes("結算日：近月合約 13:30 收盤") && settle.includes("距日盤收盤 2 小時 30 分（13:30）"), settle);
  const note = html(e(Cards.ContextCard, { st: base({ ctx: { ...base().ctx, proj: undefined, proj_note: "後端在開盤後才啟動，沒有完整的開盤資料，不預測" } }) }));
  check("脈絡卡：不預測時說明原因、不顯示歷史檢驗說明", note.includes("不預測") && !note.includes("±40%") && !note.includes("之後預期振幅"));
  const loading = html(e(Cards.ContextCard, { st: base({ ctx: { session: "day", ready: false, elapsed: 95, mins_to_close: 205, close_hhmm: "13:45", settle_day: false } }) }));
  check("脈絡卡：背景還沒算好顯示計算中，倒數照樣有", loading.includes("計算中") && loading.includes("距日盤收盤 3 小時 25 分"), loading);
  const night = html(e(Cards.ContextCard, { st: base({ ctx: { session: "night", ready: true, mins_to_close: 740, close_hhmm: "05:00" } }) }));
  check("脈絡卡：夜盤只有倒數、說明沒有樣本", night.includes("夜盤 · 距收盤（05:00）還有 12 小時 20 分") && night.includes("沒有足夠的歷史樣本") && !night.includes("之後預期振幅約") && !night.includes("68% 區間"), night);
  check("脈絡卡：非交易時段", html(e(Cards.ContextCard, { st: base({ ctx: { session: "other", ready: true } }) })).includes("非交易時段"));
  check("脈絡卡：舊後端（沒有 ctx）", html(e(Cards.ContextCard, { st: base({ ctx: undefined }) })).includes("尚無資料") && html(e(Cards.ContextCard, { st: base({ ctx: null }) })).includes("尚無資料"));
  const all = [t, c30, settle, note, loading, night].join("");
  check("脈絡卡：任何形狀都沒有 undefined／NaN／null", !/undefined|NaN|null/.test(all));
}

// ── 整個面板（hooks 替身）──
const NAMES = ["st", "err"];
function renderPanel(forced = {}, props = {}) {
  let n = 0;
  React.useState = (init) => { const i = n++; return [Object.prototype.hasOwnProperty.call(forced, NAMES[i]) ? forced[NAMES[i]] : init, () => {}]; };
  React.useEffect = () => {}; React.useCallback = (f) => f;
  const out = renderToStaticMarkup(e(Panel, props));
  if (n !== NAMES.length) throw new Error(`useState 數量 ${n} ≠ 測試裡的 ${NAMES.length}：元件的 state 增減了，請同步更新 NAMES`);
  return out.replace(/<[^>]+>/g, "").replace(/&gt;/g, ">").replace(/&lt;/g, "<").replace(/&amp;/g, "&");
}
{
  const full = renderPanel({ st: base() });
  check("面板：舊的三欄還在，新的三張卡片也在", full.includes("外/內盤（只算真實成交）") && full.includes("日盤振幅") && full.includes("與盤前判斷")
    && full.includes("買賣掛單（五檔）") && full.includes("均價與基差") && full.includes("時段脈絡"));
  check("面板：compact 模式也有三張卡片", renderPanel({ st: base() }, { compact: true }).includes("買賣掛單（五檔）"));
  check("面板：還沒有行情時不顯示卡片", !renderPanel({ st: base({ ready: false }) }).includes("買賣掛單（五檔）") && renderPanel({ st: null }).includes("尚未收到行情"));
  const old = base(); for (const k of ["book", "session_flow", "vwap", "basis", "ex_range", "ctx", "range_source", "session"]) delete old[k];
  const o = renderPanel({ st: old });
  check("面板：舊後端（全部新欄位都沒有）照常顯示、新卡片顯示尚無", o.includes("日盤振幅") && o.includes("尚無五檔資料") && !/undefined|NaN/.test(o), o);
  check("面板：高低取自交易所時的說明", renderPanel({ st: base({ range_source: "exchange" }) }).includes("高低取自交易所"));
  check("面板：自己累計不完整時仍顯示原本的黃字警告", renderPanel({ st: base({ partial: true, since: "10:30:00" }) }).includes("重啟後重新累計，可能少算開盤段"));
  check("面板：免責聲明提到發現 28", renderPanel({ st: base() }).includes("發現 28"));
}

console.log(fail ? `\n${fail} 項失敗` : "\n全部通過");
process.exit(fail ? 1 : 0);
