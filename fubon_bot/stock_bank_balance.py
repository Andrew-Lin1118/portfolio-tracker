#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查詢富邦證券銀行帳戶餘額 → 輸出 JSON
Usage: python stock_bank_balance.py

會回傳：
  - 交割帳戶可用餘額（可買股票用）
  - 銀行存款餘額（今日餘額）
  - 交割日 T+2 預計買賣款
"""
import os, sys, json, datetime, traceback
import yaml

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.yaml")

_FUTURES_TYPES = {"futopt", "future", "futures", "f", "期貨"}

def _safe_str(v):
    """安全地將 SDK 物件轉為字串（SDK 的 __str__ 可能回傳 None）。"""
    if v is None:
        return ""
    try:
        s = str(v)
        if s is None:
            return ""
        return s.strip()
    except Exception:
        return ""

def _is_stock_account(acc):
    atype = _safe_str(getattr(acc, "account_type", "")).lower()
    if atype in _FUTURES_TYPES:
        return False
    ano = _safe_str(getattr(acc, "account", "")).replace("-", "")
    if ano.isdigit() and len(ano) == 7:
        return True
    return True

def _safe_float(obj, *names):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            try: return float(v)
            except: pass
    return None

def main():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    target_acc = _safe_str(cfg.get("account", "")).replace("-", "")

    from fubon_neo.sdk import FubonSDK

    sdk    = FubonSDK()
    result = sdk.apikey_login(
        cfg["personal_id"], cfg["api_key"], cfg["cert_path"], cfg["cert_pass"]
    )
    if not result.is_success:
        print(json.dumps({"error": f"登入失敗: {result.message}"}))
        return

    account  = None
    all_accs = []
    for acc in (result.data or []):
        atype = _safe_str(getattr(acc, "account_type", ""))
        ano   = _safe_str(getattr(acc, "account", "")).replace("-", "")
        aname = _safe_str(getattr(acc, "name", ""))
        all_accs.append({"type": atype, "account": ano, "name": aname})
        if target_acc and (ano == target_acc or target_acc in ano):
            account = acc; break
        if _is_stock_account(acc) and account is None:
            account = acc

    if not account:
        print(json.dumps({
            "error": f"找不到股票帳號 '{target_acc}'，可見: {all_accs}",
        }))
        try:
            sdk.logout()
        except Exception:
            pass
        return

    acc_no   = _safe_str(getattr(account, "account", target_acc)) or target_acc
    acc_name = _safe_str(getattr(account, "name", ""))

    output = {
        "status":        "ok",
        "account":       acc_no,
        "account_name":  acc_name,
        "timestamp":     datetime.datetime.now().isoformat(timespec="seconds"),
        "balance":       {},
        "_all_accounts": all_accs,
    }

    # ── 銀行餘額（嘗試多個 SDK 方法名稱）──────────────────────────────────────
    bank_result = None
    bank_method = None
    for method_name in ("bank_remain", "query_bank_remain", "bank_balance",
                        "query_bank_balance", "get_bank_remain", "bank_remain_query"):
        fn = getattr(sdk.accounting, method_name, None)
        if fn:
            try:
                r = fn(account)
                if getattr(r, "is_success", False):
                    bank_result = r
                    bank_method = method_name
                    break
            except Exception:
                continue

    if bank_result is None:
        acc_methods = [m for m in dir(sdk.accounting) if not m.startswith("_")]
        output["balance"] = {
            "error": "找不到 bank_remain 方法",
            "_accounting_methods": acc_methods,
        }
    else:
        d = bank_result.data
        all_fields = {}
        try:
            for k in dir(d):
                if k.startswith("_"):
                    continue
                try:
                    val = getattr(d, k, None)
                    if not callable(val):
                        all_fields[k] = _safe_str(val)
                except Exception:
                    pass
        except Exception:
            pass

        avail  = _safe_float(d, "available_balance", "availableBalance", "available",
                               "usable_balance", "usableBalance")
        today  = _safe_float(d, "today_balance", "todayBalance", "current_balance",
                               "currentBalance", "bank_balance", "bankBalance")
        buy    = _safe_float(d, "buy_amount",   "buyAmount",   "purchase_amount",  "purchaseAmount")
        sell   = _safe_float(d, "sell_amount",  "sellAmount",  "redemption_amount","redemptionAmount")
        settle = _safe_float(d, "settle_amount","settleAmount","settlement_amount","settlementAmount")
        net    = _safe_float(d, "net_amount",   "netAmount",   "net_balance",      "netBalance")

        output["balance"] = {
            "available":     avail,
            "today_balance": today,
            "buy_amount":    buy,
            "sell_amount":   sell,
            "settle_amount": settle,
            "net_amount":    net,
            "_method":       bank_method,
            "_all_fields":   all_fields,
        }
        output["_balance_method"] = bank_method

    # ── 交割款（T+2 預計） ───────────────────────────────────────────────────
    try:
        for method_name in ("settlement_query", "query_settlement",
                            "query_bank_account", "bank_account"):
            fn = getattr(sdk.accounting, method_name, None)
            if fn:
                try:
                    r2 = fn(account)
                    if r2 and getattr(r2, "is_success", False) and r2.data:
                        output["settlement"] = {
                            "method": method_name,
                            "data":   _safe_str(r2.data),
                        }
                    break
                except Exception:
                    continue
    except Exception as e:
        output["settlement_error"] = str(e)

    print(json.dumps(output, ensure_ascii=False))

    # ── 登出，釋放 SDK 連線名額 ──────────────────────────────────────────────
    try:
        sdk.logout()
    except Exception:
        try:
            sdk.logout()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({
            "error":     str(e),
            "traceback": traceback.format_exc(),
            "balance":   {},
        }, ensure_ascii=False))
