"use client";

import { useState, useEffect, useCallback } from "react";
import { FlaskConical } from "lucide-react";
import { api, Replay, ReplayCell, ReplayDay, ReplayGroup, ReplayValidation } from "@/lib/api";

const INPUT =
  "w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-2 py-1 text-xs font-mono text-[#e0e0f0] " +
  "focus:outline-none focus:border-[#3b82f6]";
const GREEN = "#00e676", RED = "#ff1744", YELLOW = "#ffc107", MUTED = "#7070a0";

const LEVEL: Record<ReplayValidation["verdict"]["level"], { label: string; color: string }> = {
  insufficient: { label: "樣本不足", color: YELLOW },
  none: { label: "沒有顯著差異——目前只是好看，不是有效訊號", color: "#ff9800" },
  significant: { label: "有顯著差異（還要用新資料再驗證）", color: GREEN },
  wrong_way: { label: "顯著，但方向相反", color: RED },
};

const pct = (x: number | null | undefined, d = 0) => (x == null ? "—" : `${(x * 100).toFixed(d)}%`);
const pts = (x: number | null | undefined) => (x == null ? "—" : `${x >= 0 ? "+" : ""}${x.toFixed(1)}`);

function Field({ label, value, onChange, step }: { label: string; value: string; onChange: (v: string) => void; step?: string }) {
  return (
    <label className="block">
      <span className="text-[10px] text-[#7070a0] block mb-0.5">{label}</span>
      <input type="number" step={step ?? "any"} value={value} onChange={(e) => onChange(e.target.value)} className={INPUT} />
    </label>
  );
}

// 一格：背景＝當天盤前判斷的顏色（Hurst 色相、IV 飽和度）；形狀＝該格結束時的買賣力道；外框＝協調（綠實線）／矛盾（紅虛線）
function Cell({ c, day }: { c: ReplayCell; day: ReplayDay }) {
  const ring = c.coherence === 1 ? `2px solid ${GREEN}` : c.coherence === -1 ? `2px dashed ${RED}` : "2px solid transparent";
  const glyph = c.shape === "up" ? "▲" : c.shape === "down" ? "▼" : "·";
  const verdict = c.coherence == null ? "（沒有預期方向或資料不足）" : c.coherence === 1 ? "協調" : c.coherence === -1 ? "矛盾" : "力道中性";
  return (
    <div
      title={`${day.date} ${c.start}　外盤 ${pct(c.share)}　${verdict}　之後 ${pts(c.fwd)} 點`}
      className="w-[18px] h-[18px] rounded-sm flex items-center justify-center text-[10px] leading-none text-white/90"
      style={{ background: day.color.css, border: ring, opacity: c.share == null ? 0.3 : 1 }}
    >{glyph}</div>
  );
}

function Group({ name, g, color }: { name: string; g: ReplayGroup; color: string }) {
  return (
    <tr className="border-t border-[#1e1e3a]">
      <td className="px-2 py-1" style={{ color }}>{name}</td>
      <td className="px-2 py-1">{g.n}</td>
      <td className="px-2 py-1">{pct(g.hit_rate)}</td>
      <td className="px-2 py-1" style={{ color: (g.mean_move ?? 0) >= 0 ? GREEN : RED }}>{pts(g.mean_move)}</td>
    </tr>
  );
}

// CUSUM：協調格的累計（往預期方向走了幾點），對照「同樣數量的隨機格子」的 5%~95% 區間
function CusumChart({ cusum }: { cusum: NonNullable<ReplayValidation["cusum"]> }) {
  const obs = cusum.observed.map((x) => x ?? 0), lo = cusum.lo.map((x) => x ?? 0), hi = cusum.hi.map((x) => x ?? 0);
  const n = obs.length;
  if (n < 2) return null;
  const all = [...obs, ...lo, ...hi, 0];
  const mn = Math.min(...all), mx = Math.max(...all), W = 300, H = 70;
  const X = (i: number) => (i / (n - 1)) * W;
  const Y = (v: number) => H - ((v - mn) / Math.max(mx - mn, 1e-9)) * H;
  const band = [...hi.map((v, i) => `${X(i)},${Y(v)}`), ...lo.map((v, i) => `${X(n - 1 - i)},${Y(lo[n - 1 - i])}`)].join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-[70px]" preserveAspectRatio="none">
      <line x1="0" x2={W} y1={Y(0)} y2={Y(0)} stroke="#404060" strokeDasharray="3 3" />
      <polygon points={band} fill="#7070a0" opacity="0.25" />
      <polyline points={obs.map((v, i) => `${X(i)},${Y(v)}`).join(" ")} fill="none" stroke="#3b82f6" strokeWidth="1.5" />
    </svg>
  );
}

export default function IndicatorReplayPanel() {
  const [data, setData] = useState<Replay | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [days, setDays] = useState("40");
  const [blockMin, setBlockMin] = useState("15");
  const [band, setBand] = useState("0.05");
  const [full, setFull] = useState("0.10");

  const run = useCallback(async () => {
    setBusy(true);
    setErr(null);
    try {
      setData(await api.market.replay({ days: Number(days), block_min: Number(blockMin), neutral_band: Number(band), full_at: Number(full) }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [days, blockMin, band, full]);

  useEffect(() => { run(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  const v = data?.validation;
  const lv = v ? LEVEL[v.verdict.level] : null;
  const rows = data ? [...data.days].reverse() : [];

  return (
    <div className="lg:col-span-2 bg-[#141420] rounded-xl border border-[#1e1e3a] p-5 space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        <FlaskConical size={14} className="text-[#ffc107]" />
        <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest">市場指標回放（歷史）</h2>
        <span className="text-[11px] text-[#404060]">驗證中——通過統計驗證前，不要當交易訊號，也不做即時版</span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 items-end">
        <Field label="最近幾個交易日" value={days} onChange={setDays} step="1" />
        <Field label="每格幾分鐘" value={blockMin} onChange={setBlockMin} step="1" />
        <Field label="中性帶（|H−0.5| 內是灰色）" value={band} onChange={setBand} step="0.01" />
        <Field label="全色點（|H−0.5| 到此顏色全開）" value={full} onChange={setFull} step="0.01" />
        <button onClick={run} disabled={busy}
          className="px-3 py-1.5 text-xs rounded border border-[#3b82f6]/40 text-[#3b82f6] bg-[#3b82f6]/10 hover:bg-[#3b82f6]/20 transition-colors disabled:opacity-40">
          {busy ? "計算中…" : "重新計算"}
        </button>
      </div>
      {err && <p className="text-[11px] text-[#ff1744]">{err}</p>}

      {v && lv && (
        <div className="rounded-lg border p-3 space-y-2" style={{ borderColor: `${lv.color}60`, background: `${lv.color}10` }}>
          <div className="text-xs font-semibold" style={{ color: lv.color }}>統計驗證：{lv.label}</div>
          <p className="text-[11px] text-[#e0e0f0] leading-relaxed">{v.verdict.text}</p>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div className="overflow-x-auto">
              <table className="w-full text-[11px] font-mono">
                <thead>
                  <tr className="text-[#7070a0] text-left">
                    <th className="font-normal px-2 py-1">力道 vs 預期方向</th>
                    <th className="font-normal px-2 py-1">格數</th>
                    <th className="font-normal px-2 py-1">之後往預期方向走的比例</th>
                    <th className="font-normal px-2 py-1">平均（點）</th>
                  </tr>
                </thead>
                <tbody>
                  <Group name="協調" g={v.coherent} color={GREEN} />
                  <Group name="矛盾" g={v.contradictory} color={RED} />
                  <Group name="力道中性" g={v.neutral} color={MUTED} />
                </tbody>
              </table>
              <p className="text-[10px] text-[#7070a0] font-mono mt-1 leading-relaxed">
                協調－矛盾 ＝ {pts(v.diff)} 點，p ＝ {v.p_value == null ? "—" : v.p_value.toFixed(3)}（同一天內洗牌 {v.n_perm} 次）
                {v.day_level && ` · 日級：預期方向猜中當天日盤漲跌 ${pct(v.day_level.hit_rate)}（${v.day_level.days} 天，p ＝ ${v.day_level.p_value?.toFixed(2) ?? "—"}）`}
              </p>
            </div>
            <div>
              {v.cusum ? (
                <>
                  <CusumChart cusum={v.cusum} />
                  <p className="text-[10px] text-[#7070a0] font-mono">
                    CUSUM：藍線＝協調格累計往預期方向走了幾點；灰帶＝同樣格數的隨機格子的 5%~95% 區間
                    {v.cusum.end_outside ? "（終點在灰帶之外）" : "（終點在灰帶之內＝跟隨機沒有差別）"}
                  </p>
                </>
              ) : <p className="text-[11px] text-[#7070a0]">協調的格子太少，畫不出 CUSUM</p>}
            </div>
          </div>
        </div>
      )}

      {data && (
        <div className="overflow-x-auto">
          <div className="min-w-max space-y-[2px]">
            <div className="flex items-center gap-2 text-[9px] text-[#404060] font-mono">
              <div className="w-[190px] shrink-0">日期　盤前 H　IV 百分位　預期方向</div>
              <div className="flex gap-[2px]">
                {data.blocks.map((b) => (
                  <div key={b.i} className="w-[18px] text-center">{b.start.endsWith(":00") ? b.start.slice(0, 2) : ""}</div>
                ))}
              </div>
            </div>
            {rows.map((d) => (
              <div key={d.date} className="flex items-center gap-2">
                <div className="w-[190px] shrink-0 text-[10px] font-mono flex gap-2 whitespace-nowrap">
                  <span className="text-[#e0e0f0]">{d.date.slice(5)}</span>
                  <span className="text-[#7070a0]">{d.hurst == null ? "H —" : `H ${d.hurst.toFixed(2)}`}</span>
                  <span className="text-[#7070a0]">{d.iv_pct == null ? "IV —" : `IV ${d.iv_pct.toFixed(0)}%`}</span>
                  <span style={{ color: d.want > 0 ? GREEN : d.want < 0 ? RED : MUTED }}>{d.want > 0 ? "偏多" : d.want < 0 ? "偏空" : "不偏"}</span>
                </div>
                <div className="flex gap-[2px]">
                  {d.cells.map((c) => <Cell key={c.i} c={c} day={d} />)}
                </div>
              </div>
            ))}
            {rows.length === 0 && <p className="text-[11px] text-[#7070a0] py-4">沒有日盤資料完整的交易日（外/內盤歷史還沒回補？）</p>}
          </div>
        </div>
      )}

      <div className="text-[10px] text-[#404060] leading-relaxed space-y-1">
        <p>
          <span style={{ color: "hsl(20, 80%, 50%)" }}>■</span> 暖色＝趨勢延續　<span style={{ color: "hsl(200, 80%, 50%)" }}>■</span> 冷色＝均值回歸
          <span style={{ color: "hsl(0, 0%, 46%)" }}>■</span> 灰＝中性　顏色越鮮豔＝IV 百分位越高（沒有 IV 歷史的日子一律淡色）
          ▲ 買方主動　▼ 賣方主動　· 中性　<span style={{ color: GREEN }}>▢</span> 協調　<span style={{ color: RED }}>▢</span>（虛線）矛盾
        </p>
        <p>
          顏色用「前一個交易日收盤為止」的日 K 算（盤前就知道的），不看當天；買賣力道重現〈盤中即時〉面板（最近 100 筆成交的外盤占比、60%/40% 門檻＋遲滯），
          取各格結束那一刻；協調＝力道方向和盤前預期方向（Hurst 狀態 × 日 K 方向，與 scalp 偏向 = 自動同一套）一致。
          Hurst 本身沒有方向，所以暖色不代表看多。
        </p>
      </div>
    </div>
  );
}
