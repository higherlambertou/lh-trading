"""
shioaji 子進程 worker。
由 broker.py 的 WorkerBridge 啟動，透過 multiprocessing.Queue 與主進程通訊。
所有 shioaji C/Rust 物件只活在這個進程；主進程永遠只碰純 Python dict。
"""
import logging
import multiprocessing
import os
import time
import traceback
from multiprocessing import Queue as MPQueue
from typing import Any

logger = logging.getLogger(__name__)


# ── 序列化 helpers ───────────────────────────────────────────────────

def _extract_quote(quote) -> dict:
    ts_ns = getattr(quote, "ts", None)
    ts = ts_ns / 1e9 if ts_ns else time.time()
    try:
        price = float(quote.close)
        code  = str(quote.code)
    except (TypeError, ValueError):
        return {}
    return {
        "code": code,
        "close": price,
        "open": float(getattr(quote, "open", 0) or 0),
        "high": float(getattr(quote, "high", 0) or 0),
        "low": float(getattr(quote, "low", 0) or 0),
        "volume": int(getattr(quote, "volume", 0) or 0),
        "total_volume": int(getattr(quote, "total_volume", 0) or 0),
        "change_price": float(getattr(quote, "change_price", 0) or 0),
        "tick_type": int(getattr(quote, "tick_type", 0) or 0),
        "simtrade": bool(getattr(quote, "simtrade", False)),     # 試算（開盤前試撮）行情；目前只用來記錄與核對，隔離靠時段（見 quote_hub）
        "ts": ts,
    }


def _extract_trade(t) -> dict:
    deals = getattr(t.status, "deals", []) or []
    total_qty = sum(d.quantity for d in deals)
    avg = sum(d.price * d.quantity for d in deals) / total_qty if total_qty > 0 else 0.0
    deal_ts_raw = getattr(deals[-1], "ts", None) if deals else None
    order_dt = getattr(t.status, "order_datetime", None)
    return {
        "id": str(t.status.id),
        "action": t.order.action.value,
        "price": float(t.order.price),
        "quantity": int(t.order.quantity),
        "status": t.status.status.value,
        "deal_quantity": int(getattr(t.status, "deal_quantity", 0) or 0),
        "order_datetime": str(order_dt or ""),
        "deal_ts": deal_ts_raw / 1e9 if isinstance(deal_ts_raw, (int, float)) and deal_ts_raw > 1e12 else 0.0,
        "avg_deal_price": round(avg, 2),
    }


def _opt_right(c) -> str:
    """回傳大寫的買賣權字串（module-level，避免 @staticmethod 在 function 內的問題）。"""
    r = getattr(c, "option_right", "")
    return str(getattr(r, "value", r)).upper()


def _extract_position(p) -> dict:
    direction = getattr(p, "direction", None)
    return {
        "code": str(p.code),
        "direction": getattr(direction, "value", str(direction)),
        "quantity": int(p.quantity),
        "price": float(p.price),
        "last_price": float(getattr(p, "last_price", 0) or 0),
        "pnl": float(getattr(p, "pnl", 0) or 0),
    }


def _extract_profit_loss(p) -> dict:
    """FutureProfitLoss → 純 dict。欄位對應前端 ProfitLoss：price = 進場價；dseq 用 id（前端當 key）。"""
    direction = getattr(p, "direction", None)
    return {
        "id": int(getattr(p, "id", 0) or 0),
        "code": str(getattr(p, "code", "")),
        "direction": getattr(direction, "value", str(direction)),
        "quantity": int(getattr(p, "quantity", 0) or 0),
        "price": float(getattr(p, "entry_price", 0) or 0),
        "cover_price": float(getattr(p, "cover_price", 0) or 0),
        "pnl": float(getattr(p, "pnl", 0) or 0),
        "fee": float(getattr(p, "fee", 0) or 0),
        "tax": float(getattr(p, "tax", 0) or 0),
        "date": str(getattr(p, "date", "") or ""),
        "dseq": str(getattr(p, "id", "") or ""),
    }


# ── Worker 主函式 ─────────────────────────────────────────────────────

def parent_alive() -> bool:
    """這個 worker 的父進程（main.py）還活著嗎。

    父進程被 kill -9 時沒有機會叫 worker 登出：run_live.sh 的 cleanup() 只給 main.py 正常關閉 2 秒就強殺，
    watchdog 偵測到凍結的自動重啟也是 kill -9。而 worker 原本只會等指令、從不檢查父進程——
    它會變成孤兒（PPID=1）、帶著券商連線一直活下去，連線被佔住、不會逾時（2026-10-08 實測：3 小時多都沒掉，
    最近 4 次停機有 2 次漏，把券商連線數從 1 條撐到 3 條；上限 5 條，滿了新的 worker 就登不進去）。

    不是 multiprocessing 子進程時（直接呼叫、測試）parent_process() 是 None，視為活著。"""
    pp = multiprocessing.parent_process()
    return pp is None or pp.is_alive()


def run_worker(cmd_q: MPQueue, event_q: MPQueue) -> None:
    """子進程入口，由 multiprocessing.Process 呼叫。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [worker]  %(levelname)-8s  %(message)s",
    )

    import shioaji as sj
    from dotenv import load_dotenv
    load_dotenv()

    simulation = os.getenv("SIMULATION", "true").lower() == "true"
    api_key    = os.getenv("SHIOAJI_API_KEY", "")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY", "")
    ca_path    = os.getenv("CA_PATH")
    ca_pass    = os.getenv("CA_PASSWORD")
    person_id  = os.getenv("PERSON_ID")

    api: sj.Shioaji | None = None
    _contracts: dict[str, Any] = {}  # prefix → contract object
    _option_contracts: list = []      # cached option contracts

    # ── contract helpers ──────────────────────────────────────────────

    def _get_futures_contract(prefix: str):
        prefix = prefix.upper()
        if prefix not in _contracts:
            mapping = {
                "TMF": lambda: api.Contracts.Futures.TMF.TMFR1,
                "MXF": lambda: api.Contracts.Futures.MXF.MXFR1,
                "TXF": lambda: api.Contracts.Futures.TXF.TXFR1,
            }
            if prefix not in mapping:
                raise ValueError(f"Unknown contract prefix: {prefix}")
            _contracts[prefix] = mapping[prefix]()
        return _contracts[prefix]

    def _list_option_contracts(category: str = "TXO") -> list:
        nonlocal _option_contracts
        opts = getattr(api.Contracts, "Options", None)
        if opts is None:
            return []
        cat = getattr(opts, category, None)
        if cat is None:
            return []
        _option_contracts = list(cat)
        return _option_contracts

    def _get_option_contract(delivery_month: str, strike: int, right: str, category: str):
        r = right.upper()[:1]
        contracts = _list_option_contracts(category)
        for c in contracts:
            if str(c.delivery_month) != str(delivery_month):
                continue
            try:
                if int(float(c.strike_price)) != int(strike):
                    continue
            except (TypeError, ValueError):
                continue
            cr = _opt_right(c)
            if cr.startswith(r) or cr.startswith({"C": "CALL", "P": "PUT"}.get(r, r)):
                return c
        raise ValueError(f"找不到選擇權合約: {category} {delivery_month} {strike} {right}")

    # ── callbacks ─────────────────────────────────────────────────────

    def _on_quote(exchange, quote):
        data = _extract_quote(quote)
        if data:
            try:
                event_q.put_nowait({"type": "quote", "data": data})
            except Exception:
                pass

    def _on_order(stat, msg):
        """從 shioaji order callback 萃取純 Python dict，子進程 → 主進程 IPC 安全。"""
        try:
            state_name = getattr(stat, "name", "") or str(stat)
            is_deal = "Deal" in state_name
            msg_d = msg if isinstance(msg, dict) else {}

            def _get(d, *keys):
                for k in keys:
                    try:
                        v = d.get(k) if isinstance(d, dict) else getattr(d, k, None)
                        if v is not None and v != "":
                            return v
                    except Exception:
                        pass
                return None

            ev = {
                "type": "order_event", "state": state_name,
                "trade_id": "", "price": 0.0, "quantity": 0,
                "op_type": "", "op_code": "", "op_msg": "",
            }

            if is_deal:
                ev["price"]    = float(_get(msg_d, "price") or 0)
                ev["quantity"] = int(_get(msg_d, "quantity") or 0)
                tid = _get(msg_d, "trade_id", "seqno", "ordno")
                if not tid:
                    tid = _get(_get(msg_d, "status") or {}, "id")
                if not tid:
                    tid = _get(_get(msg_d, "order") or {}, "ordno", "seqno")
                ev["trade_id"] = str(tid or "")
            else:
                status_d = _get(msg_d, "status") or {}
                ev["trade_id"] = str(
                    _get(status_d, "id") or _get(msg_d, "seqno", "ordno", "trade_id") or ""
                )
                op_d = _get(msg_d, "operation") or {}
                ev["op_type"] = str(_get(op_d, "op_type") or "")
                ev["op_code"] = str(_get(op_d, "op_code") or "")
                ev["op_msg"]  = str(_get(op_d, "op_msg")  or "")

            event_q.put_nowait(ev)
        except Exception:
            pass

    # ── login ─────────────────────────────────────────────────────────

    try:
        logger.info("Worker 登入中…(simulation=%s)", simulation)
        api = sj.Shioaji(simulation=simulation)
        api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=15000)
        logger.info("Worker 登入成功")
        if ca_path and ca_pass and person_id:
            api.activate_ca(ca_path=ca_path, ca_passwd=ca_pass, person_id=person_id)
            logger.info("Worker CA 啟用成功")
        api.set_order_callback(_on_order)
        api.quote.set_on_quote_fop_v1_callback(_on_quote)
        event_q.put({"type": "connected"})
    except Exception as e:
        event_q.put({"type": "error", "msg": f"Worker 登入失敗: {e}"})
        return

    # ── command loop ──────────────────────────────────────────────────

    orphaned = False
    while True:
        try:
            cmd = cmd_q.get(timeout=1.0)
        except Exception:
            if not parent_alive():                 # 閒置時（每秒一次）確認父進程還在；有指令進來代表父進程活著，不必檢查
                logger.warning("Worker 的父進程已不在（被強制結束？）→ 自行登出並結束，避免變成孤兒佔住券商連線")
                orphaned = True
                break
            continue

        if cmd is None or cmd.get("type") == "shutdown":
            break

        req_id  = cmd.get("req_id")
        method  = cmd.get("method", "")

        try:
            result: Any = None

            if method == "subscribe":
                c = _get_futures_contract(cmd["contract_code"])
                api.quote.subscribe(c, quote_type=sj.constant.QuoteType.Quote,
                                    version=sj.constant.QuoteVersion.v1)
                result = True

            elif method == "subscribe_option":
                contract = _get_option_contract(
                    cmd["delivery_month"], cmd["strike"], cmd["right"], cmd["category"]
                )
                api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Quote,
                                    version=sj.constant.QuoteVersion.v1)
                result = True

            elif method == "update_status":
                api.update_status(api.futopt_account)
                result = True

            elif method == "list_trades":
                result = [_extract_trade(t) for t in api.list_trades()]

            elif method == "list_trades_with_status":
                api.update_status(api.futopt_account)
                result = [_extract_trade(t) for t in api.list_trades()]

            elif method == "list_positions":
                result = [_extract_position(p)
                          for p in api.list_positions(api.futopt_account)]

            elif method == "list_profit_loss":
                # 已實現損益（當日）：帳務查詢，worker 單執行緒、查詢期間指令會排隊，呼叫端要控制頻率
                day = time.strftime("%Y-%m-%d")
                pls = api.list_profit_loss(api.futopt_account, cmd.get("begin") or day,
                                           cmd.get("end") or day, timeout=int(cmd.get("timeout_ms", 4000)))
                result = [_extract_profit_loss(p) for p in pls]

            elif method == "cancel_order":
                trade_id = cmd["trade_id"]
                api.update_status(api.futopt_account)
                trades = api.list_trades()
                trade = next((t for t in trades if t.status.id == trade_id), None)
                if trade:
                    api.cancel_order(trade)
                    result = True
                else:
                    raise ValueError(f"找不到 trade_id={trade_id}")

            elif method == "place_order":
                contract = _get_futures_contract(cmd["contract_code"])
                action     = getattr(sj.constant.Action,             cmd["action"])
                price_type = getattr(sj.constant.FuturesPriceType,   cmd["price_type"])
                order_type = getattr(sj.constant.OrderType,          cmd["order_type"])
                octype     = getattr(sj.constant.FuturesOCType,      cmd["octype"])
                order = sj.FuturesOrder(
                    action=action, price=cmd.get("price", 0), quantity=cmd["quantity"],
                    price_type=price_type, order_type=order_type, octype=octype,
                    account=api.futopt_account,
                )
                trade = api.place_order(contract, order)
                result = {"trade_id": trade.status.id, "status": trade.status.status.value}

            elif method == "place_option_order":
                contract = _get_option_contract(
                    cmd["delivery_month"], cmd["strike"], cmd["right"], cmd["category"]
                )
                action     = getattr(sj.constant.Action,           cmd["action"])
                order_type = getattr(sj.constant.OrderType,        cmd["order_type"])
                order = sj.FuturesOrder(
                    action=action, price=cmd["price"], quantity=cmd["quantity"],
                    price_type=sj.constant.FuturesPriceType.LMT,
                    order_type=order_type, octype=sj.constant.FuturesOCType.Auto,
                    account=api.futopt_account,
                )
                trade = api.place_order(contract, order)
                result = {
                    "trade_id": trade.status.id,
                    "status": trade.status.status.value,
                    "code": str(getattr(contract, "code", "")),
                }

            elif method == "snapshots":
                contracts = [_get_futures_contract(c) for c in cmd.get("contracts", [])]
                snaps = api.snapshots(contracts)
                result = [
                    {"code": str(getattr(s, "code", "")),
                     "close": float(getattr(s, "close", 0) or 0),
                     "buy_price": float(getattr(s, "buy_price", 0) or 0),
                     "sell_price": float(getattr(s, "sell_price", 0) or 0),
                     "total_volume": int(getattr(s, "total_volume", 0) or 0)}
                    for s in snaps
                ]

            elif method == "kbars":
                # 1 分 K（shioaji 1.5.x 的 kbars 只有 1 分 K，ts 為奈秒）。
                # worker 是單執行緒，查詢期間下單指令會排隊——呼叫端只在盤前/盤後用。
                contract = _get_futures_contract(cmd["contract_code"])
                kb = api.kbars(contract=contract, start=cmd["start"], end=cmd["end"],
                               timeout=int(cmd.get("timeout_ms", 30000)))
                result = {
                    "ts": [int(x) for x in kb.ts],
                    "open": [float(x) for x in kb.Open],
                    "high": [float(x) for x in kb.High],
                    "low": [float(x) for x in kb.Low],
                    "close": [float(x) for x in kb.Close],
                    "volume": [int(x) for x in kb.Volume],
                }

            elif method == "option_snapshot":
                contract = _get_option_contract(
                    cmd["delivery_month"], cmd["strike"], cmd["right"], cmd["category"]
                )
                snaps = api.snapshots([contract])
                if not snaps:
                    raise ValueError("查無報價")
                s = snaps[0]
                result = {
                    "code": str(getattr(contract, "code", "")),
                    "close": float(getattr(s, "close", 0) or 0),
                    "bid": float(getattr(s, "buy_price", 0) or 0),
                    "ask": float(getattr(s, "sell_price", 0) or 0),
                    "total_volume": int(getattr(s, "total_volume", 0) or 0),
                }

            elif method == "option_expiries":
                contracts = _list_option_contracts(cmd.get("category", "TXO"))
                result = sorted({str(c.delivery_month) for c in contracts})

            elif method == "option_strikes":
                r = cmd["right"].upper()[:1]
                dm = str(cmd["delivery_month"])
                contracts = _list_option_contracts(cmd.get("category", "TXO"))
                out = set()
                for c in contracts:
                    if str(c.delivery_month) != dm:
                        continue
                    cr = _opt_right(c)
                    if not (cr.startswith(r) or cr.startswith({"C": "CALL", "P": "PUT"}.get(r, r))):
                        continue
                    try:
                        out.add(int(float(c.strike_price)))
                    except (TypeError, ValueError):
                        pass
                result = sorted(out)

            elif method == "margin":
                m = api.margin(api.futopt_account)
                result = {
                    "equity": float(m.equity),
                    "equity_amount": float(m.equity_amount),
                    "margin_call": float(m.margin_call),
                    "initial_margin": float(m.initial_margin),
                    "maintenance_margin": float(m.maintenance_margin),
                }

            elif method == "usage":
                u = api.usage()
                used  = int(getattr(u, "bytes", 0) or 0)
                limit = int(getattr(u, "limit_bytes", 0) or 0)
                remaining = int(getattr(u, "remaining_bytes", max(limit - used, 0)) or 0)
                result = {
                    "connections": int(getattr(u, "connections", 0) or 0),
                    "used_bytes": used, "limit_bytes": limit,
                    "remaining_bytes": remaining,
                    "percent": round(used / limit * 100, 1) if limit > 0 else 0.0,
                }

            if req_id:
                event_q.put({"type": "response", "req_id": req_id, "result": result})

        except Exception as e:
            logger.error("Worker 執行 %s 失敗: %s", method, traceback.format_exc())
            if req_id:
                event_q.put({"type": "response", "req_id": req_id, "error": str(e)})

    # ── shutdown ──────────────────────────────────────────────────────
    try:
        api.logout()
        logger.info("Worker 已登出")
    except Exception:
        pass
    if orphaned:
        os._exit(0)             # 沒有父進程會來收我們（正常關閉時由 broker.logout() 在 5 秒後 kill）；shioaji 的執行緒可能擋住正常結束，直接離開
