#!/usr/bin/env python3
"""OpenD smoke test — Open Question 1 sub-checks 5-10.

Verifies the moomoo SDK can talk to a local OpenD daemon at 127.0.0.1:11111
through the full SIMULATE/paper trading lifecycle:

  accinfo      → sub-checks 5 (SIMULATE present) + 6 (USD balance fetch)
  quote        → sub-check 6 (US quote permission)
  place-cancel → sub-checks 7+8 (paper limit order place + cancel)
  market-fill  → sub-check 9 (paper market order + fill detection)
  timezone     → sub-check 10 (market-hours / session boundary math)

Usage:
  uv run python scripts/smoke_opend.py accinfo
  uv run python scripts/smoke_opend.py quote --symbol US.SPY
  uv run python scripts/smoke_opend.py place-cancel
  uv run python scripts/smoke_opend.py market-fill --symbol US.SPY --qty 1
  uv run python scripts/smoke_opend.py timezone

Env: OPEND_HOST (default 127.0.0.1), OPEND_PORT (default 11111).
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from moomoo import (
    Currency,
    ModifyOrderOp,
    OpenQuoteContext,
    OpenSecTradeContext,
    OrderType,
    SecurityFirm,
    TrdEnv,
    TrdMarket,
    TrdSide,
)

OPEND_HOST = os.environ.get("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.environ.get("OPEND_PORT", "11111"))


def make_trade_ctx(market=TrdMarket.US, security_firm=SecurityFirm.FUTUINC):
    return OpenSecTradeContext(
        filter_trdmarket=market,
        host=OPEND_HOST,
        port=OPEND_PORT,
        security_firm=security_firm,
    )


def make_quote_ctx():
    return OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)


def find_simulate_us_acc_id(ctx):
    """Return acc_id of first SIMULATE US paper account in this firm."""
    ret, data = ctx.get_acc_list()
    if ret != 0:
        raise RuntimeError(f"get_acc_list failed (ret={ret}): {data}")
    print("All accounts under FUTUINC + US filter:")
    print(data.to_string())
    print()
    sim = data[data["trd_env"] == "SIMULATE"]
    if sim.empty:
        raise RuntimeError("No SIMULATE account found — open a paper account in moomoo app")
    return int(sim.iloc[0]["acc_id"])


def cmd_accinfo(args):
    ctx = make_trade_ctx()
    try:
        print(f"[accinfo] connected to OpenD at {OPEND_HOST}:{OPEND_PORT}")
        acc_id = find_simulate_us_acc_id(ctx)
        print(f"[accinfo] SIMULATE acc_id: {acc_id}")
        ret, data = ctx.accinfo_query(
            trd_env=TrdEnv.SIMULATE,
            acc_id=acc_id,
            currency=Currency.USD,
        )
        if ret != 0:
            print(f"[accinfo] FAIL — accinfo_query: {data}", file=sys.stderr)
            return 1
        print(f"[accinfo] balance (USD):\n{data.to_string()}")
        print()
        print("[accinfo] sub-check 5 (SIMULATE listed)  ✅")
        print("[accinfo] sub-check 6 (USD balance fetch) ✅")
        return 0
    finally:
        ctx.close()


def cmd_quote(args):
    qctx = make_quote_ctx()
    try:
        print(f"[quote] connected to OpenD at {OPEND_HOST}:{OPEND_PORT}")
        ret, data = qctx.get_market_snapshot([args.symbol])
        if ret != 0:
            print(f"[quote] FAIL — get_market_snapshot({args.symbol}): {data}", file=sys.stderr)
            return 1
        print(f"[quote] snapshot for {args.symbol}:")
        print(data.to_string())
        print()
        print("[quote] sub-check 6 (US quote permission) ✅")
        return 0
    finally:
        qctx.close()


def cmd_place_cancel(args):
    """Place a SIMULATE limit order well off the market, then cancel it."""
    ctx = make_trade_ctx()
    try:
        acc_id = find_simulate_us_acc_id(ctx)
        symbol = args.symbol
        qty = args.qty
        price = args.price
        print(f"[place-cancel] BUY {qty} {symbol} @ ${price} LIMIT, SIMULATE acc {acc_id}")
        ret, data = ctx.place_order(
            price=price,
            qty=qty,
            code=symbol,
            trd_side=TrdSide.BUY,
            order_type=OrderType.NORMAL,
            trd_env=TrdEnv.SIMULATE,
            acc_id=acc_id,
        )
        if ret != 0:
            print(f"[place-cancel] FAIL — place_order: {data}", file=sys.stderr)
            return 1
        print(f"[place-cancel] place_order OK:\n{data.to_string()}\n")
        order_id = str(data.iloc[0]["order_id"])
        time.sleep(args.dwell)
        print(f"[place-cancel] cancelling order_id={order_id}")
        ret, data = ctx.modify_order(
            modify_order_op=ModifyOrderOp.CANCEL,
            order_id=order_id,
            qty=0,
            price=0,
            trd_env=TrdEnv.SIMULATE,
            acc_id=acc_id,
        )
        if ret != 0:
            print(f"[place-cancel] FAIL — cancel: {data}", file=sys.stderr)
            return 1
        print(f"[place-cancel] cancel OK:\n{data.to_string()}\n")
        print("[place-cancel] sub-check 7 (paper limit place)  ✅")
        print("[place-cancel] sub-check 8 (paper limit cancel) ✅")
        return 0
    finally:
        ctx.close()


def cmd_market_fill(args):
    """Place a paper MARKET order during RTH, poll for fill."""
    ctx = make_trade_ctx()
    try:
        acc_id = find_simulate_us_acc_id(ctx)
        symbol = args.symbol
        qty = args.qty
        print(f"[market-fill] MARKET BUY {qty} {symbol}, SIMULATE acc {acc_id}")
        ret, data = ctx.place_order(
            price=0.0,
            qty=qty,
            code=symbol,
            trd_side=TrdSide.BUY,
            order_type=OrderType.MARKET,
            trd_env=TrdEnv.SIMULATE,
            acc_id=acc_id,
        )
        if ret != 0:
            print(f"[market-fill] FAIL — place_order: {data}", file=sys.stderr)
            return 1
        print(f"[market-fill] place_order OK:\n{data.to_string()}\n")
        order_id = str(data.iloc[0]["order_id"])
        deadline = time.time() + args.timeout
        last_status = None
        while time.time() < deadline:
            ret, orders = ctx.order_list_query(
                order_id=order_id,
                trd_env=TrdEnv.SIMULATE,
                acc_id=acc_id,
            )
            if ret == 0 and not orders.empty:
                row = orders.iloc[0]
                status = row.get("order_status")
                dealt = row.get("dealt_qty")
                if status != last_status:
                    print(f"[market-fill] status={status} dealt_qty={dealt}")
                    last_status = status
                if str(status) in {"FILLED_ALL", "OrderStatus.FILLED_ALL"}:
                    print(f"[market-fill] FILLED — dealt_qty={dealt}, dealt_avg_price={row.get('dealt_avg_price')}")
                    print("[market-fill] sub-check 9 (market fill) ✅")
                    return 0
                if str(status) in {"CANCELLED_ALL", "FAILED", "DELETED"}:
                    print(f"[market-fill] terminal non-fill status={status}", file=sys.stderr)
                    return 1
            time.sleep(args.poll_interval)
        print(f"[market-fill] TIMEOUT after {args.timeout}s waiting for fill", file=sys.stderr)
        return 2
    finally:
        ctx.close()


def cmd_timezone(args):
    """Verify OpenD's view of US market hours."""
    qctx = make_quote_ctx()
    try:
        ret, data = qctx.get_market_state(["US.SPY"])
        if ret != 0:
            print(f"[timezone] FAIL — get_market_state: {data}", file=sys.stderr)
            return 1
        print(f"[timezone] market_state for US.SPY:\n{data.to_string()}\n")
        ret, gs = qctx.get_global_state()
        if ret == 0:
            print(f"[timezone] global_state:\n{gs}\n")
        print("[timezone] sub-check 10 (timezone / session) — inspect output above")
        return 0
    finally:
        qctx.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("accinfo")

    p_quote = sub.add_parser("quote")
    p_quote.add_argument("--symbol", default="US.SPY")

    p_pc = sub.add_parser("place-cancel")
    p_pc.add_argument("--symbol", default="US.AAPL")
    p_pc.add_argument("--qty", type=int, default=1)
    p_pc.add_argument("--price", type=float, default=1.00,
                      help="Far-from-market limit price so the order sits open")
    p_pc.add_argument("--dwell", type=float, default=2.0,
                      help="Seconds to wait between place and cancel")

    p_mf = sub.add_parser("market-fill")
    p_mf.add_argument("--symbol", default="US.SPY")
    p_mf.add_argument("--qty", type=int, default=1)
    p_mf.add_argument("--timeout", type=float, default=30.0)
    p_mf.add_argument("--poll-interval", type=float, default=1.0)

    sub.add_parser("timezone")

    args = parser.parse_args()
    handlers = {
        "accinfo": cmd_accinfo,
        "quote": cmd_quote,
        "place-cancel": cmd_place_cancel,
        "market-fill": cmd_market_fill,
        "timezone": cmd_timezone,
    }
    sys.exit(handlers[args.cmd](args))


if __name__ == "__main__":
    main()
