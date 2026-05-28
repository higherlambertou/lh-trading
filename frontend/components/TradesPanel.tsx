"use client";

import { useState, useEffect, useCallback } from "react";
import { X } from "lucide-react";
import { api, Trade } from "@/lib/api";

const STATUS: Record<string, { label: string; color: string }> = {
  Filled:        { label: "成交",   color: "text-[#00e676]" },
  Submitted:     { label: "委託中", color: "text-[#3b82f6]" },
  PendingSubmit: { label: "送出中", color: "text-[#ffc107]" },
  Cancelled:     { label: "取消",   color: "text-[#404060]" },
  Failed:        { label: "失敗",   color: "text-[#ff1744]" },
};

export default function TradesPanel() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [cancelling, setCancelling] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      const data = await api.order.trades();
      setTrades(data);
    } catch { /* silent */ }
  }, []);

  useEffect(() => {
    loadData();
    const id = setInterval(loadData, 3000);
    return () => clearInterval(id);
  }, [loadData]);

  const cancel = async (id: string) => {
    setCancelling(id);
    try {
      await api.order.cancel(id);
      loadData();
    } catch { /* ignore */ }
    setCancelling(null);
  };

  return (
    <div className="bg-[#141420] rounded-xl border border-[#1e1e3a] p-5">
      <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest mb-4">
        委託紀錄
      </h2>

      {trades.length === 0 ? (
        <p className="text-sm text-[#404060] text-center py-8">今日無委託紀錄</p>
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
                    <td
                      className={`py-2.5 font-semibold ${
                        t.action === "Buy" ? "text-[#00e676]" : "text-[#ff1744]"
                      }`}
                    >
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
                    <td className="text-right text-[#7070a0]">
                      {t.deal_quantity} / {t.quantity}
                    </td>
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
  );
}
