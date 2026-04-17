#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查詢台股庫存、帳戶餘額、今日委託 → 輸出 JSON
Usage: python stock_positions.py

一次登入查詢所有資料，登出後結束，避免佔用 SDK 連線名額。

SDK 實際欄位（已驗證）：
  accounting.bank_remain(account).data:
    available_balance, balance, currency, account, branch_no
  accounting.unrealized_gains_and_loses(account).data:
    stock_no, cost_price, today_qty, tradable_qty,
    unrealized_profit, unrealized_loss, buy_sell, order_type, date
  accounting.inventories(account).data:
    stock_no, tradable_qty, today_qty, lastday_qty, date,
    odd (InventoryOdd sub-object with: today_qty, tradable_qty, lastday_qty)
  sdk.logout() — 不帶參數
"""
import os, sys, json, datetime, traceback
import yaml

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.yaml")

_FUTURES_TYPES = {"futopt", "future", "futures", "f", "期貨"}


def _safe_str(v):
    """安全地將 SDK 物件轉為字串（SDK __str__ 可能回傳 None）。"""
    if v is None:
        return ""
    try:
        s = str(v)
        return (s or "").strip() if s is not None else ""
    except Exception:
        return ""


def _safe_int(obj, *names):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            try:
                return int(v)
            except Exception:
                pass
    return 0


def _safe_float(obj, *names):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    return None


def _is_stock_account(acc):
    atype = _safe_str(getattr(acc, "account_type", "")).lower()
    if atype in _FUTURES_TYPES:
        return False
    ano = _safe_str(getattr(acc, "account", "")).replace("-", "")
    if ano.isdigit() and len(ano) == 7:
        return True
    return atype not in _FUTURES_TYPES


def main():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    target_acc = _safe_str(cfg.get("account", "")).replace("-", "")

    from fubon_neo.sdk import FubonSDK
    from fubon_neo.constant import BSAction

    sdk = FubonSDK()
    result = sdk.apikey_login(
        cfg["personal_id"], cfg["api_key"], cfg["cert_path"], cfg["cert_pass"]
    )
    if not result.is_success:
        print(json.dumps({"error": f"登入失敗: {result.message}"}))
        return

    try:
        # ── 尋找股票帳號 ──────────────────────────────────────────────────────
        account = None
        all_accs = []
        for acc in (result.data or []):
            atype = _safe_str(getattr(acc, "account_type", "")).lower()
            ano   = _safe_str(getattr(acc, "account", "")).replace("-", "")
            aname = _safe_str(getattr(acc, "name", ""))
            all_accs.append({"type": atype, "account": ano, "name": aname})
            if target_acc and (ano == target_acc or target_acc in ano):
                account = acc
                break
            if _is_stock_account(acc) and account is None:
                account = acc

        if not account:
            print(json.dumps({
                "error": f"找不到股票帳號 '{target_acc}'",
                "holdings": [], "balance": {},
                "_all_accounts": all_accs,
            }))
            try: sdk.logout()
            except Exception: pass
            return

        acc_no   = _safe_str(getattr(account, "account", "")) or target_acc
        acc_name = _safe_str(getattr(account, "name", ""))

    except Exception as e:
        print(json.dumps({
            "error": str(e), "traceback": traceback.format_exc(),
            "holdings": [], "balance": {},
        }, ensure_ascii=False))
        try: sdk.logout()
        except Exception: pass
        return

    output = {
        "status":        "ok",
        "account":       acc_no,
        "account_name":  acc_name,
        "timestamp":     datetime.datetime.now().isoformat(timespec="seconds"),
        "holdings":      [],
        "balance":       {},
        "bank_balance":  {},
        "orders_today":  [],
        "_all_accounts": all_accs,
    }

    # ══════════════════════════════════════════════════════════════════════════
    # 銀行交割餘額  (sdk.accounting.bank_remain)
    # 已驗證欄位: available_balance, balance, currency
    # ══════════════════════════════════════════════════════════════════════════
    try:
        r = sdk.accounting.bank_remain(account)
        if r.is_success and r.data:
            d = r.data
            avail = _safe_float(d, "available_balance")
            today = _safe_float(d, "balance")
            currency = _safe_str(getattr(d, "currency", "TWD"))

            output["bank_balance"] = {
                "available":     avail,
                "today_balance": today,
                "currency":      currency,
                "_method":       "bank_remain",
            }
            output["balance"] = {
                "available":     avail or 0.0,
                "today_balance": today or 0.0,
            }
        else:
            output["bank_balance"] = {
                "error": _safe_str(getattr(r, "message", "bank_remain failed")),
            }
    except Exception as e:
        output["bank_balance"] = {"error": str(e)}

    # ══════════════════════════════════════════════════════════════════════════
    # 庫存持股  (sdk.accounting.unrealized_gains_and_loses — 主要資料來源)
    # 已驗證欄位: stock_no, cost_price, today_qty, tradable_qty,
    #            unrealized_profit, unrealized_loss
    # 說明: inventories() 將零股放在 odd 子物件，不易取得完整數量。
    #       unrealized_gains_and_loses() 直接回傳合併後的正確數量與成本。
    # ══════════════════════════════════════════════════════════════════════════
    try:
        r = sdk.accounting.unrealized_gains_and_loses(account)
        if r.is_success and r.data:
            for item in r.data:
                try:
                    sym = _safe_str(getattr(item, "stock_no", None))
                    if not sym:
                        continue

                    qty       = _safe_int(item, "today_qty", "qty")
                    tradable  = _safe_int(item, "tradable_qty")
                    cost      = _safe_float(item, "cost_price", "avg_price") or 0.0
                    profit    = _safe_float(item, "unrealized_profit") or 0.0
                    loss      = _safe_float(item, "unrealized_loss") or 0.0
                    unrealized = round(profit - loss, 2)

                    # 推算現價: cost + unrealized / qty
                    cur_price = 0.0
                    if qty > 0 and cost > 0:
                        cur_price = round(cost + unrealized / qty, 2)

                    unrealized_pct = None
                    if cost > 0 and cur_price > 0:
                        unrealized_pct = round((cur_price / cost - 1) * 100, 2)

                    market_value = round(cur_price * qty, 2) if cur_price > 0 else None

                    output["holdings"].append({
                        "symbol":         sym,
                        "name":           sym,
                        "qty":            qty,
                        "lots":           qty // 1000,
                        "odd_qty":        qty % 1000,
                        "tradable_qty":   tradable,
                        "avg_price":      round(cost, 2),
                        "cur_price":      cur_price,
                        "unrealized":     unrealized,
                        "unrealized_pct": unrealized_pct,
                        "market_value":   market_value,
                    })
                except Exception as item_e:
                    output.setdefault("holdings_warnings", []).append(
                        f"{getattr(item, 'stock_no', '?')}: {item_e}"
                    )

            output["_holdings_source"] = "unrealized_gains_and_loses"
        else:
            msg = _safe_str(getattr(r, "message", ""))
            output["holdings_error"] = f"unrealized_gains_and_loses failed: {msg}"
    except Exception as e:
        output["holdings_error"] = f"unrealized_gains_and_loses: {e}"

    # ── 若 unrealized 查詢失敗，fallback 到 inventories（含零股處理）──────
    if output.get("holdings_error"):
        output.pop("holdings_error")
        try:
            r = sdk.accounting.inventories(account)
            if r.is_success and r.data:
                for inv in r.data:
                    try:
                        sym = _safe_str(getattr(inv, "stock_no", None))
                        if not sym:
                            continue

                        # 整股數量
                        qty      = _safe_int(inv, "tradable_qty", "today_qty", "lastday_qty")
                        tradable = _safe_int(inv, "tradable_qty")

                        # 零股數量（odd 子物件）
                        odd_obj = getattr(inv, "odd", None)
                        odd_qty = 0
                        if odd_obj is not None:
                            odd_qty = _safe_int(odd_obj, "today_qty", "tradable_qty",
                                                "lastday_qty")
                            tradable += _safe_int(odd_obj, "tradable_qty")

                        total_qty = qty + odd_qty
                        if total_qty <= 0:
                            continue

                        output["holdings"].append({
                            "symbol":         sym,
                            "name":           sym,
                            "qty":            total_qty,
                            "lots":           total_qty // 1000,
                            "odd_qty":        total_qty % 1000,
                            "tradable_qty":   tradable,
                            "avg_price":      0.0,
                            "cur_price":      0.0,
                            "unrealized":     None,
                            "unrealized_pct": None,
                            "market_value":   None,
                        })
                    except Exception as item_e:
                        output.setdefault("holdings_warnings", []).append(
                            f"inv {getattr(inv, 'stock_no', '?')}: {item_e}"
                        )

                output["_holdings_source"] = "inventories_fallback"
            else:
                msg = _safe_str(getattr(r, "message", ""))
                output["holdings_error"] = f"inventories also failed: {msg}"
        except Exception as e:
            output["holdings_error"] = f"inventories fallback: {e}"

    # ══════════════════════════════════════════════════════════════════════════
    # 今日委託  (sdk.stock.get_order_results)
    # ══════════════════════════════════════════════════════════════════════════
    try:
        r = sdk.stock.get_order_results(account)
        if r.is_success and r.data:
            for o in r.data:
                sym = _safe_str(getattr(o, "stock_no", None))
                bs  = getattr(o, "buy_sell", None)
                side = "buy" if (bs == BSAction.Buy
                                 or _safe_str(bs).lower() in ("buy", "b", "1")) else "sell"
                order_no = _safe_str(getattr(o, "order_no", ""))
                seq_no   = _safe_str(getattr(o, "seq_no",   ""))
                mkt_type = _safe_str(getattr(o, "market_type", ""))
                status_raw = _safe_str(getattr(o, "status", ""))
                # Status codes: 0=待確認/預約, 1=委託中, 2=部份成交, 3=全部成交, 10=撤銷中, 20=已撤銷, 30=已刪除
                STATUS_MAP = {"0":"預約", "1":"委託中", "2":"部份成交",
                              "3":"已成交", "10":"撤銷中", "20":"已撤銷", "30":"已刪除"}
                status_label = STATUS_MAP.get(status_raw, status_raw)
                output["orders_today"].append({
                    "order_no":    order_no or seq_no,   # effective ID for cancel
                    "seq_no":      seq_no,
                    "symbol":      sym,
                    "name":        _safe_str(getattr(o, "stock_name", "")) or sym,
                    "side":        side,
                    "price":       float(getattr(o, "price", 0) or 0),
                    "qty":         int(getattr(o, "quantity", 0) or 0),
                    "filled_qty":  int(getattr(o, "filled_qty", 0) or 0),
                    "status":      status_label,
                    "status_code": status_raw,
                    "market_type": mkt_type,
                    "time":        _safe_str(getattr(o, "last_time", "")),
                })
    except Exception as e:
        output["orders_error"] = str(e)

    print(json.dumps(output, ensure_ascii=False))

    # ── 登出（不帶參數）────────────────────────────────────────────────────
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
            "holdings":  [],
            "balance":   {},
        }, ensure_ascii=False))
