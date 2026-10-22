// 〈已驗證無效〉的收合面板（frontend/components/Folded.tsx）＋分頁上的使用位置：預設收起時不掛載（或 keepMounted 只藏起來）、記錄讀寫失敗當作沒記、原因一定寫出來。
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "folded-check-")));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });
const tr = (src, out) => { fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText); return require(out); };
const M = tr(path.join(FRONT, "components", "Folded.tsx"), path.join(TMP, "folded.js"));
const F = M.default;
let fail = 0;
const check = (n, ok, x = "") => { console.log((ok ? "PASS " : "FAIL ") + n + (ok ? "" : "  " + String(x).slice(0, 600))); if (!ok) fail++; };
const Child = () => React.createElement("div", null, "CHILD-BODY");
const r = (p) => renderToStaticMarkup(React.createElement(F, { id: "x", title: "自動策略", why: "回測沒有優勢", ...p }, React.createElement(Child)));
const closed = r({});
check("收起：標題、僅供參考、原因、提示展開；內容不掛載", closed.includes("自動策略") && closed.includes("僅供參考") && closed.includes("回測沒有優勢") && closed.includes("按一下展開") && !closed.includes("CHILD-BODY") && closed.includes('aria-expanded="false"'), closed);
const kept = r({ keepMounted: true });
check("keepMounted 收起：內容還在、但藏起來", kept.includes("CHILD-BODY") && kept.includes('class="hidden"'), kept);
const open = r({ defaultOpen: true });
check("預設展開：內容顯示、不提示展開", open.includes("CHILD-BODY") && !open.includes("按一下展開") && open.includes('aria-expanded="true"'), open);
const keptOpen = r({ defaultOpen: true, keepMounted: true });
check("keepMounted 展開：不藏", keptOpen.includes("CHILD-BODY") && !keptOpen.includes('class="hidden"'), keptOpen);
// readFold：沒記錄用預設、記錄壞掉或讀取丟例外也用預設
const store = {};
global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };
check("沒記錄 → 預設", M.readFold("a", true) === true && M.readFold("a", false) === false);
store[M.foldKey("a")] = "open"; check("記了 open", M.readFold("a", false) === true);
store[M.foldKey("a")] = "closed"; check("記了 closed", M.readFold("a", true) === false);
store[M.foldKey("a")] = "亂碼"; check("記錄壞掉 → 預設", M.readFold("a", true) === true);
global.localStorage = { getItem: () => { throw new Error("blocked"); } };
check("讀取丟例外 → 預設", M.readFold("a", true) === true && M.readFold("a", false) === false);
// 分頁上的用法：策略面板 keepMounted（收起不能洗掉正在跑的狀態）、正式盤預設收起；回放面板收起時不掛載
const tabs = fs.readFileSync(path.join(FRONT, "components", "DashboardTabs.tsx"), "utf8");
check("策略面板：正式盤預設收起、keepMounted", /<Folded id="strategy"[^>]*defaultOpen=\{isSimMode\(\)\}[^>]*keepMounted/.test(tabs) && /<Folded id="strategy"[\s\S]*?<StrategyPanel \/>\s*<\/Folded>/.test(tabs));
check("回放面板：收起時不掛載", /<Folded id="replay"(?![^>]*keepMounted)[\s\S]*?<IndicatorReplayPanel \/>\s*<\/Folded>/.test(tabs));
console.log(fail ? `\n${fail} 項失敗` : "\n全部通過");
process.exit(fail ? 1 : 0);
