"""順勢爆量訊號的影子模式（update.md 發現 43）：同一個規則（core/signal_rules.py）在微台即時資料（flow_1m）上跑，
只記錄與通知、**不下單**；每個訊號之後 60 分鐘的 1 分 K 都存下來，之後想算任何持有時間或停損停利組合都可以（結果在讀取時才算，定義改了不必重寫資料）。

為什麼要有：歷史檢定（日盤 1,611 天）沒有找到優勢（毛報酬約 0，扣成本 ≈ −4 點）；夜盤沒有歷史可檢，只能累積前向資料；之後要在這個規則上加條件時，也要有帶時間戳的真實訊號可以回頭分析。
三個變體（門檻 30／40／50 點，各自冷卻 30 分鐘）都記；通知預設只發 50 點那個（SHADOW_NOTIFY：`off` 或逗號分隔的點數，例如 `30,40,50`），日盤平均每個日盤約 2.6 則。
資料庫：data/signals.db（前向資料，補不回來，有備份）。只讀本機資料庫與記憶體，不查券商、不碰下單。SIGNAL_SHADOW=false 關閉。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import statistics
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from core import signal_rules as sr
from core import vol_watch as vw
from core.daytrade import TAIPEI
from core.notify import notify

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "signals.db"
POLL_SEC = 20.0
N_SESSION = {"day": vw.DAY_MIN, "night": vw.NIGHT_MIN}
FRESH_SEC = 300.0                        # 訊號那一分鐘收完之後這麼久內才通知（重啟後補記的舊訊號不通知）
DONE_GRACE_SEC = 120.0                   # 60 分鐘的後續 K 棒都過去之後再多等一下才標成完成
BASELINE_TTL = 6 * 3600.0
SESSION_ZH = {"day": "日盤", "night": "夜盤"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule TEXT NOT NULL,                 -- burst30／burst40／burst50
  ts INTEGER NOT NULL,                -- 訊號那一分鐘的起點（epoch 秒）
  session TEXT NOT NULL,              -- day／night
  direction INTEGER NOT NULL,         -- +1 急漲、−1 急跌（順勢＝跟著這個方向）
  move REAL NOT NULL,                 -- 5 分鐘變動比例（帶正負）
  vol_ratio REAL NOT NULL,
  signal_close REAL NOT NULL,         -- 訊號那一分鐘的收盤
  entry_ts INTEGER,                   -- 進場那一分鐘（正常是 ts+60；那分鐘沒有成交就是之後第一根）
  entry_price REAL,                   -- 進場價＝那一分鐘的開盤
  path_json TEXT,                     -- 訊號之後 60 分鐘的 1 分 K：[[t,o,h,l,c,v],…]
  done INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  UNIQUE(rule, ts)
);
"""


def enabled() -> bool:
    return os.getenv("SIGNAL_SHADOW", "true").lower() != "false"


def notify_points() -> set[int]:
    """哪些變體會通知：SHADOW_NOTIFY＝off 或逗號分隔的點數；預設只有 50。"""
    raw = os.getenv("SHADOW_NOTIFY", "50").strip().lower()
    if raw in ("off", "false", "0", "none", ""):
        return set()
    out = set()
    for x in raw.split(","):
        try:
            v = int(x.strip())
        except ValueError:
            continue
        if v in sr.POINTS:
            out.add(v)
    return out


# ── 儲存 ─────────────────────────────────────────────────────

class SignalStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else DB_PATH
        self._ready = False

    def _conn(self) -> sqlite3.Connection:
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=10)) as c:
                c.execute("PRAGMA journal_mode=WAL")
                c.executescript(SCHEMA)
                c.commit()
            self._ready = True
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def add(self, rule: str, ts: int, session: str, direction: int, move: float, ratio: float, signal_close: float) -> bool:
        """新增一個訊號；同一個 (rule, ts) 已經有了就回 False（重啟後重新偵測不會重複）。"""
        with closing(self._conn()) as c:
            cur = c.execute("INSERT OR IGNORE INTO shadow_signals(rule, ts, session, direction, move, vol_ratio, signal_close, created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (rule, int(ts), session, int(direction), float(move), float(ratio), float(signal_close), time.time()))
            c.commit()
            return cur.rowcount == 1

    def pending(self) -> list[dict[str, Any]]:
        with closing(self._conn()) as c:
            return [dict(r) for r in c.execute("SELECT * FROM shadow_signals WHERE done=0 ORDER BY ts").fetchall()]

    def set_path(self, sid: int, entry_ts: Optional[int], entry_price: Optional[float], path: list[list[float]], done: bool) -> None:
        with closing(self._conn()) as c:
            c.execute("UPDATE shadow_signals SET entry_ts=?, entry_price=?, path_json=?, done=? WHERE id=?",
                      (entry_ts, entry_price, json.dumps(path), 1 if done else 0, sid))
            c.commit()

    def rows(self, limit: Optional[int] = None, since_ts: Optional[int] = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM shadow_signals", []
        if since_ts is not None:
            sql += " WHERE ts >= ?"
            args.append(int(since_ts))
        sql += " ORDER BY ts DESC" + (f" LIMIT {int(limit)}" if limit else "")
        with closing(self._conn()) as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]


_default_store: Optional[SignalStore] = None


def default_store() -> SignalStore:
    global _default_store
    if _default_store is None:
        _default_store = SignalStore()
    return _default_store


# ── 從 flow_1m 組出各時段的陣列 ──────────────────────────────

def session_start_ts(session: str, key: str) -> int:
    h, m = (8, 45) if session == "day" else (15, 0)
    d = datetime.strptime(key, "%Y-%m-%d")
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=TAIPEI).timestamp())


def sessions_from_flow(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[int, tuple[float, float, float, float, float]]]]:
    """flow_1m 的列 → {時段: {時段代號: {序號: (開, 高, 低, 收, 量)}}}；量＝外盤＋內盤＋不明口數。"""
    out: dict[str, dict[str, dict[int, tuple]]] = {"day": {}, "night": {}}
    for r in rows:
        try:
            pos = vw.session_pos(datetime.fromtimestamp(int(r["ts"]), TAIPEI))
            o, h, lo, c = (float(r[k]) for k in ("open", "high", "low", "close"))
            vol = float(int(r.get("buy_vol") or 0) + int(r.get("sell_vol") or 0) + int(r.get("unk_vol") or 0))
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        if pos and o > 0 and h > 0 and lo > 0 and c > 0:
            out[pos[0]].setdefault(pos[1], {})[pos[2]] = (o, h, lo, c, vol)
    return out


def to_arrays(bars: dict[int, tuple], n: int) -> dict[str, np.ndarray]:
    a = {k: np.full(n, np.nan) for k in ("o", "h", "l", "c", "v")}
    for i, (o, h, lo, c, v) in bars.items():
        if 0 <= i < n:
            a["o"][i], a["h"][i], a["l"][i], a["c"][i], a["v"][i] = o, h, lo, c, v
    return a


# ── 引擎 ─────────────────────────────────────────────────────

class ShadowEngine:
    def __init__(self, store: Optional[SignalStore] = None) -> None:
        self.store = store or default_store()
        self._prior: Optional[tuple[str, str, float, np.ndarray]] = None      # (時段, 時段代號, 建立時間, 前面各時段的窗口合計矩陣)

    async def _prior_windows(self, session: str, key: str, now_ts: float, mstore: Any) -> np.ndarray:
        p = self._prior
        if p and p[0] == session and p[1] == key and now_ts - p[2] < BASELINE_TTL:
            return p[3]
        since = now_ts - (45 if session == "day" else 40) * 86400
        rows = await asyncio.to_thread(mstore.flow_1m, "TMF", since)

        def build() -> np.ndarray:
            sess = sessions_from_flow(rows)[session]
            keys = sorted(k for k in sess if k < key)[-sr.BASE_SESSIONS:]
            n = N_SESSION[session]
            return np.vstack([sr.rolling_sum(to_arrays(sess[k], n)["v"]) for k in keys]) if keys else np.empty((0, n))

        m = await asyncio.to_thread(build)
        self._prior = (session, key, now_ts, m)
        logger.info("影子訊號的量基準已建立：%s %s，前 %d 個時段", session, key, len(m))
        return m

    async def check(self, now_ts: float, mstore: Any) -> list[dict[str, Any]]:
        """偵測新訊號（記錄、通知）並更新還沒完成的訊號的後續 K 棒；回傳這次新增的訊號。例外一律吞掉。"""
        new: list[dict[str, Any]] = []
        try:
            pos = vw.session_pos(datetime.fromtimestamp(now_ts, TAIPEI))
            if pos is not None:
                new = await self._detect(now_ts, pos, mstore)
            await self._update_pending(now_ts, mstore)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("影子訊號檢查失敗（已忽略）")
        return new

    async def _detect(self, now_ts: float, pos: tuple[str, str, int], mstore: Any) -> list[dict[str, Any]]:
        session, key, _ = pos
        n = N_SESSION[session]
        start = session_start_ts(session, key)
        prior = await self._prior_windows(session, key, now_ts, mstore)
        rows = await asyncio.to_thread(mstore.flow_1m, "TMF", start - 300)
        cur = sessions_from_flow(rows)[session].get(key)
        if not cur:
            return []
        a = to_arrays(cur, n)
        w = sr.rolling_sum(a["v"])
        base = sr.baseline(np.vstack([prior, w]))[-1]
        ratio = sr.ratio_of(w, base)
        new: list[dict[str, Any]] = []
        for pts in sr.POINTS:
            for e in sr.detect(a["c"], ratio, sr.threshold_pct(pts)):
                ts = start + e.m * 60
                if self.store.add(f"burst{pts}", ts, session, e.direction, e.move, e.ratio, float(a["c"][e.m])):
                    new.append({"rule": f"burst{pts}", "points": pts, "ts": ts, "session": session, "direction": e.direction, "move": e.move,
                                "ratio": e.ratio, "close": float(a["c"][e.m])})
        self._notify(new, now_ts)
        return new

    def _notify(self, new: list[dict[str, Any]], now_ts: float) -> None:
        want = notify_points()
        by_ts: dict[int, dict[str, Any]] = {}
        for s in new:
            if s["points"] in want and now_ts - (s["ts"] + 60) <= FRESH_SEC:
                by_ts[s["ts"]] = s                                  # new 依 POINTS 由小到大排，同一分鐘後面的蓋掉前面的＝通知取達到的最高門檻
        for ts, s in sorted(by_ts.items()):
            try:
                title, body = message(s)
                notify(title, body, key=f"shadow:{s['session']}:{ts}", cooldown=3600.0, priority="default")
            except Exception:  # noqa: BLE001
                logger.exception("影子訊號通知失敗（已忽略）")

    async def _update_pending(self, now_ts: float, mstore: Any) -> None:
        pend = await asyncio.to_thread(self.store.pending)
        if not pend:
            return
        rows = await asyncio.to_thread(mstore.flow_1m, "TMF", pend[0]["ts"] + 60)
        by_t = {int(r["ts"]) // 60 * 60: r for r in rows}
        for s in pend:
            t0 = s["ts"] + 60
            path: list[list[float]] = []
            for k in range(sr.HORIZON_MAX):
                r = by_t.get(t0 + 60 * k)
                if r is None:
                    continue
                vol = int(r.get("buy_vol") or 0) + int(r.get("sell_vol") or 0) + int(r.get("unk_vol") or 0)
                path.append([t0 + 60 * k, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), vol])
            done = now_ts >= t0 + 60 * sr.HORIZON_MAX + DONE_GRACE_SEC
            if path or done:
                await asyncio.to_thread(self.store.set_path, s["id"], int(path[0][0]) if path else None, path[0][1] if path else None, path, done)


def message(s: dict[str, Any]) -> tuple[str, str]:
    mv_pts = s["move"] * sr.REF_PRICE
    hhmm = datetime.fromtimestamp(s["ts"], TAIPEI).strftime("%H:%M")
    title = f"📈 影子訊號：{'急漲' if s['direction'] > 0 else '急跌'} {mv_pts:+.0f} 點／5 分鐘、量是平常的 {s['ratio']:.1f} 倍"
    body = (f"{SESSION_ZH.get(s['session'], s['session'])} {hhmm}，微台 {s['close']:,.0f}。規則：5 分鐘內變動 ≥ {s['points']} 點、成交量 ≥ 平常 2 倍 → 順勢。"
            f"歷史檢定（日盤 1,611 天）沒有找到優勢：順勢的平均毛報酬約 0、扣成本約 −{sr.cost_pts():g} 點。"
            "**這是影子模式，沒有下單**，只記錄之後 60 分鐘的結果（`python -m core.signal_shadow report` 看累積的成績）。")
    return title, body


# ── 結果（讀取時才算）────────────────────────────────────────

def path_arrays(s: dict[str, Any]) -> Optional[dict[str, np.ndarray]]:
    """訊號之後每一分鐘（第 0 分鐘＝進場那一分鐘）的高／低／收；沒有成交的分鐘沿用前一分鐘的收盤（價格沒動）。沒有進場價回 None。"""
    if not s.get("entry_price") or not s.get("path_json"):
        return None
    try:
        path = json.loads(s["path_json"])
    except ValueError:
        return None
    if not path:
        return None
    t0 = s["ts"] + 60
    op, h, lo, c = (np.full(sr.HORIZON_MAX, np.nan) for _ in range(4))
    for t, o, hh, ll, cc, _ in path:
        k = int(round((t - t0) / 60))
        if 0 <= k < sr.HORIZON_MAX:
            op[k], h[k], lo[k], c[k] = o, hh, ll, cc
    last = float(s["entry_price"])
    for k in range(sr.HORIZON_MAX):
        if np.isnan(c[k]):
            op[k], h[k], lo[k], c[k] = last, last, last, last
        else:
            last = c[k]
    return {"o": op, "h": h, "l": lo, "c": c}


def outcomes_of(s: dict[str, Any], cost: Optional[float] = None) -> dict[int, sr.Outcome]:
    """每個持有時間的結果；訊號還沒走完那麼久（資料不夠長）的持有時間不算。"""
    a = path_arrays(s)
    if a is None:
        return {}
    last_t = json.loads(s["path_json"])[-1][0]
    elapsed = int((last_t - (s["ts"] + 60)) // 60) + 1                  # 資料實際涵蓋到進場後第幾分鐘（後端中途沒開就會比 60 短）
    out: dict[int, sr.Outcome] = {}
    for h in sr.HORIZONS:
        if elapsed < h:
            continue                                                    # 還沒走到、或資料沒涵蓋到：不算，也不用「價格沒動」去補尾巴
        o = sr.outcome(float(s["entry_price"]), a["h"], a["l"], a["c"], int(s["direction"]), h, cost)
        if o is not None:
            out[h] = o
    return out


def bracket_of(s: dict[str, Any], tp_pts: float, sl_pts: float, cost: Optional[float] = None) -> Optional[sr.Bracket]:
    """這個訊號用停利 tp_pts／停損 sl_pts（點，÷47,000）、最長持有 30 分鐘的結果（定義同發現 44 的歷史檢定）；資料沒涵蓋到 30 分鐘就不算。"""
    a = path_arrays(s)
    if a is None:
        return None
    last_t = json.loads(s["path_json"])[-1][0]
    if int((last_t - (s["ts"] + 60)) // 60) + 1 < sr.MAX_HOLD:
        return None
    return sr.bracket(float(s["entry_price"]), int(s["direction"]), a["o"], a["h"], a["l"], a["c"], sr.threshold_pct(tp_pts), sr.threshold_pct(sl_pts), sr.MAX_HOLD, cost)


def summary(rows: list[dict[str, Any]], cost: Optional[float] = None) -> dict[str, Any]:
    """各變體、各時段、各持有時間的成績（淨報酬單位是換算到 47,000 點的『點』）。"""
    from core.signal_study import cluster_t
    res: dict[str, Any] = {"total": len(rows), "done": sum(1 for r in rows if r.get("done")), "rules": {}}
    for pts in sr.POINTS:
        name = f"burst{pts}"
        sub = [r for r in rows if r["rule"] == name]
        rule: dict[str, Any] = {"n": len(sub), "by_session": {}}
        for sess in ("day", "night", "all"):
            ss = [r for r in sub if sess == "all" or r["session"] == sess]
            per_h: dict[str, Any] = {}
            for h in sr.HORIZONS:
                pairs = [(r, outcomes_of(r, cost).get(h)) for r in ss]
                pairs = [(r, o) for r, o in pairs if o is not None]
                if not pairs:
                    per_h[str(h)] = {"n": 0}
                    continue
                net = np.array([o.net for _, o in pairs])
                days = np.array([int(datetime.fromtimestamp(r["ts"], TAIPEI).strftime("%Y%m%d")) for r, _ in pairs])
                mu, t, g = cluster_t(net, days)
                per_h[str(h)] = {"n": len(pairs), "mean_net_pts": mu * sr.REF_PRICE, "t": t, "days": g,
                                 "mean_gross_pts": float(np.mean([o.gross for _, o in pairs])) * sr.REF_PRICE, "win": float((net > 0).mean()),
                                 "mfe_med_pts": float(statistics.median([o.mfe for _, o in pairs])) * sr.REF_PRICE,
                                 "mae_med_pts": float(statistics.median([o.mae for _, o in pairs])) * sr.REF_PRICE}
            rule["by_session"][sess] = {"n": len(ss), "horizons": per_h}
        br: dict[str, Any] = {}
        for tp in sr.TP_GRID:
            for sl_ in sr.SL_GRID:
                got = [b for b in (bracket_of(r, tp, sl_, cost) for r in sub) if b is not None]
                br[f"{tp}/{sl_}"] = ({"n": len(got), "mean_net_pts": float(np.mean([b.net for b in got])) * sr.REF_PRICE,
                                      "mean_gross_pts": float(np.mean([b.gross for b in got])) * sr.REF_PRICE, "win": float(np.mean([b.net > 0 for b in got]))}
                                     if got else {"n": 0})
        rule["brackets"] = br
        res["rules"][name] = rule
    return res


def format_summary(r: dict[str, Any]) -> str:
    L = [f"影子訊號累積成績：共 {r['total']} 個訊號（已走完 60 分鐘的 {r['done']} 個）；淨報酬已扣 {sr.cost_pts():g} 點成本，單位是換算到 {sr.REF_PRICE:,.0f} 點的「點」", ""]
    for pts in sr.POINTS:
        rule = r["rules"][f"burst{pts}"]
        L.append(f"{pts} 點變體：{rule['n']} 個訊號")
        for sess, zh in (("day", "日盤"), ("night", "夜盤"), ("all", "合計")):
            b = rule["by_session"][sess]
            cells = []
            for h in sr.HORIZONS:
                x = b["horizons"][str(h)]
                cells.append(f"{h:>2} 分 n={x['n']:3d}" + (f" 淨 {x['mean_net_pts']:+6.2f} t={x['t']:+5.2f} 勝率 {x['win']:.0%}" if x["n"] else ""))
            L.append(f"   {zh}（{b['n']}）  " + "｜".join(cells))
    b = r["rules"]["burst50"]["brackets"]
    L.append("")
    L.append("50 點變體加停損停利（停利 TP／停損 SL，點；最長持有 30 分鐘；日盤＋夜盤合計）：")
    for tp in sr.TP_GRID:
        cells = []
        for sl_ in sr.SL_GRID:
            x = b[f"{tp}/{sl_}"]
            cells.append(f"SL{sl_:>2} n={x['n']:3d}" + (f" 淨 {x['mean_net_pts']:+6.2f} 勝率 {x['win']:.0%}" if x["n"] else ""))
        L.append(f"   TP{tp:>2}  " + "｜".join(cells))
    L.append("")
    L.append("（歷史日盤的檢定結果：持有固定時間與加停損停利都是淨約 −4 點、沒有一個過關；這裡累積的是前向資料，樣本要夠多才有意義，不要看幾筆就下結論。）")
    return "\n".join(L)


# ── 背景迴圈 ────────────────────────────────────────────────

shadow_engine = ShadowEngine()


async def shadow_loop(mstore: Any) -> None:
    if not enabled():
        logger.info("影子訊號已關閉（SIGNAL_SHADOW=false）")
        return
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            await shadow_engine.check(time.time(), mstore)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("影子訊號迴圈例外（已忽略）")


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    import sys
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] not in ("report", "recent"):
        print("用法：python -m core.signal_shadow report | recent [N]")
        return 2
    store = default_store()
    if a[0] == "report":
        print(format_summary(summary(store.rows())))
        return 0
    n = int(a[1]) if len(a) > 1 else 20
    for r in store.rows(limit=n):
        t = datetime.fromtimestamp(r["ts"], TAIPEI).strftime("%m/%d %H:%M")
        print(f"{t} {SESSION_ZH.get(r['session'], r['session'])} {r['rule']} {'↑' if r['direction'] > 0 else '↓'} {r['move'] * sr.REF_PRICE:+.0f} 點 量比 {r['vol_ratio']:.1f} "
              f"收 {r['signal_close']:,.0f} {'已完成' if r['done'] else '進行中'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(main())
