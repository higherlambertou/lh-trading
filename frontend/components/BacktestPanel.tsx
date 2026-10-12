"use client";

import { useCallback, useEffect, useState } from "react";
import { History } from "lucide-react";
import { api, apiErrorDetail } from "@/lib/api";
import {
  BT_STRATEGIES, BacktestInfo, BacktestJob, BacktestReport, BtForm, DEFAULT_FORM,
  buildRequest, fmtPct, fmtPts, fmtT, topRows, verdict,
} from "@/lib/backtest";

const INPUT =
  "w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-2 py-1 text-xs font-mono text-[#e0e0f0] " +
  "focus:outline-none focus:border-[#3b82f6] disabled:opacity-40";
const GREEN = "#00e676", RED = "#ff1744", ORANGE = "#ff9800", MUTED = "#7070a0";
const LEVEL_COLOR = { green: GREEN, gray: ORANGE, red: RED } as const;
const posNeg = (x: number) => (x >= 0 ? GREEN : RED);

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-[10px] text-[#7070a0] block mb-0.5">{label}</span>
      {children}
    </label>
  );
}

function ReportCard({ r, top }: { r: BacktestReport; top: number }) {
  const [all, setAll] = useState(false);
  const v = verdict(r);
  const color = LEVEL_COLOR[v.level];
  const rows = all ? topRows(r, r.rows.length) : topRows(r, top);
  const wf = r.walk_forward;
  return (
    <div className="rounded-lg border p-3 space-y-2" style={{ borderColor: `${color}60`, background: `${color}10` }}>
      <div className="flex items-baseline gap-x-3 gap-y-1 flex-wrap">
        <span className="font-mono text-sm text-[#e0e0f0]">{r.strategy}</span>
        <span className="text-[10px] text-[#7070a0] font-mono">
          {r.combos} 組參數 · {r.days} 天（{r.first} → {r.last}）· 每筆成本 {r.cost} 點 · 樣本外 {r.split.oos_days} 天（{r.split.oos_from} 起）
        </span>
      </div>
      <p className="text-[11px] leading-relaxed" style={{ color }}>{v.text}</p>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px] font-mono">
        <div><div className="text-[10px] text-[#7070a0]">扣成本前每筆（中位）</div><span style={{ color: posNeg(r.median_gross_pertrade) }}>{fmtPts(r.median_gross_pertrade)} 點</span></div>
        <div><div className="text-[10px] text-[#7070a0]">扣成本後每筆（中位）</div><span style={{ color: posNeg(r.median_net_pertrade) }}>{fmtPts(r.median_net_pertrade)} 點</span></div>
        <div><div className="text-[10px] text-[#7070a0]">全期淨賺的組數</div><span className="text-[#e0e0f0]">{r.positive_all}／{r.combos}</span></div>
        <div><div className="text-[10px] text-[#7070a0]">樣本內為正 → 樣本外仍為正</div><span className="text-[#e0e0f0]">{r.is_positive} → {r.is_positive_oos_positive}</span></div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-[11px] font-mono whitespace-nowrap">
          <thead>
            <tr className="text-[#7070a0] text-left">
              <th className="font-normal px-2 py-1">{all ? `全部 ${r.rows.length} 組（依樣本內 t 排序）` : `樣本內最好的 ${rows.length} 組`}</th>
              <th className="font-normal px-2 py-1">筆/天</th>
              <th className="font-normal px-2 py-1">勝率</th>
              <th className="font-normal px-2 py-1">樣本內 點/天</th>
              <th className="font-normal px-2 py-1">t</th>
              <th className="font-normal px-2 py-1">樣本外 點/天</th>
              <th className="font-normal px-2 py-1">t</th>
              <th className="font-normal px-2 py-1">全期每筆</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((x) => (
              <tr key={x.label} className="border-t border-[#1e1e3a]">
                <td className="px-2 py-1 text-[#e0e0f0]">{x.label.replace(`${r.strategy} `, "")}</td>
                <td className="px-2 py-1">{(x.all.trades / r.days).toFixed(1)}</td>
                <td className="px-2 py-1">{fmtPct(x.all.winrate)}</td>
                <td className="px-2 py-1" style={{ color: posNeg(x.is.perday) }}>{fmtPts(x.is.perday)}</td>
                <td className="px-2 py-1 text-[#7070a0]">{fmtT(x.is.t)}</td>
                <td className="px-2 py-1" style={{ color: posNeg(x.oos.perday) }}>{fmtPts(x.oos.perday)}</td>
                <td className="px-2 py-1 text-[#7070a0]">{fmtT(x.oos.t)}</td>
                <td className="px-2 py-1" style={{ color: posNeg(x.all.pertrade) }}>{fmtPts(x.all.pertrade)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {r.rows.length > top && (
          <button onClick={() => setAll(!all)} className="mt-1 text-[10px] text-[#3b82f6] hover:underline">
            {all ? "只看前幾名" : `顯示全部 ${r.rows.length} 組`}
          </button>
        )}
      </div>

      {wf && (
        <p className="text-[11px] text-[#e0e0f0] font-mono leading-relaxed">
          滾動式驗證（每次用前 {wf.train} 天挑最好的、跑接下來 {wf.test} 天）：樣本外 {wf.days} 天，
          <span style={{ color: posNeg(wf.perday) }}> {fmtPts(wf.perday)} 點/天</span>（t={fmtT(wf.t)}），{(wf.trades / Math.max(wf.days, 1)).toFixed(1)} 筆/天，
          {wf.positive_windows}／{wf.windows} 個視窗賺錢，挑過 {wf.distinct_picks} 種不同的組合
        </p>
      )}
      {r.null && r.null.length > 0 && (
        <p className="text-[11px] text-[#7070a0] font-mono leading-relaxed">
          沒有技巧的對照（{r.null.length} 組打亂資料、同樣流程）：樣本內最佳 t 介於 {fmtT(Math.min(...r.null.map((x) => x.best_is_t)))} ~ {fmtT(Math.max(...r.null.map((x) => x.best_is_t)))}
          （真實資料 {fmtT(r.best_is_t)}）
          {wf && r.null.every((x) => typeof x.wf_t === "number") &&
            `；滾動式驗證樣本外 t 介於 ${fmtT(Math.min(...r.null.map((x) => x.wf_t as number)))} ~ ${fmtT(Math.max(...r.null.map((x) => x.wf_t as number)))}（真實資料 ${fmtT(wf.t)}）`}
        </p>
      )}
      <p className="text-[10px] text-[#404060] leading-relaxed">
        偵測力：以 {r.days} 天的資料，真實的每日平均邊際要大於約 +{r.mde_perday.toFixed(0)} 點/天（1 點＝10 元）才看得出來；比這小的優勢（含比成本小的）這份資料分辨不出來。
      </p>
    </div>
  );
}

export default function BacktestPanel() {
  const [form, setForm] = useState<BtForm>(DEFAULT_FORM);
  const [info, setInfo] = useState<BacktestInfo | null>(null);
  const [job, setJob] = useState<BacktestJob | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const set = <K extends keyof BtForm>(k: K, v: BtForm[K]) => setForm((f) => ({ ...f, [k]: v }));

  const loadFirst = useCallback(async () => {
    try { setInfo(await api.backtest.info()); } catch { /* 靜默：後端剛重啟或回測 API 關閉 */ }
    try {
      const list = await api.backtest.jobs();
      if (list.length) setJob(await api.backtest.job(list[0].id));
    } catch { /* 靜默 */ }
  }, []);
  useEffect(() => { loadFirst(); }, [loadFirst]);

  const jobId = job?.id, running = job?.status === "running";
  useEffect(() => {
    if (!jobId || !running) return;
    const id = setInterval(async () => {
      try { setJob(await api.backtest.job(jobId)); }
      catch (e) { setErr(apiErrorDetail(e)); }
    }, 2000);
    return () => clearInterval(id);
  }, [jobId, running]);

  const run = async () => {
    setErr("");
    const b = buildRequest(form);
    if (!b.ok) { setErr(b.error); return; }
    setBusy(true);
    try {
      const { id } = await api.backtest.run(b.req);
      setJob(await api.backtest.job(id));
    } catch (e) {
      setErr(apiErrorDetail(e));
    } finally {
      setBusy(false);
    }
  };
  const cancel = async () => {
    if (!job) return;
    try { await api.backtest.cancel(job.id); setJob(await api.backtest.job(job.id)); }
    catch (e) { setErr(apiErrorDetail(e)); }
  };

  const flow = form.source === "flow";
  const heavy = form.strategy === "all" || Number(form.nullN) >= 6;
  return (
    <div className="lg:col-span-2 bg-[#141420] rounded-xl border border-[#1e1e3a] p-5 space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        <History size={14} className="text-[#3b82f6]" />
        <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest">策略回測</h2>
        <span className="text-[11px] text-[#404060]">
          用真實的策略程式碼在本機 1 分 K 上跑參數網格；跑在背景子行程（低優先權），不連券商、不影響交易
        </span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 items-end">
        <Field label="策略">
          <select value={form.strategy} onChange={(e) => set("strategy", e.target.value)} className={INPUT}>
            {BT_STRATEGIES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
          </select>
        </Field>
        <Field label="資料">
          <select value={form.source} onChange={(e) => { const s = e.target.value as BtForm["source"]; set("source", s); if (s === "bars") set("session", "day"); }} className={INPUT}>
            <option value="bars">日盤 K 棒{info ? `（${info.bars.days} 天）` : ""}</option>
            <option value="flow">TMF 成交彙總，含夜盤{info ? `（${info.flow.minutes.toLocaleString()} 分鐘）` : ""}</option>
          </select>
        </Field>
        <Field label="時段（成交彙總才能選）">
          <select value={form.session} disabled={!flow} onChange={(e) => set("session", e.target.value as BtForm["session"])} className={INPUT}>
            <option value="day">日盤</option><option value="night">夜盤</option><option value="all">日盤＋夜盤</option>
          </select>
        </Field>
        <Field label="每筆成本（點，手續費＋稅＋滑價）">
          <input type="number" step="any" value={form.cost} onChange={(e) => set("cost", e.target.value)} className={INPUT} />
        </Field>
        <Field label="樣本內比例">
          <input type="number" step="0.05" value={form.split} onChange={(e) => set("split", e.target.value)} className={INPUT} />
        </Field>
        <Field label="打亂資料對照（組數，0＝不做）">
          <input type="number" step="1" value={form.nullN} onChange={(e) => set("nullN", e.target.value)} className={INPUT} />
        </Field>
        <Field label="顯示前幾名">
          <input type="number" step="1" value={form.top} onChange={(e) => set("top", e.target.value)} className={INPUT} />
        </Field>
        <div className="flex items-end gap-2">
          <label className="flex items-center gap-1.5 text-[11px] text-[#e0e0f0] pb-1.5 whitespace-nowrap">
            <input type="checkbox" checked={form.wf} onChange={(e) => set("wf", e.target.checked)} />滾動式驗證
          </label>
          <input type="number" value={form.wfTrain} disabled={!form.wf} title="訓練天數" onChange={(e) => set("wfTrain", e.target.value)} className={INPUT} />
          <input type="number" value={form.wfTest} disabled={!form.wf} title="測試天數" onChange={(e) => set("wfTest", e.target.value)} className={INPUT} />
        </div>
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <button onClick={run} disabled={busy || running}
          className="px-3 py-1.5 text-xs rounded border border-[#3b82f6]/40 text-[#3b82f6] bg-[#3b82f6]/10 hover:bg-[#3b82f6]/20 transition-colors disabled:opacity-40">
          {running ? "回測進行中…" : "執行回測"}
        </button>
        {running && (
          <button onClick={cancel} className="px-3 py-1.5 text-xs rounded border border-[#ff1744]/40 text-[#ff1744] bg-[#ff1744]/10 hover:bg-[#ff1744]/20 transition-colors">
            取消
          </button>
        )}
        <span className="text-[10px] text-[#404060]">
          {heavy ? "選了全部策略或多組對照，會比較久（每次最多開 " : "單一策略通常幾秒～半分鐘（每次最多開 "}{info?.workers ?? 2} 個平行行程，同時只跑一個回測）
        </span>
      </div>
      <p className="text-[10px] text-[#404060] leading-relaxed">
        沒有 scalp：它是逐筆的限價單狀態機（訊號看最近 20 筆成交、停利是掛在券商的限價單），1 分 K 沒有這些資訊，回測出來的數字不可信。
        scalp 請看〈風險〉面板（用成交紀錄的真實損益）；細節見下方「怎麼讀」。
      </p>
      {err && <p className="text-[11px] text-[#ff1744] whitespace-pre-wrap">{err}</p>}

      {job && (
        <div className="space-y-3">
          {job.status === "running" && (
            <div className="space-y-1">
              <div className="text-[11px] text-[#e0e0f0] font-mono">
                已跑 {Math.round(job.elapsed_sec)} 秒 · 完成 {job.progress.done}／{job.progress.total} 個策略
              </div>
              <div className="h-1.5 rounded bg-[#0d0d14] overflow-hidden">
                <div className="h-full bg-[#3b82f6]/70 transition-all" style={{ width: `${Math.max(4, (job.progress.done / Math.max(job.progress.total, 1)) * 100)}%` }} />
              </div>
            </div>
          )}
          {job.status === "failed" && <p className="text-[11px] text-[#ff1744] whitespace-pre-wrap">回測失敗：{job.error}</p>}
          {job.status === "cancelled" && <p className="text-[11px] text-[#7070a0]">已取消</p>}
          {job.status === "done" && job.reports && job.reports.map((r) => <ReportCard key={r.strategy} r={r} top={job.request.top} />)}
          {job.status === "done" && (
            <p className="text-[10px] text-[#404060]">共跑 {Math.round(job.elapsed_sec)} 秒。後端重啟後結果會消失；重跑一次即可。</p>
          )}
        </div>
      )}

      <details className="text-[11px] text-[#7070a0] leading-relaxed">
        <summary className="cursor-pointer text-[#3b82f6]">怎麼讀這些數字（容易誤判的地方）</summary>
        <ul className="list-disc pl-5 mt-1 space-y-1">
          <li><b>樣本內最好的組合，成績本來就會被高估</b>（從很多組裡挑最好的）。要看樣本外，以及「打亂資料」——把 K 棒順序打亂、沒有任何規律的資料，用同樣流程也會挑出看起來不錯的組合。</li>
          <li><b>滾動式驗證</b>是誠實的「先最佳化再交易」：每次只用過去的天數挑參數，再跑接下來幾天；結論（上方顏色）只看這一項。</li>
          <li><b>t 值</b>以「天」為單位，|t| 要大於 2 才算有一點證據；挑了很多組時要更高。</li>
          <li>逐 tick 的策略（ma_cross、breakout、rsi、bollinger、momentum）在這裡是<b>分鐘版</b>（每根 K 棒收盤當一個 tick，參數單位變成分鐘），不是正式環境的 tick 版。</li>
          <li>盤中停損停利用 K 棒高低判斷，兩個價位同一根都碰到算停損先到；進場在訊號 K 棒收盤；收盤前強制平倉。成本是假設，請改成自己的。</li>
          <li><b>為什麼沒有 scalp</b>：① 它的訊號是最近 20 筆成交的買賣方向，1 分 K 裡沒有；② 進場是限價單（要看後面的成交才知道有沒有成交、15 個 tick 沒成交就取消），停利是掛在券商的限價單，
            1 分 K 分不出一根 K 棒裡先碰到停利還是停損（本工具一律算停損先到，對停利 20 點這種短線會嚴重偏悲觀）；③ 要回測得用逐筆資料，而本機的逐筆資料只有幾天，樣本太少什麼都說不了。
            想知道 scalp 的實際表現：累積成交紀錄後用〈風險〉面板的「真實損益分布」。</li>
          <li>日盤 K 棒資料指數從 2.5 萬漲到 4.9 萬、波動變化很大，固定點數的參數在不同時期意義不同；這份資料只能看出夠大的優勢（見每張卡片最後一行）。</li>
        </ul>
      </details>
    </div>
  );
}
