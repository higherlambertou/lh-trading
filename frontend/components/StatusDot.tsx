"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

// 連線狀態燈：每 5 秒打一次 /health
//   🟢 綠 = 正常（後端有回應且 broker 連著）
//   🟡 黃 = 後端活著但沒連上券商（登入中 / 斷線重連中）
//   🟠 橘 = 連線假死：後端活著、但券商那邊沒報價或查詢一直失敗，watchdog 會重啟（update.md 發現 65）
//   🔴 紅 = 後端沒回應（沒啟動 / 凍結 / 網路不通）
type State = "ok" | "nobroker" | "stale" | "down" | "loading";

const META: Record<State, { color: string; label: string }> = {
  ok: { color: "#22c55e", label: "連線正常" },
  nobroker: { color: "#eab308", label: "後端活著・券商未連上" },
  stale: { color: "#f97316", label: "券商連線假死・重啟中" },
  down: { color: "#ef4444", label: "後端無回應" },
  loading: { color: "#6b7280", label: "檢查中…" },
};

/** /health 的回覆 → 燈號（純函式，tests/frontend/status_check.js）。 */
export function healthState(h: { broker_connected?: string; session?: string }): State {
  if (h.broker_connected === "True") return "ok";
  return h.session === "stale" ? "stale" : "nobroker";
}

export default function StatusDot() {
  const [state, setState] = useState<State>("loading");
  const [reason, setReason] = useState<string>("");

  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const h = await api.health();
        if (!alive) return;
        setState(healthState(h));
        setReason(h.stale_reason ?? "");
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
      title={reason ? `${m.label}：${reason}` : m.label}
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
