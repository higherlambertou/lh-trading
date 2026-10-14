// 〈建議停損停利〉卡片：純函式、列表、容器（載入／收合／錯誤／輪詢／帶入）。hooks 換成替身。
// 畫面吃的資料由 tests/test_frontend_stop_suggest.py 用 core/stop_suggest.py 現算（環境變數 SS_FIXTURES 指到目錄）。
// 用法：SS_FIXTURES=<dir> node tests/frontend/stop_suggest_check.js（需要 frontend/node_modules）
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
if (!process.env.SS_FIXTURES) { console.error("需要環境變數 SS_FIXTURES（由 tests/test_frontend_stop_suggest.py 產生）"); process.exit(2); }
const fx = JSON.parse(fs.readFileSync(path.join(process.env.SS_FIXTURES, "fixtures.json"), "utf8"));
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "stop-suggest-check-")));   // realpath：macOS 的 /var 是 /private/var 的連結，不然同一個模組會被載入兩份
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });
const transpile = (src, out) => {
  fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText);
  return require(out);
};
const map = { "@/lib/stopSuggest": path.join(TMP, "stopSuggest.js"), "@/lib/api": path.join(TMP, "api.js") };
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) { return map[req] || orig.call(this, req, ...r); };
const L = transpile(path.join(FRONT, "lib", "stopSuggest.ts"), path.join(TMP, "stopSuggest.js"));
const apiMod = transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "api.js"));
const C = transpile(path.join(FRONT, "components", "StopSuggestCard.tsx"), path.join(TMP, "card.js"));
const Card = C.default, e = React.createElement;
let fail = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + String(extra).slice(0, 900))); if (!ok) fail++; };
const J = (x) => JSON.stringify(x);
const strip = (s) => s.replace(/<\/?(div|button|span)\b[^>]*>/g, (m) => (/^<\/?div|^<\/?button/.test(m) ? " " : "")).replace(/<[^>]+>/g, "").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ");
const text = (el) => strip(renderToStaticMarkup(el));
const BAD = /undefined|NaN|\bnull\b|Infinity|\[object/;

// ══ 純函式 ═══════════════════════════════════════════════════════
check("pct：四捨五入、缺值是 —", L.pct(0.6024) === "60%" && L.pct(0.6024, 1) === "60.2%" && L.pct(0) === "0%" && L.pct(null) === "—" && L.pct(undefined) === "—" && L.pct(NaN) === "—" && L.pct(1) === "100%");
check("money：千分位、缺值是 —", L.money(52318) === "52,318" && L.money(1234.6) === "1,235" && L.money(0) === "0" && L.money(null) === "—" && L.money(Infinity) === "—");
check("needPts：機率差換成『個百分點』", L.needPts(0.0488) === "4.9" && L.needPts(0.0242) === "2.4" && L.needPts(null) === "—");
check("lotsText：最多幾口、1 口就超過", L.lotsText({ max_lots: 2 }, 1) === "1% 風險最多 2 口" && L.lotsText({ max_lots: 0 }, 1) === "1 口就超過 1% 風險" && L.lotsText({ max_lots: null }, 1) === "" && L.lotsText({ max_lots: 1 }, 2) === "2% 風險最多 1 口");
check("sessionText／calText／friendlySuggestError", L.sessionText("night") === "夜盤" && L.sessionText("day") === "日盤"
  && L.calText({ sessions: 1609, n: 82028, few: false }, "day") === "1609 個日盤時段、82,028 筆" && L.calText({ sessions: 39, n: 6183, few: true }, "night") === "39 個夜盤時段、6,183 筆（樣本少）" && L.calText(null, "day") === ""
  && L.friendlySuggestError("Not Found") === "後端還沒有這個 API（要重啟後端才有）" && L.friendlySuggestError("502 壞了 Not Found 在別處") === "502 壞了 Not Found 在別處" && L.HORIZONS.join() === "5,15,30,60");

// ══ 後端現算的資料 ═══════════════════════════════════════════════
const d = fx.day, rows = Object.fromEntries(d.rows.map((r) => [r.name, r]));
check("後端現算的資料形狀（三組、校準表有值）", d.rows.length === 3 && d.calibrated && d.cal.sessions > 1000 && rows["平衡"].touch_sl > 0.1 && rows["平衡"].within && d.sigma === 17.6 && d.noise > 60, J(d).slice(0, 400));

// ══ 列表 ═══════════════════════════════════════════════════════
const applied = [];
const t = text(e(C.SuggestRows, { data: d, onApply: (sl, tp) => applied.push([sl, tp]) }));
check("列表：三種型態的名稱、停利停損點數", ["高勝率型", "平衡", "高賺賠比型"].every((n) => t.includes(n)) && d.rows.every((r) => t.includes(`停利 ${r.tp}`) && t.includes(`停損 ${r.sl}`)) && !BAD.test(t), t);
check("列表：沒技巧勝率、停損被掃到機率、單口停損與占權益、風險限額", d.rows.every((r) => t.includes(`沒有技巧時的勝率 ${L.pct(r.p0)}`) && t.includes(`停損被雜訊掃到 ${L.pct(r.touch_sl)}`) && t.includes(`單口停損 −${L.money(r.loss_twd)} 元（${r.loss_pct}%）`) && t.includes(L.lotsText(r, d.risk_pct))), t);
check("列表：T 分鐘內停利先到／停損先到／都沒到", d.rows.every((r) => t.includes(`${d.horizon} 分鐘內（沒有技巧時）：停利先到 ${L.pct(r.within.tp)}／停損先到 ${L.pct(r.within.sl)}／都沒到 ${L.pct(r.within.none)}`)), t);
check("列表：高勝率型勝率最高、高賺賠比型最低（勝率換賺賠比）", rows["高勝率型"].p0 > rows["平衡"].p0 && rows["平衡"].p0 > rows["高賺賠比型"].p0 && rows["高勝率型"].p0 > 0.75 && rows["高賺賠比型"].p0 < 0.45);
check("列表：1 口就超過風險限額時要說出來", d.rows.some((r) => r.max_lots === 0) && t.includes("1 口就超過 1% 風險"), t);
const tree = C.SuggestRows({ data: d, onApply: (sl, tp) => applied.push([sl, tp]) });
const buttons = []; (function walk(n) { if (!n || typeof n !== "object") return; if (Array.isArray(n)) return n.forEach(walk); if (n.type === "button") buttons.push(n); walk(n.props && n.props.children); })(tree);
buttons.forEach((b) => b.props.onClick());
check("列表：三個「帶入」按鈕，按下去把停損、停利（字串）交給呼叫端", buttons.length === 3 && J(applied) === J(d.rows.map((r) => [String(r.sl), String(r.tp)])), J(applied));
check("列表：沒給 onApply 就沒有按鈕", !renderToStaticMarkup(e(C.SuggestRows, { data: d })).includes("帶入"));
const tn = text(e(C.SuggestRows, { data: fx.noequity }));
check("列表：沒有權益時不寫占權益與口數、仍寫單口虧多少", !tn.includes("%）") && !tn.includes("風險最多") && !tn.includes("就超過") && tn.includes("單口停損 −") && !BAD.test(tn), tn);
const tu = text(e(C.SuggestRows, { data: fx.unstable }));
check("列表：不穩的格子標「不穩」（高勝率型的停損掃到機率、平衡的 T 分鐘內比例，各一處）", (tu.match(/（不穩）/g) || []).length === 2 && /高勝率型.*?被雜訊掃到 6%（不穩）/.test(tu) && /平衡.*?都沒到 \d+%（不穩）/.test(tu) && !/高賺賠比型[^]*（不穩）/.test(tu), tu);
const tf = text(e(C.SuggestRows, { data: fx.far }));
check("列表：停利比今天預期剩餘振幅上緣還遠時標示", tf.includes("停利比今天預期的剩餘振幅上緣還遠") && !text(e(C.SuggestRows, { data: fx.day })).includes("還遠"), tf);
const tc = text(e(C.SuggestRows, { data: fx.uncal }));
check("列表：沒有校準表時不寫掃到機率與 T 分鐘內的比例", !tc.includes("被雜訊掃到") && !tc.includes("分鐘內（沒有技巧時）") && tc.includes("沒有技巧時的勝率"), tc);

// ══ 容器：hooks 換成替身 ═════════════════════════════════════════
const S = { open: 0, horizon: 1, data: 2, err: 3 };
function harness(initial = {}) {
  const st = [], cbs = [], effects = []; let idx = 0;
  React.useState = (init) => { const i = idx++; if (!(i in st)) st[i] = i in initial ? initial[i] : init; return [st[i], (v) => { st[i] = typeof v === "function" ? v(st[i]) : v; }]; };
  React.useCallback = (f) => { cbs.push(f); return f; };
  React.useEffect = (f) => { effects.push(f); };
  const reset = () => { idx = 0; cbs.length = 0; effects.length = 0; };
  return { st, cbs, effects, render: (props = {}) => { reset(); return renderToStaticMarkup(e(Card, { contract: "TMF", onApply: () => {}, ...props })); } };
}
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  {
    const h = harness({ [S.open]: true, [S.horizon]: 15, [S.data]: d, [S.err]: null });
    const raw = h.render(), tx = strip(raw);
    check("卡片：標題、持有時間按鈕（目前的有標示）、σ 與雜訊範圍", tx.includes("建議停損停利（依波動）") && tx.includes("持有約") && ["5", "15", "30", "60"].every((x) => raw.includes(`>${x}</button>`))
      && /border-\[#3b82f6\][^>]*>15<\/button>/.test(raw) && !/border-\[#3b82f6\][^>]*>5<\/button>/.test(raw) && tx.includes(`最近 30 分鐘波動 ${d.sigma} 點／分鐘`) && tx.includes(`15 分鐘的雜訊範圍約 ±${d.noise} 點`) && tx.includes("（日盤）"), tx);
    check("卡片：今天的尺度（已走多少、預期還會走多少）", tx.includes("今天已走 90 點，預期之後還會走約 160 點（68% 區間 100～240）"), tx);
    const need = L.needPts(d.rows[1].need);
    check("卡片：成本與要多贏幾個百分點、三種型態一樣", tx.includes(`成本 ${d.cost_pts} 點：要比「沒有技巧」的勝率多贏約 ${need} 個百分點才打平`) && tx.includes("改比例只是把勝率換成賺賠比"), tx);
    check("卡片：勝率高不等於賺錢的提醒、校準資料說明、權益與成本是假設", tx.includes("勝率高 ≠ 賺錢") && tx.includes("期望值＝−成本") && tx.includes(`掃到機率與「都沒到」是實測（${L.calText(d.cal, "day")}）`) && tx.includes("權益 52,318 元、單筆風險上限 1%、成本是假設值"), tx);
    check("卡片：沒有 undefined／NaN", !BAD.test(tx), tx.match(BAD));
  }
  {
    const h = harness({ [S.open]: true, [S.data]: d });                                          // 沒指定持有時間：預設 15 分鐘
    check("卡片：預設持有 15 分鐘", /border-\[#3b82f6\][^>]*>15<\/button>/.test(h.render()) && h.st[S.horizon] === 15);
    const skew = JSON.parse(JSON.stringify(d)); skew.rows[0].need = 0.05; skew.rows[1].need = 0.04; skew.rows[2].need = 0.03;
    const t2 = strip(harness({ [S.open]: true, [S.data]: skew }).render());
    check("卡片：打平門檻取三組裡最高的（保守）", t2.includes("多贏約 5.0 個百分點"), t2);
  }
  {
    const h = harness({ [S.open]: true, [S.horizon]: 5, [S.data]: fx.night });
    const tx = strip(h.render());
    check("夜盤：標示夜盤與樣本少", tx.includes("（夜盤）") && tx.includes("（樣本少）") && tx.includes("5 分鐘的雜訊範圍") && !tx.includes("今天已走"), tx);
  }
  {
    const h = harness({ [S.open]: true, [S.data]: fx.note });
    const tx = strip(h.render());
    check("資料不夠：只寫原因，不寫建議、也不寫成本門檻", tx.includes("成交太少") && tx.includes("12") && !tx.includes("停利 ") && !tx.includes("打平") && !tx.includes("雜訊範圍約") && tx.includes("最近 30 分鐘波動 — 點／分鐘"), tx);
  }
  {
    const h = harness({ [S.open]: true, [S.data]: fx.uncal });
    const tx = strip(h.render());
    check("沒有校準表：說明沒有實測的掃到機率", tx.includes("沒有校準表，沒有實測的掃到機率") && !tx.includes("掃到機率與「都沒到」是實測"), tx);
  }
  {
    const h = harness({ [S.open]: true, [S.err]: "後端還沒有這個 API（要重啟後端才有）" });
    const tx = strip(h.render());
    check("還沒載入過就失敗：只顯示錯誤", tx.includes("後端還沒有這個 API（要重啟後端才有）") && !tx.includes("停利 "), tx);
    const h2 = harness({ [S.open]: true, [S.data]: d, [S.err]: "502 暫時連不上" });
    const t2 = strip(h2.render());
    check("載入過之後更新失敗：舊的建議留著、另外標示更新失敗", t2.includes("停利 ") && t2.includes("更新失敗：502 暫時連不上"), t2);
    const h3 = harness({ [S.open]: true });
    check("還沒載入：只有標題、沒有錯誤也沒有建議", !strip(h3.render()).includes("停利 ") && !strip(h3.render()).includes("更新失敗"));
  }
  {
    const h = harness({ [S.open]: false, [S.data]: d });
    const raw = h.render(), tx = strip(raw);
    check("收合：只剩標題，沒有按鈕組與內容", tx.includes("▸ 建議停損停利（依波動）") && !tx.includes("持有約") && !tx.includes("停利 ") && !tx.includes("勝率高"), tx);
  }
  // 載入與輪詢
  {
    const calls = []; apiMod.api.market.suggest = async (c, hz) => { calls.push([c, hz]); return d; };
    const h = harness({ [S.open]: true, [S.horizon]: 30 }); h.render({ contract: "MXF" });
    const realSet = global.setInterval, realClear = global.clearInterval, ivs = []; let cleared = null;
    global.setInterval = (f, ms) => { ivs.push([f, ms]); return 99; }; global.clearInterval = (id) => { cleared = id; };
    try {
      const cleanup = h.effects[1](); await tick();
      check("載入：用目前的合約與持有時間向後端要建議，每 30 秒再要一次", J(calls) === J([["MXF", 30]]) && ivs.length === 1 && ivs[0][1] === 30000 && h.st[S.data] === d && h.st[S.err] === null, J(calls));
      cleanup();                                                       // 在還原替身之前呼叫
    } finally { global.setInterval = realSet; global.clearInterval = realClear; }
    check("載入：離開時清掉計時器", cleared === 99);
    const h0 = harness({ [S.open]: false }); h0.render(); calls.length = 0;
    const r0 = h0.effects[1](); await tick();
    check("收合時不向後端要資料", calls.length === 0 && r0 === undefined);
  }
  {
    apiMod.api.market.suggest = async () => { throw new Error('404 — {"detail":"Not Found"}'); };
    const h = harness({ [S.open]: true }); h.render();
    await h.cbs[1](); // load
    check("載入失敗：後端是舊版（404）顯示白話說明", h.st[S.err] === "後端還沒有這個 API（要重啟後端才有）" && h.st[S.data] === null, h.st[S.err]);
    apiMod.api.market.suggest = async () => d;
    await h.cbs[1]();
    check("恢復後錯誤消失、資料出現", h.st[S.err] === null && h.st[S.data] === d);
  }
  // 收合記憶（localStorage 可能不存在或會丟例外）
  {
    const store = {}; global.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } };
    const h = harness({ [S.open]: true }); h.render();
    h.cbs[0]();                                                      // toggle
    check("收合：記在 localStorage、狀態翻轉", store.stop_suggest_open === "0" && h.st[S.open] === false, J(store));
    h.render(); h.cbs[0]();
    check("再展開：記成 1", store.stop_suggest_open === "1" && h.st[S.open] === true);
    global.localStorage = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
    const h2 = harness({ [S.open]: true }); h2.render();
    let threw = false; try { h2.cbs[0](); h2.effects[0](); } catch (x) { threw = true; }
    check("localStorage 壞掉（隱私模式）也不會丟例外", !threw && h2.st[S.open] === false);
    delete global.localStorage;
  }
  console.log(fail ? `\n${fail} 項失敗` : "\n全部通過");
  process.exit(fail ? 1 : 0);
})();
