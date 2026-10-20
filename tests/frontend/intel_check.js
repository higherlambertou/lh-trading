// 〈資訊〉面板：lib/intel.ts 的純函式、各區塊元件（靜態渲染）、容器（載入／輪詢／錯誤）。hooks 換成替身。
// 畫面吃的資料由 tests/test_frontend_intel.py 用 core/intel_reader.py 在種好資料的資料庫上現算（環境變數 IN_FIXTURES 指到目錄）。
// 用法：IN_FIXTURES=<dir> node tests/frontend/intel_check.js（需要 frontend/node_modules）
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript"), React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
if (!process.env.IN_FIXTURES) { console.error("需要環境變數 IN_FIXTURES（由 tests/test_frontend_intel.py 產生）"); process.exit(2); }
const fx = JSON.parse(fs.readFileSync(path.join(process.env.IN_FIXTURES, "fixtures.json"), "utf8"));
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "intel-check-")));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });
const transpile = (src, out) => {
  fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText);
  return require(out);
};
const map = { "@/lib/intel": path.join(TMP, "intel.js"), "@/lib/api": path.join(TMP, "api.js") };
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) { return map[req] || orig.call(this, req, ...r); };
const L = transpile(path.join(FRONT, "lib", "intel.ts"), path.join(TMP, "intel.js"));
const apiMod = transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "api.js"));
const C = transpile(path.join(FRONT, "components", "IntelPanel.tsx"), path.join(TMP, "panel.js"));
const e = React.createElement;
let fail = 0;
const check = (name, ok, extra = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  " + String(extra).slice(0, 900))); if (!ok) fail++; };
const strip = (s) => s.replace(/<\/?(div|button|li|tr|summary|details|table|thead|tbody)\b[^>]*>/g, " ").replace(/<[^>]+>/g, "").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/\s+/g, " ");
const text = (el) => strip(renderToStaticMarkup(el));
const J = (x) => JSON.stringify(x);
const BAD = /undefined|NaN|\bnull\b|Infinity|\[object/;
const S = fx.full, M = (k) => S.markets.find((m) => m.key === k);

// ══ 純函式 ═══════════════════════════════════════════════════════
check("fmt：千分位與小數位、null／NaN 是破折號", L.fmt(11137.294) === "11,137.29" && L.fmt(5, 0) === "5" && L.fmt(null) === "—" && L.fmt(undefined) === "—" && L.fmt(NaN) === "—" && L.fmt(0) === "0.00");
check("signed：正負號（用真的減號）、零沒有號", L.signed(3.5) === "+3.50" && L.signed(-3.5) === "−3.50" && L.signed(0) === "0.00" && L.signed(-2530, 0) === "−2,530" && L.signed(null) === "—");
check("upDown：台灣習慣漲紅跌綠、零與空值是灰", L.upDown(1) === L.UP && L.upDown(-1) === L.DOWN && L.upDown(0) === L.MUTED && L.upDown(null) === L.MUTED && L.UP === "#ff5252" && L.DOWN === "#00e676");
check("marketValue：依單位加上符號", L.marketValue({ value: 412.61, unit: "usd" }) === "$412.61" && L.marketValue({ value: 5.1, unit: "pct" }) === "5.10%" && L.marketValue({ value: 19.99, unit: "points" }) === "19.99" && L.marketValue({ value: null, unit: "usd" }) === "—");
check("marketChange：價格類是「+3.53（+21.45%）」、殖利率類是 bp、沒有前一天是破折號", L.marketChange({ kind: "price", chg: 3.53, chg_pct: 21.445 }) === "+3.53（+21.45%）" && L.marketChange({ kind: "rate", chg: -0.09 }) === "−9 bp"
  && L.marketChange({ kind: "price", chg: null }) === "—" && L.marketChange({ kind: "price", chg: 1, chg_pct: null }) === "+1.00" && L.marketChange({ kind: "rate", chg: 0.05 }) === "+5 bp");
check("marketDate：超過 3 天加「N 天前」、沒有日期是沒有資料", L.marketDate({ date: "2026-10-16", age_days: 3 }) === "10/16" && L.marketDate({ date: "2026-10-09", age_days: 10 }) === "10/09・10 天前" && L.marketDate({ date: null }) === "沒有資料");
check("stars：重要度 0~3 顆星、超出範圍截斷", L.stars(3) === "★★★" && L.stars(2) === "★★" && L.stars(0) === "" && L.stars(9) === "★★★" && L.stars(-1) === "");
const ev = (m, extra = {}) => ({ minutes_to: m, taipei_time: "10/29 02:00", taipei_weekday: "四", local_date: "2026-10-28", ...extra });
check("countdown：分鐘／小時／天、已過、只知道日期", L.countdown(ev(30)) === "30 分鐘後" && L.countdown(ev(60)) === "1 小時後" && L.countdown(ev(47 * 60 + 59)) === "47 小時後" && L.countdown(ev(48 * 60)) === "2 天後" && L.countdown(ev(13567)) === "9 天後"
  && L.countdown(ev(-5)) === "已過 5 分鐘" && L.countdown(ev(-180)) === "已過" && L.countdown(ev(null)) === "（只知道日期）" && L.countdown(ev(0)) === "0 分鐘後");
check("eventWhen：有時間用台北時間與星期、只有日期寫美東日期", L.eventWhen(ev(10)) === "10/29 02:00（四）" && L.eventWhen(ev(null, { taipei_time: null })) === "10/28（美東日期）");
check("newsAge：剛剛／分鐘／小時／天", L.newsAge(0) === "剛剛" && L.newsAge(10) === "10 分鐘前" && L.newsAge(60) === "1 小時前" && L.newsAge(47 * 60) === "47 小時前" && L.newsAge(48 * 60) === "2 天前");
check("ageText：收集器最後成功", L.ageText(null) === "從沒成功" && L.ageText(30) === "剛剛" && L.ageText(600) === "10 分鐘前" && L.ageText(7200) === "2 小時前" && L.ageText(3 * 86400) === "3 天前");
check("collectorLight：失敗紅、從沒成功黃、其他綠", L.collectorLight({ fails: 1, last_ok: 5 }) === "bad" && L.collectorLight({ fails: 0, last_ok: null }) === "warn" && L.collectorLight({ fails: 0, last_ok: 5 }) === "ok");
check("systemLight／systemText：沒有心跳紅、有失敗黃、正常綠", L.systemLight({ service_ok: false, problems: [] }) === "bad" && L.systemLight({ service_ok: true, problems: ["x"] }) === "warn" && L.systemLight({ service_ok: true, problems: [] }) === "ok"
  && L.systemText({ heartbeat_age_sec: null, service_ok: false, problems: [] }).includes("沒有心跳") && L.systemText({ heartbeat_age_sec: 600, service_ok: false, problems: [] }).includes("10 分鐘沒有心跳")
  && L.systemText({ heartbeat_age_sec: 5, service_ok: true, problems: ["twse_inst", "news_rss"] }) === "資訊服務在跑，但 twse_inst、news_rss 最近失敗" && L.systemText({ heartbeat_age_sec: 5, service_ok: true, problems: [] }) === "資訊服務正常");
check("friendlyIntelError：舊後端沒有這個 API 時講白話", L.friendlyIntelError("Not Found").includes("重啟後端") && L.friendlyIntelError("502 x") === "502 x");

// ══ 區塊（後端現算的資料）════════════════════════════════════════
const tm = text(e(C.MarketsCard, { markets: S.markets }));
check("市場：加權指數與臺指選擇權波動率指數排在最前面（後端的數字）", S.markets[0].key === "TAIEX" && S.markets[1].key === "VIXTW" && tm.includes("加權指數") && tm.includes("46,822.08") && tm.includes("臺指選擇權波動率指數") && tm.includes("26.37") && tm.includes(L.marketChange(M("TAIEX"))) && L.marketChange(M("VIXTW")) === "−4.24（−13.85%）", L.marketChange(M("VIXTW")));
check("市場：每一項的名稱、收盤、較前一日（後端的數字）", S.markets.every((m) => tm.includes(m.label)) && tm.includes(L.marketValue(M("VIX"))) && tm.includes(L.marketChange(M("VIX"))) && tm.includes(L.marketChange(M("US10Y"))) && tm.includes("bp"), tm);
check("市場：沒有資料的項目顯示破折號與「沒有資料」，不是 0", tm.includes("日經 225") && /日經 225——沒有資料/.test(tm) && !/日經 225 ?0/.test(tm), tm);
check("市場：超過 3 天沒更新的日期標「N 天前」（黃色）", /10\/09・10 天前/.test(tm) && renderToStaticMarkup(e(C.MarketsCard, { markets: S.markets })).includes("color:#ffc107"));
const rawM = renderToStaticMarkup(e(C.MarketsCard, { markets: S.markets }));
check("市場：漲紅跌綠（VIX 漲＝紅、費半跌＝綠）", new RegExp(`color:${L.UP}[^>]*>\\+3\\.53`).test(rawM) && new RegExp(`color:${L.DOWN}[^>]*>−443\\.93`).test(rawM), rawM.slice(0, 400));

const te = text(e(C.EventsCard, { events: S.events }));
check("事件：標題（中文）、台北時間與星期、倒數、重要度星星、國家", te.includes("FOMC 利率決策") && te.includes("10/29 02:00（四）") && te.includes("9 天後") && te.includes("★★★") && te.includes("美國 CPI") && te.includes("US"), te);
check("事件：只知道日期的標「美東日期」與「只知道日期」；剛過的淡化", te.includes("10/22（美東日期）") && te.includes("（只知道日期）") && renderToStaticMarkup(e(C.EventsCard, { events: S.events })).includes("opacity:0.5") && te.includes("已過 120 分鐘"), te);
check("事件：休市日提醒（日期、名稱、幾天後）", te.includes("台股休市：") && te.includes("10/26 國慶補假（7 天後）") && !te.includes("10/25"), te);
check("事件：沒有事件時寫沒有、不寫休市", text(e(C.EventsCard, { events: { upcoming: [], closures: [] } })).includes("未來兩週沒有重要事件") && !text(e(C.EventsCard, { events: { upcoming: [], closures: [] } })).includes("台股休市"));

const tc = text(e(C.ChipsCard, { chips: S.chips }));
check("籌碼：期貨三大法人（大台、小台、微台）淨未平倉、較前一日、多／空", tc.includes("臺股期貨（大台）") && tc.includes("−4,000") && tc.includes("+300") && tc.includes("1,000／5,000") && tc.includes("自營商") && !tc.includes("小型臺指") && !tc.includes("微型臺指"), tc);                  // 小台、微台沒有資料就整組不列
check("籌碼：選擇權 Call／Put 與變動、Put/Call 比（含前日）", tc.includes("買權 Call") && tc.includes("賣權 Put") && tc.includes("+5,820") && tc.includes("Put/Call 比") && tc.includes("未平倉 120.00%") && tc.includes("前日 60.00"), tc);
check("籌碼：現貨三大法人（億元）今天與近 5 日、融資融券標暫定", tc.includes("現貨三大法人買賣超（億元") && tc.includes("−104.0") && tc.includes("−510.0") && tc.includes("近 5 日合計") && tc.includes("融資融券（10/16，暫定）") && tc.includes("融資餘額 6,455.9 億") && tc.includes("融券 233,602 張"), tc);
check("籌碼：大盤成交（億）與近 20 日平均的倍數、股票漲跌家數（漲停跌停）", tc.includes("大盤成交（10/16）") && tc.includes("8,667 億") && /前 12 日平均的 1\.0\d 倍/.test(tc) && tc.includes("股票漲跌家數（10/16）") && tc.includes("上漲 684（漲停 19）") && tc.includes("下跌 286（跌停 0）") && tc.includes("持平 106") && !tc.includes("99999"), tc);
const mk = S.chips.market;
check("籌碼：大盤的後端欄位（億元換算、倍數、漲跌家數）", mk && mk.turnover_100m === 8667 && mk.turnover_ratio20 > 1 && mk.turnover_ratio20 < 1.1 && mk.turnover_days === 12 && mk.up === 684 && mk.limit_down === 0 && mk.date === "2026-10-16" && mk.breadth_date === "2026-10-16", J(mk));
const onlyTurn = text(e(C.ChipsCard, { chips: { ...S.chips, market: { ...mk, up: null, down: null, limit_up: null, limit_down: null, flat: null, breadth_date: null } } }));
const onlyBreadth = text(e(C.ChipsCard, { chips: { ...S.chips, market: { ...mk, turnover_100m: null, turnover_ratio20: null, date: null } } }));
const noRatio = text(e(C.ChipsCard, { chips: { ...S.chips, market: { ...mk, turnover_ratio20: null } } }));
const fewDays = text(e(C.ChipsCard, { chips: { ...S.chips, market: { ...mk, turnover_days: 10 } } }));
const legacy = text(e(C.ChipsCard, { chips: { ...S.chips, market: { ...mk, turnover_days: null } } }));
const noLimit = text(e(C.ChipsCard, { chips: { ...S.chips, market: { ...mk, limit_up: null, limit_down: null, flat: null } } }));
const onlyMarket = text(e(C.ChipsCard, { chips: { futures: [], options: null, pcr: null, inst: null, margin: null, market: mk } }));
check("籌碼：只有大盤資料時不寫「還沒有籌碼資料」", onlyMarket.includes("大盤成交") && !onlyMarket.includes("還沒有籌碼資料"), onlyMarket);
const rawChips = renderToStaticMarkup(e(C.ChipsCard, { chips: S.chips }));
check("籌碼：平均用了幾天就寫幾天（資料不到 20 天不假裝是 20 日）", fewDays.includes("前 10 日平均的") && !fewDays.includes("前 20 日") && legacy.includes("前 20 日平均的"), fewDays);
check("籌碼：上漲家數是紅、下跌家數是綠（台灣習慣）", new RegExp(`color:${L.UP}[^>]*>上漲 684`).test(rawChips) && new RegExp(`color:${L.DOWN}[^>]*>下跌 286`).test(rawChips), "");
const twoDates = text(e(C.ChipsCard, { chips: { ...S.chips, market: { ...mk, date: "2026-10-16", breadth_date: "2026-10-19" } } }));
const noDown = text(e(C.ChipsCard, { chips: { ...S.chips, market: { ...mk, down: null } } }));
check("籌碼：成交與漲跌家數各自標自己的日期；缺下跌家數就不列漲跌家數", twoDates.includes("大盤成交（10/16）") && twoDates.includes("股票漲跌家數（10/19）") && !noDown.includes("漲跌家數") && noDown.includes("大盤成交"), twoDates);
check("籌碼：大盤缺一半就只列另一半、缺倍數不寫倍數、缺漲停跌停不寫括號", onlyTurn.includes("大盤成交") && !onlyTurn.includes("漲跌家數") && onlyBreadth.includes("股票漲跌家數") && !onlyBreadth.includes("大盤成交") && !noRatio.includes("日平均") && noRatio.includes("8,667 億")
  && noLimit.includes("上漲 684") && !noLimit.includes("漲停") && !noLimit.includes("持平") && !BAD.test(onlyTurn + onlyBreadth + noRatio + noLimit), [onlyTurn, onlyBreadth].join("|"));
check("籌碼：沒有資料的項目不顯示、全部沒有時寫「還沒有籌碼資料」", text(e(C.ChipsCard, { chips: { futures: [], options: null, pcr: null, inst: null, margin: null, market: null } })).includes("還沒有籌碼資料") && !text(e(C.ChipsCard, { chips: S.chips })).includes("還沒有籌碼資料"));
check("籌碼：淨空的口數是綠、淨多是紅", new RegExp(`color:${L.DOWN}[^>]*>−4,000`).test(renderToStaticMarkup(e(C.ChipsCard, { chips: S.chips }))), "");

const tn = text(e(C.NewsCard, { news: S.news }));
check("新聞：標題、來源、台北時間、多久前、連結在新分頁開", S.news.every((n) => tn.includes(n.title)) && tn.includes("中央社") && tn.includes("理財周刊") && tn.includes("10 分鐘前") && /target="_blank"/.test(renderToStaticMarkup(e(C.NewsCard, { news: S.news }))) && /rel="noopener noreferrer"/.test(renderToStaticMarkup(e(C.NewsCard, { news: S.news }))), tn);
check("新聞：沒有時寫沒有", text(e(C.NewsCard, { news: [] })).includes("還沒有新聞"));

const th = renderToStaticMarkup(e(C.HealthBar, { collectors: S.collectors })), thx = strip(th);
check("收集器狀態：燈、文字、各收集器的最後成功與連續失敗", thx.includes("資訊服務在跑，但 twse_inst 最近失敗") && thx.includes("twse_inst") && thx.includes("連續失敗 2 次") && thx.includes("等來源公布") && thx.includes("從沒成功") && th.includes("background:#ffc107") && th.includes("background:#ff1744"), thx);
check("收集器狀態：服務沒心跳是紅燈", renderToStaticMarkup(e(C.HealthBar, { collectors: fx.noheartbeat.collectors })).includes("沒有心跳") && renderToStaticMarkup(e(C.HealthBar, { collectors: fx.noheartbeat.collectors })).includes("background:#ff1744"));
check("沒有 undefined／NaN／null", ![tm, te, tc, tn, thx].some((x) => BAD.test(x)), [tm, te, tc, tn, thx].map((x) => x.match(BAD)));

// ══ 容器：hooks 換成替身 ═════════════════════════════════════════
const Panel = C.default;
function harness(initial = {}) {
  const st = [], effects = []; let idx = 0;
  React.useState = (init) => { const i = idx++; if (!(i in st)) st[i] = i in initial ? initial[i] : init; return [st[i], (v) => { st[i] = typeof v === "function" ? v(st[i]) : v; }]; };
  React.useEffect = (f) => { effects.push(f); };
  return { st, effects, render: () => { idx = 0; effects.length = 0; return renderToStaticMarkup(e(Panel)); } };
}
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  {
    const p = text(e(Panel)); // 預設 state：什麼都還沒載入
    check("容器：還沒載入時只有說明（只顯示、不影響交易）", p.includes("只顯示、不影響任何交易決策") && !p.includes("近期事件") && !p.includes("資訊系統沒有資料"), p);
    const full = strip(harness({ 0: S }).render());
    check("容器：載入後有五個區塊與更新時間", ["資訊服務在跑", "近期事件", "全球市場", "籌碼", "最新新聞標題", "更新 "].every((x) => full.includes(x)), full.slice(0, 300));
    const un = strip(harness({ 0: fx.unavailable }).render());
    check("容器：資料庫不在時顯示原因、不顯示任何區塊", un.includes("資訊系統沒有資料：找不到資料庫") && !un.includes("近期事件") && !un.includes("全球市場"), un);
    check("容器：還沒載入過就失敗只顯示錯誤；載入過之後失敗：舊的留著、另外標示", strip(harness({ 1: "後端還沒有這個 API" }).render()).includes("後端還沒有這個 API") && strip(harness({ 0: S, 1: "502 暫時連不上" }).render()).includes("更新失敗：502 暫時連不上（顯示的是上一次的）") && strip(harness({ 0: S, 1: "502" }).render()).includes("全球市場"));
  }
  {
    const calls = []; apiMod.api.intel.summary = async () => { calls.push(1); return S; };
    const h = harness(); h.render();
    const realSet = global.setInterval, realClear = global.clearInterval, ivs = []; let cleared = null;
    global.setInterval = (f, ms) => { ivs.push([f, ms]); return 9; }; global.clearInterval = (id) => { cleared = id; };
    const cleanup = h.effects[0]();
    global.setInterval = realSet;
    await tick();
    check("載入：一開始抓一次、30 秒輪詢（只讀本機資料庫）", calls.length === 1 && ivs.length === 1 && ivs[0][1] === 30000, JSON.stringify([calls.length, ivs.map((x) => x[1])]));
    check("載入成功：存進 state、清掉錯誤", h.st[0] === S && h.st[1] === null);
    ivs[0][0](); await tick(); check("輪詢觸發：再抓一次", calls.length === 2);
    cleanup(); global.clearInterval = realClear; check("卸載：停掉輪詢", cleared === 9);
    apiMod.api.intel.summary = async () => fx.unavailable;
    ivs[0][0](); await tick(); check("卸載之後回來的結果不會再寫進 state", h.st[0] === S);
  }
  {
    apiMod.api.intel.summary = async () => { throw new Error("404 — Not Found"); };
    const h = harness(); h.render(); h.effects[0](); await tick();
    check("舊後端（404）：講「要重啟後端」", h.st[1].includes("重啟後端") && h.st[0] === null, h.st[1]);
    apiMod.api.intel.summary = async () => { throw new Error('503 — {"detail":"忙線中"}'); };
    const h2 = harness(); h2.render(); h2.effects[0](); await tick();
    check("其他錯誤：顯示後端那句說明", h2.st[1] === "忙線中", h2.st[1]);
  }
  {
    const seen = []; global.fetch = async (url) => { seen.push(url); return { ok: true, json: async () => ({}), text: async () => "" }; };
    global.window = { location: { hostname: "localhost" } }; global.localStorage = { getItem: () => null, setItem: () => {} };
    const fresh = transpile(path.join(FRONT, "lib", "api.ts"), path.join(TMP, "api_fresh.js"));
    await fresh.api.intel.summary();
    check("API：GET /intel/summary", seen.length === 1 && /\/intel\/summary$/.test(seen[0]), JSON.stringify(seen));
  }
  console.log(fail ? `\n${fail} FAIL` : "\n全部通過");
  process.exit(fail ? 1 : 0);
})();
