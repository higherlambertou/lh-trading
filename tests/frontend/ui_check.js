// 2026-11-12 的幾個直覺性修正：頁首行情順序固定（微台在前）、說明開關、委託卡空狀態一行、下單面板的「今日判斷」文字。
// hooks 換成替身、靜態渲染。用法：node tests/frontend/ui_check.js（需要 frontend/node_modules）
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "ui-check-")));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });
const transpile = (src, out) => {
  fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText);
  return require(out);
};
fs.writeFileSync(path.join(TMP, "lucide_stub.js"), "module.exports = new Proxy({}, { get: (_, n) => (p) => require('react').createElement('i', { 'data-icon': String(n) }) });");
fs.writeFileSync(path.join(TMP, "msp_stub.js"), "module.exports = { __esModule: true, STATE_TEXT: { TREND: '趨勢', REVERT: '均值回歸', UNCLEAR: '不明確', IV_LOW: 'IV 偏低', IV_HIGH: 'IV 偏高' } };");
const map = { "@/lib/api": path.join(TMP, "api.js"), "lucide-react": path.join(TMP, "lucide_stub.js"), "./MarketStatePanel": path.join(TMP, "msp_stub.js") };
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) { return map[req] || orig.call(this, req, ...r); };
transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "api.js"));
const Q = transpile(path.join(FRONT, "components", "QuoteBar.tsx"), path.join(TMP, "quotebar.js"));
const H = transpile(path.join(FRONT, "components", "HelpToggle.tsx"), path.join(TMP, "help.js"));
const T = transpile(path.join(FRONT, "components", "TodayHint.tsx"), path.join(TMP, "today.js"));
const Trades = transpile(path.join(FRONT, "components", "TradesPanel.tsx"), path.join(TMP, "trades.js")).default;
const e = React.createElement;
let fail = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + String(extra).slice(0, 700))); if (!ok) fail++; };
const strip = (raw) => raw.replace(/<[^>]+>/g, " ").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ");
const text = (el) => strip(renderToStaticMarkup(el));
const BAD = /undefined|NaN|\bnull\b|Infinity|\[object/;
function stub(forced) {                                   // 依宣告順序的 useState 替身
  let n = 0;
  React.useState = (init) => { const i = n++; return [i in forced ? forced[i] : init, () => {}]; };
  React.useEffect = () => {}; React.useCallback = (f) => f; React.useRef = (v) => ({ current: v });
  return () => n;
}

// ── 頁首行情條：順序固定（微台、台指），不看誰先到 ──
check("codeRank：TMF < TXF < MXF < 其他", Q.codeRank("TMFK6") < Q.codeRank("TXFK6") && Q.codeRank("TXFK6") < Q.codeRank("MXFK6") && Q.codeRank("MXFK6") < Q.codeRank("ZZZ"));
const q = (code, close) => ({ code, close, open: close, high: close + 10, low: close - 10, volume: 1, total_volume: 1, change_price: 5, ts: 1 });
for (const order of [["TXFK6", "TMFK6"], ["TMFK6", "TXFK6"]]) {
  const quotes = {}; for (const c of order) quotes[c] = q(c, c.startsWith("TMF") ? 43250 : 43249);
  stub({ 0: quotes, 1: true, 2: {} });
  const t = text(e(Q.default));
  check(`行情條：鍵的順序是 ${order.join(",")} 時，畫面仍是微台在前`, t.indexOf("微台") < t.indexOf("台指") && t.includes("43,250") && !BAD.test(t), t);
}

// ── 說明開關：預設關、按了改 <html data-help> 並記在 localStorage；沒有 localStorage 也不會壞 ──
{
  stub({ 0: false });
  const raw = renderToStaticMarkup(e(H.default));
  check("說明開關：預設顯示「說明：關」、aria-pressed=false", raw.includes("說明：關") && raw.includes('aria-pressed="false"'), raw);
  stub({ 0: true });
  const rawOn = renderToStaticMarkup(e(H.default));
  check("說明開關：開著時顯示「說明：開」、aria-pressed=true", rawOn.includes("說明：開") && rawOn.includes('aria-pressed="true"'), rawOn);
  const store = {};
  global.document = { documentElement: { dataset: {} } };
  global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };
  H.setHelp(true);
  check("setHelp(true)：<html data-help=on>、localStorage help:on=1", global.document.documentElement.dataset.help === "on" && store["help:on"] === "1" && H.helpOn() === true, JSON.stringify([global.document.documentElement.dataset, store]));
  H.setHelp(false);
  check("setHelp(false)：拿掉屬性、記 0", !("help" in global.document.documentElement.dataset) && store["help:on"] === "0" && H.helpOn() === false);
  global.localStorage = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
  let threw = false; try { H.setHelp(true); } catch (err) { threw = true; }
  check("localStorage 被擋：setHelp 不丟例外、畫面仍切換", !threw && global.document.documentElement.dataset.help === "on");
  delete global.document; delete global.localStorage;
  check("helpOn：沒有 document（伺服器端）是 false", H.helpOn() === false);
  const css = fs.readFileSync(path.join(FRONT, "app", "globals.css"), "utf8");
  check("CSS：沒開說明時 .help 整個不顯示", /html:not\(\[data-help="on"\]\) \.help \{ display: none !important; \}/.test(css));
  const layout = fs.readFileSync(path.join(FRONT, "app", "layout.tsx"), "utf8");
  check("layout：第一次繪製前就從 localStorage 把 data-help 設回來", layout.includes("localStorage.getItem('help:on')==='1'") && layout.includes("dataset.help='on'") && layout.includes("dangerouslySetInnerHTML={{ __html: HELP_INIT }}"));
}

// ── 今日判斷那一行（下單面板）：狀態文字用和市場狀態面板同一張表、建議、日 K ──
{
  check("hintText：IV 偏高、不操作、日 K 偏空", T.hintText({ ready: true, state: "IV_HIGH", strategies: [], direction_label: "偏空", config: {} }) === "IV 偏高 · 建議不操作 · 日 K 偏空", T.hintText({ ready: true, state: "IV_HIGH", strategies: [], direction_label: "偏空", config: {} }));
  check("hintText：趨勢、有建議的策略", T.hintText({ ready: true, state: "TREND", strategies: ["orb", "trend"], config: {} }) === "趨勢 · 建議orb、trend", T.hintText({ ready: true, state: "TREND", strategies: ["orb", "trend"], config: {} }));
  check("hintText：不認得的狀態照原字、沒有方向就不寫", T.hintText({ ready: true, state: "WEIRD", config: {} }) === "WEIRD · 建議不操作");
  stub({ 0: null });
  check("TodayHint：還沒載到就什麼都不畫", renderToStaticMarkup(e(T.default)) === "");
  stub({ 0: { ready: false, config: {} } });
  check("TodayHint：盤前還沒算出來也不畫", renderToStaticMarkup(e(T.default)) === "");
  stub({ 0: { ready: true, state: "IV_HIGH", strategies: [], direction_label: "偏空", config: {} } });
  const rawT = renderToStaticMarkup(e(T.default)), t = strip(rawT);
  check("TodayHint：一行「今日判斷 IV 偏高 · 建議不操作 · 日 K 偏空 只提醒」", t.includes("今日判斷") && t.includes("IV 偏高 · 建議不操作 · 日 K 偏空") && t.includes("只提醒") && !BAD.test(t), t);
  check("TodayHint：IV 偏高用黃色", rawT.includes("color:#ffc107"), rawT);
}

// ── 委託卡：沒有內容時標題和「無…」同一行、沒有表格；有內容才有表格（可橫捲） ──
{
  stub({ 0: [], 1: [], 2: null });
  const raw = renderToStaticMarkup(e(Trades)), t = strip(raw);
  check("委託卡（空）：兩個標題各接一句「無」、沒有 <table>", t.includes("停損停利監控 無監控中的部位") && t.includes("委託紀錄 今日無委託紀錄") && !raw.includes("<table"), t);
  stub({ 0: [{ id: "T1", action: "Buy", price: 0, deal_price: 43000, quantity: 1, deal_quantity: 1, status: "Filled", deal_time: "10:00:00" }], 1: [], 2: null });
  const raw2 = renderToStaticMarkup(e(Trades)), t2 = strip(raw2);
  check("委託卡（有委託）：表格在可橫捲的容器裡、不寫「今日無委託紀錄」", raw2.includes('class="scroll-x"') && raw2.includes("<table") && t2.includes("43,000") && !t2.includes("今日無委託紀錄") && t2.includes("無監控中的部位"), t2);
}
console.log(fail ? `\n${fail} FAIL` : "\n全部通過, 0 FAIL");
process.exit(fail ? 1 : 0);
