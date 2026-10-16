// 〈進場判斷標記〉卡片：純函式、列、清單、容器（載入／輪詢／點標籤／備註／錯誤）。hooks 換成替身。
// 畫面吃的資料由 tests/test_frontend_entry_label.py 用 core/entry_log.fetch_entries 現算（環境變數 EL_FIXTURES 指到目錄）。
// 用法：EL_FIXTURES=<dir> node tests/frontend/entry_label_check.js（需要 frontend/node_modules）
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
if (!process.env.EL_FIXTURES) { console.error("需要環境變數 EL_FIXTURES（由 tests/test_frontend_entry_label.py 產生）"); process.exit(2); }
const fx = JSON.parse(fs.readFileSync(path.join(process.env.EL_FIXTURES, "fixtures.json"), "utf8"));
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "entry-label-check-")));   // realpath：macOS 的 /var 是 /private/var 的連結，不然同一個模組會被載入兩份
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });
const transpile = (src, out) => {
  fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText);
  return require(out);
};
const map = { "@/lib/entryLabel": path.join(TMP, "entryLabel.js"), "@/lib/api": path.join(TMP, "api.js") };
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) { return map[req] || orig.call(this, req, ...r); };
const L = transpile(path.join(FRONT, "lib", "entryLabel.ts"), path.join(TMP, "entryLabel.js"));
const apiMod = transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "api.js"));
const C = transpile(path.join(FRONT, "components", "EntryLabelCard.tsx"), path.join(TMP, "card.js"));
const Card = C.default, e = React.createElement;
let fail = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + String(extra).slice(0, 900))); if (!ok) fail++; };
const strip = (s) => s.replace(/<[^>]+>/g, " ").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ");
const BAD = /undefined|NaN|\bnull\b|Infinity|\[object/;
const byId = Object.fromEntries(fx.rows.map((r) => [r.trade_id, r]));
const M1 = byId.M1, M2 = byId.M2, M3 = byId.M3, S1 = byId.S1;

// ══ 純函式 ═══════════════════════════════════════════════════════
check("資料前提：後端的方向是 long／short、策略單的缺值原因是 strategy_order", M1.direction === "long" && M2.direction === "short" && S1.label_missing_reason === "strategy_order" && M3.label_missing_reason === "not_entered", JSON.stringify([M1.direction, M2.direction]));
check("manualRows：只留手動單（有標籤或原因是 not_entered）、保持後端的順序、策略單不在", L.manualRows(fx.rows).map((r) => r.trade_id).join() === fx.rows.filter((r) => r.trade_id !== "S1").map((r) => r.trade_id).join(), L.manualRows(fx.rows).map((r) => r.trade_id));
check("manualRows：最多 max 筆、null／空陣列不會丟例外", L.manualRows(fx.rows, 2).length === 2 && L.manualRows(null).length === 0 && L.manualRows([]).length === 0);
check("trendText：四個選項與未知值", L.trendText("multi") === "多" && L.trendText("short") === "空" && L.trendText("range") === "盤整" && L.trendText("none") === "無明確判斷" && L.trendText(null) === "—" && L.trendText("x") === "—");
check("TREND_OPTIONS 的 id 和後端 LABELS 一致", L.TREND_OPTIONS.map((o) => o.id).join() === fx.labels.join(), fx.labels);
check("NOTE_MAX 和後端一致", L.NOTE_MAX === fx.note_max, fx.note_max);
check("hhmm：取台北時間的時分、壞掉的原樣回傳", L.hhmm(M1.entry_time) === M1.entry_time.slice(11, 16) && L.hhmm(null) === "—" && L.hhmm("abc") === "abc", L.hhmm(M1.entry_time));
check("flowText：外盤占比、外−內、累計起點（用後端的數字）", L.flowText(M1) === `外盤 ${(M1.flow_ratio * 100).toFixed(1)}%・外−內 +${M1.flow_net_diff.toLocaleString()} 口（${M1.flow_accum_start}）`, L.flowText(M1));
check("flowText：沒記到內外盤就寫後端給的原因，不顯示 0", L.flowText(M3) === M3.flow_missing_reason && M3.flow_missing_reason.length > 0 && !/\b0\b/.test(L.flowText(M3)), L.flowText(M3));
check("flowText：外−內為負數時帶負號", L.flowText({ ...M1, flow_net_diff: -300 }).includes("外−內 -300 口"), L.flowText({ ...M1, flow_net_diff: -300 }));
check("flowText：原因也沒有時給預設說明", L.flowText({ ...M3, flow_missing_reason: null }) === "沒有記到內外盤");
check("statusText：持有中／已平帶點數／部分平倉", L.statusText(M1) === "持有中" && L.statusText(M2) === `已平 +${M2.pnl_points} 點` && L.statusText({ ...M2, pnl_points: -12.34 }) === "已平 -12.3 點" && L.statusText({ ...M1, status: "partial" }) === "部分平倉", L.statusText(M2));
check("friendlyLabelError：舊後端沒有這支 API 時講白話", L.friendlyLabelError("Not Found").includes("重啟後端") && L.friendlyLabelError("只有手動單可以標記") === "只有手動單可以標記");

// ══ 清單與列（靜態渲染）══════════════════════════════════════════
const list = L.manualRows(fx.rows);
const raw = renderToStaticMarkup(e(C.LabelList, { rows: list, busyId: null, onPick: () => {} })), tx = strip(raw);
check("清單：每筆手動單一列、策略單不出現", (raw.match(/存備註/g) || []).length === list.length && !tx.includes("scalp"), tx);
check("清單：方向、合約、口數、進場價、狀態", tx.includes(`多 ${M1.contract}`) && tx.includes(`空 ${M2.contract}`) && tx.includes(`${M1.filled_lots} 口 @ ${M1.entry_price_avg.toLocaleString()}`) && tx.includes("持有中") && tx.includes(`已平 +${M2.pnl_points} 點`), tx);
check("清單：多單綠色、空單紅色", new RegExp(`text-\\[#00e676\\]">多 ${M1.contract}`).test(raw) && new RegExp(`text-\\[#ff1744\\]">空 ${M2.contract}`).test(raw), raw.slice(0, 600));
const pressed = (html) => [...html.matchAll(/<button[^>]*aria-pressed="(true|false)"[^>]*>([^<]*)<\/button>/g)].map((m) => `${m[1]}:${m[2]}`);
const r1 = renderToStaticMarkup(e(C.LabelRow, { row: M1, busy: false, onPick: () => {} }));
check("列：目前的標籤那顆按下（aria-pressed）、其他沒有", pressed(r1).join() === "true:多,false:空,false:盤整,false:無明確判斷", pressed(r1).join());
const r3 = renderToStaticMarkup(e(C.LabelRow, { row: M3, busy: false, onPick: () => {} }));
check("列：還沒標的四顆都沒按下、備註存檔鈕停用", pressed(r3).every((p) => p.startsWith("false:")) && /disabled=""[^>]*>存備註/.test(r3), pressed(r3).join());
check("列：已標且備註沒改過時，存備註停用", /disabled=""[^>]*>存備註/.test(renderToStaticMarkup(e(C.LabelRow, { row: M2, busy: false, onPick: () => {} }))));
check("列：備註欄帶出已存的備註、上限是 NOTE_MAX", renderToStaticMarkup(e(C.LabelRow, { row: M2, busy: false, onPick: () => {} })).includes(`value="${M2.trend_note}"`) && renderToStaticMarkup(e(C.LabelRow, { row: M2, busy: false, onPick: () => {} })).includes(`maxLength="${fx.note_max}"`));
check("列：事後標記只在 label_after_exit 的那筆出現", (raw.match(/事後標記/g) || []).length === list.filter((r) => r.label_after_exit).length && M2.label_after_exit === true && !r1.includes("事後標記") && renderToStaticMarkup(e(C.LabelRow, { row: M2, busy: false, onPick: () => {} })).includes("事後標記"));
check("列：忙碌時四顆標籤按鈕都停用", (renderToStaticMarkup(e(C.LabelRow, { row: M1, busy: true, onPick: () => {} })).match(/disabled=""/g) || []).length >= 4);
check("列：進場時內外盤摘要（有／缺值）", tx.includes("進場時內外盤：" + L.flowText(M1)) && tx.includes("進場時內外盤：" + M3.flow_missing_reason), tx);
check("清單：空的時候寫沒有手動進場", strip(renderToStaticMarkup(e(C.LabelList, { rows: [], busyId: null, onPick: () => {} }))).includes("沒有手動進場"));
check("沒有 undefined／NaN／null", !BAD.test(tx), tx.match(BAD));

// ══ 容器：hooks 換成替身 ═════════════════════════════════════════
const S = { rows: 0, err: 1, busy: 2 };
function harness(initial = {}) {
  const st = [], cbs = [], effects = []; let idx = 0;
  React.useState = (init) => { const i = idx++; if (!(i in st)) st[i] = i in initial ? initial[i] : init; return [st[i], (v) => { st[i] = typeof v === "function" ? v(st[i]) : v; }]; };
  React.useCallback = (f) => { cbs.push(f); return f; };
  React.useEffect = (f) => { effects.push(f); };
  const reset = () => { idx = 0; cbs.length = 0; effects.length = 0; };
  return { st, cbs, effects, render: () => { reset(); return renderToStaticMarkup(e(Card)); } };
}
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  {
    const t = strip(harness({ [S.rows]: fx.rows }).render());
    check("卡片：標題、說明、還沒標的筆數", t.includes("進場判斷標記") && t.includes("1 筆還沒標") && t.includes("多／空／盤整／無明確判斷") && t.includes("不影響下單") && t.includes("事後"), t);
    check("卡片：全部標完就不顯示「還沒標」", !strip(harness({ [S.rows]: fx.rows.filter((r) => r.trade_id !== "M3") }).render()).includes("還沒標"));
    check("卡片：還沒載入時只有標題與說明（不顯示清單、不顯示沒有手動進場）", !strip(harness().render()).includes("沒有手動進場") && !strip(harness().render()).includes("存備註"));
    check("卡片：載入失敗顯示錯誤", strip(harness({ [S.rows]: null, [S.err]: "後端還沒有這個 API（要重啟後端才有）" }).render()).includes("後端還沒有這個 API"));
    check("卡片：沒有 undefined／NaN", !BAD.test(t), t.match(BAD));
  }
  // 載入、輪詢、下單事件
  {
    const calls = []; apiMod.api.tradelog.entries = async (days, limit) => { calls.push([days, limit]); return fx.rows; };
    const listeners = {}; global.window = { addEventListener: (n, f) => { listeners[n] = f; }, removeEventListener: (n) => { delete listeners[n]; } };
    const h = harness(); h.render();
    const realSet = global.setInterval, realClear = global.clearInterval, realTO = global.setTimeout, ivs = [], tos = []; let cleared = null;
    global.setInterval = (f, ms) => { ivs.push([f, ms]); return 7; }; global.clearInterval = (id) => { cleared = id; };
    global.setTimeout = (f, ms) => { tos.push([f, ms]); return 8; };
    const cleanup = h.effects[0]();
    global.setInterval = realSet; global.setTimeout = realTO;
    await tick();
    check("載入：一開始抓最近 3 天、最多 40 筆", calls.length === 1 && calls[0][0] === 3 && calls[0][1] === 40, JSON.stringify(calls));
    check("輪詢：15 秒一次（只讀本機資料庫）", ivs.length === 1 && ivs[0][1] === 15000, JSON.stringify(ivs.map((x) => x[1])));
    check("下單事件：有掛監聽，下單後等 3 秒再抓一次", typeof listeners[apiMod.ORDER_PLACED_EVENT] === "function", Object.keys(listeners));
    global.setTimeout = (f, ms) => { tos.push([f, ms]); return 8; };
    listeners[apiMod.ORDER_PLACED_EVENT](); global.setTimeout = realTO;
    check("下單事件：3 秒後才抓（不是立刻）", tos.length === 1 && tos[0][1] === 3000 && calls.length === 1, JSON.stringify(tos.map((x) => x[1])));
    ivs[0][0](); await tick(); check("輪詢觸發：再抓一次", calls.length === 2);
    cleanup(); global.clearInterval = realClear;
    check("卸載：停掉輪詢、拿掉監聽", cleared === 7 && !(apiMod.ORDER_PLACED_EVENT in listeners), JSON.stringify([cleared, Object.keys(listeners)]));
    check("載入成功會存進 state、清掉錯誤", h.st[S.rows] === fx.rows && h.st[S.err] === null);
  }
  // 點標籤
  {
    const labels = [], loads = []; apiMod.api.tradelog.entries = async () => { loads.push(1); return fx.rows; };
    apiMod.api.tradelog.label = async (id, trend, note) => { labels.push([id, trend, note]); return {}; };
    const h = harness({ [S.rows]: fx.rows }); h.render();
    const pick = h.cbs[1], seen = [];
    const p = pick(M3, "range", "  橫盤整理  "); seen.push(h.st[S.busy]); await p;
    check("點標籤：送出 trade_id、選項、去頭尾空白的備註", labels.length === 1 && labels[0].join("|") === "M3|range|橫盤整理", JSON.stringify(labels));
    check("點標籤：送出期間標記忙碌、完成後清掉並重抓", seen[0] === "M3" && h.st[S.busy] === null && loads.length === 1 && h.st[S.err] === null, JSON.stringify([seen, h.st[S.busy], loads.length]));
  }
  {
    apiMod.api.tradelog.entries = async () => fx.rows;
    apiMod.api.tradelog.label = async () => { throw new Error('422 — {"detail":"只有手動單可以標記趨勢判斷"}'); };
    const h = harness({ [S.rows]: fx.rows }); h.render();
    await h.cbs[1](S1, "multi", "");
    check("點標籤失敗：顯示後端那句說明、忙碌狀態清掉、清單還在", h.st[S.err] === "只有手動單可以標記趨勢判斷" && h.st[S.busy] === null && h.st[S.rows] === fx.rows, JSON.stringify([h.st[S.err], h.st[S.busy]]));
    apiMod.api.tradelog.label = async () => { throw new Error("404 — Not Found"); };
    const h2 = harness({ [S.rows]: fx.rows }); h2.render(); await h2.cbs[1](M1, "multi", "");
    check("舊後端（404）：講「要重啟後端」", h2.st[S.err].includes("重啟後端"), h2.st[S.err]);
  }
  // API 包裝
  {
    const seen = []; global.fetch = async (url, init) => { seen.push([url, init]); return { ok: true, json: async () => ({}), text: async () => "" }; };
    global.window = { location: { hostname: "localhost" } }; global.localStorage = { getItem: () => null, setItem: () => {} };
    const fresh = transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "api_fresh.js"));          // 前面的測試把 apiMod 的方法換成替身了
    let ok = true; try { await fresh.api.tradelog.entries(3, 40); await fresh.api.tradelog.label("M 1", "multi", ""); await fresh.api.tradelog.label("M2", "none", "備註"); } catch (x) { ok = false; seen.push(String(x)); }
    check("API：entries 帶 days／limit、label 用 POST＋JSON（trade_id 要編碼、空備註送 null）", ok && /\/tradelog\/entries\?days=3&limit=40$/.test(seen[0][0]) && /\/tradelog\/entries\/M%201\/label$/.test(seen[1][0]) && seen[1][1].method === "POST"
      && JSON.parse(seen[1][1].body).trend === "multi" && JSON.parse(seen[1][1].body).note === null && JSON.parse(seen[2][1].body).note === "備註", JSON.stringify(seen).slice(0, 500));
  }
  console.log(fail ? `\n${fail} FAIL` : "\n全部通過");
  process.exit(fail ? 1 : 0);
})();
