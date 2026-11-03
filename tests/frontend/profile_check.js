// 價量分布（update.md 發現 77）：lib/volumeProfile.ts 的純函式（長方形幾何、價位線、說明文字）與 lib/profilePrimitive.ts 的繪圖（假的畫布與價格軸）。
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

const row = (price, buy, sell, other) => ({ price, buy, sell, other, total: buy + sell + other });
const ROWS = [row(100, 6, 2, 2), row(110, 0, 5, 0), row(120, 0, 0, 0), row(130, 1, 1, 0)];
const up = (p) => 500 - p;                                       // 價格越高、y 越小

// ── profileBars ──
{
  const r = V.profileBars(ROWS, 10, up, 100, { poc: 105, vah: 140, val: 100 });
  eq(r.length, 3, "量是 0 的那一格不畫");
  eq([r[0].top, r[0].height, r[0].width], [390, 10, 100], "量最大的那格寬度＝maxWidth；上緣是價格高的那邊（110 → y 390）");
  eq([r[0].sell, r[0].buy, r[0].other], [20, 60, 20], "各段寬度依內外盤比例");
  ok(Math.abs(r[0].sell + r[0].buy + r[0].other - r[0].width) < 1e-9 && r.every((x) => Math.abs(x.sell + x.buy + x.other - x.width) < 1e-9), "各段加起來等於整格寬度");
  eq([r[1].width, r[1].sell, r[1].buy], [50, 50, 0], "量一半的格子寬度一半");
  eq(r[2].width, 20, "量 2 ／ 最大 10 ＝ 20%");
  eq(r.map((x) => x.inValue), [true, true, true], "價值區 100～140：三格都在裡面");
  eq(r.map((x) => x.poc), [true, false, false], "POC 105 在 100～110 那一格");
  eq(V.profileBars(ROWS, 10, up, 100, { poc: 110, vah: 125, val: 105 }).map((x) => [x.inValue, x.poc]), [[false, false], [true, true], [false, false]], "格子要整格在價值區內才算；POC 剛好在格子的上緣算下一格（左閉右開）");
  eq(V.profileBars(ROWS, 10, up, 100).map((x) => [x.inValue, x.poc]), [[false, false], [false, false], [false, false]], "沒有價位資訊：都不是");
  eq(V.profileBars(ROWS, 10, (p) => (p === 100 ? null : up(p)), 100).length, 2, "價格軸算不出位置（不在畫面上）的格子略過");
  eq(V.profileBars(ROWS, 10, (p) => (p === 110 ? NaN : up(p)), 100).length, 1, "座標是 NaN 的略過（110 同時是 100 那格的上緣與 110 那格的下緣，相鄰兩格都不畫）");
  eq(V.profileBars([row(100, 1, 1, 0)], 10, () => 7, 50)[0].height, 1, "格子高度至少 1 像素");
  eq(V.profileBars([row(100, 1, 1, 0)], 10, (p) => (p === 110 ? 40 : 52), 50)[0].top, 40, "不管價格軸方向，上緣取較小的 y");
  eq([V.profileBars([], 10, up, 100), V.profileBars([row(100, 0, 0, 0)], 10, up, 100), V.profileBars(ROWS, 10, up, 0), V.profileBars(ROWS, 0, up, 100)], [[], [], [], []], "沒有量、寬度 0、格寬 0：什麼都不畫");
}

// ── 顏色語意與更新頻率 ──
{
  const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const [br, bg, bb] = rgb(V.BUY_COLOR), [sr, sg, sb] = rgb(V.SELL_COLOR);
  ok(br > bg && br > bb, "外盤（買方主動）是紅色（台股慣例：漲＝紅）");
  ok(sg > sr && sg > sb, "內盤（賣方主動）是綠色");
  ok(V.POLL_MS >= 10000 && V.POLL_MS <= 60000, "每 15 秒左右問一次（後端快取 10 秒；不要輪詢太密）");
  ok(V.MAX_FRAC > 0 && V.MAX_FRAC <= 0.4, "最寬不超過圖寬的 40%（否則蓋住 K 棒）");
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

// ── captionText ──
{
  const prof = { label: "11/3 日盤", pbin: 10, complete: true, rows: [1], total: 215256, buy: 96865, sell: 111933, other: 6458, poc: 44555, vah: 44680, val: 43730,
    prev: { label: "11/2 日盤", poc: 45155, vah: 45260, val: 44990, note: null } };
  eq(V.captionText(prof), "價量分布 11/3 日盤，每格 10 點：POC 44,555　價值區 43,730～44,680　總量 215,256 口（外盤 45%／內盤 52%）｜11/2 日盤：POC 45,155　價值區 44,990～45,260", "完整的一行");
  ok(V.captionText({ ...prof, complete: false }).includes("11/3 日盤（進行中）"), "還沒收完要標進行中");
  ok(V.captionText({ ...prof, prev: { ...prof.prev, note: "換月：這個日盤的近月是 TMFJ6" } }).includes("（換月：這個日盤的近月是 TMFJ6）"), "換月的註記跟在前一個日盤後面");
  ok(V.captionText({ ...prof, prev: null }).endsWith("｜找不到前一個日盤"), "沒有前一個日盤要說");
  eq(V.captionText(null), "價量分布載入中…", "還沒載入");
  eq(V.captionText(prof, "壞了"), "壞了", "錯誤訊息優先");
  eq(V.captionText({ ...prof, rows: [], note: "最近 10 天沒有成交" }), "最近 10 天沒有成交", "沒有資料寫後端給的原因");
  eq(V.captionText({ ...prof, rows: [], note: null }), "11/3 日盤還沒有成交", "沒有原因就寫這個時段還沒有成交");
  ok(V.captionText({ ...prof, poc: null, val: null, vah: null, total: 0, buy: 0, sell: 0 }).includes("POC —") && V.captionText({ ...prof, total: 0 }).includes("外盤 —"), "算不出來的寫「—」，不寫 NaN／null");
  ok(!/NaN|undefined|null/.test(V.captionText(prof)), "沒有 NaN／undefined／null");
}

// ── 繪圖層（假的畫布與價格軸）──
{
  const prim = new P.ProfilePrimitive();
  let updates = 0;
  const series = { priceToCoordinate: (p) => 1000 - p };
  const calls = [];
  const ctx = { globalAlpha: 1, fillStyle: "", fillRect(x, y, w, h) { calls.push({ color: this.fillStyle, alpha: this.globalAlpha, x, y, w, h }); } };
  const used = [];
  const target = { useMediaCoordinateSpace: (f) => { used.push("media"); return f({ context: ctx, mediaSize: { width: 300, height: 400 } }); },
    useBitmapCoordinateSpace: () => { used.push("bitmap"); } };
  const view = prim.paneViews()[0];
  eq(view.zOrder(), "bottom", "畫在 K 棒下面");
  view.renderer().draw(target);
  eq(calls.length, 0, "還沒掛上去、沒資料：不畫");
  prim.attached({ series, requestUpdate: () => { updates++; } });
  eq(updates, 1, "掛上去會要求重畫");
  view.renderer().draw(target);
  eq(calls.length, 0, "沒資料：不畫");
  const data = { rows: [row(100, 6, 2, 2), row(110, 0, 5, 0)], pbin: 10, poc: null, vah: null, val: null };
  prim.setData(data);
  eq(updates, 2, "換資料會要求重畫");
  view.renderer().draw(target);
  // 最寬 90（＝300 × 30%）；量最大那格：賣 18、買 54、其他 18，從右邊（300）往左堆；價格 100～110 → y 890～900，高 10 → 畫 9（留 1 像素縫）
  eq(calls.slice(0, 3).map((c) => [c.color, c.x, c.y, c.w, c.h]), [[V.SELL_COLOR, 282, 890, 18, 9], [V.BUY_COLOR, 228, 890, 54, 9], [V.OTHER_COLOR, 210, 890, 18, 9]], "三段顏色、位置與寬度");
  eq(calls.slice(3).map((c) => [c.color, c.x, c.y, c.w]), [[V.SELL_COLOR, 255, 880, 45]], "第二格只有內盤、寬度一半，只畫一段（量是 0 的段不畫）");
  ok(calls.every((c) => c.alpha === 0.28), "沒有價值區資訊：全部淡色");
  eq(ctx.globalAlpha, 1, "畫完把透明度還原");
  eq(used, ["media"], "有資料才畫，而且用 CSS 像素座標（media），不是 bitmap");
  calls.length = 0;
  prim.setData({ ...data, val: 100, vah: 120, poc: 105 });
  view.renderer().draw(target);
  ok(calls.every((c) => c.alpha === 0.55), "價值區裡的格子畫濃一點");
  calls.length = 0;
  const wide = { useMediaCoordinateSpace: (f) => f({ context: ctx, mediaSize: { width: 1200, height: 400 } }) };
  view.renderer().draw(wide);
  eq(calls.slice(0, 3).reduce((a, c) => a + c.w, 0), V.MAX_PX, "圖很寬時最寬 220 像素");
  calls.length = 0;
  prim.setData(null);
  view.renderer().draw(target);
  eq(calls.length, 0, "關掉（null）：不畫");
  const tiny = new P.ProfilePrimitive();                                   // 縮得很小的圖：10 點只有 1～3 像素高，不能被「留縫」吃掉
  tiny.attached({ series: { priceToCoordinate: (p) => 1000 - p / 10 }, requestUpdate: () => {} });
  tiny.setData({ rows: [row(100, 1, 0, 0), row(200, 2, 0, 0)], pbin: 10, poc: null, vah: null, val: null });
  calls.length = 0;
  tiny.paneViews()[0].renderer().draw(target);
  eq(calls.map((c) => [c.y, c.h]), [[989, 1], [979, 1]], "高度 1 像素的格子照畫 1 像素");
  tiny.attached({ series: { priceToCoordinate: (p) => 1000 - p / 5 }, requestUpdate: () => {} });
  calls.length = 0;
  tiny.paneViews()[0].renderer().draw(target);
  eq(calls.map((c) => c.h), [2, 2], "高度 2 像素的格子照畫 2 像素（超過 3 才留縫）");
  tiny.attached({ series: { priceToCoordinate: (p) => 1000 - p * 0.35 }, requestUpdate: () => {} });
  calls.length = 0;
  tiny.paneViews()[0].renderer().draw(target);
  eq(calls.map((c) => Math.round(c.h * 100) / 100), [3.5 - 1, 3.5 - 1], "高度大於 3 像素才留 1 像素的縫");
  calls.length = 0;
  prim.setData(data);
  prim.detached();
  view.renderer().draw(target);
  eq(calls.length, 0, "拆掉之後不畫、也不會丟例外");
  prim.setData(data);                                              // 拆掉之後換資料不能丟例外
  ok(true, "拆掉之後 setData 不丟例外");
  const prim2 = new P.ProfilePrimitive();
  prim2.attached({ series: { priceToCoordinate: () => null }, requestUpdate: () => {} });
  prim2.setData(data);
  calls.length = 0;
  prim2.paneViews()[0].renderer().draw(target);
  eq(calls.length, 0, "價格軸算不出位置：整個不畫");
}

console.log(`${passed} PASS, ${failed} FAIL`);
process.exit(failed ? 1 : 0);
