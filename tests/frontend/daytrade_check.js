// 手動下單面板的「當沖」勾選：只有小台／大台看得到、微台不能帶著勾選送出、送出的倉別對不對。
// 前端沒有 DOM 測試工具，做法同 stop_confirm_check.js：hooks 換成替身、直接呼叫元件函式取得元素樹、呼叫 onClick／onChange。
// 由 tests/test_frontend_daytrade.py 執行。用法：node tests/frontend/daytrade_check.js（需要 frontend/node_modules）
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "daytrade-check-"));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });

const transpile = (src, out) => {
  const js = ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText;
  fs.writeFileSync(out, js);
  return require(out);
};

// ── api 換成記錄呼叫的替身（isSimMode=true：模擬盤不用兩段式確認，一次點擊就送）──
const calls = [];
const spy = (name) => (...a) => { calls.push([name, ...a]); return Promise.resolve({ trade_id: "T1", status: "PendingSubmit", code: "X", limit_price: 1 }); };
global.__api_stub = { api: { order: { place: spy("place"), placeOption: spy("placeOption"), optionExpiries: spy("expiries"), optionStrikes: spy("strikes"), optionQuote: spy("quote") },
                             quote: { last: () => Promise.resolve({}) } },
                      isSimMode: () => true, notifyOrderPlaced: () => {} };
fs.writeFileSync(path.join(TMP, "api_stub.js"), "module.exports = global.__api_stub;");
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) { return req === "@/lib/api" ? path.join(TMP, "api_stub.js") : orig.call(this, req, ...r); };
const Comp = transpile(path.join(FRONT, "components", "OrderPanel.tsx"), path.join(TMP, "panel.js")).default;

// 狀態順序必須和元件裡 useState 的宣告順序一致
const NAMES = ["mode", "action", "quantity", "orderType", "limitPrice", "stopLoss", "takeProfit", "busy", "result", "armed",
               "contract", "priceType", "futPrice", "dayTrade",
               "right", "expiries", "deliveryMonth", "strikes", "strike", "exitBuffer", "optLoading", "optErr", "optQuote", "underlying"];
const IDX = Object.fromEntries(NAMES.map((n, i) => [n, i]));
function render(forced = {}) {
  const sets = [];
  let n = 0;
  React.useState = (init) => {
    const i = n++; const v = Object.prototype.hasOwnProperty.call(forced, NAMES[i]) ? forced[NAMES[i]] : init;
    return [v, (x) => sets.push([NAMES[i], typeof x === "function" ? x(v) : x])];
  };
  React.useEffect = () => {}; React.useCallback = (f) => f; React.useRef = (v) => ({ current: v });
  const tree = Comp();
  if (n !== NAMES.length) throw new Error(`useState 數量 ${n} ≠ 測試裡的 ${NAMES.length}：元件的 state 增減了，請同步更新 NAMES`);
  return { tree, sets };
}
function find(node, pred, out = []) {
  if (Array.isArray(node)) node.forEach((c) => find(c, pred, out));
  else if (node && typeof node === "object" && node.props) { if (pred(node)) out.push(node); find(node.props.children, pred, out); }
  return out;
}
const text = (node) => (Array.isArray(node) ? node.map(text).join("") : typeof node === "string" || typeof node === "number" ? String(node) : node && node.props ? text(node.props.children) : "");
const buttons = (tree) => find(tree, (e) => e.type === "button");
const button = (tree, label) => buttons(tree).find((e) => text(e.props.children).trim().startsWith(label));
const checkbox = (tree) => find(tree, (e) => e.type === "input" && e.props.type === "checkbox")[0];
const submit = (tree) => buttons(tree).find((e) => text(e.props.children).trim().startsWith("確認") || text(e.props.children).includes("送出中") || text(e.props.children).includes("真錢下單"));
const tick = () => new Promise((r) => setTimeout(r, 5));
const J = JSON.stringify;
let fail = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + extra)); if (!ok) fail++; };
const lastPlace = () => [...calls].reverse().find((c) => c[0] === "place")?.[1];

(async () => {
  // ── 看得到什麼 ──
  for (const c of ["MXF", "TXF"]) {
    const { tree } = render({ contract: c });
    const cb = checkbox(tree);
    check(`${c}：有當沖勾選，預設沒勾`, !!cb && cb.props.checked === false, J(cb && cb.props));
    check(`${c}：沒勾的時候不顯示注意事項`, !text(tree).includes("只限日盤"));
    check(`${c}：不顯示「微台沒有當沖」`, !text(tree).includes("微台沒有當沖"));
  }
  {
    const { tree } = render({ contract: "TMF" });
    check("TMF：沒有勾選，改顯示微台沒有當沖保證金減半", !checkbox(tree) && text(tree).includes("微台沒有當沖保證金減半"));
  }
  {
    const { tree } = render({ contract: "MXF", dayTrade: true });
    const t = text(tree);
    check("勾了當沖：顯示注意事項（日盤、開倉平倉都要勾、收盤前沖掉、永豐資格）",
      ["只限日盤 08:45–13:45", "開倉和平倉都要勾", "強制沖銷", "永豐"].every((s) => t.includes(s)), t.slice(0, 400));
    check("勾了當沖：送出鈕標示【當沖】", text(submit(tree).props.children).includes("【當沖】"));
    check("勾選框反映狀態", checkbox(tree).props.checked === true);
  }
  {
    const { tree } = render({ mode: "option" });
    check("選擇權模式：沒有當沖勾選也沒有微台說明", !checkbox(tree) && !text(tree).includes("微台沒有當沖"));
  }
  {
    const { tree } = render({ mode: "option", contract: "MXF", dayTrade: true });     // 上次期貨選的是小台：切到選擇權也不能出現當沖
    check("選擇權模式（上次期貨選小台）：沒有當沖勾選與注意事項", !checkbox(tree) && !text(tree).includes("只限日盤"));
  }

  // ── 點勾選框 ──
  {
    const { tree, sets } = render({ contract: "MXF" });
    checkbox(tree).props.onChange({ target: { checked: true } });
    check("點勾選框 → 設成 true", J(sets) === J([["dayTrade", true]]), J(sets));
  }
  {
    const { tree, sets } = render({ contract: "MXF", dayTrade: true });
    checkbox(tree).props.onChange({ target: { checked: false } });
    check("取消勾選 → 設成 false", J(sets) === J([["dayTrade", false]]), J(sets));
  }

  // ── 換合約 ──
  {
    const { tree, sets } = render({ contract: "MXF", dayTrade: true });
    button(tree, "TMF").props.onClick();
    check("勾著當沖時換到微台 → 取消勾選（不帶著過去）", J(sets) === J([["contract", "TMF"], ["dayTrade", false]]), J(sets));
  }
  for (const c of ["MXF", "TXF"]) {
    const { tree, sets } = render({ contract: "MXF", dayTrade: true });
    button(tree, c).props.onClick();
    check(`勾著當沖時換到${c} → 勾選保留`, J(sets) === J([["contract", c]]), J(sets));
  }

  // ── 送出的倉別 ──
  const sendWith = async (forced) => { calls.length = 0; const { tree } = render(forced); await submit(tree).props.onClick(); await tick(); return lastPlace(); };
  for (const c of ["MXF", "TXF"]) {
    const p = await sendWith({ contract: c, dayTrade: true });
    check(`${c} 勾當沖 → octype=DayTrade，合約不變`, p && p.octype === "DayTrade" && p.contract === c, J(p));
  }
  for (const c of ["MXF", "TXF", "TMF"]) {
    const p = await sendWith({ contract: c });
    check(`${c} 沒勾 → octype=Auto（跟以前一樣）`, p && p.octype === "Auto" && p.contract === c, J(p));
  }
  {
    const p = await sendWith({ contract: "TMF", dayTrade: true });          // 殘留的 true（例如以程式改狀態）也不能送當沖
    check("微台就算 dayTrade 殘留 true 也送 Auto", p && p.octype === "Auto" && p.contract === "TMF", J(p));
    const { tree } = render({ contract: "TMF", dayTrade: true });
    check("微台殘留 true 時送出鈕不標【當沖】", !text(submit(tree).props.children).includes("當沖"));
  }
  {
    const p = await sendWith({ contract: "MXF", dayTrade: true, stopLoss: "30", takeProfit: "50", action: "Sell", quantity: 2, priceType: "LMT", limitPrice: "49000", orderType: "ROD" });
    check("當沖單的其他欄位照常帶出（停損停利、口數、限價）",
      p && p.octype === "DayTrade" && p.stop_loss_pts === 30 && p.take_profit_pts === 50 && p.quantity === 2 && p.action === "Sell" && p.price === 49000 && p.price_type === "LMT", J(p));
  }
  {
    calls.length = 0;
    const { tree } = render({ mode: "option", deliveryMonth: "202610", strike: 49800, limitPrice: "120", dayTrade: true });
    await submit(tree).props.onClick(); await tick();
    const o = calls.find((c) => c[0] === "placeOption");
    check("選擇權下單不帶 octype（就算 dayTrade 殘留 true）", !!o && !("octype" in o[1]), J(o));
  }

  console.log(fail ? `\n${fail} 項失敗` : "\n全部通過");
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
