// 〈盤前跨市場快照〉面板：格式化與缺值原因（純函式）、檢視元件、容器的載入／錯誤／空白狀態（hooks 換成替身）。
// 畫面吃的資料由 tests/test_frontend_premarket.py 用 core/premarket_log.py 現算（環境變數 PM_FIXTURES 指到目錄）。
// 用法：PM_FIXTURES=<dir> node tests/frontend/premarket_check.js（需要 frontend/node_modules）
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
if (!process.env.PM_FIXTURES) { console.error("需要環境變數 PM_FIXTURES（由 tests/test_frontend_premarket.py 產生）"); process.exit(2); }
const fx = (n) => JSON.parse(fs.readFileSync(path.join(process.env.PM_FIXTURES, n + ".json"), "utf8"));
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "premarket-check-"));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });

const transpile = (src, out) => {
  fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText);
  return require(out);
};

const L = transpile(path.join(FRONT, "lib", "premarket.ts"), path.join(TMP, "premarket.js"));
const realApi = transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "real_api.js"));
const calls = [];
let impl = {};
const spy = (name) => (...a) => { calls.push([name, ...a]); return impl[name] ? impl[name](...a) : Promise.resolve(undefined); };
global.__api_stub = { api: { premarket: { status: spy("status"), snapshots: spy("snapshots"), preview: spy("preview") } },
                      apiErrorDetail: realApi.apiErrorDetail, getBase: () => "http://100.0.0.1:8002/api" };
fs.writeFileSync(path.join(TMP, "api_stub.js"), "module.exports = global.__api_stub;");
fs.writeFileSync(path.join(TMP, "lucide_stub.js"), "module.exports = new Proxy({}, { get: () => () => null });");
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) {
  if (req === "@/lib/api") return path.join(TMP, "api_stub.js");
  if (req === "@/lib/premarket") return path.join(TMP, "premarket.js");
  if (req === "lucide-react") return path.join(TMP, "lucide_stub.js");
  return orig.call(this, req, ...r);
};
const P = transpile(path.join(FRONT, "components", "PremarketPanel.tsx"), path.join(TMP, "panel.js"));
const Comp = P.default, e = React.createElement;

let fail = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + String(extra).slice(0, 900))); if (!ok) fail++; };
const strip = (s) => s.replace(/<\/?(td|th|tr|thead|tbody|table|div|p|li|h\d|button|br)\b[^>]*>/g, " ").replace(/<[^>]+>/g, "").replace(/&gt;/g, ">").replace(/&lt;/g, "<").replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/\s+/g, " ");
const raw = (el) => renderToStaticMarkup(el);
const html = (el) => strip(raw(el));
const BAD = /undefined|NaN|\bnull\b|Infinity|\[object/;

// ══ 純函式 ═══════════════════════════════════════════════════════
check("fmtPct：正負號、四捨五入到 0、缺值", L.fmtPct(0.0123) === "+1.23%" && L.fmtPct(-0.00736) === "−0.74%" && L.fmtPct(0) === "0.00%" && L.fmtPct(-0.00001) === "0.00%"
  && L.fmtPct(null) === "—" && L.fmtPct(undefined) === "—" && L.fmtPct(NaN) === "—" && L.fmtPct(0.5, 1) === "+50.0%", [L.fmtPct(0.0123), L.fmtPct(-0.00736), L.fmtPct(-0.00001)]);
check("fmtValue：VIX 是收盤值、其他是報酬、缺值是 —", L.fmtValue("vix", { value: 15.206 }) === "15.21" && L.fmtValue("sox", { value: -0.027 }) === "−2.70%"
  && L.fmtValue("txf_night", { value: 0.00736 }) === "+0.74%" && L.fmtValue("tsm", { value: null }) === "—" && L.fmtValue("vix", undefined) === "—" && L.fmtLevel(Infinity) === "—");
const usClose = Date.UTC(2026, 9, 13, 20, 0, 0) / 1000;           // 美東 10/13 16:00（夏令）＝ UTC 20:00 ＝ 台北 10/14 04:00
check("tpeTime：一律台北時間（不跟瀏覽器時區）、午夜是 00 不是 24", L.tpeTime(usClose) === "10-14 04:00" && L.tpeTime(Date.UTC(2026, 9, 13, 16, 0, 0) / 1000) === "10-14 00:00"
  && L.tpeTime(Date.UTC(2026, 2, 6, 21, 0, 0) / 1000) === "03-07 05:00" && L.tpeTime(null) === "—" && L.tpeTime(NaN) === "—", [L.tpeTime(usClose), L.tpeTime(Date.UTC(2026, 9, 13, 16, 0, 0) / 1000)]);
const R = L.reasonText;
check("reasonText：每種原因都有白話、沒看懂的原樣顯示、沒有原因是空字串",
  R("us_closed: 2026-05-25 陣亡將士紀念日") === "美股休市：2026-05-25 陣亡將士紀念日" && R("source_error: nasdaq: network: timeout") === "來源失敗：nasdaq: network: timeout"
  && R("stale_or_missing: 最新資料 2026-10-12，預期 2026-10-13") === "資料還沒更新：最新資料 2026-10-12，預期 2026-10-13" && R("insufficient_history: 只有 5 筆") === "歷史資料不足"
  && R("out_of_range: 0.3 不在 [-0.25, 0.25]").startsWith("超出異常值範圍") && R("no_ticks: 夜盤沒有 TXF 成交紀錄") === "本機沒有那段成交紀錄"
  && R("incomplete_session: 夜盤或前一日日盤最後一筆離收盤超過 5 分鐘").startsWith("夜盤或日盤的紀錄不完整") && R("ticks_db_unavailable: OperationalError") === "本機成交資料庫讀不到"
  && R("night_not_finished: T 在夜盤收盤之前") === "夜盤還沒收盤" && R("bad_price: 收盤價 ≤ 0") === "價格異常" && R("某個新原因: x") === "某個新原因: x" && R(null) === "" && R("") === "");
check("lateText／nextText／usSessionText／missingCount", L.lateText(59) === "" && L.lateText(null) === "" && L.lateText(6120) === "補記（晚了 102 分鐘）"
  && L.nextText({ trading_date: "2026-10-14", time: "2026-10-14T08:30+08:00", late: false }) === "10-14 08:30"
  && L.nextText({ trading_date: "2026-10-14", time: "2026-10-14T08:30+08:00", late: true }) === "10-14 08:30（現在就會補記）" && L.nextText(null) === "—" && L.nextText({ time: "亂寫", late: false }) === "亂寫"
  && L.usSessionText({ us_session_date: "2026-10-13", us_early_close: false, us_closed: false, notes: "" }) === "用的美股是 2026-10-13 收盤"
  && L.usSessionText({ us_session_date: "2026-11-27", us_early_close: true, us_closed: false, notes: "感恩節隔天" }) === "用的美股是 2026-11-27 收盤（提早收盤）"
  && L.usSessionText({ us_session_date: "2026-05-25", us_early_close: false, us_closed: true, notes: "陣亡將士紀念日" }) === "美股 2026-05-25 休市（陣亡將士紀念日），三個美股欄位留空"
  && L.usSessionText({ us_session_date: "2026-05-25", us_early_close: false, us_closed: true, notes: "" }) === "美股 2026-05-25 休市，三個美股欄位留空" && L.usSessionText(null) === "—"
  && L.twSourceText("twse") === "證交所" && L.twSourceText("twse_stale_cache").startsWith("證交所（舊快取") && L.twSourceText("fallback_weekday").startsWith("只依週一至週五") && L.twSourceText("x") === "x" && L.twSourceText(null) === "—"
  && L.missingCount({ txf_night: { value: null }, sox: { value: 0 }, tsm: { value: null }, vix: { value: 15 } }) === 2 && L.missingCount({}) === 4, [L.nextText(null)]);
const base = { enabled: true, snapshot_time: "08:30", sources: ["nasdaq", "cboe"], alerts: 0, last_alert: null, today: "2026-10-14", today_recorded: false, missed_dates: [], next_snapshot: null, snapshots: 0 };
const chips = (s) => L.statusChips(s).map((c) => c[0]);
check("statusChips：關閉、已記幾筆、今天記了沒、警示、錯過",
  J(chips(base)) === J(["記錄中", "T 08:30", "已記 0 筆", "今天還沒記"]) && J(chips({ ...base, enabled: false, snapshots: 12, today_recorded: true, alerts: 2, missed_dates: ["a", "b"] })) === J(["已關閉", "T 08:30", "已記 12 筆", "今天已記", "失敗警示 2", "錯過 2 天"])
  && L.statusChips({ ...base, alerts: 1 }).find((c) => c[0].startsWith("失敗"))[1] === "#ffc107" && L.statusChips({ ...base, snapshots: 3 })[2][1] === "#00e676", chips(base));
function J(x) { return JSON.stringify(x); }

// ══ 用後端現算的資料渲染檢視元件 ═════════════════════════════════
const rows = fx("snapshots"), pvSame = fx("preview_same"), pvVoided = fx("preview_voided"), pvOther = fx("preview_other"), status = fx("status"), status0 = fx("status_empty"),
  statusOff = fx("status_disabled"), statusAlert = fx("status_alert");
const byDate = Object.fromEntries(rows.map((r) => [r.trading_date, r]));
check("後端現算的資料形狀：7 天裡有休市、補記、更正、作廢、缺值", rows.length === 6 && rows[0].trading_date === "2026-10-16" && byDate["2026-10-16"].voided && byDate["2026-10-15"].corrected
  && byDate["2026-10-14"].late_seconds > 6000 && byDate["2026-05-26"].calendar.us_closed && byDate["2026-10-12"].features.txf_night.value === null, rows.map((r) => r.trading_date));
const ft = (r) => html(e(P.FeatureTable, { features: r.features, calendar: r.calendar }));
{
  const ok = byDate["2026-10-13"], t = ft(ok);
  check("欄位表：四個欄位的名稱、值（用同一個格式化函式）、來源、資料時間、日曆說明", ["台指期夜盤", "費半 SOX", "台積電 ADR", "VIX"].every((n) => t.includes(n))
    && L.PM_KEYS.every((k) => t.includes(L.fmtValue(k, ok.features[k])) && t.includes(L.tpeTime(ok.features[k].source_timestamp)) && !(L.fmtValue(k, ok.features[k]) === "—"))
    && t.includes("nasdaq") && t.includes("cboe") && t.includes("ticks.db") && t.includes(`用的美股是 ${ok.calendar.us_session_date} 收盤`) && t.includes("台股日曆來源：證交所") && t.includes("不用前一天的值、也不補預設值") && !BAD.test(t), t);
  check("欄位表：資料時間是台北時間（美股 10/12 16:00 夏令收盤＝台北 10/13 04:00）", t.includes("10-13 04:00") && t.includes(`前一個台股交易日 ${ok.calendar.prev_tw_trading_day}`), t);
  const night = ft(byDate["2026-10-12"]);
  check("欄位表：夜盤缺值顯示 — 與原因（不是 0）", night.includes("本機成交資料庫讀不到") && !BAD.test(night), night);
  const closed = ft(byDate["2026-05-26"]);
  check("欄位表：美股休市顯示原因、三個美股欄位都是 — 且沒有來源、說明句講得通", closed.includes("美股休市：2026-05-25 陣亡將士紀念日") && (closed.match(/美股休市：/g) || []).length === 3
    && closed.includes("美股 2026-05-25 休市（陣亡將士紀念日），三個美股欄位留空；前一個台股交易日 2026-05-25") && !closed.includes("用的美股是")
    && L.PM_KEYS.slice(1).every((k) => L.fmtValue(k, byDate["2026-05-26"].features[k]) === "—" && !byDate["2026-05-26"].features[k].source), closed);
  const corrected = ft(byDate["2026-10-15"]);
  check("欄位表：更正後的 VIX（手動）顯示 15.50 與來源", corrected.includes("15.50") && corrected.includes("手動"), corrected);
}
{
  const t = html(e(P.RecentTable, { rows }));
  check("最近的紀錄：每一天一列、新到舊、日期都在", rows.every((r) => t.includes(r.trading_date)) && t.indexOf("2026-10-16") < t.indexOf("2026-10-15") && t.indexOf("2026-10-15") < t.indexOf("2026-10-14") && !BAD.test(t), t);
  check("最近的紀錄：作廢（附原因）、已更正、缺幾欄有標示；只有真的補記的那天才標補記（平常晚幾秒不標）", t.includes("作廢：颱風假，期交所休市") && t.includes("已更正") && t.includes("缺 3 欄") && t.includes("缺 1 欄")
    && (t.match(/補記（晚了/g) || []).length === 1 && t.includes("補記（晚了 102 分鐘）"), t);
  const m = raw(e(P.RecentTable, { rows }));
  check("最近的紀錄：作廢那天變淡、缺值格子的 title 是原因", m.includes("opacity:0.45") && m.includes('title="美股休市：2026-05-25 陣亡將士紀念日"'), m.slice(0, 600));
}
{
  const t = html(e(P.StatusChips, { s: status }));
  check("狀態晶片：有資料時顯示已記幾筆", t.includes("記錄中") && t.includes("T 08:30") && t.includes(`已記 ${status.snapshots} 筆`) && status.snapshots === 6 && !BAD.test(t), t);
  check("狀態晶片：警示與錯過", html(e(P.StatusChips, { s: statusAlert })).includes("失敗警示 2") && html(e(P.StatusChips, { s: status })).includes("錯過 1 天"));
}

// ══ 容器：hooks 換成替身，驗證載入、錯誤、空白、預覽 ═══════════════
function harness(initial = {}) {
  const st = [], cbs = [], effects = [], refs = [];
  let idx = 0, ridx = 0;
  React.useState = (init) => { const i = idx++; if (!(i in st)) st[i] = i in initial ? initial[i] : (typeof init === "function" ? init() : init); return [st[i], (v) => { st[i] = typeof v === "function" ? v(st[i]) : v; }]; };
  React.useRef = (init) => { const i = ridx++; if (!(i in refs)) refs[i] = { current: init }; return refs[i]; };
  React.useCallback = (f) => { cbs.push(f); return f; };
  React.useEffect = (f) => { effects.push(f); };
  const reset = () => { idx = 0; ridx = 0; cbs.length = 0; effects.length = 0; };
  // useCallback 的順序：loadPreview 在前、load 在後（load 會在後端恢復時呼叫 loadPreview）
  return { st, cbs, effects, load: () => cbs[1](), loadPreview: () => cbs[0](), render: () => { reset(); return html(e(Comp)); }, raw: () => { reset(); return raw(e(Comp)); } };
}
const S = { status: 0, rows: 1, err: 2, preview: 3, pvBusy: 4, pvErr: 5 };
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  {
    const h = harness();
    const t = h.render();
    check("容器：還沒載入時只有標題、沒有錯誤也沒有『還沒有紀錄』", t.includes("盤前跨市場快照") && t.includes("只記錄、不影響任何交易決策") && !t.includes("還沒有任何紀錄") && !t.includes("後端還沒有這個 API") && !BAD.test(t), t);
    check("容器：載入時會抓狀態、最近 12 筆紀錄與預覽（呼叫 effect 之後）", h.effects.length === 1 && h.cbs.length === 2, [h.effects.length, h.cbs.length]);
  }
  {
    calls.length = 0;
    impl = { status: () => Promise.resolve(status), snapshots: () => Promise.resolve(rows), preview: () => Promise.resolve(pvOther) };
    const h = harness(); h.render();
    await h.load(); await h.loadPreview();
    check("容器：load 抓 status 與 snapshots(60, 12)、loadPreview 抓 preview（不帶日期＝後端預設）", J(calls) === J([["status"], ["snapshots", 60, 12], ["preview"]]), J(calls));
    check("容器：載入完成的狀態", h.st[S.status] === status && h.st[S.rows] === rows && h.st[S.err] === null && h.st[S.preview] === pvOther && h.st[S.pvBusy] === false && h.st[S.pvErr] === null);
    const t = h.render();
    check("容器：有紀錄時顯示最新一筆（欄位表）、預覽（另一天）、最近的紀錄、狀態晶片、CSV 連結", t.includes("最新紀錄 2026-10-15") && !t.includes("最新紀錄 2026-10-16") && t.includes("15.50") && t.includes(`預覽 ${pvOther.trading_date}（還沒有紀錄）`) && t.includes("最近的紀錄")
      && t.includes("記錄中") && t.includes(`下一筆：${L.nextText(status.next_snapshot)}`) && t.includes("後端在 13:45 之後才啟動而錯過的日子：2026-10-20") && !t.includes("預覽的這一天就是上面的最新紀錄") && !BAD.test(t), t);
    check("容器：最新一筆與預覽是兩張欄位表（有兩份『欄位』表頭）", (t.match(/欄位 值 來源/g) || []).length === 2, (t.match(/欄位 值 來源/g) || []).length);
    check("容器：CSV 下載連結指向後端的 snapshots.csv", h.raw().includes('href="http://100.0.0.1:8002/api/premarket/snapshots.csv"') && t.includes("下載 CSV（全部）"));
    check("容器：說明歷史預檢的結論與限制", t.includes("VIX 對日盤波動有穩定的增量，費半沒有，隔夜資訊對方向沒有") && t.includes("不決定上線") && t.includes("250 個交易日"));
  }
  {
    impl = { status: () => Promise.resolve(status), snapshots: () => Promise.resolve(rows), preview: () => Promise.resolve(pvSame) };
    const h = harness(); h.render(); await h.load(); await h.loadPreview();
    const t = h.render();
    check("容器：預覽的那一天就是最新紀錄時不重複顯示", t.includes("預覽的這一天就是上面的最新紀錄") && (t.match(/欄位 值 來源/g) || []).length === 1 && t.includes(`預覽 ${pvSame.trading_date}（這一天已經有紀錄）`), t);
  }
  {
    impl = { status: () => Promise.resolve(status), snapshots: () => Promise.resolve(rows), preview: () => Promise.resolve(pvVoided) };
    const h = harness(); h.render(); await h.load(); await h.loadPreview();
    const t = h.render();
    check("容器：預覽的那一天已作廢：標題與說明都講作廢（附原因）、不顯示那天的預覽表", pvVoided.recorded && t.includes(`預覽 ${pvVoided.trading_date}（這一天的紀錄已作廢）`) && t.includes("預覽的這一天已經作廢：颱風假，期交所休市。")
      && (t.match(/欄位 值 來源/g) || []).length === 1 && !t.includes("預覽的這一天就是上面的最新紀錄"), t);
  }
  {
    impl = { status: () => Promise.resolve(status), snapshots: () => Promise.resolve(rows.filter((r) => r.voided)), preview: () => Promise.resolve(pvOther) };
    const h = harness(); h.render(); await h.load(); await h.loadPreview();
    const t = h.render();
    check("容器：紀錄都作廢了不能當『最新紀錄』，要說沒有有效的", t.includes("目前沒有有效的紀錄（都已作廢）。") && !t.includes("最新紀錄") && t.includes("最近的紀錄") && t.includes("作廢：颱風假，期交所休市"), t);
  }
  {
    impl = { status: () => Promise.resolve(statusOff), snapshots: () => Promise.resolve([]), preview: () => Promise.resolve(pvOther) };
    const h = harness(); h.render(); await h.load(); await h.loadPreview();
    const t = h.render();
    check("容器：功能關閉時說明是 PREMARKET_LOG=false，不說下一筆什麼時候記", t.includes("已關閉") && t.includes("盤前紀錄已關閉（.env 的 PREMARKET_LOG=false），不會記錄。") && !t.includes("下一筆在"), t);
  }
  {
    impl = { status: () => Promise.resolve(status0), snapshots: () => Promise.resolve([]), preview: () => Promise.resolve(pvOther) };
    const h = harness(); h.render(); await h.load(); await h.loadPreview();
    const t = h.render();
    check("容器：還沒有任何紀錄時說明下一筆什麼時候記、並顯示預覽", t.includes(`還沒有任何紀錄。下一筆在 ${L.nextText(status0.next_snapshot)}。`) && !t.includes("後端重啟之後") && t.includes("預覽") && !t.includes("最近的紀錄") && !t.includes("下載 CSV") && !BAD.test(t), t);
    check("容器：第一次的狀態晶片（0 筆、今天還沒記）", t.includes("已記 0 筆") && t.includes("今天還沒記"), t);
  }
  {
    impl = { status: () => Promise.reject(new Error('404 — {"detail":"Not Found"}')), snapshots: () => Promise.resolve([]), preview: () => Promise.reject(new Error('502 — {"detail":"預覽失敗：RuntimeError: 來源全掛"}')) };
    const h = harness(); h.render(); await h.load(); await h.loadPreview();
    const t = h.render();
    check("容器：後端是舊版（404）顯示白話說明，不是 Not Found", t.includes("後端還沒有這個 API（盤前跨市場紀錄是新功能，要重啟後端才有）") && !t.includes("Not Found") && !t.includes("還沒有任何紀錄"), t);
  }
  {
    impl = { status: () => Promise.resolve(status), snapshots: () => Promise.resolve(rows), preview: () => Promise.reject(new Error('502 — {"detail":"預覽失敗：RuntimeError: 來源全掛"}')) };
    const h = harness(); h.render(); await h.load(); await h.loadPreview();
    const t = h.render();
    check("容器：狀態正常但預覽失敗：顯示後端給的原因、按鈕恢復可按、其他區塊照常", t.includes("預覽失敗：RuntimeError: 來源全掛") && h.st[S.pvBusy] === false && t.includes("重新預覽") && t.includes("最新紀錄 2026-10-15") && !t.includes("後端還沒有這個 API"), t);
  }
  {
    let up = false;
    impl = { status: () => (up ? Promise.resolve(status) : Promise.reject(new Error("Failed to fetch"))), snapshots: () => Promise.resolve(rows),
             preview: () => (up ? Promise.resolve(pvOther) : Promise.reject(new Error('502 — {"detail":"預覽失敗：暫時連不上"}'))) };
    const h = harness(); h.render(); await h.load(); await h.loadPreview();
    const bad = h.render();
    up = true; await h.load(); await tick();                              // 下一次定時重抓：成功，而且因為上次失敗過，預覽也會自己重抓
    const good = h.render();
    check("容器：整個後端連不上時只顯示連線錯誤（預覽區塊收起來）；恢復後錯誤消失、紀錄與預覽自己出現",
      bad.includes("Failed to fetch") && !bad.includes("預覽失敗") && !bad.includes("重新預覽") && !good.includes("Failed to fetch") && !good.includes("預覽失敗") && good.includes("最新紀錄 2026-10-15") && good.includes("預覽 2026-10-19"), [bad, good]);
  }
  {
    impl = { status: () => Promise.resolve(status), snapshots: () => Promise.resolve(rows), preview: () => Promise.reject(new Error('502 — {"detail":"預覽失敗：nasdaq: HTTP 404"}')) };
    const h = harness(); h.render(); await h.load(); await h.loadPreview();
    const t = h.render();
    check("容器：錯誤訊息裡剛好有 404（來源回的）不能被當成『後端是舊版』", t.includes("預覽失敗：nasdaq: HTTP 404") && !t.includes("後端還沒有這個 API"), t);
  }
  {
    let release; const gate = new Promise((r) => { release = r; });
    impl = { preview: () => gate.then(() => pvOther) };
    const h = harness(); h.render();
    const p = h.loadPreview();
    await tick();
    const busy = h.render();
    const DISABLED = /<button[^>]*\sdisabled=""/;
    check("容器：預覽抓取中按鈕顯示『抓取中…』且真的停用（不是只有 disabled: 樣式類別）", busy.includes("抓取中…") && DISABLED.test(h.raw()), busy);
    release(); await p;
    check("容器：抓完恢復成『重新預覽』且可以按", h.render().includes("重新預覽") && !h.render().includes("抓取中…") && !DISABLED.test(h.raw()));
  }
  {
    calls.length = 0;
    impl = { status: () => Promise.resolve(status), snapshots: () => Promise.resolve(rows), preview: () => Promise.resolve(pvOther) };
    const h = harness(); h.render();
    const realSet = global.setInterval, realClear = global.clearInterval, ivs = []; let cleared = null;
    global.setInterval = (f, ms) => { ivs.push([f, ms]); return 77; }; global.clearInterval = (id) => { cleared = id; };
    let cleanup;
    try { cleanup = h.effects[0](); await tick(); cleanup(); } finally { global.setInterval = realSet; global.clearInterval = realClear; }
    const atStart = J(calls);
    calls.length = 0; await ivs[0][0]();                                  // 計時器到點時呼叫的函式
    check("容器：一載入就抓狀態、紀錄與預覽各一次；之後每 60 秒只重抓狀態與紀錄（預覽會去抓外部資料，不輪詢）",
      atStart === J([["status"], ["snapshots", 60, 12], ["preview"]]) && ivs.length === 1 && ivs[0][1] === 60000 && J(calls) === J([["status"], ["snapshots", 60, 12]]), [atStart, ivs.map((x) => x[1]), J(calls)]);
    check("容器：離開頁面時清掉計時器", cleared === 77, cleared);
  }
  {
    const h = harness({ [S.status]: statusAlert, [S.rows]: rows });
    const t = h.render();
    check("容器：最近一次失敗警示顯示在說明裡（資料庫鎖住）", t.includes("最近一次失敗警示：run（2026-10-14）database is locked") && t.includes("失敗警示 2"), t);
  }
  {
    let up = false; calls.length = 0;
    impl = { status: () => (up ? Promise.resolve(status) : Promise.reject(new Error('404 — {"detail":"Not Found"}'))), snapshots: () => Promise.resolve(rows),
             preview: () => (up ? Promise.resolve(pvOther) : Promise.reject(new Error('404 — {"detail":"Not Found"}'))) };
    const h = harness(); h.render(); await h.load(); await h.loadPreview();
    const t0 = h.render();
    check("容器：後端是舊版時只顯示一句說明，預覽區塊（按鈕與說明）先收起來", t0.includes("後端還沒有這個 API") && (t0.match(/後端還沒有這個 API/g) || []).length === 1 && !t0.includes("重新預覽") && !t0.includes("如果現在記"), t0);
    calls.length = 0; up = true; await h.load();
    await tick();
    check("容器：後端恢復（例如剛重啟）時，下一次的定時重抓會連預覽一起重抓一次、錯誤訊息消失", J(calls) === J([["status"], ["snapshots", 60, 12], ["preview"]]) && h.st[S.err] === null && h.st[S.preview] === pvOther && h.render().includes("預覽 2026-10-19"), J(calls));
    calls.length = 0; await h.load();
    check("容器：沒有出過錯時，定時重抓不會去抓預覽", J(calls) === J([["status"], ["snapshots", 60, 12]]), J(calls));
  }
  {
    const h = harness({ [S.err]: "連不上", [S.preview]: pvOther });
    const t = h.render();
    check("容器：狀態載入失敗但之前已有預覽時，預覽留著（不因為一次連線失敗就消失）", t.includes("連不上") && t.includes("預覽 2026-10-19") && t.includes("重新預覽"), t);
  }
  {
    // 表格結構：欄位表四欄（缺值原因放在欄位名稱下面，手機上才看得到）；紀錄表五欄（備註另起一行，不要把列撐高）
    const closedRaw = raw(e(P.FeatureTable, { features: byDate["2026-05-26"].features, calendar: byDate["2026-05-26"].calendar }));
    check("欄位表：只有四個表頭（沒有會被擠出手機畫面的『狀態』欄），缺值原因在欄位名稱的格子裡", (closedRaw.match(/<th /g) || []).length === 4 && !html(e(P.FeatureTable, { features: byDate["2026-05-26"].features, calendar: byDate["2026-05-26"].calendar })).includes("狀態")
      && /<td[^>]*>費半 SOX<div[^>]*>美股休市：2026-05-25 陣亡將士紀念日<\/div><\/td>/.test(closedRaw), closedRaw.slice(0, 900));
    const recRaw = raw(e(P.RecentTable, { rows }));
    check("紀錄表：五個表頭用短欄名、全名放在 title；每一天是一組 tbody", (recRaw.match(/<th /g) || []).length === 5 && ["夜盤", "SOX", "TSM", "VIX"].every((n) => recRaw.includes(`>${n}</th>`))
      && ["台指期夜盤", "費半 SOX", "台積電 ADR", "VIX"].every((n) => recRaw.includes(`title="${n}"`)) && (recRaw.match(/<tbody/g) || []).length === rows.length, recRaw.slice(0, 700));
    const chunks = recRaw.split("<tbody").slice(1);                       // 一天一組
    const hasNote = Object.fromEntries(chunks.map((c) => [/(\d{4}-\d{2}-\d{2})/.exec(c)[1], c.includes('colSpan="5"')]));
    check("紀錄表：備註在整列寬的另一行（colSpan＝欄數）：有備註的 5 天有、沒有備註的 10-13 沒有", J(hasNote) === J({ "2026-10-16": true, "2026-10-15": true, "2026-10-14": true, "2026-10-13": false, "2026-10-12": true, "2026-05-26": true }), J(hasNote));
    const noNote = rows.filter((r) => !r.voided && !r.corrected && r.late_seconds < 60 && L.missingCount(r.features) === 0);
    const one = raw(e(P.RecentTable, { rows: noNote }));
    check("紀錄表：整天沒有任何備註時沒有備註那一行", noNote.length === 1 && !one.includes("colSpan"), one);
  }
  console.log(fail ? `\n${fail} 項失敗` : "\n全部通過");
  process.exit(fail ? 1 : 0);
})();
