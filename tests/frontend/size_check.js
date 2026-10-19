// 〈口數提示〉：lib/size.ts 的純函式、SizeBody（靜態渲染）、SizeHint 容器（載入／防抖／輪詢／清理）。hooks 換成替身。
// 畫面吃的資料由 tests/test_frontend_size.py 用 core/position_size.py 現算（環境變數 SZ_FIXTURES 指到目錄）。
// 用法：SZ_FIXTURES=<dir> node tests/frontend/size_check.js（需要 frontend/node_modules）
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
if (!process.env.SZ_FIXTURES) { console.error("需要環境變數 SZ_FIXTURES（由 tests/test_frontend_size.py 產生）"); process.exit(2); }
const fx = JSON.parse(fs.readFileSync(path.join(process.env.SZ_FIXTURES, "fixtures.json"), "utf8"));
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "size-check-")));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });
const transpile = (src, out) => {
  fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText);
  return require(out);
};
const map = { "@/lib/size": path.join(TMP, "size.js"), "@/lib/api": path.join(TMP, "api.js") };
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) { return map[req] || orig.call(this, req, ...r); };
const L = transpile(path.join(FRONT, "lib", "size.ts"), path.join(TMP, "size.js"));
const apiMod = transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "api.js"));
const C = transpile(path.join(FRONT, "components", "SizeHint.tsx"), path.join(TMP, "hint.js"));
const e = React.createElement;
let fail = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + String(extra).slice(0, 900))); if (!ok) fail++; };
const J = (x) => JSON.stringify(x);
const strip = (s) => s.replace(/<\/?(div|button)\b[^>]*>/g, " ").replace(/<[^>]+>/g, "").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const text = (el) => strip(renderToStaticMarkup(el));
const BAD = /undefined|NaN|\bnull\b|Infinity|\[object/;

// ══ 純函式 ═══════════════════════════════════════════════════════
check("money／pctText：千分位、百分比去尾零、缺值破折號", L.money(1234.6) === "1,235" && L.money(null) === "—" && L.pctText(0.6) === "0.6%" && L.pctText(1) === "1%" && L.pctText(2.423) === "2.42%" && L.pctText(0) === "0%" && L.pctText(null) === "—" && L.pctText(NaN) === "—");
const one = fx.one, two = fx.over, zero = fx.zero;
check("後端現算的資料形狀", one.lots === 1 && one.binding === "single" && one.risk_per_lot === 320 && two.qty_over === true && zero.lots === 0 && zero.reason === "below_one" && fx.nostop.lots === null, J(one).slice(0, 300));
check("lotsLine：最多幾口與哪個上限在管", L.lotsLine(one) === "最多 1 口（單筆風險上限 1%）" && L.lotsLine(fx.total) === "最多 2 口（總風險上限 3% 的餘額）" && L.lotsLine(fx.cap) === "最多 3 口（單筆口數上限 3 口）", [L.lotsLine(one), L.lotsLine(fx.total), L.lotsLine(fx.cap)]);
check("lotsLine：不足 1 口時說要什麼才夠；不給口數時寫原因", L.lotsLine(zero).startsWith("算出不足 1 口（單筆風險上限 1%）：要 1 口需停損縮到 ") && L.lotsLine(zero).includes(`${zero.max_stop_for_one} 點以內`) && L.lotsLine(zero).includes(`權益到 ${L.money(zero.need_equity_single)} 元`)
  && L.lotsLine(fx.nostop) === "填停損點數後，這裡會顯示依風險上限最多幾口" && L.lotsLine(fx.noequity) === "權益還沒取得，無法算口數" && L.lotsLine(fx.stale).includes("已過期"), [L.lotsLine(zero), L.lotsLine(fx.noequity)]);
check("perLotLine／qtyLine：含緩衝的每口風險；超過上限要點名是哪一個", L.perLotLine(one) === "停損 30 點＋緩衝 2 點，每口約 320 元（0.62% 權益）" && L.perLotLine(fx.nostop) === ""
  && L.qtyLine(two) === "你填 2 口：風險約 640 元（1.23% 權益），超過上限（單筆風險 1%）" && L.qtyLine(one) === "" && L.qtyLine(fx.in_limit) === "你填 1 口：風險約 320 元（0.62% 權益），在上限內", [L.perLotLine(one), L.qtyLine(two), L.qtyLine(fx.in_limit)]);
check("openRiskLine／warnLines：已佔用風險；沒登記停損與選擇權要警告", L.openRiskLine(fx.total).startsWith("已佔用風險 1,260 元（2.42% 權益，上限 3%）") && L.openRiskLine(fx.total).endsWith("3 口有登記停損") && L.openRiskLine(one) === "已佔用風險 0 元（0% 權益，上限 3%）"
  && L.warnLines(fx.total).length === 2 && L.warnLines(fx.total)[0].startsWith("另有 1 口期貨沒有登記停損（TMFJ6）") && L.warnLines(fx.total)[0].includes("只是下限") && L.warnLines(fx.total)[1] === "選擇權部位 1 口不計入（買方最大虧損＝權利金）" && L.warnLines(one).length === 0, [L.openRiskLine(fx.total), L.warnLines(fx.total)]);
check("lotsLine：預算小到連 0 點停損都放不下時，不寫『停損縮到 0 點』", fx.tiny.lots === 0 && fx.tiny.max_stop_for_one === 0 && !L.lotsLine(fx.tiny).includes("停損縮到") && L.lotsLine(fx.tiny).includes("或權益到"), L.lotsLine(fx.tiny));
check("qtyLine：沒有口數上限（lots 是 null）就不寫", L.qtyLine({ ...one, lots: null, qty: 2, qty_risk_twd: 640, qty_risk_pct: 1.2 }) === "" && L.qtyLine({ ...one, qty: null }) === "");
check("friendlySizeError", L.friendlySizeError("Not Found") === "後端還沒有這個 API（要重啟後端才有）" && L.friendlySizeError("502 壞了") === "502 壞了");

// ══ SizeBody ═════════════════════════════════════════════════════
for (const [name, s] of Object.entries(fx)) {
  const raw = renderToStaticMarkup(e(C.SizeBody, { s })), tx = strip(raw);
  check(`SizeBody(${name})：沒有 undefined／NaN`, !BAD.test(tx), tx.match(BAD));
}
{
  const raw = renderToStaticMarkup(e(C.SizeBody, { s: two })), tx = strip(raw);
  check("SizeBody：超過上限時標警告色與 ⚠", tx.includes("⚠ 你填 2 口") && /text-\[#ffc107\][^>]*>⚠/.test(raw), tx);
  const raw1 = renderToStaticMarkup(e(C.SizeBody, { s: fx.in_limit }));
  check("SizeBody：在上限內不標警告", !raw1.includes("⚠"), strip(raw1));
  const raw0 = renderToStaticMarkup(e(C.SizeBody, { s: zero }));
  check("SizeBody：不足 1 口標紅、不給口數用灰色", /text-\[#ff1744\][^>]*>算出不足 1 口/.test(raw0) && /text-\[#7070a0\][^>]*>填停損點數後/.test(renderToStaticMarkup(e(C.SizeBody, { s: fx.nostop }))), strip(raw0));
  check("SizeBody：沒有口數時不寫已佔用風險", !strip(renderToStaticMarkup(e(C.SizeBody, { s: fx.noequity }))).includes("已佔用風險") && strip(renderToStaticMarkup(e(C.SizeBody, { s: fx.total }))).includes("已佔用風險 1,260 元"));
}

// ══ 容器：hooks 換成替身 ═════════════════════════════════════════
const S = { data: 0, err: 1 };
function harness(initial = {}) {
  const st = [], effects = []; let idx = 0;
  React.useState = (init) => { const i = idx++; if (!(i in st)) st[i] = i in initial ? initial[i] : init; return [st[i], (v) => { st[i] = typeof v === "function" ? v(st[i]) : v; }]; };
  React.useEffect = (f, deps) => { effects.push([f, deps]); };
  return { st, effects, render: (props) => { idx = 0; effects.length = 0; return renderToStaticMarkup(e(C.default, { contract: "TMF", stopLoss: "30", quantity: 1, ...props })); } };
}
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  {
    const h = harness({ [S.data]: null });
    const tx = strip(h.render({ stopLoss: "" }));
    check("容器：沒填停損只提示怎麼用，不問後端", tx.includes("口數提示（只提醒，不擋單）") && tx.includes("填停損點數後，這裡會顯示依風險上限最多幾口") && tx.includes("假設停損會成交"), tx);
    const calls = []; apiMod.api.risk.size = async (...a) => { calls.push(a); return one; };
    const r = h.effects[0][0](); await tick();
    check("容器：沒有停損時不呼叫後端", calls.length === 0 && r === undefined && h.st[S.data] === null);
    const realTO0 = global.setTimeout, fired = []; global.setTimeout = (fn) => { fired.push(fn); return 1; };
    try { for (const bad of ["abc", "0", "-5", "NaN", "Infinity"]) { const hb = harness(); hb.render({ stopLoss: bad }); const r2 = hb.effects[0][0](); if (r2) r2(); } } finally { global.setTimeout = realTO0; }
    for (const f of fired) await f();
    await tick();
    check("容器：停損不是正數也不呼叫後端（也沒有排任何計時器）", calls.length === 0 && fired.length === 0, J(calls));
    {
      const hc = harness({ [S.data]: one, [S.err]: "舊的錯" }); hc.render({ stopLoss: "" }); hc.effects[0][0]();
      check("容器：停損清空時，舊的口數與錯誤一併清掉", hc.st[S.data] === null && hc.st[S.err] === null);
    }
  }
  {
    const h = harness({ [S.data]: one });
    const tx = strip(h.render());
    check("容器：有資料就畫出口數與免責說明", tx.includes("最多 1 口（單筆風險上限 1%）") && tx.includes("跳空或極端行情可能更差") && !BAD.test(tx), tx);
    check("容器：依賴是 有沒有停損／合約／停損值／口數", J(h.effects[0][1]) === J([true, "TMF", 30, 1]), J(h.effects[0][1]));
  }
  {
    const calls = []; apiMod.api.risk.size = async (...a) => { calls.push(a); return two; };
    const h = harness(); h.render({ contract: "MXF", stopLoss: "25", quantity: 3 });
    const realSet = global.setInterval, realClear = global.clearInterval, realTO = global.setTimeout, realCT = global.clearTimeout, ivs = [], tos = [], cleared = [];
    global.setInterval = (f, ms) => { ivs.push([f, ms]); return 77; }; global.clearInterval = (id) => { cleared.push(["i", id]); };
    global.setTimeout = (f, ms) => { tos.push([f, ms]); return 55; }; global.clearTimeout = (id) => { cleared.push(["t", id]); };
    let cleanup;
    try {
      cleanup = h.effects[0][0]();
      check("容器：先等 400 毫秒（打字防抖）再問、之後每 15 秒問一次", tos.length === 1 && tos[0][1] === 400 && ivs.length === 1 && ivs[0][1] === 15000 && calls.length === 0, J({ tos: tos.length, ivs: ivs.length }));
      await tos[0][0](); await tick();
      check("容器：用目前的合約、停損與口數問後端", J(calls) === J([["MXF", 25, 3]]) && h.st[S.data] === two && h.st[S.err] === null, J(calls));
      cleanup();                                                                         // 在還原替身之前呼叫
    } finally { global.setInterval = realSet; global.clearInterval = realClear; global.setTimeout = realTO; global.clearTimeout = realCT; }
    check("容器：離開時清掉計時器", J(cleared) === J([["t", 55], ["i", 77]]), J(cleared));
    h.st[S.data] = null;
    await tos[0][0](); await tick();                                                   // 清理之後才回來的載入：被 cancelled 擋下
    check("容器：清理後晚到的回應被丟掉", h.st[S.data] === null);
  }
  {
    const calls = []; apiMod.api.risk.size = async (...a) => { calls.push(a); return one; };
    const h = harness(); h.render({ quantity: 0 });
    const realTO = global.setTimeout; let f; global.setTimeout = (fn) => { f = fn; return 1; };
    try { h.effects[0][0](); } finally { global.setTimeout = realTO; }
    await f(); await tick();
    {
      apiMod.api.risk.size = async () => one;
      const hr = harness({ [S.err]: "上次失敗" }); hr.render();
      let f2; const rt = global.setTimeout; global.setTimeout = (fn) => { f2 = fn; return 1; };
      try { hr.effects[0][0](); } finally { global.setTimeout = rt; }
      await f2(); await tick();
      check("容器：成功之後把上次的錯誤清掉", hr.st[S.err] === null && hr.st[S.data] === one);
    }
    check("容器：口數 0 或沒填就不帶 qty", J(calls) === J([["TMF", 30, undefined]]), J(calls));
  }
  {
    apiMod.api.risk.size = async () => { throw new Error('404 — {"detail":"Not Found"}'); };
    const h = harness(); h.render();
    const realTO = global.setTimeout; let f; global.setTimeout = (fn) => { f = fn; return 1; };
    try { h.effects[0][0](); } finally { global.setTimeout = realTO; }
    await f(); await tick();
    check("容器：後端是舊版（404）顯示白話說明", h.st[S.err] === "後端還沒有這個 API（要重啟後端才有）", h.st[S.err]);
    const tx = strip(harness({ [S.err]: "後端還沒有這個 API（要重啟後端才有）" }).render());
    check("容器：沒載入過就失敗只顯示錯誤；載入過再失敗則保留舊資料並標更新失敗", tx.includes("後端還沒有這個 API") && !tx.includes("最多")
      && strip(harness({ [S.data]: one, [S.err]: "502 暫時連不上" }).render()).includes("最多 1 口") && strip(harness({ [S.data]: one, [S.err]: "502 暫時連不上" }).render()).includes("更新失敗：502 暫時連不上"), tx);
  }
  console.log(fail ? `\n${fail} 項失敗` : "\n全部通過");
  process.exit(fail ? 1 : 0);
})();
