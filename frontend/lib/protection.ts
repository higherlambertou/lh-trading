// 沒有停損保護的持倉（後端 core/protection.py 的 unprotected；update.md 發現 32）。
// 欄位名稱要和後端一致（tests/test_frontend_protection.py 用後端現算的資料渲染，欄位改了沒跟著改會被抓到）。

export interface Unprotected {
  code: string;
  direction: string;      // "Buy" | "Sell"
  quantity: number;       // 還沒被停損覆蓋的口數
  total: number;          // 這筆持倉的總口數
}

export const dirZh = (d: string): string => (d === "Buy" ? "多" : d === "Sell" ? "空" : "?");

// 「TMFJ6 多 1 口、MXFJ6 空 2 口（共 3 口）」
export function describeUnprotected(items: Unprotected[]): string {
  return items.map((i) => `${i.code} ${dirZh(i.direction)} ${i.quantity} 口${i.total > i.quantity ? `（共 ${i.total} 口）` : ""}`).join("、");
}
