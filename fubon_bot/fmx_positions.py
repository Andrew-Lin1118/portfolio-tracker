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

    # ── STEP 7b: 交易紀錄留存 + 計算累計已實現損益 ───────────────────────
    # 問題：
    #   - Fubon query_margin_equity.realized_pnl 每日 13:45 結算後重置為 0，
    #     所以不能當作「從開倉起」的累計值
    #   - close_position_record 只給成交紀錄（buy_sell/price/lots），沒有現成 P&L
    #
    # 解法（兩層）：
    #   1. 每次查詢時把 close_position_record 結果合併進 data/fut_trades.json
    #      (去重 by order_no+date+price+buy_sell)，用於長期監控與歷史回放
    #   2. realized_pnl 顯示值：優先讀 fmx_state.json 的 realized_pnl
    #      （策略自己累積，從開倉第一筆起，不受每日結算影響）
    trade_ledger_path = os.path.join(BASE_DIR, "..", "data", "fut_trades.json")
    os.makedirs(os.path.dirname(trade_ledger_path), exist_ok=True)
    range_start = str(cfg.get("realized_pnl_since") or f"{datetime.date.today().year}-04-01")
    # 日期格式轉為 YYYYMMDD
    start_iso = range_start if "-" in range_start else f"{range_start[:4]}-{range_start[4:6]}-{range_start[6:8]}"
    start_ymd = start_iso.replace("-", "")
    end_iso   = datetime.date.today().isoformat()
    end_ymd   = end_iso.replace("-", "")

    close_rows_dbg  = []
    new_trades_count = 0
    today_added_count = 0
    total_ledger     = 0

    # ── 載入現有 ledger（不論兩個 API 成不成功都會用到） ─────────
    ledger = {"trades": [], "updated_at": ""}
    try:
        if os.path.exists(trade_ledger_path):
            with open(trade_ledger_path, encoding="utf-8") as f:
                ledger = json.load(f) or ledger
    except Exception:
        ledger = {"trades": [], "updated_at": ""}

    # 兩個 API 的 order_no 格式不同：
    #   close_position_record → 'a03f8-0000'（含後綴）
    #   get_order_results     → 'a03f8'    （無後綴）
    # 統一補 '-0000' 做 dedup key（確保隔天結算後的 record 不會跟即時 record 重複）
    def _norm_no(no):
        s = str(no or "")
        return s if (not s or s.endswith("-0000")) else (s + "-0000")

    # 把現有 ledger 的 order_no 也 normalize 後做 seen set
    seen = {(_norm_no(t.get("order_no")), t.get("date"), t.get("price"),
             t.get("buy_sell"), t.get("orig_lots")) for t in ledger.get("trades", [])}

    # ─── API #1: close_position_record（已結算成交，含 fee/tax，T+1 延遲）───
    try:
        try:
            r = sdk.futopt_accounting.close_position_record(
                fut_account, start_date=start_ymd, end_date=end_ymd
            )
        except TypeError:
            r = sdk.futopt_accounting.close_position_record(fut_account, start_ymd, end_ymd)

        if hasattr(r, "is_success") and r.is_success:
            rows = r.data or []
            diag.append(f"close_position_record [{start_iso} ~ {end_iso}]: OK → {len(rows)} rows")
            for inv in rows:
                no_norm = _norm_no(getattr(inv, "order_no", "") or "")
                rec = {
                    "date":          str(getattr(inv, "date", "") or ""),
                    "symbol":        str(getattr(inv, "symbol", "") or ""),
                    "buy_sell":      str(getattr(inv, "buy_sell", "")).replace("BSAction.", ""),
                    "position_kind": str(getattr(inv, "position_kind", "") or ""),
                    "order_type":    str(getattr(inv, "order_type", "")).replace("FutOptOrderType.", ""),
                    "orig_lots":     float(getattr(inv, "orig_lots", 0) or 0),
                    "price":         float(getattr(inv, "price", 0) or 0),
                    "tax":           float(getattr(inv, "tax", 0) or 0),
                    "fee":           float(getattr(inv, "transaction_fee", 0) or 0),
                    "order_no":      no_norm,  # normalized 寫回 ledger
                    "expiry":        str(getattr(inv, "expiry_date", "") or ""),
                }
                key = (no_norm, rec["date"], rec["price"], rec["buy_sell"], rec["orig_lots"])
                if key not in seen:
                    ledger.setdefault("trades", []).append(rec)
                    seen.add(key)
                    new_trades_count += 1
                if len(close_rows_dbg) < 5:
                    close_rows_dbg.append(rec)
        else:
            diag.append(f"close_position_record: {getattr(r, 'message', 'failed')}")
    except Exception as e:
        diag.append(f"close_position_record error: {type(e).__name__}: {e}")

    # ─── API #2: get_order_results（當日即時成交，無 fee/tax，含夜盤未結算）───
    # 補強：解 close_position_record T+1 延遲導致夜盤成交當日看不到的問題
    try:
        r2 = sdk.futopt.get_order_results(fut_account)
        if r2 and getattr(r2, "is_success", False):
            today_rows = r2.data or []
            for inv in today_rows:
                # 只取 已成交（filled_lot > 0）
                filled_lot = float(getattr(inv, "filled_lot", 0) or 0)
                if filled_lot <= 0:
                    continue
                no_norm = _norm_no(getattr(inv, "order_no", "") or "")
                # 成交價：filled_money 不一定有，fallback 到 price
                price = float(getattr(inv, "filled_money", 0) or 0)
                if price <= 0:
                    price = float(getattr(inv, "price", 0) or 0)
                rec = {
                    "date":          str(getattr(inv, "date", "") or ""),
                    "time":          str(getattr(inv, "last_time", "") or "")[:8],  # '20:23:33'
                    "symbol":        str(getattr(inv, "symbol", "") or ""),
                    "buy_sell":      str(getattr(inv, "buy_sell", "")).replace("BSAction.", ""),
                    "position_kind": "1",
                    "order_type":    str(getattr(inv, "order_type", "")).replace("FutOptOrderType.", ""),
                    "orig_lots":     filled_lot,
                    "price":         price,
                    "tax":           0.0,   # 即時 API 不回傳 tax；隔天 close_position_record 會 dedup 補上
                    "fee":           0.0,
                    "order_no":      no_norm,
                    "expiry":        str(getattr(inv, "expiry_date", "") or ""),
                    "source":        "get_order_results",   # 標記來源（debug 用）
                }
                key = (no_norm, rec["date"], rec["price"], rec["buy_sell"], rec["orig_lots"])
                if key not in seen:
                    ledger.setdefault("trades", []).append(rec)
                    seen.add(key)
                    today_added_count += 1
            diag.append(f"get_order_results: OK → {len(today_rows)} rows, +{today_added_count} 筆當日新增")
        elif r2 is not None:
            diag.append(f"get_order_results: {getattr(r2, 'message', 'failed')}")
    except Exception as e:
        diag.append(f"get_order_results error: {type(e).__name__}: {e}")

    # ─── 排序 + 寫回（兩個 API 都跑完）───
    try:
        ledger["trades"].sort(key=lambda t: (t.get("date", ""), t.get("order_no", "")))
        ledger["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        ledger["query_range"] = {"start": start_iso, "end": end_iso}
        total_ledger = len(ledger["trades"])
        with open(trade_ledger_path, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
        diag.append(
            f"trade ledger 寫入 {trade_ledger_path}："
            f"+{new_trades_count}（結算）+{today_added_count}（當日即時）/ 總 {total_ledger} 筆"
        )
    except Exception as e:
        diag.append(f"trade ledger 寫入失敗: {e}")

    # ── 已實現損益：錨點 + 未來 FIFO 累加 ────────────────────────────────
    # 設計：
    #   使用者在 Fubon 券商平台查到 2026-04-01 ~ 2026-04-23 平倉淨損益 = 160,967
    #   （11 口、手續費 308、稅 155 已扣）→ 這是「官方認定」的正確基準。
    #
    #   → 錨點 = 160,967 at 2026-04-23
    #   → 錨點時的 open lots 快照 = fmx_state.json 的 contracts @ avg_cost
    #   → 未來每次平倉（order_no 不在 counted set）：
    #       FIFO 從 open_lots 最舊配對（anchor 那筆最舊，先吃完再吃後進的 New）
    #       P&L = (close_price - open_price) * mult * qty − fee − tax
    #   → baseline += incremental_pnl
    #
    # 幾個重要設計：
    #   (a) 首次執行會把現有 ledger 所有 trade 都標成「已處理」，避免把
    #       錨點前的成交重複計入；之後只有新加入 ledger 的 trade 才會被累加。
    #   (b) 狀態持久化在 data/fut_realized_state.json，與 fut_trades.json 分開。
    #   (c) 失敗時 fallback 為 margin_info 原本的 broker realized_pnl（當日值）。
    try:
        realized_state_path = os.path.join(BASE_DIR, "..", "data", "fut_realized_state.json")
        mult_map = {"FITM": 10, "FIMTX": 10, "MTX": 10, "TXF": 200, "TX": 200}

        # ── 首次執行：以 160,967 為錨點初始化 ─────────────────────
        if not os.path.exists(realized_state_path):
            # 先把目前 ledger 裡所有 trade 的 order_no 全部標成已處理
            counted_closes = []
            processed_opens = []
            for t in (ledger.get("trades", []) if 'ledger' in locals() else []):
                ono = t.get("order_no", "")
                if not ono:
                    continue
                if t.get("order_type") == "Close":
                    counted_closes.append(ono)
                elif t.get("order_type") == "New":
                    processed_opens.append(ono)

            # 錨點 open lots 快照：讀 fmx_state.json 目前持倉
            anchor_open_lots = []
            try:
                if os.path.exists(STATE_FILE):
                    with open(STATE_FILE, encoding="utf-8") as f:
                        _st = json.load(f)
                    _contracts = float(_st.get("contracts", 0) or 0)
                    _avg       = float(_st.get("avg_cost", 0) or 0)
                    if _contracts > 0 and _avg > 0:
                        anchor_open_lots.append({
                            "date":     "anchor-2026-04-23",
                            "symbol":   "FITM",
                            "lots":     _contracts,
                            "price":    _avg,
                            "side":     "Buy",
                            "order_no": "__ANCHOR__",
                        })
            except Exception:
                pass

            seed = {
                "baseline_pnl":    160967.0,
                "anchored_at":     "2026-04-23",
                "anchor_note":     "Fubon 券商平倉查詢 4/1~4/23 淨損益小計 (已扣手續費 308 + 稅 155)",
                "multiplier":      mult_map,
                "open_lots":       anchor_open_lots,
                "counted_close_order_nos":  counted_closes,
                "processed_open_order_nos": processed_opens,
                "updated_at":      datetime.datetime.now().isoformat(timespec="seconds"),
            }
            with open(realized_state_path, "w", encoding="utf-8") as f:
                json.dump(seed, f, ensure_ascii=False, indent=2)
            diag.append(f"realized_state 初始化：baseline=160967, 開倉快照 {len(anchor_open_lots)} 筆, 已標記 {len(counted_closes)} 筆 Close / {len(processed_opens)} 筆 New")

        # ── 載入狀態 ───────────────────────────────────
        with open(realized_state_path, encoding="utf-8") as f:
            rs = json.load(f)

        baseline     = float(rs.get("baseline_pnl", 0))
        counted      = set(rs.get("counted_close_order_nos", []))
        proc_opens   = set(rs.get("processed_open_order_nos", []))
        open_lots    = list(rs.get("open_lots", []))
        mult_map     = rs.get("multiplier", mult_map)

        # ── 處理尚未計入的 trades（僅 ledger 有但還沒 counted 的）──────
        new_closes = 0
        new_opens  = 0
        new_delta  = 0.0
        fifo_log   = []

        all_trades = sorted(
            (ledger.get("trades", []) if 'ledger' in locals() else []),
            key=lambda t: (t.get("date", ""), t.get("order_no", ""))
        )

        for t in all_trades:
            ono   = t.get("order_no", "")
            otype = t.get("order_type", "")
            sym   = t.get("symbol", "FITM")
            mult  = mult_map.get(sym, 10)

            if otype == "New":
                if ono in proc_opens:
                    continue
                open_lots.append({
                    "date":     t.get("date", ""),
                    "symbol":   sym,
                    "lots":     float(t.get("orig_lots", 0)),
                    "price":    float(t.get("price", 0)),
                    "side":     t.get("buy_sell", ""),
                    "order_no": ono,
                })
                proc_opens.add(ono)
                new_opens += 1

            elif otype == "Close":
                if ono in counted:
                    continue
                remaining   = float(t.get("orig_lots", 0))
                close_price = float(t.get("price", 0))
                close_side  = t.get("buy_sell", "")  # Sell=平多, Buy=平空
                fee         = float(t.get("fee", 0))
                tax         = float(t.get("tax", 0))
                pnl_close   = 0.0

                # FIFO 配對：Sell Close 配 Buy 開倉，Buy Close 配 Sell 開倉
                want_open_side = "Buy" if close_side == "Sell" else "Sell"
                i = 0
                while remaining > 0 and i < len(open_lots):
                    lot = open_lots[i]
                    if lot.get("symbol") != sym:
                        i += 1; continue
                    lot_side = lot.get("side", "Buy")
                    # 允許 anchor 預設為 Buy（多頭策略）
                    if lot_side != want_open_side:
                        i += 1; continue
                    pair_qty = min(float(lot["lots"]), remaining)
                    if close_side == "Sell":
                        pnl_close += (close_price - float(lot["price"])) * mult * pair_qty
                    else:
                        pnl_close += (float(lot["price"]) - close_price) * mult * pair_qty
                    fifo_log.append({
                        "close_date": t.get("date"), "close_no": ono,
                        "open_date":  lot.get("date"), "open_no": lot.get("order_no"),
                        "qty": pair_qty,
                        "open_price": float(lot["price"]), "close_price": close_price,
                    })
                    lot["lots"] = float(lot["lots"]) - pair_qty
                    remaining  -= pair_qty
                    if lot["lots"] <= 1e-9:
                        open_lots.pop(i)
                    else:
                        i += 1

                pnl_close -= (fee + tax)
                new_delta += pnl_close
                counted.add(ono)
                new_closes += 1

        baseline += new_delta

        # ── 寫回狀態 ───────────────────────────────────
        rs["baseline_pnl"]              = baseline
        rs["open_lots"]                 = open_lots
        rs["counted_close_order_nos"]   = sorted(counted)
        rs["processed_open_order_nos"]  = sorted(proc_opens)
        rs["updated_at"]                = datetime.datetime.now().isoformat(timespec="seconds")
        if new_closes or new_opens:
            rs.setdefault("last_fifo_pairings", [])
            rs["last_fifo_pairings"] = fifo_log[-20:]  # 最近 20 筆配對
        with open(realized_state_path, "w", encoding="utf-8") as f:
            json.dump(rs, f, ensure_ascii=False, indent=2)

        # ── 覆寫前端顯示的 realized_pnl ───────────────────
        margin_info["realized_pnl_broker"]  = margin_info.get("realized_pnl")  # 當日 broker 值
        margin_info["realized_pnl"]         = baseline
        margin_info["realized_pnl_source"]  = "fut_realized_state.json (anchor+FIFO)"
        margin_info["realized_pnl_since"]   = rs.get("anchored_at", "2026-04-01")
        margin_info["realized_pnl_anchor"]  = {
            "value":       float(rs.get("baseline_pnl", baseline)) - new_delta,
            "anchored_at": rs.get("anchored_at"),
            "note":        rs.get("anchor_note", ""),
        }
        margin_info["realized_pnl_delta_new"] = new_delta

        if new_closes or new_opens:
            diag.append(f"realized_pnl 累加：新處理 {new_closes} 筆 Close / {new_opens} 筆 New，Δ={new_delta:+.2f} → baseline={baseline:.2f}")
        else:
            diag.append(f"realized_pnl 維持錨點 baseline={baseline:.2f}（無新交易）")
    except Exception as e:
        diag.append(f"realized_pnl 累加計算失敗: {type(e).__name__}: {e}")

    # ── FINAL OUTPUT (last JSON line — server uses this) ──────────────────
    final = {
        "account":    acc_no,
        "name":       acc_name,
        "query_ts":   datetime.datetime.now().isoformat(timespec="seconds"),
        "positions":  positions,
        "margin":     margin_info,
        "live_quote": live_quote,
        "_diag":      diag,
        "_close_rows_sample": close_rows_dbg,
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
