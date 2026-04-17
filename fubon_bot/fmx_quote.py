#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fmx_quote.py — 查詢富邦期貨即時報價，輸出 JSON 至 stdout。
供 server.py /fmx/quote 端點呼叫。

注意：query_hybrid_position 的 price 欄位是「均攤成本」而非「昨日參考價」，
因此 change / change_pct 不應由此計算。改為留空，由前端合併 MIS 資料補齊。
"""

import os, json, argparse, datetime, sys
import yaml
from fubon_neo.sdk import FubonSDK

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=CONFIG_FILE)
    parser.add_argument("--symbol", default=None, help="期貨代碼，預設讀 config.yaml futures_symbol")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        _out({"error": f"找不到設定檔: {args.config}"}); return

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    symbol = args.symbol or cfg.get("futures_symbol", "MXFR1")

    sdk = FubonSDK()
    result = sdk.apikey_login(
        cfg["personal_id"],
        cfg["api_key"],
        cfg["cert_path"],
        cfg["cert_pass"],
    )
    if not result.is_success:
        _out({"error": f"登入失敗: {result.message}"}); return

    # 尋找期貨帳號
    target_acc  = str(cfg.get("futures_account", "") or "").replace("-", "").strip()
    fut_account = None
    for acc in result.data:
        acc_type = (getattr(acc, "account_type", "") or "").lower()
        acc_no   = str(getattr(acc, "account", "") or "").replace("-", "").strip()
        if acc_type in ("futopt", "future", "futures"):
            if not target_acc or target_acc in acc_no or acc_no in target_acc:
                fut_account = acc
                break

    if not fut_account:
        _out({"error": "找不到期貨帳號"}); return

    output = {
        "symbol": symbol,
        "ts":     datetime.datetime.now().isoformat(timespec="seconds"),
    }

    # ── 1. 嘗試 Fugle marketdata REST API（有正確的 change / ref）──────
    rest_ok = False
    try:
        sdk.init_realtime()
        rest_client = sdk.marketdata.rest_client

        # 先用 tickers() 找出近月合約代碼
        actual_symbol = symbol
        try:
            raw = rest_client.futopt.intraday.tickers(type='FUTURE')
            d = _inspect(raw) if not isinstance(raw, dict) else raw
            items = d.get("data") or d.get("tickers") or []
            if not items and isinstance(raw, list):
                items = raw
            today_str = datetime.date.today().isoformat()
            base_code = symbol.upper().replace("R1", "").replace("R2", "")
            candidates = []
            for item in items:
                it = _inspect(item) if not isinstance(item, dict) else item
                sym = str(it.get("symbol", "") or "")
                end = str(it.get("endDate", "") or "")
                if sym.upper().startswith(base_code) and end >= today_str:
                    candidates.append((end, sym))
            if candidates:
                candidates.sort(key=lambda x: x[0])
                actual_symbol = candidates[0][1]
        except Exception:
            pass

        raw = rest_client.futopt.intraday.quote(symbol=actual_symbol)
        d = _inspect(raw) if not isinstance(raw, dict) else raw
        if "data" in d and isinstance(d["data"], dict):
            d = d["data"]

        if d:
            last = _pick(d, ["lastPrice","closePrice","last_price","close_price","tradePrice","currentPrice","price"])
            ref  = _pick(d, ["previousClose","referencePrice","prevClose","reference_price","basePrice"])
            chg  = _pick(d, ["change","changePrice","priceChange","Change"])
            pct  = _pick(d, ["changePercent","changeRate","change_percent","change_rate"])
            bid  = _pick(d, ["bid","bidPrice","bid_price","Bid"])
            ask  = _pick(d, ["ask","askPrice","ask_price","Ask"])
            high = _pick(d, ["highPrice","high_price","high","High"])
            low  = _pick(d, ["lowPrice","low_price","low","Low"])
            opn  = _pick(d, ["openPrice","open_price","open","Open"])
            vol  = _pick(d, ["tradeVolume","volume","totalVolume","Volume","total_volume"])

            if last and last > 1000:  # 合理性檢查
                if ref and chg is None:
                    chg = round(last - ref, 1)
                if ref and pct is None and ref != 0:
                    pct = round((last / ref - 1) * 100, 2)

                output.update({
                    "last":       last,
                    "bid":        bid,
                    "ask":        ask,
                    "ref":        ref,
                    "open":       opn,
                    "change":     chg,
                    "change_pct": pct,
                    "volume":     vol,
                    "high":       high,
                    "low":        low,
                    "symbol":     actual_symbol,
                    "_source":    "fugle_rest",
                })
                rest_ok = True
    except Exception:
        pass

    # ── 2. REST API 無資料時，退回 hybrid_position（只有 last price，無 change）──
    if not rest_ok:
        try:
            r = sdk.futopt_accounting.query_hybrid_position(fut_account)
            if r.is_success:
                items = r.data or []
                sym_upper = symbol.upper().replace("R1", "").replace("R2", "")
                target = None
                for item in items:
                    isym = str(getattr(item, "symbol", "") or "").upper()
                    if isym == symbol.upper() or isym == sym_upper or \
                       (sym_upper in ("MXF", "MXFR1", "MXFR2") and isym == "FITM"):
                        target = item
                        break
                if target is None and items:
                    target = items[0]

                if target is not None:
                    last = _num(getattr(target, "market_price", None))
                    qty  = _num(getattr(target, "orig_lots", None))
                    output.update({
                        "last":       last,
                        "bid":        None,
                        "ask":        None,
                        "ref":        None,       # 持倉 price 是均攤成本，不是昨日參考價
                        "change":     None,       # 無法從持倉資料計算每日漲跌
                        "change_pct": None,
                        "volume":     qty,
                        "high":       None,
                        "low":        None,
                        "open":       None,
                        "_source":    "hybrid_position",
                    })
                else:
                    output["error"] = "no positions found"
            else:
                output["error"] = getattr(r, "message", "query_hybrid_position failed")
        except Exception as e:
            output["error"] = str(e)

    _out(output)


def _num(v):
    if v is None: return None
    try: return float(v)
    except (ValueError, TypeError): return None


def _pick(d: dict, fields: list, min_val=None):
    for f in fields:
        v = _num(d.get(f))
        if v is None:
            continue
        if min_val is not None and v < min_val:
            continue
        return v
    return None


def _inspect(obj):
    if isinstance(obj, dict):
        return obj
    for m in ("model_dump", "dict"):
        try:
            r = getattr(obj, m)()
            if isinstance(r, dict) and r:
                return r
        except Exception:
            pass
    try:
        r = vars(obj)
        if r: return dict(r)
    except Exception:
        pass
    result = {}
    for attr in dir(obj):
        if attr.startswith("_"): continue
        try:
            val = getattr(obj, attr)
            if not callable(val):
                result[attr] = val
        except Exception:
            pass
    return result


def _out(data: dict):
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
