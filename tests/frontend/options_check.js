// 〈選擇權試算〉面板：表單驗證與請求內容、到期月預設、格式；結果畫面（試算／平倉）；容器的操作流程。
// 前端沒有 DOM 測試工具：純函式直接呼叫；結果檢視（沒有 hooks）用 react-dom/server 渲染；容器把 hooks 換成替身後直接呼叫元件函式，
// 取元素樹、呼叫 onClick／onChange。畫面吃的資料由 tests/test_frontend_options.py 用 core/options_calc.py 現算（環境變數 OPT_FIXTURES 指到目錄）。
// 用法：OPT_FIXTURES=<dir> node tests/frontend/options_check.js（需要 frontend/node_modules）
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
if (!process.env.OPT_FIXTURES) { console.error("需要環境變數 OPT_FIXTURES（由 tests/test_frontend_options.py 產生）"); process.exit(2); }
const fx = (n) => JSON.parse(fs.readFileSync(path.join(process.env.OPT_FIXTURES, n + ".json"), "utf8"));
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "options-check-"));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });

const transpile = (src, out) => {
  fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText);
  return require(out);
};

// ── 模組：lib/options（真的）、lib/api 的 apiErrorDetail（真的）、api 物件（替身）、lucide（替身）──
const O = transpile(path.join(FRONT, "lib", "options.ts"), path.join(TMP, "options.js"));
const realApi = transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "real_api.js"));
const calls = [];
let impl = {};
const spy = (name) => (...a) => { calls.push([name, ...a]); return impl[name] ? impl[name](...a) : Promise.resolve(undefined); };
global.__api_stub = { api: { order: { optionExpiries: spy("expiries") }, options: { calc: spy("calc"), close: spy("close"), chain: spy("chain") } },
                      apiErrorDetail: realApi.apiErrorDetail };
fs.writeFileSync(path.join(TMP, "api_stub.js"), "module.exports = global.__api_stub;");
fs.writeFileSync(path.join(TMP, "lucide_stub.js"), "module.exports = new Proxy({}, { get: () => () => null });");
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) {
  if (req === "@/lib/api") return path.join(TMP, "api_stub.js");
  if (req === "@/lib/options") return path.join(TMP, "options.js");
  if (req === "lucide-react") return path.join(TMP, "lucide_stub.js");
  return orig.call(this, req, ...r);
};
const P = transpile(path.join(FRONT, "components", "OptionCalcPanel.tsx"), path.join(TMP, "panel.js"));
const Comp = P.default, e = React.createElement;

let fail = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + String(extra).slice(0, 700))); if (!ok) fail++; };
const strip = (s) => s.replace(/<[^>]+>/g, "").replace(/&gt;/g, ">").replace(/&lt;/g, "<").replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#x27;/g, "'");
const html = (el) => strip(renderToStaticMarkup(el));
const J = JSON.stringify;
const BAD = /undefined|NaN|\bnull\b|Infinity/;

// ══ 純函式 ═══════════════════════════════════════════════════════
const live = { manual: false, deliveryMonth: "202610", mF: "", mIv: "", mDays: "", mSpread: "2" };
const man = { manual: true, deliveryMonth: "", mF: "49300", mIv: "20.5", mDays: "9", mSpread: "2" };
{
  const b = O.buildCalcRequest(O.DEFAULT_FORM, live);
  check("請求：預設表單＋即時＋到期月", b.ok && b.body.kind === "straddle" && b.body.delivery_month === "202610" && b.body.lots === 1 && b.body.hold_hours === 6
    && b.body.category === "TXO" && b.body.select === "delta" && !("move_pts" in b.body) && !("budget_twd" in b.body) && !("manual" in b.body), J(b));
  const f = { ...O.DEFAULT_FORM, kind: "strangle", select: "move", k: "0.8", movePts: "800", lots: "3", budget: "100000", holdHours: "12" };
  const m = O.buildCalcRequest(f, live);
  check("請求：勒式＋預期波動點數＋預算", m.ok && m.body.select === "move" && m.body.k === 0.8 && m.body.move_pts === 800 && m.body.lots === 3 && m.body.budget_twd === 100000 && m.body.hold_hours === 12, J(m));
  const mm = O.buildCalcRequest(O.DEFAULT_FORM, man);
  check("請求：手動模式帶 manual、不帶到期月", mm.ok && !("delivery_month" in mm.body) && J(mm.body.manual) === J({ F: 49300, iv: 20.5, days: 9, spread_pct: 0.02 }), J(mm));
  const bad = (f2, s2, re) => { const r = O.buildCalcRequest(f2, s2); return !r.ok && re.test(r.error); };
  const F = O.DEFAULT_FORM;
  check("請求：跨式不檢查 delta／k（用不到）", O.buildCalcRequest({ ...F, kind: "straddle", delta: "abc", k: "xyz" }, live).ok);
  check("請求：delta 要在 0~1", bad({ ...F, kind: "strangle", delta: "0" }, live, /Delta/) && bad({ ...F, kind: "call", delta: "1.2" }, live, /Delta/) && bad({ ...F, kind: "put", delta: "" }, live, /Delta/));
  check("請求：距離倍數範圍", bad({ ...F, kind: "strangle", select: "move", k: "9" }, live, /倍數/) && O.buildCalcRequest({ ...F, kind: "call", select: "move", k: "-0.3" }, live).ok);
  check("請求：預期波動、組數、預算、持有時間", bad({ ...F, movePts: "-5" }, live, /預期波動/) && bad({ ...F, lots: "0" }, live, /組數/) && bad({ ...F, lots: "1.5" }, live, /組數/)
    && bad({ ...F, budget: "0" }, live, /預算/) && bad({ ...F, holdHours: "-1" }, live, /持有時間/) && bad({ ...F, holdHours: "9999" }, live, /持有時間/));
  check("請求：沒選到期月", bad(F, { ...live, deliveryMonth: "" }, /到期月/));
  check("請求：手動模式的各欄位", bad(F, { ...man, mF: "" }, /遠期價/) && bad(F, { ...man, mIv: "0" }, /IV/) && bad(F, { ...man, mDays: "-1" }, /天數/) && bad(F, { ...man, mSpread: "150" }, /價差/));
  check("切換結構的預設 delta", O.defaultDelta("strangle") === "0.25" && O.defaultDelta("call") === "0.40" && O.defaultDelta("put") === "0.40");
}
{
  const rows = [{ right: "C", strike: "49300", qty: "2", entry: "615", bid: "590", ask: "" }, { right: "P", strike: "49300", qty: "1", entry: "635.5", bid: "", ask: "640" }];
  const b = O.buildCloseRequest(rows, live, "700", "12");
  check("平倉請求：各腳、選填的買賣價、預期波動、持有時間", b.ok && J(b.body.legs) === J([{ right: "C", strike: 49300, qty: 2, entry: 615, bid: 590 }, { right: "P", strike: 49300, qty: 1, entry: 635.5, ask: 640 }])
    && b.body.move_pts === 700 && b.body.hold_hours === 12 && b.body.delivery_month === "202610" && b.body.category === "TXO", J(b));
  const bad = (rs, re, s2 = live) => { const r = O.buildCloseRequest(rs, s2, "", "24"); return !r.ok && re.test(r.error); };
  const ok1 = { right: "C", strike: "100", qty: "1", entry: "5", bid: "", ask: "" };
  check("平倉請求：錯誤訊息指出是第幾腳", bad([ok1, { ...ok1, strike: "" }], /第 2 腳：履約價/) && bad([{ ...ok1, qty: "0" }], /第 1 腳：口數/) && bad([{ ...ok1, entry: "" }], /第 1 腳：買進價/)
    && bad([{ ...ok1, entry: "-1" }], /買進價/) && bad([{ ...ok1, bid: "-3" }], /目前買價/) && bad([{ ...ok1, ask: "x" }], /目前賣價/));
  check("平倉請求：沒有任何一腳、沒選到期月、手動欄位不對", bad([], /至少/) && bad([ok1], /到期月/, { ...live, deliveryMonth: "" }) && bad([ok1], /遠期價/, { ...man, mF: "" }));
  check("平倉請求：手動模式", O.buildCloseRequest([ok1], man, "", "24").ok && "manual" in O.buildCloseRequest([ok1], man, "", "24").body);
  const calc = fx("calc_straddle");
  const back = O.rowsFromCalc(calc);
  check("帶到平倉：各腳、買進價＝進場價、口數＝每腳口數 × 組數", back.length === 2 && back[0].right === "C" && back[1].right === "P" && back[0].qty === String(calc.legs[0].qty * calc.cost.lots)
    && back[0].entry === String(calc.legs[0].entry) && back[0].bid === "" && back[0].strike === String(calc.legs[0].strike), J(back));
}
{
  const d = O.thirdWednesday("202610");
  check("第三個週三（2026-10 是 21 號）", d && d.getFullYear() === 2026 && d.getMonth() === 9 && d.getDate() === 21 && d.getHours() === 13 && d.getMinutes() === 30 && O.thirdWednesday("2026") === null);
  const d2 = O.thirdWednesday("202611"), d3 = O.thirdWednesday("202606");
  check("第三個週三（2026-11 是 18 號、2026-06 是 17 號）", d2.getDate() === 18 && d2.getDay() === 3 && d3.getDate() === 17 && d3.getDay() === 3);
  check("預設到期月：第一個距到期 ≥ 7 天的", O.defaultMonth(["202611", "202610", "202612"], new Date(2026, 9, 12, 18, 0)) === "202610"
    && O.defaultMonth(["202610", "202611"], new Date(2026, 9, 15, 10, 0)) === "202611" && O.defaultMonth(["202610"], new Date(2026, 9, 20)) === "202610" && O.defaultMonth([]) === "");
}
{
  check("格式：點與正負號", O.pts(1234.5, 1) === "1,234.5" && O.pts(null) === "—" && O.pts(NaN) === "—" && O.spts(-3.04, 1) === "−3.0" && O.spts(0.01, 1) === "0.0" && O.spts(1267, 0) === "+1,267");
  check("格式：元", O.twd(-62500) === "−62,500 元" && O.twd(62500) === "62,500 元" && O.twd(null) === "—" && O.stwd(1500) === "+1,500" && O.stwd(-1500) === "−1,500" && O.stwd(0) === "0");
  check("格式：百分比與 σ", O.pct1(0.43, 0) === "43%" && O.pct1(null) === "—" && O.spct(0.0257) === "+2.57%" && O.spct(-0.0123) === "−1.23%" && O.sigma(-0.795) === "−0.80σ" && O.sigma(0.817) === "+0.82σ");
  check("損益兩平一行字", O.beText({ level: 50556.4, dist_pts: 1266.7, dist_pct: 0.02571, dist_em: 0.817 }) === "上 50,556（+1,267 點、+2.57%、+0.82σ）"
    && O.beText({ level: 48050, dist_pts: -1233.3, dist_pct: -0.025, dist_em: -0.795 }).startsWith("下 48,050（−1,233 點"));
  check("顏色：賺綠、賠紅、零灰", O.pnlColor(5) === "#00e676" && O.pnlColor(-5) === "#ff1744" && O.pnlColor(0) === "#7070a0" && O.pnlColor(null) === "#7070a0");
}

// ══ 結果畫面 ═════════════════════════════════════════════════════
{
  const r = fx("calc_straddle");
  const t = html(e(P.CalcResultView, { r, onToClose: () => {} }));
  check("試算畫面：來源、到期月、距到期、遠期價、ATM IV", t.includes("即時報價") && t.includes("到期月 202610（2026-10-21 13:30）") && t.includes(`距到期 ${r.days} 天`) && t.includes(`遠期價 ${O.pts(r.F, 1)}`) && t.includes(`ATM IV ${r.atm_iv}%`), t.slice(0, 400));
  check("試算畫面：成本、組數、預算", t.includes(O.twd(r.cost.total_twd)) && t.includes(`${r.cost.lots} 組`) && t.includes(`最多買 ${r.cost.max_lots} 組`) && t.includes(O.twd(r.cost.budget_twd)), t.slice(0, 900));
  check("試算畫面：最大虧損", t.includes("最大虧損") && t.includes(`−${O.twd(r.max_loss.twd)}`));
  check("試算畫面：損益兩平（每一個）", r.breakeven.length === 2 && r.breakeven.every((b) => t.includes(O.beText(b))));
  check("試算畫面：預期波動與機率（模型、歷史）", t.includes(`1σ ≈ ${O.pts(r.expected_move.sigma_pts)} 點`) && t.includes(O.pct1(r.prob.lognormal, 0)) && r.prob.hist !== null
    && t.includes(`過去任意 ${r.prob.hist.h_days} 個交易日`) && t.includes(O.pct1(r.prob.hist.p, 0)), t);
  check("試算畫面：各腳表格", r.legs.every((l) => t.includes(O.pts(l.strike)) && t.includes(O.pts(l.entry, 1))) && t.includes("買權 Call") && t.includes("賣權 Put") && t.includes("賣價（進場）"));
  check("試算畫面：Greeks（元）", t.includes("Theta（每天") && t.includes(O.stwd(r.greeks.theta_day_twd)) && t.includes(O.stwd(r.greeks.vega_twd)) && t.includes(r.greeks.delta.toFixed(3)));
  const rows = (t.match(/不動\s*(?=[+−0-9])/g) || []).length, titles = t.includes("價格 × 時間，IV 不變") && t.includes("價格 × IV 平移");
  check("試算畫面：兩張情境表（各 9 列，其中一列「不動」）", titles && rows === 2 && r.scenarios.moves.length === 9 && r.scenarios.time_cols.every((c) => t.includes(c.label)), `${rows} ${titles}`);
  check("試算畫面：情境表的格子是元、正負號", t.includes(O.stwd(r.scenarios.pnl_twd[0][0])) && t.includes(O.stwd(r.scenarios.iv.pnl_twd[4][2])));
  check("試算畫面：有「帶到平倉試算」按鈕與免責說明", t.includes("帶到平倉試算") && t.includes("不是預測") && t.includes("不下單"));
  check("試算畫面：沒給 onToClose 就沒有那個按鈕", !html(e(P.CalcResultView, { r })).includes("帶到平倉試算"));
  check("試算畫面：沒有 undefined／NaN／null", !BAD.test(t), t.match(BAD));
  const u = fx("calc_strangle_user");
  const tu = html(e(P.CalcResultView, { r: u }));
  check("試算畫面：自己填預期波動 → 標示 1σ 用你的數字", u.expected_move.ref_source === "user" && tu.includes("你填的 600 點") && tu.includes("情境表以你填的 600 點為 1σ"), tu);
  check("試算畫面：沒有歷史收盤時說沒有、價差寬的警告用 ⚠ 顯示", tu.includes("沒有歷史收盤可比較") && u.warnings.length > 0 && u.warnings.every((w) => tu.includes("⚠ " + w)) && tu.includes("買賣價差佔中價"), tu);
  check("試算畫面：勒式兩個損益兩平、兩腳", u.breakeven.length === 2 && u.legs.length === 2 && !BAD.test(tu), tu.match(BAD));
  const c = fx("calc_call");
  const tc = html(e(P.CalcResultView, { r: c }));
  check("試算畫面：單向 Call 只有一個損益兩平、一腳", c.breakeven.length === 1 && c.legs.length === 1 && tc.includes(O.beText(c.breakeven[0])) && !BAD.test(tc));
  const tp = html(e(P.CalcResultView, { r: { ...c, source: "manual", delivery_month: null, expiry: null } }));
  check("試算畫面：手動模式標「手動模型」、沒有到期月", tp.includes("手動模型") && !tp.includes("到期月") && !BAD.test(tp), tp.slice(0, 300));
}
{
  const hs = fx("calc_straddle").hedge, hc = fx("calc_call").hedge;
  check("對沖文字：賣出／買進／不用對沖", O.hedgeText({ ...hs, tmf_lots: -4, net_delta: 0.8, delta_twd_per_pt: 40, residual_twd_per_pt: 0 }) === "淨 Delta 0.80（約 +40 元/點）→ 賣出 4 口微台可中和，剩 0 元/點"
    && O.hedgeText({ ...hs, tmf_lots: 3, net_delta: -0.6, delta_twd_per_pt: -30, residual_twd_per_pt: 10 }) === "淨 Delta -0.60（約 −30 元/點）→ 買進 3 口微台可中和，剩 +10 元/點"
    && O.hedgeText({ ...hs, tmf_lots: 0, net_delta: 0.01, delta_twd_per_pt: 1 }).endsWith("：不用對沖"), O.hedgeText(hs));
  const t = html(e(P.HedgeCard, { h: hs }));
  check("對沖卡片：建議口數、打平點與實際波動、成本", t.includes("用微台對沖 Delta（試算，不下單）") && t.includes(O.hedgeText(hs)) && hs.be_rms_pts > 0 && t.includes(`${O.pts(hs.be_rms_pts)} 點`)
    && t.includes(`市場隱含約 ${O.pts(hs.implied_rms_pts)} 點`) && t.includes(`最近 20 個交易日實際約 ${O.pts(hs.realized_rms_pts)} 點`) && t.includes(`${hs.ratio.toFixed(2)} 倍`)
    && t.includes("只顯示、不下單"), t);
  // 成本那一句：要對沖時顯示「每次對沖約付 X」，不用對沖（0 口）時改說每口多少，不要寫「每次對沖約付 0 元」
  const tl = html(e(P.HedgeCard, { h: { ...hs, tmf_lots: -4, net_delta: 0.8, cost_per_hedge_twd: 120, cost_pts: 3 } }));
  const tz = html(e(P.HedgeCard, { h: { ...hs, tmf_lots: 0, net_delta: 0.01, cost_per_hedge_twd: 0, cost_pts: 3 } }));
  check("對沖卡片：成本那一句（要對沖／不用對沖）", tl.includes(`每次對沖約付 ${O.twd(120)}`) && !tl.includes("目前不用對沖")
    && tz.includes("目前不用對沖") && tz.includes(`每買賣 1 口微台約付 ${O.twd(30)}`) && !tz.includes("每次對沖約付"), [tl.slice(-500), tz.slice(-500)]);
  // 歷史模擬的結論要寫在卡片上（數字來自 update.md 發現 30 C，改動時兩邊要一起改）
  check("對沖卡片：歷史模擬的結論（標準差 73%→26～29%、平均賠 −7%／−10～11%、不會變成賺錢）", ["77 個月", "73% 降到 26～29%", "每天對沖 −7%", "−10～11%", "不會讓買跨式變成賺錢", "update.md 發現 30"].every((x) => t.includes(x)), t.slice(-700));
  const tn = html(e(P.HedgeCard, { h: { ...hs, realized_rms_pts: null, ratio: null } }));
  check("對沖卡片：沒有歷史收盤時說沒有", tn.includes("沒有歷史收盤可比較") && !tn.includes("最近 20 個交易日"));
  const tg = html(e(P.HedgeCard, { h: { ...hs, be_rms_pts: null, implied_rms_pts: null, realized_rms_pts: null, ratio: null } }));
  check("對沖卡片：沒有 Gamma／Theta 時的說明", tg.includes("沒有 Gamma／Theta 可算") && !tg.includes("才賺得回"));
  const tc = html(e(P.CalcResultView, { r: fx("calc_call") }));
  check("試算畫面：有對沖卡片、單向 Call 建議賣微台", tc.includes("用微台對沖 Delta") && tc.includes("賣出") && hc.tmf_lots < 0 && !BAD.test(tc), tc.match(BAD));
  check("對沖卡片沒有 undefined／NaN／null", !BAD.test(t + tn + tg), (t + tn + tg).match(BAD));
}
{
  const r = fx("close_doc");
  const t = html(e(P.CloseResultView, { r, sel: "close_1", onSelect: () => {} }));
  check("平倉畫面：各腳、虧損腳與獲利腳", t.includes("虧損腳") && t.includes("獲利腳") && t.includes("買權 Call") && t.includes("賣權 Put"), t.slice(0, 600));
  check("平倉畫面：四個動作", ["全部持有", "只平掉 買權 100", "只平掉 賣權 100", "全部平掉"].every((x) => t.includes(x)), t);
  // 需求文件的例子：Put 剩 20 → 平掉 Put 已虧 30 點（×50＝−1,500 元），留下 Call 要值到 80 才回本，損益兩平在 履約價 100 + 80 ＝ 180
  const a = r.actions.find((x) => x.id === "close_1");
  check("平倉畫面：文件的例子（已實現 −1,500 元、淨成本 80 點、損益兩平 180）", a.realized_pts === -30 && a.net_basis_pts === 80 && t.includes("−1,500") && t.includes("80.0 點") && t.includes("上 180（"), t);
  check("平倉畫面：被選中的動作顯示它的情境表", t.includes("只平掉 賣權 100") && t.includes("價格 × 時間，IV 不變") && t.includes("價格 × IV 平移") && t.includes("留下部位：Delta"), t);
  const ca = r.actions.find((x) => x.id === "close_1");
  check("平倉畫面：被選中的動作顯示微台對沖建議、全部平掉沒有", ca.hedge !== null && t.includes("微台對沖：" + O.hedgeText(ca.hedge)) && !html(e(P.CloseResultView, { r, sel: "all", onSelect: () => {} })).includes("微台對沖："), t.slice(-600));
  const tAll = html(e(P.CloseResultView, { r, sel: "all", onSelect: () => {} }));
  check("平倉畫面：全部平掉＝損益固定、沒有情境表", tAll.includes("全部平掉後損益就固定了") && !tAll.includes("價格 × 時間"), tAll);
  const tUnknown = html(e(P.CloseResultView, { r, sel: "nope", onSelect: () => {} }));
  check("平倉畫面：選到不存在的動作退回「全部持有」", tUnknown.includes("價格 × 時間，IV 不變") && !BAD.test(tUnknown), tUnknown.match(BAD));
  const raw1 = renderToStaticMarkup(e(P.CloseResultView, { r: fx("close_live"), sel: "hold", onSelect: () => {} }));
  const hold = fx("close_live").actions[0];
  check("平倉畫面：損益兩平一腳一行（不擠成一長串）", hold.breakeven.length === 2 && hold.breakeven.every((b) => raw1.includes("<div>" + O.beText(b).replace(/&/g, "&amp;") + "</div>")), raw1.slice(raw1.indexOf("min-w-[15rem]"), raw1.indexOf("min-w-[15rem]") + 400));
  check("平倉畫面：淨成本的說明與目前值倍數", t.includes("淨成本＝全部買進成本 − 已收回的錢") && t.includes("要 1.00 倍"), t);
  check("平倉畫面：沒有 undefined／NaN／null", !BAD.test(t), t.match(BAD));
  const tw = html(e(P.CloseResultView, { r: { ...r, warnings: ["測試警告 A", "測試警告 B"] }, sel: "hold", onSelect: () => {} }));
  check("平倉畫面：警告用 ⚠ 顯示", tw.includes("⚠ 測試警告 A") && tw.includes("⚠ 測試警告 B") && !t.includes("⚠"), tw.slice(-300));
  const live2 = fx("close_live");
  const tl = html(e(P.CloseResultView, { r: live2, sel: "hold", onSelect: () => {} }));
  check("平倉畫面（即時鏈）：沒有壞值、損益兩平有顯示", !BAD.test(tl) && live2.actions[0].breakeven.every((b) => tl.includes(O.beText(b))) && tl.includes("即時報價"), tl.match(BAD));
}

// ══ 容器（hooks 替身）═══════════════════════════════════════════
const NAMES = ["mode", "src", "form", "expiries", "ctx", "calc", "calcErr", "busy", "rows", "closeMove", "closeHold", "close", "closeErr", "sel"];
function render(forced = {}) {
  const sets = {};
  const effects = [];
  let n = 0;
  React.useState = (init) => {
    const i = n++; const name = NAMES[i];
    const v = Object.prototype.hasOwnProperty.call(forced, name) ? forced[name] : init;
    return [v, (x) => { sets[name] = typeof x === "function" ? x(v) : x; }];
  };
  React.useEffect = (fn) => { effects.push(fn); };
  React.useCallback = (f) => f;
  const tree = Comp();
  if (n !== NAMES.length) throw new Error(`useState 數量 ${n} ≠ 測試裡的 ${NAMES.length}：元件的 state 增減了，請同步更新 NAMES`);
  return { tree, sets, effects };
}
function find(node, pred, out = []) {
  if (Array.isArray(node)) node.forEach((c) => find(c, pred, out));
  else if (node && typeof node === "object" && node.props) { if (pred(node)) out.push(node); find(node.props.children, pred, out); }
  return out;
}
const text = (node) => (Array.isArray(node) ? node.map(text).join("") : typeof node === "string" || typeof node === "number" ? String(node) : node && node.props ? text(node.props.children) : "");
const buttons = (tree) => find(tree, (x) => x.type === "button");
const button = (tree, label) => buttons(tree).find((b) => text(b.props.children).trim().startsWith(label));
const seg = (tree, label) => find(tree, (x) => Array.isArray(x.props.options) && x.props.options.some((o) => o.label === label))[0];
const segPick = (tree, label) => { const s = seg(tree, label); s.props.onChange(s.props.options.find((o) => o.label === label).id); };
const field = (tree, label) => find(tree, (x) => x.props.label === label)[0];
const control = (tree, label) => find(field(tree, label).props.children, (x) => x.type === "input" || x.type === "select")[0];
const tick = () => new Promise((r) => setTimeout(r, 5));
const EXP = ["202610", "202611"];

(async () => {
  {
    const { tree } = render({ expiries: EXP, src: live });
    const t = text(tree);
    check("容器：初始畫面有各個控制項", t.includes("選擇權試算（買方）") && ["資料來源", "到期月", "結構", "預期波動（點，選填）", "組數", "預算（元，選填）", "持有時間（小時）"].every((x) => !!field(tree, x)) && !!button(tree, "試算"), t);
    check("容器：到期月選單有各月份、跨式沒有「履約價怎麼選」", find(control(tree, "到期月").props.children, (x) => x.type === "option").length === 2 && !field(tree, "履約價怎麼選"));
    check("容器：沒有結果就不顯示結果", find(tree, (x) => x.type === P.CalcResultView).length === 0);
    check("容器：結構四個選項", seg(tree, "買跨式").props.options.map((o) => o.label).join() === "買跨式,買勒式,買 Call,買 Put");
  }
  {
    const { tree } = render({ expiries: EXP, src: live, form: { ...O.DEFAULT_FORM, kind: "strangle" } });
    check("容器：勒式多了「履約價怎麼選」與 Delta 欄位", !!field(tree, "履約價怎麼選") && !!field(tree, "兩翼的 |Delta|") && !field(tree, "距離＝幾倍的預期波動"));
    const mv = render({ expiries: EXP, src: live, form: { ...O.DEFAULT_FORM, kind: "call", select: "move" } }).tree;
    check("容器：單向＋預期波動點數 → 距離倍數欄位", !!field(mv, "距離＝幾倍的預期波動") && !field(mv, "目標 |Delta|"));
  }
  {
    const { tree } = render({ expiries: EXP, src: man });
    check("容器：手動模式有遠期價、ATM IV、距到期，沒有到期月", !!field(tree, "遠期價（點）") && !!field(tree, "ATM IV（%）") && !!field(tree, "距到期（天）") && !field(tree, "到期月"));
  }
  {
    const r1 = render({ expiries: EXP, src: live });
    segPick(r1.tree, "買勒式");
    check("容器：點「買勒式」→ 結構換掉、delta 變成 0.25", r1.sets.form.kind === "strangle" && r1.sets.form.delta === "0.25", J(r1.sets.form));
    const r2 = render({ expiries: EXP, src: live });
    segPick(r2.tree, "買 Call");
    check("容器：點「買 Call」→ delta 變成 0.40", r2.sets.form.kind === "call" && r2.sets.form.delta === "0.40", J(r2.sets.form));
    const r3 = render({ expiries: EXP, src: live });
    segPick(r3.tree, "手動（休市也能用）");
    check("容器：切到手動模式", r3.sets.src.manual === true);
    const r4 = render({ expiries: EXP, src: live });
    control(r4.tree, "到期月").props.onChange({ target: { value: "202611" } });
    check("容器：換到期月", r4.sets.src.deliveryMonth === "202611");
    const r5 = render({ expiries: EXP, src: live });
    segPick(r5.tree, "平倉試算");
    check("容器：切到「平倉試算」", r5.sets.mode === "close");
  }
  {
    calls.length = 0; impl = {};
    const r = render({ expiries: EXP, src: { ...live, deliveryMonth: "" } });
    await button(r.tree, "試算").props.onClick();
    check("容器：沒選到期月 → 顯示錯誤、不呼叫後端", /到期月/.test(r.sets.calcErr) && calls.length === 0, J(r.sets));
  }
  {
    calls.length = 0; impl = { calc: () => Promise.resolve(fx("calc_straddle")) };
    const r = render({ expiries: EXP, src: live, form: { ...O.DEFAULT_FORM, kind: "strangle", select: "move", k: "0.6", movePts: "700", lots: "2" } });
    await button(r.tree, "試算").props.onClick();
    await tick();
    const body = calls.find((c) => c[0] === "calc")[1];
    check("容器：按「試算」送出正確的請求", body.kind === "strangle" && body.select === "move" && body.k === 0.6 && body.move_pts === 700 && body.lots === 2 && body.delivery_month === "202610", J(body));
    check("容器：收到結果存起來、錯誤清空、忙碌結束", J(r.sets.calc) === J(fx("calc_straddle")) && r.sets.calcErr === "" && r.sets.busy === false, J(Object.keys(r.sets)));
  }
  {
    calls.length = 0; impl = { calc: () => Promise.reject(new Error('422 — {"detail":"鏈上沒有任何報價（若是休市，可以改用手動模式）"}')) };
    const r = render({ expiries: EXP, src: live });
    await button(r.tree, "試算").props.onClick();
    await tick();
    check("容器：後端錯誤 → 顯示後端給的那句說明（不是整段 JSON）", r.sets.calcErr === "鏈上沒有任何報價（若是休市，可以改用手動模式）" && r.sets.busy === false, J(r.sets));
  }
  {
    const r = render({ expiries: EXP, src: live, calc: fx("calc_straddle"), busy: true });
    check("容器：忙碌時按鈕停用並顯示「試算中」", button(r.tree, "試算中").props.disabled === true);
    const r2 = render({ expiries: EXP, src: live, calc: fx("calc_straddle") });
    const view = find(r2.tree, (x) => x.type === P.CalcResultView)[0];
    view.props.onToClose();
    check("容器：「帶到平倉試算」→ 各腳帶過去、切到平倉分頁、清掉舊結果", J(r2.sets.rows) === J(O.rowsFromCalc(fx("calc_straddle"))) && r2.sets.mode === "close" && r2.sets.close === null && r2.sets.closeErr === "", J(r2.sets));
  }
  {
    const rows = [{ right: "C", strike: "100", qty: "1", entry: "50", bid: "78", ask: "82" }, { right: "P", strike: "100", qty: "1", entry: "50", bid: "20", ask: "20" }];
    const r = render({ mode: "close", expiries: EXP, src: man, rows });
    const t = text(r.tree);
    check("容器：平倉分頁每一腳都有輸入欄位、有新增與試算按鈕", ["買賣權", "履約價", "口數", "買進價（點）", "目前買價", "目前賣價"].every((x) => find(r.tree, (y) => y.props.label === x).length === 2)
      && t.includes("試算平倉") && t.includes("＋ 新增一腳") && find(r.tree, (x) => x.type === "select").length === 2, t);
    calls.length = 0; impl = { close: () => Promise.resolve(fx("close_doc")) };
    await button(r.tree, "試算平倉").props.onClick();
    await tick();
    const body = calls.find((c) => c[0] === "close")[1];
    check("容器：「試算平倉」送出正確的請求", body.legs.length === 2 && body.legs[0].bid === 78 && body.legs[0].ask === 82 && body.legs[1].entry === 50 && body.hold_hours === 24 && body.manual.F === 49300, J(body));
    check("容器：收到平倉結果、預設選中「只平掉虧損腳」", J(r.sets.close) === J(fx("close_doc")) && r.sets.sel === "close_1" && r.sets.closeErr === "", J(r.sets));
    const r2 = render({ mode: "close", expiries: EXP, src: man, rows });
    button(r2.tree, "＋ 新增一腳").props.onClick();
    check("容器：新增一腳", r2.sets.rows.length === 3 && r2.sets.rows[2].qty === "1" && r2.sets.rows[2].entry === "");
    const dels = buttons(r2.tree).filter((b) => text(b.props.children).trim() === "刪除");
    dels[0].props.onClick();
    check("容器：刪除一腳", dels.length === 2 && r2.sets.rows.length === 1 && r2.sets.rows[0].right === "P");
    const one = render({ mode: "close", expiries: EXP, src: man, rows: [rows[0]] });
    check("容器：只剩一腳時不能刪", buttons(one.tree).filter((b) => text(b.props.children).trim() === "刪除")[0].props.disabled === true);
    const bad = render({ mode: "close", expiries: EXP, src: man, rows: [{ ...rows[0], strike: "" }] });
    calls.length = 0;
    await button(bad.tree, "試算平倉").props.onClick();
    check("容器：平倉輸入不對 → 顯示錯誤、不呼叫後端", /第 1 腳：履約價/.test(bad.sets.closeErr) && calls.length === 0);
    const withRes = render({ mode: "close", expiries: EXP, src: man, rows, close: fx("close_doc"), sel: "close_1" });
    const cv = find(withRes.tree, (x) => x.type === P.CloseResultView)[0];
    cv.props.onSelect("hold");
    check("容器：點動作列切換選中的動作", cv.props.sel === "close_1" && withRes.sets.sel === "hold");
  }
  {
    calls.length = 0; impl = { expiries: () => Promise.resolve(EXP), chain: () => Promise.resolve({ F: 49283, days: 8.8, atm_iv_pct: 20.3, em_sigma_pts: 1551.2, straddle_pts: 1237.7 }) };
    const r = render({ src: { ...live, deliveryMonth: "202610" } });
    r.effects.forEach((fn) => fn());
    await tick();
    check("容器：載入時取到期月清單並選好預設月份", calls.some((c) => c[0] === "expiries") && J(r.sets.expiries) === J(EXP) && typeof r.sets.src === "object" && EXP.includes(r.sets.src.deliveryMonth), J(r.sets));
    check("容器：選了到期月就查一次鏈摘要", J(calls.filter((c) => c[0] === "chain")) === J([["chain", "202610"]]) && r.sets.ctx.em_sigma_pts === 1551.2, J(calls));
    const m = render({ src: man });
    calls.length = 0;
    m.effects.forEach((fn) => fn());
    await tick();
    check("容器：手動模式不查鏈、清掉摘要", !calls.some((c) => c[0] === "chain") && m.sets.ctx === null);
    const hint = render({ expiries: EXP, src: live, ctx: { F: 49283, days: 8.8, atm_iv_pct: 20.3, em_sigma_pts: 1551.2, straddle_pts: 1237.7 } });
    check("容器：有鏈摘要時顯示遠期價、ATM IV 與市場隱含 1σ", field(hint.tree, "到期月").props.hint === "遠期 49,283.0 · ATM IV 20.3% · 距到期 8.8 天" && field(hint.tree, "預期波動（點，選填）").props.hint.includes("1,551"), text(field(hint.tree, "到期月").props.hint));
  }
  console.log(fail ? `\n${fail} 項失敗` : "\n全部通過");
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(1); });
