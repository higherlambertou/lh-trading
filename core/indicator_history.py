"""市場指標視覺化工具的資料基礎：三個指標「合在一起的歷史」。

視覺化需求文件規定先做歷史回放、統計驗證，通過才能即時上線；驗證需要 Hurst、IV 百分位、外/內盤比例三者的歷史，
而這些原本都不存在（IV 只有每天一筆、外/內盤比例只活在記憶體）。這裡負責把歷史補起來：

  indicator_daily  每個交易日的 Hurst（日 K 60 根）、日 K 方向、IV 與 IV 百分位——由日 K 與 iv_history 重算，隨時可重建。
                   date ＝「用到這天收盤為止的資料」（as-of）；T 日盤前能用的是 date < T 的最後一列，回放時不可用到未來。
  flow_1m          每分鐘 TMF 外/內盤成交統計。即時由 core/flow_store.py 寫入；歷史有兩個來源：
                   ① 本機 ticks.db（已經記下來的逐筆，不用連券商） ② 券商歷史逐筆 api.ticks()（唯讀，模擬盤登入）。

IV 歷史補不回來（過期選擇權沒有歷史報價可反推），只能從每天的自動抓取慢慢累積；沒有 IV 的日子，IV 那層就是空的。

用法（只有 flow-fetch 會連券商；其餘只讀寫 data/market_state.db 與 ticks.db）：
  python -m core.indicator_history status
  python -m core.indicator_history daily                       # 重建 indicator_daily
  python -m core.indicator_history flow-from-ticks             # 把本機 ticks.db 的逐筆聚合進 flow_1m
  python -m core.indicator_history flow-fetch --probe [--date 2026-10-08]   # 向券商抓一天，看流量／耗時，並和本機 ticks.db 比對數字
  python -m core.indicator_history flow-fetch --days 60        # 補最近 60 天（有流量上限與保留額度保護）
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import numpy as np

from core.broker_guard import room_for_login
from core.flow_store import PREFIX, aggregate_trades
from core.hurst_analyzer import analyze, trend_direction
from core.hurst_study import CODE as BAR_CODE
from core.iv_monitor import iv_percentile, iv_signal
from core.market_store import MarketStore
from core.quote_hub import PREOPEN_WINDOWS
from core.tick_store import DB_PATH as TICKS_DB

logger = logging.getLogger(__name__)

HURST_WINDOW = 60
MIN_IV_HISTORY = 60              # 與 IV_MIN_HISTORY 預設一致：歷史不足就不給百分位
SLEEP_BETWEEN_CALLS = 3.0        # 券商行情查詢有頻率上限，一天一次呼叫、中間睡一下
BYTES_PER_TICK = 42              # 實測：128,629 筆 → 流量 +5.15 MB（約 40 位元組/筆），取保守一點


# ── 時間與聚合 ───────────────────────────────────────────────────

def naive_ns_to_epoch(ts_ns: np.ndarray) -> np.ndarray:
    """券商歷史資料的時間戳是「台灣當地時間當成 UTC 的 epoch（奈秒）」→ 真實 epoch 秒。
    偏移量用資料第一筆的日期決定（台灣沒有夏令時間，同一批資料偏移相同）。"""
    f = np.asarray(ts_ns, dtype=np.float64) / 1e9
    if f.size == 0:
        return f
    naive = datetime(1970, 1, 1) + timedelta(seconds=float(f[0]))
    return f + (naive.timestamp() - float(f[0]))


def preopen_mask(epoch_sec: np.ndarray) -> np.ndarray:
    """開盤前試算時段（與報價隔離用同一份定義）的遮罩。以分鐘為單位算，成千上萬筆也只要查幾千個分鐘。"""
    minutes = (np.asarray(epoch_sec, dtype=np.int64) // 60)
    uniq = np.unique(minutes)
    bad = set()
    for m in uniq.tolist():
        lt = time.localtime(m * 60)
        hhmm = lt.tm_hour * 100 + lt.tm_min
        if any(a <= hhmm < b for a, b in PREOPEN_WINDOWS):
            bad.add(m)
    return np.isin(minutes, np.fromiter(bad, dtype=np.int64)) if bad else np.zeros(minutes.shape, dtype=bool)


def broker_ticks_to_rows(ticks: Any) -> list[dict[str, Any]]:
    """api.ticks() 的結果 → 每分鐘一列（開盤前試算時段略過）。"""
    ts = naive_ns_to_epoch(np.asarray(ticks.ts, dtype=np.int64))
    if ts.size == 0:
        return []
    order = np.argsort(ts, kind="stable")
    ts = ts[order]
    price = np.asarray(ticks.close, dtype=float)[order]
    volume = np.asarray(ticks.volume, dtype=np.int64)[order]
    tick_type = np.asarray(ticks.tick_type, dtype=np.int64)[order]
    return aggregate_trades(ts, price, volume, tick_type, drop=preopen_mask(ts))


def ticks_db_to_rows(path: str | Path | None = None, prefix: str = PREFIX) -> list[dict[str, Any]]:
    """本機 ticks.db（已經是真實成交）→ 每分鐘一列。唯讀開啟，服務執行中也能讀。"""
    p = Path(path or TICKS_DB)
    with closing(sqlite3.connect(f"file:{quote(str(p))}?mode=ro", uri=True, timeout=30)) as c:
        rows = c.execute("SELECT ts, price, volume, tick_type FROM ticks WHERE volume > 0 AND code LIKE ? ORDER BY ts",
                         (prefix + "%",)).fetchall()
    if not rows:
        return []
    ts = np.array([r[0] for r in rows], dtype=float)
    return aggregate_trades(ts, [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], drop=preopen_mask(ts))


# ── 每日指標歷史 ─────────────────────────────────────────────────

def build_daily(store: MarketStore | None = None, window: int = HURST_WINDOW,
                min_iv_history: int = MIN_IV_HISTORY) -> list[dict[str, Any]]:
    """由日 K 與 IV 歷史重算每個交易日的指標並寫進 indicator_daily（可重複執行）。
    Hurst：日 K 近 window 根（現行做法）；日 K 方向：收盤 vs 20 日均線；IV 百分位：歷史不足 min_iv_history 筆就是 None。"""
    store = store or MarketStore()
    bars = store.bars(BAR_CODE, 10_000)
    dates = [b["date"] for b in bars]
    closes = [b["close"] for b in bars]
    ivs = store.iv_series()
    iv_by_date = dict(ivs)
    rows: list[dict[str, Any]] = []
    for i, d in enumerate(dates):
        h = None
        if i + 1 >= window:
            h = analyze(closes[i - window + 1: i + 1], dates[i - window + 1: i + 1], window=window)
        iv = iv_by_date.get(d)
        pct = state = None
        if iv is not None:
            before = [v for dd, v in ivs if dd < d][::-1]                    # 新 → 舊
            if len(before) >= min_iv_history:
                pct = iv_percentile(iv, before)
                state = iv_signal(pct) if pct is not None else None
        rows.append({"date": d, "hurst": h.value if h else None, "hurst_z": h.z if h else None,
                     "hurst_state": h.state if h else None, "direction": trend_direction(closes[: i + 1]),
                     "iv": iv, "iv_pct": pct, "iv_state": state})
    store.upsert_indicator_daily(rows)
    return rows


# ── 券商歷史逐筆回補 ─────────────────────────────────────────────

def open_readonly_api() -> Any:
    """模擬盤登入（只讀歷史行情、不下單、不動到正式盤連線）。呼叫端一定要 logout。"""
    import shioaji as sj
    from dotenv import load_dotenv
    load_dotenv()
    api = sj.Shioaji(simulation=True)
    api.login(api_key=os.environ["SHIOAJI_API_KEY"], secret_key=os.environ["SHIOAJI_SECRET_KEY"], contracts_timeout=15000)
    return api


def broker_usage(api: Any) -> dict[str, int]:
    u = api.usage()
    used = int(getattr(u, "bytes", 0) or 0)
    limit = int(getattr(u, "limit_bytes", 0) or 0)
    return {"connections": int(getattr(u, "connections", 0) or 0), "used": used, "limit": limit,
            "remaining": int(getattr(u, "remaining_bytes", max(limit - used, 0)) or 0)}


def _hhmm(ts: float) -> int:
    lt = time.localtime(ts)
    return lt.tm_hour * 100 + lt.tm_min


def trading_days(days: int, today: date | None = None, include_today: bool = False) -> list[date]:
    """最近 days 個日曆天內的平日（新 → 舊）。國定假日不特別排除（券商會回空資料）。"""
    today = today or date.today()
    out = []
    for k in range(0 if include_today else 1, days + 1):
        d = today - timedelta(days=k)
        if d.weekday() < 5:
            out.append(d)
    return out


def fetch_flow_history(days: int = 60, *, max_mb: float = 300.0, reserve_mb: float = 800.0, max_connections: int = 4,
                       probe: bool = False, force: bool = False, include_today: bool = False, dates: list[date] | None = None,
                       store: MarketStore | None = None, api: Any = None, today: date | None = None,
                       sleep: Callable[[float], None] = time.sleep, log: Callable[..., None] = print,
                       precheck: Callable[[], tuple[bool, str]] = room_for_login) -> dict[str, Any]:
    """向券商抓歷史逐筆 → 每分鐘彙總 → flow_1m（新的日子先抓，已有日盤資料的日子跳過）。

    流量保護：券商每日流量有上限（依成交量分級，這個帳戶是 2 GB），而即時行情訂閱用的是同一份額度——回補把額度吃光，
    即時行情就會斷。單日逐筆約 5~7 MB。所以：每次呼叫前檢查剩餘流量不低於 reserve_mb、本次累計（估算）不超過 max_mb；連線數已達 max_connections 就不登入
    （同一個身分最多 5 條，重啟留下的殘留連線還沒釋放時再多登入一條，可能讓正式盤下次重啟登不進去）。
    probe=True 只抓一天（dates 可指定），用來看流量、耗時與數字是否正確；抓到的列放在 summary["rows"]，可直接和本機資料比對。"""
    store = store or MarketStore()
    have = set() if force else store.flow_days(PREFIX)
    todo = list(dates) if dates else [d for d in trading_days(days, today, include_today) if d.isoformat() not in have]
    if probe:
        todo = todo[:1]
    summary: dict[str, Any] = {"requested": len(todo), "fetched": [], "empty": [], "failed": [], "minutes": 0,
                               "bytes": 0, "stopped": None, "rows": []}
    if not todo:
        summary["stopped"] = "沒有需要補的日子"
        return summary
    own = api is None
    if own:
        ok, why = precheck()                       # 登入「之前」先問後端有幾條連線，太多就不登入（登入本身就佔一條）
        if not ok:
            summary["stopped"] = why
            return summary
        log(why)
        api = open_readonly_api()
    try:
        u0 = broker_usage(api)
        summary["usage_before"] = u0
        log(f"券商連線數 {u0['connections']}、今日已用 {u0['used'] / 1e6:.0f} MB / {u0['limit'] / 1e6:.0f} MB")
        if u0["connections"] > max_connections:
            summary["stopped"] = f"連線數 {u0['connections']} 已超過 {max_connections}，不再多登入（等殘留連線釋放）"
            return summary
        contract = getattr(api.Contracts.Futures, PREFIX)
        contract = getattr(contract, PREFIX + "R1")
        spent = 0
        for d in todo:
            u = broker_usage(api)
            if u["remaining"] < reserve_mb * 1e6:
                summary["stopped"] = f"剩餘流量 {u['remaining'] / 1e6:.0f} MB 低於保留額度 {reserve_mb:.0f} MB（留給即時行情）"
                break
            if spent >= max_mb * 1e6:
                summary["stopped"] = f"本次已用 {spent / 1e6:.0f} MB，達上限 {max_mb:.0f} MB"
                break
            ticks = None
            for attempt in (1, 2):
                try:
                    ticks = api.ticks(contract=contract, date=d.isoformat(), timeout=90000)
                    break
                except Exception as e:
                    log(f"  {d} 第 {attempt} 次失敗：{e!r}")
                    sleep(SLEEP_BETWEEN_CALLS * 2)
            if ticks is None:
                summary["failed"].append(d.isoformat())
                continue
            t0 = time.time()
            rows = broker_ticks_to_rows(ticks)
            # 券商的流量計數有延遲（抓完當下是 +0，十幾秒後才反映），所以不能只看 usage 的差額：
            # 用「筆數 × 每筆位元組」估算，和實測差額取較大的，本次上限才擋得住
            delta = max(broker_usage(api)["used"] - u["used"], 0)
            cost = max(delta, int(len(ticks.ts) * BYTES_PER_TICK))
            spent += cost
            summary["bytes"] = spent
            if not rows:
                summary["empty"].append(d.isoformat())
                log(f"  {d}：沒有成交資料（假日？）")
            else:
                store.upsert_flow_1m(PREFIX, rows, source="broker")
                summary["fetched"].append(d.isoformat())
                summary["minutes"] += len(rows)
                if probe:
                    summary["rows"] = rows
                first, last = time.strftime("%m-%d %H:%M", time.localtime(rows[0]["ts"])), time.strftime("%m-%d %H:%M", time.localtime(rows[-1]["ts"]))
                day_min = sum(1 for r in rows if 845 <= _hhmm(r["ts"]) < 1345)
                log(f"  {d}：{len(ticks.ts):,} 筆逐筆 → {len(rows)} 分鐘（{first} ~ {last}；日盤 {day_min} 分鐘、其餘 {len(rows) - day_min} 分鐘），"
                    f"流量約 {cost / 1e6:.1f} MB，處理 {time.time() - t0:.1f} 秒")
            sleep(SLEEP_BETWEEN_CALLS)
        summary["usage_after"] = broker_usage(api)
        return summary
    finally:
        if own:
            try:
                api.logout()
            except Exception:
                pass


def compare_rows(local: list[dict[str, Any]], other: list[dict[str, Any]]) -> dict[str, Any]:
    """兩份「每分鐘一列」的外/內盤統計（本機 ticks.db 彙總 vs 券商歷史逐筆）逐分鐘比對，檢查回補的數字可不可信。
    本機 ticks.db 在服務重啟的空檔會缺資料，所以「完全相同」不會是 100%——看筆數比例與買方占比的相關性。"""
    a = {r["ts"]: r for r in local}
    b = {r["ts"]: r for r in other}
    common = sorted(set(a) & set(b))
    if not common:
        return {"minutes": 0, "note": "兩邊沒有重疊的分鐘"}

    def n(r: dict[str, Any]) -> int:
        return r["buy_n"] + r["sell_n"] + r["unk_n"]

    n_a = np.array([n(a[t]) for t in common], dtype=float)
    n_b = np.array([n(b[t]) for t in common], dtype=float)
    sh_a = np.array([a[t]["buy_n"] / max(a[t]["buy_n"] + a[t]["sell_n"], 1) for t in common])
    sh_b = np.array([b[t]["buy_n"] / max(b[t]["buy_n"] + b[t]["sell_n"], 1) for t in common])
    corr = float(np.corrcoef(sh_a, sh_b)[0, 1]) if len(common) > 2 and sh_a.std() > 0 and sh_b.std() > 0 else None
    return {"minutes": len(common), "only_in_local": len(set(a) - set(b)), "only_in_other": len(set(b) - set(a)),
            "identical_count_rate": float((n_a == n_b).mean()),
            "count_ratio_other_over_local": float(n_b.sum() / n_a.sum()) if n_a.sum() else None,
            "mean_abs_count_diff": float(np.abs(n_a - n_b).mean()), "buy_share_corr": corr,
            "mean_abs_close_diff": float(np.mean([abs(a[t]["close"] - b[t]["close"]) for t in common]))}


# ── CLI ──────────────────────────────────────────────────────────

def status(store: MarketStore | None = None) -> dict[str, Any]:
    store = store or MarketStore()
    span = store.flow_1m_span(PREFIX)
    daily = store.indicator_daily()
    fmt = lambda t: time.strftime("%F %H:%M", time.localtime(t)) if t else None  # noqa: E731
    return {"flow_1m": {"minutes": span["n"], "first": fmt(span["first_ts"]), "last": fmt(span["last_ts"]),
                        "day_session_days": len(store.flow_days(PREFIX))},
            "indicator_daily": {"days": len(daily), "first": daily[0]["date"] if daily else None,
                                "last": daily[-1]["date"] if daily else None,
                                "with_hurst": sum(1 for r in daily if r["hurst"] is not None),
                                "with_iv_pct": sum(1 for r in daily if r["iv_pct"] is not None)},
            "iv_points": len(store.iv_series())}


def _main() -> None:  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="市場指標視覺化的歷史資料（外/內盤逐分鐘、每日 Hurst/IV）")
    ap.add_argument("cmd", choices=["status", "daily", "flow-from-ticks", "flow-fetch"])
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--max-mb", type=float, default=300.0, help="本次回補最多用多少券商流量（MB）")
    ap.add_argument("--reserve-mb", type=float, default=800.0, help="剩餘流量低於這個數字就停（留給即時行情）")
    ap.add_argument("--probe", action="store_true", help="只抓最近一個缺的日子，看流量與數字")
    ap.add_argument("--force", action="store_true", help="已經有資料的日子也重抓")
    ap.add_argument("--include-today", action="store_true")
    ap.add_argument("--date", default=None, help="只抓指定日期（YYYY-MM-DD），搭配 --probe 用來對照本機資料")
    ap.add_argument("--ticks", default=None, help="ticks.db 路徑（預設 data/ticks.db）")
    a = ap.parse_args()
    store = MarketStore()
    if a.cmd == "status":
        import json
        print(json.dumps(status(store), ensure_ascii=False, indent=2))
    elif a.cmd == "daily":
        rows = build_daily(store)
        got = [r for r in rows if r["hurst"] is not None]
        print(f"indicator_daily：{len(rows)} 天（有 Hurst {len(got)} 天、有 IV 百分位 {sum(1 for r in rows if r['iv_pct'] is not None)} 天）")
    elif a.cmd == "flow-from-ticks":
        rows = ticks_db_to_rows(a.ticks)
        store.upsert_flow_1m(PREFIX, rows, source="ticks.db")
        print(f"本機 ticks.db → flow_1m：{len(rows)} 分鐘")
    else:
        import json
        res = fetch_flow_history(a.days, max_mb=a.max_mb, reserve_mb=a.reserve_mb, probe=a.probe, force=a.force,
                                 include_today=a.include_today, store=store,
                                 dates=[date.fromisoformat(a.date)] if a.date else None)
        print(f"完成 {len(res['fetched'])} 天、空 {len(res['empty'])} 天、失敗 {len(res['failed'])} 天；{res['minutes']} 分鐘；"
              f"流量 {res['bytes'] / 1e6:.1f} MB" + (f"；停止原因：{res['stopped']}" if res["stopped"] else ""))
        if a.probe and res["rows"]:
            print("與本機 ticks.db 同一分鐘的比對：")
            print(json.dumps(compare_rows(ticks_db_to_rows(a.ticks), res["rows"]), ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover
    _main()
