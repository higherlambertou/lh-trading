import argparse
import os
import sys
import time
from typing import Iterable

import shioaji as sj


def _require_env(*keys: str) -> dict[str, str]:
    missing = [key for key in keys if not os.getenv(key)]
    if missing:
        missing_list = ", ".join(missing)
        print(f"Missing required environment variables: {missing_list}")
        sys.exit(2)
    return {key: os.environ[key] for key in keys}


def _build_api(simulation: bool) -> sj.Shioaji:
    return sj.Shioaji(simulation=simulation)


def _login(api: sj.Shioaji) -> Iterable:
    creds = _require_env("SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY")
    return api.login(
        api_key=creds["SHIOAJI_API_KEY"],
        secret_key=creds["SHIOAJI_SECRET_KEY"],
    )


def _stock_order(api: sj.Shioaji, code: str, price: float, quantity: int) -> None:
    contract = api.Contracts.Stocks.TSE[code]
    order = api.Order(
        price=price,
        quantity=quantity,
        action=sj.constant.Action.Buy,
        price_type=sj.constant.StockPriceType.LMT,
        order_type=sj.constant.OrderType.ROD,
        account=api.stock_account,
    )
    trade = api.place_order(contract, order)
    print(trade)


def _futures_order(api: sj.Shioaji, price: float, quantity: int) -> None:
    contract = min(
        [
            x
            for x in api.Contracts.Futures.TXF
            if x.code[-2:] not in ["R1", "R2"]
        ],
        key=lambda x: x.delivery_date,
    )
    order = api.Order(
        action=sj.constant.Action.Buy,
        price=price,
        quantity=quantity,
        price_type=sj.constant.FuturesPriceType.LMT,
        order_type=sj.constant.OrderType.ROD,
        octype=sj.constant.FuturesOCType.Auto,
        account=api.futopt_account,
    )
    trade = api.place_order(contract, order)
    print(trade)


def _stock_quote(api: sj.Shioaji, code: str, seconds: int) -> None:
    def on_tick(exchange: sj.Exchange, tick: sj.TickSTKv1) -> None:
        print(f"{exchange} {tick}")

    api.quote.set_on_tick_stk_v1_callback(on_tick)
    try:
        contract = api.Contracts.Stocks[code]
    except KeyError:
        contract = api.Contracts.Stocks.TSE[code]

    api.quote.subscribe(
        contract,
        quote_type=sj.constant.QuoteType.Tick,
        version=sj.constant.QuoteVersion.v1,
    )
    time.sleep(seconds)


def _futures_quote(api: sj.Shioaji, code: str | None, seconds: int) -> None:
    def on_tick(exchange: sj.Exchange, tick: sj.TickFOPv1) -> None:
        print(f"{exchange} {tick}")

    api.quote.set_on_tick_fop_v1_callback(on_tick)
    if code:
        contract = api.Contracts.Futures.TXF[code]
    else:
        contract = min(
            [
                x
                for x in api.Contracts.Futures.TXF
                if x.code[-2:] not in ["R1", "R2"]
            ],
            key=lambda x: x.delivery_date,
        )

    api.quote.subscribe(
        contract,
        quote_type=sj.constant.QuoteType.Tick,
        version=sj.constant.QuoteVersion.v1,
    )
    time.sleep(seconds)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Shioaji API test runner based on Sinotrade API test flow",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("version", help="Print installed Shioaji version")

    login_parser = subparsers.add_parser("login", help="Login test (simulation by default)")
    login_parser.add_argument(
        "--production",
        action="store_true",
        help="Use production mode instead of simulation",
    )

    stock_parser = subparsers.add_parser(
        "stock-order",
        help="Place a simulated stock order (TSE by default)",
    )
    stock_parser.add_argument("--stock-code", default="2890")
    stock_parser.add_argument("--price", type=float, default=18)
    stock_parser.add_argument("--quantity", type=int, default=1)

    futures_parser = subparsers.add_parser(
        "futures-order",
        help="Place a simulated futures order (near-month TXF)",
    )
    futures_parser.add_argument("--price", type=float, default=15000)
    futures_parser.add_argument("--quantity", type=int, default=1)

    stock_quote_parser = subparsers.add_parser(
        "stock-quote",
        help="Stream stock tick quotes (default 15 seconds)",
    )
    stock_quote_parser.add_argument("--stock-code", default="2330")
    stock_quote_parser.add_argument("--seconds", type=int, default=15)
    stock_quote_parser.add_argument(
        "--production",
        action="store_true",
        help="Use production mode instead of simulation",
    )

    futures_quote_parser = subparsers.add_parser(
        "futures-quote",
        help="Stream futures tick quotes (default 15 seconds)",
    )
    futures_quote_parser.add_argument("--futures-code")
    futures_quote_parser.add_argument("--seconds", type=int, default=15)
    futures_quote_parser.add_argument(
        "--production",
        action="store_true",
        help="Use production mode instead of simulation",
    )

    subparsers.add_parser(
        "check-signed",
        help="Check API test signed status (production mode)",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "version":
        print(sj.__version__)
        return

    if args.command == "check-signed":
        api = _build_api(simulation=False)
        accounts = _login(api)
        print(accounts)
        return

    simulation = True
    if args.command in {"login", "stock-quote", "futures-quote"}:
        simulation = not args.production

    api = _build_api(simulation=simulation)
    accounts = _login(api)
    print(accounts)

    if args.command == "stock-order":
        _stock_order(api, args.stock_code, args.price, args.quantity)
    elif args.command == "futures-order":
        _futures_order(api, args.price, args.quantity)
    elif args.command == "stock-quote":
        _stock_quote(api, args.stock_code, args.seconds)
    elif args.command == "futures-quote":
        _futures_quote(api, args.futures_code, args.seconds)


if __name__ == "__main__":
    main()
