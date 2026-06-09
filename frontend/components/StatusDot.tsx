"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

// 連線狀態燈：每 5 秒打一次 /health
//   🟢 綠 = 正常（後端有回應且 broker 連著）
//   🟡 黃 = 後端活著但沒連上券商（登入中 / 斷線重連中）
//   🔴 紅 = 後端沒回應（沒啟動 / 凍結 / 網路不通）
type State = "ok" | "nobroker" | "down" | "loading";

const META: Record<State, { color: string; label: string }> = {
  ok: { color: "#22c55e", label: "連線正常" },
  nobroker: { color: "#eab308", label: "後端活著・券商未連上" },
  down: { color: "#ef4444", label: "後端無回應" },
  loading: { color: "#6b7280", label: "檢查中…" },
};

export default function StatusDot() {
  const [state, setState] = useState<State>("loading");

  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const h = await api.health();
        if (!alive) return;
        setState(h.broker_connected === "True" ? "ok" : "nobroker");
      } catch {
        if (alive) setState("down");
      }
    };
    check();
    const id = setInterval(check, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const m = META[state];
  return (
    <span
      className="flex items-center gap-1.5 text-xs text-[#7070a0]"
      title={m.label}
    >
      <span
        className="inline-block w-2.5 h-2.5 rounded-full"
        style={{
          backgroundColor: m.color,
          boxShadow: `0 0 6px ${m.color}`,
        }}
      />
      <span className="hidden sm:inline">{m.label}</span>
    </span>
  );
}
