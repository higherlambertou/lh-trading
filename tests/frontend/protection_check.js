// 〈沒有停損保護〉橫幅：純函式、元件、放進〈部位面板〉的位置（hooks 換成替身）。
// 畫面吃的資料由 tests/test_frontend_protection.py 用 core/protection.py 現算（環境變數 PROT_FIXTURES 指到目錄）。
// 用法：PROT_FIXTURES=<dir> node tests/frontend/protection_check.js（需要 frontend/node_modules）
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
if (!process.env.PROT_FIXTURES) { console.error("需要環境變數 PROT_FIXTURES（由 tests/test_frontend_protection.py 產生）"); process.exit(2); }
const fx = JSON.parse(fs.readFileSync(path.join(process.env.PROT_FIXTURES, "fixtures.json"), "utf8"));
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "protection-check-")));   // realpath：macOS 的 /var 是 /private/var 的連結，不然同一個模組會被載入兩份
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });
const transpile = (src, out) => {
  fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText);
  return require(out);
};
fs.writeFileSync(path.join(TMP, "lucide_stub.js"), "module.exports = new Proxy({}, { get: (_, n) => (p) => require('react').createElement('i', { 'data-icon': String(n) }) });");
const map = { "@/lib/protection": path.join(TMP, "protection.js"), "@/lib/api": path.join(TMP, "api.js"), "./UnprotectedBanner": path.join(TMP, "banner.js"), "lucide-react": path.join(TMP, "lucide_stub.js"),
              "@/lib/staleness": path.join(TMP, "staleness.js") };
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) { return map[req] || orig.call(this, req, ...r); };
const P = transpile(path.join(FRONT, "lib", "protection.ts"), path.join(TMP, "protection.js"));
transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "api.js"));
const S = transpile(path.join(FRONT, "lib", "staleness.ts"), path.join(TMP, "staleness.js"));
const Banner = transpile(path.join(FRONT, "components", "UnprotectedBanner.tsx"), path.join(TMP, "banner.js")).default;
const Panel = transpile(path.join(FRONT, "components", "PositionPanel.tsx"), path.join(TMP, "panel.js")).default;
const e = React.createElement;
let fail = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + String(extra).slice(0, 700))); if (!ok) fail++; };
const text = (el) => renderToStaticMarkup(el).replace(/<[^>]+>/g, "").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ");
const BAD = /undefined|NaN|\bnull\b|Infinity|\[object/;

check("方向翻成多／空，不認得的是 ?", P.dirZh("Buy") === "多" && P.dirZh("Sell") === "空" && P.dirZh("x") === "?" && P.dirZh("") === "?");
check("後端現算的資料形狀（單筆、部分覆蓋、兩筆）", fx.single.length === 1 && fx.partial[0].quantity === 1 && fx.partial[0].total === 3 && fx.two.length === 2 && fx.none.length === 0, JSON.stringify(fx));
check("描述：單筆", P.describeUnprotected(fx.single) === "TMFJ6 多 1 口", P.describeUnprotected(fx.single));
check("描述：只有部分口數沒保護時寫出總口數", P.describeUnprotected(fx.partial) === "TMFJ6 多 1 口（共 3 口）", P.describeUnprotected(fx.partial));
check("描述：多筆用頓號", P.describeUnprotected(fx.two) === "TMFJ6 多 1 口、MXFJ6 空 2 口", P.describeUnprotected(fx.two));
check("描述：空陣列是空字串", P.describeUnprotected([]) === "");

for (const empty of [[], null, undefined]) check(`橫幅：沒有資料（${JSON.stringify(empty)}）什麼都不畫`, renderToStaticMarkup(e(Banner, { items: empty })) === "");
const t1 = text(e(Banner, { items: fx.single }));
check("橫幅：寫出哪一筆沒有保護、為什麼、怎麼辦", t1.includes("沒有停損保護：TMFJ6 多 1 口") && t1.includes("這些口數沒有策略在跑、也沒有停損監控覆蓋") && t1.includes("〈委託〉面板") && !BAD.test(t1), t1);
const raw = renderToStaticMarkup(e(Banner, { items: fx.two }));
check("橫幅：role=alert、黃色警告樣式、圖示", raw.includes('role="alert"') && raw.includes("#ffc107") && raw.includes('data-icon="TriangleAlert"'), raw.slice(0, 300));
check("橫幅：兩筆", text(e(Banner, { items: fx.two })).includes("TMFJ6 多 1 口、MXFJ6 空 2 口"));

// 〈部位面板〉：hooks 換成替身，強制 state（順序：positions, margin, pnlList, error, quotes, dataAge, posAge, unprotected, cadence）
const NAMES = ["positions", "margin", "pnlList", "error", "quotes", "dataAge", "posAge", "unprotected", "cadence"];
function panel(forced) {
  let n = 0;
  React.useState = (init) => { const i = n++; return [Object.prototype.hasOwnProperty.call(forced, NAMES[i]) ? forced[NAMES[i]] : init, () => {}]; };
  React.useEffect = () => {}; React.useCallback = (f) => f;
  return renderToStaticMarkup(e(Panel));
}
const POS = [{ code: "TMFJ6", direction: "Buy", quantity: 1, price: 49000, last_price: 49010, pnl: 100 }];
const p1 = panel({ positions: POS, unprotected: fx.single, posAge: 2, dataAge: 2 });
check("部位面板：有持倉又沒保護 → 橫幅出現在標題下面、持倉列表上面", p1.includes("沒有停損保護") && p1.indexOf("部位 / 損益") < p1.indexOf("沒有停損保護") && p1.includes("49,000") && p1.indexOf("沒有停損保護") < p1.indexOf("49,000"), p1.slice(0, 400));
check("部位面板：有持倉但有保護（空陣列）→ 沒有橫幅", !panel({ positions: POS, unprotected: [], posAge: 2, dataAge: 2 }).includes("沒有停損保護"));
check("部位面板：沒有持倉 → 就算後端傳來資料也不畫橫幅（不會有「沒有持倉卻說沒保護」）", !panel({ positions: [], unprotected: fx.single, posAge: 2, dataAge: 2 }).includes("沒有停損保護"));
check("部位面板：預設狀態（還沒載入）沒有橫幅", !panel({}).includes("沒有停損保護"));
check("過期門檻：平日部位 30 秒、保證金 150 秒；週末部位 360 秒、保證金 1500 秒", S.posLimit({ positions_every: 5, margin_every: 60 }) === 30 && S.marginLimit({ positions_every: 5, margin_every: 60 }) === 150
  && S.posLimit({ positions_every: 120 }) === 360 && S.marginLimit({ margin_every: 600 }) === 1500 && S.staleAge(45, 2, { positions_every: 5, margin_every: 60 }) === null && S.staleAge(200, 40, null) === 200);
// 資料過期的判斷照刷新間隔（發現 69）：保證金本來就 60 秒刷新一次，45 秒不是過期；週末部位 2 分鐘刷新一次
const WEEKDAY = { positions_every: 5, margin_every: 60 }, WEEKEND = { positions_every: 120, margin_every: 600 };
check("過期：平日保證金 45 秒前、部位 2 秒前 → 不警告（以前會誤報）", !panel({ positions: [], dataAge: 45, posAge: 2, cadence: WEEKDAY }).includes("後端可能重啟中"));
check("過期：平日保證金 200 秒前 → 警告", panel({ positions: [], dataAge: 200, posAge: 2, cadence: WEEKDAY }).includes("資料 200s 前（後端可能重啟中）"));
check("過期：平日部位 40 秒前 → 警告、而且不說「目前無持倉」", (() => { const t = panel({ positions: [], dataAge: 10, posAge: 40, cadence: WEEKDAY }); return t.includes("資料 40s 前") && t.includes("部位資料已過期（40 秒前）") && !t.includes("目前無持倉"); })());
check("過期：週末部位 100 秒前、保證金 300 秒前 → 正常（目前無持倉）", (() => { const t = panel({ positions: [], dataAge: 300, posAge: 100, cadence: WEEKEND }); return !t.includes("後端可能重啟中") && t.includes("目前無持倉"); })());
check("過期：週末部位 400 秒前 → 部位資料已過期", panel({ positions: [], dataAge: 300, posAge: 400, cadence: WEEKEND }).includes("部位資料已過期（400 秒前）"));
check("過期：舊版後端沒有間隔 → 用預設（部位 30 秒、保證金 150 秒）", !panel({ positions: [], dataAge: 100, posAge: 20 }).includes("後端可能重啟中") && panel({ positions: [], dataAge: 10, posAge: 31 }).includes("部位資料已過期"));
// 載入：meta 的 unprotected 真的被讀進 state（effect 照真的跑一次，API 換成替身）
const api = require(path.join(TMP, "api.js")).api;
async function load(meta) {
  const set = {}; const effects = []; let n = 0;
  React.useState = (init) => { const name = NAMES[n++]; return [init, (v) => { set[name] = v; }]; };
  React.useEffect = (f) => { effects.push(f); }; React.useCallback = (f) => f;
  api.position.list = async () => POS; api.position.margin = async () => { throw new Error("x"); }; api.position.pnl = async () => [];
  api.position.meta = meta;
  global.window = { addEventListener() {}, removeEventListener() {} };
  const realSet = global.setInterval, realClear = global.clearInterval;
  global.setInterval = () => 1; global.clearInterval = () => {};
  try { renderToStaticMarkup(e(Panel)); effects[0](); await new Promise((r) => setImmediate(r)); await new Promise((r) => setImmediate(r)); }
  finally { global.setInterval = realSet; global.clearInterval = realClear; delete global.window; }
  return set;
}
(async () => {
  const ok = await load(async () => ({ updated_at: 1, age_sec: 1, positions_age_sec: 2, unprotected: fx.single }));
  check("載入：meta 帶 unprotected → 讀進 state（和持倉一起）", JSON.stringify(ok.unprotected) === JSON.stringify(fx.single) && ok.positions === POS && ok.posAge === 2, JSON.stringify(ok));
  const none = await load(async () => ({ updated_at: 1, age_sec: 1, positions_age_sec: 2 }));
  check("載入：舊版後端的 meta 沒有這個欄位 → 空陣列（不會壞掉）", JSON.stringify(none.unprotected) === "[]", JSON.stringify(none));
  const bad = await load(async () => { throw new Error("500"); });
  check("載入：meta 失敗 → 空陣列（取不到就不亂警告）", JSON.stringify(bad.unprotected) === "[]" && bad.posAge === -1, JSON.stringify(bad));
  console.log(fail ? `\n${fail} 項失敗` : "\n全部通過");
  process.exit(fail ? 1 : 0);
})();
