#!/usr/bin/env python3
"""Collect Pionex grid-profit snapshots for Portfolio Tracker's calendar.

The script is read-only against Pionex.  It keeps one rolling record per
08:00-to-08:00 Taipei day and writes data/pionex_grid_calendar.json atomically.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "pionex_grid_calendar.json"
CRYPTO_BALANCE_PATH = ROOT / "data" / "crypto_balance.json"
CREDENTIAL_PATH = ROOT / "派網網格資料更新API.txt"
API_BASE = "https://api.pionex.com"
BOUNDARY_HOUR = 8
TAIPEI = dt.timezone(dt.timedelta(hours=8))

ALIAS_TO_SYMBOL = {
    "TQQQX": "TQQQ", "CRWVX": "CRWV", "CBRS": "CBRS", "IRENX": "IREN",
    "RKLBX": "RKLB", "ASTSX": "ASTS", "BEX": "BE", "INTCX": "INTC",
    "AXTIX": "AXTI", "CRCLX": "CRCL", "EWYX": "EWY", "SOXLX": "SOXL",
    "SKHY": "SKHY", "DRAMX": "DRAM", "MSTRX": "MSTR", "AVGOX": "AVGO",
    "NBISX": "NBIS", "SPCX": "SPCX", "ORCLX": "ORCL", "METAX": "META",
    "LITEX": "LITE", "AAOIX": "AAOI", "SKHX": "SK", "SMSN": "SMS",
    "PLTRX": "PLTR", "ONDSX": "ONDS", "TSMX": "TSM", "SNDKX": "SNDK",
    "AMDX": "AMD", "TSLAX": "TSLA", "MUX": "MU", "NVDAX": "NVDA",
    "SLVX": "SLV", "XAUT": "XAUT", "SNXXX": "SNXX",
}

# 2026-08-27 11:19 試算表版本（ETH 換機器人之前）的 U/日 + 可用網格。
# 本機日曆曾用 11:08 備援當 08:00 基準，會少算 08:00–11:08 的利潤。
PRE_ETH_SNAPSHOT_20260827 = [
    {"symbol": "SNDK", "az": 0.8, "bc": 238.0}, {"symbol": "MU", "az": 0.3, "bc": 54.9},
    {"symbol": "SLV", "az": 0.0, "bc": 712.6}, {"symbol": "NVDA", "az": 0.2, "bc": 538.1},
    {"symbol": "AMD", "az": 0.0, "bc": 19.9}, {"symbol": "ETH", "az": 0.0, "bc": 5.0},
    {"symbol": "XAUT", "az": 0.0, "bc": 379.2}, {"symbol": "SK", "az": 0.8, "bc": 55.1},
    {"symbol": "TSM", "az": 0.0, "bc": 282.5}, {"symbol": "SMS", "az": 0.5, "bc": 400.5},
    {"symbol": "PLTR", "az": 0.0, "bc": 20.0}, {"symbol": "AAOI", "az": 0.2, "bc": 1099.5},
    {"symbol": "ORCL", "az": 0.0, "bc": 11.5}, {"symbol": "TSLA", "az": 0.0, "bc": 18.9},
    {"symbol": "SKHY", "az": 0.2, "bc": 71.5}, {"symbol": "EWY", "az": 0.2, "bc": 37.8},
    {"symbol": "NBIS", "az": 0.1, "bc": 63.3}, {"symbol": "DRAM", "az": 0.2, "bc": 53.8},
    {"symbol": "CRCL", "az": 0.1, "bc": 9.0}, {"symbol": "LITE", "az": 0.1, "bc": 22.1},
    {"symbol": "SPCX", "az": 0.0, "bc": 28.4}, {"symbol": "MSTR", "az": 0.0, "bc": 16.5},
    {"symbol": "META", "az": 0.0, "bc": 21.3}, {"symbol": "AVGO", "az": 0.0, "bc": 21.4},
    {"symbol": "CBRS", "az": 0.1, "bc": 48.6}, {"symbol": "RKLB", "az": 0.0, "bc": 23.3},
    {"symbol": "IREN", "az": 0.0, "bc": 38.1}, {"symbol": "CRWV", "az": 0.0, "bc": 39.5},
    {"symbol": "ASTS", "az": 0.0, "bc": 27.1}, {"symbol": "BE", "az": 0.0, "bc": 55.7},
    {"symbol": "ONDS", "az": 0.0, "bc": 146.8}, {"symbol": "AXTI", "az": 0.1, "bc": 72.2},
    {"symbol": "SNXX", "az": 0.4, "bc": 1.8}, {"symbol": "INTC", "az": 0.0, "bc": 22.0},
    {"symbol": "SNXX", "az": 0.1, "bc": 118.8}, {"symbol": "SOXL", "az": 0.1, "bc": 36.8},
    {"symbol": "TQQQ", "az": 0.0, "bc": 6.2},
]
ETH_REOPEN_MS = dt.datetime(2026, 8, 27, 11, 19, 0, tzinfo=TAIPEI).timestamp() * 1000


def _credentials() -> tuple[str, str]:
    lines = [line.strip() for line in CREDENTIAL_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) < 4 or lines[0] != "API Key" or lines[2] != "Api Secret":
        raise RuntimeError("派網 API 憑證檔格式錯誤")
    if not lines[1] or not lines[3]:
        raise RuntimeError("派網 API Key 或 Secret 為空")
    return lines[1], lines[3]


def _private_get(path: str, params: dict[str, Any], key: str, secret: str) -> dict[str, Any]:
    query_params = {**params, "timestamp": str(int(time.time() * 1000))}
    query = urlencode(sorted(query_params.items()))
    signature = hmac.new(secret.encode(), f"GET{path}?{query}".encode(), hashlib.sha256).hexdigest()
    request = Request(
        f"{API_BASE}{path}?{query}",
        headers={
            "PIONEX-KEY": key,
            "PIONEX-SIGNATURE": signature,
            "User-Agent": "Mozilla/5.0 PionexCalendar/1.0",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urlopen(request, timeout=25) as response:
        payload = json.load(response)
    if payload.get("result") is not True:
        raise RuntimeError(f"派網 API 失敗：{payload.get('code') or payload.get('message')}")
    return payload


def _public_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        f"{API_BASE}{path}?{urlencode(params)}",
        headers={"User-Agent": "Mozilla/5.0 PionexCalendar/1.0", "Accept": "application/json"},
    )
    with urlopen(request, timeout=25) as response:
        payload = json.load(response)
    if payload.get("result") is not True:
        raise RuntimeError("派網公開行情 API 失敗")
    return payload


def _running_grids(key: str, secret: str) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    page_token = ""
    seen: set[str] = set()
    for _ in range(20):
        params: dict[str, Any] = {"buOrderTypes": "futures_grid", "status": "running"}
        if page_token:
            params["pageToken"] = page_token
        payload = _private_get("/api/v1/bot/orders", params, key, secret)
        data = payload.get("data") or {}
        orders.extend(data.get("results") or [])
        next_token = str(data.get("nextPageToken") or "").strip()
        if not next_token:
            break
        if next_token in seen:
            raise RuntimeError("派網 Bot API 分頁 token 重複")
        seen.add(next_token)
        page_token = next_token
    if not orders:
        raise RuntimeError("派網沒有回傳任何運行中期貨網格")
    ids = [str(order.get("buOrderId") or "") for order in orders]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise RuntimeError("派網 Bot ID 缺少或重複")
    return orders


def _finite(value: Any, label: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeError(f"{label} 不是有效數字")
    return number


def _truncate2(value: float) -> float:
    return math.floor(max(0.0, value) * 100.0 + 1e-9) / 100.0


def _perpetual_prices() -> dict[str, float]:
    payload = _public_get("/api/v1/market/tickers", {"type": "PERP"})
    prices: dict[str, float] = {}
    for ticker in (payload.get("data") or {}).get("tickers") or []:
        symbol = str(ticker.get("symbol") or "").upper()
        if symbol and symbol not in prices:
            close = _finite(ticker.get("close"), f"{symbol} close")
            if close > 0:
                prices[symbol] = close
    return prices


def _order_alias(order: dict[str, Any]) -> str:
    base = str(order.get("base") or "").upper()
    quote = str(order.get("quote") or "").upper()
    data = order.get("buOrderData") or {}
    if base == "USDT.PERP" and quote == "ETH" and str(data.get("cateType") or "").upper() == "FUTURE_GRID_COIN_MARGINED":
        return "ETH_COIN"
    if quote == "USDT" and base.endswith(".PERP"):
        return base[:-5]
    return ""


def _running_label(milliseconds: float) -> str:
    total_hours = max(0, int(milliseconds // 3_600_000))
    return f"{total_hours // 24}日{total_hours % 24:02d}時"


def _metrics(orders: list[dict[str, Any]]) -> tuple[float, dict[str, float], list[dict[str, Any]]]:
    needs_eth = any(
        str(order.get("base") or "").upper() == "USDT.PERP"
        and str(order.get("quote") or "").upper() == "ETH"
        for order in orders
    )
    eth_usdt = 1.0
    if needs_eth:
        ticker = _public_get("/api/v1/market/tickers", {"symbol": "ETH_USDT"})
        eth_usdt = _finite(((ticker.get("data") or {}).get("tickers") or [{}])[0].get("close"), "ETH_USDT")
        if eth_usdt <= 0:
            raise RuntimeError("ETH_USDT 行情必須大於 0")

    perp_prices = _perpetual_prices()
    available_total = 0.0
    gross_by_bot: dict[str, float] = {}
    bot_rows: list[dict[str, Any]] = []
    now_ms = dt.datetime.now(TAIPEI).timestamp() * 1000
    for order in orders:
        bot_id = str(order["buOrderId"])
        data = order.get("buOrderData") or {}
        alias = _order_alias(order)
        symbol = "ETH" if alias == "ETH_COIN" else ALIAS_TO_SYMBOL.get(alias, alias)
        if not symbol:
            raise RuntimeError(f"{bot_id} 無法辨識標的")
        gross = _finite(data.get("gridProfit", 0), f"{bot_id} gridProfit")
        withdrawn = _finite(data.get("profitWithdrawn", 0) or 0, f"{bot_id} profitWithdrawn")
        reinvested = _finite(data.get("profitReinvest", 0) or 0, f"{bot_id} profitReinvest")
        reduced = _finite(data.get("profitReduce", 0) or 0, f"{bot_id} profitReduce")
        factor = eth_usdt if (
            str(order.get("base") or "").upper() == "USDT.PERP"
            and str(order.get("quote") or "").upper() == "ETH"
        ) else 1.0
        available = (gross - withdrawn - reinvested - reduced) * factor
        tolerance = max(1e-8, abs(gross * factor) * 1e-10)
        if available < -tolerance or gross < 0:
            raise RuntimeError(f"{bot_id} 網格利潤資料不合理")
        available_usdt = _truncate2(available)
        available_total += available_usdt
        gross_by_bot[bot_id] = gross * factor

        leverage = _finite(data.get("leverage"), f"{symbol} leverage")
        quote_investment = _finite(data.get("quoteInvestment"), f"{symbol} quoteInvestment")
        opened_at_ms = _finite(order.get("createTime"), f"{symbol} createTime")
        running_ms = now_ms - opened_at_ms
        if leverage <= 0 or quote_investment <= 0 or running_ms <= 0:
            raise RuntimeError(f"{symbol} 機器人倉位或運行時間不合理")

        down = _finite(data.get("estimateLiquidationPriceDown", 0) or 0, f"{symbol} liquidation down")
        up = _finite(data.get("estimateLiquidationPriceUp", 0) or 0, f"{symbol} liquidation up")
        if alias == "ETH_COIN":
            if up <= 0 or down > 0:
                raise RuntimeError("ETH 幣本位強平欄位不符合預期")
            liquidation = 1 / up
            current_price = perp_prices.get("ETH_USDT_PERP")
            position_usdt = quote_investment * eth_usdt
            direction = "多"
        else:
            candidates = [value for value in (down, up) if value > 0]
            if len(candidates) != 1:
                raise RuntimeError(f"{symbol} 強平價不是唯一有效值")
            liquidation = candidates[0]
            current_price = perp_prices.get(f"{alias}_USDT_PERP")
            position_usdt = quote_investment
            direction = "多" if down > 0 else "空"
        if current_price is None or current_price <= 0:
            raise RuntimeError(f"{symbol} 缺少永續合約現價")
        liquidation_distance = ((current_price - liquidation) / current_price if direction == "多"
                                else (liquidation - current_price) / current_price)
        cumulative_usdt = gross * factor
        annualized = cumulative_usdt / position_usdt * (365 * 24 * 60 * 60 * 1000 / running_ms)
        bot_rows.append({
            "bot_id": bot_id,
            "symbol": symbol,
            "api_alias": alias,
            "direction": direction,
            "leverage": leverage,
            "position_usdt": round(position_usdt, 2),
            "current_price": current_price,
            "liquidation_price": liquidation,
            "liquidation_distance_pct": liquidation_distance * 100,
            "available_grid_profit_usdt": available_usdt,
            "cumulative_grid_profit_usdt": round(cumulative_usdt, 2),
            "annualized_grid_return_pct": annualized * 100,
            "opened_at": dt.datetime.fromtimestamp(opened_at_ms / 1000, TAIPEI).isoformat(timespec="seconds"),
            "running_time": _running_label(running_ms),
        })
    bot_rows.sort(key=lambda row: (-row["position_usdt"], row["symbol"], row["bot_id"]))
    return round(available_total, 2), gross_by_bot, bot_rows


def _load() -> dict[str, Any]:
    try:
        data = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("days"), dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {
        "schema_version": 1,
        "timezone": "Asia/Taipei",
        "boundary_hour": BOUNDARY_HOUR,
        "days": {},
    }


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_atomic(data: dict[str, Any]) -> None:
    _write_json_atomic(OUT_PATH, data)


def _sync_dashboard_pionex_twd(value: float, captured_at: dt.datetime) -> None:
    """Update the dashboard's shared Pionex-TWD field without touching other balances."""
    try:
        data = json.loads(CRYPTO_BALANCE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"無法解析 {CRYPTO_BALANCE_PATH.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{CRYPTO_BALANCE_PATH.name} 根節點必須是物件")
    data["manual_pionex_twd"] = round(float(value), 2)
    data["manual_pionex_twd_ts"] = captured_at.isoformat(timespec="seconds")
    _write_json_atomic(CRYPTO_BALANCE_PATH, data)


def _take_pre_eth_snap(by_symbol: dict[str, list[dict[str, float]]], symbol: str, available: float) -> dict[str, float] | None:
    candidates = by_symbol.get(symbol) or []
    if not candidates:
        return None
    if len(candidates) == 1:
        return by_symbol.pop(symbol)[0]
    chosen = min(candidates, key=lambda row: abs(float(row["bc"]) - available))
    candidates.remove(chosen)
    if candidates:
        by_symbol[symbol] = candidates
    else:
        by_symbol.pop(symbol, None)
    return chosen


def _recovered_0800_baseline(day_key: str, gross_by_bot: dict[str, float], bot_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Rebuild 08:00 gross baseline from the pre-ETH sheet snapshot."""
    if day_key != "2026-08-27":
        return None
    by_symbol: dict[str, list[dict[str, float]]] = {}
    for row in PRE_ETH_SNAPSHOT_20260827:
        by_symbol.setdefault(row["symbol"], []).append(dict(row))
    baseline: dict[str, float] = {}
    recovered_daily = 0.0
    for row in bot_rows:
        bot_id = str(row["bot_id"])
        gross = _finite(gross_by_bot.get(bot_id, 0), f"{bot_id} gross")
        available = _finite(row.get("available_grid_profit_usdt", 0), f"{bot_id} available")
        opened = dt.datetime.fromisoformat(str(row["opened_at"]))
        opened_ms = opened.timestamp() * 1000
        snap = _take_pre_eth_snap(by_symbol, str(row["symbol"]).upper(), available)
        az_then = float(snap["az"]) if snap else 0.0
        bc_then = float(snap["bc"]) if snap else available
        replaced = bool(snap) and opened_ms > ETH_REOPEN_MS
        delta = max(0.0, gross) if replaced else max(0.0, available - bc_then)
        daily = az_then + delta
        recovered_daily += daily
        baseline[bot_id] = max(0.0, gross - daily)
    return {
        "baseline": baseline,
        "recovered_daily": round(recovered_daily, 2),
        "captured_at": "2026-08-27T08:00:00+08:00",
    }


def collect(pionex_net_twd: float | None, daily_profit_seed: float | None) -> dict[str, Any]:
    key, secret = _credentials()
    orders = _running_grids(key, secret)
    grid_profit, gross_by_bot, bot_rows = _metrics(orders)
    now = dt.datetime.now(TAIPEI)
    day_key = (now - dt.timedelta(hours=BOUNDARY_HOUR)).date().isoformat()
    data = _load()
    days = data.setdefault("days", {})
    day = days.get(day_key) if isinstance(days.get(day_key), dict) else {}

    baseline = day.get("baseline_gross_by_bot")
    baseline_captured_at = day.get("baseline_captured_at")
    baseline_is_true_0800 = bool(day.get("baseline_is_true_0800"))
    repaired = None
    if daily_profit_seed is None and not baseline_is_true_0800:
        repaired = _recovered_0800_baseline(day_key, gross_by_bot, bot_rows)
        if repaired:
            baseline = repaired["baseline"]
            baseline_captured_at = repaired["captured_at"]
            baseline_is_true_0800 = True
    if not isinstance(baseline, dict):
        if daily_profit_seed is None:
            baseline = dict(gross_by_bot)
            baseline_captured_at = now.isoformat(timespec="seconds")
            # 若第一次建檔已過 08:00，這不是真正的 08:00 基準，不可冒充。
            eight = now.replace(hour=BOUNDARY_HOUR, minute=0, second=0, microsecond=0)
            baseline_is_true_0800 = now <= eight + dt.timedelta(minutes=10)
        else:
            gross_total = sum(gross_by_bot.values())
            scale = max(0.0, (gross_total - daily_profit_seed) / gross_total) if gross_total > 0 else 0.0
            baseline = {bot_id: value * scale for bot_id, value in gross_by_bot.items()}
            baseline_captured_at = f"{day_key} 08:00:00+08:00"
            baseline_is_true_0800 = True
    if not baseline_captured_at:
        baseline_captured_at = now.isoformat(timespec="seconds")
        eight = now.replace(hour=BOUNDARY_HOUR, minute=0, second=0, microsecond=0)
        baseline_is_true_0800 = False

    last_values = day.get("last_gross_by_bot") if isinstance(day.get("last_gross_by_bot"), dict) else {}
    for bot_id, value in gross_by_bot.items():
        if bot_id not in baseline:
            baseline[bot_id] = value
        last_values[bot_id] = value

    daily_profit = sum(
        _finite(last_values.get(bot_id, base), f"{bot_id} last gross") - _finite(base, f"{bot_id} baseline")
        for bot_id, base in baseline.items()
    )
    if daily_profit_seed is not None:
        daily_profit = daily_profit_seed

    previous_net = day.get("pionex_net_twd")
    net_value = pionex_net_twd if pionex_net_twd is not None else previous_net
    if net_value is not None and (not math.isfinite(float(net_value)) or float(net_value) <= 0):
        raise RuntimeError("派網淨值必須是正數")

    days[day_key] = {
        "date": day_key,
        "snapshot_at": now.isoformat(timespec="seconds"),
        "period_label": f"{day_key} 08:00 起",
        "pionex_net_twd": round(float(net_value), 2) if net_value is not None else None,
        "pionex_net_captured_at": now.isoformat(timespec="seconds") if pionex_net_twd is not None else day.get("pionex_net_captured_at"),
        "grid_profit_usdt": round(grid_profit, 2),
        "daily_grid_profit_usdt": round(daily_profit, 2),
        "bot_count": len(orders),
        "baseline_gross_by_bot": baseline,
        "baseline_captured_at": baseline_captured_at,
        "baseline_is_true_0800": baseline_is_true_0800,
        "last_gross_by_bot": last_values,
    }
    for row in bot_rows:
        bot_id = row["bot_id"]
        row["daily_grid_profit_usdt"] = round(
            _finite(last_values.get(bot_id, gross_by_bot[bot_id]), f"{bot_id} last gross")
            - _finite(baseline.get(bot_id, gross_by_bot[bot_id]), f"{bot_id} baseline"),
            2,
        )
    data["bots"] = bot_rows
    data["updated_at"] = now.isoformat(timespec="seconds")
    # Keep the visible calendar compact while retaining a full year of source data.
    for old_key in sorted(days)[:-370]:
        days.pop(old_key, None)
    _write_atomic(data)
    # Only a freshly supplied Chrome snapshot may update the dashboard field.
    # Background grid refreshes without --pionex-net-twd must not refresh its timestamp.
    if pionex_net_twd is not None:
        _sync_dashboard_pionex_twd(float(net_value), now)
    result = {
        "ok": True,
        "date": day_key,
        "pionex_net_twd": days[day_key]["pionex_net_twd"],
        "grid_profit_usdt": days[day_key]["grid_profit_usdt"],
        "daily_grid_profit_usdt": days[day_key]["daily_grid_profit_usdt"],
        "bot_count": len(orders),
        "baseline_is_true_0800": baseline_is_true_0800,
    }
    if repaired:
        result["recovered_from"] = "pre-eth-2026-08-27-11:19"
        result["recovered_daily"] = repaired["recovered_daily"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pionex-net-twd", type=float)
    parser.add_argument("--daily-profit-seed", type=float)
    args = parser.parse_args()
    print(json.dumps(collect(args.pionex_net_twd, args.daily_profit_seed), ensure_ascii=False))


if __name__ == "__main__":
    main()
