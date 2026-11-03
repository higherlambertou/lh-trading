// 價量分布（update.md 發現 77）：lib/volumeProfile.ts 的純函式（合併成粗格、長方形幾何、價位線、標籤排列、說明文字）
// 與 lib/profilePrimitive.ts 的繪圖（假的畫布、價格軸與圖）。
// 由 tests/test_frontend_profile.py 執行。用法：node tests/frontend/profile_check.js
const path = require("path"), fs = require("fs"), os = require("os"), Module = require("module");
const ROOT = path.resolve(__dirname, "..", ".."), FRONT = path.join(ROOT, "frontend");
process.env.NODE_PATH = path.join(FRONT, "node_modules"); Module._initPaths();
const ts = require("typescript");
const TMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "profile-check-")));
process.on("exit", () => { try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* 忽略 */ } });
let failed = 0, passed = 0;
const ok = (c, m) => { if (c) { passed++; } else { failed++; console.log("FAIL:", m); } };
const eq = (a, b, m) => ok(JSON.stringify(a) === JSON.stringify(b), `${m}\n   got ${JSON.stringify(a)}\n   want ${JSON.stringify(b)}`);
const near = (a, b, m) => ok(typeof a === "number" && Math.abs(a - b) < 1e-9, `${m}\n   got ${a}\n   want ${b}`);
const transpile = (src, out) => {
  fs.writeFileSync(out, ts.transpileModule(fs.readFileSync(src, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText);
  return require(out);
};
const orig = Module._resolveFilename;
Module._resolveFilename = function (req, ...r) {
  if (req === "@/lib/volumeProfile") return path.join(TMP, "volumeProfile.js");
  return orig.call(this, req, ...r);
};
const V = transpile(path.join(FRONT, "lib", "volumeProfile.ts"), path.join(TMP, "volumeProfile.js"));
const P = transpile(path.join(FRONT, "lib", "profilePrimitive.ts"), path.join(TMP, "profilePrimitive.js"));

const row = (price, buy, sell, other = 0) => ({ price, buy, sell, other, total: buy + sell + other });
const up = (p) => 500 - p;                                       // 價格越高、y 越小

// ── niceStep：合併成幾點一格 ──
{
  ok(V.MIN_ROW_PX >= 6 && V.MIN_ROW_PX <= 12, "每一格至少 6～12 像素高（再細就是一排髮絲、再粗就看不出輪廓）");
  eq(V.niceStep(10), 1, "每點 10 像素：1 點一格就夠高");
  eq(V.niceStep(V.MIN_ROW_PX), 1, "每點剛好 MIN_ROW_PX 像素：不用合併");
  eq(V.niceStep(V.MIN_ROW_PX / 2), 2, "每點半個 MIN_ROW_PX：2 點一格");
  eq(V.niceStep(1), 10, "每點 1 像素：要 7 點以上 → 10");
  eq(V.niceStep(0.62), 20, "每點 0.62 像素（夜盤一整屏）：11.3 點 → 20");
  eq(V.niceStep(1, 10), 10, "要 10 像素、每點 1 像素：剛好 10 → 選 10（不是 20）");
  eq(V.niceStep(2, 10), 5, "minPx 參數有作用");
  eq(V.niceStep(1, 15), 20, "minPx 15、每點 1 像素：要 15 點以上 → 20（預設的 MIN_ROW_PX 只會給 10）");
  eq(V.niceStep(0.0001), 1000, "再小也不超過最大的一檔");
  eq([V.niceStep(0), V.niceStep(-1), V.niceStep(NaN), V.niceStep(Infinity)], [1, 1, 1, 1], "算不出來（沒座標、負數、NaN、無限大）回 1");
  ok(V.NICE_STEPS[0] === 1 && V.NICE_STEPS.every((s, i, a) => i === 0 || s > a[i - 1]), "候選的格寬由小到大、從 1 開始");
}

// ── rebin：細的列合併成粗格 ──
{
  const fine = [row(103, 1, 0), row(105, 0, 2), row(112, 4, 0, 1), row(99, 0, 1)];
  eq(V.rebin(fine, 10).map((x) => [x.price, x.buy, x.sell, x.other, x.total]), [[90, 0, 1, 0, 1], [100, 1, 2, 0, 3], [110, 4, 0, 1, 5]], "依 10 點合併，下緣是 10 的倍數，由低到高，各欄位相加");
  ok(V.rebin(fine, 1) === fine && V.rebin(fine, 0) === fine && V.rebin(fine, -3) === fine, "格寬 ≤ 1：原樣（同一個陣列）");
  eq(fine[0], row(103, 1, 0), "不改動原本的列");
  eq(V.rebin(V.rebin(fine, 5), 10), V.rebin(fine, 10), "先合 5 再合 10 ＝ 直接合 10");
  eq(V.rebin([row(100, 1, 0), row(101, 0, 1)], 2).map((x) => [x.price, x.buy, x.sell, x.total]), [[100, 1, 1, 2]], "格寬 2 也要合併（只有 ≤ 1 才原樣）");
  eq(V.rebin([row(-5, 1, 0)], 10).map((x) => x.price), [-10], "負價位往下取整");
  eq(V.rebin([], 10), [], "空的");
}

// ── profileBars ──
{
  const ROWS = [row(100, 6, 2, 2), row(110, 0, 5), row(120, 0, 0), row(130, 1, 1)];          // 長度（外盤＋內盤）：8、5、0、2
  const r = V.profileBars(ROWS, 10, up, 100, { poc: 105, vah: 140, val: 100 });
  eq(r.length, 3, "量是 0 的那一格不畫");
  eq([r[0].top, r[0].height, r[0].width], [390, 10, 100], "最長的那格寬度＝maxWidth；上緣是價格高的那邊（110 → y 390）");
  eq([r[0].sell, r[0].buy], [25, 75], "各段寬度依內外盤比例（內盤 2、外盤 6）");
  ok(!("other" in r[0]), "判定不出內外盤的量不畫（只留在說明列的總數裡）");
  ok(r.every((x) => Math.abs(x.sell + x.buy - x.width) < 1e-9), "內盤＋外盤等於整格寬度");
  eq([r[1].width, r[1].sell, r[1].buy], [62.5, 62.5, 0], "5／8 的格子寬 62.5");
  eq(r[2].width, 25, "2／8 ＝ 25%");
  eq(r.map((x) => x.inValue), [true, true, true], "價值區 100～140：三格的中心（105、115、135）都在裡面");
  eq(r.map((x) => x.poc), [true, false, false], "外框標在最長的那一格");
  eq(V.profileBars(ROWS, 10, up, 100, { poc: 110, vah: 125, val: 105 }).map((x) => x.inValue), [true, true, false], "格子的中心落在價值區裡（含邊界：105 剛好是下緣）才算在裡面");
  eq(V.profileBars(ROWS, 10, up, 100, { poc: null, vah: 125, val: 106 }).map((x) => x.inValue), [false, true, false], "中心 105 在下緣 106 之外");
  eq(V.profileBars(ROWS, 10, up, 100, { poc: null, vah: 135, val: 100 }).map((x) => x.inValue), [true, true, true], "中心 135 剛好在上緣：算在裡面");
  eq(V.profileBars(ROWS, 10, up, 100, { poc: null, vah: 134.9, val: 100 }).map((x) => x.inValue), [true, true, false], "中心 135 超過上緣 134.9：不算");
  eq(V.profileBars(ROWS, 10, up, 100, { poc: null, vah: 140, val: null }).map((x) => x.inValue), [false, false, false], "只有上緣、沒有下緣：都不算");
  eq(V.profileBars(ROWS, 10, up, 100).map((x) => [x.inValue, x.poc]), [[false, true], [false, false], [false, false]], "沒有價位資訊：都不在價值區；最長的那格照樣有外框");
  eq(V.profileBars(ROWS, 10, (p) => (p === 100 ? null : up(p)), 100).length, 2, "價格軸算不出位置（不在畫面上）的格子略過");
  eq(V.profileBars(ROWS, 10, (p) => (p === 110 ? NaN : up(p)), 100).length, 1, "座標是 NaN 的略過（110 同時是 100 那格的上緣與 110 那格的下緣，相鄰兩格都不畫）");
  eq(V.profileBars(ROWS, 10, (p) => (p === 110 ? Infinity : up(p)), 100).length, 1, "座標是無限大的也略過");
  eq(V.profileBars([row(100, 1, 1)], 10, () => 7, 50)[0].height, 1, "格子高度至少 1 像素");
  eq(V.profileBars([row(100, 1, 1)], 10, (p) => (p === 110 ? 40 : 52), 50)[0].top, 40, "不管價格軸方向，上緣取較小的 y");
  eq(V.profileBars([row(100, 1, 1)], 10, (p) => (p === 110 ? 52 : 40), 50)[0].top, 40, "價格軸反過來（價格越高 y 越大）：上緣仍取較小的 y");
  eq([V.profileBars([], 10, up, 100), V.profileBars([row(100, 0, 0)], 10, up, 100), V.profileBars([row(100, 0, 0, 5)], 10, up, 100), V.profileBars(ROWS, 10, up, 0), V.profileBars(ROWS, 0, up, 100)],
    [[], [], [], [], []], "沒有列、量是 0、只有判定不出的量、寬度 0、格寬 0：什麼都不畫");
}

// ── profileBars：只算畫面上看得到的格子；外框標「整段裡最長的」──
{
  const two = [row(100, 10, 0), row(300, 2, 0)];
  eq(V.profileBars(two, 10, up, 100).map((x) => x.width), [100, 20], "沒給畫面高度：兩格都算，依最長的正規化");
  eq(V.profileBars(two, 10, up, 100, undefined, 250).map((x) => x.width), [100], "給了高度：價格 100 的那格在畫面外（y 390～400 > 250）略過，剩下的依自己正規化");
  eq(V.profileBars(two, 10, (p) => 800 - p * 2, 100, undefined, 1000).map((x) => x.width), [100, 20], "兩格都在畫面裡：照常");
  const edge = (top) => V.profileBars([row(0, 1, 0)], 10, (p) => (p === 10 ? top : top + 10), 50, undefined, 100).length;
  eq([edge(-10), edge(-10.5), edge(100), edge(100.5)], [1, 0, 1, 0], "剛好貼著上緣或下緣的格子還算；整格出界才略過");
  const g = [row(100, 8, 0), row(110, 3, 0)];
  eq(V.profileBars(g, 10, (p) => (p === 100 || p === 110 ? null : up(p)), 100).length, 0, "兩格都算不出位置：空");
  const only110 = V.profileBars(g, 10, (p) => (p === 110 || p === 120 ? up(p) : null), 100);
  eq([only110.length, only110[0].width, only110[0].poc], [1, 100, false], "整段最長的那格（100）在畫面外：畫面上不標外框，剩下那格仍依自己寬度正規化");
  const tie = [row(100, 3, 2), row(110, 2, 3), row(120, 1, 1)];
  eq(V.profileBars(tie, 10, up, 100, { poc: 112, vah: null, val: null }).map((x) => x.poc), [false, true, false], "一樣長：外框標在離後端 POC 最近的那格（112 → 110～120）");
  eq(V.profileBars(tie, 10, up, 100, { poc: 101, vah: null, val: null }).map((x) => x.poc), [true, false, false], "POC 101 → 100～110 那格");
  eq(V.profileBars(tie, 10, up, 100, { poc: 108, vah: null, val: null }).map((x) => x.poc), [true, false, false], "POC 108 在 100～110 裡：照中心算離 105 最近（不是離下緣 110 最近）→ 還是 100～110 那格");
  eq(V.profileBars(tie, 10, up, 100, { poc: null, vah: null, val: null }).map((x) => x.poc), [true, false, false], "沒有後端 POC：一樣長取最低價的那格");
  const coarse = [row(100, 7, 0), row(110, 8, 0), row(120, 1, 0)];
  eq(V.profileBars(coarse, 10, up, 100, { poc: 105, vah: null, val: null }).map((x) => x.poc), [false, true, false], "後端 POC 在 100～110，但格子較粗時最長的是 110～120：外框跟著最長的直條走");
}

// ── blend 與配色 ──
{
  eq(V.blend("#ffffff", 0.5, "#000000"), "rgb(128, 128, 128)", "一半一半（127.5 四捨五入）");
  eq(V.blend("#ff0000", 1, "#00ff00"), "rgb(255, 0, 0)", "alpha 1 ＝ 前景色");
  eq(V.blend("#ff0000", 0, "#00ff00"), "rgb(0, 255, 0)", "alpha 0 ＝ 背景色");
  eq(V.blend("#e0636a", 0.5), V.blend("#e0636a", 0.5, V.PANE_BG), "預設的背景是圖的背景色");
  const rgb = (s) => (s.match(/^rgb\((\d+), (\d+), (\d+)\)$/) || []).slice(1).map(Number);
  const c = V.BAR_COLORS, light = (s) => rgb(s).reduce((a, b) => a + b, 0);
  ok([c.in.buy, c.in.sell, c.out.buy, c.out.sell].every((x) => rgb(x).length === 3), "直條是實心色（rgb，不是半透明：半透明會透出格線）");
  ok(rgb(c.in.buy)[0] > rgb(c.in.buy)[1] && rgb(c.in.buy)[0] > rgb(c.in.buy)[2] && rgb(c.out.buy)[0] > rgb(c.out.buy)[1], "外盤（買方主動）是紅色（台股慣例：漲＝紅）");
  ok(rgb(c.in.sell)[1] > rgb(c.in.sell)[0] && rgb(c.in.sell)[1] > rgb(c.in.sell)[2] && rgb(c.out.sell)[1] > rgb(c.out.sell)[0], "內盤（賣方主動）是綠色");
  ok(light(c.in.buy) > light(c.out.buy) && light(c.in.sell) > light(c.out.sell), "價值區裡比價值區外亮");
  eq([c.in.buy, c.out.sell], [V.blend(V.BUY_COLOR, V.ALPHA_IN), V.blend(V.SELL_COLOR, V.ALPHA_OUT)], "由兩個 alpha 常數與兩個基本色混出來");
  ok(V.ALPHA_IN > V.ALPHA_OUT && V.ALPHA_OUT > 0 && V.ALPHA_IN <= 1, "價值區的 alpha 大於區外");
  ok(V.POLL_MS >= 10000 && V.POLL_MS <= 60000, "每 15 秒左右問一次（後端快取 10 秒；不要輪詢太密）");
  ok(V.MAX_FRAC > 0 && V.MAX_FRAC <= 0.4, "最寬不超過圖寬的 40%（否則蓋住 K 棒）");
  ok(V.PAD_FRAC >= 0.2 && V.PAD_FRAC <= 0.6, "右邊留白占顯示根數的 20～60%（太小蓋住 K 棒、太大 K 棒擠成一小塊）");
}

// ── levelLines ──
{
  const base = { session: "day", date: "2026-11-03", poc: 44555, prev: null };
  eq(V.levelLines(null), [], "沒資料：沒有線");
  eq(V.levelLines({ ...base }), [{ price: 44555, title: "今POC", color: V.POC_COLOR, style: 0 }], "只有現在的 POC：一條實線");
  const prev = { date: "2026-11-02", poc: 45155, vah: 45260, val: 44990 };
  const l = V.levelLines({ ...base, prev });
  eq(l.map((x) => [x.title, x.price, x.style]), [["昨POC", 45155, 2], ["昨VAH", 45260, 2], ["昨VAL", 44990, 2], ["今POC", 44555, 0]], "前一個日盤三條虛線＋現在的 POC 實線");
  eq(l.map((x) => x.color), [V.POC_COLOR, V.VA_COLOR, V.VA_COLOR, V.POC_COLOR], "POC 黃、價值區藍");
  const n = V.levelLines({ session: "night", date: "2026-11-03", poc: 43105, prev: { ...prev, date: "2026-11-03" } });
  eq(n.map((x) => x.title), ["日POC", "日VAH", "日VAL", "夜POC"], "夜盤：前一個日盤是同一天剛收的日盤，標「日」；現在的標「夜」");
  eq(V.levelLines({ ...base, poc: null, prev: { date: "x", poc: null, vah: 0, val: -5 } }), [], "null、0、負數都不畫");
  eq(V.levelLines({ ...base, poc: NaN }).length, 0, "NaN 不畫");
  eq(V.levelLines({ ...base, poc: Infinity }).length, 0, "無限大不畫");
}

// ── layoutLabels：標籤的垂直位置 ──
{
  eq(V.layoutLabels([100, 102, 104], 16, 8, 400), [100, 116, 132], "靠得太近就由上往下推開，隔 gap 像素");
  eq(V.layoutLabels([104, 100, 102], 16, 8, 400), [132, 100, 116], "回傳的順序和輸入一樣（不是排序後的）");
  eq(V.layoutLabels([50, 200], 16, 8, 400), [50, 200], "離得夠遠就不動");
  eq(V.layoutLabels([390, 395, 400], 16, 8, 400), [368, 384, 400], "推到超過下緣：最後一個貼著下緣，其餘往上推回來");
  eq(V.layoutLabels([2, 4], 16, 8, 400), [8, 24], "靠近上緣：先壓進上緣再推開，兩個標籤不會疊在一起");
  eq(V.layoutLabels([100, 100], 16, 8, 400), [100, 116], "一樣高：照輸入順序，前面的在上");
  eq(V.layoutLabels([], 16, 8, 400), [], "沒有標籤");
  const jam = V.layoutLabels([10, 12, 14], 16, 8, 30);
  ok(jam.every((v) => v >= 8 && v <= 30), "放不下那麼多：寧可疊在一起，也不跑出畫面");
}

// ── labelMode：標籤放得下什麼樣式 ──
{
  eq([V.labelMode(100, 90, 40), V.labelMode(90, 90, 40), V.labelMode(89, 90, 40), V.labelMode(40, 90, 40), V.labelMode(39, 90, 40)], ["full", "full", "short", "short", "none"], "夠放全部就全放；剛好等於也放；再窄只放名稱；再窄就不放");
  eq([V.labelMode(0, 90, 40), V.labelMode(-5, 90, 40), V.labelMode(NaN, 90, 40), V.labelMode(Infinity, 90, 40)], ["none", "none", "none", "full"], "沒有空間（0、負數、NaN）不放；無限寬（沒有 K 棒要避）全放");
}

// ── captionText ──
{
  const prof = { label: "11/3 日盤", pbin: 10, complete: true, rows: [1], total: 215256, buy: 96865, sell: 111933, other: 6458, poc: 44555, vah: 44680, val: 43730,
    prev: { label: "11/2 日盤", poc: 45155, vah: 45260, val: 44990, note: null } };
  eq(V.captionText(prof), "價量分布 11/3 日盤：POC 44,555　價值區 43,730～44,680　外盤 45%／內盤 52%｜11/2 日盤：POC 45,155　價值區 44,990～45,260", "完整的一行");
  ok(V.captionText({ ...prof, complete: false }).includes("11/3 日盤（進行中）"), "還沒收完要標進行中");
  ok(V.captionText({ ...prof, prev: { ...prof.prev, note: "換月：這個日盤的近月是 TMFJ6" } }).includes("（換月：這個日盤的近月是 TMFJ6）"), "換月的註記跟在前一個日盤後面");
  ok(V.captionText({ ...prof, prev: null }).endsWith("｜找不到前一個日盤"), "沒有前一個日盤要說");
  eq(V.captionText(null), "價量分布載入中…", "還沒載入");
  eq(V.captionText(prof, "壞了"), "壞了", "錯誤訊息優先");
  eq(V.captionText({ ...prof, rows: [], note: "最近 10 天沒有成交" }), "最近 10 天沒有成交", "沒有資料寫後端給的原因");
  eq(V.captionText({ ...prof, rows: [], note: null }), "11/3 日盤還沒有成交", "沒有原因就寫這個時段還沒有成交");
  ok(V.captionText({ ...prof, poc: null, val: null, vah: null, total: 0, buy: 0, sell: 0 }).includes("POC —") && V.captionText({ ...prof, total: 0 }).includes("外盤 —"), "算不出來的寫「—」，不寫 NaN／null");
  ok(!/NaN|undefined|null/.test(V.captionText(prof)), "沒有 NaN／undefined／null");
  ok(V.HELP_TEXT.includes("不是進場訊號") && V.HELP_TEXT.includes("沒有發現前一日的 POC／價值區有磁吸或擋住的預測力") && V.HELP_TEXT.includes("70%"), "滑過說明列的完整解釋：價值區定義、不是進場訊號、歷史檢驗沒有預測力");
}

// ── 繪圖層（假的畫布、價格軸與圖）──
const MAXW = (w) => Math.min(w * V.MAX_FRAC, V.MAX_PX);
function setup({ width = 300, height = 400, y = (p) => 400 - 10 * p, lastX = 100, withChart = true } = {}) {
  const ops = [], used = [], asked = [];
  const ctx = {
    globalAlpha: 1, fillStyle: "", strokeStyle: "", lineWidth: 1, font: "", textBaseline: "", textAlign: "", dash: undefined, mv: null, lt: null,
    fillRect(x, yy, w, h) { ops.push({ op: "fill", color: this.fillStyle, alpha: this.globalAlpha, x, y: yy, w, h }); },
    strokeRect(x, yy, w, h) { ops.push({ op: "box", color: this.strokeStyle, alpha: this.globalAlpha, x, y: yy, w, h }); },
    fillText(t, x, yy) { ops.push({ op: "text", text: t, color: this.fillStyle, x, y: yy }); },
    measureText: (t) => ({ width: t.length * 6 }),
    setLineDash(d) { this.dash = d; },
    beginPath() {}, moveTo(x, yy) { this.mv = [x, yy]; }, lineTo(x, yy) { this.lt = [x, yy]; },
    stroke() { ops.push({ op: "line", color: this.strokeStyle, alpha: this.globalAlpha, dash: this.dash, from: this.mv, to: this.lt }); },
  };
  const target = { useMediaCoordinateSpace: (f) => { used.push("media"); return f({ context: ctx, mediaSize: { width, height } }); },
    useBitmapCoordinateSpace: () => { used.push("bitmap"); } };
  const series = { priceToCoordinate: y, dataByIndex: (i, dir) => { asked.push([i, dir]); return lastX == null ? null : { time: 123 }; } };
  const chart = { timeScale: () => ({ timeToCoordinate: (t) => (t === 123 ? lastX : null) }) };
  const prim = new P.ProfilePrimitive();
  let updates = 0;
  const attach = () => prim.attached({ series, ...(withChart ? { chart } : {}), requestUpdate: () => { updates++; } });
  const [barsView, labelsView] = prim.paneViews();
  const drawBars = () => { ops.length = 0; barsView.renderer().draw(target); return ops.slice(); };
  const drawLabels = () => { ops.length = 0; labelsView.renderer().draw(target); return ops.slice(); };
  return { prim, ops, ctx, used, asked, attach, drawBars, drawLabels, barsView, labelsView, updates: () => updates, target };
}
const DATA = () => ({ session: "day", date: "d1", pbin: 10, row_bin: 1, poc: 30.5, val: 30, vah: 32, prev: null,
  rows: [row(30, 6, 2, 2), row(31, 0, 5), row(32, 1, 1)] });                     // 價格 30～33，每點 10 像素：y(30)=100、y(31)=90、y(32)=80、y(33)=70；長度 8、5、2

{
  const h = setup();
  eq([h.barsView.zOrder(), h.labelsView.zOrder()], ["bottom", "top"], "直條與線畫在 K 棒下面、標籤畫在最上面");
  eq(h.drawBars().length + h.drawLabels().length, 0, "還沒掛上去、沒資料：不畫");
  h.attach();
  eq(h.updates(), 1, "掛上去會要求重畫");
  eq(h.drawBars().length + h.drawLabels().length, 0, "沒資料：不畫");
  h.prim.setData(DATA());
  eq(h.updates(), 2, "換資料會要求重畫");

  const bars = h.drawBars(), W = MAXW(300);
  near(W, 66, "圖寬 300：最寬 66（22%）");
  eq(bars.filter((o) => o.op === "fill").length, 6, "一個價值區底＋ 5 段直條（長度 0 的段不畫）");
  const band = bars[0];
  eq([band.op, band.color, band.x, band.y, band.w, band.h], ["fill", "rgba(127, 214, 245, 0.07)", 234, 80, 66, 20], "價值區的淡底：只在價量分布的寬度內，從 VAH（y 80）到 VAL（y 100）");
  const segs = bars.filter((o) => o.op === "fill").slice(1);
  const C = V.BAR_COLORS;
  eq(segs.map((o) => [o.color, o.x, o.y, o.w, o.h]), [
    [C.in.sell, 283.5, 90, 16.5, 9], [C.in.buy, 234, 90, 49.5, 9],                // 價格 30 那格：最長（66）；內盤 2/8、外盤 6/8；從右邊往左堆；格子 10 像素高、留 1 像素縫
    [C.in.sell, 258.75, 80, 41.25, 9],                                            // 價格 31：只有內盤、5/8
    [C.out.sell, 291.75, 70, 8.25, 9], [C.out.buy, 283.5, 70, 8.25, 9],           // 價格 32：中心 32.5 在價值區（30～32）外 → 淡色
  ], "每格的位置、寬度、顏色（價值區內濃、區外淡；內盤在右、外盤在左）");
  ok(segs.every((o) => o.alpha === 1), "直條是實心色（不靠透明度）");
  const boxes = bars.filter((o) => o.op === "box");
  eq(boxes.map((o) => [o.color, o.x, o.y, o.w, o.h]), [[V.POC_COLOR, 234.5, 90.5, 65, 8]], "外框只有最長的那一格，畫在直條的外緣內側");
  const lines = bars.filter((o) => o.op === "line");
  eq(lines.map((o) => [o.color, o.alpha, o.dash, o.from, o.to]), [[V.POC_COLOR, 0.55, [], [0, 95.5], [300, 95.5]]], "現在的 POC（30.5 → y 95）一條實線，從左邊畫到右緣");
  eq(h.ctx.globalAlpha, 1, "畫完把透明度還原");
  eq(h.ctx.dash, [], "畫完把虛線還原成實線");

  eq(h.used.filter((u) => u !== "media").length, 0, "用 CSS 像素座標（media），不是 bitmap");

  h.prim.setData(null);
  eq(h.drawBars().length + h.drawLabels().length, 0, "關掉（null）：不畫");
  h.prim.setData(DATA());
  h.prim.detached();
  eq(h.drawBars().length + h.drawLabels().length, 0, "拆掉之後不畫、也不會丟例外");
  h.prim.setData(DATA());
  ok(true, "拆掉之後 setData 不丟例外");
}

// ── 繪圖層：格子的高度、留縫、最寬 ──
{
  const gap = (px) => { const h = setup({ y: (p) => 400 - px * (p - 29) }); h.attach(); h.prim.setData(DATA()); return h.drawBars().filter((o) => o.op === "fill").slice(1)[0].h; };
  near(gap(9), 8, "格子 9 像素高：留 1 像素縫");
  near(gap(8.9), 8.9, "格子不到 9 像素：不留縫（連在一起才看得出輪廓）");
  near(gap(40), 39, "格子很高也只留 1 像素");
  const wide = setup({ width: 1200 });
  wide.attach(); wide.prim.setData(DATA());
  eq(wide.drawBars().filter((o) => o.op === "box")[0].w + 1, V.MAX_PX, "圖很寬時最寬 MAX_PX 像素");
  const nar = setup({ width: 100 });
  nar.attach(); nar.prim.setData(DATA());
  near(nar.drawBars().filter((o) => o.op === "box")[0].w + 1, 22, "窄圖：圖寬的 22%");
}

// ── 繪圖層：細的列依價格軸合併；舊後端；沒有座標 ──
{
  const fine = Array.from({ length: 100 }, (_, i) => row(100 + i, 1, 1));         // 價格 100～199 每點一列，量都一樣
  const h = setup({ y: (p) => 1000 - 0.62 * p, height: 1000 });
  h.attach();
  h.prim.setData({ session: "day", date: "d", pbin: 10, row_bin: 1, poc: 150, val: null, vah: null, prev: null, rows: fine });
  const out = h.drawBars();
  const fills = out.filter((o) => o.op === "fill");
  eq(fills.length, 10, "每點 0.62 像素：合併成 20 點一格，5 格各 2 段");
  near(fills[0].h, 0.62 * 20 - 1, "合併後每格 12.4 像素高（留 1 像素縫）");
  const box = out.filter((o) => o.op === "box");
  eq(box.length, 1, "外框只有一格");
  near(box[0].y, 1000 - 0.62 * 160 + 0.5, "量一樣：外框標在離後端 POC（150）最近的那格（140～160）");

  const old = setup({ y: (p) => 1000 - 10 * p, height: 1000 });
  old.attach();
  old.prim.setData({ session: "day", date: "d", pbin: 10, poc: 35, val: null, vah: null, prev: null, rows: [row(30, 1, 1), row(40, 1, 3)] });          // 舊版後端：沒有 row_bin，一列就是 pbin（10）點
  const oldFills = old.drawBars().filter((o) => o.op === "fill");
  eq(oldFills.map((o) => o.h), [99, 99, 99, 99], "舊後端：沒有 row_bin 就用 pbin 當格寬（每格 10 點 ＝ 100 像素），不是 1 點");

  const oldMerge = setup({ y: (p) => 1000 - 0.62 * p, height: 1000 });
  oldMerge.attach();
  oldMerge.prim.setData({ session: "day", date: "d", pbin: 10, poc: 105, val: null, vah: null, prev: null, rows: [row(100, 1, 1), row(110, 1, 3), row(120, 2, 2), row(130, 1, 1)] });
  const om = oldMerge.drawBars().filter((o) => o.op === "fill");
  eq(om.map((o) => Math.round(o.h * 100) / 100), [11.4, 11.4, 11.4, 11.4], "舊後端的 10 點列、格子不夠高：合併成 20 點一格（2 格各 2 段，每格 12.4 像素、留縫）");

  const off = setup({ y: (p) => 1000 - 0.3 * p, height: 1000 });
  off.attach();
  off.prim.setData({ session: "day", date: "d", pbin: 10, poc: 105, val: null, vah: null, prev: null, rows: [row(100, 1, 1), row(110, 1, 3)] });
  eq(off.drawBars().filter((o) => o.op === "fill").map((o) => Math.round(o.h * 100) / 100), [3, 3, 3, 3], "舊後端的列對不上粗格（25 點不是 10 的倍數）：照原本的 10 點一格畫");

  const nanBand = setup({ y: (p) => (p === 32 ? NaN : 400 - 10 * p) });
  nanBand.attach(); nanBand.prim.setData(DATA());
  const nb = nanBand.drawBars().filter((o) => o.op === "fill");
  ok(nb.length === 2 && nb.every((o) => o.color !== "rgba(127, 214, 245, 0.07)"), "價值區上緣（價格 32）算不出位置（NaN）：不畫淡底；以它為邊界的兩格（31、32）也略過，只剩價格 30 那格的 2 段");

  const empty = setup(); empty.attach(); empty.prim.setData({ ...DATA(), rows: [] });
  eq(empty.drawBars().length + empty.drawLabels().length, 0, "沒有列：什麼都不畫，也不丟例外");

  const scroll = setup({ y: (p) => 400 - 10 * (p - 31.5) });                        // 價格 30 那格（最長）在畫面下緣外：y 405～415
  scroll.attach(); scroll.prim.setData(DATA());
  const sc = scroll.drawBars();
  eq(sc.filter((o) => o.op === "fill").slice(1).map((o) => [o.y, o.w]), [[395, 66], [385, 13.2], [385, 13.2]], "畫面外的格子不畫，也不參與正規化：剩下最長的（價格 31，一半在畫面裡）頂到最寬 66，價格 32 是它的 2／5");
  eq(sc.filter((o) => o.op === "box").length, 0, "整段最長的那格（價格 30）在畫面外：畫面上不標外框");

  const none = setup({ y: () => null });
  none.attach(); none.prim.setData(DATA());
  eq(none.drawBars().length + none.drawLabels().length, 0, "價格軸算不出位置：整個不畫");
  const part = setup({ y: (p) => (p === 30.5 ? null : 400 - 10 * p) });
  part.attach(); part.prim.setData(DATA());
  eq(part.drawBars().filter((o) => o.op === "fill").length, 6, "只有 POC 那一點算不出位置：每點多少像素當作 0 → 退回 1 點一格，其餘照畫");
}

// ── 繪圖層：價位線與標籤 ──
{
  const prev = { date: "d0", poc: 5, vah: 8, val: 3 };
  const withPrev = () => ({ ...DATA(), prev });                                  // 價位線：昨POC 5（y 350）、昨VAH 8（y 320）、昨VAL 3（y 370）、今POC 30.5（y 95）
  const h = setup();
  h.attach(); h.prim.setData(withPrev());
  const lines = h.drawBars().filter((o) => o.op === "line");
  eq(lines.map((o) => [o.dash, o.color, o.from[1]]), [[[5, 4], V.POC_COLOR, 350.5], [[5, 4], V.VA_COLOR, 320.5], [[5, 4], V.VA_COLOR, 370.5], [[], V.POC_COLOR, 95.5]], "前一個日盤三條虛線（POC 黃、價值區藍）、現在的 POC 一條實線");
  const lab = h.drawLabels();
  eq(h.asked, [[Number.MAX_SAFE_INTEGER, -1]], "問最新一根 K 棒：往左找最近的一根");
  const texts = lab.filter((o) => o.op === "text");
  eq(texts.map((o) => o.text), ["昨POC 5", "昨VAH 8", "昨VAL 3", "今POC 31"], "標籤＝名稱＋價格（四捨五入、千分位）");
  eq(texts.map((o) => o.color), [V.POC_COLOR, V.VA_COLOR, V.VA_COLOR, V.POC_COLOR], "標籤的顏色同價位線");
  const edge = 300 - MAXW(300) - V.LABEL_GAP;
  const fills = lab.filter((o) => o.op === "fill"), boxes = lab.filter((o) => o.op === "box");
  eq(fills.map((o) => Math.round((o.x + o.w) * 1000) / 1000), [edge, edge, edge, edge], "標籤的右緣都貼著價量分布的左邊（不蓋住直條），寬度不同也排成整齊的一列");
  eq(fills.map((o) => o.w), [46, 46, 46, 52], "寬度＝字數 × 6（假的量字寬）＋ 10");
  eq(boxes.map((o) => [o.x + o.w, o.alpha]), [[edge - 0.5, 0.8], [edge - 0.5, 0.8], [edge - 0.5, 0.8], [edge - 0.5, 0.8]], "外框跟底色對齊、淡一點");
  eq(texts.map((o) => o.x), [fills[0].x + 5, fills[1].x + 5, fills[2].x + 5, fills[3].x + 5], "文字離左緣 5 像素");
  eq(texts.map((o) => o.y), [350.5, 320.5, 370.5, 95.5], "價位離得開：標籤跟著各自的價位（y ＋ 0.5）");
  ok(lab.every((o) => o.op !== "line"), "標籤層不畫線");
  eq(h.ctx.globalAlpha, 1, "畫完把透明度還原");

  const close = { ...DATA(), prev: { date: "d0", poc: 5, vah: 5.2, val: 3 } };       // 昨POC y 350、昨VAH y 348：只差 2 像素
  const c = setup(); c.attach(); c.prim.setData(close);
  const ty = c.drawLabels().filter((o) => o.op === "text").map((o) => [o.text.slice(0, 4), o.y - 0.5]);
  eq(ty, [["昨POC", 364], ["昨VAH", 348], ["昨VAL", 380], ["今POC", 95]], "太近的標籤往下推開（隔 16 像素）；線還在原本的價位");
  const cl = c.drawBars().filter((o) => o.op === "line").map((o) => o.from[1]);
  eq(cl, [350.5, 348.5, 370.5, 95.5], "價位線不跟著標籤移動");

  // 放得下的程度由最新一根 K 棒的位置決定：edge ＝ 228；全放要 52、只放名稱要 34
  const at = (lastX) => { const s = setup({ lastX }); s.attach(); s.prim.setData(DATA()); return s.drawLabels().filter((o) => o.op === "text").map((o) => o.text); };
  eq([at(100), at(170), at(171), at(188), at(189)], [["今POC 31"], ["今POC 31"], ["今POC"], ["今POC"], []], "K 棒離得遠：名稱＋價格；剛好放得下仍放；再靠近只放名稱；再靠近就不放（room ＝ 228 − (最新 K 棒 x ＋ 6)）");
  eq(at(null), ["今POC 31"], "取不到最新一根 K 棒：不避讓，全放");
  const nc = setup({ withChart: false }); nc.attach(); nc.prim.setData(DATA());
  eq(nc.drawLabels().filter((o) => o.op === "text").map((o) => o.text), ["今POC 31"], "沒有 chart：不避讓，全放");
  const short = setup({ lastX: 180 }); short.attach(); short.prim.setData({ ...DATA(), prev });
  const st = short.drawLabels();
  eq(st.filter((o) => o.op === "text").map((o) => o.text), ["昨POC", "昨VAH", "昨VAL", "今POC"], "只放名稱時四個標籤都只有名稱");
  eq(st.filter((o) => o.op === "fill").map((o) => o.w), [34, 34, 34, 34], "只放名稱的寬度 ＝ 字數 × 6 ＋ 10");

  const out = setup({ y: (p) => 400 - 10 * p + (p === 5 ? 1000 : 0) });            // 昨POC 在畫面外
  out.attach(); out.prim.setData(withPrev());
  eq(out.drawBars().filter((o) => o.op === "line").map((o) => o.from[1]), [320.5, 370.5, 95.5], "畫面外的價位線不畫");
  eq(out.drawLabels().filter((o) => o.op === "text").map((o) => o.text), ["昨VAH 8", "昨VAL 3", "今POC 31"], "畫面外的標籤也不畫");
  const edge0 = setup({ y: (p) => (p === 5 ? 0 : p === 8 ? 400 : p === 3 ? -0.5 : 400 - 10 * p) });
  edge0.attach(); edge0.prim.setData(withPrev());
  eq(edge0.drawBars().filter((o) => o.op === "line").length, 3, "剛好貼著上緣（0）或下緣（400）的價位線還畫；出界 0.5 就不畫");

  const dashed = setup(); dashed.attach(); dashed.prim.setData({ ...DATA(), poc: null, prev });                // 最後一條線是虛線
  dashed.drawBars();
  eq([dashed.ctx.dash, dashed.ctx.globalAlpha], [[], 1], "最後一條是虛線也要把虛線與透明度還原（不然會影響同一張畫布上接著畫的東西）");
  eq(at(NaN), ["今POC 31"], "最新一根 K 棒的座標是 NaN：當作取不到，不避讓");

  const noLine = setup(); noLine.attach(); noLine.prim.setData({ ...DATA(), poc: null, prev: null });
  eq(noLine.drawLabels().length, 0, "沒有任何價位線：標籤層什麼都不畫");
}

console.log(`${passed} PASS, ${failed} FAIL`);
process.exit(failed ? 1 : 0);
