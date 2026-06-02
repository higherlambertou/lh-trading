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
      className={`
        ml-auto flex items-center gap-2 px-3 py-1 rounded-full text-xs font-semibold
        transition-colors border
        ${sim
          ? "bg-yellow-500/10 border-yellow-500/40 text-yellow-400 hover:bg-yellow-500/20"
          : "bg-blue-500/10 border-blue-500/40 text-blue-400 hover:bg-blue-500/20"
        }
      `}
    >
      <span className={`w-2 h-2 rounded-full ${sim ? "bg-yellow-400" : "bg-blue-400"}`} />
      {sim ? "模擬盤" : "正式盤"}
    </button>
  );
}
