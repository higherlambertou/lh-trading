"use client";

import { useState, useEffect, useCallback } from "react";
import { TrendingUp, TrendingDown } from "lucide-react";
import { api, Position, Margin, ProfitLoss } from "@/lib/api";

function Num({ value }: { value: number }) {
  const pos = value >= 0;
  return (
    <span className={`font-mono ${pos ? "text-[#00e676]" : "text-[#ff1744]"}`}>
      {pos ? "+" : ""}
      {value.toLocaleString()}
    </span>
  );
}

export default function PositionPanel() {
  const [positions, setPositions] = useState<Position[]>([]);
  const [margin, setMargin] = useState<Margin | null>(null);
  const [pnlList, setPnlList] = useState<ProfitLoss[]>([]);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      const [pos, mar, pnl] = await Promise.all([
        api.position.list(),
        api.position.margin(),
        api.position.pnl(),
      ]);
      setPositions(pos);
      setMargin(mar);
      setPnlList(pnl);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "無法取得部位資料");
    }
  }, []);

  useEffect(() => {
    loadData();
    const id = setInterval(loadData, 2000);
    return () => clearInterval(id);
  }, [loadData]);

  const totalPnl = positions.reduce((sum, p) => sum + p.pnl, 0);
  const totalRealizedPnl = pnlList.reduce((sum, p) => sum + p.pnl, 0);

  return (
    <div className="bg-[#141420] rounded-xl border border-[#1e1e3a] p-5">
      <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest mb-4">
        部位 / 損益
      </h2>

      {error ? (
        <p className="text-xs text-[#ff1744]/70 bg-[#ff1744]/5 border border-[#ff1744]/10 rounded p-3">
          {error}
        </p>
      ) : (
        <>
          {/* Margin grid */}
          {margin && (
            <div className="grid grid-cols-2 gap-2 mb-4">
              {[
                { label: "權益數", value: margin.equity.toLocaleString(), plain: true },
                { label: "未實現損益", value: totalPnl, plain: false },
                { label: "已實現損益", value: totalRealizedPnl, plain: false },
                { label: "原始保證金", value: margin.initial_margin.toLocaleString(), plain: true },
                { label: "維持保證金", value: margin.maintenance_margin.toLocaleString(), plain: true },
              ].map(({ label, value, plain }) => (
                <div key={label} className="bg-[#0d0d14] rounded-lg p-3">
                  <div className="text-[11px] text-[#7070a0] mb-1">{label}</div>
                  {plain ? (
                    <div className="font-mono text-sm text-[#e0e0f0]">{value}</div>
                  ) : (
                    <Num value={value as number} />
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Realized PnL detail */}
          {pnlList.length > 0 && (
            <div className="mb-4">
              <div className="text-[11px] text-[#7070a0] mb-2">已實現損益明細</div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs font-mono">
                  <thead>
                    <tr className="text-[#404060] border-b border-[#1e1e3a]">
                      <th className="text-left pb-1.5 font-normal">商品</th>
                      <th className="text-right pb-1.5 font-normal">口數</th>
                      <th className="text-right pb-1.5 font-normal">均價</th>
                      <th className="text-right pb-1.5 font-normal">損益</th>
                      <th className="text-right pb-1.5 font-normal">日期</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[#0d0d14]">
                    {pnlList.map((pl) => (
                      <tr key={pl.dseq} className="hover:bg-[#1a1a2e]/40">
                        <td className="py-1.5 text-[#e0e0f0]">{pl.code}</td>
                        <td className="text-right text-[#7070a0]">{pl.quantity}</td>
                        <td className="text-right text-[#7070a0]">{pl.price.toLocaleString()}</td>
                        <td className="text-right"><Num value={pl.pnl} /></td>
                        <td className="text-right text-[#404060]">{pl.date || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Positions */}
          {positions.length === 0 ? (
            <p className="text-sm text-[#404060] text-center py-6">目前無持倉</p>
          ) : (
            <div className="space-y-2">
              {positions.map((p, i) => (
                <div
                  key={i}
                  className="bg-[#0d0d14] rounded-lg px-4 py-3 flex items-center justify-between"
                >
                  <div className="flex items-center gap-3">
                    <span
                      className={`flex items-center gap-1 text-sm font-semibold ${
                        p.direction === "Buy" ? "text-[#00e676]" : "text-[#ff1744]"
                      }`}
                    >
                      {p.direction === "Buy" ? (
                        <TrendingUp size={14} />
                      ) : (
                        <TrendingDown size={14} />
                      )}
                      {p.direction === "Buy" ? "多" : "空"}
                    </span>
                    <div>
                      <div className="font-mono text-sm text-[#e0e0f0]">{p.code}</div>
                      <div className="text-[11px] text-[#7070a0]">
                        {p.quantity} 口 @ {p.price.toLocaleString()}
                      </div>
                    </div>
                  </div>
                  <div className="text-right">
                    <Num value={p.pnl} />
                    <div className="text-[11px] text-[#7070a0] mt-0.5">
                      現價 {p.last_price.toLocaleString()}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
