"use client";

import { useState, useEffect, useCallback } from "react";
import { X, Check } from "lucide-react";
import { api, Trade, Watch, ORDER_PLACED_EVENT } from "@/lib/api";

const STATUS: Record<string, { label: string; color: string }> = {
  Filled:        { label: "成交",   color: "text-[#00e676]" },
  Submitted:     { label: "委託中", color: "text-[#3b82f6]" },
  PendingSubmit: { label: "送出中", color: "text-[#ffc107]" },
  Cancelled:     { label: "取消",   color: "text-[#404060]" },
  Failed:        { label: "失敗",   color: "text-[#ff1744]" },
};

function WatchRow({ watch, onChanged }: { watch: Watch; onChanged: () => void }) {
  const [sl, setSl] = useState(String(watch.stop_loss_pts));
  const [tp, setTp] = useState(String(watch.take_profit_pts));
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState(false);

  const dirty =
    Number(sl) !== watch.stop_loss_pts || Number(tp) !== watch.take_profit_pts;

  const save = async () => {
    setSaving(true);
    try {
      await api.order.updateWatch(watch.id, {
        stop_loss_pts: Number(sl),
        take_profit_pts: Number(tp),
      });
      onChanged();
    } catch { /* ignore */ }
    setSaving(false);
  };

  const remove = async () => {
    setRemoving(true);
    try {
      await api.order.removeWatch(watch.id);
      onChanged();
    } catch { /* ignore */ }
    setRemoving(false);
  };

  const dirColor = watch.direction === "Buy" ? "text-[#00e676]" : "text-[#ff1744]";

  return (
    <tr className="border-t border-[#1e1e3a]">
      <td className="py-2 pr-2 font-mono text-[11px] text-[#7070a0]">{watch.id}</td>
      <td className={`py-2 pr-2 text-xs font-semibold ${dirColor}`}>
        {watch.direction === "Buy" ? "多" : "空"} {watch.contract}
      </td>
      <td className="py-2 pr-2 font-mono text-[11px] text-[#e0e0f0]">
        {watch.entry_price > 0 ? watch.entry_price.toLocaleString() : "市價"}
      </td>
      {/* stop loss input */}
      <td className="py-2 pr-1">
        <input
          type="number"
          min={0}
          value={sl}
          onChange={(e) => setSl(e.target.value)}
          className="w-16 bg-[#0d0d14] border border-[#ff1744]/30 rounded px-1.5 py-1 font-mono text-xs text-[#e0e0f0] focus:outline-none focus:border-[#ff1744]"
        />
      </td>
      {/* take profit input */}
      <td className="py-2 pr-2">
        <input
          type="number"
          min={0}
          value={tp}
          onChange={(e) => setTp(e.target.value)}
          className="w-16 bg-[#0d0d14] border border-[#00e676]/30 rounded px-1.5 py-1 font-mono text-xs text-[#e0e0f0] focus:outline-none focus:border-[#00e676]"
        />
      </td>
      {/* save / remove */}
      <td className="py-2 text-right">
        <div className="flex items-center justify-end gap-1.5">
          {dirty && (
            <button
              onClick={save}
              disabled={saving}
              className="text-[#3b82f6] hover:text-[#60a5fa] disabled:opacity-30 transition-colors"
              title="儲存"
            >
              <Check size={13} />
            </button>
          )}
          <button
            onClick={remove}
            disabled={removing}
            className="text-[#404060] hover:text-[#ff1744] disabled:opacity-30 transition-colors"
            title="移除監控"
          >
            <X size={13} />
          </button>
        </div>
      </td>
    </tr>
  );
}

export default function TradesPanel() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [watches, setWatches] = useState<Watch[]>([]);
  const [cancelling, setCancelling] = useState<string | null>(null);

  const loadTrades = useCallback(async () => {
    try { setTrades(await api.order.trades()); } catch { /* silent */ }
  }, []);

  const loadWatches = useCallback(async () => {
    try { setWatches(await api.order.watches()); } catch { /* silent */ }
  }, []);

  useEffect(() => {
    loadTrades();
    loadWatches();
    const t = setInterval(loadTrades, 15000);
    const w = setInterval(loadWatches, 5000);
    // 下單成功 → 立即刷新（稍等讓委託落到券商端）
    const onOrder = () => { setTimeout(() => { loadTrades(); loadWatches(); }, 1000); };
    window.addEventListener(ORDER_PLACED_EVENT, onOrder);
    return () => {
      clearInterval(t);
      clearInterval(w);
      window.removeEventListener(ORDER_PLACED_EVENT, onOrder);
    };
  }, [loadTrades, loadWatches]);

  const cancel = async (id: string) => {
    setCancelling(id);
    try { await api.order.cancel(id); loadTrades(); } catch { /* ignore */ }
    setCancelling(null);
  };

  return (
    <div className="bg-[#141420] rounded-xl border border-[#1e1e3a] p-5 flex flex-col gap-5">

      {/* ── 停損停利監控 ─────────────────────────────── */}
      <div>
        <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest mb-3">
          停損停利監控
        </h2>
        {watches.length === 0 ? (
          <p className="text-sm text-[#404060] text-center py-3">無監控中的部位</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] text-[#7070a0]">
                <th className="text-left pb-1.5 font-normal">ID</th>
                <th className="text-left pb-1.5 font-normal">方向</th>
                <th className="text-left pb-1.5 font-normal">入場</th>
                <th className="text-left pb-1.5 font-normal">
                  <span className="text-[#ff1744]/70">停損</span>
                </th>
                <th className="text-left pb-1.5 font-normal">
                  <span className="text-[#00e676]/70">停利</span>
                </th>
                <th className="pb-1.5 w-12"></th>
              </tr>
            </thead>
            <tbody>
              {watches.map((w) => (
                <WatchRow key={w.id} watch={w} onChanged={loadWatches} />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* ── 委託紀錄 ─────────────────────────────────── */}
      <div>
        <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest mb-3">
          委託紀錄
        </h2>
        {trades.length === 0 ? (
          <p className="text-sm text-[#404060] text-center py-3">今日無委託紀錄</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-[11px] text-[#7070a0] border-b border-[#1e1e3a]">
                  <th className="text-left pb-2 font-normal">方向</th>
                  <th className="text-right pb-2 font-normal">委託價</th>
                  <th className="text-right pb-2 font-normal">成交均價</th>
                  <th className="text-right pb-2 font-normal">委 / 成</th>
                  <th className="text-right pb-2 font-normal">狀態</th>
                  <th className="text-right pb-2 font-normal">時間</th>
                  <th className="pb-2 w-6"></th>
                </tr>
              </thead>
              <tbody className="font-mono divide-y divide-[#0d0d14]">
                {trades.map((t) => {
                  const st = STATUS[t.status] ?? { label: t.status, color: "text-[#7070a0]" };
                  return (
                    <tr key={t.id} className="hover:bg-[#1a1a2e]/40 transition-colors">
                      <td className={`py-2.5 font-semibold ${t.action === "Buy" ? "text-[#00e676]" : "text-[#ff1744]"}`}>
                        {t.action === "Buy" ? "買" : "賣"}
                      </td>
                      <td className="text-right text-[#e0e0f0]">
                        {t.price === 0 ? "市價" : t.price.toLocaleString()}
                      </td>
                      <td className="text-right">
                        {t.deal_price > 0
                          ? <span className="text-[#ffc107]">{t.deal_price.toLocaleString()}</span>
                          : <span className="text-[#404060]">—</span>}
                      </td>
                      <td className="text-right text-[#7070a0]">{t.deal_quantity} / {t.quantity}</td>
                      <td className={`text-right ${st.color}`}>{st.label}</td>
                      <td className="text-right text-[#404060] text-[11px]">
                        {t.deal_time || t.order_time || "—"}
                      </td>
                      <td className="text-right pl-2">
                        {t.status === "Submitted" && (
                          <button
                            onClick={() => cancel(t.id)}
                            disabled={cancelling === t.id}
                            className="text-[#404060] hover:text-[#ff1744] transition-colors disabled:opacity-30"
                            title="取消委託"
                          >
                            <X size={13} />
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

    </div>
  );
}
