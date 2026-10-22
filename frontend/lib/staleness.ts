// 部位面板的「資料過期」判斷（update.md 發現 69）：照後端說的刷新間隔判斷，不是固定 30 秒。
// 以前把保證金（本來就 60 秒才刷新一次）也拿 30 秒判斷，每分鐘大約一半時間誤報「後端可能重啟中」；週末部位改成 2 分鐘刷新後，還會誤報「部位資料已過期」。
export interface Cadence { positions_every?: number; margin_every?: number }

/** 部位超過這麼多秒沒更新才算過期：預期間隔的 3 倍，至少 30 秒。 */
export const posLimit = (c?: Cadence | null): number => Math.max(30, 3 * (c?.positions_every ?? 5));
/** 保證金超過這麼多秒沒更新才算過期：預期間隔的 2.5 倍，至少 150 秒。 */
export const marginLimit = (c?: Cadence | null): number => Math.max(150, 2.5 * (c?.margin_every ?? 60));

/** 要警告的過期秒數（超過門檻的那幾個裡最大的）；都沒過期回 null。age < 0（從未取得）不在這裡判斷。 */
export function staleAge(dataAge: number, posAge: number, c?: Cadence | null): number | null {
  const over: number[] = [];
  if (posAge > posLimit(c)) over.push(posAge);
  if (dataAge > marginLimit(c)) over.push(dataAge);
  return over.length ? Math.max(...over) : null;
}
