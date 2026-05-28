"use client";

import { useState } from "react";
import { ArrowUp, ArrowDown } from "lucide-react";
import { api } from "@/lib/api";

type Action = "Buy" | "Sell";
type PriceType = "MKT" | "LMT";

export default function OrderPanel() {
  const [action, setAction] = useState<Action>("Buy");
  const [quantity, setQuantity] = useState(1);
  const [priceType, setPriceType] = useState<PriceType>("MKT");
  const [limitPrice, setLimitPrice] = useState("");
  const [orderType, setOrderType] = useState("IOC");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const submit = async () => {
    setBusy(true);
    try {
      const res = await api.order.place({
        action,
        quantity,
        price: priceType === "LMT" ? Number(limitPrice) : undefined,
        price_type: priceType,
        order_type: orderType as "ROD" | "IOC" | "FOK",
        octype: "Auto",
      });
      setResult({ ok: true, text: `委託成功 — ${res.trade_id}` });
    } catch (e: unknown) {
      setResult({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
      setTimeout(() => setResult(null), 6000);
    }
  };

  const isBuy = action === "Buy";

  return (
    <div className="bg-[#141420] rounded-xl border border-[#1e1e3a] p-5">
      <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest mb-4">
        手動下單 — TMFR1（微台）
      </h2>

      {/* Buy / Sell toggle */}
      <div className="flex gap-2 mb-4">
        {(["Buy", "Sell"] as Action[]).map((a) => {
          const active = action === a;
          const color = a === "Buy" ? "#00e676" : "#ff1744";
          return (
            <button
              key={a}
              onClick={() => setAction(a)}
              className="flex-1 flex items-center justify-center gap-1.5 py-2.5 rounded-lg text-sm font-semibold border transition-colors"
              style={{
                borderColor: active ? `${color}60` : "#1e1e3a",
                backgroundColor: active ? `${color}18` : "transparent",
                color: active ? color : "#404060",
              }}
            >
              {a === "Buy" ? <ArrowUp size={14} /> : <ArrowDown size={14} />}
              {a === "Buy" ? "買多" : "賣空"}
            </button>
          );
        })}
      </div>

      {/* Fields */}
      <div className="grid grid-cols-2 gap-3 mb-4">
        <div>
          <label className="text-[11px] text-[#7070a0] block mb-1">口數</label>
          <input
            type="number"
            min={1}
            value={quantity}
            onChange={(e) => setQuantity(Number(e.target.value))}
            className="w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-3 py-2 font-mono text-sm text-[#e0e0f0] focus:outline-none focus:border-[#3b82f6]"
          />
        </div>
        <div>
          <label className="text-[11px] text-[#7070a0] block mb-1">價格類型</label>
          <select
            value={priceType}
            onChange={(e) => setPriceType(e.target.value as PriceType)}
            className="w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-3 py-2 text-sm text-[#e0e0f0] focus:outline-none focus:border-[#3b82f6]"
          >
            <option value="MKT">市價</option>
            <option value="LMT">限價</option>
          </select>
        </div>

        {priceType === "LMT" && (
          <div className="col-span-2">
            <label className="text-[11px] text-[#7070a0] block mb-1">限價（點）</label>
            <input
              type="number"
              value={limitPrice}
              onChange={(e) => setLimitPrice(e.target.value)}
              placeholder="例：21500"
              className="w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-3 py-2 font-mono text-sm text-[#e0e0f0] focus:outline-none focus:border-[#3b82f6]"
            />
          </div>
        )}

        <div>
          <label className="text-[11px] text-[#7070a0] block mb-1">委託類型</label>
          <select
            value={orderType}
            onChange={(e) => setOrderType(e.target.value)}
            className="w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-3 py-2 text-sm text-[#e0e0f0] focus:outline-none focus:border-[#3b82f6]"
          >
            <option value="IOC">IOC（立即成交）</option>
            <option value="ROD">ROD（當日有效）</option>
            <option value="FOK">FOK（全部成交）</option>
          </select>
        </div>
      </div>

      {/* Submit */}
      <button
        onClick={submit}
        disabled={busy || (priceType === "LMT" && !limitPrice)}
        className="w-full py-3 rounded-lg font-semibold text-sm transition-colors disabled:opacity-40"
        style={{
          backgroundColor: isBuy ? "#00e676" : "#ff1744",
          color: isBuy ? "#0d0d14" : "#fff",
        }}
      >
        {busy
          ? "送出中..."
          : `確認${isBuy ? "買多" : "賣空"} ${quantity} 口${priceType === "LMT" ? ` @ ${limitPrice}` : "（市價）"}`}
      </button>

      {result && (
        <div
          className={`mt-3 text-xs px-3 py-2 rounded border ${
            result.ok
              ? "bg-[#00e676]/10 text-[#00e676] border-[#00e676]/20"
              : "bg-[#ff1744]/10 text-[#ff1744] border-[#ff1744]/20"
          }`}
        >
          {result.text}
        </div>
      )}
    </div>
  );
}
