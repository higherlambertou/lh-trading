// 〈機率範圍〉：lib/range.ts 的純函式、RangeSection 的列與容器（載入／輪詢／錯誤）。hooks 換成替身。
// 畫面吃的資料由 tests/test_frontend_range.py 用 core/range_suggest.py＋真正的實測表現算（環境變數 RG_FIXTURES 指到目錄）。
// 用法：RG_FIXTURES=<dir> node tests/frontend/range_check.js（需要 frontend/node_modules）
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
if (!process.env.RG_FIXTURES) { console.error("需要環境變數 RG_FIXTURES（由 tests/test_frontend_range.py 產生）"); process.exit(2); }
const fx = JSON.parse(fs.readFileSync(path.join(process.env.RG_FIXTURES, "fixtures.json"), "utf8"));
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "range-check-")));   // realpath：macOS 的 /var 是 /private/var 的連結，不然同一個模組會被載入兩份
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });
const transpile = (src, out) => {
  fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText);
  return require(out);
};
const map = { "@/lib/range": path.join(TMP, "range.js"), "@/lib/api": path.join(TMP, "api.js") };
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) { return map[req] || orig.call(this, req, ...r); };
const L = transpile(path.join(FRONT, "lib", "range.ts"), path.join(TMP, "range.js"));
const apiMod = transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "api.js"));
const C = transpile(path.join(FRONT, "components", "RangeSection.tsx"), path.join(TMP, "card.js"));
const Section = C.default, e = React.createElement;
let fail = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + String(extra).slice(0, 900))); if (!ok) fail++; };
const strip = (s) => s.replace(/<\/?(div|button)\b[^>]*>/g, " ").replace(/<[^>]+>/g, "").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ");        // div／button 當分隔、行內標籤直接拿掉
const text = (el) => strip(renderToStaticMarkup(el));
const BAD = /undefined|NaN|\bnull\b|Infinity|\[object/;
const lv = (b, p) => b.levels.find((l) => l.p === p);

// ══ 資料前提（後端現算的）═════════════════════════════════════════
const d = fx.day, b15 = d.bands["15"], close = d.bands.close;
check("資料前提：五組、三個層級、落點與期間都有距離與上下緣", L.BAND_KEYS.every((k) => d.bands[k]) && b15.levels.map((l) => l.p).join() === "50,80,95"
  && b15.levels.every((l) => l.term.d > 0 && l.path.d > l.term.d && l.term.lo < d.price && l.term.hi > d.price), JSON.stringify(b15.levels[0]));

// ══ 純函式 ═══════════════════════════════════════════════════════
check("pickBand：有就回、沒有／null 回 null", L.pickBand(d, "15") === b15 && L.pickBand(d, "99") === null && L.pickBand(null, "15") === null && L.pickBand({ bands: {} }, "close") === null);
check("hasClose：只有有到收盤的那份是真", L.hasClose(d) === true && L.hasClose(fx.night) === false && L.hasClose(null) === false);
check("edge：下緣往下取整、上緣往上取整（寧可寬）", L.edge(29979.3, "lo") === 29979 && L.edge(29979.0, "lo") === 29979 && L.edge(30020.1, "hi") === 30021 && L.edge(30020.0, "hi") === 30020 && L.edge(-0.5, "lo") === -1);
check("sideText：整數上下緣＋±距離（一位小數）；沒有就「—」", L.sideText({ d: 20.24, lo: 29979.76, hi: 30020.24 }) === "29,979 ~ 30,021（±20.2）" && L.sideText(null) === "—" && L.sideText(undefined) === "—", L.sideText({ d: 20.24, lo: 29979.76, hi: 30020.24 }));
check("bandText：時間標籤", L.bandText("15") === "15 分" && L.bandText("close") === "到收盤");
check("isBandKey：只認五個", ["5", "15", "30", "60", "close"].every(L.isBandKey) && !L.isBandKey("7") && !L.isBandKey(15) && !L.isBandKey(null));
check("KINDS：落點與期間各有說明", L.KINDS.map((k) => k.id).join() === "term,path" && L.KINDS.every((k) => k.hint.length > 10));
const lines = L.bandLines(d, "15", "term");
check("bandLines：三個層級各上下兩條（共 6 條）、價位就是後端的上下緣", lines.length === 6 && lines.filter((x) => x.upper).length === 3
  && lines.filter((x) => x.upper).every((x) => x.price === lv(b15, x.p).term.hi) && lines.filter((x) => !x.upper).every((x) => x.price === lv(b15, x.p).term.lo), JSON.stringify(lines[0]));
check("bandLines：標題＝層級＋時間（5 分以外也一樣）、期間用 path 的價位", lines[0].title === "50%·15分" && L.bandLines(d, "close", "term")[0].title === "50%·收盤"
  && L.bandLines(d, "15", "path").find((x) => x.p === 80 && x.upper).price === lv(b15, 80).path.hi);
const unst = JSON.parse(JSON.stringify(d)); lv(unst.bands["15"], 80).unstable_term = true;
const ul = L.bandLines(unst, "15", "term");
check("bandLines：不穩的格子標題後面加「?」，其他不加；落點與期間的不穩各看各的", ul.filter((x) => x.title.endsWith("?")).length === 2 && ul.every((x) => x.title.endsWith("?") === (x.p === 80))
  && L.bandLines(unst, "15", "path").every((x) => !x.title.endsWith("?")), JSON.stringify(ul.map((x) => x.title)));
check("bandLines：只畫指定層級、缺值的層級略過、沒有資料回空", L.bandLines(d, "15", "term", [80]).length === 2 && L.bandLines(d, "99", "term").length === 0 && L.bandLines(null, "15", "term").length === 0
  && L.bandLines({ ...d, bands: { "15": { ...b15, levels: [{ ...lv(b15, 50), term: null }, lv(b15, 80)] } } }, "15", "term").length === 2);
check("ageText：剛剛／分鐘前／小時前", L.ageText(30) === "剛剛" && L.ageText(120) === "2 分鐘前" && L.ageText(7200) === "2 小時前" && L.ageText(null) === "");
check("friendlyRangeError：舊後端沒有端點時講白話", L.friendlyRangeError("Not Found").includes("重啟後端") && L.friendlyRangeError("502 x") === "502 x");
check("holdoutText：樣本外驗證通過幾格", L.holdoutText(d.cal) === `樣本外驗證（${d.cal.split} 以前算表、以後量涵蓋率）${d.cal.holdout_ok}／${d.cal.holdout_total} 格通過` && L.holdoutText(null) === "" && L.holdoutText({ holdout_total: 0 }) === "");
check("fmt：千分位、一位小數", L.fmt(30001.1234) === "30,001.1" && L.fmt(5) === "5");

// ══ 列與內容（靜態渲染）══════════════════════════════════════════
const t = text(e(C.RangeBody, { data: d, horizon: 15 }));
check("內容：中心價、落點與期間的說明、15 分鐘與到收盤兩張", t.includes(`以最新成交價 ${L.fmt(d.price)} 為中心`) && t.includes("落點＝15 分鐘後價格落在這裡的機率") && t.includes("期間＝這段時間內最高最低都沒超出的機率")
  && t.includes("15 分鐘") && t.includes(`到收盤（剩 ${close.minutes} 分鐘）`), t.slice(0, 500));
check("內容：每個層級的落點與期間文字（直接用後端的上下緣與距離）", [50, 80, 95].every((p) => t.includes(L.sideText(lv(b15, p).term)) && t.includes(L.sideText(lv(b15, p).path))), t);
check("內容：到收盤那張用到收盤的數字", [50, 80, 95].every((p) => t.includes(L.sideText(lv(close, p).term))), t);
const nUnstable = (b) => b.levels.reduce((n, l) => n + (l.unstable_term ? 1 : 0) + (l.unstable_path ? 1 : 0), 0);
const rawBody = renderToStaticMarkup(e(C.RangeBody, { data: d, horizon: 15 }));
check("內容：「⚠不穩」的個數＝顯示中兩張的不穩格數（到收盤 31~60 分的期間 50%／80% 沒通過驗證）", (rawBody.match(/這一格在樣本外驗證沒通過/g) || []).length === nUnstable(b15) + nUnstable(close) && nUnstable(close) >= 2, `${(rawBody.match(/這一格在樣本外驗證沒通過/g) || []).length} vs ${nUnstable(b15) + nUnstable(close)}`);
check("內容：寫明只說會走多遠、不是進場訊號、歷史實測不是模擬、驗證摘要", t.includes("只說會走多遠、不說往哪個方向") && t.includes("不是進場訊號") && t.includes("歷史實測（不是模擬）") && t.includes(L.holdoutText(d.cal)) && t.includes(`${d.cal.day_sessions.toLocaleString()} 個時段`), t);
check("內容：接近收盤（剩不到 90 分）時多一句提醒", t.includes("接近收盤時價格比最近 30 分鐘的波動更容易放大") && !text(e(C.RangeBody, { data: fx.morning, horizon: 15 })).includes("接近收盤時"));
const tm = text(e(C.RangeBody, { data: fx.morning, horizon: 5 }));
check("早盤：到收盤是 121~270 那組、5 分鐘那張", tm.includes("5 分鐘") && tm.includes(`到收盤（剩 ${fx.morning.bands.close.minutes} 分鐘）`) && [50, 80, 95].every((p) => tm.includes(L.sideText(lv(fx.morning.bands["5"], p).term))), tm);
const tn = text(e(C.RangeBody, { data: fx.night, horizon: 30 }));
check("夜盤：沒有到收盤、標實測樣本少", !tn.includes("到收盤") && tn.includes("實測樣本少（夜盤只有幾十晚）"), tn);
const ts_ = text(e(C.RangeBody, { data: fx.stale, horizon: 15 }));
check("舊價：提醒行情可能已經動了", ts_.includes("這個價是") && ts_.includes("行情可能已經動了") && !t.includes("行情可能已經動了"), ts_);
const tx = text(e(C.RangeBody, { data: fx.nosigma, horizon: 15 }));
check("成交太少：只寫原因，不寫數字", tx.includes("成交太少") && !tx.includes("±") && !tx.includes("落點＝"), tx);
check("沒有成交價：只寫原因", text(e(C.RangeBody, { data: fx.noprice, horizon: 15 })).includes("沒有最近的成交價") && !text(e(C.RangeBody, { data: fx.noprice, horizon: 15 })).includes("±"));
check("沒有實測表：只寫原因", text(e(C.RangeBody, { data: fx.uncal, horizon: 15 })).includes("實測表") && !text(e(C.RangeBody, { data: fx.uncal, horizon: 15 })).includes("±"));
check("沒有 undefined／NaN／null", ![t, tm, tn, ts_, tx].some((x) => BAD.test(x)), [t, tm, tn, ts_, tx].map((x) => x.match(BAD)));
check("RangeTable：三個層級各一列、列的內容有落點與期間", (renderToStaticMarkup(e(C.RangeTable, { band: b15, title: "15 分鐘" })).match(/落點/g) || []).length === 3 && (renderToStaticMarkup(e(C.RangeTable, { band: b15, title: "x" })).match(/期間/g) || []).length === 3);

// ══ 容器：hooks 換成替身 ═════════════════════════════════════════
const S = { data: 0, err: 1 };
function harness(initial = {}) {
  const st = [], effects = []; let idx = 0;
  React.useState = (init) => { const i = idx++; if (!(i in st)) st[i] = i in initial ? initial[i] : init; return [st[i], (v) => { st[i] = typeof v === "function" ? v(st[i]) : v; }]; };
  React.useEffect = (f) => { effects.push(f); };
  return { st, effects, render: (props = { horizon: 15 }) => { idx = 0; effects.length = 0; return renderToStaticMarkup(e(Section, props)); } };
}
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  {
    const h = harness({ [S.data]: d });
    const tx2 = strip(h.render({ horizon: 30 }));
    check("容器：標題與選到的持有時間", tx2.includes("機率範圍（依波動）") && tx2.includes("30 分鐘") && L.sideText(lv(d.bands["30"], 80).term) && tx2.includes(L.sideText(lv(d.bands["30"], 80).term)), tx2.slice(0, 300));
    check("容器：還沒載入時只有標題", !strip(harness().render()).includes("落點＝") && !strip(harness().render()).includes("更新失敗"));
    check("容器：還沒載入過就失敗只顯示錯誤", strip(harness({ [S.err]: "後端還沒有這個 API（要重啟後端才有）" }).render()).includes("後端還沒有這個 API"));
    const t3 = strip(harness({ [S.data]: d, [S.err]: "502 暫時連不上" }).render());
    check("容器：載入過之後更新失敗：舊的留著、另外標示", t3.includes("±") && t3.includes("更新失敗：502 暫時連不上"), t3);
  }
  {
    const calls = []; apiMod.api.market.range = async () => { calls.push(1); return d; };
    const h = harness(); h.render();
    const realSet = global.setInterval, realClear = global.clearInterval, ivs = []; let cleared = null;
    global.setInterval = (f, ms) => { ivs.push([f, ms]); return 5; }; global.clearInterval = (id) => { cleared = id; };
    const cleanup = h.effects[0]();
    global.setInterval = realSet;
    await tick();
    check("載入：一開始抓一次、15 秒輪詢（只讀本機資料庫與記憶體）", calls.length === 1 && ivs.length === 1 && ivs[0][1] === 15000, JSON.stringify([calls.length, ivs.map((x) => x[1])]));
    check("載入成功：存進 state、清掉錯誤", h.st[S.data] === d && h.st[S.err] === null);
    ivs[0][0](); await tick(); check("輪詢觸發：再抓一次", calls.length === 2);
    cleanup(); global.clearInterval = realClear; check("卸載：停掉輪詢", cleared === 5);
    apiMod.api.market.range = async () => fx.night;                                                    // 卸載之後才回來的是另一份資料
    ivs[0][0](); await tick(); check("卸載之後回來的結果不會再寫進 state", h.st[S.data] === d && h.st[S.data] !== fx.night);
  }
  {
    apiMod.api.market.range = async () => { throw new Error("404 — Not Found"); };
    const h = harness(); h.render(); h.effects[0](); await tick();
    check("舊後端（404）：講「要重啟後端」", h.st[S.err].includes("重啟後端") && h.st[S.data] === null, h.st[S.err]);
    apiMod.api.market.range = async () => { throw new Error('503 — {"detail":"忙線中"}'); };
    const h2 = harness(); h2.render(); h2.effects[0](); await tick();
    check("其他錯誤：顯示後端那句說明", h2.st[S.err] === "忙線中", h2.st[S.err]);
  }
  {
    const seen = []; global.fetch = async (url) => { seen.push(url); return { ok: true, json: async () => ({}), text: async () => "" }; };
    global.window = { location: { hostname: "localhost" } }; global.localStorage = { getItem: () => null, setItem: () => {} };
    const fresh = transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "api_fresh.js"));
    await fresh.api.market.range();
    check("API：GET /market/range", seen.length === 1 && /\/market\/range$/.test(seen[0]), JSON.stringify(seen));
  }
  console.log(fail ? `\n${fail} FAIL` : "\n全部通過");
  process.exit(fail ? 1 : 0);
})();
