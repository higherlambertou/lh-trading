"use client";

import { useCallback, useEffect, useState } from "react";
import { api, apiErrorDetail } from "@/lib/api";
import {
  HORIZONS, calText, friendlySuggestError, lotsText, money, needPts, pct, sessionText,
  type Suggest, type SuggestRow,
} from "@/lib/stopSuggest";

// 依波動的建議停損停利（只顯示，不碰下單；update.md 發現 33）。
// 重點不是「勝率高」：進場沒有資訊時，每筆期望值＝−成本，跟停損停利怎麼放無關；停利小、停損大只是把勝率換成賺賠比。
// 這張卡告訴你：停損放在雜訊外要多遠、被雜訊掃到的機率多大、要靠時機比基準多贏幾個百分點才打平成本、單口虧多少。
const OPEN_KEY = "stop_suggest_open";

export function SuggestRows({ data, onApply }: { data: Suggest; onApply?: (sl: string, tp: string) => void }) {
  return (
    <div className="space-y-1.5">
      {data.rows.map((r: SuggestRow) => (
        <div key={r.name} className="rounded-lg bg-[#0d0d14] px-3 py-2">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-baseline gap-2 flex-wrap">
              <span className="text-xs text-[#e0e0f0]">{r.name}</span>
              <span className="font-mono text-xs"><span className="text-[#00e676]">停利 {r.tp}</span>　<span className="text-[#ff1744]">停損 {r.sl}</span></span>
            </div>
            {onApply && (
              <button onClick={() => onApply(String(r.sl), String(r.tp))}
                className="text-[11px] px-2 py-0.5 rounded border border-[#1e1e3a] text-[#e0e0f0] hover:border-[#3b82f6] whitespace-nowrap">帶入</button>
            )}
          </div>
          <div className="mt-1 text-[10px] leading-snug text-[#7070a0]">
            沒有技巧時的勝率 {pct(r.p0)}
            {r.touch_sl != null && <>・停損被雜訊掃到 {pct(r.touch_sl)}{r.touch_unstable ? "（不穩）" : ""}</>}
            ・單口停損 −{money(r.loss_twd)} 元{r.loss_pct != null && `（${r.loss_pct}%）`}
            {lotsText(r, data.risk_pct) && <>・{lotsText(r, data.risk_pct)}</>}
          </div>
          {r.within && (
            <div className="text-[10px] leading-snug text-[#404060]">
              {data.horizon} 分鐘內（沒有技巧時）：停利先到 {pct(r.within.tp)}／停損先到 {pct(r.within.sl)}／都沒到 {pct(r.within.none)}{r.win_unstable ? "（不穩）" : ""}
            </div>
          )}
          {r.far_tp && <div className="text-[10px] text-[#ffc107]">停利比今天預期的剩餘振幅上緣還遠，不容易碰到</div>}
        </div>
      ))}
    </div>
  );
}

export default function StopSuggestCard({ contract, onApply }: { contract: string; onApply: (sl: string, tp: string) => void }) {
  const [open, setOpen] = useState(true);
  const [horizon, setHorizon] = useState<number>(15);
  const [data, setData] = useState<Suggest | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    try { if (localStorage.getItem(OPEN_KEY) === "0") setOpen(false); } catch { /* 沒有 localStorage（隱私模式等）就維持展開 */ }
  }, []);

  const toggle = useCallback(() => {
    setOpen((o) => {
      try { localStorage.setItem(OPEN_KEY, o ? "0" : "1"); } catch { /* 忽略 */ }
      return !o;
    });
  }, []);

  const load = useCallback(async () => {
    try { setData(await api.market.suggest(contract, horizon)); setErr(null); }
    catch (e) { setErr(friendlySuggestError(apiErrorDetail(e))); }
  }, [contract, horizon]);

  useEffect(() => {
    if (!open) return;
    load();
    const id = setInterval(load, 30000);                  // 只讀本機資料庫與記憶體，30 秒一次就夠
    return () => clearInterval(id);
  }, [open, load]);

  return (
    <div className="mb-3 rounded-lg border border-[#1e1e3a] p-3">
      <div className="flex items-center gap-2 flex-wrap">
        <button onClick={toggle} className="text-[11px] text-[#7070a0] uppercase tracking-widest hover:text-[#e0e0f0]">
          {open ? "▾" : "▸"} 建議停損停利（依波動）
        </button>
        {open && (
          <div className="ml-auto flex items-center gap-1">
            <span className="text-[10px] text-[#404060]">持有約</span>
            {HORIZONS.map((h) => (
              <button key={h} onClick={() => setHorizon(h)}
                className={`text-[11px] px-1.5 py-0.5 rounded border ${h === horizon ? "border-[#3b82f6] text-[#e0e0f0]" : "border-[#1e1e3a] text-[#7070a0]"}`}>{h}</button>
            ))}
            <span className="text-[10px] text-[#404060]">分鐘</span>
          </div>
        )}
      </div>

      {open && (
        <div className="mt-2 space-y-2">
          {err && !data && <div className="text-xs text-[#ffc107]">{err}</div>}
          {data && (
            <>
              <div className="text-[11px] text-[#7070a0] leading-snug">
                最近 30 分鐘波動 {data.sigma != null ? <span className="font-mono text-[#e0e0f0]">{data.sigma}</span> : "—"} 點／分鐘
                {data.noise != null && <>　→ {data.horizon} 分鐘的雜訊範圍約 ±<span className="font-mono text-[#e0e0f0]">{data.noise}</span> 點</>}
                <span className="text-[#404060]">（{sessionText(data.session)}）</span>
              </div>
              {data.today && data.today.expect_mid != null && (
                <div className="text-[10px] text-[#7070a0]">
                  今天已走 {money(data.today.done)} 點，預期之後還會走約 {money(data.today.expect_mid)} 點（68% 區間 {money(data.today.expect_lo)}～{money(data.today.expect_hi)}）
                </div>
              )}
              {data.note && <div className="text-xs text-[#ffc107]">{data.note}</div>}
              {data.rows.length > 0 && (
                <>
                  <SuggestRows data={data} onApply={onApply} />
                  <div className="text-[10px] leading-snug text-[#7070a0]">
                    成本 {data.cost_pts} 點：要比「沒有技巧」的勝率多贏約 <span className="text-[#e0e0f0]">{needPts(Math.max(...data.rows.map((r) => r.need)))}</span> 個百分點才打平（成本 ÷ 停利＋停損）。
                    三種型態一樣：改比例只是把勝率換成賺賠比；持有越久、距離越遠，這個門檻越低。
                  </div>
                  <div className="text-[10px] leading-snug text-[#404060]">
                    <span className="text-[#ffc107]">勝率高 ≠ 賺錢</span>：進場沒有資訊時，每筆期望值＝−成本，與停損停利怎麼放無關。這張卡只決定停損離雜訊多遠、倉位多大。
                    {data.calibrated && data.cal ? <>　掃到機率與「都沒到」是實測（{calText(data.cal, data.session)}）。</> : <>　沒有校準表，沒有實測的掃到機率。</>}
                    {data.equity != null && <>　權益 {money(data.equity)} 元、單筆風險上限 {data.risk_pct}%、成本是假設值（SUGGEST_COST_PTS）。</>}
                  </div>
                </>
              )}
            </>
          )}
          {err && data && <div className="text-[10px] text-[#ffc107]">更新失敗：{err}</div>}
        </div>
      )}
    </div>
  );
}
