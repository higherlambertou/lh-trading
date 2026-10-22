"use client";

import { useCallback, useState } from "react";
import { api, apiErrorDetail } from "@/lib/api";
import { buildCompareRequest, chartGeometry, pnlColor, pts, spts, stwd, twd, type CmpStructure, type OptCompare, type OptForm, type OptSource } from "@/lib/options";

// 跨式 vs 勒式：在各個指數點位（遠期價）時，到期與持有 N 天後各賺賠多少。只試算、不下單；沿用上面表單的到期月、勒式設定、預期波動、組數。
const STRADDLE = "#3b82f6";
const STRANGLE = "#f59e0b";
const W = 720, H = 260;

function StructCard({ name, color, s }: { name: string; color: string; s: CmpStructure }) {
  return (
    <div className="rounded-lg bg-[#0d0d14] px-3 py-2 space-y-0.5">
      <div className="text-xs" style={{ color }}>{name}</div>
      <div className="text-[11px] font-mono text-[#e0e0f0]">
        {s.legs.map((l) => `${l.right === "C" ? "Call" : "Put"} ${pts(l.strike)} @ ${pts(l.entry, 1)}`).join("　＋　")}
      </div>
      <div className="text-[11px] font-mono text-[#7070a0]">
        成本 {pts(s.cost_pts, 1)} 點 ＝ <span className="text-[#e0e0f0]">{twd(s.cost_twd)}</span>（最多賠這麼多）
      </div>
      <div className="text-[11px] font-mono text-[#7070a0]">
        到期損益兩平：{s.breakevens.map((b, i) => `${pts(b)}（${spts(s.breakeven_moves[i])}）`).join("、") || "—"}
      </div>
    </div>
  );
}

export function CompareChart({ r }: { r: OptCompare }) {
  const g = chartGeometry(r.curve, W, H);
  const yTicks = [g.y0, 0, g.y1];
  const bes = [...r.structures.straddle.breakevens.map((b) => ({ b, c: STRADDLE })), ...r.structures.strangle.breakevens.map((b) => ({ b, c: STRANGLE }))]
    .filter((x) => x.b >= g.x0 && x.b <= g.x1);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto" role="img" aria-label="跨式與勒式在各點位的損益曲線">
      <line x1={g.X(g.x0)} x2={g.X(g.x1)} y1={g.Y(0)} y2={g.Y(0)} stroke="#404060" strokeWidth={1} />
      <line x1={g.X(r.F)} x2={g.X(r.F)} y1={8} y2={H - 22} stroke="#7070a0" strokeDasharray="2 3" strokeWidth={1} />
      <text x={g.X(r.F) + 3} y={18} fontSize={10} fill="#7070a0">現價 {pts(r.F)}</text>
      {bes.map((x, i) => <circle key={i} cx={g.X(x.b)} cy={g.Y(0)} r={3} fill={x.c} />)}
      <path d={g.path((c) => c.straddle.expiry)} fill="none" stroke={STRADDLE} strokeWidth={2} />
      <path d={g.path((c) => c.strangle.expiry)} fill="none" stroke={STRANGLE} strokeWidth={2} />
      {!r.hold_is_expiry && <path d={g.path((c) => c.straddle.hold)} fill="none" stroke={STRADDLE} strokeWidth={1.5} strokeDasharray="5 4" />}
      {!r.hold_is_expiry && <path d={g.path((c) => c.strangle.hold)} fill="none" stroke={STRANGLE} strokeWidth={1.5} strokeDasharray="5 4" />}
      {yTicks.map((y, i) => <text key={i} x={2} y={g.Y(y) + 3} fontSize={9} fill="#7070a0">{stwd(y)}</text>)}
      {[g.x0, (g.x0 + g.x1) / 2, g.x1].map((x, i) => <text key={i} x={g.X(x)} y={H - 6} fontSize={9} fill="#7070a0" textAnchor={i === 0 ? "start" : i === 2 ? "end" : "middle"}>{pts(x)}</text>)}
    </svg>
  );
}

export function CompareTable({ r }: { r: OptCompare }) {
  const near = r.rows.reduce((best, x) => (Math.abs(x.S - r.F) < Math.abs(best.S - r.F) ? x : best), r.rows[0]);
  const holdLabel = r.hold_is_expiry ? "（＝到期）" : `${pts(r.hold_days, r.hold_days % 1 ? 1 : 0)} 天後`;
  const cell = (v: number) => <td className="px-1 sm:px-1.5 py-1 text-right font-mono text-[11px] sm:text-xs" style={{ color: pnlColor(v) }}>{stwd(v)}</td>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="text-[10px] text-[#7070a0]">
            <th className="px-1.5 py-1 text-left font-normal">指數點位</th>
            <th className="px-1.5 py-1 text-right font-normal hidden sm:table-cell">距現價</th>
            <th className="px-1.5 py-1 text-right font-normal" style={{ color: STRADDLE }}>跨式・到期</th>
            <th className="px-1.5 py-1 text-right font-normal" style={{ color: STRADDLE }}>跨式・{holdLabel}</th>
            <th className="px-1.5 py-1 text-right font-normal" style={{ color: STRANGLE }}>勒式・到期</th>
            <th className="px-1.5 py-1 text-right font-normal" style={{ color: STRANGLE }}>勒式・{holdLabel}</th>
          </tr>
        </thead>
        <tbody>
          {r.rows.map((x) => (
            <tr key={x.S} className={`border-t border-[#1e1e3a] ${x === near ? "bg-[#1e1e3a]/60" : ""}`}>
              <td className="px-1.5 py-1 font-mono text-xs text-[#e0e0f0]">{pts(x.S)}{x === near ? <span className="text-[#7070a0]"> ←現價</span> : ""}</td>
              <td className="px-1.5 py-1 text-right font-mono text-xs text-[#7070a0] hidden sm:table-cell">{spts(x.chg)}</td>
              {cell(x.straddle.expiry)}{cell(x.straddle.hold)}{cell(x.strangle.expiry)}{cell(x.strangle.hold)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function CompareView({ r }: { r: OptCompare }) {
  return (
    <div className="space-y-3">
      <div className="text-[11px] text-[#7070a0] font-mono">
        現價（遠期）{pts(r.F)} · 距到期 {pts(r.days, 1)} 天 · ATM IV {r.atm_iv == null ? "—" : `${(r.atm_iv * 100).toFixed(1)}%`} · 預期波動 1σ ≈ {pts(r.em_pts)} 點 · {r.lots} 組 · 每列間隔 {pts(r.step)} 點
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        <StructCard name="買跨式（同一履約價的 Call＋Put）" color={STRADDLE} s={r.structures.straddle} />
        <StructCard name="買勒式（價外 Call＋價外 Put）" color={STRANGLE} s={r.structures.strangle} />
      </div>
      <CompareChart r={r} />
      <div className="text-[10px] text-[#7070a0] flex flex-wrap gap-x-4">
        <span><span style={{ color: STRADDLE }}>━</span> 跨式</span><span><span style={{ color: STRANGLE }}>━</span> 勒式</span>
        <span>實線＝到期時；{r.hold_is_expiry ? "持有天數已到期，只有實線" : `虛線＝持有 ${pts(r.hold_days, r.hold_days % 1 ? 1 : 0)} 天後（IV 不變、用模型估）`}</span><span>圓點＝到期損益兩平</span>
      </div>
      <CompareTable r={r} />
      <div className="text-[10px] leading-snug text-[#404060]">
        金額是賺賠（元，已扣買進成本、未扣手續費與稅）。到期那欄是精確的；「N 天後」假設 IV 不變，實際 IV 漲跌會讓它差很多（下跌時 IV 通常變大）。
        點位是台指期／遠期價。只試算、不下單；這不是進場訊號——先前的檢驗沒有找到買跨式、勒式的穩定優勢（發現 30）。
      </div>
      {r.warnings.length > 0 && <ul className="space-y-1">{r.warnings.map((w, i) => <li key={i} className="text-[11px]" style={{ color: "#ffc107" }}>⚠ {w}</li>)}</ul>}
    </div>
  );
}

export default function StrategyCompare({ form, src }: { form: OptForm; src: OptSource }) {
  const [holdDays, setHoldDays] = useState("1");
  const [r, setR] = useState<OptCompare | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const run = useCallback(async () => {
    const b = buildCompareRequest(form, src, holdDays);
    if (!b.ok) { setErr(b.error); return; }
    setBusy(true); setErr("");
    try { setR(await api.options.compare(b.body)); } catch (e) { setErr(apiErrorDetail(e)); } finally { setBusy(false); }
  }, [form, src, holdDays]);
  return (
    <div className="rounded-lg border border-[#1e1e3a] p-3 space-y-3">
      <div className="flex items-center gap-3 flex-wrap">
        <h3 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest">跨式 vs 勒式：各點位賺賠多少</h3>
        <label className="text-[11px] text-[#7070a0] flex items-center gap-1">持有
          <input className="w-16 bg-[#0d0d14] border border-[#1e1e3a] rounded px-2 py-1 font-mono text-xs text-[#e0e0f0]" inputMode="decimal" value={holdDays} onChange={(e) => setHoldDays(e.target.value)} />
          天後（另一條虛線；到期看實線）</label>
        <button type="button" disabled={busy} onClick={run} className="px-3 py-1 text-xs rounded bg-[#3b82f6] text-white disabled:opacity-40">{busy ? "計算中…" : "比較"}</button>
        {err && <span className="text-[11px]" style={{ color: "#ff5252" }}>{err}</span>}
      </div>
      <div className="text-[10px] text-[#404060]">跨式用最靠近現價的履約價；勒式用上面「履約價怎麼選」的設定（Delta 或距離倍數）。即時報價一次查詢、5 秒內重複用快取。</div>
      {r && <CompareView r={r} />}
    </div>
  );
}
