#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fmx_positions.py — 查詢富邦期貨庫存 + 保證金
  策略：先用 os.write 輸出 fmx_state.json 估算值 (heartbeat)，再嘗試 SDK 取得即時數據。
  伺服器讀取 stdout 的 **最後一行** JSON 為最終結果。
  即使 SDK 崩潰，至少有 heartbeat 資料可用。
"""

import os, sys, json, datetime, traceback, argparse

# ── 使用原始 fd 寫入，完全繞過 Python IO 層 ────────────────────────────────
def _raw_emit(obj: dict):
    """Write single-line JSON to stdout fd=1 directly (bypasses Python IO entirely)."""
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    os.write(1, line.encode("utf-8"))

# 立即輸出啟動心跳，確保 server.py 至少能讀到一行 JSON
_raw_emit({"status": "starting", "ts": datetime.datetime.now().isoformat(timespec="seconds")})

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
STATE_FILE  = os.path.join(BASE_DIR, "..", "fmx_state.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")


# ── helpers ─────────────────────────────────────────────────────────────────
def _num(v):
    if v is None: return None
    try:    return float(v)
    except: return str(v)


def _load_state_positions(cfg: dict) -> list:
    try:
        if not os.path.exists(STATE_FILE): return []
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        contracts = float(st.get("contracts", 0))
        if not contracts: return []
        return [{
            "symbol":         cfg.get("futures_symbol", "MXFR1"),
            "name":           "微型台指期貨 (策略估算)",
            "direction":      "多",
            "qty":            contracts,
            "tradable":       contracts,
            "avg_price":      _num(st.get("avg_cost")),
            "cur_price":      _num(st.get("last_price")),
            "unrealized_pnl": _num(st.get("unrealized")),
            "maint_margin":   None,
            "contract_month": "",
            "_source":        "fmx_state.json",
        }]
    except Exception:
        return []


# ════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=CONFIG_FILE)
    args = parser.parse_args()

    if not os.path.exists(args.config):
        _raw_emit({"error": f"找不到設定檔: {args.config}", "positions": [], "margin": {}})
        return

    try:
        import yaml
        with open(args.config, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        _raw_emit({"error": f"讀取設定檔失敗: {e}", "positions": [], "margin": {}})
        return

    # ── HEARTBEAT: emit state-file fallback immediately ───────────────────
    fallback_pos = _load_state_positions(cfg)
    _raw_emit({
        "status":    "loading",
        "positions": fallback_pos,
        "margin":    {},
        "note":      "SDK 查詢中，此為策略估算值",
        "query_ts":  datetime.datetime.now().isoformat(timespec="seconds"),
    })

    # ── STEP 1: import SDK ───────────────────────────────────────────────
    diag = []
    try:
        from fubon_neo.sdk import FubonSDK
        diag.append("SDK import OK")
    except Exception as e:
        _raw_emit({"error": f"fubon_neo 匯入失敗: {e}",
                   "traceback": traceback.format_exc(),
                   "positions": fallback_pos, "margin": {}, "_diag": diag})
        return

    # ── STEP 2: login ────────────────────────────────────────────────────
    try:
        sdk = FubonSDK()
        diag.append("FubonSDK() OK")
        result = sdk.apikey_login(
            cfg["personal_id"],
            cfg["api_key"],
            cfg["cert_path"],
            cfg["cert_pass"],
        )
        diag.append(f"apikey_login success={result.is_success}")
    except Exception as e:
        _raw_emit({"error": f"登入例外: {e}",
                   "traceback": traceback.format_exc(),
                   "positions": fallback_pos, "margin": {}, "_diag": diag})
        return

    if not result.is_success:
        _raw_emit({"error": f"登入失敗: {result.message}",
                   "positions": fallback_pos, "margin": {}, "_diag": diag})
        return

    # ── STEP 3: find futures account ─────────────────────────────────────
    target_acc  = str(cfg.get("futures_account", "") or "").replace("-", "").strip()
    fut_account = None
    all_accs    = []
    try:
        for acc in (result.data or []):
            atype = str(getattr(acc, "account_type", "") or "").lower()
            ano   = str(getattr(acc, "account",      "") or "").replace("-", "").strip()
            aname = str(getattr(acc, "name",          "") or "")
            all_accs.append({"type": atype, "account": ano, "name": aname})
            if atype in ("futopt", "future", "futures"):
                if not target_acc or target_acc in ano or ano in target_acc:
                    fut_account = acc
        diag.append(f"accounts: {all_accs}")
    except Exception as e:
        diag.append(f"account loop: {e}")

    if not fut_account:
        _raw_emit({"error": f"找不到期貨帳號 '{target_acc}'. 可見: {all_accs}",
                   "positions": fallback_pos, "margin": {}, "_diag": diag})
        return

    acc_no   = str(getattr(fut_account, "account", target_acc) or target_acc)
    acc_name = str(getattr(fut_account, "name", "") or "")
    diag.append(f"using: {acc_no} / {acc_name}")

    # ── STEP 4: diagnostic ───────────────────────────────────────────────
    try:
        fa_methods = [m for m in dir(sdk.futopt_accounting) if not m.startswith("_")]
        diag.append(f"sdk.futopt_accounting methods: {fa_methods}")
    except Exception as e:
        diag.append(f"dir(sdk.futopt_accounting) error: {e}")

    # ── STEP 5: query positions via futopt_accounting.query_hybrid_position ──
    positions  = []
    inv_errors = []

    # Primary: query_hybrid_position (aggregated per symbol, for market_price)
    hybrid_rows = []
    try:
        r = sdk.futopt_accounting.query_hybrid_position(fut_account)
        if hasattr(r, "is_success") and r.is_success:
            hybrid_rows = r.data or []
            for inv in hybrid_rows:
                positions.append(_parse_hybrid_inv(inv))
            diag.append(f"futopt_accounting.query_hybrid_position: OK → {len(positions)} rows")
        else:
            msg = f"futopt_accounting.query_hybrid_position: {getattr(r, 'message', 'failed')}"
            inv_errors.append(msg); diag.append(msg)
    except Exception as e:
        msg = f"futopt_accounting.query_hybrid_position: {type(e).__name__}: {e}"
        inv_errors.append(msg); diag.append(msg)

    # Also get single positions for accurate P&L (hybrid returns profit_or_loss=0)
    single_pnl = {}   # key = symbol → total_pnl
    try:
        r2 = sdk.futopt_accounting.query_single_position(fut_account)
        if hasattr(r2, "is_success") and r2.is_success:
            for inv in (r2.data or []):
                sym = str(getattr(inv, "symbol", "") or "")
                pnl = float(getattr(inv, "profit_or_loss", 0) or 0)
                single_pnl[sym] = single_pnl.get(sym, 0) + pnl
            diag.append(f"query_single_position PNL: {single_pnl}")
    except Exception as e:
        diag.append(f"query_single_position: {e}")

    # Patch P&L into positions from single position data
    for p in positions:
        sym = p.get("symbol", "")
        if sym in single_pnl:
            p["unrealized_pnl"] = _num(single_pnl[sym])

    # Fallback: query_single_position (individual lots)
    if not positions:
        try:
            r = sdk.futopt_accounting.query_single_position(fut_account)
            if hasattr(r, "is_success") and r.is_success:
                positions = _aggregate_single_positions(r.data or [])
                diag.append(f"futopt_accounting.query_single_position: OK → {len(positions)} aggregated rows")
            else:
                msg = f"futopt_accounting.query_single_position: {getattr(r, 'message', 'failed')}"
                inv_errors.append(msg); diag.append(msg)
        except Exception as e:
            msg = f"futopt_accounting.query_single_position: {type(e).__name__}: {e}"
            inv_errors.append(msg); diag.append(msg)

    if not positions:
        positions = fallback_pos   # use state file if no SDK positions

    # ── STEP 6: live quote — extract market_price from positions ──────────
    symbol     = cfg.get("futures_symbol", "MXFR1")
    live_quote = {}
    # market_price is embedded in hybrid position data
    for p in positions:
        if p.get("cur_price") and not p.get("_source"):
            live_quote = {
                "symbol": p.get("symbol", symbol),
                "last":   p["cur_price"],
            }
            diag.append(f"quote from positions: market_price={p['cur_price']}")
            break

    # ── STEP 7: margin via futopt_accounting.query_margin_equity ─────────
    margin_info = {}
    try:
        r = sdk.futopt_accounting.query_margin_equity(fut_account)
        if hasattr(r, "is_success") and r.is_success:
            # data is a list; pick TWD row (or first row)
            items = r.data or []
            item = None
            for it in items:
                if str(getattr(it, "currency", "")).upper() in ("TWD", "NTD"):
                    item = it; break
            if item is None and items:
                item = items[0]
            if item is not None:
                margin_info = _parse_margin(item)
                diag.append(f"margin via futopt_accounting.query_margin_equity: {margin_info}")
        else:
            diag.append(f"query_margin_equity: {getattr(r, 'message', 'failed')}")
    except Exception as e:
        diag.append(f"query_margin_equity error: {e}")

    # ── FINAL OUTPUT (last JSON line — server uses this) ──────────────────
    final = {
        "account":    acc_no,
        "name":       acc_name,
        "query_ts":   datetime.datetime.now().isoformat(timespec="seconds"),
        "positions":  positions,
        "margin":     margin_info,
        "live_quote": live_quote,
        "_diag":      diag,
    }
    if inv_errors:
        final["inv_errors"] = inv_errors
    _raw_emit(final)


def _bs_direction(bs_val) -> str:
    """Convert BSAction enum or string to 多/空."""
    s = str(bs_val)
    if "buy" in s.lower() or s in ("B", "Buy"):
        return "多"
    if "sell" in s.lower() or s in ("S", "Sell"):
        return "空"
    return s


def _parse_hybrid_inv(inv) -> dict:
    """Parse a query_hybrid_position row (aggregated position per symbol)."""
    def g(*attrs, default=None):
        for a in attrs:
            v = getattr(inv, a, None)
            if v is not None and v != "": return v
        return default

    sym      = g("symbol", "stock_no", "code", default="")
    bs       = g("buy_sell", "direction", "side", default="")
    qty      = g("orig_lots", "quantity", "net_qty", default=0)
    trd      = g("tradable_lot", "tradable_qty", default=qty)
    avg      = g("price", "cost_price", "avg_price", default=0)      # 'price' = avg cost in hybrid pos
    cur      = g("market_price", "current_price", "last_price", default=0)
    pnl      = g("profit_or_loss", "unrealized_profit", "unrealized_pnl")
    marg     = g("maintenance_margin", "clearing_margin")
    cont_raw = g("expiry_date", "contract_month", "expiry", "maturity", default="")
    # Format expiry_date e.g. 202604 → 2604
    cont = str(cont_raw)
    if len(cont) == 6 and cont.isdigit():
        cont = cont[2:]   # 202604 → 2604

    # Symbol alias: FITM → MXFR1 (微型台指期貨近月)
    sym_str = str(sym)
    name = "微型台指期貨" if sym_str == "FITM" else sym_str

    return {
        "symbol":         sym_str,
        "name":           name,
        "direction":      _bs_direction(bs),
        "qty":            _num(qty),
        "tradable":       _num(trd),
        "avg_price":      _num(avg),
        "cur_price":      _num(cur),
        "unrealized_pnl": _num(pnl),
        "maint_margin":   _num(marg),
        "contract_month": cont,
    }


def _aggregate_single_positions(items: list) -> list:
    """Aggregate individual lot rows from query_single_position into per-symbol rows."""
    groups = {}
    for inv in items:
        sym   = str(getattr(inv, "symbol", "") or "")
        bs    = getattr(inv, "buy_sell", "")
        key   = (sym, str(bs))
        if key not in groups:
            groups[key] = []
        groups[key].append(inv)

    result = []
    for (sym, bs), rows in groups.items():
        total_qty = sum(float(getattr(r, "orig_lots", 0) or 0) for r in rows)
        total_trd = sum(float(getattr(r, "tradable_lot", 0) or 0) for r in rows)
        # Weighted average cost
        weighted_sum = sum(
            float(getattr(r, "price", 0) or 0) * float(getattr(r, "orig_lots", 0) or 0)
            for r in rows
        )
        avg_price = weighted_sum / total_qty if total_qty else 0
        cur_price = _num(getattr(rows[0], "market_price", None))
        pnl_sum   = sum(float(getattr(r, "profit_or_loss", 0) or 0) for r in rows)
        marg      = _num(getattr(rows[0], "maintenance_margin", None) or getattr(rows[0], "clearing_margin", None))
        cont_raw  = str(getattr(rows[0], "expiry_date", "") or "")
        cont      = cont_raw[2:] if len(cont_raw) == 6 and cont_raw.isdigit() else cont_raw
        name      = "微型台指期貨" if sym == "FITM" else sym
        result.append({
            "symbol":         sym,
            "name":           name,
            "direction":      _bs_direction(bs),
            "qty":            _num(total_qty),
            "tradable":       _num(total_trd),
            "avg_price":      _num(avg_price),
            "cur_price":      cur_price,
            "unrealized_pnl": _num(pnl_sum),
            "maint_margin":   marg,
            "contract_month": cont,
        })
    return result


def _parse_inv(inv) -> dict:
    """Legacy parser (kept for compatibility, not used by main flow)."""
    def g(*attrs, default=None):
        for a in attrs:
            v = getattr(inv, a, None)
            if v is not None and v != "": return v
        return default
    sym  = g("stock_no", "symbol", "code", default="")
    bs   = g("buy_sell", "direction", "side", default="")
    qty  = g("orig_lots", "quantity", "net_qty", default=0)
    trd  = g("tradable_lot", "tradable_qty", default=qty)
    avg  = g("price", "cost_price", "avg_price", default=0)
    cur  = g("market_price", "current_price", "last_price", default=0)
    pnl  = g("profit_or_loss", "unrealized_profit", "unrealized_pnl")
    marg = g("maintenance_margin", "clearing_margin")
    cont = g("expiry_date", "contract_month", "expiry", "maturity", default="")
    cont_s = str(cont)
    if len(cont_s) == 6 and cont_s.isdigit():
        cont_s = cont_s[2:]
    sym_str = str(sym)
    name = "微型台指期貨" if sym_str == "FITM" else sym_str
    return {
        "symbol": sym_str, "name": name, "direction": _bs_direction(bs),
        "qty": _num(qty), "tradable": _num(trd),
        "avg_price": _num(avg), "cur_price": _num(cur),
        "unrealized_pnl": _num(pnl), "maint_margin": _num(marg),
        "contract_month": cont_s,
    }


def _parse_margin(d) -> dict:
    """Parse Equity object from query_margin_equity."""
    if d is None: return {}
    def g(*attrs):
        for a in attrs:
            v = getattr(d, a, None)
            if v is not None: return _num(v)
        return None
    return {
        "available":         g("available_margin", "excess_margin", "available_balance"),
        "equity":            g("today_equity", "equity", "net_value"),
        "today_balance":     g("today_balance", "current_balance"),
        "yesterday_balance": g("yesterday_balance", "prev_balance"),
        "withdrawal":        g("today_withdrawal", "withdrawal", "disgorgement"),
        "realized_pnl":      g("fut_realized_pnl", "realized_profit", "realized_pnl"),
        "unrealized_pnl":    g("fut_unrealized_pnl", "unrealized_profit", "unrealized_pnl"),
    }


if __name__ == "__main__":
    main()
