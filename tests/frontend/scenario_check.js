// 持倉情境表（update.md 2026-11-10）：lib/scenario.ts 的純函式與 ScenarioCard（靜態渲染；useState 換成替身）。
// 用法：node tests/frontend/scenario_check.js（需要 frontend/node_modules）；由 tests/test_frontend_scenario.py 執行。
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "scenario-check-")));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });
const transpile = (src, out) => {
  fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText);
  return require(out);
};
const map = { "@/lib/scenario": path.join(TMP, "scenario.js"), "@/lib/api": path.join(TMP, "api.js") };
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) { return map[req] || orig.call(this, req, ...r); };
global.localStorage = { getItem: () => null, setItem: () => {} };
transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "api.js"));
const L = transpile(path.join(FRONT, "lib", "scenario.ts"), path.join(TMP, "scenario.js"));
const C = transpile(path.join(FRONT, "components", "ScenarioCard.tsx"), path.join(TMP, "card.js")).default;
let fail = 0, pass = 0;
const check = (name, ok, extra = "") => { if (ok) pass++; else { fail++; console.log("FAIL: " + name + "  " + JSON.stringify(extra).slice(0, 700)); } };
const J = JSON.stringify;
const pos = (code, direction, quantity, price = 43000, last = 43100) => ({ code, direction, quantity, price, last_price: last, pnl: 0, margin_original: 0 });
const mg = (equity, maint) => ({ equity, equity_amount: equity, margin_call: 0, initial_margin: 0, maintenance_margin: maint });

// ── 純函式 ──
check("isFuture：台指期三種是、選擇權不是", L.isFuture("TMFK6") && L.isFuture("MXFK6") && L.isFuture("TXFK6") && !L.isFuture("TXO22000J6") && !L.isFuture("TX122000K6") && !L.isFuture(""));
const P = [pos("TMFK6", "Buy", 2), pos("TXO22000K6", "Buy", 1), pos("MXFK6", "Sell", 0)];
check("isFuture：只看開頭（中間或結尾有 TMF 的不算）", !L.isFuture("ATMFK6") && !L.isFuture("XTXFK6") && L.isFuture("TXFK6"));
check("futures／hasOthers：只留數量 > 0 的期貨；有選擇權要知道", L.futures(P).length === 1 && L.futures(P)[0].code === "TMFK6" && L.hasOthers(P) && !L.hasOthers([pos("TMFK6", "Buy", 1)]) && !L.hasOthers([pos("TXO1", "Buy", 0)]));
check("exposure：每點金額、多正空負、各合約加總", L.exposure([pos("TMFK6", "Buy", 2)]) === 20 && L.exposure([pos("MXFK6", "Sell", 1)]) === -50 && L.exposure([pos("TMFK6", "Buy", 2), pos("MXFK6", "Sell", 1)]) === -30
  && L.exposure([pos("TXFK6", "Buy", 1)]) === 200 && L.exposure(P) === 20 && L.exposure([pos("TMFK6", "Buy", 5), pos("TMFK6", "Sell", 5)]) === 0 && L.exposure([]) === 0);
check("statusOf：權益 ≤ 0 歸零；低於維持保證金追繳；1.25 倍以內接近；剛好等於不算", L.statusOf(0, 12000) === "zero" && L.statusOf(-5, 12000) === "zero" && L.statusOf(11999, 12000) === "call" && L.statusOf(12000, 12000) === "near"
  && L.statusOf(14999, 12000) === "near" && L.statusOf(15000, 12000) === "ok" && L.statusOf(5000, 0) === "ok" && L.statusOf(0, 0) === "zero");
const r = L.rows(20, 53556, 12000);
check("rows：8 個情境、損益＝每點金額 × 漲跌、權益＝現在 ＋ 損益、占比", r.length === 8 && J(r.map((x) => x.move)) === J(L.MOVES) && r[0].pnl === -6000 && r[0].equity === 47556 && Math.abs(r[0].pct - (-6000 / 53556 * 100)) < 1e-9 && r[7].pnl === 6000 && r[7].equity === 59556 && r.every((x) => x.status === "ok"), r.slice(0, 2));
const r2 = L.rows(20, 20000, 12000, [-100, -300, -500, -1000, 100]);
check("rows：接近追繳、追繳、歸零的分界；自訂點數", J(r2.map((x) => x.status)) === J(["ok", "near", "call", "zero", "ok"]) && J(r2.map((x) => x.equity)) === J([18000, 14000, 10000, 0, 22000]), r2);
check("rows：權益 ≤ 0 時占比算不出來", L.rows(20, 0, 0, [-100])[0].pct === null && L.rows(-50, 1000, 0, [100])[0].pnl === -5000);
const rc = L.reach(20, 53556, 12000);
check("reach：多方看下跌、離追繳／歸零幾點（無條件捨去）", rc.dir === -1 && rc.toCall === 2077 && rc.toZero === 2677, rc);
const rs = L.reach(-50, 53556, 12000);
check("reach：空方看上漲、每點金額用絕對值", rs.dir === 1 && rs.toCall === Math.floor(41556 / 50) && rs.toZero === Math.floor(53556 / 50), rs);
check("reach：多空相抵、沒有維持保證金、已經低於追繳線、權益為負", J(L.reach(0, 5000, 100)) === J({ dir: 0, toCall: null, toZero: null }) && L.reach(20, 53556, 0).toCall === null && L.reach(20, 53556, 0).toZero === 2677
  && L.reach(20, 10000, 12000).toCall === 0 && L.reach(20, -100, 12000).toZero === 0 && L.reach(20, -100, 12000).toCall === 0);
check("格式：千分位、正負號用 − 與 +、百分比一位小數、缺值破折號", L.money(1234.6) === "1,235" && L.money(-6000) === "-6,000" && L.signed(-300) === "−300" && L.signed(50) === "+50" && L.signed(0) === "0" && L.pctText(-11.2036) === "−11.2%" && L.pctText(0.04) === "+0.0%" && L.pctText(null) === "—" && L.pctText(0) === "0.0%");
const S1 = L.summaryLines([pos("TMFK6", "Buy", 2)], mg(53556, 12000));
check("summaryLines：多 2 口微台 → 每跌 100 點、離追繳、離歸零", J(S1) === J(["價格每跌 100 點：損益 −2,000 元（占權益 3.7%）", "再跌約 2,077 點，權益掉到維持保證金 12,000 元（追繳）", "再跌約 2,677 點，權益歸零"]), S1);
const S2 = L.summaryLines([pos("MXFK6", "Sell", 1)], mg(53556, 12000));
check("summaryLines：空單 → 每漲", S2[0].startsWith("價格每漲 100 點：損益 −5,000 元（占權益 9.3%）") && S2[1].startsWith("再漲約 "), S2);
check("summaryLines：權益剛好 0 也說清楚（不能算出百分比）", J(L.summaryLines([pos("TMFK6", "Buy", 1)], mg(0, 12000))) === J(["權益已經 0 元（≤ 0）"]));
check("summaryLines：沒有資料、沒有期貨、多空相抵、權益 ≤ 0、沒有維持保證金、已低於追繳線", L.summaryLines([pos("TMFK6", "Buy", 1)], null) === null && L.summaryLines([], mg(1, 1)) === null && L.summaryLines([pos("TXO1", "Buy", 1)], mg(5e4, 1e4)) === null
  && J(L.summaryLines([pos("TMFK6", "Buy", 3), pos("TMFK6", "Sell", 3)], mg(5e4, 1e4))) === J(["多空相抵：價格漲跌對權益沒有影響（假設各合約同點數漲跌）"])
  && L.summaryLines([pos("TMFK6", "Buy", 1)], mg(-300, 1e4))[0] === "權益已經 -300 元（≤ 0）"
  && L.summaryLines([pos("TMFK6", "Buy", 1)], mg(40000, 0))[1] === "維持保證金還沒取得，算不出離追繳多遠"
  && L.summaryLines([pos("TMFK6", "Buy", 1)], mg(9000, 12000))[1] === "權益已經低於維持保證金 12,000 元（追繳線）" && L.summaryLines([pos("TMFK6", "Buy", 1)], { equity: NaN })  === null);

// ── 元件 ──
function render(props, forced = {}) {
  let n = 0;
  React.useState = (init) => { const i = n++; return [Object.prototype.hasOwnProperty.call(forced, i) ? forced[i] : init, () => {}]; };
  const out = renderToStaticMarkup(React.createElement(C, props));
  return { html: out, text: out.replace(/<[^>]+>/g, " ").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim() };
}
const base = { positions: [pos("TMFK6", "Buy", 2, 43000, 43100)], last: { TMFK6: 43250 }, margin: mg(53556, 12000) };
const closed = render(base);
check("收起時：標題、摘要三行、頁尾說明、有展開鈕、沒有表格", closed.text.includes("情境表（只提醒，不擋單）") && closed.text.includes("價格每跌 100 點：損益 −2,000 元") && closed.text.includes("再跌約 2,077 點") && closed.text.includes(L.FOOT_TEXT)
  && closed.text.includes("展開") && closed.html.includes('aria-expanded="false"') && !closed.html.includes("<table"), closed.text);
const opened = render(base, { 0: true });
check("展開後：8 列、價位用最新報價（43,250 ± 漲跌）、鈕變收起", opened.html.includes("<table") && (opened.html.match(/<tr/g) || []).length === 9 && opened.text.includes("43,250") === false && opened.text.includes("42,950") && opened.text.includes("43,550") && opened.text.includes("收起"), opened.text);
check("展開後：損益、權益、百分比", opened.text.includes("−6,000") && opened.text.includes("47,556") && opened.text.includes("+6,000") && opened.text.includes("59,556") && opened.text.includes("−11.2%"), opened.text);
const noQuote = render({ ...base, last: {} }, { 0: true });
const rowOf = (h, mv) => (h.match(new RegExp(`<tr><td[^>]*>${mv.replace("+", "\\+")}</td>.*?</tr>`)) || [""])[0];
check("價位欄：−300 那列是現價 −300（不是 +300）、+300 那列是現價 +300", rowOf(opened.html, "−300").includes(">42,950<") && rowOf(opened.html, "+300").includes(">43,550<") && !rowOf(opened.html, "−300").includes("43,550"), [rowOf(opened.html, "−300")]);
check("損益顏色：賠的紅、賺的綠", rowOf(opened.html, "−300").includes("text-[#ff1744]") && !rowOf(opened.html, "−300").includes("text-[#00e676]") && rowOf(opened.html, "+300").includes("text-[#00e676]") && !rowOf(opened.html, "+300").includes("text-[#ff1744]"));
const mixed = render({ positions: [pos("TMFK6", "Buy", 5, 43000, 43100), pos("TXFK6", "Buy", 1, 43000, 43100)], last: { TMFK6: 43111, TXFK6: 43222 }, margin: mg(60000, 20000) }, { 0: true });
check("價位欄用每點金額最大的那筆（大台 200 > 5 口微台 50）：欄名與價位", mixed.html.includes(">TXFK6</th>") && mixed.text.includes("42,922") && !mixed.text.includes("42,811"), mixed.text);
check("沒有即時報價就用部位自己的現價（43,100）", noQuote.text.includes("42,800") && noQuote.text.includes("43,400"), noQuote.text);
const noPrice = render({ ...base, last: {}, positions: [pos("TMFK6", "Buy", 2, 43000, 0)] }, { 0: true });
const ntd = (h) => (h.match(/<td/g) || []).length;
check("沒有價位時每列少一格", ntd(noPrice.html) === 8 * 4 && ntd(opened.html) === 8 * 5, [ntd(noPrice.html), ntd(opened.html)]);
const nth = (h) => (h.match(/<th[ >]/g) || []).length;                               // <thead> 也以 <th 開頭，要排除
check("完全沒有價位：不顯示價位欄（4 欄）；有價位時 5 欄", nth(noPrice.html) === 4 && nth(opened.html) === 5, [nth(noPrice.html), nth(opened.html)]);
const danger = render({ ...base, margin: mg(20000, 12000) }, { 0: true });
const cell = (h, t) => (h.match(new RegExp(`<td[^>]*>${t}</td>`, "g")) || []).length;               // 狀態欄的格子（摘要裡也有「追繳」「權益歸零」字樣，不能用整頁文字找）
check("權益 2 萬、維持 1.2 萬、多 2 口微台：−300 點是接近追繳、表上沒有追繳與歸零的列（最遠 −300）", cell(danger.html, "接近追繳") === 1 && cell(danger.html, "追繳") === 0 && cell(danger.html, "權益歸零") === 0, danger.html.match(/<td[^>]*>[^<]*(追繳|歸零)[^<]*<\/td>/g));
const danger2 = render({ positions: [pos("TXFK6", "Buy", 1)], last: {}, margin: mg(55000, 20000) }, { 0: true });
check("大台 1 口、權益 5.5 萬、維持 2 萬：−50 點 −10,000；−200 點 15,000 追繳、−300 點 −5,000 權益歸零；−100 點 35,000 沒事", danger2.text.includes("−10,000") && cell(danger2.html, "追繳") === 1 && cell(danger2.html, "權益歸零") === 1 && cell(danger2.html, "接近追繳") === 0
  && danger2.text.includes("15,000") && danger2.text.includes("-5,000") && danger2.text.includes("35,000"), danger2.text);
const edge = render({ positions: [pos("TXFK6", "Buy", 1)], last: {}, margin: mg(60000, 20000) }, { 0: true });
check("權益剛好掉到維持保證金（−200 點 ＝ 20,000）不算追繳、算接近", cell(edge.html, "接近追繳") === 1 && cell(edge.html, "追繳") === 0, edge.text);
check("空單：價位往上加、損益往上漲是負的", render({ positions: [pos("MXFK6", "Sell", 1, 43000, 43100)], last: {}, margin: mg(53556, 12000) }, { 0: true }).text.includes("43,400") );
check("沒有期貨持倉／沒有權益資料：整張不顯示", render({ ...base, positions: [] }).html === "" && render({ ...base, margin: null }).html === "" && render({ ...base, positions: [pos("TXO22000K6", "Buy", 1)] }).html === "");
const hedged = render({ ...base, positions: [pos("TMFK6", "Buy", 2), pos("TMFK6", "Sell", 2)] });
check("多空相抵：只有一行說明、沒有展開鈕", hedged.text.includes("多空相抵") && !hedged.text.includes("展開") && !hedged.html.includes("<table"));
check("權益資料過期要寫；沒過期不寫", render({ ...base, stale: true }).text.includes("權益資料不是最新的") && !closed.text.includes("權益資料不是最新的"));
check("同時有選擇權：頁尾說明不在表裡", render({ ...base, positions: [...base.positions, pos("TXO22000K6", "Buy", 1)] }).text.includes(L.OPTION_NOTE) && !closed.text.includes(L.OPTION_NOTE));
check("沒有 NaN／undefined／null", !/NaN|undefined|\bnull\b|Infinity/.test(opened.html + closed.html + danger2.html + hedged.html), opened.html.match(/NaN|undefined|null|Infinity/));
check("只顯示：沒有下單相關字樣", !/api\.order|placeOrder|fetch\(/.test(fs.readFileSync(path.join(FRONT, "components", "ScenarioCard.tsx"), "utf8") + fs.readFileSync(path.join(FRONT, "lib", "scenario.ts"), "utf8")));

console.log(`${pass} PASS, ${fail} FAIL`);
process.exit(fail ? 1 : 0);
