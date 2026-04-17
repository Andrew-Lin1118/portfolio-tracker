#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
取消台股委託 → 輸出 JSON
Usage: python stock_cancel.py --order-no "ORD123456"
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
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--order-no", required=True)
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

        # cancel_order needs the OrderResult object, not a raw string.
        # Look up matching order from get_order_results first.
        order_obj = None
        order_id  = args.order_no  # could be order_no or seq_no

        lookup = sdk.stock.get_order_results(account)
        if lookup and getattr(lookup, "is_success", False) and lookup.data:
            orders = lookup.data if isinstance(lookup.data, list) else [lookup.data]
            for o in orders:
                ono  = str(getattr(o, "order_no", "") or "")
                sno  = str(getattr(o, "seq_no",   "") or "")
                if order_id and (ono == order_id or sno == order_id):
                    order_obj = o
                    break

        if order_obj is None:
            print(json.dumps({"error": f"找不到委託 {order_id}"}))
            return

        r = sdk.stock.cancel_order(account, order_obj)
        if r.is_success:
            print(json.dumps({
                "status":  "ok",
                "message": f"委託 {order_id} 已取消",
            }))
        else:
            print(json.dumps({
                "error": getattr(r, "message", "取消委託失敗"),
            }))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
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
