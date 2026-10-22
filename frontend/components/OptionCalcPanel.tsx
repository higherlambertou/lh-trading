"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import { Calculator } from "lucide-react";
import StrategyCompare from "./StrategyCompare";
import { api, apiErrorDetail } from "@/lib/api";
import { defaultSeriesKey, parseSeriesKey, seriesKey, type OptionSeries } from "@/lib/optionSeries";
import {
  DEFAULT_FORM, DEFAULT_SOURCE, EMPTY_ROW, KIND_HINT, KIND_TEXT, beText, buildCalcRequest, buildCloseRequest, defaultDelta, hedgeText,
  pct1, pnlColor, pts, rightText, rowsFromCalc, sigma, spts, stwd, twd,
  type CloseRow, type OptCalc, type OptChain, type OptClose, type OptCloseAction, type OptForm, type OptHedge, type OptKind, type OptScen, type OptSource,
} from "@/lib/options";

// 〈選擇權試算〉：買跨式／買勒式／買單向 Call、Put 的成本、損益兩平、Greeks、情境表，以及已買部位的「平倉試算」。
// 只試算、不下單；資料是即時鏈或手動模型（後端 api/routes_options.py）。結果檢視（CalcResultView／CloseResultView）沒有 hooks，測試直接渲染。

const INPUT =
  "w-full bg-[#0d0d14] border border-[#1e1e3a] rounded px-2 py-1 text-xs font-mono text-[#e0e0f0] focus:outline-none focus:border-[#3b82f6] disabled:opacity-40";
const CARD = "bg-[#0d0d14] rounded-lg border border-[#1e1e3a] p-3 space-y-1.5";
const TITLE = "text-[10px] text-[#7070a0] uppercase tracking-widest";
const MUTED = "#7070a0", YELLOW = "#ffc107";

function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return (
    <label className="block">
      <span className="text-[10px] text-[#7070a0] block mb-0.5">{label}</span>
      {children}
      {hint && <span className="text-[10px] text-[#404060] block mt-0.5 leading-snug">{hint}</span>}
    </label>
  );
}

function Seg<T extends string>({ value, options, onChange }: { value: T; options: { id: T; label: string; title?: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="flex rounded border border-[#1e1e3a] overflow-hidden text-xs">
      {options.map((o) => (
        <button key={o.id} type="button" title={o.title} onClick={() => onChange(o.id)}
          className={`flex-1 px-2 py-1 whitespace-nowrap ${value === o.id ? "bg-[#1e1e3a] text-[#e0e0f0]" : "text-[#7070a0] hover:text-[#e0e0f0]"}`}>{o.label}</button>
      ))}
    </div>
  );
}

// 損益表：列＝價格變動（參考波動的倍數），欄＝時間或 IV 平移；格子是損益（元），顏色看正負
function ScenarioTable({ title, moves, cols, cells, note }: { title: string; moves: OptScen["moves"]; cols: string[]; cells: number[][]; note?: string }) {
  return (
    <div className="space-y-1">
      <div className="text-[11px] text-[#7070a0]">{title}</div>
      <div className="overflow-x-auto">
        <table className="w-full text-[11px] font-mono whitespace-nowrap">
          <thead>
            <tr className="text-[#7070a0]">
              <th className="font-normal text-left px-2 py-1">標的</th>
              {cols.map((c) => <th key={c} className="font-normal text-right px-2 py-1">{c}</th>)}
            </tr>
          </thead>
          <tbody>
            {moves.map((m, i) => (
              <tr key={m.mult} className={m.mult === 0 ? "bg-[#141420]" : ""}>
                <td className="px-2 py-0.5 text-[#7070a0]">{m.mult === 0 ? "不動" : `${spts(m.pts)} 點`}<span className="text-[#404060]"> {m.mult === 0 ? "" : `${m.mult > 0 ? "+" : "−"}${Math.abs(m.mult)}σ`}</span></td>
                {cells[i].map((v, j) => <td key={j} className="px-2 py-0.5 text-right" style={{ color: pnlColor(v) }}>{stwd(v)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {note && <div className="text-[10px] text-[#404060] leading-snug">{note}</div>}
    </div>
  );
}

function Scenarios({ sc, refLabel }: { sc: OptScen; refLabel: string }) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <ScenarioTable title={`損益（元）：價格 × 時間，IV 不變`} moves={sc.moves} cols={sc.time_cols.map((c) => c.label)} cells={sc.pnl_twd}
        note={`列＝標的相對現在的變動（${refLabel}）；用各履約價自己的 IV 重新估價（理論價，未扣出場價差）。`} />
      <ScenarioTable title={`損益（元）：價格 × IV 平移（${pts(sc.iv.after_days * 24, 0)} 小時後）`} moves={sc.moves} cols={sc.iv.shocks.map((s) => `IV ${s > 0 ? "+" : s < 0 ? "−" : ""}${Math.abs(s)}`)} cells={sc.iv.pnl_twd}
        note="IV 平移的單位是波動率點數（例如 20% → 17.5% 是 −2.5）。事件後 IV 常常下降（Vega 壓縮），方向對了也可能賺不到。" />
    </div>
  );
}

function Chip({ text, color = MUTED }: { text: string; color?: string }) {
  return <span className="text-[10px] px-1.5 py-0.5 rounded border font-mono whitespace-nowrap" style={{ color, borderColor: `${color}40`, background: `${color}15` }}>{text}</span>;
}

// 歷史模擬的結論（update.md 發現 30 C；數字改動時 tests/frontend/options_check.js 也要一起改）
const HEDGE_HISTORY = "歷史模擬（2020-04～2026-08，77 個月的 ATM 跨式，用台指期 1 分 K 當微台）：對沖讓每月報酬率的標準差從 73% 降到 26～29%，但平均是賠的——"
  + "每天對沖 −7%、每小時或門檻對沖 −10～11%（後兩者在 4 個檢定的校正後仍顯著）。主因是隱含波動平均高於實際波動，再加對沖成本。"
  + "對沖讓結果變穩，不會讓買跨式變成賺錢；只有實際波動比隱含大才賺（update.md 發現 30）。";

// 用微台對沖 delta（試算，不下單）：現在要買賣幾口微台、對沖後每個交易日要動多少才打平時間價值、最近實際動多少
export function HedgeCard({ h }: { h: OptHedge }) {
  return (
    <div className={CARD}>
      <div className={TITLE}>用微台對沖 Delta（試算，不下單）</div>
      <div className="text-[11px] font-mono text-[#e0e0f0]">{hedgeText(h)}</div>
      {h.be_rms_pts != null ? (
        <div className="text-[11px] font-mono text-[#7070a0]">
          對沖後每個交易日的波動（均方根）要超過 <span className="text-[#e0e0f0]">{pts(h.be_rms_pts)} 點</span>才賺得回時間價值（市場隱含約 {pts(h.implied_rms_pts)} 點）
          {h.realized_rms_pts != null
            ? <>；最近 20 個交易日實際約 <span className="text-[#e0e0f0]">{pts(h.realized_rms_pts)} 點</span>（是打平點的 {h.ratio?.toFixed(2)} 倍）</>
            : "；沒有歷史收盤可比較"}
        </div>
      ) : <div className="text-[11px] text-[#404060]">沒有 Gamma／Theta 可算（太接近到期或沒有 IV）</div>}
      <div className="text-[10px] text-[#404060] leading-snug">
        {h.tmf_lots === 0
          ? `淨 Delta 接近 0，目前不用對沖；之後每買賣 1 口微台約付 ${twd(h.cost_pts * 10)}（微台買賣價差＋手續費與期交稅，假設每口 ${h.cost_pts} 點）。`
          : `每次對沖約付 ${twd(h.cost_per_hedge_twd)}（微台買賣價差＋手續費與期交稅，假設每口 ${h.cost_pts} 點）。`}
        只顯示、不下單；要對沖請自己用〈手動下單〉的微台下。
      </div>
      <div className="text-[10px] text-[#7070a0] leading-snug">{HEDGE_HISTORY}</div>
    </div>
  );
}

export function MetaLine({ r }: { r: { source: string; delivery_month: string | null; expiry: string | null; days: number; F: number | null; atm_iv?: number | null; iv?: number | null } }) {
  const iv = r.atm_iv ?? r.iv ?? null;
  return (
    <div className="flex items-center gap-x-2 gap-y-1 flex-wrap text-[11px] font-mono text-[#7070a0]">
      <Chip text={r.source === "live" ? "即時報價" : "手動模型"} color={r.source === "live" ? "#00e676" : YELLOW} />
      {r.delivery_month && <span>到期月 {r.delivery_month}{r.expiry ? `（${r.expiry.replace("T", " ")}）` : ""}</span>}
      <span>距到期 {r.days} 天</span>
      <span>遠期價 {pts(r.F, 1)}</span>
      {iv != null && <span>ATM IV {iv}%</span>}
    </div>
  );
}

export function CalcResultView({ r, onToClose }: { r: OptCalc; onToClose?: () => void }) {
  const em = r.expected_move;
  const refLabel = em.ref_source === "user" ? `1σ ＝ 你填的 ${pts(em.ref_pts)} 點` : `1σ ＝ 市場隱含 ${pts(em.sigma_pts)} 點`;
  const h = r.prob.hist;
  return (
    <div className="space-y-4">
      <MetaLine r={r} />
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
        <div className={CARD}>
          <div className={TITLE}>成本（買進、照賣價 ask）</div>
          <div className="font-mono text-2xl">{twd(r.cost.total_twd)}</div>
          <div className="text-[11px] text-[#7070a0] font-mono">{pts(r.cost.pts, 1)} 點 × {r.cost.lots} 組（每組 {twd(r.cost.per_lot_twd)}）</div>
          <div className="text-[11px] text-[#404060] font-mono">
            中價約 {pts(r.cost.mid_pts, 1)} 點；一進場就差 {pts(r.cost.slippage_pts, 1)} 點，整組平倉再付 {pts(r.cost.exit_spread_pts, 1)} 點價差
          </div>
          {r.cost.max_lots != null && <div className="text-[11px] text-[#7070a0] font-mono">預算 {twd(r.cost.budget_twd)} 最多買 {r.cost.max_lots} 組</div>}
        </div>
        <div className={CARD}>
          <div className={TITLE}>最大虧損（買方＝權利金）</div>
          <div className="font-mono text-2xl" style={{ color: "#ff1744" }}>−{twd(r.max_loss.twd)}</div>
          <div className="text-[11px] text-[#7070a0] font-mono">到期時標的剛好落在履約價（跨式）或兩翼之間（勒式）</div>
        </div>
        <div className={CARD}>
          <div className={TITLE}>到期損益兩平</div>
          {r.breakeven.length ? r.breakeven.map((b, i) => <div key={i} className="text-[11px] font-mono text-[#e0e0f0]">{beText(b)}</div>) : <div className="text-[11px] text-[#7070a0]">沒有</div>}
          <div className="text-[10px] text-[#404060] leading-snug">離現價多少點、%、以及是市場隱含 1σ 的幾倍</div>
        </div>
        <div className={CARD}>
          <div className={TITLE}>預期波動與機率</div>
          <div className="text-[11px] font-mono text-[#e0e0f0]">1σ ≈ {pts(em.sigma_pts)} 點；跨式價≈平均絕對移動 {pts(em.straddle_pts)} 點</div>
          <div className="text-[11px] font-mono text-[#7070a0]">到期獲利機率（模型，用 ATM IV）：{pct1(r.prob.lognormal, 0)}</div>
          {h ? (
            <div className="text-[11px] font-mono text-[#7070a0]">
              過去任意 {h.h_days} 個交易日（{pts(h.n)} 個窗口）越過損益兩平：{pct1(h.p, 0)}；漲跌幅中位數 {pts(h.abs_move_q["50"])} 點、90% 分位 {pts(h.abs_move_q["90"])} 點
            </div>
          ) : <div className="text-[11px] text-[#404060]">沒有歷史收盤可比較</div>}
          {em.ref_source === "user" && <div className="text-[11px] font-mono" style={{ color: YELLOW }}>情境表以你填的 {pts(em.ref_pts)} 點為 1σ</div>}
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-[11px] font-mono whitespace-nowrap">
          <thead>
            <tr className="text-[#7070a0] text-left">
              {["買進", "履約價", "口數", "買價", "賣價（進場）", "中價", "價差", "IV", "Delta"].map((h2) => <th key={h2} className="font-normal px-2 py-1">{h2}</th>)}
            </tr>
          </thead>
          <tbody>
            {r.legs.map((l, i) => (
              <tr key={i}>
                <td className="px-2 py-0.5">{rightText(l.right)}</td>
                <td className="px-2 py-0.5">{pts(l.strike)}</td>
                <td className="px-2 py-0.5">{l.qty}</td>
                <td className="px-2 py-0.5">{pts(l.bid, 1)}</td>
                <td className="px-2 py-0.5">{pts(l.entry, 1)}</td>
                <td className="px-2 py-0.5">{pts(l.mid, 1)}</td>
                <td className="px-2 py-0.5">{l.spread == null ? "—" : `${pts(l.spread, 1)}（${pct1(l.spread_pct, 1)}）`}</td>
                <td className="px-2 py-0.5">{l.iv == null ? "—" : pct1(l.iv, 1)}</td>
                <td className="px-2 py-0.5">{l.delta == null ? "—" : l.delta.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px] font-mono">
        <div><div className="text-[10px] text-[#7070a0]">Delta（標的每動 1 點）</div><span className="text-[#e0e0f0]">{r.greeks.delta.toFixed(3)}</span> <span className="text-[#404060]">≈ {stwd(r.greeks.delta_twd)} 元/點</span></div>
        <div><div className="text-[10px] text-[#7070a0]">Gamma</div><span className="text-[#e0e0f0]">{r.greeks.gamma}</span></div>
        <div><div className="text-[10px] text-[#7070a0]">Theta（每天，什麼都不動也會少）</div><span style={{ color: pnlColor(r.greeks.theta_day_twd) }}>{stwd(r.greeks.theta_day_twd)} 元</span> <span className="text-[#404060]">{spts(r.greeks.theta_day_pts, 1)} 點</span></div>
        <div><div className="text-[10px] text-[#7070a0]">Vega（IV 每 +1 個波動點）</div><span style={{ color: pnlColor(r.greeks.vega_twd) }}>{stwd(r.greeks.vega_twd)} 元</span> <span className="text-[#404060]">{spts(r.greeks.vega_pts, 1)} 點</span></div>
      </div>

      <HedgeCard h={r.hedge} />

      <Scenarios sc={r.scenarios} refLabel={refLabel} />

      {r.warnings.length > 0 && (
        <ul className="space-y-1">
          {r.warnings.map((w, i) => <li key={i} className="text-[11px] leading-snug" style={{ color: YELLOW }}>⚠ {w}</li>)}
        </ul>
      )}
      <div className="flex items-center gap-3 flex-wrap">
        {onToClose && <button type="button" onClick={onToClose} className="px-3 py-1 text-xs rounded border border-[#1e1e3a] text-[#e0e0f0] hover:border-[#3b82f6]">帶到平倉試算 →</button>}
        <span className="text-[10px] text-[#404060] leading-snug">
          全是模型與當下報價的推算，不是預測；理論結算以到期日標的價計，不含手續費與交易稅。只試算、不下單。
        </span>
      </div>
    </div>
  );
}

export function CloseResultView({ r, sel, onSelect }: { r: OptClose; sel: string; onSelect: (id: string) => void }) {
  const cur: OptCloseAction = r.actions.find((a) => a.id === sel) ?? r.actions[0];
  return (
    <div className="space-y-4">
      <MetaLine r={r} />
      <div className="overflow-x-auto">
        <table className="w-full text-[11px] font-mono whitespace-nowrap">
          <thead>
            <tr className="text-[#7070a0] text-left">
              {["買進", "履約價", "口數", "買進價", "目前買價", "目前賣價", "中價", "損益（中價）", "損益（以買價賣出）", ""].map((h2) => <th key={h2} className="font-normal px-2 py-1">{h2}</th>)}
            </tr>
          </thead>
          <tbody>
            {r.legs.map((l, i) => (
              <tr key={i}>
                <td className="px-2 py-0.5">{rightText(l.right)}</td>
                <td className="px-2 py-0.5">{pts(l.strike)}</td>
                <td className="px-2 py-0.5">{l.qty}</td>
                <td className="px-2 py-0.5">{pts(l.entry, 1)}</td>
                <td className="px-2 py-0.5">{pts(l.bid, 1)}</td>
                <td className="px-2 py-0.5">{pts(l.ask, 1)}</td>
                <td className="px-2 py-0.5">{pts(l.mid, 1)}</td>
                <td className="px-2 py-0.5" style={{ color: pnlColor(l.pnl_mid_pts) }}>{spts(l.pnl_mid_pts, 1)} 點</td>
                <td className="px-2 py-0.5" style={{ color: pnlColor(l.pnl_bid_pts) }}>{spts(l.pnl_bid_pts, 1)} 點</td>
                <td className="px-2 py-0.5">{l.role === "loser" ? <Chip text="虧損腳" color="#ff1744" /> : l.role === "winner" ? <Chip text="獲利腳" color="#00e676" /> : null}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-[11px] font-mono whitespace-nowrap">
          <thead>
            <tr className="text-[#7070a0] text-left">
              {["動作", "已實現損益（元）", "最大總虧損（元）", "留下的腳到期要值多少才回本", "目前值", "到期損益兩平"].map((h2) => <th key={h2} className="font-normal px-2 py-1">{h2}</th>)}
            </tr>
          </thead>
          <tbody>
            {r.actions.map((a) => (
              <tr key={a.id} onClick={() => onSelect(a.id)} className={`cursor-pointer ${a.id === cur.id ? "bg-[#1e1e3a]" : "hover:bg-[#141420]"}`}>
                <td className="px-2 py-1 text-[#e0e0f0]">{a.label}</td>
                <td className="px-2 py-1" style={{ color: pnlColor(a.realized_pts) }}>{stwd(a.realized_twd)}</td>
                <td className="px-2 py-1" style={{ color: pnlColor(a.max_loss_twd) }}>{stwd(a.max_loss_twd)}</td>
                <td className="px-2 py-1">{a.breakeven.length || a.id === "hold" ? `${pts(a.net_basis_pts, 1)} 點` : "—"}</td>
                <td className="px-2 py-1 text-[#7070a0]">{a.id === "all" ? "—" : `${pts(a.remaining_now_pts, 1)} 點${a.need_ratio != null ? `（要 ${a.need_ratio.toFixed(2)} 倍）` : ""}`}</td>
                <td className="px-2 py-1 text-[#e0e0f0] whitespace-normal min-w-[15rem]">
                  {a.breakeven.length ? a.breakeven.map((b, i) => <div key={i}>{beText(b)}</div>) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="text-[10px] text-[#404060] leading-snug">
        淨成本＝全部買進成本 − 已收回的錢；留下的腳到期要值這麼多才回本（平掉越早，剩下那邊的回本門檻越低，但失去的是另一邊的保護）。點選上面的列看那個動作的情境表。
      </div>

      <div className="space-y-2">
        <div className="text-xs text-[#e0e0f0]">{cur.label}</div>
        {cur.greeks && (
          <div className="text-[11px] font-mono text-[#7070a0]">
            留下部位：Delta {cur.greeks.delta.toFixed(3)}（{stwd(cur.greeks.delta_twd)} 元/點）· Theta <span style={{ color: pnlColor(cur.greeks.theta_day_twd) }}>{stwd(cur.greeks.theta_day_twd)} 元/天</span> · Vega {stwd(cur.greeks.vega_twd)} 元/IV 點
          </div>
        )}
        {cur.hedge && <div className="text-[11px] font-mono text-[#7070a0]">微台對沖：{hedgeText(cur.hedge)}</div>}
        {cur.scenarios
          ? <Scenarios sc={cur.scenarios} refLabel={`1σ ＝ ${pts(r.ref_pts)} 點`} />
          : <div className="text-[11px] text-[#7070a0]">全部平掉後損益就固定了：{stwd(cur.realized_twd)} 元（以目前買價賣出）。</div>}
      </div>

      {r.warnings.length > 0 && (
        <ul className="space-y-1">
          {r.warnings.map((w, i) => <li key={i} className="text-[11px] leading-snug" style={{ color: YELLOW }}>⚠ {w}</li>)}
        </ul>
      )}
    </div>
  );
}

export default function OptionCalcPanel() {
  const [mode, setMode] = useState<"calc" | "close">("calc");
  const [src, setSrc] = useState<OptSource>(DEFAULT_SOURCE);
  const [form, setForm] = useState<OptForm>(DEFAULT_FORM);
  const [series, setSeries] = useState<OptionSeries[]>([]);           // 月選＋週選（發現 70）
  const [ctx, setCtx] = useState<OptChain | null>(null);
  const [calc, setCalc] = useState<OptCalc | null>(null);
  const [calcErr, setCalcErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [rows, setRows] = useState<CloseRow[]>([{ ...EMPTY_ROW, right: "C" }, { ...EMPTY_ROW, right: "P" }]);
  const [closeMove, setCloseMove] = useState("");
  const [closeHold, setCloseHold] = useState("24");
  const [close, setClose] = useState<OptClose | null>(null);
  const [closeErr, setCloseErr] = useState("");
  const [sel, setSel] = useState("hold");

  const setS = (p: Partial<OptSource>) => setSrc((s) => ({ ...s, ...p }));
  const setF = (p: Partial<OptForm>) => setForm((f) => ({ ...f, ...p }));

  useEffect(() => {
    let alive = true;
    api.order.optionSeries().then((xs) => { if (alive) { setSeries(xs); setSrc((s) => (s.deliveryMonth ? s : { ...s, ...parseSeriesKey(defaultSeriesKey(xs)) })); } }).catch(() => {});
    return () => { alive = false; };
  }, []);

  // 選了到期月就順便看一眼這個月的鏈摘要（遠期價、ATM IV、市場隱含的預期波動）；一次券商查詢，5 秒內重複用快取
  useEffect(() => {
    if (src.manual || !src.deliveryMonth) { setCtx(null); return; }
    let alive = true;
    api.options.chain(src.deliveryMonth, src.category || "TXO").then((c) => { if (alive) setCtx(c); }).catch(() => { if (alive) setCtx(null); });
    return () => { alive = false; };
  }, [src.manual, src.deliveryMonth, src.category]);

  const runCalc = useCallback(async () => {
    const b = buildCalcRequest(form, src);
    if (!b.ok) { setCalcErr(b.error); return; }
    setBusy(true); setCalcErr("");
    try { setCalc(await api.options.calc(b.body)); } catch (e) { setCalcErr(apiErrorDetail(e)); } finally { setBusy(false); }
  }, [form, src]);

  const runClose = useCallback(async () => {
    const b = buildCloseRequest(rows, src, closeMove, closeHold);
    if (!b.ok) { setCloseErr(b.error); return; }
    setBusy(true); setCloseErr("");
    try {
      const r = await api.options.close(b.body);
      setClose(r);
      const loser = r.actions.find((a) => a.id.startsWith("close_") && r.legs[a.closed[0]]?.role === "loser");
      setSel(loser?.id ?? "hold");
    } catch (e) { setCloseErr(apiErrorDetail(e)); } finally { setBusy(false); }
  }, [rows, src, closeMove, closeHold]);

  const toClose = () => { if (calc) { setRows(rowsFromCalc(calc)); setClose(null); setCloseErr(""); setMode("close"); } };
  const setRow = (i: number, p: Partial<CloseRow>) => setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...p } : r)));

  const impliedHint = ctx ? `留空＝市場隱含 1σ ≈ ${pts(ctx.em_sigma_pts)} 點` : calc ? `留空＝市場隱含 1σ ≈ ${pts(calc.expected_move.sigma_pts)} 點` : "留空＝市場隱含的 1 個標準差";

  return (
    <div className="lg:col-span-2 bg-[#141420] rounded-xl border border-[#1e1e3a] p-5 space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <Calculator size={14} className="text-[#3b82f6]" />
        <h2 className="text-xs font-semibold text-[#7070a0] uppercase tracking-widest">選擇權試算（買方）</h2>
        <div className="w-48"><Seg value={mode} onChange={setMode} options={[{ id: "calc", label: "試算" }, { id: "close", label: "平倉試算" }]} /></div>
        <span className="text-[10px] text-[#404060]">只試算、不下單。買方的最大虧損就是權利金。</span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <Field label="資料來源">
          <Seg value={src.manual ? "manual" : "live"} onChange={(v) => setS({ manual: v === "manual" })}
            options={[{ id: "live", label: "即時報價" }, { id: "manual", label: "手動（休市也能用）" }]} />
        </Field>
        {!src.manual ? (
          <Field label="到期日（月選／週選）" hint={ctx ? `遠期 ${pts(ctx.F, 1)} · ATM IV ${ctx.atm_iv_pct == null ? "—" : pts(ctx.atm_iv_pct, 1)}% · 距到期 ${pts(ctx.days, 1)} 天` : undefined}>
            <select className={INPUT} value={seriesKey({ category: src.category || "TXO", delivery_month: src.deliveryMonth })}
              onChange={(e) => setS(parseSeriesKey(e.target.value))}>
              {series.length === 0 && <option value="">（載入中…）</option>}
              {series.map((s) => <option key={seriesKey(s)} value={seriesKey(s)}>{s.label}</option>)}
            </select>
          </Field>
        ) : (
          <>
            <Field label="遠期價（點）"><input className={INPUT} inputMode="decimal" placeholder="例：49300" value={src.mF} onChange={(e) => setS({ mF: e.target.value })} /></Field>
            <Field label="ATM IV（%）"><input className={INPUT} inputMode="decimal" placeholder="例：20.5" value={src.mIv} onChange={(e) => setS({ mIv: e.target.value })} /></Field>
            <Field label="距到期（天）"><input className={INPUT} inputMode="decimal" placeholder="例：9" value={src.mDays} onChange={(e) => setS({ mDays: e.target.value })} /></Field>
          </>
        )}
      </div>

      {mode === "calc" ? (
        <>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
            <div className="md:col-span-2">
              <Field label="結構" hint={KIND_HINT[form.kind]}>
                <Seg value={form.kind} onChange={(k: OptKind) => setF({ kind: k, delta: defaultDelta(k) })}
                  options={(["straddle", "strangle", "call", "put"] as OptKind[]).map((k) => ({ id: k, label: KIND_TEXT[k], title: KIND_HINT[k] }))} />
              </Field>
            </div>
            {form.kind !== "straddle" && (
              <>
                <Field label="履約價怎麼選">
                  <Seg value={form.select} onChange={(s) => setF({ select: s })} options={[{ id: "delta", label: "Delta" }, { id: "move", label: "預期波動點數" }]} />
                </Field>
                {form.select === "delta" ? (
                  <Field label={form.kind === "strangle" ? "兩翼的 |Delta|" : "目標 |Delta|"} hint="越小越價外、越便宜、越難回本；0.5 ≈ 平值">
                    <input className={INPUT} inputMode="decimal" value={form.delta} onChange={(e) => setF({ delta: e.target.value })} />
                  </Field>
                ) : (
                  <Field label="距離＝幾倍的預期波動" hint={form.kind === "strangle" ? "兩翼各放在 現價 ± 倍數 × 預期波動" : "倍數 > 0 價外、< 0 價內"}>
                    <input className={INPUT} inputMode="decimal" value={form.k} onChange={(e) => setF({ k: e.target.value })} />
                  </Field>
                )}
              </>
            )}
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Field label="預期波動（點，選填）" hint={impliedHint}>
              <input className={INPUT} inputMode="decimal" placeholder="例：800" value={form.movePts} onChange={(e) => setF({ movePts: e.target.value })} />
            </Field>
            <Field label="組數"><input className={INPUT} inputMode="numeric" value={form.lots} onChange={(e) => setF({ lots: e.target.value })} /></Field>
            <Field label="預算（元，選填）"><input className={INPUT} inputMode="numeric" placeholder="例：50000" value={form.budget} onChange={(e) => setF({ budget: e.target.value })} /></Field>
            <Field label="持有時間（小時）" hint="夜盤單向約 6；波段跨式看 1 天"><input className={INPUT} inputMode="decimal" value={form.holdHours} onChange={(e) => setF({ holdHours: e.target.value })} /></Field>
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <button type="button" disabled={busy} onClick={runCalc} className="px-4 py-1.5 text-xs rounded bg-[#3b82f6] text-white disabled:opacity-40">{busy ? "試算中…" : "試算"}</button>
            {calcErr && <span className="text-[11px]" style={{ color: "#ff5252" }}>{calcErr}</span>}
          </div>
          {calc && <CalcResultView r={calc} onToClose={toClose} />}
          <StrategyCompare form={form} src={src} />
        </>
      ) : (
        <>
          <div className="space-y-2">
            <div className="text-[11px] text-[#7070a0] leading-snug">填你已經買進的各腳（買進價、口數）。目前買價／賣價留空＝用即時鏈上同履約價的報價（找不到就用模型估）；休市時自己填。</div>
            <div className="space-y-1.5">
              {rows.map((r, i) => (
                <div key={i} className="grid grid-cols-3 md:grid-cols-7 gap-2 items-end">
                  <Field label="買賣權"><select className={INPUT} value={r.right} onChange={(e) => setRow(i, { right: e.target.value as "C" | "P" })}><option value="C">Call</option><option value="P">Put</option></select></Field>
                  <Field label="履約價"><input className={INPUT} inputMode="decimal" value={r.strike} onChange={(e) => setRow(i, { strike: e.target.value })} /></Field>
                  <Field label="口數"><input className={INPUT} inputMode="numeric" value={r.qty} onChange={(e) => setRow(i, { qty: e.target.value })} /></Field>
                  <Field label="買進價（點）"><input className={INPUT} inputMode="decimal" value={r.entry} onChange={(e) => setRow(i, { entry: e.target.value })} /></Field>
                  <Field label="目前買價"><input className={INPUT} inputMode="decimal" placeholder="選填" value={r.bid} onChange={(e) => setRow(i, { bid: e.target.value })} /></Field>
                  <Field label="目前賣價"><input className={INPUT} inputMode="decimal" placeholder="選填" value={r.ask} onChange={(e) => setRow(i, { ask: e.target.value })} /></Field>
                  <button type="button" disabled={rows.length <= 1} onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))} className="px-2 py-1 text-xs rounded border border-[#1e1e3a] text-[#7070a0] disabled:opacity-30">刪除</button>
                </div>
              ))}
            </div>
            <button type="button" disabled={rows.length >= 8} onClick={() => setRows((rs) => [...rs, { ...EMPTY_ROW }])} className="px-3 py-1 text-xs rounded border border-[#1e1e3a] text-[#7070a0] hover:text-[#e0e0f0] disabled:opacity-30">＋ 新增一腳</button>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Field label="預期波動（點，選填）" hint={impliedHint}><input className={INPUT} inputMode="decimal" placeholder="例：800" value={closeMove} onChange={(e) => setCloseMove(e.target.value)} /></Field>
            <Field label="情境表的持有時間（小時）"><input className={INPUT} inputMode="decimal" value={closeHold} onChange={(e) => setCloseHold(e.target.value)} /></Field>
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <button type="button" disabled={busy} onClick={runClose} className="px-4 py-1.5 text-xs rounded bg-[#3b82f6] text-white disabled:opacity-40">{busy ? "試算中…" : "試算平倉"}</button>
            {closeErr && <span className="text-[11px]" style={{ color: "#ff5252" }}>{closeErr}</span>}
          </div>
          {close && <CloseResultView r={close} sel={sel} onSelect={setSel} />}
        </>
      )}
    </div>
  );
}
