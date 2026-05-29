"use client";

import { useEffect, useRef, useState } from "react";
import { Wifi, WifiOff } from "lucide-react";

interface QuoteMsg {
  code: string;
  close: number;
  open: number;
  high: number;
  low: number;
  volume: number;
  total_volume: number;
  change_price: number;
  ts: number;
}

type QuoteMap = Record<string, QuoteMsg>;

const WS_URL =
  (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8002/api")
    .replace(/^http/, "ws")
    .replace(/\/api$/, "") + "/api/quote/ws";

function codeLabel(code: string): string | null {
  if (code.startsWith("TMF")) return "微台";
  if (code.startsWith("TXF")) return "台指";
  return null;
}

function fmt(n: number) {
  return n.toLocaleString("zh-TW", { minimumFractionDigits: 0 });
}

function tsToTime(ts: number) {
  if (!ts) return "--:--:--";
  return new Date(ts * 1000).toLocaleTimeString("zh-TW", { hour12: false });
}

const THROTTLE_MS = 300;

export default function QuoteBar() {
  const [quotes, setQuotes] = useState<QuoteMap>({});
  const [connected, setConnected] = useState(false);
  const [flash, setFlash] = useState<Record<string, "up" | "dn" | null>>({});

  // accumulated buffer — flushed to state every THROTTLE_MS
  const bufRef = useRef<QuoteMap>({});
  const prevClose = useRef<Record<string, number>>({});
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    // flush buffer → state at fixed interval
    const flushId = setInterval(() => {
      const buf = bufRef.current;
      if (Object.keys(buf).length === 0) return;
      bufRef.current = {};
      setQuotes((prev) => ({ ...prev, ...buf }));
    }, THROTTLE_MS);

    function connect() {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);

      ws.onmessage = (e) => {
        let msg: QuoteMsg & { ping?: number };
        try { msg = JSON.parse(e.data); } catch { return; }
        if ("ping" in msg) return;

        const code = msg.code;
        const prev = prevClose.current[code];
        const dir =
          prev === undefined ? null
          : msg.close > prev ? "up"
          : msg.close < prev ? "dn"
          : null;
        prevClose.current[code] = msg.close;

        // write to buffer (no state update here)
        bufRef.current[code] = msg;

        if (dir) {
          setFlash((f) => ({ ...f, [code]: dir }));
          setTimeout(() => setFlash((f) => ({ ...f, [code]: null })), 400);
        }
      };

      ws.onclose = () => {
        setConnected(false);
        retryRef.current = setTimeout(connect, 3000);
      };

      ws.onerror = () => ws.close();
    }

    connect();

    return () => {
      clearInterval(flushId);
      if (retryRef.current) clearTimeout(retryRef.current);
      wsRef.current?.close();
    };
  }, []);

  const codes = Object.keys(quotes).filter((c) => codeLabel(c) !== null);

  return (
    <div className="flex items-center gap-4">
      <span className={`flex items-center gap-1 text-[11px] ${connected ? "text-[#00e676]" : "text-[#ff5252]"}`}>
        {connected ? <Wifi size={11} /> : <WifiOff size={11} />}
        {connected ? "即時" : "斷線"}
      </span>

      {codes.length === 0 && (
        <span className="text-[11px] text-[#404060]">等待報價…</span>
      )}

      {codes.map((code) => {
        const q = quotes[code];
        const chg = q.change_price;
        const chgColor = chg > 0 ? "text-[#ff5252]" : chg < 0 ? "text-[#00e676]" : "text-[#7070a0]";
        const priceFlash = flash[code];
        const priceColor =
          priceFlash === "up" ? "text-[#ff5252]"
          : priceFlash === "dn" ? "text-[#00e676]"
          : "text-[#e0e0f0]";

        return (
          <div key={code} className="flex items-center gap-2 border-l border-[#1e1e3a] pl-4">
            <span className="text-[11px] text-[#7070a0]">{codeLabel(code) ?? code}</span>
            <span className={`font-mono text-sm font-semibold tabular-nums transition-colors duration-200 ${priceColor}`}>
              {fmt(q.close)}
            </span>
            <span className={`font-mono text-[11px] tabular-nums ${chgColor}`}>
              {chg > 0 ? "▲" : chg < 0 ? "▼" : "—"}
              {Math.abs(chg) > 0 ? fmt(Math.abs(chg)) : ""}
            </span>
            <span className="text-[10px] text-[#404060] tabular-nums">
              {tsToTime(q.ts)}
            </span>
          </div>
        );
      })}
    </div>
  );
}
