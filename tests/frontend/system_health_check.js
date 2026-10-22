// 系統健康面板（frontend/components/SystemHealthPanel.tsx、lib/systemHealth.ts）：用 core/system_health.py 現算的資料渲染（tests/test_frontend_system_health.py）。
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
const fx = JSON.parse(fs.readFileSync(path.join(process.env.SH_FIXTURES, "fixtures.json"), "utf8"));
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "sh-check-")));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });
const tr = (src, out) => { fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText); return require(out); };
const map = { "@/lib/systemHealth": path.join(TMP, "sh.js"), "@/lib/api": path.join(TMP, "api.js"), "./systemHealth": path.join(TMP, "sh.js"), "./chart": path.join(TMP, "chart.js") };
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) { return map[req] || orig.call(this, req, ...r); };
const L = tr(path.join(FRONT, "lib", "systemHealth.ts"), map["@/lib/systemHealth"]);
fs.writeFileSync(map["./chart"], "module.exports={};");
tr(path.join(FRONT, "lib", "api.ts"), map["@/lib/api"]);
const C = tr(path.join(FRONT, "components", "SystemHealthPanel.tsx"), path.join(TMP, "panel.js"));
let fail = 0;
const check = (n, ok, x = "") => { console.log((ok ? "PASS " : "FAIL ") + n + (ok ? "" : "  " + String(x).slice(0, 600))); if (!ok) fail++; };
const html = (el) => renderToStaticMarkup(el).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
check("全部正常", L.summaryLine(fx.good) === "全部正常", L.summaryLine(fx.good));
check("有異常也有注意", L.summaryLine(fx.mixed) === "1 項異常：lh-intel 資訊收集；2 項要注意：自動重啟（近 7 天）、資料庫備份", L.summaryLine(fx.mixed));
for (const [n, h] of Object.entries(fx)) {
  const t = html(React.createElement(C.SystemHealthBody, { h }));
  check(`畫面(${n})：六項都在、沒有怪值`, ["券商連線", "自動重啟", "資料庫備份", "期交所資料", "lh-intel", "磁碟與 log"].every((k) => t.includes(k)) && !/undefined|NaN|\bnull\b|Infinity/.test(t), t);
}
const raw = renderToStaticMarkup(React.createElement(C.SystemHealthBody, { h: fx.mixed }));
check("顏色跟著狀態走", raw.includes("background-color:#ff1744") && raw.includes("background-color:#ffc107") && raw.includes("background-color:#00e676"), raw);
check("滑過去看細節（title）", raw.includes('title="'), raw);
console.log(fail ? `\n${fail} 項失敗` : "\n全部通過");
process.exit(fail ? 1 : 0);
