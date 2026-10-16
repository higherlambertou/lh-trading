"use client";

// 走勢圖（update.md 發現 42）：微台 1 分 K（後端每分鐘記錄的真實成交）＋成交量，可疊均線、VWAP、場次水位線、你的持倉／停損停利線。
// 只讀：資料來自 /api/market/bars（本機資料庫與記憶體）、/api/position/、/api/order/watches，不下單。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { IChartApi, IPriceLine, ISeriesApi, UTCTimestamp } from "lightweight-charts";
import { api } from "@/lib/api";
import {
  aggregate, DEFAULT_TOGGLES, INTERVALS, mergeBars, parseToggles, positionLines, sameBars, sessionLevels, sma, TZ_OFFSET, vwap,
  type Bar, type Interval, type Toggles,
} from "@/lib/chart";
import type { Position, Watch } from "@/lib/api";

const STORE_KEY = "lh_chart_prefs";
const POLL_MS = 3000;
const FIRST_MINUTES = 1500;          // 第一次往前抓約 25 小時；之後只抓增量
const VISIBLE_BARS = 120;

const UP = "#ff5252";                // 與報價列一致：漲＝紅、跌＝綠
const DOWN = "#00e676";

const TOGGLE_LABEL: { key: keyof Toggles; label: string; color: string }[] = [
  { key: "ma5", label: "MA5", color: "#ffc107" },
  { key: "ma20", label: "MA20", color: "#4fc3f7" },
  { key: "ma60", label: "MA60", color: "#ba68c8" },
  { key: "vwap", label: "VWAP", color: "#ffffff" },
  { key: "levels", label: "開高低昨收", color: "#9090c0" },
  { key: "position", label: "持倉／停損", color: "#ff9800" },
];

function fmt(n: number | null | undefined): string {
  return n == null ? "—" : n.toLocaleString("zh-TW", { maximumFractionDigits: 1 });
}

function hhmm(t: number): string {
  const d = new Date((t + TZ_OFFSET) * 1000);
  return `${String(d.getUTCMonth() + 1).padStart(2, "0")}/${String(d.getUTCDate()).padStart(2, "0")} ${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
}

interface Refs {
  chart: IChartApi | null;
  candle: ISeriesApi<"Candlestick"> | null;
  volume: ISeriesApi<"Histogram"> | null;
  ma5: ISeriesApi<"Line"> | null;
  ma20: ISeriesApi<"Line"> | null;
  ma60: ISeriesApi<"Line"> | null;
  vwap: ISeriesApi<"Line"> | null;
}

export default function PriceChart({ active = true }: { active?: boolean }) {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const refs = useRef<Refs>({ chart: null, candle: null, volume: null, ma5: null, ma20: null, ma60: null, vwap: null });
  const lines = useRef<IPriceLine[]>([]);
  const drawn = useRef<{ key: string; n: number }>({ key: "", n: 0 });
  const [ready, setReady] = useState(false);
  const [bars, setBars] = useState<Bar[]>([]);
  const [interval, setIntervalMin] = useState<Interval>(1);
  const [toggles, setToggles] = useState<Toggles>({ ...DEFAULT_TOGGLES });
  const [positions, setPositions] = useState<Position[]>([]);
  const [watches, setWatches] = useState<Watch[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [legend, setLegend] = useState<string>("");
  const lastT = useRef<number>(0);

  // 讀回上次的設定（隱私模式等沒有 localStorage 就用預設）
  useEffect(() => {
    try {
      const p = parseToggles(localStorage.getItem(STORE_KEY));
      setIntervalMin(p.interval);
      setToggles(p.toggles);
    } catch { /* 沒有 localStorage：用預設 */ }
  }, []);

  const save = useCallback((i: Interval, t: Toggles) => {
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ interval: i, toggles: t })); } catch { /* 忽略 */ }
  }, []);

  // 建圖（一次）。lightweight-charts 用動態載入，避免伺服器端渲染時碰到 window。
  useEffect(() => {
    let disposed = false;
    (async () => {
      const lc = await import("lightweight-charts");
      if (disposed || !boxRef.current) return;
      const chart = lc.createChart(boxRef.current, {
        autoSize: true,
        layout: { background: { type: lc.ColorType.Solid, color: "#141420" }, textColor: "#7070a0", fontSize: 11 },
        grid: { vertLines: { color: "#1e1e3a" }, horzLines: { color: "#1e1e3a" } },
        crosshair: { mode: lc.CrosshairMode.Normal },
        rightPriceScale: { borderColor: "#1e1e3a" },
        timeScale: { borderColor: "#1e1e3a", timeVisible: true, secondsVisible: false },
      });
      const candle = chart.addSeries(lc.CandlestickSeries, {
        upColor: UP, downColor: DOWN, borderUpColor: UP, borderDownColor: DOWN, wickUpColor: UP, wickDownColor: DOWN,
        priceFormat: { type: "price", precision: 0, minMove: 1 },       // 台指期的價格是整數點
      });
      candle.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.24 } });
      const volume = chart.addSeries(lc.HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
      chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      const line = (color: string, width: 1 | 2 = 1) =>
        chart.addSeries(lc.LineSeries, { color, lineWidth: width, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      refs.current = {
        chart, candle, volume,
        ma5: line("#ffc107"), ma20: line("#4fc3f7"), ma60: line("#ba68c8"), vwap: line("#ffffff", 1),
      };
      chart.subscribeCrosshairMove((p) => {
        const d = p.seriesData.get(candle) as { open: number; high: number; low: number; close: number } | undefined;
        if (!p.time || !d) { setLegend(""); return; }
        const t = (p.time as number) - TZ_OFFSET;
        setLegend(`${hhmm(t)}  開 ${fmt(d.open)}  高 ${fmt(d.high)}  低 ${fmt(d.low)}  收 ${fmt(d.close)}`);
      });
      setReady(true);
    })();
    return () => {
      disposed = true;
      try { refs.current.chart?.remove(); } catch { /* 已經移除 */ }
      refs.current = { chart: null, candle: null, volume: null, ma5: null, ma20: null, ma60: null, vwap: null };
      lines.current = [];
      drawn.current = { key: "", n: 0 };
      setReady(false);
    };
  }, []);

  // 抓資料：第一次抓全部、之後只抓增量；頁面沒顯示時暫停
  useEffect(() => {
    if (!active) return;
    let stop = false;
    let first = lastT.current === 0;
    const tick = async () => {
      try {
        const q = first ? { minutes: FIRST_MINUTES } : { since: Math.max(0, lastT.current - 180) };
        const [r, pos, w] = await Promise.allSettled([api.market.bars(q), api.position.list(), api.order.watches()]);
        if (stop) return;
        if (r.status === "fulfilled") {
          first = false;
          setBars((prev) => {
            const m = mergeBars(prev, r.value.bars, r.value.live);
            lastT.current = m.length ? m[m.length - 1].t : lastT.current;
            return sameBars(prev, m) ? prev : m;
          });
          setErr(null);
          setUpdatedAt(Date.now());
        } else {
          setErr("連不上後端（K 棒暫停更新）");
        }
        if (pos.status === "fulfilled") setPositions(pos.value);
        if (w.status === "fulfilled") setWatches(w.value);
      } catch {
        if (!stop) setErr("連不上後端（K 棒暫停更新）");
      }
    };
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => { stop = true; clearInterval(id); };
  }, [active]);

  const agg = useMemo(() => aggregate(bars, interval), [bars, interval]);

  // 畫 K 棒、成交量、均線、VWAP。每次都整批 setData（最多約 1,500 根，很小）：
  // lightweight-charts 的 update() 只能改「最後一根」，但上一分鐘收完後數字可能和進行中的版本差一點，整批換最單純、不會漏。
  useEffect(() => {
    const r = refs.current;
    if (!ready || !r.candle || !r.volume || !r.chart) return;
    const time = (t: number) => (t + TZ_OFFSET) as UTCTimestamp;
    const ts = r.chart.timeScale();
    const key = `${interval}|${agg.length ? agg[0].t : 0}`;           // 換週期或換了起點才重設視野；開關均線不動使用者的縮放
    const range = ts.getVisibleLogicalRange();
    const atEnd = !range || drawn.current.n === 0 || range.to >= drawn.current.n - 1;   // 使用者本來就看著最新那一根才跟著走
    r.candle.setData(agg.map((b) => ({ time: time(b.t), open: b.o, high: b.h, low: b.l, close: b.c })));
    r.volume.setData(agg.map((b) => ({ time: time(b.t), value: b.v, color: b.c >= b.o ? `${UP}66` : `${DOWN}66` })));
    const closes = agg.map((b) => b.c);
    const series = (vals: (number | null)[], on: boolean) =>
      on ? vals.flatMap((v, i) => (v == null ? [] : [{ time: time(agg[i].t), value: v }])) : [];
    r.ma5?.setData(series(sma(closes, 5), toggles.ma5));
    r.ma20?.setData(series(sma(closes, 20), toggles.ma20));
    r.ma60?.setData(series(sma(closes, 60), toggles.ma60));
    r.vwap?.setData(series(vwap(agg), toggles.vwap));
    if (agg.length && (drawn.current.key !== key || drawn.current.n === 0)) {
      ts.setVisibleLogicalRange({ from: Math.max(0, agg.length - VISIBLE_BARS), to: agg.length + 4 });
    } else if (atEnd) {
      ts.scrollToRealTime();
    }
    drawn.current = { key, n: agg.length };
  }, [ready, agg, interval, toggles.ma5, toggles.ma20, toggles.ma60, toggles.vwap]);

  // 水位線與持倉／停損停利線
  useEffect(() => {
    const r = refs.current;
    if (!ready || !r.candle) return;
    for (const l of lines.current) { try { r.candle.removePriceLine(l); } catch { /* 已經不在 */ } }
    lines.current = [];
    const add = (price: number | null, title: string, color: string, dashed = true) => {
      if (price == null || !(price > 0) || !r.candle) return;
      lines.current.push(r.candle.createPriceLine({ price, color, lineWidth: 1, lineStyle: dashed ? 2 : 0, axisLabelVisible: true, title }));
    };
    if (toggles.levels && bars.length) {
      const lv = sessionLevels(bars);
      add(lv.prevClose, "昨收", "#7070a0");
      add(lv.open, "開", "#9090c0");
      add(lv.high, "高", "#ffc10799");
      add(lv.low, "低", "#ffc10799");
    }
    if (toggles.position) {
      for (const p of positionLines(positions, watches)) {
        add(p.price, p.title, p.kind === "entry" ? "#4fc3f7" : p.kind === "stop" ? "#ff1744" : "#00e676", p.kind !== "entry");
      }
    }
  }, [ready, bars, toggles.levels, toggles.position, positions, watches]);

  const setI = (i: Interval) => { setIntervalMin(i); save(i, toggles); };
  const flip = (k: keyof Toggles) => {
    const t = { ...toggles, [k]: !toggles[k] };
    setToggles(t);
    save(interval, t);
  };

  const last = agg.length ? agg[agg.length - 1] : null;
  const chip = "text-[11px] px-2 py-0.5 rounded border transition-colors";

  return (
    <div className="bg-[#141420] rounded-xl border border-[#1e1e3a] p-4">
      <div className="flex flex-wrap items-center gap-2 mb-2">
        <h2 className="text-sm font-semibold text-[#e0e0f0]">
          走勢圖 <span className="text-[11px] text-[#7070a0] font-normal">微台 · {interval} 分 K</span>
        </h2>
        {last && (
          <span className="font-mono text-sm tabular-nums" style={{ color: last.c >= last.o ? UP : DOWN }}>{fmt(last.c)}</span>
        )}
        <div className="flex gap-1 ml-auto flex-wrap" role="group" aria-label="K 棒週期">
          {INTERVALS.map((i) => (
            <button key={i} type="button" onClick={() => setI(i)} aria-pressed={interval === i}
              className={`${chip} ${interval === i ? "border-[#4fc3f7] text-[#4fc3f7] bg-[#4fc3f715]" : "border-[#1e1e3a] text-[#7070a0] hover:text-[#e0e0f0]"}`}>
              {i} 分
            </button>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap gap-1 mb-2" role="group" aria-label="圖上的線">
        {TOGGLE_LABEL.map((t) => (
          <button key={t.key} type="button" onClick={() => flip(t.key)} aria-pressed={toggles[t.key]}
            className={`${chip} ${toggles[t.key] ? "text-[#e0e0f0]" : "border-[#1e1e3a] text-[#505070]"}`}
            style={toggles[t.key] ? { borderColor: `${t.color}99`, background: `${t.color}18` } : undefined}>
            <span style={{ color: t.color }}>●</span> {t.label}
          </button>
        ))}
      </div>
      <div className="text-[11px] font-mono text-[#7070a0] mb-1 h-4 truncate" aria-live="off">{legend || (last ? `${hhmm(last.t)}  開 ${fmt(last.o)}  高 ${fmt(last.h)}  低 ${fmt(last.l)}  收 ${fmt(last.c)}  量 ${last.v}（外盤 ${last.b}／內盤 ${last.s}）` : "")}</div>
      <div className="relative">
        <div ref={boxRef} className="w-full h-[300px] md:h-[420px]" />
        {ready && bars.length === 0 && (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-[#7070a0] pointer-events-none">
            {err ? err : "還沒有 K 棒資料（後端每分鐘記錄真實成交，剛啟動要等一分鐘）"}
          </div>
        )}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 text-[10px] text-[#505070]">
        <span>資料：微台真實成交的 1 分 K，後端沒開著的分鐘沒有資料（K 棒會有缺口）。VWAP 是用 1 分 K 估的，不是交易所的值。只顯示、不下單。</span>
        {err && <span className="text-[#ffc107]">⚠ {err}</span>}
        {updatedAt && !err && <span>更新 {new Date(updatedAt).toLocaleTimeString("zh-TW", { hour12: false })}</span>}
      </div>
    </div>
  );
}
