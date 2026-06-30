"use client";

import { useState, useEffect, useCallback } from "react";
import { Play, Square, AlertCircle } from "lucide-react";
import { api, StrategyInfo } from "@/lib/api";

export default function StrategyPanel() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [editParams, setEditParams] = useState<Record<string, Record<string, number>>>({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const loadData = useCallback(async () => {
    try {
      const data = await api.strategy.list();
      setStrategies(data);
      setEditParams((prev) => {
        const next = { ...prev };
        data.forEach((s) => {
          if (!next[s.name]) next[s.name] = { ...s.params };
        });
        return next;
      });
    } catch { /* silent on poll */ }
  }, []);

  useEffect(() => {
    loadData();
    const id = setInterval(loadData, 2000);
    return () => clearInterval(id);
  }, [loadData]);

  const flash = (ok: boolean, text: string) => {
    setMsg({ ok, text });
    setTimeout(() => setMsg(null), 4000);
  };

  const handleStart = async (s: StrategyInfo) => {
    setBusy(true);
    try {
      await api.strategy.start(s.name, editParams[s.name] ?? {});
      flash(true, `${s.name} 已啟動`);
    } catch (e: unknown) {
      flash(false, e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      loadData();
    }
  };

  const handleStop = async (s: StrategyInfo) => {
    setBusy(true);
    try {
      await api.strategy.stop(s.name);
      flash(true, `${s.name} 已停止`);
    } catch (e: unknown) {
      flash(false, e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      loadData();
    }
  };

  const running = strategies.find((s) => s.is_running);
  const anyRunning = !!running;
  const sel = strategies.find((s) => s.name === selected);

  return (
    <div className="bg-[#141420] rounded-xl border border-[#1e1e3a] p-5 space-y-4">
      <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest">
        策略管理
      </h2>

      {msg && (
        <div className={`px-3 py-2 rounded text-xs ${
          msg.ok
            ? "bg-[#00e676]/10 text-[#00e676] border border-[#00e676]/20"
            : "bg-[#ff1744]/10 text-[#ff1744] border border-[#ff1744]/20"
        }`}>
          {msg.text}
        </div>
      )}

      {/* ── 執行中的策略（常駐顯示） ───────────────────── */}
      {running && (
        <div className="bg-[#0d0d14] rounded-lg border border-[#00e676]/20 p-3 space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <div className="w-2 h-2 rounded-full bg-[#00e676] animate-pulse" />
              <span className="font-mono text-sm">{running.name}</span>
              <span className="text-[10px] px-1.5 py-0.5 bg-[#00e676]/10 text-[#00e676] rounded border border-[#00e676]/20">
                執行中
              </span>
            </div>
            <button
              onClick={() => handleStop(running)}
              disabled={busy}
              className="flex items-center gap-1 px-3 py-1.5 text-xs rounded border border-[#ff1744]/40 text-[#ff1744] bg-[#ff1744]/10 hover:bg-[#ff1744]/20 transition-colors disabled:opacity-40"
            >
              <Square size={11} /> 停止
            </button>
          </div>

          <div className="grid grid-cols-3 gap-2 text-center">
            {([
              {
                label: "部位",
                value: running.position > 0 ? `多 ${running.position}口` : running.position < 0 ? `空 ${Math.abs(running.position)}口` : "無",
                color: running.position > 0 ? "text-[#00e676]" : running.position < 0 ? "text-[#ff1744]" : "text-[#7070a0]",
              },
              {
                label: "未實現",
                value: `${running.unrealized_pnl >= 0 ? "+" : ""}${running.unrealized_pnl.toLocaleString()}`,
                color: running.unrealized_pnl >= 0 ? "text-[#00e676]" : "text-[#ff1744]",
              },
              {
                label: "已實現",
                value: `${running.realized_pnl >= 0 ? "+" : ""}${running.realized_pnl.toLocaleString()}`,
                color: running.realized_pnl >= 0 ? "text-[#00e676]" : "text-[#ff1744]",
              },
            ] as { label: string; value: string; color: string }[]).map(({ label, value, color }) => (
              <div key={label} className="bg-[#141420] rounded py-2">
                <div className="text-[11px] text-[#7070a0] mb-0.5">{label}</div>
                <div className={`font-mono text-sm ${color}`}>{value}</div>
              </div>
            ))}
          </div>

          {/* 目前生效的風控參數 */}
          {(running.params.daily_max_loss > 0 || running.params.max_trades_per_day > 0) && (
            <div className="bg-[#141420] rounded p-2">
              <div className="text-[10px] text-[#7070a0] uppercase tracking-widest mb-1.5">風控設定</div>
              <div className="flex gap-4 flex-wrap">
                {running.params.daily_max_loss > 0 && (
                  <span className="text-[11px] font-mono text-[#a0a0c0]">
                    當日虧損上限 <span className="text-[#ffc107]">{running.params.daily_max_loss.toLocaleString()} 元</span>
                  </span>
                )}
                {running.params.max_trades_per_day > 0 && (
                  <span className="text-[11px] font-mono text-[#a0a0c0]">
                    最大進場次數 <span className="text-[#ffc107]">{running.params.max_trades_per_day} 次</span>
                  </span>
                )}
              </div>
            </div>
          )}

          {running.events.length > 0 && (
            <div className="bg-[#141420] rounded p-2">
              <div className="text-[10px] text-[#7070a0] uppercase tracking-widest mb-1.5">事件</div>
              <ul className="space-y-0.5 max-h-24 overflow-y-auto">
                {[...running.events].reverse().slice(0, 8).map((ev, i) => (
                  <li key={i} className="font-mono text-[11px] text-[#a0a0c0] leading-tight">{ev}</li>
                ))}
              </ul>
            </div>
          )}

          {running.errors.length > 0 && (
            <div className="flex items-start gap-1.5 text-xs text-[#ffc107]">
              <AlertCircle size={12} className="mt-0.5 flex-shrink-0" />
              <span>{running.errors[running.errors.length - 1]}</span>
            </div>
          )}
        </div>
      )}

      {/* ── 策略選擇器 ──────────────────────────────────── */}
      <div>
        <div className="text-[11px] text-[#7070a0] mb-2">選擇策略</div>
        <div className="grid grid-cols-4 gap-1.5">
          {strategies.map((s) => (
            <button
              key={s.name}
              onClick={() => setSelected(selected === s.name ? null : s.name)}
              className={`px-2 py-2 rounded text-[11px] font-mono text-left transition-colors border ${
                s.is_running
                  ? "border-[#00e676]/30 bg-[#00e676]/5 text-[#00e676]"
                  : selected === s.name
                  ? "border-[#3b82f6]/50 bg-[#3b82f6]/10 text-[#e0e0f0]"
                  : "border-[#1e1e3a] bg-[#0d0d14] text-[#7070a0] hover:border-[#2e2e4a] hover:text-[#e0e0f0]"
              }`}
            >
              {s.is_running && (
                <span className="inline-block w-1.5 h-1.5 rounded-full bg-[#00e676] mr-1 mb-0.5 animate-pulse" />
              )}
              {s.name}
            </button>
          ))}
        </div>
      </div>

      {/* ── 選中策略的參數 ──────────────────────────────── */}
      {sel && !sel.is_running && (
        <div className="border border-[#1e1e3a] rounded-lg p-3 space-y-3">
          <div className="text-[11px] text-[#7070a0] font-semibold uppercase tracking-widest">
            {sel.name} 參數
          </div>

          {sel.param_schema.length > 0 ? (
            <div className="grid grid-cols-2 gap-2">
              {sel.param_schema.map((p) => (
                <div key={p.key}>
                  <label className="text-[11px] text-[#7070a0] block mb-1">{p.label}</label>
                  <input
                    type="number"
                    min={p.min}
                    max={p.max}
                    value={editParams[sel.name]?.[p.key] ?? ""}
                    disabled={anyRunning}
                    onChange={(e) =>
                      setEditParams((prev) => ({
                        ...prev,
                        [sel.name]: { ...prev[sel.name], [p.key]: Number(e.target.value) },
                      }))
                    }
                    className="w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-2 py-1.5 text-sm font-mono text-[#e0e0f0] focus:outline-none focus:border-[#3b82f6] disabled:opacity-40"
                  />
                </div>
              ))}
            </div>
          ) : (
            <p className="text-[11px] text-[#404060]">此策略無可調整參數</p>
          )}

          <button
            onClick={() => handleStart(sel)}
            disabled={busy || anyRunning}
            className="w-full flex items-center justify-center gap-1.5 px-3 py-2 text-xs rounded border border-[#00e676]/40 text-[#00e676] bg-[#00e676]/10 hover:bg-[#00e676]/20 transition-colors disabled:opacity-40"
          >
            <Play size={11} />
            {anyRunning ? "請先停止現有策略" : `啟動 ${sel.name}`}
          </button>
        </div>
      )}
    </div>
  );
}
