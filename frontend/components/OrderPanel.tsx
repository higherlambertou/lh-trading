"use client";

import { useEffect, useState } from "react";
import { ArrowUp, ArrowDown } from "lucide-react";
import { api } from "@/lib/api";

type Action = "Buy" | "Sell";
type PriceType = "MKT" | "LMT";
type Contract = "TMF" | "MXF" | "TXF";
type Mode = "future" | "option";
type Right = "C" | "P";

const CONTRACT_OPTIONS: { value: Contract; label: string; sub: string }[] = [
  { value: "TMF", label: "微台 TMFR1", sub: "10元/點" },
  { value: "MXF", label: "小台 MXFR1", sub: "50元/點" },
  { value: "TXF", label: "大台 TXFR1", sub: "200元/點" },
];

function fmtMonth(m: string): string {
  // "202606" → "2026/06"，週選代碼維持原樣
  return /^\d{6}$/.test(m) ? `${m.slice(0, 4)}/${m.slice(4)}` : m;
}

export default function OrderPanel() {
  const [mode, setMode] = useState<Mode>("future");

  // 共用
  const [action, setAction] = useState<Action>("Buy");
  const [quantity, setQuantity] = useState(1);
  const [orderType, setOrderType] = useState("IOC");
  const [limitPrice, setLimitPrice] = useState("");
  const [stopLoss, setStopLoss] = useState("");
  const [takeProfit, setTakeProfit] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  // 期貨專用
  const [contract, setContract] = useState<Contract>("TMF");
  const [priceType, setPriceType] = useState<PriceType>("MKT");
  const [futPrice, setFutPrice] = useState<number | null>(null);

  // 選擇權專用
  const [right, setRight] = useState<Right>("C");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [deliveryMonth, setDeliveryMonth] = useState("");
  const [strikes, setStrikes] = useState<number[]>([]);
  const [strike, setStrike] = useState<number | "">("");
  const [exitBuffer, setExitBuffer] = useState("3");
  const [optLoading, setOptLoading] = useState(false);
  const [optErr, setOptErr] = useState("");
  const [optQuote, setOptQuote] = useState<{
    close: number;
    bid: number;
    ask: number;
    vol: number;
  } | null>(null);

  // 切到選擇權 → 載入到期月份；委託類型預設 ROD（選擇權多為掛單等成交）
  useEffect(() => {
    if (mode !== "option") return;
    setOrderType("ROD");
    if (expiries.length) return;
    setOptLoading(true);
    setOptErr("");
    api.order
      .optionExpiries()
      .then((xs) => {
        setExpiries(xs);
        if (xs.length) setDeliveryMonth((cur) => cur || xs[0]);
      })
      .catch((e) => setOptErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setOptLoading(false));
  }, [mode, expiries.length]);

  // 月份 / 買賣權變動 → 載入履約價，預設挑中間（接近價平）
  useEffect(() => {
    if (mode !== "option" || !deliveryMonth) return;
    setOptLoading(true);
    setOptErr("");
    api.order
      .optionStrikes(deliveryMonth, right)
      .then((xs) => {
        setStrikes(xs);
        setStrike(xs.length ? xs[Math.floor(xs.length / 2)] : "");
      })
      .catch((e) => setOptErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setOptLoading(false));
  }, [mode, deliveryMonth, right]);

  // 選好合約 → 顯示參考現價，每 5 秒刷新（選擇權不需每秒，省行情查詢額度）
  useEffect(() => {
    if (mode !== "option" || !deliveryMonth || strike === "") {
      setOptQuote(null);
      return;
    }
    let alive = true;
    const fetchQ = () => {
      api.order
        .optionQuote(deliveryMonth, Number(strike), right)
        .then((q) => {
          if (alive) setOptQuote({ close: q.close, bid: q.bid, ask: q.ask, vol: q.total_volume });
        })
        .catch(() => {
          if (alive) setOptQuote(null);
        });
    };
    fetchQ();
    const t = setInterval(fetchQ, 5000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [mode, deliveryMonth, strike, right]);

  // 期貨現價：讀後端推播快取（不對永豐發查詢），每 2 秒刷新
  useEffect(() => {
    if (mode !== "future") {
      setFutPrice(null);
      return;
    }
    let alive = true;
    const fetchP = () => {
      api.quote
        .last()
        .then((m) => {
          if (!alive) return;
          const hit = Object.entries(m).find(([code]) => code.startsWith(contract));
          setFutPrice(hit ? hit[1] : null);
        })
        .catch(() => {
          if (alive) setFutPrice(null);
        });
    };
    fetchP();
    const t = setInterval(fetchP, 2000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [mode, contract]);

  const submitFuture = async () => {
    setBusy(true);
    try {
      const res = await api.order.place({
        action,
        quantity,
        price: priceType === "LMT" ? Number(limitPrice) : undefined,
        price_type: priceType,
        order_type: orderType as "ROD" | "IOC" | "FOK",
        octype: "Auto",
        contract,
        stop_loss_pts: stopLoss ? Number(stopLoss) : undefined,
        take_profit_pts: takeProfit ? Number(takeProfit) : undefined,
      });
      const sltp = res.watch_id ? `，停損停利監控已啟動 (${res.watch_id})` : "";
      setResult({ ok: true, text: `委託成功 — ${res.trade_id}${sltp}` });
    } catch (e: unknown) {
      setResult({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
      setTimeout(() => setResult(null), 6000);
    }
  };

  const submitOption = async () => {
    if (!deliveryMonth || strike === "" || !limitPrice) return;
    setBusy(true);
    try {
      const res = await api.order.placeOption({
        delivery_month: deliveryMonth,
        strike: Number(strike),
        option_right: right,
        action,
        quantity,
        price: Number(limitPrice),
        order_type: orderType as "ROD" | "IOC" | "FOK",
        stop_loss_pts: stopLoss ? Number(stopLoss) : undefined,
        take_profit_pts: takeProfit ? Number(takeProfit) : undefined,
        exit_buffer_pts: exitBuffer ? Number(exitBuffer) : undefined,
      });
      const sltp = res.watch_id ? `，停損停利監控已啟動 (${res.watch_id})` : "";
      setResult({
        ok: true,
        text: `委託成功 — ${res.code} @ ${res.limit_price}${sltp}`,
      });
    } catch (e: unknown) {
      setResult({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
      setTimeout(() => setResult(null), 6000);
    }
  };

  const isBuy = action === "Buy";
  const isOption = mode === "option";
  const inputCls =
    "w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-3 py-2 text-sm text-[#e0e0f0] focus:outline-none focus:border-[#3b82f6]";
  const monoCls =
    "w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-3 py-2 font-mono text-sm text-[#e0e0f0] focus:outline-none focus:border-[#3b82f6]";

  return (
    <div className="bg-[#141420] rounded-xl border border-[#1e1e3a] p-5">
      <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest mb-4">
        手動下單
      </h2>

      {/* 期貨 / 選擇權 模式切換 */}
      <div className="flex gap-2 mb-4">
        {(["future", "option"] as Mode[]).map((m) => {
          const active = mode === m;
          return (
            <button
              key={m}
              onClick={() => {
                setMode(m);
                setResult(null);
                // 切換時把限價清掉，避免把期貨點數誤當權利金送出
                setLimitPrice("");
                if (m === "future") setOrderType("IOC");
              }}
              className="flex-1 py-2 rounded-lg border text-xs font-semibold transition-colors"
              style={{
                borderColor: active ? "#a855f780" : "#1e1e3a",
                backgroundColor: active ? "#a855f718" : "transparent",
                color: active ? "#d8b4fe" : "#404060",
              }}
            >
              {m === "future" ? "期貨" : "選擇權"}
            </button>
          );
        })}
      </div>

      {/* ── 期貨：合約選擇 ── */}
      {!isOption && (
        <div className="flex gap-2 mb-4">
          {CONTRACT_OPTIONS.map((c) => {
            const active = contract === c.value;
            return (
              <button
                key={c.value}
                onClick={() => setContract(c.value)}
                className="flex-1 flex flex-col items-center py-2 rounded-lg border text-xs transition-colors"
                style={{
                  borderColor: active ? "#3b82f680" : "#1e1e3a",
                  backgroundColor: active ? "#3b82f618" : "transparent",
                  color: active ? "#93c5fd" : "#404060",
                }}
              >
                <span className="font-semibold">{c.value}</span>
                <span className="text-[10px] opacity-70">{c.sub}</span>
              </button>
            );
          })}
        </div>
      )}

      {/* 期貨現價（每 2 秒，讀快取不發查詢） */}
      {!isOption && (
        <div className="flex items-center gap-3 text-xs bg-[#0d0d14] border border-[#1e1e3a] rounded px-3 py-2 mb-4">
          <span className="text-[#7070a0]">{contract} 現價</span>
          <span className="font-mono text-[#ffc107]">
            {futPrice != null ? futPrice.toLocaleString() : "—"}
          </span>
          <button
            type="button"
            onClick={() => {
              if (futPrice == null) return;
              setPriceType("LMT");
              setOrderType("ROD");
              setLimitPrice(String(futPrice));
            }}
            disabled={futPrice == null}
            className="ml-auto px-2 py-1 rounded border border-[#3b82f660] text-[#93c5fd] hover:bg-[#3b82f618] disabled:opacity-40"
          >
            帶入限價
          </button>
        </div>
      )}

      {/* ── 選擇權：買權/賣權 + 月份 + 履約價 ── */}
      {isOption && (
        <div className="mb-4 space-y-3">
          <div className="flex gap-2">
            {(["C", "P"] as Right[]).map((r) => {
              const active = right === r;
              const color = r === "C" ? "#00e676" : "#ff1744";
              return (
                <button
                  key={r}
                  onClick={() => setRight(r)}
                  className="flex-1 py-2 rounded-lg border text-xs font-semibold transition-colors"
                  style={{
                    borderColor: active ? `${color}60` : "#1e1e3a",
                    backgroundColor: active ? `${color}18` : "transparent",
                    color: active ? color : "#404060",
                  }}
                >
                  {r === "C" ? "買權 Call" : "賣權 Put"}
                </button>
              );
            })}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-[11px] text-[#7070a0] block mb-1">到期月份</label>
              <select
                value={deliveryMonth}
                onChange={(e) => setDeliveryMonth(e.target.value)}
                disabled={optLoading || !expiries.length}
                className={inputCls}
              >
                {!expiries.length && <option value="">載入中…</option>}
                {expiries.map((m) => (
                  <option key={m} value={m}>
                    {fmtMonth(m)}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-[11px] text-[#7070a0] block mb-1">履約價</label>
              <select
                value={strike === "" ? "" : String(strike)}
                onChange={(e) => setStrike(e.target.value ? Number(e.target.value) : "")}
                disabled={optLoading || !strikes.length}
                className={monoCls}
              >
                {!strikes.length && <option value="">—</option>}
                {strikes.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* 參考現價（每 5 秒刷新） */}
          {strike !== "" && (
            <div className="flex items-center gap-3 text-xs bg-[#0d0d14] border border-[#1e1e3a] rounded px-3 py-2">
              <span className="text-[#7070a0]">現價</span>
              <span className="font-mono text-[#ffc107]">{optQuote?.close || "—"}</span>
              <span className="text-[#7070a0]">
                買<span className="font-mono text-[#00e676] ml-1">{optQuote?.bid || "—"}</span>
              </span>
              <span className="text-[#7070a0]">
                賣<span className="font-mono text-[#ff1744] ml-1">{optQuote?.ask || "—"}</span>
              </span>
              <button
                type="button"
                onClick={() => {
                  if (!optQuote) return;
                  const v =
                    action === "Buy"
                      ? optQuote.ask || optQuote.close
                      : optQuote.bid || optQuote.close;
                  if (v) setLimitPrice(String(v));
                }}
                disabled={!optQuote}
                className="ml-auto px-2 py-1 rounded border border-[#3b82f660] text-[#93c5fd] hover:bg-[#3b82f618] disabled:opacity-40"
              >
                帶入限價
              </button>
            </div>
          )}
        </div>
      )}

      {/* 買 / 賣 */}
      <div className="flex gap-2 mb-4">
        {(["Buy", "Sell"] as Action[]).map((a) => {
          const active = action === a;
          const color = a === "Buy" ? "#00e676" : "#ff1744";
          const label = isOption
            ? a === "Buy"
              ? "買進（買方）"
              : "賣出（賣方）"
            : a === "Buy"
            ? "買多"
            : "賣空";
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
              {label}
            </button>
          );
        })}
      </div>

      {/* 欄位 */}
      <div className="grid grid-cols-2 gap-3 mb-4">
        <div>
          <label className="text-[11px] text-[#7070a0] block mb-1">口數</label>
          <input
            type="number"
            min={1}
            value={quantity}
            onChange={(e) => setQuantity(Number(e.target.value))}
            className={monoCls}
          />
        </div>

        {/* 期貨：價格類型；選擇權：一律限價，這格放委託類型 */}
        {!isOption ? (
          <div>
            <label className="text-[11px] text-[#7070a0] block mb-1">價格類型</label>
            <select
              value={priceType}
              onChange={(e) => {
                const pt = e.target.value as PriceType;
                setPriceType(pt);
                if (pt === "LMT") setOrderType("ROD");
                if (pt === "MKT") setOrderType("IOC");
              }}
              className={inputCls}
            >
              <option value="MKT">市價</option>
              <option value="LMT">限價</option>
            </select>
          </div>
        ) : (
          <div>
            <label className="text-[11px] text-[#7070a0] block mb-1">委託類型</label>
            <select
              value={orderType}
              onChange={(e) => setOrderType(e.target.value)}
              className={inputCls}
            >
              <option value="ROD">ROD（當日有效）</option>
              <option value="IOC">IOC（立即成交）</option>
              <option value="FOK">FOK（全部成交）</option>
            </select>
          </div>
        )}

        {/* 限價 / 權利金限價 */}
        {(isOption || priceType === "LMT") && (
          <div className="col-span-2">
            <label className="text-[11px] text-[#7070a0] block mb-1">
              {isOption ? "權利金限價（必填）" : "限價（點）"}
            </label>
            <input
              type="number"
              value={limitPrice}
              onChange={(e) => setLimitPrice(e.target.value)}
              placeholder={isOption ? "例：120" : "例：21500"}
              className={monoCls}
            />
          </div>
        )}

        {/* 期貨委託類型（選擇權已移到上面那格） */}
        {!isOption && (
          <div>
            <label className="text-[11px] text-[#7070a0] block mb-1">委託類型</label>
            <select
              value={orderType}
              onChange={(e) => setOrderType(e.target.value)}
              className={inputCls}
            >
              <option value="IOC">IOC（立即成交）</option>
              <option value="ROD">ROD（當日有效）</option>
              <option value="FOK">FOK（全部成交）</option>
            </select>
          </div>
        )}

        {/* 選擇權平倉讓價 */}
        {isOption && (
          <div>
            <label className="text-[11px] text-[#7070a0] block mb-1">平倉讓價（點）</label>
            <input
              type="number"
              min={0}
              value={exitBuffer}
              onChange={(e) => setExitBuffer(e.target.value)}
              placeholder="例：3"
              className={monoCls}
            />
          </div>
        )}

        <div>
          <label className="text-[11px] text-[#ff1744] block mb-1">
            停損點數{isOption ? "（權利金）" : ""}（留空=停用）
          </label>
          <input
            type="number"
            min={0}
            value={stopLoss}
            onChange={(e) => setStopLoss(e.target.value)}
            placeholder="例：30"
            className="w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-3 py-2 font-mono text-sm text-[#e0e0f0] focus:outline-none focus:border-[#ff1744]"
          />
        </div>
        <div>
          <label className="text-[11px] text-[#00e676] block mb-1">
            停利點數{isOption ? "（權利金）" : ""}（留空=停用）
          </label>
          <input
            type="number"
            min={0}
            value={takeProfit}
            onChange={(e) => setTakeProfit(e.target.value)}
            placeholder="例：50"
            className="w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-3 py-2 font-mono text-sm text-[#e0e0f0] focus:outline-none focus:border-[#00e676]"
          />
        </div>
      </div>

      {optErr && isOption && (
        <div className="mb-3 text-xs px-3 py-2 rounded border bg-[#ff1744]/10 text-[#ff1744] border-[#ff1744]/20">
          選擇權合約載入失敗：{optErr}
        </div>
      )}

      {/* 送出 */}
      <button
        onClick={isOption ? submitOption : submitFuture}
        disabled={
          busy ||
          (!isOption && priceType === "LMT" && !limitPrice) ||
          (isOption && (!deliveryMonth || strike === "" || !limitPrice))
        }
        className="w-full py-3 rounded-lg font-semibold text-sm transition-colors disabled:opacity-40"
        style={{
          backgroundColor: isBuy ? "#00e676" : "#ff1744",
          color: isBuy ? "#0d0d14" : "#fff",
        }}
      >
        {busy
          ? "送出中..."
          : isOption
          ? `確認${isBuy ? "買進" : "賣出"} ${fmtMonth(deliveryMonth)} ${strike}${
              right === "C" ? "C" : "P"
            } ${quantity}口 @ ${limitPrice || "?"}`
          : `確認${isBuy ? "買多" : "賣空"} ${quantity} 口${
              priceType === "LMT" ? ` @ ${limitPrice}` : "（市價）"
            }`}
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
