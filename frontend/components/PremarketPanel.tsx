"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Globe2 } from "lucide-react";
import { api, apiErrorDetail, getBase } from "@/lib/api";
import {
  PM_KEYS, PM_LABEL, backfilledAt, fmtValue, isBackfill, lateText, missingCount, nextText, reasonText, statusChips, tpeTime, twSourceText, usSessionText,
  type PmCalendar, type PmField, type PmPreview, type PmSnapshot, type PmStatus,
} from "@/lib/premarket";

// 〈盤前跨市場快照〉：每個台股交易日 08:30 記一筆隔夜資料（台指期夜盤、費半、台積電 ADR、VIX），只記錄、不影響任何交易決策。
// 上面是最新一筆（還沒有紀錄時顯示「預覽」：如果現在記會長什麼樣，不會寫入），下面是最近的紀錄。後端 core/premarket_log.py。
// 檢視元件（StatusChips／FeatureTable／RecentTable）沒有 hooks，測試直接渲染。

const MUTED = "#7070a0", YELLOW = "#ffc107";
const TH = "text-left font-normal text-[10px] text-[#7070a0] px-2 py-1 whitespace-nowrap";
const TD = "px-2 py-1 text-xs font-mono text-[#e0e0f0] whitespace-nowrap";
// 「最近的紀錄」欄位多，手機寬度放得下四個值要靠短欄名與較窄的間距
const THS = "text-left font-normal text-[10px] text-[#7070a0] px-1.5 py-1 whitespace-nowrap";
const TDS = "px-1.5 py-1 text-xs font-mono text-[#e0e0f0] whitespace-nowrap";

// 後端是舊版（沒有這個 API）時 FastAPI 回 404「Not Found」：講白話
const friendlyError = (msg: string): string => (/^Not Found$/i.test(msg) ? "後端還沒有這個 API（盤前跨市場紀錄是新功能，要重啟後端才有）" : msg);

export function StatusChips({ s }: { s: PmStatus }) {
  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      {statusChips(s).map(([text, color]) => (
        <span key={text} className="text-[10px] px-1.5 py-0.5 rounded border font-mono whitespace-nowrap" style={{ color, borderColor: `${color}40`, background: `${color}15` }}>{text}</span>
      ))}
    </div>
  );
}

// 四個欄位：值、來源、資料本身的時間（台北）；缺值的原因放在欄位名稱下面（放在最右欄，手機上會被擠出畫面）
export function FeatureTable({ features, calendar }: { features: Record<string, PmField>; calendar: PmCalendar }) {
  return (
    <div className="space-y-1.5">
      <div className="overflow-x-auto">
        <table className="w-full max-w-3xl">
          <thead><tr><th className={TH}>欄位</th><th className={TH}>值</th><th className={TH}>來源</th><th className={TH}>資料時間（台北）</th></tr></thead>
          <tbody>
            {PM_KEYS.map((k) => {
              const f = features[k];
              const why = reasonText(f?.missing_reason);
              return (
                <tr key={k} className="border-t border-[#1e1e3a] align-top">
                  <td className={TD} title={PM_LABEL[k].hint}>
                    {PM_LABEL[k].name}
                    {why && <div className="text-[10px] font-sans text-[#ffc107] whitespace-normal max-w-[12rem] leading-snug">{why}</div>}
                  </td>
                  <td className={TD} style={{ color: f?.value == null ? MUTED : undefined }}>{fmtValue(k, f)}</td>
                  <td className={TD} style={{ color: MUTED }}>
                    {f?.source || "—"}
                    {backfilledAt(f) != null && <span className="text-[#404060]" title="來源比取樣時間晚更新，事後補進來的（資料本身的時間仍在取樣時間之前）">　補值於 {tpeTime(backfilledAt(f))}</span>}
                  </td>
                  <td className={TD} style={{ color: MUTED }}>{tpeTime(f?.source_timestamp)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="text-[10px] text-[#404060] leading-snug">
        {usSessionText(calendar)}；前一個台股交易日 {calendar.prev_tw_trading_day}；台股日曆來源：{twSourceText(calendar.tw_source)}。
        缺值就是缺值：不用前一天的值、也不補預設值。
      </div>
    </div>
  );
}

// 最近的紀錄：一天一列（備註另起一行，不然手機上備註欄會被壓得一字一行）；缺值的格子顯示「—」（滑鼠移上去看原因）
export function RecentTable({ rows }: { rows: PmSnapshot[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full max-w-2xl">
        <thead><tr><th className={THS}>台股交易日</th>{PM_KEYS.map((k) => <th key={k} className={THS} title={PM_LABEL[k].name}>{PM_LABEL[k].short}</th>)}</tr></thead>
        {rows.map((r) => {
          const notes = [r.voided ? `作廢：${r.void_reason ?? ""}` : "", r.corrected ? (isBackfill(r) ? "已補值" : "已更正") : "", lateText(r.late_seconds), missingCount(r.features) > 0 ? `缺 ${missingCount(r.features)} 欄` : ""].filter(Boolean).join("、");
          return (
            <tbody key={r.trading_date} className="border-t border-[#1e1e3a]" style={{ opacity: r.voided ? 0.45 : 1 }}>
              <tr>
                <td className={TDS}>{r.trading_date}</td>
                {PM_KEYS.map((k) => (
                  <td key={k} className={TDS} style={{ color: r.features[k]?.value == null ? MUTED : undefined }} title={reasonText(r.features[k]?.missing_reason)}>{fmtValue(k, r.features[k])}</td>
                ))}
              </tr>
              {notes && <tr><td colSpan={PM_KEYS.length + 1} className="px-1.5 pb-1 text-[11px] text-[#ffc107]">{notes}</td></tr>}
            </tbody>
          );
        })}
      </table>
    </div>
  );
}

export default function PremarketPanel() {
  const [status, setStatus] = useState<PmStatus | null>(null);
  const [rows, setRows] = useState<PmSnapshot[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [preview, setPreview] = useState<PmPreview | null>(null);
  const [pvBusy, setPvBusy] = useState(false);
  const [pvErr, setPvErr] = useState<string | null>(null);

  const hadErr = useRef(false);

  const loadPreview = useCallback(async () => {
    setPvBusy(true); setPvErr(null);
    try { setPreview(await api.premarket.preview()); }
    catch (e) { setPvErr(friendlyError(apiErrorDetail(e))); }
    finally { setPvBusy(false); }
  }, []);

  const load = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([api.premarket.status(), api.premarket.snapshots(60, 12)]);
      setStatus(s); setRows(r); setErr(null);
      if (hadErr.current) { hadErr.current = false; loadPreview(); }     // 後端剛從失敗中恢復（例如剛重啟）：預覽也重抓一次，不然會留著舊的錯誤訊息
    } catch (e) {
      hadErr.current = true;
      setErr(friendlyError(apiErrorDetail(e)));
    }
  }, [loadPreview]);

  useEffect(() => {
    load();
    loadPreview();
    const id = setInterval(load, 60000);                  // 狀態與紀錄只是讀本機資料庫，一分鐘一次就夠；預覽會去抓外部資料，只在載入與按鈕時做
    return () => clearInterval(id);
  }, [load, loadPreview]);

  const latest = rows ? rows.find((r) => !r.voided) ?? null : null;                  // 最新一筆『有效』的（作廢的只在下面的表格標示）
  const pvRow = preview && rows ? rows.find((r) => r.trading_date === preview.trading_date) : undefined;

  return (
    <div className="lg:col-span-2 bg-[#141420] rounded-xl border border-[#1e1e3a] p-5 space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <Globe2 size={14} className="text-[#3b82f6]" />
        <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest">盤前跨市場快照</h2>
        {status && <StatusChips s={status} />}
        <span className="text-[10px] text-[#404060]">只記錄、不影響任何交易決策。</span>
      </div>

      {err && <div className="text-xs text-[#ff1744]">{err}</div>}

      {status && (
        <div className="text-[11px] text-[#7070a0] leading-snug">
          每個台股交易日 {status.snapshot_time} 記一筆隔夜資料；下一筆：<span className="text-[#e0e0f0] font-mono">{nextText(status.next_snapshot)}</span>。來源：{status.sources.join("、")}。
          {status.missed_dates.length > 0 && <span style={{ color: YELLOW }}>　後端在 13:45 之後才啟動而錯過的日子：{status.missed_dates.join("、")}。</span>}
          {status.last_alert && <span style={{ color: YELLOW }}>　最近一次失敗警示：{status.last_alert.where}（{status.last_alert.trading_date}）{status.last_alert.error}</span>}
        </div>
      )}

      {/* 最新一筆；還沒有任何紀錄就顯示預覽 */}
      {latest ? (
        <div className="space-y-1.5">
          <div className="text-[10px] text-[#7070a0] uppercase tracking-widest">最新紀錄　{latest.trading_date}（{tpeTime(latest.fetched_at_utc)} 記下）{lateText(latest.late_seconds) && `　${lateText(latest.late_seconds)}`}</div>
          <FeatureTable features={latest.features} calendar={latest.calendar} />
        </div>
      ) : (
        status && !err && (
          <div className="text-xs text-[#7070a0]">
            {!status.enabled
              ? "盤前紀錄已關閉（.env 的 PREMARKET_LOG=false），不會記錄。"
              : `${rows && rows.length > 0 ? "目前沒有有效的紀錄（都已作廢）。" : "還沒有任何紀錄。"}下一筆在 ${nextText(status.next_snapshot)}。`}
          </div>
        )
      )}

      {(!err || preview) && (
      <div className="space-y-1.5">
        <div className="flex items-center gap-3 flex-wrap">
          <div className="text-[10px] text-[#7070a0] uppercase tracking-widest">
            預覽{preview ? `　${preview.trading_date}（${preview.recorded ? (pvRow?.voided ? "這一天的紀錄已作廢" : "這一天已經有紀錄") : "還沒有紀錄"}）` : ""}
          </div>
          <button onClick={loadPreview} disabled={pvBusy}
            className="text-[11px] px-2 py-0.5 rounded border border-[#1e1e3a] text-[#e0e0f0] hover:border-[#3b82f6] disabled:opacity-40">
            {pvBusy ? "抓取中…" : "重新預覽"}
          </button>
          <span className="text-[10px] text-[#404060]">如果現在記，這一天會長這樣；不會寫入（會去抓 Nasdaq、Cboe、證交所各一次）。</span>
        </div>
        {pvErr && <div className="text-xs text-[#ff1744]">{pvErr}</div>}
        {preview && !pvRow && <FeatureTable features={preview.features} calendar={preview.calendar} />}
        {preview && pvRow && (
          <div className="text-[11px] text-[#7070a0]">
            {pvRow.voided ? `預覽的這一天已經作廢：${pvRow.void_reason ?? ""}。` : "預覽的這一天就是上面的最新紀錄，不重複顯示。"}
          </div>
        )}
      </div>
      )}

      {rows && rows.length > 0 && (
        <div className="space-y-1.5">
          <div className="flex items-center gap-3 flex-wrap">
            <div className="text-[10px] text-[#7070a0] uppercase tracking-widest">最近的紀錄</div>
            <a href={`${getBase()}/premarket/snapshots.csv`} className="text-[11px] text-[#3b82f6] hover:underline">下載 CSV（全部）</a>
          </div>
          <RecentTable rows={rows} />
        </div>
      )}

      <div className="text-[10px] text-[#404060] leading-snug">
        歷史預檢（2017-05 起、樣本外 1,682 天，update.md 發現 31）：VIX 對日盤波動有穩定的增量，費半沒有，隔夜資訊對方向沒有。預檢只決定要不要繼續記錄，不決定上線；
        前向紀錄累積到 250 個交易日以上再用同一套標準獨立確認。
      </div>
    </div>
  );
}
