"use client";

import { useState, useEffect, useCallback, ReactNode } from "react";
import { Compass, RefreshCw } from "lucide-react";
import { api, MarketState, JournalRow, MarketStats } from "@/lib/api";

const COLOR: Record<string, string> = {
  TREND: "#00e676", REVERT: "#3b82f6", UNCLEAR: "#7070a0", IV_LOW: "#ffc107", IV_HIGH: "#ffc107",
  RANDOM: "#7070a0", UNCERTAIN: "#7070a0", NORMAL: "#00e676", LOW: "#ffc107", HIGH: "#ffc107",
  UNKNOWN: "#7070a0",
};
const STATE_TEXT: Record<string, string> = {
  TREND: "趨勢", REVERT: "均值回歸", UNCLEAR: "不明確", IV_LOW: "IV 偏低", IV_HIGH: "IV 偏高",
};
const PHASE_TEXT: Record<string, string> = { early: "預算", pre: "盤前判斷", manual: "手動重算" };
const INPUT =
  "bg-[#0d0d14] border border-[#1e1e3a] rounded px-2 py-1 text-[11px] font-mono text-[#e0e0f0] " +
  "focus:outline-none focus:border-[#3b82f6]";

function Chip({ code, text }: { code?: string | null; text: string }) {
  const c = COLOR[code ?? ""] ?? "#7070a0";
  return (
    <span className="text-[10px] px-1.5 py-0.5 rounded border font-mono whitespace-nowrap"
      style={{ color: c, borderColor: `${c}40`, background: `${c}15` }}>
      {text}
    </span>
  );
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="bg-[#0d0d14] rounded-lg border border-[#1e1e3a] p-3 space-y-1.5">
      <div className="text-[10px] text-[#7070a0] uppercase tracking-widest">{title}</div>
      {children}
    </div>
  );
}

const money = (n: number) => `${n >= 0 ? "+" : ""}${Math.round(n).toLocaleString()}`;
const pnlColor = (n: number) => (n > 0 ? "text-[#00e676]" : n < 0 ? "text-[#ff1744]" : "text-[#7070a0]");

export default function MarketStatePanel() {
  const [st, setSt] = useState<MarketState | null>(null);
  const [rows, setRows] = useState<JournalRow[]>([]);
  const [stats, setStats] = useState<MarketStats | null>(null);
  const [drafts, setDrafts] = useState<Record<string, { basis: string; notes: string }>>({});
  const [ivInput, setIvInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const loadState = useCallback(async () => {
    try { setSt(await api.market.state()); } catch { /* 輪詢靜默 */ }
  }, []);

  const loadJournal = useCallback(async () => {
    try {
      const [j, s] = await Promise.all([api.market.journal(14), api.market.stats("scalp")]);
      setRows(j);
      setStats(s);
    } catch { /* 輪詢靜默 */ }
  }, []);

  useEffect(() => {
    loadState();
    loadJournal();
    const a = setInterval(loadState, 15000);
    const b = setInterval(loadJournal, 60000);
    return () => { clearInterval(a); clearInterval(b); };
  }, [loadState, loadJournal]);

  const flash = (ok: boolean, text: string) => {
    setMsg({ ok, text });
    setTimeout(() => setMsg(null), 5000);
  };
  const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

  const handleRefresh = async () => {
    setBusy(true);
    try {
      setSt(await api.market.refresh());
      flash(true, "已重算今日市場狀態");
    } catch (e) {
      const text = errText(e);
      // 策略執行中：後端預設拒絕（查詢期間 worker 會排隊下單指令），確認後才強制
      if (text.startsWith("409") &&
          window.confirm("策略執行中：查詢期間券商 worker 會暫時排隊下單指令。仍要重算嗎？")) {
        try { setSt(await api.market.refresh(true)); flash(true, "已重算今日市場狀態"); }
        catch (e2) { flash(false, errText(e2)); }
      } else {
        flash(false, text);
      }
    } finally {
      setBusy(false);
      loadJournal();
    }
  };

  const handleIv = async () => {
    const v = Number(ivInput);
    if (!(v > 0 && v < 300)) { flash(false, "IV 需為 0~300 之間的數字（單位 %）"); return; }
    setBusy(true);
    try {
      setSt(await api.market.setIv(v));
      setIvInput("");
      flash(true, `已記錄 ATM IV ${v}%，今日狀態已重算`);
    } catch (e) {
      flash(false, errText(e));
    } finally {
      setBusy(false);
      loadJournal();
    }
  };

  const draftOf = (r: JournalRow) => drafts[r.date] ?? { basis: r.basis ?? "", notes: r.notes ?? "" };
  const setDraft = (r: JournalRow, patch: Partial<{ basis: string; notes: string }>) =>
    setDrafts((p) => ({ ...p, [r.date]: { ...draftOf(r), ...patch } }));
  const saveNote = async (r: JournalRow) => {
    const d = drafts[r.date];
    if (!d || (d.basis === (r.basis ?? "") && d.notes === (r.notes ?? ""))) return;
    try {
      await api.market.saveNote(r.date, d);
      await loadJournal();
      setDrafts((p) => { const n = { ...p }; delete n[r.date]; return n; });
    } catch (e) {
      flash(false, errText(e));
    }
  };

  const h = st?.hurst;
  const iv = st?.iv;
  const today = new Date().toLocaleDateString("sv");     // YYYY-MM-DD（本機時區）

  return (
    <div className="lg:col-span-2 bg-[#141420] rounded-xl border border-[#1e1e3a] p-5 space-y-4">
      <div className="flex items-center gap-2">
        <Compass size={14} className="text-[#3b82f6]" />
        <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest">市場狀態</h2>
        {st?.ready && (
          <span className="text-[11px] text-[#7070a0] font-mono">
            {st.date}
            {st.date !== today && <span className="text-[#ffc107]"> · 非今日</span>}
            {st.phase && ` · ${PHASE_TEXT[st.phase] ?? st.phase}`}
            {st.computed_at ? ` ${new Date(st.computed_at * 1000).toLocaleTimeString("zh-TW", { hour12: false })}` : ""}
          </span>
        )}
        <button
          onClick={handleRefresh}
          disabled={busy}
          title="向券商重抓日K與 ATM 選擇權報價並重算（盤中重算視為「重大事件」才用）"
          className="ml-auto flex items-center gap-1 px-2.5 py-1 text-[11px] rounded border border-[#1e1e3a] text-[#7070a0] hover:text-[#e0e0f0] hover:border-[#3b82f6]/50 transition-colors disabled:opacity-40"
        >
          <RefreshCw size={11} className={busy ? "animate-spin" : ""} /> 重算
        </button>
      </div>

      {msg && (
        <div className={`px-3 py-2 rounded text-xs ${
          msg.ok
            ? "bg-[#00e676]/10 text-[#00e676] border border-[#00e676]/20"
            : "bg-[#ff1744]/10 text-[#ff1744] border border-[#ff1744]/20"
        }`}>{msg.text}</div>
      )}

      {!st?.ready ? (
        <p className="text-[11px] text-[#404060]">
          尚未計算：券商連線後會自動計算（盤前 {String(st?.config.pre_hhmm ?? 830).padStart(4, "0")}），或按右上「重算」。
        </p>
      ) : (
        <>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <Card title="第一層 · Hurst">
              <div className="flex items-baseline gap-2">
                <span className="font-mono text-2xl">{h?.value != null ? h.value.toFixed(2) : "—"}</span>
                <Chip code={h?.state} text={h?.label ?? "—"} />
              </div>
              <div className="text-[11px] text-[#7070a0] font-mono">
                {h?.z != null ? `z=${h.z >= 0 ? "+" : ""}${h.z.toFixed(1)} · 雜訊 ±${h.se}` : "—"}
              </div>
              <div className="text-[11px] text-[#404060] font-mono">
                {h?.window} 根日K · 至 {h?.last_bar || "—"}
              </div>
            </Card>

            <Card title="第二層 · IV">
              <div className="flex items-baseline gap-2">
                <span className="font-mono text-2xl">
                  {iv?.percentile != null ? `${iv.percentile.toFixed(0)}%` : "—"}
                </span>
                <Chip code={iv?.state} text={iv?.label ?? "—"} />
              </div>
              <div className="text-[11px] text-[#7070a0] font-mono">
                {iv?.value != null
                  ? `ATM IV ${iv.value.toFixed(1)}%（${iv.source}${iv.as_of && iv.as_of !== st.date ? ` ${iv.as_of}` : ""}）`
                  : "尚無 IV"}
              </div>
              <div className="text-[11px] text-[#404060] font-mono">
                歷史 {iv?.history_n}/{iv?.min_history} 天
              </div>
            </Card>

            <Card title="今日判斷">
              <div className="flex items-center gap-2">
                <Chip code={st.state} text={STATE_TEXT[st.state ?? ""] ?? st.state ?? "—"} />
                <span className="text-[11px] text-[#7070a0]">{st.state_label}</span>
              </div>
              <div className="text-sm text-[#e0e0f0]">{st.hint}</div>
              <div className="text-[11px] text-[#404060] font-mono">
                方向 {st.direction_label}（scalp market_bias：1 順勢／-1 逆勢／2 依狀態自動）
              </div>
            </Card>
          </div>

          {(st.notes?.length ?? 0) > 0 && (
            <ul className="space-y-0.5">
              {st.notes!.map((n, i) => (
                <li key={i} className="text-[11px] text-[#ffc107] leading-tight">※ {n}</li>
              ))}
            </ul>
          )}
        </>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] text-[#7070a0]">手動輸入今日 ATM IV（%）</span>
        <input
          type="number" step="0.1" min={0} max={300} value={ivInput} placeholder="18.5"
          onChange={(e) => setIvInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") handleIv(); }}
          className={`${INPUT} w-20`}
        />
        <button
          onClick={handleIv} disabled={busy || !ivInput}
          className="px-2.5 py-1 text-[11px] rounded border border-[#3b82f6]/40 text-[#3b82f6] bg-[#3b82f6]/10 hover:bg-[#3b82f6]/20 transition-colors disabled:opacity-40"
        >送出</button>
        <span className="text-[10px] text-[#404060]">
          {st?.config.iv_auto ? "已啟用券商自動抓取；手動值優先" : "僅手動輸入"}
        </span>
      </div>

      {/* ── 每日日誌 ─────────────────────────────────────────── */}
      <div className="overflow-x-auto">
        <table className="w-full text-[11px] font-mono">
          <thead>
            <tr className="text-[#7070a0] text-left">
              {["日期", "Hurst", "IV%分位", "狀態", "scalp", "結果", "判斷依據", "備註"].map((t) => (
                <th key={t} className="font-normal px-1.5 py-1 whitespace-nowrap">{t}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={8} className="px-1.5 py-2 text-[#404060]">尚無日誌（每個交易日盤前自動產生）</td></tr>
            )}
            {rows.map((r) => (
              <tr key={r.date} className="border-t border-[#1e1e3a]">
                <td className="px-1.5 py-1 whitespace-nowrap text-[#a0a0c0]">{r.date.slice(5)}</td>
                <td className="px-1.5 py-1 whitespace-nowrap">
                  {r.hurst != null ? r.hurst.toFixed(2) : "—"}
                  {r.hurst_z != null && <span className="text-[#404060]"> z{r.hurst_z >= 0 ? "+" : ""}{r.hurst_z.toFixed(1)}</span>}
                </td>
                <td className="px-1.5 py-1 whitespace-nowrap">
                  {r.iv_pct != null ? `${r.iv_pct.toFixed(0)}%` : "—"}
                  {r.iv != null && <span className="text-[#404060]"> ({r.iv.toFixed(1)})</span>}
                </td>
                <td className="px-1.5 py-1">
                  {r.market_state ? <Chip code={r.market_state} text={STATE_TEXT[r.market_state] ?? r.market_state} /> : "—"}
                </td>
                <td className="px-1.5 py-1">{r.scalp_on ? "是" : "否"}</td>
                <td className="px-1.5 py-1 whitespace-nowrap">
                  <span className={pnlColor(r.pnl)}>{r.result}</span>
                  {r.trades > 0 && <span className={`${pnlColor(r.pnl)} ml-1`}>{money(r.pnl)}</span>}
                </td>
                {(["basis", "notes"] as const).map((k) => (
                  <td key={k} className="px-1.5 py-1">
                    <input
                      value={draftOf(r)[k]}
                      onChange={(e) => setDraft(r, { [k]: e.target.value })}
                      onBlur={() => saveNote(r)}
                      onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                      placeholder={k === "basis" ? "你看到了什麼" : "備註"}
                      className={`${INPUT} w-full min-w-[9rem]`}
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* ── 驗證統計 ─────────────────────────────────────────── */}
      {stats && (stats.by_state.length > 0 || stats.by_iv.length > 0) && (
        <details className="text-[11px]">
          <summary className="cursor-pointer text-[#7070a0]">驗證統計（{stats.strategy}）</summary>
          <div className="mt-2 grid grid-cols-1 md:grid-cols-2 gap-4 font-mono">
            <table className="w-full">
              <thead>
                <tr className="text-[#7070a0] text-left">
                  {["狀態", "天數", "勝/負", "勝率", "賺賠比", "總損益"].map((t) => (
                    <th key={t} className="font-normal px-1.5 py-1">{t}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {stats.by_state.map((g) => (
                  <tr key={g.state} className="border-t border-[#1e1e3a]">
                    <td className="px-1.5 py-1"><Chip code={g.state} text={STATE_TEXT[g.state] ?? g.state} /></td>
                    <td className="px-1.5 py-1">{g.days}</td>
                    <td className="px-1.5 py-1">{g.wins}/{g.losses}</td>
                    <td className="px-1.5 py-1">{g.win_rate != null ? `${(g.win_rate * 100).toFixed(0)}%` : "—"}</td>
                    <td className="px-1.5 py-1">{g.payoff != null ? g.payoff.toFixed(2) : "—"}</td>
                    <td className={`px-1.5 py-1 ${pnlColor(g.total_pnl)}`}>{money(g.total_pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <table className="w-full">
              <thead>
                <tr className="text-[#7070a0] text-left">
                  {["IV 狀態", "天數", `大波動(≥${stats.big_move}×)`, "比例", "平均振幅比"].map((t) => (
                    <th key={t} className="font-normal px-1.5 py-1">{t}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {stats.by_iv.map((g) => (
                  <tr key={g.iv_state} className="border-t border-[#1e1e3a]">
                    <td className="px-1.5 py-1"><Chip code={g.iv_state} text={g.iv_state} /></td>
                    <td className="px-1.5 py-1">{g.days}</td>
                    <td className="px-1.5 py-1">{g.big_move_days}</td>
                    <td className="px-1.5 py-1">{(g.big_move_rate * 100).toFixed(0)}%</td>
                    <td className="px-1.5 py-1">{g.avg_range_ratio.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  );
}
