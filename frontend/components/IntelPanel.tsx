"use client";

import { useEffect, useState } from "react";
import { api, apiErrorDetail } from "@/lib/api";
import {
  LIGHT_COLOR, MUTED, WARN, ageText, collectorLight, countdown, eventWhen, fmt, friendlyIntelError, marketChange, marketDate, marketValue, newsAge, signed, stars, systemLight, systemText, upDown,
  type IntelChips, type IntelEvent, type IntelMarket, type IntelNews, type IntelSummary,
} from "@/lib/intel";

// 〈資訊〉：事件倒數、全球市場、籌碼、新聞、資訊服務狀態。資料來自另一個 repo（lh-intel）的 intel.db，**唯讀、只顯示、不影響任何交易決策**。
// 數字的日期各有各的意義（美股是美國當地交易日、籌碼是台北交易日）；資料超過 3 天沒更新會標「N 天前」。後端 core/intel_reader.py。
// 區塊元件沒有 hooks，測試直接渲染。

const CARD = "bg-[#141420] rounded-xl border border-[#1e1e3a] p-4";
const H = "text-[11px] text-[#7070a0] uppercase tracking-widest";
const TH = "text-left font-normal text-[10px] text-[#7070a0] px-1.5 py-1 whitespace-nowrap";
const TD = "px-1.5 py-1 text-xs font-mono text-[#e0e0f0] whitespace-nowrap";

export function EventsCard({ events }: { events: NonNullable<IntelSummary["events"]> }) {
  return (
    <div className={`${CARD} space-y-2`}>
      <h2 className={H}>近期事件（重要度 ★★ 以上，台北時間）</h2>
      {events.upcoming.length === 0 && <div className="text-xs text-[#7070a0]">未來兩週沒有重要事件。</div>}
      <ul className="space-y-1">
        {events.upcoming.map((e: IntelEvent) => (
          <li key={e.uid} className="flex items-baseline gap-2 text-xs" style={{ opacity: e.past ? 0.5 : 1 }}>
            <span className="text-[#ffc107] w-10 shrink-0 text-[10px]" title={`重要度 ${e.importance}`}>{stars(e.importance)}</span>
            <span className="text-[#e0e0f0] min-w-0 flex-1">{e.title}<span className="text-[10px] text-[#505070]">　{e.country}</span></span>
            <span className="font-mono text-[10px] text-[#7070a0] whitespace-nowrap">{eventWhen(e)}</span>
            <span className="font-mono text-[10px] w-20 text-right whitespace-nowrap" style={{ color: e.minutes_to != null && e.minutes_to >= 0 && e.minutes_to < 180 ? WARN : MUTED }}>{countdown(e)}</span>
          </li>
        ))}
      </ul>
      {events.closures.length > 0 && (
        <div className="text-[11px] text-[#7070a0] border-t border-[#1e1e3a] pt-2">
          台股休市：{events.closures.map((c) => `${c.date.slice(5).replace("-", "/")} ${c.title}（${c.days_to === 0 ? "今天" : `${c.days_to} 天後`}）`).join("　")}
        </div>
      )}
    </div>
  );
}

export function MarketsCard({ markets }: { markets: IntelMarket[] }) {
  return (
    <div className={`${CARD} space-y-2`}>
      <h2 className={H}>全球市場（日收盤；日期是該市場當地的交易日）</h2>
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead><tr><th className={TH}>項目</th><th className={`${TH} text-right`}>收盤</th><th className={`${TH} text-right`}>較前一日</th><th className={TH}>日期</th></tr></thead>
          <tbody>
            {markets.map((m) => (
              <tr key={m.key} className="border-t border-[#1e1e3a]">
                <td className="px-1.5 py-1 text-xs text-[#e0e0f0] whitespace-nowrap">{m.label}</td>
                <td className={`${TD} text-right`} style={{ color: m.value == null ? MUTED : undefined }}>{marketValue(m)}</td>
                <td className={`${TD} text-right`} style={{ color: upDown(m.kind === "rate" ? m.chg : m.chg) }}>{marketChange(m)}</td>
                <td className="px-1.5 py-1 text-[10px] font-mono whitespace-nowrap" style={{ color: m.age_days != null && m.age_days > 3 ? WARN : MUTED }}>{marketDate(m)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const lots = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString("zh-TW"));
const dateText = (d: string) => d.slice(5).replace("-", "/");

export function ChipsCard({ chips }: { chips: IntelChips }) {
  const { futures, options, pcr, inst, margin, market } = chips;
  const empty = !futures.length && !options && !pcr && !inst && !margin && !market;
  return (
    <div className={`${CARD} space-y-3`}>
      <h2 className={H}>籌碼（盤後；台北交易日）</h2>
      {empty && <div className="text-xs text-[#7070a0]">還沒有籌碼資料。</div>}
      {futures.map((f) => (
        <div key={f.commodity}>
          <div className="text-[11px] text-[#7070a0] mb-0.5">三大法人期貨 · {f.label}（未平倉淨口數，{dateText(f.date)}）</div>
          <table className="w-full">
            <thead><tr><th className={TH}>身份別</th><th className={`${TH} text-right`}>淨未平倉</th><th className={`${TH} text-right`}>較前一日</th><th className={`${TH} text-right`}>多／空</th></tr></thead>
            <tbody>
              {f.rows.map((r) => (
                <tr key={r.who} className="border-t border-[#1e1e3a]">
                  <td className="px-1.5 py-1 text-xs text-[#e0e0f0]">{r.label}</td>
                  <td className={`${TD} text-right`} style={{ color: upDown(r.net_oi) }}>{signed(r.net_oi, 0)}</td>
                  <td className={`${TD} text-right`} style={{ color: upDown(r.chg) }}>{signed(r.chg, 0)}</td>
                  <td className={`${TD} text-right`} style={{ color: MUTED }}>{lots(r.long_oi)}／{lots(r.short_oi)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      {options && (
        <div>
          <div className="text-[11px] text-[#7070a0] mb-0.5">三大法人臺指選擇權（未平倉淨口數，{dateText(options.date)}）</div>
          <table className="w-full">
            <thead><tr><th className={TH}>身份別</th><th className={`${TH} text-right`}>買權 Call</th><th className={`${TH} text-right`}>賣權 Put</th></tr></thead>
            <tbody>
              {options.rows.map((r) => (
                <tr key={r.who} className="border-t border-[#1e1e3a]">
                  <td className="px-1.5 py-1 text-xs text-[#e0e0f0]">{r.label}</td>
                  <td className={`${TD} text-right`}>{signed(r.call_net_oi, 0)}<span className="text-[10px]" style={{ color: upDown(r.call_chg) }}>（{signed(r.call_chg, 0)}）</span></td>
                  <td className={`${TD} text-right`}>{signed(r.put_net_oi, 0)}<span className="text-[10px]" style={{ color: upDown(r.put_chg) }}>（{signed(r.put_chg, 0)}）</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {pcr && (
        <div className="text-xs flex flex-wrap gap-x-4 gap-y-0.5">
          <span className="text-[#7070a0]">Put/Call 比（{dateText(pcr.date)}）</span>
          <span className="font-mono text-[#e0e0f0]">未平倉 {fmt(pcr.oi_ratio)}%<span className="text-[10px] text-[#7070a0]">（前日 {fmt(pcr.oi_ratio_prev)}）</span></span>
          <span className="font-mono text-[#e0e0f0]">成交量 {fmt(pcr.vol_ratio)}%<span className="text-[10px] text-[#7070a0]">（前日 {fmt(pcr.vol_ratio_prev)}）</span></span>
        </div>
      )}
      {inst && (
        <div>
          <div className="text-[11px] text-[#7070a0] mb-0.5">現貨三大法人買賣超（億元，{dateText(inst.date)}）</div>
          <table className="w-full">
            <thead><tr><th className={TH}>身份別</th><th className={`${TH} text-right`}>今天</th><th className={`${TH} text-right`}>近 5 日合計</th></tr></thead>
            <tbody>
              {inst.rows.map((r) => (
                <tr key={r.who} className="border-t border-[#1e1e3a]">
                  <td className="px-1.5 py-1 text-xs text-[#e0e0f0]">{r.label}</td>
                  <td className={`${TD} text-right`} style={{ color: upDown(r.net) }}>{signed(r.net, 1)}</td>
                  <td className={`${TD} text-right`} style={{ color: upDown(r.net5) }}>{signed(r.net5, 1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {market && (
        <div className="text-xs space-y-0.5">
          {market.turnover_100m != null && (
            <div className="flex flex-wrap gap-x-4 gap-y-0.5">
              <span className="text-[#7070a0]">大盤成交（{market.date ? dateText(market.date) : "—"}）</span>
              <span className="font-mono text-[#e0e0f0]">{fmt(market.turnover_100m, 0)} 億{market.turnover_ratio20 != null && <span className="text-[10px] text-[#7070a0]">（前 {market.turnover_days ?? 20} 日平均的 {fmt(market.turnover_ratio20, 2)} 倍）</span>}</span>
            </div>
          )}
          {market.up != null && market.down != null && (
            <div className="flex flex-wrap gap-x-4 gap-y-0.5">
              <span className="text-[#7070a0]">股票漲跌家數（{market.breadth_date ? dateText(market.breadth_date) : "—"}）</span>
              <span className="font-mono"><span style={{ color: upDown(1) }}>上漲 {lots(market.up)}{market.limit_up != null && `（漲停 ${lots(market.limit_up)}）`}</span>　<span style={{ color: upDown(-1) }}>下跌 {lots(market.down)}{market.limit_down != null && `（跌停 ${lots(market.limit_down)}）`}</span>{market.flat != null && <span className="text-[#7070a0]">　持平 {lots(market.flat)}</span>}</span>
            </div>
          )}
        </div>
      )}
      {margin && (
        <div className="text-xs flex flex-wrap gap-x-4 gap-y-0.5">
          <span className="text-[#7070a0]">融資融券（{dateText(margin.date)}{margin.provisional ? "，暫定" : ""}）</span>
          <span className="font-mono text-[#e0e0f0]">融資餘額 {fmt(margin.financing_100m, 1)} 億<span className="text-[10px]" style={{ color: upDown(margin.financing_chg_100m) }}>（{signed(margin.financing_chg_100m, 1)}）</span></span>
          <span className="font-mono text-[#e0e0f0]">融券 {lots(margin.short_lots)} 張<span className="text-[10px]" style={{ color: upDown(margin.short_chg) }}>（{signed(margin.short_chg, 0)}）</span></span>
        </div>
      )}
      <div className="text-[10px] leading-snug text-[#404060]">期貨「淨未平倉」＝多方未平倉 − 空方未平倉（負數＝淨空）。融資餘額是當天暫定值，隔天才會變成最終值。</div>
    </div>
  );
}

export function NewsCard({ news }: { news: IntelNews[] }) {
  return (
    <div className={`${CARD} space-y-2`}>
      <h2 className={H}>最新新聞標題（中央社、Google News；只存標題）</h2>
      {news.length === 0 && <div className="text-xs text-[#7070a0]">還沒有新聞。</div>}
      <ul className="space-y-1.5">
        {news.map((n, i) => (
          <li key={i} className="text-xs leading-snug">
            <a href={n.url} target="_blank" rel="noopener noreferrer" className="text-[#e0e0f0] hover:text-[#3b82f6]">{n.title}</a>
            <div className="text-[10px] text-[#7070a0]">{n.label}・{n.taipei_time}（{newsAge(n.age_min)}）</div>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function HealthBar({ collectors }: { collectors: NonNullable<IntelSummary["collectors"]> }) {
  const light = systemLight(collectors);
  return (
    <details className={`${CARD} !py-2.5`}>
      <summary className="cursor-pointer flex items-center gap-2 text-xs text-[#e0e0f0]">
        <span className="inline-block w-2 h-2 rounded-full" style={{ background: LIGHT_COLOR[light] }} />
        {systemText(collectors)}
        <span className="text-[10px] text-[#505070] ml-auto">展開收集器狀態</span>
      </summary>
      <table className="w-full mt-2">
        <thead><tr><th className={TH}>收集器</th><th className={TH}>最後成功</th><th className={TH}>狀態</th></tr></thead>
        <tbody>
          {collectors.items.map((c) => (
            <tr key={c.name} className="border-t border-[#1e1e3a]">
              <td className="px-1.5 py-1 text-xs font-mono text-[#e0e0f0] whitespace-nowrap"><span className="inline-block w-1.5 h-1.5 rounded-full mr-1.5" style={{ background: LIGHT_COLOR[collectorLight(c)] }} />{c.name}</td>
              <td className="px-1.5 py-1 text-[11px] text-[#7070a0] whitespace-nowrap">{ageText(c.last_ok_age_sec)}</td>
              <td className="px-1.5 py-1 text-[11px] whitespace-nowrap" style={{ color: c.fails ? "#ff1744" : MUTED }}>{c.fails ? `連續失敗 ${c.fails} 次` : c.last_status === "not_ready" ? "等來源公布" : c.last_status ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

export default function IntelPanel() {
  const [data, setData] = useState<IntelSummary | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    const load = async () => {
      try { const d = await api.intel.summary(); if (!stop) { setData(d); setErr(null); } }
      catch (e) { if (!stop) setErr(friendlyIntelError(apiErrorDetail(e))); }
    };
    load();
    const id = setInterval(load, 30000);                    // 只讀本機資料庫（10 秒快取），30 秒一次就夠
    return () => { stop = true; clearInterval(id); };
  }, []);

  return (
    <div className="space-y-4 lg:col-span-2">
      <div className="text-[11px] leading-snug text-[#7070a0]">
        資料來自另一個系統（lh-intel）的資料庫，<span className="text-[#ffc107]">只顯示、不影響任何交易決策</span>；沒有任何策略或風控讀它。
        {data?.available && <span className="text-[#505070]">　更新 {new Date(data.as_of * 1000).toLocaleTimeString("zh-TW", { hour12: false })}</span>}
      </div>
      {err && !data && <div className="text-xs text-[#ffc107]">{err}</div>}
      {err && data && <div className="text-[10px] text-[#ffc107]">更新失敗：{err}（顯示的是上一次的）</div>}
      {data && !data.available && <div className={`${CARD} text-xs text-[#ffc107]`}>資訊系統沒有資料：{data.reason}</div>}
      {data?.available && data.collectors && <HealthBar collectors={data.collectors} />}
      {data?.available && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
          <div className="space-y-4">
            {data.events && <EventsCard events={data.events} />}
            {data.markets && <MarketsCard markets={data.markets} />}
            {data.news && <NewsCard news={data.news} />}
          </div>
          <div className="space-y-4">
            {data.chips && <ChipsCard chips={data.chips} />}
          </div>
        </div>
      )}
    </div>
  );
}
