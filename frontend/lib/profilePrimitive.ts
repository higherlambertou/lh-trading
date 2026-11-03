// 走勢圖右側的價量分布（update.md 發現 77）：lightweight-charts 的 series primitive，直接畫在圖裡、用圖自己的價格軸換算位置，
// 所以縮放、拖曳、價格軸改變時都會跟著對齊（不用自己同步座標）。畫的東西：價值區淡底、每個價位的量（外盤紅／內盤綠，價值區濃）、
// POC 那一格的外框，以及前一個日盤的 POC／價值區上下緣與現在 POC 的價位線與標籤（標籤放在價量分布的左邊、最新一根 K 棒的右邊：不蓋住 K 棒、不蓋住直條，也不擠價格軸；
// 放不下（窄螢幕）就先縮成只有名稱、再不夠就不放，數字看上方說明列）。
// 只畫圖、不碰資料來源與下單。
import type { IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, MismatchDirection, SeriesAttachedParameter, Time } from "lightweight-charts";
import {
  BAR_COLORS, LABEL_GAP, MAX_FRAC, MAX_PX, POC_COLOR,
  labelMode, layoutLabels, levelLines, niceStep, priceText, profileBars, rebin, type LevelLine, type ProfileData, type Rect,
} from "@/lib/volumeProfile";

const FONT = "11px -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang TC', sans-serif";
const LABEL_H = 15;

interface Geometry {
  maxW: number;                          // 最寬的格子（像素）
  right: number;                         // 圖的右緣（像素）
  bars: Rect[];
  band: { top: number; height: number } | null;        // 價值區（價量分布區域裡的淡底）
  lines: { y: number; line: LevelLine }[];             // 在畫面裡的價位線
}

export class ProfilePrimitive implements ISeriesPrimitive<Time> {
  private series: ISeriesApi<"Candlestick"> | null = null;
  private chart: IChartApi | null = null;
  private requestUpdate: (() => void) | null = null;
  private data: ProfileData | null = null;
  private readonly views: readonly IPrimitivePaneView[];

  constructor() {
    const bars: IPrimitivePaneRenderer = {
      draw: (target) => target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        const g = this.geometry(mediaSize.width, mediaSize.height);
        if (!g) return;
        if (g.band) {
          ctx.fillStyle = "rgba(127, 214, 245, 0.07)";
          ctx.fillRect(g.right - g.maxW, g.band.top, g.maxW, g.band.height);
        }
        for (const b of g.bars) {
          const h = b.height >= 9 ? b.height - 1 : b.height;          // 格子夠高（9 像素以上）才留 1 像素縫，矮的連在一起才看得出輪廓
          let x = g.right;
          const c = b.inValue ? BAR_COLORS.in : BAR_COLORS.out;          // 價值區濃一點、其餘淡一點（實心色，格線不會透出來）
          for (const [w, color] of [[b.sell, c.sell], [b.buy, c.buy]] as const) {
            if (w <= 0) continue;
            x -= w;
            ctx.fillStyle = color;
            ctx.fillRect(x, b.top, w, h);
          }
          if (b.poc) {                                                  // 最長的那一格加外框
            ctx.strokeStyle = POC_COLOR;
            ctx.lineWidth = 1;
            ctx.strokeRect(g.right - b.width + 0.5, b.top + 0.5, Math.max(b.width - 1, 1), Math.max(h - 1, 1));
          }
        }
        ctx.lineWidth = 1;
        for (const { y, line } of g.lines) {
          ctx.setLineDash(line.style === 2 ? [5, 4] : []);
          ctx.strokeStyle = line.color;
          ctx.globalAlpha = 0.55;
          ctx.beginPath();
          ctx.moveTo(0, Math.round(y) + 0.5);
          ctx.lineTo(g.right, Math.round(y) + 0.5);
          ctx.stroke();
        }
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
      }),
    };
    const labels: IPrimitivePaneRenderer = {
      draw: (target) => target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        const g = this.geometry(mediaSize.width, mediaSize.height);
        if (!g || !g.lines.length) return;
        ctx.font = FONT;
        ctx.textBaseline = "middle";
        ctx.textAlign = "left";
        const full = g.lines.map(({ line }) => `${line.title} ${priceText(line.price)}`);
        const short = g.lines.map(({ line }) => line.title);
        const wide = (ts: string[]) => Math.ceil(Math.max(...ts.map((t) => ctx.measureText(t).width))) + 10;
        const edge = g.right - g.maxW - LABEL_GAP;                              // 標籤的右緣：價量分布的左邊
        const lastX = this.lastBarX();
        const mode = labelMode(lastX == null ? Infinity : edge - (lastX + LABEL_GAP), wide(full), wide(short));
        if (mode === "none") return;
        const texts = mode === "full" ? full : short;
        const ys = layoutLabels(g.lines.map((l) => l.y), LABEL_H + 1, LABEL_H / 2, mediaSize.height - LABEL_H / 2);
        g.lines.forEach(({ line }, i) => {
          const w = Math.ceil(ctx.measureText(texts[i]).width) + 10;
          const x = edge - w, top = ys[i] - LABEL_H / 2;                        // 右緣對齊，各標籤寬度不同時排成整齊的一列
          ctx.globalAlpha = 1;
          ctx.fillStyle = "rgba(18, 18, 30, 0.88)";
          ctx.fillRect(x, top, w, LABEL_H);
          ctx.globalAlpha = 0.8;
          ctx.strokeStyle = line.color;
          ctx.lineWidth = 1;
          ctx.strokeRect(x + 0.5, top + 0.5, w - 1, LABEL_H - 1);
          ctx.globalAlpha = 1;
          ctx.fillStyle = line.color;
          ctx.fillText(texts[i], x + 5, ys[i] + 0.5);
        });
        ctx.globalAlpha = 1;
      }),
    };
    this.views = [{ zOrder: () => "bottom", renderer: () => bars }, { zOrder: () => "top", renderer: () => labels }];          // 直條與線畫在 K 棒下面、標籤畫在最上面
  }

  /** 最新一根 K 棒的 x（像素）；沒有資料或取不到回 null（標籤就不避讓）。 */
  private lastBarX(): number | null {
    const s = this.series, ts = this.chart?.timeScale();
    if (!s || !ts) return null;
    const last = s.dataByIndex(Number.MAX_SAFE_INTEGER, -1 as MismatchDirection);       // -1＝往左找最近的一根＝最後一根
    if (!last) return null;
    const x = ts.timeToCoordinate(last.time);
    return x != null && Number.isFinite(x) ? x : null;
  }

  /** 依目前的價格軸算出要畫的東西（兩個圖層共用；很便宜）。 */
  private geometry(width: number, height: number): Geometry | null {
    const p = this.data, s = this.series;
    if (!p || !s || !p.rows.length) return null;
    const y = (px: number) => s.priceToCoordinate(px);
    const y1 = y(p.rows[0].price), y2 = y(p.rows[0].price + 100);                  // 每點幾像素（價格軸是線性的，取哪一點都一樣）
    const rowBin = p.row_bin ?? p.pbin ?? 1;                                       // 舊版後端沒有 row_bin：它的列就是 pbin 點一格
    const coarse = niceStep(y1 != null && y2 != null ? Math.abs(y1 - y2) / 100 : 0);
    const merge = coarse % rowBin === 0;                                           // 只合併成原本格寬的整數倍（格子夠高時 coarse 小於 rowBin，不會是倍數）；對不上就照原本的格寬畫
    const maxW = Math.min(width * MAX_FRAC, MAX_PX);
    const rows = merge ? rebin(p.rows, coarse) : p.rows;
    let band: Geometry["band"] = null;
    if (p.vah != null && p.val != null) {
      const a = y(p.vah), b = y(p.val);
      if (a != null && b != null && Number.isFinite(a) && Number.isFinite(b)) band = { top: Math.min(a, b), height: Math.abs(b - a) };
    }
    return {
      maxW, right: width, band,
      bars: profileBars(rows, merge ? coarse : rowBin, y, maxW, p, height),
      lines: levelLines(p).flatMap((line) => { const yy = y(line.price); return yy != null && yy >= 0 && yy <= height ? [{ y: yy, line }] : []; }),
    };
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.series = param.series as ISeriesApi<"Candlestick">;
    this.chart = param.chart;
    this.requestUpdate = param.requestUpdate;
    this.requestUpdate();
  }

  detached(): void {
    this.series = null;
    this.chart = null;
    this.requestUpdate = null;
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }

  /** 換資料或關掉（null）；馬上要求重畫。 */
  setData(d: ProfileData | null): void {
    this.data = d;
    this.requestUpdate?.();
  }
}
