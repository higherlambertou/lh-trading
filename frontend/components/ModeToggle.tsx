"use client";

import { useEffect, useState } from "react";
import { isSimMode, setSimMode } from "@/lib/api";

export default function ModeToggle() {
  const [sim, setSim] = useState(false);

  useEffect(() => {
    setSim(isSimMode());
  }, []);

  function toggle() {
    const next = !sim;
    setSimMode(next);
    setSim(next);
    // 重新載入頁面讓所有 API 請求都使用新的 base URL
    window.location.reload();
  }

  return (
    <button
      onClick={toggle}
      title={sim ? "點擊切換到正式盤（真錢）" : "⚠ 正式盤＝真實下單真錢交易，點擊切換到模擬盤"}
      className={`
        ml-auto flex items-center gap-2 px-3 py-1 rounded-full text-xs font-semibold
        transition-colors border
        ${sim
          ? "bg-yellow-500/10 border-yellow-500/40 text-yellow-400 hover:bg-yellow-500/20"
          : "bg-red-500/15 border-red-500/60 text-red-400 hover:bg-red-500/25 animate-pulse"
        }
      `}
    >
      <span className={`w-2 h-2 rounded-full ${sim ? "bg-yellow-400" : "bg-red-500"}`} />
      {sim ? "模擬盤" : "正式盤・真錢"}
    </button>
  );
}
