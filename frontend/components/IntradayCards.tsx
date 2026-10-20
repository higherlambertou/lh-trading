import { Fragment } from "react";
import type { LiveState } from "@/lib/api";
import { depthShare, imbalanceText, minutesText, pct, pctPair, price, profileHeights, SESSION_TEXT, signed } from "@/lib/intraday";

// 盤中即時面板的三張卡片：買賣掛單（五檔）／均價與基差／時段脈絡。
// 資料來源是報價事件本來就帶的欄位（後端新近才轉發），純顯示、不影響下單；舊後端沒有這些欄位時各卡片顯示「尚無」。
// 沒有 hooks、沒有網路：整個畫面由 LiveStatePanel 傳進來的 st 決定，測試直接 renderToStaticMarkup（tests/frontend/intraday_check.js）。

const GREEN = "#00e676";
const RED = "#ff1744";
const YELLOW = "#ffc107";
const CARD = "bg-[#0d0d14] rounded-lg border border-[#1e1e3a] p-3 space-y-1.5";
const TITLE = "text-[10px] text-[#7070a0] uppercase tracking-widest";
const EMPTY = "text-[11px] text-[#404060]";

export function BookCard({ st }: { st: LiveState }) {
  const b = st.book, f = st.session_flow;
  const share = depthShare(b?.bv5, b?.av5);
  const stale = b != null && b.age > 10;
  return (
    <div className={CARD}>
      <div className={TITLE}>買賣掛單（五檔）</div>
      {!b ? (
        <div className={EMPTY}>尚無五檔資料（後端更新並重啟後才有）</div>
      ) : (
        <div className={`space-y-1.5 ${stale ? "opacity-50" : ""}`}>
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-2xl">{b.spread == null ? "—" : `${b.spread} 點`}</span>
            <span className="text-[11px] text-[#7070a0] font-mono">
              價差{b.spread_avg != null && ` · 近 ${b.window_sec} 秒平均 ${b.spread_avg}`}
            </span>
          </div>
          <div className="flex items-baseline justify-between text-[11px] font-mono whitespace-nowrap">
            <span style={{ color: GREEN }}>買 {b.bv5} 口</span>
            <span style={{ color: RED }}>賣 {b.av5} 口</span>
          </div>
          <div className="h-1.5 rounded bg-[#0d0d14] overflow-hidden flex">
            {share != null && (
              <>
                <div style={{ width: `${share * 100}%`, background: GREEN, opacity: 0.8 }} />
                <div style={{ width: `${(1 - share) * 100}%`, background: RED, opacity: 0.8 }} />
              </>
            )}
          </div>
          <div className="text-[11px] text-[#7070a0] font-mono">
            五檔失衡：{imbalanceText(b.imb5)}
            {b.imb5_avg != null && `（近 ${b.window_sec} 秒平均：${imbalanceText(b.imb5_avg)}）`}
          </div>
          <div className="text-[10px] text-[#404060] font-mono">
            買一 {price(b.bid1)} × {b.bv1} · 賣一 {price(b.ask1)} × {b.av1}
            {stale && ` · ${Math.round(b.age)} 秒前`}
          </div>
        </div>
      )}
      {f && (
        <div className="pt-1.5 border-t border-[#1e1e3a] space-y-0.5">
          <div className="flex items-baseline justify-between text-[11px] font-mono">
            <span className="text-[#7070a0]">{SESSION_TEXT[f.session] ?? f.session}累計（交易所）</span>
            <span>外盤 {pctPair(f.share)[0]} · 內盤 {pctPair(f.share)[1]}</span>
          </div>
          <div className="text-[10px] text-[#404060] font-mono">
            外盤 {f.outside.toLocaleString("en-US")} 口 / 內盤 {f.inside.toLocaleString("en-US")} 口（整個時段，重啟後也在）
          </div>
        </div>
      )}
    </div>
  );
}

export function VwapCard({ st }: { st: LiveState }) {
  const v = st.vwap, bs = st.basis, ex = st.ex_range;
  return (
    <div className={CARD}>
      <div className={TITLE}>均價與基差</div>
      {!v && !bs ? (
        <div className={EMPTY}>尚無資料（後端更新並重啟後才有）</div>
      ) : (
        <>
          {v && (
            <>
              <div className="flex items-baseline gap-2">
                <span className="font-mono text-2xl">{price(v.price)}</span>
                <span className="text-[11px] text-[#7070a0] font-mono">{SESSION_TEXT[v.session] ?? v.session}均價（交易所）</span>
              </div>
              <div className="text-[11px] text-[#7070a0] font-mono">現價 {signed(v.diff, 1)} 點</div>
            </>
          )}
          {bs && (
            <div className="text-[11px] text-[#7070a0] font-mono">
              {bs.valid ? (
                <>基差 <span className="text-[#e0e0f0]">{signed(bs.value, 1)}</span> 點（期貨 − 現貨 {price(bs.spot, 2)}）</>
              ) : (
                "基差：現貨只在 09:00~13:30 更新，現在不顯示"
              )}
            </div>
          )}
          {ex != null && ex.mismatch > 0 && (
            <div className="text-[11px]" style={{ color: YELLOW }}>
              自己累計的日盤高低與交易所不一致 {ex.mismatch} 次（已寫入 log）
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function ContextCard({ st }: { st: LiveState }) {
  const c = st.ctx;
  const heights = profileHeights(c?.profile);
  const cur = c?.bucket?.idx;                      // 現在所在的那一格（亮綠色）
  return (
    <div className={CARD}>
      <div className={TITLE}>時段脈絡</div>
      {!c || c.session === "other" ? (
        <div className={EMPTY}>{c ? "非交易時段" : "尚無資料（後端更新並重啟後才有）"}</div>
      ) : c.session === "night" ? (
        <>
          <div className="text-sm text-[#e0e0f0]">夜盤 · 距收盤（{c.close_hhmm}）還有 {minutesText(c.mins_to_close)}</div>
          <div className={EMPTY}>夜盤沒有足夠的歷史樣本，不提供時段倍數與預期振幅。</div>
        </>
      ) : (
        <>
          {c.bucket ? (
            <>
              <div className="text-[11px] text-[#7070a0] font-mono">
                現在 {c.bucket.label} · 每分鐘波動約全日平均的 <span className="text-[#e0e0f0]">{c.bucket.mult.toFixed(2)}×</span>
              </div>
              <div className="flex items-end gap-px h-6" aria-label="各時段每分鐘波動倍數（08:45 起每 15 分鐘一格）">
                {heights.map((h, i) => (
                  <div key={i} className="flex-1 rounded-sm"
                    style={{ height: `${Math.round(h * 100)}%`, background: i === cur ? GREEN : "#2a2a4a" }} />
                ))}
              </div>
              <div className="flex justify-between text-[9px] text-[#404060] font-mono"><span>08:45</span><span>13:45</span></div>
            </>
          ) : (
            <div className={EMPTY}>{c.ready ? "時段倍數資料不足" : "時段脈絡計算中（背景，約數秒）…"}</div>
          )}
          <div className="text-[11px] text-[#7070a0] font-mono">
            距日盤收盤 {minutesText(c.mins_to_close)}（{c.close_hhmm}）
            {c.settle_day && <span style={{ color: YELLOW }}> · 結算日：近月合約 13:30 收盤</span>}
          </div>
          {c.day_iv?.expect ? (
            <div className="space-y-0.5">
              <div className="text-sm text-[#e0e0f0]">
                開盤前預期日盤振幅約 <span className="font-mono">{price(c.day_iv.expect.mid, 0)}</span> 點
                <span className="text-[11px] text-[#7070a0] font-mono">（68% 區間 {price(c.day_iv.expect.lo, 0)}~{price(c.day_iv.expect.hi, 0)}）</span>
              </div>
              <div className="text-[11px] text-[#7070a0] font-mono">
                依 {c.day_iv.vix_date ? c.day_iv.vix_date.slice(5).replace("-", "/") : "前一天"} 臺指選擇權波動率指數 {c.day_iv.vix?.toFixed(2)}；近 20 日均 {price(c.day_iv.r20, 0)} 點
              </div>
              <div className="text-[10px] text-[#404060] leading-snug">
                歷史檢驗：比只看近 20 日均，平方誤差約降 15%（平均絕對誤差降約 9%），但各年差很多（2022～2023 幾乎沒幫助），2025 年起區間實際涵蓋約 63%（略偏窄）。
                只對整天的來回幅度有用；是範圍，不是目標價；範圍帶與建議停損沒有用它。
              </div>
            </div>
          ) : (
            c.day_iv?.note && <div className={EMPTY}>預期日盤振幅：{c.day_iv.note}</div>
          )}
          {c.proj ? (
            <div className="space-y-0.5">
              <div className="text-sm text-[#e0e0f0]">
                之後預期振幅約 <span className="font-mono">{price(c.proj.expect.mid, 0)}</span> 點
                <span className="text-[11px] text-[#7070a0] font-mono">（68% 區間 {price(c.proj.expect.lo, 0)}~{price(c.proj.expect.hi, 0)}）</span>
              </div>
              <div className="text-[11px] text-[#7070a0] font-mono">
                依開盤後 {c.proj.c} 分振幅 {price(c.proj.early, 0)} 點；{c.proj.c === 30 ? "09:15" : "09:45"} 後已走 {price(c.proj.done, 0)} 點
                {c.proj.done_ratio != null && `（預期中位數的 ${Math.round(c.proj.done_ratio * 100)}%）`}
              </div>
            </div>
          ) : (
            c.proj_note && <div className={EMPTY}>{c.proj_note}</div>
          )}
          {c.proj && (
            <div className="text-[10px] text-[#404060] leading-snug">
              歷史檢驗（約 1,600 日）：比只看近 20 日均（{price(c.r20, 0)} 點）略準，樣本外誤差約降 6%，仍有約 ±40% 的不確定。是範圍，不是目標價。
            </div>
          )}
        </>
      )}
    </div>
  );
}

// 各合約（大台／小台／微台）近 5／15／30 分鐘的外盤占比（口數）並排；放在〈外/內盤〉那張卡片的下面。
// 只是並排顯示：大台與微台的差有沒有意義沒驗證過，所以不上色、不下判讀。舊後端沒有 xc 欄位就整塊不顯示。
export function CrossFlow({ st }: { st: LiveState }) {
  const xc = st.xc;
  if (!xc) return null;
  const rows = Object.entries(xc.windows).sort((a, b) => Number(a[0]) - Number(b[0]));
  const cols = [{ key: "TXF", label: "大台" }, { key: "MXF", label: "小台" }, { key: "TMF", label: "微台" }] as const;
  return (
    <div className="pt-2 border-t border-[#1e1e3a] space-y-1">
      <div className="text-[10px] text-[#7070a0] uppercase tracking-widest">各合約外盤占比（口數）</div>
      <div className="grid grid-cols-[3.4rem_repeat(4,minmax(0,1fr))] gap-x-1 gap-y-0.5 text-[11px] font-mono whitespace-nowrap">
        <span />
        {cols.map((c) => <span key={c.key} className="text-right text-[#7070a0]">{c.label}</span>)}
        <span className="text-right text-[#7070a0]">微−大</span>
        {rows.map(([w, r]) => (
          <Fragment key={w}>
            <span className="text-[#7070a0]">近 {w} 分</span>
            {cols.map((c) => <span key={c.key} className="text-right">{pct(r[c.key]?.share)}</span>)}
            <span className="text-right">{r.gap == null ? "—" : signed(r.gap * 100, 0)}</span>
          </Fragment>
        ))}
      </div>
      <div className="text-[10px] text-[#404060] leading-snug">
        成交少於 {xc.min_trades} 筆的格子不顯示；微−大＝微台減大台（百分點）。只是並排顯示，兩者的差有沒有意義沒驗證過。
      </div>
    </div>
  );
}

export default function IntradayCards({ st }: { st: LiveState }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
      <BookCard st={st} />
      <VwapCard st={st} />
      <ContextCard st={st} />
    </div>
  );
}
