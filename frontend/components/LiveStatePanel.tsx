"use client";

import { useState, useEffect, useCallback } from "react";
import { Radio } from "lucide-react";
import { api, LiveState, FlowWindow } from "@/lib/api";

const GREEN = "#00e676";
const RED = "#ff1744";
const MUTED = "#7070a0";
const YELLOW = "#ffc107";

function Chip({ color, text }: { color: string; text: string }) {
  return (
    <span className="text-[10px] px-1.5 py-0.5 rounded border font-mono whitespace-nowrap"
      style={{ color, borderColor: `${color}40`, background: `${color}15` }}>
      {text}
    </span>
  );
}

const pct = (x: number | null) => (x == null ? "—" : `${Math.round(x * 100)}%`);

// 外盤（綠）／內盤（紅）占比條；沒有成交資料時顯示空條
function FlowRow({ label, w, up, down }: { label: string; w: FlowWindow | undefined; up: number; down: number }) {
  const share = w?.share ?? null;
  const color = share == null ? MUTED : share >= up ? GREEN : share <= down ? RED : MUTED;
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between text-[11px] font-mono">
        <span className="text-[#7070a0]">{label}</span>
        <span style={{ color }}>
          {share == null ? "尚無成交" : `外盤 ${pct(share)} · 內盤 ${pct(1 - share)}`}
        </span>
      </div>
      <div className="h-1.5 rounded bg-[#0d0d14] overflow-hidden flex">
        {share != null && (
          <>
            <div style={{ width: `${share * 100}%`, background: GREEN, opacity: 0.8 }} />
            <div style={{ width: `${(1 - share) * 100}%`, background: RED, opacity: 0.8 }} />
          </>
        )}
      </div>
      <div className="text-[10px] text-[#404060] font-mono">
        {w && w.n > 0
          ? `${w.n} 筆 · 約 ${w.span_sec} 秒 · 口數外盤 ${pct(w.vol_share)}`
          : "—"}
      </div>
    </div>
  );
}

export default function LiveStatePanel() {
  const [st, setSt] = useState<LiveState | null>(null);
  const [err, setErr] = useState(false);

  const load = useCallback(async () => {
    try {
      setSt(await api.market.live());
      setErr(false);
    } catch {
      setErr(true);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [load]);

  const th = st?.thresholds ?? { flow_up: 0.6, flow_down: 0.4, big_move: 1.5, quiet: 0.6 };
  const cohColor = st?.coherence === 1 ? GREEN : st?.coherence === -1 ? RED : MUTED;
  const cohText = st?.coherence === 1 ? "協調" : st?.coherence === -1 ? "矛盾" : st?.coherence === 0 ? "中性" : "—";
  const rangeColor = st?.range_label === "大波動" ? YELLOW : st?.range_label === "正常" ? GREEN : MUTED;

  return (
    <div className="lg:col-span-2 bg-[#141420] rounded-xl border border-[#1e1e3a] p-5 space-y-4">
      <div className="flex items-center gap-2">
        <Radio size={14} className="text-[#00e676]" />
        <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest">盤中即時</h2>
        <span className="text-[11px] text-[#7070a0] font-mono">
          {st?.prefix ?? "TMF"}
          {st?.last != null && ` · 最新 ${st.last.toLocaleString()}`}
          {st?.last_age != null && st.last_age > 30 && (
            <span className="text-[#ffc107]"> · {Math.round(st.last_age)} 秒前</span>
          )}
        </span>
        <span className="ml-auto text-[10px] text-[#404060]">
          {err ? "連線中斷，顯示舊資料" : "每 3 秒更新"}
        </span>
      </div>

      {!st?.ready ? (
        <p className="text-[11px] text-[#404060]">尚未收到行情（券商連線並有成交後會顯示）。</p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div className="bg-[#0d0d14] rounded-lg border border-[#1e1e3a] p-3 space-y-3">
            <div className="text-[10px] text-[#7070a0] uppercase tracking-widest">外/內盤（只算真實成交）</div>
            {(["20", "100", "300"] as const).map((k) => (
              <FlowRow key={k} label={`最近 ${k} 筆`} w={st.flow[k]} up={th.flow_up} down={th.flow_down} />
            ))}
          </div>

          <div className="bg-[#0d0d14] rounded-lg border border-[#1e1e3a] p-3 space-y-1.5">
            <div className="text-[10px] text-[#7070a0] uppercase tracking-widest">日盤振幅</div>
            {st.range != null ? (
              <>
                <div className="flex items-baseline gap-2">
                  <span className="font-mono text-2xl">{st.range_ratio != null ? `${st.range_ratio.toFixed(2)}×` : `${st.range} 點`}</span>
                  {st.range_label && <Chip color={rangeColor} text={st.range_label} />}
                </div>
                <div className="text-[11px] text-[#7070a0] font-mono">
                  高 {st.high?.toLocaleString()} · 低 {st.low?.toLocaleString()} · 振幅 {st.range} 點
                </div>
                <div className="text-[11px] text-[#404060] font-mono">
                  {st.avg_range != null ? `近 20 日均 ${st.avg_range} 點` : "近 20 日均：資料不足"}
                </div>
                {st.partial && (
                  <div className="text-[11px] text-[#ffc107]">
                    資料自 {st.since} 起（重啟後重新累計，可能少算開盤段）
                  </div>
                )}
              </>
            ) : (
              <div className="text-[11px] text-[#404060]">
                {st.in_session ? "今日日盤尚無資料" : "非日盤時段（日盤 08:45~13:45）"}
              </div>
            )}
          </div>

          <div className="bg-[#0d0d14] rounded-lg border border-[#1e1e3a] p-3 space-y-1.5">
            <div className="text-[10px] text-[#7070a0] uppercase tracking-widest">與盤前判斷</div>
            <div className="flex items-center gap-2">
              <Chip color={cohColor} text={cohText} />
              <span className="text-[11px] text-[#7070a0] font-mono">
                {st.pre.want === 1 ? "盤前偏向做多" : st.pre.want === -1 ? "盤前偏向做空" : "盤前不偏向任何方向"}
              </span>
            </div>
            <div className="text-sm text-[#e0e0f0]">{st.coherence_text}</div>
            {st.pre.hint && <div className="text-[11px] text-[#404060]">盤前建議：{st.pre.hint}</div>}
          </div>
        </div>
      )}

      <p className="text-[10px] text-[#404060] leading-relaxed">
        只顯示、不影響下單。判讀門檻（100 筆外盤占比 ≥{Math.round(th.flow_up * 100)}% 買方主動、≤{Math.round(th.flow_down * 100)}% 賣方主動；
        振幅比 ≥{th.big_move}× 大波動、≤{th.quiet}× 清淡）尚未經過歷史驗證，僅供參考。
      </p>
    </div>
  );
}
