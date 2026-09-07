#!/usr/bin/env python3
"""Persist the already-validated Pionex Smart Rebalance Chrome snapshot.

This helper never connects to Pionex and never changes a bot.  It only turns
the values read by the 08:30 browser automation into dashboard JSON.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "pionex_smart_rebalance.json"
TAIPEI = dt.timezone(dt.timedelta(hours=8))
EXPECTED = ("BTC", "ETH", "USDC", "SOL", "ADA", "SUI", "BNB", "XRP", "DOGE", "HYPE")
BOT_NAME = "BTC/ETH/SOL/ADA/SUI/BNB/XRP/DOGE/HYPE 市值ETF"


def _number(value: str, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} 必須是有效非負數")
    return number


def _write_atomic(payload: dict) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=OUT_PATH.name + ".", suffix=".tmp", dir=OUT_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, OUT_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--investment-usdt", required=True, type=float)
    parser.add_argument("--asset", action="append", default=[], metavar="SYMBOL:NET_USDT:TARGET_RATIO")
    args = parser.parse_args()
    if not math.isfinite(args.investment_usdt) or args.investment_usdt <= 0:
        raise ValueError("屯幣寶投資額必須是正數")

    parsed: dict[str, tuple[float, float]] = {}
    for raw in args.asset:
        parts = raw.split(":")
        if len(parts) != 3:
            raise ValueError(f"asset 格式錯誤：{raw}")
        symbol = parts[0].strip().upper()
        if symbol in parsed:
            raise ValueError(f"幣種重複：{symbol}")
        parsed[symbol] = (_number(parts[1], f"{symbol} 淨值"), _number(parts[2], f"{symbol} 目標比例"))
    if set(parsed) != set(EXPECTED):
        raise ValueError(f"屯幣寶必須恰好包含 {len(EXPECTED)} 個指定幣種：{'/'.join(EXPECTED)}")
    target_total = sum(value[1] for value in parsed.values())
    if abs(target_total - 1.0) > 1e-6:
        raise ValueError(f"目標比例合計不是 100%：{target_total * 100:.4f}%")

    net_total = sum(value[0] for value in parsed.values())
    assets = [{
        "symbol": symbol,
        "net_usdt": round(parsed[symbol][0], 2),
        "current_weight_pct": round(parsed[symbol][0] / net_total * 100, 2) if net_total else 0,
        "target_weight_pct": round(parsed[symbol][1] * 100, 2),
    } for symbol in EXPECTED]
    now = dt.datetime.now(TAIPEI).isoformat(timespec="seconds")
    payload = {
        "schema_version": 1,
        "updated_at": now,
        "name": BOT_NAME,
        "investment_usdt": round(args.investment_usdt, 2),
        "net_value_usdt": round(net_total, 2),
        "profit_usdt": round(net_total - args.investment_usdt, 2),
        "profit_pct": round((net_total / args.investment_usdt - 1) * 100, 2),
        "assets": assets,
    }
    _write_atomic(payload)
    print(json.dumps({"ok": True, "updated_at": now, "net_value_usdt": payload["net_value_usdt"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
