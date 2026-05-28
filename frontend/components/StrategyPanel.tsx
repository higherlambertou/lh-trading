"use client";

import { useState, useEffect, useCallback } from "react";
import { Play, Square, AlertCircle } from "lucide-react";
import { api, StrategyInfo } from "@/lib/api";

export default function StrategyPanel() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
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

  return (
    <div className="bg-[#141420] rounded-xl border border-[#1e1e3a] p-5">
      <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest mb-4">
        策略管理
      </h2>

      {msg && (
        <div
          className={`mb-4 px-3 py-2 rounded text-xs ${
            msg.ok
              ? "bg-[#00e676]/10 text-[#00e676] border border-[#00e676]/20"
              : "bg-[#ff1744]/10 text-[#ff1744] border border-[#ff1744]/20"
          }`}
        >
          {msg.text}
        </div>
      )}

      {strategies.map((s) => (
        <div key={s.name} className="space-y-3">
          {/* Name + status + button */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <div
                className={`w-2 h-2 rounded-full ${
                  s.is_running ? "bg-[#00e676] animate-pulse" : "bg-[#404060]"
                }`}
              />
              <span className="font-mono text-sm">{s.name}</span>
              {s.is_running && (
                <span className="text-[10px] px-1.5 py-0.5 bg-[#00e676]/10 text-[#00e676] rounded border border-[#00e676]/20">
                  執行中
                </span>
              )}
            </div>
            {s.is_running ? (
              <button
                onClick={() => handleStop(s)}
                disabled={busy}
                className="flex items-center gap-1 px-3 py-1.5 text-xs rounded border border-[#ff1744]/40 text-[#ff1744] bg-[#ff1744]/10 hover:bg-[#ff1744]/20 transition-colors disabled:opacity-40"
              >
                <Square size={11} /> 停止
              </button>
            ) : (
              <button
                onClick={() => handleStart(s)}
                disabled={busy}
                className="flex items-center gap-1 px-3 py-1.5 text-xs rounded border border-[#00e676]/40 text-[#00e676] bg-[#00e676]/10 hover:bg-[#00e676]/20 transition-colors disabled:opacity-40"
              >
                <Play size={11} /> 啟動
              </button>
            )}
          </div>

          {/* Param inputs */}
          {s.param_schema.length > 0 && (
            <div className="grid grid-cols-2 gap-2">
              {s.param_schema.map((p) => (
                <div key={p.key}>
                  <label className="text-[11px] text-[#7070a0] block mb-1">{p.label}</label>
                  <input
                    type="number"
                    min={p.min}
                    max={p.max}
                    value={editParams[s.name]?.[p.key] ?? ""}
                    disabled={s.is_running}
                    onChange={(e) =>
                      setEditParams((prev) => ({
                        ...prev,
                        [s.name]: { ...prev[s.name], [p.key]: Number(e.target.value) },
                      }))
                    }
                    className="w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-2 py-1.5 text-sm font-mono text-[#e0e0f0] focus:outline-none focus:border-[#3b82f6] disabled:opacity-40"
                  />
                </div>
              ))}
            </div>
          )}
          {s.is_running && (
            <p className="text-[11px] text-[#404060]">策略執行中，停止後才能修改參數</p>
          )}

          {/* Live stats */}
          {s.is_running && (
            <div className="grid grid-cols-3 gap-2 text-center">
              {[
                {
                  label: "部位",
                  value:
                    s.position > 0
                      ? `多 ${s.position}`
                      : s.position < 0
                      ? `空 ${Math.abs(s.position)}`
                      : "無",
                  color:
                    s.position > 0
                      ? "text-[#00e676]"
                      : s.position < 0
                      ? "text-[#ff1744]"
                      : "text-[#7070a0]",
                },
                {
                  label: "未實現",
                  value: `${s.unrealized_pnl >= 0 ? "+" : ""}${s.unrealized_pnl.toLocaleString()}`,
                  color: s.unrealized_pnl >= 0 ? "text-[#00e676]" : "text-[#ff1744]",
                },
                {
                  label: "最新價",
                  value: s.last_price.toLocaleString(),
                  color: "text-[#e0e0f0]",
                },
              ].map(({ label, value, color }) => (
                <div key={label} className="bg-[#0d0d14] rounded-lg py-2">
                  <div className="text-[11px] text-[#7070a0] mb-0.5">{label}</div>
                  <div className={`font-mono text-sm ${color}`}>{value}</div>
                </div>
              ))}
            </div>
          )}

          {/* Errors */}
          {s.errors.length > 0 && (
            <div className="flex items-start gap-1.5 text-xs text-[#ffc107]">
              <AlertCircle size={12} className="mt-0.5 flex-shrink-0" />
              <span>{s.errors[s.errors.length - 1]}</span>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
