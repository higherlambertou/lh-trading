// 走勢圖右側的價量分布（update.md 發現 77）：lightweight-charts 的 series primitive，直接畫在圖裡、用圖自己的價格軸換算位置，
// 所以縮放、拖曳、價格軸改變時都會跟著對齊（不用自己同步座標）。只畫圖、不碰資料來源與下單。
import type { IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, SeriesAttachedParameter, Time } from "lightweight-charts";
import { BUY_COLOR, MAX_FRAC, MAX_PX, OTHER_COLOR, SELL_COLOR, profileBars, type ProfileData } from "@/lib/volumeProfile";

export class ProfilePrimitive implements ISeriesPrimitive<Time> {
  private series: ISeriesApi<"Candlestick"> | null = null;
  private requestUpdate: (() => void) | null = null;
  private data: ProfileData | null = null;
  private readonly views: readonly IPrimitivePaneView[];

  constructor() {
    const renderer: IPrimitivePaneRenderer = {
      draw: (target) => {
        const p = this.data, s = this.series;
        if (!p || !s || !p.rows.length) return;
        target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
          const bars = profileBars(p.rows, p.pbin, (px) => s.priceToCoordinate(px), Math.min(mediaSize.width * MAX_FRAC, MAX_PX), p);
          const right = mediaSize.width;
          for (const b of bars) {
            const h = b.height > 3 ? b.height - 1 : b.height;          // 格子夠高就留 1 像素縫
            let x = right;
            ctx.globalAlpha = b.inValue ? 0.55 : 0.28;                  // 價值區濃一點、其餘淡一點
            for (const [w, color] of [[b.sell, SELL_COLOR], [b.buy, BUY_COLOR], [b.other, OTHER_COLOR]] as const) {
              if (w <= 0) continue;
              x -= w;
              ctx.fillStyle = color;
              ctx.fillRect(x, b.top, w, h);
            }
          }
          ctx.globalAlpha = 1;
        });
      },
    };
    this.views = [{ zOrder: () => "bottom", renderer: () => renderer }];          // 畫在 K 棒下面
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.series = param.series as ISeriesApi<"Candlestick">;
    this.requestUpdate = param.requestUpdate;
    this.requestUpdate();
  }

  detached(): void {
    this.series = null;
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
