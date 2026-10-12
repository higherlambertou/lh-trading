"""當日沖銷（當沖）下單：期交所「當日沖銷交易保證金減半」的適用範圍與下單檢查。

規則（期交所〈保證金一覽表-股價指數類〉2026/08/12 與〈交易人常見問答-結算面〉）：只適用臺股期貨(TX)、電子期貨(TE)、金融期貨(TF)、小型臺指(MTX) 的最近 2 個到期月份契約，
且只限一般（日盤）交易時段、夜盤不適用；**微型臺指(TMF)不在內**。本系統能下的合約裡只有小台 MXF、大台 TXF 符合。
另外期貨商要求開戶滿 3 個月、近 1 年成交 10 筆以上（並可能要財力證明），以及收盤前約 15 分鐘要沖掉（沒沖掉會被強制沖銷）——這些由期貨商管，系統不替它判斷。

這裡只擋「一定沒有好處、或券商行為不明」的下單：TMF 標當沖、日盤以外標當沖。
手動下單可以勾當沖；策略（scalp 等）一律用 Auto，不支援。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional

TAIPEI = timezone(timedelta(hours=8))
DAYTRADE_CONTRACTS = ("MXF", "TXF")                # 期交所適用 MTX、TX；TMF 不適用
DAY_SESSION = (time(8, 45), time(13, 45))          # 一般（日盤）交易時段
OCTYPES = ("Auto", "New", "Cover", "DayTrade")     # shioaji FuturesOCType


def taipei_now() -> datetime:
    return datetime.now(TAIPEI)


def in_day_session(now: Optional[datetime] = None) -> bool:
    """現在是不是日盤（週一～五 08:45~13:45，台北時間；不查國定假日，假日券商會自己拒絕）。"""
    t = (now or taipei_now()).astimezone(TAIPEI)
    return t.weekday() < 5 and DAY_SESSION[0] <= t.time() < DAY_SESSION[1]


def check_octype(octype: str, contract: str, now: Optional[datetime] = None) -> Optional[str]:
    """手動下單的倉別檢查。回傳錯誤訊息；None = 可以下。"""
    if octype not in OCTYPES:
        return f"octype 必須是 {'、'.join(OCTYPES)} 其中之一"
    if octype != "DayTrade":
        return None
    if (contract or "").upper() not in DAYTRADE_CONTRACTS:
        return "當沖保證金減半只適用小台(MXF)與大台(TXF)；微台(TMF)不適用（期交所不給減半），請取消勾選當沖"
    if not in_day_session(now):
        return "當沖只能在日盤（週一～五 08:45~13:45）下單；夜盤不適用保證金減半，請取消勾選當沖"
    return None


def close_octype(octype: str, now: Optional[datetime] = None) -> str:
    """停損停利監控送平倉單用的倉別：當沖部位在日盤內用 DayTrade 平倉；日盤以外（理論上不會還有當沖部位）退回 Auto，
    免得平倉單被拒絕、停損送不出去。"""
    return "DayTrade" if octype == "DayTrade" and in_day_session(now) else "Auto"
