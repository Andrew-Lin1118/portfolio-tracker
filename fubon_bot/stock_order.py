#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股下單腳本 → 輸出 JSON
Usage: python stock_order.py --side buy --symbol 2330 --price 950 --lots 1 [--market] [--dry-run]
"""
import os, sys, json, argparse
import yaml

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.yaml")

_FUTURES_TYPES = {"futopt", "future", "futures", "f", "期貨"}

def _is_stock_account(acc):
    atype = str(getattr(acc, "account_type", "") or "").lower().strip()
    if atype in _FUTURES_TYPES:
        return False
    ano = str(getattr(acc, "account", "") or "").replace("-", "").strip()
    if ano.isdigit() and len(ano) == 7:
        return True
    return True  # 預設視為股票帳號

def tick_size(price):
    """台股漲跌停升降單位（主板）"""
    if price < 10:    return 0.01
    if price < 50:    return 0.05
    if price < 100:   return 0.1
    if price < 500:   return 0.5
    if price < 1000:  return 1.0
    return 5.0

def fmt_price(price):
    """格式化價格（去掉不必要的小數位）"""
    tick = tick_size(price)
    dp   = len(str(tick).rstrip('0').split('.')[-1]) if '.' in str(tick) else 0
    if dp == 0:
        return str(int(round(price)))
    return f"{price:.{dp}f}"

def round_to_tick(price, side):
    """將價格對齊到最近的合法 tick，並向市場方向偏移一格"""
    tick = tick_size(price)
    base = round(round(price / tick) * tick, 10)
    if side == "buy":
        adjusted = round(base + tick, 10)
    else:
        adjusted = round(base - tick, 10)
    return adjusted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--side",    required=True, choices=["buy","sell"])
    parser.add_argument("--symbol",  required=True)
    parser.add_argument("--price",   type=float, required=True)
    parser.add_argument("--lots",    type=int, default=1, help="張數")
    parser.add_argument("--market",  action="store_true", help="市價單")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    dry_run = args.dry_run or cfg.get("dry_run", False)

    if dry_run:
        price_str = "市價" if args.market else fmt_price(args.price)
        print(json.dumps({
            "status":   "ok",
            "dry_run":  True,
            "message":  f"[模擬] {args.side.upper()} {args.symbol} × {args.lots}張 @ {price_str}",
            "order_no": "DRY-RUN",
            "symbol":   args.symbol,
            "side":     args.side,
            "price":    args.price,
            "lots":     args.lots,
        }, ensure_ascii=False))
        return

    from fubon_neo.sdk import FubonSDK, Order
    from fubon_neo.constant import BSAction, MarketType, PriceType, TimeInForce, OrderType

    target_acc = str(cfg.get("account", "") or "").replace("-", "").strip()
    sdk    = FubonSDK()
    result = sdk.apikey_login(
        cfg["personal_id"], cfg["api_key"], cfg["cert_path"], cfg["cert_pass"]
    )
    if not result.is_success:
        print(json.dumps({"error": f"登入失敗: {result.message}"}))
        return

    try:
        account = None
        for acc in (result.data or []):
            ano = str(getattr(acc, "account", "") or "").replace("-", "").strip()
            if target_acc and (ano == target_acc or target_acc in ano):
                account = acc; break
            if _is_stock_account(acc) and account is None:
                account = acc
        if not account:
            print(json.dumps({"error": "找不到股票帳號"}))
            return

        buy_sell    = BSAction.Buy if args.side == "buy" else BSAction.Sell
        quantity    = args.lots * 1000   # 張 → 股
        limit_price = round_to_tick(args.price, args.side)

        order = Order(
            buy_sell      = buy_sell,
            symbol        = args.symbol,
            quantity      = quantity,
            market_type   = MarketType.Common,
            price_type    = PriceType.Market if args.market else PriceType.Limit,
            time_in_force = TimeInForce.ROD,
            order_type    = OrderType.Stock,
            price         = fmt_price(limit_price) if not args.market else "0",
            user_def      = "StockDash",
        )

        r = sdk.stock.place_order(account, order)
        if r.is_success:
            order_no = str(getattr(r.data, "order_no", "") or "")
            seq_no   = str(getattr(r.data, "seq_no",   "") or "")
            # IntradayOdd orders return empty order_no; use seq_no instead
            effective_id = order_no or seq_no
            mkt_type = str(getattr(r.data, "market_type", "") or "")
            print(json.dumps({
                "status":      "ok",
                "order_no":    effective_id,
                "seq_no":      seq_no,
                "market_type": mkt_type,
                "message":     f"{args.side.upper()} {args.symbol} × {args.lots}張 @ {fmt_price(limit_price)} 委託成功",
                "symbol":      args.symbol,
                "side":        args.side,
                "price":       limit_price,
                "lots":        args.lots,
            }, ensure_ascii=False))
        else:
            print(json.dumps({
                "error":  getattr(r, "message", "下單失敗"),
                "status": "fail",
            }))
    finally:
        try:
            sdk.logout()
        except Exception:
            try:
                sdk.logout()
            except Exception:
                pass


if __name__ == "__main__":
    main()
