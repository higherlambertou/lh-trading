"use client";

import { useEffect, useState } from "react";
import { api, apiErrorDetail } from "@/lib/api";
import {
  ageText, fmt, friendlyRangeError, hasClose, holdoutText, pickBand, sideText,
  type RangeBand, type RangeData, type RangeLevel,
} from "@/lib/range";

// 機率範圍（update.md 發現 48）：以最新成交價為中心，T 分鐘後（落點）與這段時間內（期間）有 50%／80%／95% 的機率落在哪個範圍。
// 只說會走多遠、不說往哪個方向；不是進場訊號、不碰下單。範圍是用歷史實測算的，不是模擬。

const Cell = ({ label, side, bad }: { label: string; side: RangeLevel["term"]; bad: boolean }) => (
  <div className="flex items-baseline gap-1.5 flex-wrap">
    <span className="text-[10px] text-[#7070a0] w-7">{label}</span>
    <span className="font-mono text-[11px] text-[#e0e0f0]">{sideText(side)}</span>
    {bad && <span className="text-[10px] text-[#ffc107]" title="這一格在樣本外驗證沒通過（或前後半差太多），範圍可能偏窄或不穩">⚠不穩</span>}
  </div>
);

export function RangeTable({ band, title }: { band: RangeBand; title: string }) {
  return (
    <div className="space-y-1">
      <div className="text-[10px] text-[#7070a0]">{title}{band.few && <span className="text-[#ffc107]">　實測樣本少（夜盤只有幾十晚），僅供參考</span>}</div>
      {band.levels.map((lv) => (
        <div key={lv.p} className="rounded-lg bg-[#0d0d14] px-3 py-1.5 flex gap-3 items-start">
          <span className="text-xs font-semibold text-[#e0e0f0] w-9 pt-0.5">{lv.p}%</span>
          <div className="space-y-0.5 min-w-0">
            <Cell label="落點" side={lv.term} bad={lv.unstable_term} />
            <Cell label="期間" side={lv.path} bad={lv.unstable_path} />
          </div>
        </div>
      ))}
    </div>
  );
}

export function RangeBody({ data, horizon }: { data: RangeData; horizon: number }) {
  const band = pickBand(data, String(horizon));
  if (!band) return <div className="text-xs text-[#ffc107]">{data.note || "這個持有時間沒有實測表"}</div>;
  const close = hasClose(data) ? data.bands.close : null;
  const ho = holdoutText(data.cal);
  return (
    <div className="space-y-2">
      <div className="text-[11px] text-[#7070a0] leading-snug">
        以最新成交價 <span className="font-mono text-[#e0e0f0]">{data.price != null ? fmt(data.price) : "—"}</span> 為中心
        {data.stale && <span className="text-[#ffc107]">（這個價是{ageText(data.price_age_sec)}的，行情可能已經動了）</span>}
        ；<span className="text-[#e0e0f0]">落點</span>＝{horizon} 分鐘後價格落在這裡的機率、<span className="text-[#e0e0f0]">期間</span>＝這段時間內最高最低都沒超出的機率（停損停利碰不碰到看這個）。
      </div>
      <RangeTable band={band} title={`${horizon} 分鐘`} />
      {close && <RangeTable band={close} title={`到收盤（剩 ${close.minutes} 分鐘）`} />}
      <div className="text-[10px] leading-snug text-[#404060]">
        <span className="text-[#ffc107]">只說會走多遠、不說往哪個方向</span>；不是進場訊號。範圍來自歷史實測（不是模擬）：{data.cal?.day_sessions ? `日盤 ${data.cal.day_sessions.toLocaleString()} 個時段（${data.cal.from} ~ ${data.cal.to}）` : "沒有日盤資料"}。
        {ho && <>　{ho}；標「⚠不穩」的格子沒通過。</>}
        {data.session === "day" && data.remaining != null && data.remaining < 90 && <>　接近收盤時價格比最近 30 分鐘的波動更容易放大，範圍偏窄的可能性較高。</>}
      </div>
    </div>
  );
}

export default function RangeSection({ horizon }: { horizon: number }) {
  const [data, setData] = useState<RangeData | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    const load = async () => {
      try { const d = await api.market.range(); if (!stop) { setData(d); setErr(null); } }
      catch (e) { if (!stop) setErr(friendlyRangeError(apiErrorDetail(e))); }
    };
    load();
    const id = setInterval(load, 15000);                  // 只讀本機資料庫與記憶體
    return () => { stop = true; clearInterval(id); };
  }, []);

  return (
    <div className="space-y-1.5">
      <div className="text-[11px] text-[#7070a0] uppercase tracking-widest">機率範圍（依波動）</div>
      {err && !data && <div className="text-xs text-[#ffc107]">{err}</div>}
      {data && <RangeBody data={data} horizon={horizon} />}
      {err && data && <div className="text-[10px] text-[#ffc107]">更新失敗：{err}（顯示的是上一次的）</div>}
    </div>
  );
}
