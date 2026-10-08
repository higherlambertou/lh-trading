// 策略面板「停止」按鈕的點擊邏輯與確認框。前端沒有 DOM 測試工具，所以：把 React hooks 換成記錄用的替身，
// 直接呼叫元件函式取得元素樹，找按鈕、呼叫 onClick，檢查 API 呼叫與 state 變更。由 tests/test_frontend_stop_confirm.py 執行。
// 用法：node tests/frontend/stop_confirm_check.js   （需要 frontend/node_modules）
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "stop-confirm-"));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });

const transpile = (src, out) => {
  const js = ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText;
  fs.writeFileSync(out, js);
  return require(out);
};

// 真正的 lib/api.ts（要測 apiErrorDetail），但 api 物件換成記錄呼叫的替身
const realApi = transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "real_api.js"));
const calls = [];
let stopImpl = async () => ({ status: "stopped", name: "scalp" });
global.__api_stub = { api: { strategy: { list: async () => [], start: async () => ({}), stop: (...a) => { calls.push(a); return stopImpl(...a); } } },
                      apiErrorDetail: realApi.apiErrorDetail };
fs.writeFileSync(path.join(TMP, "api_stub.js"), "module.exports = global.__api_stub;");
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) { return req === "@/lib/api" ? path.join(TMP, "api_stub.js") : orig.call(this, req, ...r); };
const panel = transpile(path.join(FRONT, "components", "StrategyPanel.tsx"), path.join(TMP, "panel.js"));
const Comp = panel.default, reconcile = panel.reconcileStopConfirm;

const NAMES = ["strategies", "selected", "editParams", "busy", "confirmStop", "msg"];
function render(forced) {
  const sets = [];
  let n = 0;
  React.useState = (init) => { const i = n++; const v = Object.prototype.hasOwnProperty.call(forced, i) ? forced[i] : init;
    return [v, (x) => sets.push([NAMES[i], typeof x === "function" ? "fn" : x])]; };
  React.useEffect = () => {}; React.useCallback = (f) => f;
  return { tree: Comp(), sets };
}
function find(node, pred, out = []) {
  if (Array.isArray(node)) node.forEach((c) => find(c, pred, out));
  else if (node && typeof node === "object" && node.props) { if (pred(node)) out.push(node); find(node.props.children, pred, out); }
  return out;
}
const text = (node) => (Array.isArray(node) ? node.map(text).join("") : typeof node === "string" || typeof node === "number" ? String(node) : node && node.props ? text(node.props.children) : "");
const button = (tree, label) => find(tree, (e) => e.type === "button" && text(e.props.children).trim() === label)[0];
const strat = (position) => [{ name: "scalp", is_running: true, position, entry_price: 49500, last_price: 49520, unrealized_pnl: 0,
  realized_pnl: 0, errors: [], events: [], params: { daily_max_loss: 0, max_trades_per_day: 0 }, param_schema: [] }];
const tick = () => new Promise((r) => setTimeout(r, 5));
const J = JSON.stringify;
const BROKER_DETAIL = "策略 scalp 沒有記錄持倉，但券商帳上有 TMF 多 1 口（總 1 口，均價 48981）（可能是策略沒認出自己的成交，或手動單）。停止會取消報價訂閱，這口部位不會有任何保護。";
const err409 = (detail) => new Error(`409 — ${J({ detail })}`);

let fail = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + extra)); if (!ok) fail++; };

(async () => {
  // a) 畫面上有持倉按「停止」：不呼叫 API，只打開（前端推算的）確認框
  let { tree, sets } = render({ 0: strat(2) });
  await button(tree, "停止").props.onClick(); await tick();
  check("a1 有持倉按停止 → 沒有呼叫 API", calls.length === 0, J(calls));
  check("a2 → 開前端推算的確認框（沒有 detail）", J(sets) === J([["confirmStop", { name: "scalp" }]]), J(sets));

  // b) 空手按「停止」：直接停
  calls.length = 0; ({ tree, sets } = render({ 0: strat(0) }));
  await button(tree, "停止").props.onClick(); await tick();
  check("b1 空手按停止 → stop('scalp', false)", J(calls) === J([["scalp", false]]), J(calls));
  check("b2 成功 → 清掉確認並顯示『已停止』", sets.some((s) => s[0] === "confirmStop" && s[1] === null) && sets.some((s) => s[0] === "msg" && s[1] && s[1].text === "scalp 已停止" && !s[1].warn), J(sets));

  // c) 確認框出現後按「仍要強制停止」
  calls.length = 0; stopImpl = async () => ({ status: "stopped", name: "scalp", warning: "scalp 是在持倉中被強制停止的" });
  ({ tree, sets } = render({ 0: strat(2), 4: { name: "scalp" } }));
  check("c0 確認框有兩個按鈕", !!button(tree, "仍要強制停止") && !!button(tree, "取消"));
  await button(tree, "仍要強制停止").props.onClick(); await tick();
  check("c1 → stop('scalp', true)", J(calls) === J([["scalp", true]]), J(calls));
  check("c2 後端 warning 以警告樣式顯示", sets.some((s) => s[0] === "msg" && s[1] && s[1].warn === true && s[1].text.includes("強制停止")), J(sets));

  // d) 取消
  calls.length = 0; ({ tree, sets } = render({ 0: strat(2), 4: { name: "scalp" } }));
  await button(tree, "取消").props.onClick(); await tick();
  check("d1 取消 → 不呼叫 API、關掉確認框", calls.length === 0 && J(sets) === J([["confirmStop", null]]), J([calls, sets]));

  // e) 畫面上空手，但後端 409（部位晚到，或券商帳上有部位策略不知道）→ 轉成確認框，帶後端的說明，不跳紅字
  calls.length = 0; stopImpl = async () => { throw err409(BROKER_DETAIL); };
  ({ tree, sets } = render({ 0: strat(0) }));
  await button(tree, "停止").props.onClick(); await tick();
  check("e1 後端 409 → 確認框帶著後端的說明", sets.some((s) => s[0] === "confirmStop" && s[1] && s[1].name === "scalp" && s[1].detail === BROKER_DETAIL), J(sets));
  check("e2 不跳紅字錯誤", !sets.some((s) => s[0] === "msg" && s[1] && s[1].ok === false), J(sets));

  // f) 確認框顯示條件
  const shown = (forced) => { const r = render(forced); return !!button(r.tree, "仍要強制停止") ? r.tree : null; };
  check("f1 空手＋前端推算的確認框 → 不顯示", !shown({ 0: strat(0), 4: { name: "scalp" } }));
  check("f2 別的策略名 → 不顯示", !shown({ 0: strat(2), 4: { name: "orb" } }));
  check("f3 沒按停止 → 不顯示", !shown({ 0: strat(2) }));
  let t = shown({ 0: strat(0), 4: { name: "scalp", detail: BROKER_DETAIL } });
  check("f4 空手＋後端說明（券商帳上有部位）→ 顯示，內容是後端的說明", !!t && text(t).includes(BROKER_DETAIL) && text(t).includes("確定要停止？"));
  t = shown({ 0: strat(2), 4: { name: "scalp", detail: BROKER_DETAIL } });
  check("f5 有持倉＋後端說明 → 以後端說明為準，不再重複前端推算的條列", !!t && text(t).includes(BROKER_DETAIL) && !text(t).includes("建議先用〈手動下單〉"));
  t = shown({ 0: strat(2), 4: { name: "scalp" } });
  check("f6 前端推算的確認框 → 顯示持有方向／口數／進場價與後果", !!t && text(t).includes("目前持有多 2 口") && text(t).includes("49,500") && text(t).includes("不再檢查停損停利"));

  // g) 後端說明的確認框裡按「仍要強制停止」→ 帶 force
  calls.length = 0; stopImpl = async () => ({ status: "stopped", name: "scalp", warning: "scalp 是在券商帳上有持倉時被強制停止的" });
  ({ tree, sets } = render({ 0: strat(0), 4: { name: "scalp", detail: BROKER_DETAIL } }));
  await button(tree, "仍要強制停止").props.onClick(); await tick();
  check("g1 → stop('scalp', true)", J(calls) === J([["scalp", true]]), J(calls));
  check("g2 成功後收起確認框", sets.some((s) => s[0] === "confirmStop" && s[1] === null), J(sets));

  // h) 強制停止失敗：顯示錯誤，不再開確認框（避免無限確認）
  calls.length = 0; stopImpl = async () => { throw new Error("500 — boom"); };
  ({ tree, sets } = render({ 0: strat(2), 4: { name: "scalp" } }));
  await button(tree, "仍要強制停止").props.onClick(); await tick();
  check("h1 force 失敗 → 紅字錯誤", sets.some((s) => s[0] === "msg" && s[1] && s[1].ok === false && s[1].text.startsWith("500")), J(sets));
  stopImpl = async () => { throw err409("x"); };
  ({ tree, sets } = render({ 0: strat(2), 4: { name: "scalp" } }));
  await button(tree, "仍要強制停止").props.onClick(); await tick();
  check("h2 force 仍 409 → 顯示錯誤而不是再開確認框", sets.some((s) => s[0] === "msg" && s[1] && s[1].ok === false) && !sets.some((s) => s[0] === "confirmStop" && s[1]), J(sets));

  // i) 輪詢後確認框的去留
  const R = (c, running) => J(reconcile(c, running));
  check("i1 沒有確認框 → 不變", R(null, { name: "scalp", position: 1 }) === "null");
  check("i2 策略停了 → 收起（含後端說明的）", R({ name: "scalp", detail: "x" }, undefined) === "null");
  check("i3 換了另一個策略 → 收起", R({ name: "scalp" }, { name: "orb", position: 1 }) === "null");
  check("i4 前端推算的確認框，空手後 → 收起", R({ name: "scalp" }, { name: "scalp", position: 0 }) === "null");
  check("i5 前端推算的確認框，仍持倉 → 保留", R({ name: "scalp" }, { name: "scalp", position: 1 }) === J({ name: "scalp" }));
  check("i6 後端說明的確認框，策略仍是空手 → 保留（部位在券商帳上）", R({ name: "scalp", detail: "x" }, { name: "scalp", position: 0 }) === J({ name: "scalp", detail: "x" }));

  // j) 把 `409 — {"detail":"…"}` 還原成後端的說明
  const D = realApi.apiErrorDetail;
  check("j1 JSON detail", D(err409("策略 scalp 目前持有多 1 口")) === "策略 scalp 目前持有多 1 口");
  check("j2 非 JSON 主體原樣回傳", D(new Error("500 — Internal Server Error")) === "Internal Server Error");
  check("j3 沒有狀態碼前綴", D(new Error("network down")) === "network down" && D("plain") === "plain");
  check("j4 JSON 但 detail 不是字串 → 回傳原文", D(new Error('422 — {"detail":[{"msg":"bad"}]}')) === '{"detail":[{"msg":"bad"}]}');

  console.log(fail ? `\n${fail} 項失敗` : "\n全部通過"); process.exit(fail ? 1 : 0);
})();
