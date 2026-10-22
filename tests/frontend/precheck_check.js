// 〈下單前檢查〉卡：純函式與畫面（靜態渲染）。資料由 tests/test_frontend_precheck.py 用 core/precheck.py 現算（PC_FIXTURES）。
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
const fx = JSON.parse(fs.readFileSync(path.join(process.env.PC_FIXTURES, "fixtures.json"), "utf8"));
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "precheck-check-")));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });
const tr = (src, out) => { fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText); return require(out); };
const map = { "@/lib/precheck": path.join(TMP, "precheck.js"), "@/lib/api": path.join(TMP, "api.js") };
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) { return map[req] || orig.call(this, req, ...r); };
const L = tr(path.join(FRONT, "lib", "precheck.ts"), path.join(TMP, "precheck.js"));
tr(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "api.js"));
const C = tr(path.join(FRONT, "components", "PrecheckCard.tsx"), path.join(TMP, "card.js"));
let fail = 0;
const check = (n, ok, x = "") => { console.log((ok ? "PASS " : "FAIL ") + n + (ok ? "" : "  " + String(x).slice(0, 600))); if (!ok) fail++; };
const html = (el) => renderToStaticMarkup(el).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
const BAD = /undefined|NaN|\bnull\b|Infinity/;
const f = fx.full, e0 = fx.empty, q = fx.quiet;
check("預期振幅那一行", L.rangeLine(f) === "今天預期日盤振幅約 682 點（68% 區間 489～1,000；近 20 日均 542）" && L.rangeLine(e0).startsWith("今天預期日盤振幅：還沒有"), L.rangeLine(f));
check("IV 那一行（百分位、沒有百分位、沒有數字）", L.ivLine(f) === "隱含波動 27.1%，在過去一年是第 85 百分位（偏高）" && L.ivHigh(f) && L.ivLine(q) === "隱含波動 18.0%（累積中）" && !L.ivHigh(q) && L.ivLine(e0) === "隱含波動：還沒有今天的數字", [L.ivLine(f), L.ivLine(q)]);
check("事件：有、沒有、讀不到", L.eventLines(f)[0].startsWith("今晚 21:30 美國 CPI：歷史上隔天日盤開盤跳空約平常的 1.6 倍") && L.eventLines(q)[0] === "36 小時內沒有美國重要事件（CPI、非農、FOMC）" && L.eventLines(e0)[0] === "重要事件：讀不到 lh-intel 的事件行事曆", L.eventLines(f));
for (const [n, p] of Object.entries(fx)) {
  const t = html(React.createElement(C.PrecheckBody, { p }));
  check(`畫面(${n})：沒有怪值`, !BAD.test(t), t);
}
const ev = fx.evening, wt = fx.waiting, br = fx.broken;
check("白天沒有夜盤那一行", L.nightLine(f) === null && L.nextDayLine(f) === null && L.showDayLine(f));
check("傍晚：夜盤與下一個日盤", L.nightLine(ev) === "11/10 晚上的夜盤預期振幅約 899 點（68% 區間 600～1,364；近 20 個夜盤平均 684）"
  && L.nextDayLine(ev) === "下一個日盤預期振幅約 693 點（68% 區間 497～1,017；近 20 日均 580）", [L.nightLine(ev), L.nextDayLine(ev)]);
check("傍晚：今天日盤的『還沒有』不顯示", !L.showDayLine(ev) && !html(React.createElement(C.PrecheckBody, { p: ev })).includes("今天預期日盤振幅"));
check("等資料時寫原因", L.nightLine(wt).startsWith("11/10 晚上的夜盤預期振幅：等 11-10 收盤") && L.nextDayLine(wt).startsWith("下一個日盤預期振幅：等 11-10 收盤"), [L.nightLine(wt), L.nextDayLine(wt)]);
check("計算失敗只影響那一行", L.nightLine(br) === "夜盤預期振幅：計算失敗（細節在後端 log）" && L.nextDayLine(br) === null && html(React.createElement(C.PrecheckBody, { p: br })).includes("隱含波動"));
check("夜盤那一行有月亮", html(React.createElement(C.PrecheckBody, { p: ev })).includes("🌙 11/10 晚上的夜盤"));
check("預測驗證：還不夠", L.hitsLine(fx.hits_few) === "預測驗證：累積中（日盤 3 天、夜盤 1 晚，滿 5 筆才算命中率）", L.hitsLine(fx.hits_few));
check("預測驗證：日盤夠了、夜盤還不夠", L.hitsLine(fx.hits_ok) === "預測驗證：日盤近 20 天有 14 天落在 68% 區間內、夜盤累積中（3 晚）（模型說應約 68%）", L.hitsLine(fx.hits_ok));
check("沒有命中率資料就不顯示那一行", L.hitsLine(f) === null && html(React.createElement(C.PrecheckBody, { p: fx.hits_ok })).includes("預測驗證"));
const raw = renderToStaticMarkup(React.createElement(C.PrecheckBody, { p: f }));
check("IV 偏高與有事件時標黃色、有 📅", (raw.match(/color:#ffc107/g) || []).length >= 2 && raw.includes("📅"), raw);
check("沒事件時不標黃", !renderToStaticMarkup(React.createElement(C.PrecheckBody, { p: q })).includes("📅"));
console.log(fail ? `\n${fail} 項失敗` : "\n全部通過");
process.exit(fail ? 1 : 0);
