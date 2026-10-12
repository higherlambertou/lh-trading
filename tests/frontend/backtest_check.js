// 回測面板的邏輯：表單驗證、結論顏色、面板的點擊與各種工作狀態的畫面。
// 前端沒有 DOM 測試工具，做法同 stop_confirm_check.js：hooks 換成替身、直接呼叫元件函式取得元素樹、呼叫 onClick／onChange。
// 由 tests/test_frontend_backtest.py 執行。用法：node tests/frontend/backtest_check.js（需要 frontend/node_modules）
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "backtest-check-"));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });

const transpile = (src, out) => {
  const js = ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText;
  fs.writeFileSync(out, js);
  return require(out);
};

// ── 模組：lib/backtest（真的）、lib/api 的 apiErrorDetail（真的）、api 物件（替身）──
const calls = [];
let impl = {};
const spy = (name) => (...a) => { calls.push([name, ...a]); return impl[name] ? impl[name](...a) : Promise.resolve(undefined); };
const realBt = transpile(path.join(FRONT, "lib", "backtest.ts"), path.join(TMP, "backtest.js"));
const realApi = transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "real_api.js"));
global.__api_stub = { api: { backtest: { info: spy("info"), run: spy("run"), jobs: spy("jobs"), job: spy("job"), cancel: spy("cancel") } },
                      apiErrorDetail: realApi.apiErrorDetail };
fs.writeFileSync(path.join(TMP, "api_stub.js"), "module.exports = global.__api_stub;");
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) {
  if (req === "@/lib/api") return path.join(TMP, "api_stub.js");
  if (req === "@/lib/backtest") return path.join(TMP, "backtest.js");
  return orig.call(this, req, ...r);
};
const panel = transpile(path.join(FRONT, "components", "BacktestPanel.tsx"), path.join(TMP, "panel.js"));
const Comp = panel.default;
const { buildRequest, verdict, topRows, fmtPts, fmtT, fmtPct, DEFAULT_FORM } = realBt;

// ── 小工具 ──
const NAMES = ["form", "info", "job", "err", "busy"];
function render(forced = {}) {
  const sets = [];
  let n = 0;
  React.useState = (init) => {
    const i = n++; const v = Object.prototype.hasOwnProperty.call(forced, i) ? forced[i] : init;
    return [v, (x) => sets.push([NAMES[i], typeof x === "function" ? x(v) : x])];
  };
  React.useEffect = () => {}; React.useCallback = (f) => f;
  return { tree: Comp(), sets };
}
function find(node, pred, out = []) {
  if (Array.isArray(node)) node.forEach((c) => find(c, pred, out));
  else if (node && typeof node === "object" && node.props) { if (pred(node)) out.push(node); find(node.props.children, pred, out); }
  return out;
}
const text = (node) => (Array.isArray(node) ? node.map(text).join("") : typeof node === "string" || typeof node === "number" ? String(node) : node && node.props ? text(node.props.children) : "");
const button = (tree, label) => find(tree, (e) => e.type === "button" && text(e.props.children).trim().startsWith(label))[0];
const tick = () => new Promise((r) => setTimeout(r, 5));
const J = JSON.stringify;
let fail = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + extra)); if (!ok) fail++; };

// ── 資料 ──
const win = (perday, t, trades = 100) => ({ days: 100, trades, perday, sd: 100, t, pertrade: perday / 5, gross_pertrade: perday / 5 + 4, winrate: 0.42 });
const row = (label, ist, oost) => ({ label: `orb ${label}`, is: win(ist * 3, ist), oos: win(oost * 3, oost), all: win(1, 0.1) });
const report = (extra = {}) => ({
  strategy: "orb", combos: 36, days: 267, first: "2025-09-03", last: "2026-10-08",
  split: { is_days: 160, oos_days: 107, oos_from: "2026-05-07" }, cost: 4, median_gross_pertrade: -7.7, median_net_pertrade: -11.7,
  positive_all: 5, is_positive: 7, is_positive_oos_positive: 2, best_all_t: 0.39, best_is_t: 0.62, mde_perday: 26,
  rows: [row("A", 0.2, 0.1), row("B", 0.9, -1.5), row("C", 0.5, 0.3), row("D", -0.4, 0.2)], ...extra });
const wf = (t, perday = t * 5) => ({ days: 147, perday, t, trades: 150, positive_windows: 3, windows: 8, distinct_picks: 4, train: 120, test: 20, picks: [] });
const job = (extra = {}) => ({ id: "ab12cd34", status: "done", request: { strategy: "orb", cost: 4, split: 0.6, null: 0, walk_forward: null, top: 3, source: "bars", session: "day" },
  started_at: 1, elapsed_sec: 12.4, progress: { done: 1, total: 1 }, error: "", log_tail: [], has_reports: true, reports: [report()], ...extra });

(async () => {
  // ── buildRequest ──
  let b = buildRequest(DEFAULT_FORM);
  check("a1 預設表單 → 合法請求", b.ok && J(b.req) === J({ strategy: "orb", cost: 4, split: 0.6, null: 0, walk_forward: null, top: 3, source: "bars", session: "day" }), J(b));
  b = buildRequest({ ...DEFAULT_FORM, wf: true });
  check("a2 勾滾動式驗證 → walk_forward='120,20'", b.ok && b.req.walk_forward === "120,20");
  const bad = (label, patch, msg) => { const r = buildRequest({ ...DEFAULT_FORM, ...patch }); check(`a3 ${label} → 擋下`, !r.ok && (!msg || r.error.includes(msg)), J(r)); };
  bad("成本 -1", { cost: "-1" }, "成本"); bad("成本 51", { cost: "51" }, "成本"); bad("成本空白", { cost: "" }, "成本"); bad("成本 abc", { cost: "abc" }, "成本");
  bad("樣本內 0.2", { split: "0.2" }, "樣本內"); bad("樣本內 0.95", { split: "0.95" }, "樣本內");
  bad("對照 13 組", { nullN: "13" }, "對照"); bad("對照 1.5 組", { nullN: "1.5" }, "對照"); bad("前 0 名", { top: "0" }, "前幾名"); bad("前 11 名", { top: "11" }, "前幾名");
  bad("訓練 10 天", { wf: true, wfTrain: "10" }, "訓練"); bad("訓練 401 天", { wf: true, wfTrain: "401" }, "訓練"); bad("測試 4 天", { wf: true, wfTest: "4" }, "測試");
  bad("測試 ≥ 訓練", { wf: true, wfTrain: "30", wfTest: "30" }, "小於"); bad("日盤 K 棒選夜盤", { source: "bars", session: "night" }, "日盤");
  check("a4 成交彙總＋夜盤 → 合法", buildRequest({ ...DEFAULT_FORM, source: "flow", session: "night" }).ok);
  check("a5 沒勾滾動式驗證時，訓練／測試天數不檢查", buildRequest({ ...DEFAULT_FORM, wf: false, wfTrain: "abc" }).ok);

  // ── verdict ──
  const v = (r) => verdict(r);
  check("b1 沒做滾動式驗證 → 灰，提醒別下結論", v(report()).level === "gray" && v(report()).text.includes("先別下結論"));
  check("b2 樣本外 t≤-2 → 紅", v(report({ walk_forward: wf(-2.39) })).level === "red");
  check("b3 t≥2 且勝過全部打亂資料 → 綠，但寫明不是上線依據",
    (() => { const x = v(report({ walk_forward: wf(2.4), null: [{ best_is_t: 1, oos_t_of_best: 0, best_all_t: 1, wf_t: 1.2 }, { best_is_t: 1, oos_t_of_best: 0, best_all_t: 1, wf_t: -0.5 }] })); return x.level === "green" && x.text.includes("不是上線依據"); })());
  check("b4 t≥2 但沒勝過所有打亂資料 → 灰",
    v(report({ walk_forward: wf(2.4), null: [{ best_is_t: 1, oos_t_of_best: 0, best_all_t: 1, wf_t: 3.1 }] })).level === "gray");
  check("b5 t≥2 但沒做打亂資料對照 → 灰（不能只憑一個數字就亮綠燈）", v(report({ walk_forward: wf(2.4) })).level === "gray");
  check("b6 t=1.9 → 灰，寫『看不出比亂數強』", (() => { const x = v(report({ walk_forward: wf(1.9), null: [{ best_is_t: 1, oos_t_of_best: 0, best_all_t: 1, wf_t: -1 }] })); return x.level === "gray" && x.text.includes("看不出比亂數強"); })());
  check("b7 t≥2 但每天平均是負的（不合理的組合）→ 不亮綠燈", v(report({ walk_forward: wf(2.4, -3), null: [{ best_is_t: 0, oos_t_of_best: 0, best_all_t: 0, wf_t: 0 }] })).level !== "green");
  check("b8 結論文字帶出數字", (() => { const x = v(report({ walk_forward: wf(-2.39, -87.2), null: [{ best_is_t: 0, oos_t_of_best: 0, best_all_t: 0, wf_t: -0.8 }, { best_is_t: 0, oos_t_of_best: 0, best_all_t: 0, wf_t: 1.3 }] })); return x.text.includes("-87.2") && x.text.includes("-2.39") && x.text.includes("-0.80") && x.text.includes("+1.30"); })(), v(report({ walk_forward: wf(-2.39, -87.2) })).text);

  // ── 其他純函式 ──
  check("c1 topRows 依樣本內 t 由大到小", J(topRows(report(), 2).map((r) => r.label)) === J(["orb B", "orb C"]));
  check("c2 topRows 不改動原本的順序", (() => { const r = report(); topRows(r, 2); return r.rows[0].label === "orb A"; })());
  check("c3 格式", fmtPts(1.234) === "+1.2" && fmtPts(-4) === "-4.0" && fmtPts(null) === "—" && fmtPts(NaN) === "—" && fmtT(2.345) === "+2.35" && fmtPct(0.456) === "46%" && fmtPct(undefined) === "—");

  // ── 面板：點擊 ──
  calls.length = 0; impl = { run: async () => ({ id: "ab12cd34" }), job: async () => job({ status: "running", progress: { done: 0, total: 1 }, reports: null }) };
  let { tree, sets } = render({});
  await button(tree, "執行回測").props.onClick(); await tick();
  check("d1 按執行 → 以驗證過的請求呼叫 run，接著取工作狀態", J(calls.map((c) => c[0])) === J(["run", "job"]) && calls[0][1].strategy === "orb" && calls[0][1].walk_forward === null && calls[1][1] === "ab12cd34", J(calls));
  check("d2 → 工作放進狀態", sets.some((s) => s[0] === "job" && s[1] && s[1].id === "ab12cd34"));

  calls.length = 0; ({ tree, sets } = render({ 0: { ...DEFAULT_FORM, cost: "abc" } }));
  await button(tree, "執行回測").props.onClick(); await tick();
  check("d3 表單不合法 → 不呼叫 API，顯示錯誤", calls.length === 0 && sets.some((s) => s[0] === "err" && s[1].includes("成本")), J([calls, sets]));

  calls.length = 0; impl = { run: async () => { throw new Error('409 — {"detail":"已經有一個回測在跑，等它跑完（或先取消）"}'); } };
  ({ tree, sets } = render({}));
  await button(tree, "執行回測").props.onClick(); await tick();
  check("d4 後端 409 → 顯示後端的說明（不是原始 JSON）", sets.some((s) => s[0] === "err" && s[1] === "已經有一個回測在跑，等它跑完（或先取消）"), J(sets));
  check("d5 跑完後 busy 回到 false", sets.filter((s) => s[0] === "busy").map((s) => s[1]).join() === "true,false");

  calls.length = 0; impl = { cancel: async () => ({ cancelled: true }), job: async () => job({ status: "cancelled" }) };
  ({ tree, sets } = render({ 2: job({ status: "running", progress: { done: 2, total: 7 }, reports: null, elapsed_sec: 12 }) }));
  check("e1 執行中：按鈕顯示『回測進行中…』並停用、出現取消鈕", button(tree, "回測進行中").props.disabled === true && !!button(tree, "取消"));
  check("e2 執行中：顯示已跑秒數與完成數", text(tree).includes("已跑 12 秒 · 完成 2／7 個策略"), text(tree).slice(0, 400));
  await button(tree, "取消").props.onClick(); await tick();
  check("e3 按取消 → 呼叫 cancel 並重取工作", J(calls.map((c) => c[0])) === J(["cancel", "job"]) && calls[0][1] === "ab12cd34", J(calls));
  check("e4 沒在跑的時候沒有取消鈕", !button(render({}).tree, "取消"));

  // ── 面板：各種工作狀態的畫面 ──
  check("f1 失敗 → 顯示錯誤", text(render({ 2: job({ status: "failed", error: "boom: 資料不夠", reports: null }) }).tree).includes("回測失敗：boom: 資料不夠"));
  check("f2 取消 → 顯示已取消", text(render({ 2: job({ status: "cancelled", reports: null }) }).tree).includes("已取消"));
  const cards = find(render({ 2: job({ reports: [report({ walk_forward: wf(0.4) })] }) }).tree, (e) => typeof e.type === "function" && e.type.name === "ReportCard");
  check("f3 完成 → 每個策略一張卡，帶著表格要顯示的前幾名數", cards.length === 1 && cards[0].props.r.strategy === "orb" && cards[0].props.top === 3);
  check("f4 沒有工作 → 不顯示結果區", !text(render({}).tree).includes("共跑"));
  check("f5 『怎麼讀』說明在", text(render({}).tree).includes("樣本內最好的組合，成績本來就會被高估"));
  check("f6 面板上（不用展開）有一行說明為什麼沒有 scalp，並指到〈風險〉面板", (() => {
    const ps = find(render({}).tree, (e) => e.type === "p" && text(e.props.children).trim().startsWith("沒有 scalp"));
    return ps.length === 1 && text(ps[0].props.children).includes("風險") && text(ps[0].props.children).includes("1 分 K");
  })());
  check("f7 『怎麼讀』有完整的理由（訊號是逐筆、限價單、停損先到的偏差、逐筆資料太少）", (() => {
    const li = find(render({}).tree, (e) => e.type === "li" && text(e.props.children).includes("為什麼沒有 scalp"));
    const t = li.length === 1 ? text(li[0].props.children) : "";
    return ["最近 20 筆成交", "限價單", "停損先到", "逐筆資料", "風險"].every((k) => t.includes(k));
  })());

  // ── 面板：表單連動 ──
  ({ tree, sets } = render({ 0: { ...DEFAULT_FORM, source: "flow", session: "night" } }));
  const selects = find(tree, (e) => e.type === "select");
  const sourceSel = selects.find((s) => text(s.props.children).includes("日盤 K 棒"));
  const sessionSel = selects.find((s) => text(s.props.children).includes("日盤＋夜盤"));
  check("g1 資料來源是日盤 K 棒時，時段選單停用；成交彙總時才能選", sessionSel.props.disabled === false && render({}).tree && find(render({}).tree, (e) => e.type === "select").find((s) => text(s.props.children).includes("日盤＋夜盤")).props.disabled === true);
  sourceSel.props.onChange({ target: { value: "bars" } });
  check("g2 切回日盤 K 棒 → 時段自動回到日盤（否則會送出不合法的組合）", sets.some((s) => s[0] === "form" && s[1].source === "bars") && sets.filter((s) => s[0] === "form").pop()[1].session === "day", J(sets));

  console.log(fail ? `\n${fail} 項失敗` : "\n全部通過"); process.exit(fail ? 1 : 0);
})();
