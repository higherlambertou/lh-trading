import { TriangleAlert } from "lucide-react";
import { describeUnprotected, type Unprotected } from "@/lib/protection";

// 持倉沒有人看著停損時的橫幅（只顯示，不碰下單）：沒有策略在跑、也沒有手動停損監控。
// 後端重啟後手動停損會自動恢復（對帳後），策略不會——所以重啟後最常看到的是「策略的持倉」或「恢復不了的」。
export default function UnprotectedBanner({ items }: { items: Unprotected[] | null | undefined }) {
  if (!items || items.length === 0) return null;
  return (
    <div role="alert" className="mb-4 flex items-start gap-2 rounded-lg border border-[#ffc107]/30 bg-[#ffc107]/10 px-3 py-2 text-xs text-[#ffc107]">
      <TriangleAlert size={14} className="mt-0.5 shrink-0" />
      <div className="leading-snug">
        <span className="font-semibold">沒有停損保護：{describeUnprotected(items)}</span>
        <span className="text-[#ffc107]/80">。這些口數沒有策略在跑、也沒有停損監控覆蓋，價格不利時不會自動出場。請在〈委託〉面板設定停損，或手動平倉。</span>
      </div>
    </div>
  );
}
