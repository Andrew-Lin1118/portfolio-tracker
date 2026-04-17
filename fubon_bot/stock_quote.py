#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查詢台股即時報價 → 輸出 JSON
Usage: python stock_quote.py --symbol 2330
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
    return atype not in _FUTURES_TYPES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="2330")
    args = parser.parse_args()

    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    target_acc = str(cfg.get("account", "") or "").replace("-", "").strip()

    from fubon_neo.sdk import FubonSDK

    sdk    = FubonSDK()
    result = sdk.apikey_login(
        cfg["personal_id"], cfg["api_key"], cfg["cert_path"], cfg["cert_pass"]
    )
    if not result.is_success:
        print(json.dumps({"error": f"登入失敗: {result.message}"}))
        return

    try:
        # 尋找股票帳號
        account = None
        for acc in (result.data or []):
            ano = str(getattr(acc, "account", "") or "").replace("-", "").strip()
            if target_acc and (ano == target_acc or target_acc in ano):
                account = acc; break
            if _is_stock_account(acc) and account is None:
                account = acc
        if not account:
            print(json.dumps({"error": "找不到股票帳號"}))
            try:
                sdk.logout()
            except Exception:
                try: sdk.logout()
                except Exception: pass
            return

        r = sdk.stock.query_symbol_quote(account, args.symbol)
        if r.is_success and r.data:
            d   = r.data
            def _f(obj, *names):
                for n in names:
                    v = getattr(obj, n, None)
                    if v is not None:
                        try: return float(v)
                        except: pass
                return 0.0
            def _i(obj, *names):
                for n in names:
                    v = getattr(obj, n, None)
                    if v is not None:
                        try: return int(v)
                        except: pass
                return 0

            last  = _f(d, "last_price",    "lastPrice",   "close")
            bid   = _f(d, "bid_price",     "bidPrice",    "bid")
            ask   = _f(d, "ask_price",     "askPrice",    "ask")
            open_ = _f(d, "open_price",    "openPrice",   "open")
            high  = _f(d, "high_price",    "highPrice",   "high")
            low   = _f(d, "low_price",     "lowPrice",    "low")
            ref   = _f(d, "reference_price","referencePrice","ref_price","ref")
            vol   = _i(d, "volume",        "totalVolume", "total_volume")
            lu    = _f(d, "limitup_price", "limitupPrice","limit_up","limitUp")
            ld    = _f(d, "limitdown_price","limitdownPrice","limit_down","limitDown")

            out = {
                "symbol":      args.symbol,
                "last":        last,
                "bid":         bid,
                "ask":         ask,
                "open":        open_,
                "high":        high,
                "low":         low,
                "ref":         ref,
                "volume":      vol,
                "limit_up":    lu,
                "limit_down":  ld,
                "change":      None,
                "change_pct":  None,
            }
            if ref > 0 and last > 0:
                out["change"]     = round(last - ref, 2)
                out["change_pct"] = round((last / ref - 1) * 100, 2)
            print(json.dumps(out, ensure_ascii=False))
        else:
            print(json.dumps({
                "error":  getattr(r, "message", "查詢失敗"),
                "symbol": args.symbol,
            }))
    finally:
        # 登出，釋放 SDK 連線名額
        try:
            sdk.logout()
        except Exception:
            try:
                sdk.logout()
            except Exception:
                pass


if __name__ == "__main__":
    main()
