"use client";

import { useState, useEffect, useCallback } from "react";
import { ShieldAlert } from "lucide-react";
import { api, RuinReport } from "@/lib/api";

const INPUT =
  "w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-2 py-1 text-xs font-mono text-[#e0e0f0] " +
  "focus:outline-none focus:border-[#3b82f6]";
const GREEN = "#00e676", YELLOW = "#ffc107", RED = "#ff1744", MUTED = "#7070a0";

// 破產機率 <5% 綠、<20% 黃、其餘紅
const probColor = (p: number) => (p < 0.05 ? GREEN : p < 0.2 ? YELLOW : RED);
const pct = (x: number | null | undefined, d = 1) => (x == null ? "—" : `${(x * 100).toFixed(d)}%`);
const money = (n: number) => `${n >= 0 ? "+" : ""}${Math.round(n).toLocaleString()}`;

function Field({ label, value, onChange, step }: { label: string; value: string; onChange: (v: string) => void; step?: string }) {
  return (
    <label className="block">
      <span className="text-[10px] text-[#7070a0] block mb-0.5">{label}</span>
      <input type="number" step={step ?? "any"} value={value} onChange={(e) => onChange(e.target.value)} className={INPUT} />
    </label>
  );
}

export default function RiskPanel() {
  const [report, setReport] = useState<RuinReport | null>(null);
  const [capital, setCapital] = useState("");
  const [tp, setTp] = useState("20");
  const [sl, setSl] = useState("60");
  const [qty, setQty] = useState("1");
  const [pv, setPv] = useState("10");
  const [win, setWin] = useState("65");
  const [cost, setCost] = useState("2");
  const [useHistory, setUseHistory] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = useCallback(async (override?: { useHistory?: boolean }) => {
    setBusy(true);
    setErr(null);
    try {
      setReport(await api.risk.ruin({
        ...(capital ? { capital: Number(capital) } : {}),
        win_rate: Number(win) / 100, tp_pts: Number(tp), sl_pts: Number(sl), qty: Number(qty),
        point_value: Number(pv), cost_pts: Number(cost), trades: 1000, paths: 4000,
        use_history: override?.useHistory ?? useHistory,
      }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [capital, win, tp, sl, qty, pv, cost, useHistory]);

  // 第一次載入：本金預填帳戶權益、停利停損預填 scalp 目前參數，再自動試算一次
  useEffect(() => {
    (async () => {
      let cap = "";
      try { cap = String((await api.position.margin()).equity); } catch { /* 保證金尚未就緒 */ }
      let t = "20", s = "60", q = "1";
      try {
        const sc = (await api.strategy.list()).find((x) => x.name === "scalp");
        if (sc) { t = String(sc.params.tp_pts); s = String(sc.params.sl_pts); q = String(sc.params.max_qty ?? 1); }
      } catch { /* 用預設 */ }
      setCapital(cap); setTp(t); setSl(s); setQty(q);
      try {
        setReport(await api.risk.ruin({
          ...(cap ? { capital: Number(cap) } : {}),
          win_rate: 0.65, tp_pts: Number(t), sl_pts: Number(s), qty: Number(q), point_value: 10,
          cost_pts: 2, trades: 1000, paths: 4000, use_history: false,
        }));
      } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    })();
  }, []);

  const pt = report?.per_trade;
  const hist = report?.history;
  const wrNow = Number(win) / 100;

  return (
    <div className="lg:col-span-2 bg-[#141420] rounded-xl border border-[#1e1e3a] p-5 space-y-4">
      <div className="flex items-center gap-2">
        <ShieldAlert size={14} className="text-[#ffc107]" />
        <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest">破產機率驗證</h2>
        <span className="text-[11px] text-[#404060]">期望值為正 ≠ 不會破產：本金撐不撐得住連續虧損</span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-7 gap-2 items-end">
        <Field label="本金（元，預設＝權益數）" value={capital} onChange={setCapital} />
        <Field label="停利（點）" value={tp} onChange={setTp} />
        <Field label="停損（點）" value={sl} onChange={setSl} />
        <Field label="口數" value={qty} onChange={setQty} step="1" />
        <label className="block">
          <span className="text-[10px] text-[#7070a0] block mb-0.5">合約（每點元）</span>
          <select value={pv} onChange={(e) => setPv(e.target.value)} className={INPUT}>
            <option value="10">TMF（10）</option>
            <option value="50">MXF（50）</option>
            <option value="200">TXF（200）</option>
          </select>
        </label>
        <Field label="勝率（%）" value={win} onChange={setWin} />
        <Field label="每筆成本（點）" value={cost} onChange={setCost} />
      </div>
      <div className="flex items-center gap-3 flex-wrap">
        <button
          onClick={() => run()} disabled={busy}
          className="px-3 py-1.5 text-xs rounded border border-[#3b82f6]/40 text-[#3b82f6] bg-[#3b82f6]/10 hover:bg-[#3b82f6]/20 transition-colors disabled:opacity-40"
        >{busy ? "計算中…" : "試算"}</button>
        {hist && hist.n >= 30 && (
          <label className="flex items-center gap-1.5 text-[11px] text-[#7070a0] cursor-pointer">
            <input type="checkbox" checked={useHistory}
              onChange={(e) => { setUseHistory(e.target.checked); run({ useHistory: e.target.checked }); }} />
            用成交紀錄的真實損益分布（{hist.n} 筆）
          </label>
        )}
        {err && <span className="text-[11px] text-[#ff1744]">{err}</span>}
        {report?.history_note && <span className="text-[11px] text-[#ffc107]">{report.history_note}</span>}
      </div>

      {report && pt && (
        <>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div className="bg-[#0d0d14] rounded-lg border border-[#1e1e3a] p-3 space-y-1">
              <div className="text-[10px] text-[#7070a0] uppercase tracking-widest">每筆損益</div>
              <div className="font-mono text-sm">
                <span className="text-[#00e676]">賺 {Math.round(pt.win).toLocaleString()}</span>
                <span className="text-[#404060]"> ／ </span>
                <span className="text-[#ff1744]">賠 {Math.round(pt.loss).toLocaleString()}</span> 元
              </div>
              <div className="text-[11px] text-[#7070a0] font-mono">賺賠比 1:{pt.payoff ? (1 / pt.payoff).toFixed(1) : "—"}（成本 {Math.round(pt.cost)} 元/筆）</div>
              <div className="text-[11px] font-mono" style={{ color: wrNow >= pt.breakeven_win_rate ? GREEN : RED }}>
                損益兩平勝率 {pct(pt.breakeven_win_rate)}
              </div>
              <div className="text-[11px] font-mono" style={{ color: pt.expectancy >= 0 ? GREEN : RED }}>
                勝率 {win}% 時每筆期望 {money(pt.expectancy)} 元
              </div>
            </div>

            <div className="bg-[#0d0d14] rounded-lg border border-[#1e1e3a] p-3 space-y-1.5 md:col-span-2">
              <div className="text-[10px] text-[#7070a0] uppercase tracking-widest">
                {report.inputs.n_trades} 筆內的破產機率（{report.inputs.source === "history" ? "真實損益分布" : `勝率 ${win}%`}）
              </div>
              <div className="grid grid-cols-3 gap-2">
                {(["0.5x", "1x", "2x"] as const).map((k) => {
                  const r = report.ruin[k];
                  return (
                    <div key={k} className="text-center">
                      <div className="text-[10px] text-[#7070a0] font-mono">本金 ×{k.slice(0, -1)}（{Math.round(r.capital).toLocaleString()}）</div>
                      <div className="font-mono text-2xl" style={{ color: probColor(r.ruin_prob) }}>{pct(r.ruin_prob)}</div>
                      <div className="text-[10px] text-[#404060] font-mono">無限期上界 {pct(r.lundberg, 0)}</div>
                    </div>
                  );
                })}
              </div>
              <div className="text-[11px] text-[#7070a0] font-mono">
                撐得住連續 {report.capacity.affordable_losses ?? "—"} 筆最大虧損；{report.inputs.n_trades} 筆內預期最長連虧約 {report.capacity.expected_longest_losing_streak} 筆
                {hist && ` · 歷史最長連虧 ${hist.longest_losing_streak} 筆（${money(hist.longest_losing_streak_loss)} 元）`}
              </div>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-mono">
              <thead>
                <tr className="text-[#7070a0] text-left">
                  <th className="font-normal px-2 py-1">勝率</th>
                  <th className="font-normal px-2 py-1">每筆期望（元）</th>
                  <th className="font-normal px-2 py-1">本金 ×0.5</th>
                  <th className="font-normal px-2 py-1">×1</th>
                  <th className="font-normal px-2 py-1">×2</th>
                </tr>
              </thead>
              <tbody>
                {report.grid.map((g) => (
                  <tr key={g.win_rate} className="border-t border-[#1e1e3a]">
                    <td className="px-2 py-1">{Math.round(g.win_rate * 100)}%</td>
                    <td className="px-2 py-1" style={{ color: g.expectancy >= 0 ? GREEN : RED }}>{money(g.expectancy)}</td>
                    {(["0.5x", "1x", "2x"] as const).map((k) => (
                      <td key={k} className="px-2 py-1" style={{ color: probColor(g[k]) }}>{pct(g[k])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {hist && (
            <p className="text-[11px] text-[#7070a0] font-mono">
              成交紀錄（{hist.n} 筆，不含手續費與稅）：勝率 {pct(hist.win_rate)} · 平均賺 {Math.round(hist.avg_win)} / 平均賠 {Math.round(hist.avg_loss)} 元
              · 每筆期望 {money(hist.expectancy)} 元 · 累計 {money(hist.total)} 元
            </p>
          )}
        </>
      )}

      <p className="text-[10px] text-[#404060] leading-relaxed">
        理論估算：假設每筆獨立、損益固定，不含跳空與滑價；結果對「勝率」與「賺賠比」的估計非常敏感。
        成交紀錄不足 30 筆時，勝率是你輸入的假設值，不是實測。
      </p>
    </div>
  );
}
