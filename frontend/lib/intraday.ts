// 盤中即時面板新增的三張卡片（買賣掛單／交易所均價與基差／時段脈絡）用的純函式：只做格式與文字，沒有 React、沒有網路。
// 測試：tests/frontend/intraday_check.js（由 tests/test_frontend_intraday.py 執行）。

export const MINUS = "−";   // 真正的減號（比連字號寬，數字對齊時比較好讀）

// 帶正負號的數字：+5／−3／0；null → "—"
export function signed(x: number | null | undefined, digits = 0): string {
  if (x == null || !Number.isFinite(x)) return "—";
  const r = Number(x.toFixed(digits));
  if (r === 0) return (0).toFixed(digits);
  return `${r > 0 ? "+" : MINUS}${Math.abs(r).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

// 價格：千分位、最多 1 位小數（交易所均價有 6 位小數，畫面上沒有意義）
export function price(x: number | null | undefined, digits = 1): string {
  if (x == null || !Number.isFinite(x)) return "—";
  return x.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

export const pct = (x: number | null | undefined, digits = 0): string =>
  x == null || !Number.isFinite(x) ? "—" : `${(x * 100).toFixed(digits)}%`;

// 外盤／內盤占比的兩個百分比，湊成加起來剛好 100%（各自四捨五入會出現 51% + 50%）；沒有資料 → 兩個都是 "—"
export function pctPair(share: number | null | undefined): [string, string] {
  if (share == null || !Number.isFinite(share)) return ["—", "—"];
  const a = Math.min(100, Math.max(0, Math.round(share * 100)));
  return [`${a}%`, `${100 - a}%`];
}

// 五檔失衡：(買量−賣量)/(買量+賣量)。|x| < 5% 當均衡，避免在 0 附近一直換邊
export const BALANCED = 0.05;
export function imbalanceText(imb: number | null | undefined): string {
  if (imb == null || !Number.isFinite(imb)) return "—";
  if (Math.abs(imb) < BALANCED) return "均衡";
  return `${imb > 0 ? "買方" : "賣方"}較厚 ${Math.round(Math.abs(imb) * 100)}%`;
}

// 分鐘 → "3 小時 25 分"／"45 分"
export function minutesText(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const m = Math.max(0, Math.round(n));
  return m >= 60 ? `${Math.floor(m / 60)} 小時${m % 60 ? ` ${m % 60} 分` : ""}` : `${m} 分`;
}

export const SESSION_TEXT: Record<string, string> = { day: "日盤", night: "夜盤", other: "非交易時段" };

// 買／賣深度條的買方比例（0~1）；沒有量時 null
export function depthShare(bv5: number | null | undefined, av5: number | null | undefined): number | null {
  if (bv5 == null || av5 == null || bv5 + av5 <= 0) return null;
  return bv5 / (bv5 + av5);
}

// 時段倍數的直條高度（0~1）：以整天最大的那格為滿格
export function profileHeights(profile: number[] | undefined): number[] {
  if (!profile || !profile.length) return [];
  const mx = Math.max(...profile);
  return mx > 0 ? profile.map((v) => Math.max(0.08, v / mx)) : profile.map(() => 0.08);
}
