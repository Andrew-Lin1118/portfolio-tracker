#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fmx_quote_daemon.py — 富邦 SDK 即時報價常駐程式
════════════════════════════════════════════════════════════
登入一次 → init_realtime() → 計算近月合約代碼（如 TXFJ6）
→ 每 3 秒查詢所有商品 → 寫入 fmx_live_quotes.json
════════════════════════════════════════════════════════════
"""
import os, sys, json, time, datetime, argparse

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")
OUTPUT_FILE = os.path.join(BASE_DIR, "..", "fmx_live_quotes.json")
LOG_FILE    = os.path.join(BASE_DIR, "..", "logs", "fmx_daemon.log")

POLL_INTERVAL = 5   # 每輪查詢間隔（秒），需搭配每筆 0.35s 延遲避免 429

# 各商品在 Fugle API 的基底代碼（不含月份）
BASE_CODES = {
    "MXFR1": "MXF",
    "TXFR1": "TXF",
    "MTXR1": "MTX",
    "GDF":   "GDF",
    "BRF":   "BRF",
    "ZEF":   "ZEF",
    "ZFF":   "ZFF",
    "TF":    "TF",
    "TE":    "TE",
}

MIN_PRICE = {
    "MXFR1": 10000, "TXFR1": 10000, "MTXR1": 10000,
    "TE": 500, "TF": 200, "ZEF": 50, "ZFF": 20,
    "GDF": 1000, "BRF": 30,
}

# TAIFEX 月份代碼
_MONTH_CODES = {
    1:'F', 2:'G', 3:'H', 4:'J', 5:'K', 6:'M',
    7:'N', 8:'Q', 9:'U', 10:'V', 11:'X', 12:'Z',
}


def _log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
        return None if f != f else f
    except (ValueError, TypeError):
        try:
            return float(str(v).replace(",", "").strip())
        except Exception:
            return None


def _inspect(obj) -> dict:
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
        if r:
            return dict(r)
    except Exception:
        pass
    result = {}
    for attr in dir(obj):
        if attr.startswith("_"):
            continue
        try:
            val = getattr(obj, attr)
            if not callable(val):
                result[attr] = val
        except Exception:
            pass
    return result


def _near_month_suffix(ref_date=None) -> str:
    """
    計算當前近月合約後綴（如 'J6' 代表 2026年4月）。
    TAIFEX 期貨結算日 = 月份第三個周三。
    """
    today = ref_date or datetime.date.today()

    def third_wednesday(y, m):
        d = datetime.date(y, m, 1)
        # weekday(): Mon=0 ... Wed=2
        delta = (2 - d.weekday()) % 7
        return d + datetime.timedelta(days=delta + 14)  # 第3個

    settle = third_wednesday(today.year, today.month)
    if today >= settle:
        # 已過結算，用下個月
        if today.month == 12:
            y, m = today.year + 1, 1
        else:
            y, m = today.year, today.month + 1
    else:
        y, m = today.year, today.month

    return _MONTH_CODES[m] + str(y % 10)   # e.g. 'J6'


def _discover_symbols(rest_client) -> dict:
    """
    呼叫 tickers(type='FUTURE') 取得所有在市合約。
    依 endDate 排序取近月合約，回傳 {base_code: near_month_symbol}。
    """
    result = {}
    try:
        raw   = rest_client.futopt.intraday.tickers(type='FUTURE')
        d     = _inspect(raw) if not isinstance(raw, dict) else raw
        items = d.get("data") or d.get("tickers") or []
        if not items and isinstance(raw, list):
            items = raw
        _log(f"tickers() 共 {len(items)} 筆合約")

        today_str = datetime.date.today().isoformat()

        # 按 base code 前綴和 endDate 整理
        from collections import defaultdict
        bucket: dict[str, list] = defaultdict(list)

        # 名稱關鍵字對應（當前綴搜尋失敗時使用）
        NAME_KEYWORDS = {
            "MTX": ["小型台指", "小台指", "小台", "台指期"],
            "TF":  ["金融期貨"],
            "TE":  ["電子期貨"],
        }

        parsed = []  # list of (sym, end, name)
        for item in items:
            it   = _inspect(item) if not isinstance(item, dict) else item
            sym  = str(it.get("symbol","") or "")
            end  = str(it.get("endDate","") or "")
            name = str(it.get("name","") or "")
            if sym:
                parsed.append((sym, end, name))

        all_bases = list(BASE_CODES.values())
        for sym, end, name in parsed:
            for base in all_bases:
                if sym.upper().startswith(base.upper()):
                    bucket[base].append((end, sym))

        # 取近月（endDate 最小且 >= 今天）
        for base, items_list in bucket.items():
            valid = [(e, s) for e, s in items_list if e >= today_str]
            if not valid:
                valid = items_list
            valid.sort(key=lambda x: x[0])
            result[base] = valid[0][1]

        _log(f"tickers() 前綴近月: {result}")

        # 若部分 base code 未找到，嘗試名稱關鍵字比對
        already_mapped = set(result.values())  # 避免將已映射的合約重複指派給其他 base
        for base, keywords in NAME_KEYWORDS.items():
            if base not in result:
                candidates = []
                for sym, end, name in parsed:
                    if sym in already_mapped:
                        continue   # 已被其他 base 使用，跳過
                    if any(kw in name for kw in keywords) and (end >= today_str):
                        # 確保不是小型版本混入（TF 不要誤收 ZFF）
                        if base == "TF" and "小型" in name:
                            continue
                        if base == "TE" and "小型" in name:
                            continue
                        candidates.append((end, sym))
                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    result[base] = candidates[0][1]
                    already_mapped.add(result[base])
                    _log(f"  {base} 透過名稱找到: {result[base]}")
                else:
                    _log(f"  {base} 名稱搜尋也未找到，keywords={keywords}")
                    _log(f"  含 {base} 的所有符號: {[s for s,e,n in parsed if base.upper() in s.upper()][:10]}")
                    # MTX（小台指）在 Fugle 中可能無獨立合約，退而使用 TXF 合約
                    if base == "MTX" and "TXF" in result:
                        result[base] = result["TXF"]
                        _log(f"  MTX 使用 TXF 合約作備用: {result[base]}")

        _log(f"tickers() 最終近月: {result}")

    except Exception as e:
        _log(f"tickers() 失敗: {type(e).__name__}: {str(e)[:200]}")
    return result


# 候選欄位（依 Fugle API 實際回傳欄位名稱排序）
_LAST_F = ["lastPrice","closePrice","last_price","close_price","tradePrice","currentPrice","price"]
_REF_F  = ["previousClose","referencePrice","prevClose","reference_price","basePrice"]
_CHG_F  = ["change","changePrice","priceChange","Change"]
_PCTF   = ["changePercent","changeRate","change_percent","change_rate"]
_BID_F  = ["bid","bidPrice","bid_price","Bid"]
_ASK_F  = ["ask","askPrice","ask_price","Ask"]
_HIGH_F = ["highPrice","high_price","high","High"]
_LOW_F  = ["lowPrice","low_price","low","Low"]
_OPEN_F = ["openPrice","open_price","open","Open"]
_VOL_F  = ["tradeVolume","volume","totalVolume","Volume","total_volume"]
_OI_F   = ["openInterest","open_interest","oi"]
_MON_F  = ["contractMonth","contract_month","expiryDate","expiry_date","maturity"]


def _pick(d: dict, fields: list, min_val=None):
    for f in fields:
        v = _num(d.get(f))
        if v is None:
            continue
        if min_val is not None and v < min_val:
            continue
        return v
    return None


def _query_rest(rest_client, prod_key: str, symbol: str, first_call=False) -> dict | None:
    min_px = MIN_PRICE.get(prod_key, 0)
    try:
        raw = rest_client.futopt.intraday.quote(symbol=symbol)
        d   = _inspect(raw) if not isinstance(raw, dict) else raw
        if "data" in d and isinstance(d["data"], dict):
            d = d["data"]
        if not d:
            return None

        if first_call:
            _log(f"  [{symbol}] 首次欄位: { {k: str(v)[:30] for k, v in d.items()} }")

        # Fugle API 有 `total`（str repr of dict）和 `lastTrade`（str repr of dict）
        # 嘗試解析 total 取 tradeVolume
        total_raw = d.get("total", "")
        if total_raw and isinstance(total_raw, str) and "tradeVolume" in total_raw:
            try:
                import ast
                t = ast.literal_eval(total_raw)
                if isinstance(t, dict) and "tradeVolume" in t:
                    d["tradeVolume"] = t["tradeVolume"]
            except Exception:
                pass
        # 解析 lastTrade 取 bid/ask
        lt_raw = d.get("lastTrade", "")
        if lt_raw and isinstance(lt_raw, str) and "bid" in lt_raw:
            try:
                import ast
                lt = ast.literal_eval(lt_raw)
                if isinstance(lt, dict):
                    if "bid" in lt: d.setdefault("bid", lt["bid"])
                    if "ask" in lt: d.setdefault("ask", lt["ask"])
            except Exception:
                pass

        last = _pick(d, _LAST_F, min_val=min_px or None)
        if last is None:
            return None

        ref  = _pick(d, _REF_F,  min_val=min_px or None)
        chg  = _pick(d, _CHG_F)
        pct  = _pick(d, _PCTF)
        bid  = _pick(d, _BID_F,  min_val=min_px or None)
        ask  = _pick(d, _ASK_F,  min_val=min_px or None)
        high = _pick(d, _HIGH_F, min_val=min_px or None)
        low  = _pick(d, _LOW_F,  min_val=min_px or None)
        opn  = _pick(d, _OPEN_F, min_val=min_px or None)
        vol  = _pick(d, _VOL_F)
        oi   = _pick(d, _OI_F)

        month_raw = str(next((d[f] for f in _MON_F if d.get(f)), "") or "")
        # 統一轉為 YYYYMM 格式
        if len(month_raw) == 6 and month_raw.isdigit():
            month = month_raw          # "202605" → keep
        elif len(month_raw) == 4 and month_raw.isdigit():
            month = "20" + month_raw   # "2605" → "202605"
        else:
            month = month_raw
        # Fugle 合約代碼推算月份：末2位 = [TAIFEX月份字母][年份數字]
        # TAIFEX月份代碼（非字母順序）：F=1,G=2,H=3,J=4,K=5,M=6,N=7,Q=8,U=9,V=10,X=11,Z=12
        _TAIFEX_MONTH_DICT = {
            'F':1,'G':2,'H':3,'J':4,'K':5,'M':6,
            'N':7,'Q':8,'U':9,'V':10,'X':11,'Z':12,
        }
        if not month and len(symbol) >= 2:
            _a = symbol[-2].upper()
            _y = symbol[-1]
            _mo = _TAIFEX_MONTH_DICT.get(_a)
            if _mo and _y.isdigit():
                month = f"{2020 + int(_y)}{str(_mo).zfill(2)}"

        if ref and last and chg is None:
            chg = round(last - ref, 1)
        if ref and last and pct is None and ref != 0:
            pct = round((last / ref - 1) * 100, 2)

        return {
            "symbol": symbol, "last": last,
            "bid": bid, "ask": ask, "ref": ref, "open": opn,
            "change": chg, "change_pct": pct,
            "volume": vol, "open_interest": oi,
            "high": high, "low": low, "month": month,
        }
    except Exception as e:
        err = str(e)
        if "404" not in err or first_call:
            _log(f"  [{symbol}] 錯誤: {type(e).__name__}: {err[:120]}")
        return None


def _query_hybrid(sdk, account) -> dict:
    """備用：從持倉資料取得市場價格"""
    result = {}
    try:
        r = sdk.futopt_accounting.query_hybrid_position(account)
        if not r.is_success:
            return {}
        for item in (r.data or []):
            sym   = str(getattr(item, "symbol", "") or "").strip()
            last  = _num(getattr(item, "market_price", None))
            avg   = _num(getattr(item, "price", None))
            qty   = _num(getattr(item, "orig_lots", None))
            cont  = str(getattr(item, "expiry_date", "") or "").strip()
            # expiry_date 可能是 "202605"(YYYYMM) 或 "20260516"(YYYYMMDD) 或 "2605"(YYMM)
            # 統一存為 "YYYYMM" 格式供月份判斷
            if len(cont) >= 6 and cont[:4].isdigit():
                month = cont[:6]        # "202605" or "20260516" → "202605"
            elif len(cont) == 4 and cont.isdigit():
                month = "20" + cont     # "2605" → "202605"
            else:
                month = cont
            if sym and last:
                result[sym] = {"last": last, "volume": qty, "month": month, "_avg_cost": avg}
    except Exception as e:
        _log(f"hybrid_position 備用錯誤: {e}")
    return result


def _write_atomic(data: dict):
    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, OUTPUT_FILE)


def run_daemon(cfg: dict):
    _log("啟動富邦 SDK…")
    from fubon_neo.sdk import FubonSDK

    sdk = FubonSDK()
    login = sdk.apikey_login(
        cfg["personal_id"], cfg["api_key"],
        cfg["cert_path"],   cfg["cert_pass"],
    )
    if not login.is_success:
        _log(f"登入失敗: {login.message}"); sys.exit(1)

    target_acc  = str(cfg.get("futures_account", "") or "").replace("-", "").strip()
    fut_account = None
    for acc in (login.data or []):
        atype = (getattr(acc, "account_type", "") or "").lower()
        ano   = str(getattr(acc, "account",      "") or "").replace("-", "").strip()
        if atype in ("futopt", "future", "futures"):
            if not target_acc or target_acc in ano or ano in target_acc:
                fut_account = acc; break

    if not fut_account:
        _log("找不到期貨帳號"); sys.exit(1)
    _log(f"登入成功，帳號: {getattr(fut_account, 'account', '?')}")

    # ── 初始化 marketdata REST ────────────────────────────────────────
    rest_client = None
    try:
        sdk.init_realtime()
        rest_client = sdk.marketdata.rest_client
        _log("marketdata REST API 初始化成功")
    except Exception as e:
        _log(f"marketdata 初始化失敗: {e}，改用持倉備用模式")

    # ── 查詢持倉，取得實際持有的合約月份 ──────────────────────────────
    held_month = ""   # e.g. "202605"
    try:
        hybrid = _query_hybrid(sdk, fut_account)
        fitm = hybrid.get("FITM") or {}
        hm = str(fitm.get("month", "") or "").strip()
        # 確保為 YYYYMM 格式
        if len(hm) == 4 and hm.isdigit():
            hm = "20" + hm
        if hm and len(hm) >= 6 and hm[:4].isdigit():
            held_month = hm[:6]
            _log(f"持倉合約月份: {held_month}")
    except Exception as e:
        _log(f"持倉月份查詢失敗: {e}")

    # ── 計算近月合約後綴，並嘗試從 tickers() 確認實際代碼 ──────────
    suffix = _near_month_suffix()   # e.g. 'J6'
    _log(f"計算近月後綴: {suffix}")

    # 若持倉月份與近月不同，優先使用持倉月份（如：用戶已提前換倉至下月）
    if held_month and len(held_month) >= 6:
        try:
            hm_month = int(held_month[4:6])
            hm_year  = int(held_month[:4])
            held_suffix = _MONTH_CODES.get(hm_month, '') + str(hm_year % 10)
            if held_suffix and held_suffix != suffix:
                _log(f"持倉月份 {held_month} ({held_suffix}) 與近月 {suffix} 不同，改用持倉月份")
                suffix = held_suffix
        except Exception:
            pass

    # 商品 → 實際合約代碼 mapping
    products: dict[str, str] = {}
    discovered: dict[str, str] = {}

    if rest_client:
        discovered = _discover_symbols(rest_client)
        if discovered:
            _log(f"tickers() 發現: {discovered}")

    for prod_key, base in BASE_CODES.items():
        if base in discovered:
            products[prod_key] = discovered[base]
        else:
            products[prod_key] = base + suffix  # 計算代碼

    # 若持倉為特定月份且 tickers() 解析出的合約不同，覆蓋 MXF 為持倉月份
    if held_month and "MXF" in discovered:
        disc_sym = discovered["MXF"]
        held_sym = "MXF" + suffix
        if disc_sym != held_sym:
            products["MXFR1"] = held_sym
            _log(f"MXFR1 覆蓋為持倉月份合約: {held_sym}（tickers 解析為 {disc_sym}）")

    _log(f"使用商品代碼: {products}")
    _log(f"開始輪詢（每 {POLL_INTERVAL}s）")

    poll_count  = 0
    error_count = 0
    first_call  = True

    while True:
        try:
            # 每天 08:45 重新計算近月代碼（可能換月）
            now      = datetime.datetime.now()
            is_night = now.hour >= 15 or now.hour < 5
            if now.hour == 8 and 44 <= now.minute <= 46:
                new_suffix = _near_month_suffix()
                # 重新查詢持倉月份
                try:
                    _h = _query_hybrid(sdk, fut_account)
                    _fm = str((_h.get("FITM") or {}).get("month", "") or "").strip()
                    if len(_fm) == 4 and _fm.isdigit(): _fm = "20" + _fm
                    if _fm and len(_fm) >= 6:
                        _hm = int(_fm[4:6]); _hy = int(_fm[:4])
                        _hs = _MONTH_CODES.get(_hm, '') + str(_hy % 10)
                        if _hs and _hs != new_suffix:
                            new_suffix = _hs
                            _log(f"持倉月份 {_fm} 與近月不同，使用持倉: {new_suffix}")
                except Exception:
                    pass
                if new_suffix != suffix:
                    suffix = new_suffix
                    for pk, base in BASE_CODES.items():
                        if base not in discovered:
                            products[pk] = base + suffix
                    _log(f"換月！新後綴: {suffix}，重新計算代碼: {products}")

            quotes = {}

            if rest_client:
                for prod_key, symbol in products.items():
                    q = _query_rest(rest_client, prod_key, symbol, first_call=first_call)
                    if q:
                        quotes[prod_key] = q
                    else:
                        quotes[prod_key] = {"symbol": symbol, "error": "no data"}
                    time.sleep(0.35)   # 避免觸發 Fugle API 429 rate limit

                # ── REST API 全部失敗時，改用 hybrid_position 取得至少 last price ──
                all_failed = all(q.get("error") for q in quotes.values())
                if all_failed:
                    hybrid = _query_hybrid(sdk, fut_account)
                    fitm   = hybrid.pop("FITM", None)
                    patched = 0
                    for prod_key, symbol in products.items():
                        base = BASE_CODES[prod_key]
                        # 嘗試匹配：完整合約代碼 → base code 前綴 → FITM (MXF 專用)
                        src = hybrid.get(symbol)
                        if not src:
                            for hsym, hdata in hybrid.items():
                                if hsym.upper().startswith(base.upper()):
                                    src = hdata
                                    break
                        if not src and prod_key == "MXFR1" and fitm:
                            src = fitm
                        if src and src.get("last"):
                            min_px = MIN_PRICE.get(prod_key, 0)
                            if min_px and src["last"] < min_px:
                                continue
                            quotes[prod_key] = {
                                "symbol": symbol, "last": src["last"],
                                "bid": None, "ask": None, "ref": None, "open": None,
                                "change": None, "change_pct": None,   # 持倉無法提供每日漲跌
                                "volume": src.get("volume"), "open_interest": None,
                                "high": None, "low": None, "month": src.get("month", ""),
                                "_source": "hybrid_position",
                            }
                            patched += 1
                    if patched and (poll_count % 10 == 0):
                        _log(f"REST 全部失敗，hybrid_position 補救 {patched} 筆")
            else:
                # 備用：hybrid_position（REST client 初始化失敗）
                hybrid = _query_hybrid(sdk, fut_account)
                fitm   = hybrid.pop("FITM", None)
                for prod_key, symbol in products.items():
                    base  = BASE_CODES[prod_key]
                    src = hybrid.get(symbol)
                    if not src:
                        for hsym, hdata in hybrid.items():
                            if hsym.upper().startswith(base.upper()):
                                src = hdata
                                break
                    if not src and prod_key == "MXFR1" and fitm:
                        src = fitm
                    if src and src.get("last"):
                        min_px = MIN_PRICE.get(prod_key, 0)
                        if min_px and src["last"] < min_px:
                            continue
                        quotes[prod_key] = {
                            "symbol": symbol, "last": src["last"],
                            "bid": None, "ask": None, "ref": None, "open": None,
                            "change": None, "change_pct": None,
                            "volume": src.get("volume"), "open_interest": None,
                            "high": None, "low": None, "month": src.get("month",""),
                            "_source": "hybrid_position",
                        }

            first_call  = False
            poll_count += 1
            ok_count    = sum(1 for q in quotes.values() if q.get("last") and not q.get("error"))

            _write_atomic({
                "quotes": quotes, "ts": now.isoformat(timespec="seconds"),
                "night": is_night, "status": "ok",
                "poll": poll_count, "ok_syms": ok_count,
            })
            error_count = 0

            if poll_count % 10 == 1:
                parts = [f"{k}:{v['last']:.0f}" for k, v in quotes.items()
                         if v.get("last") and not v.get("error")]
                _log(f"Poll #{poll_count} ok={ok_count}/{len(products)}: {' | '.join(parts) or '(無資料)'}")

        except Exception as e:
            error_count += 1
            _log(f"輪詢錯誤 #{error_count}: {type(e).__name__}: {e}")
            if error_count >= 5:
                _log("連續 5 次錯誤，退出（由 server 重啟）"); sys.exit(2)
            time.sleep(10); continue

        time.sleep(POLL_INTERVAL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=CONFIG_FILE)
    args = parser.parse_args()
    try:
        import yaml
        with open(args.config, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        _log(f"讀取設定失敗: {e}"); sys.exit(1)
    run_daemon(cfg)


if __name__ == "__main__":
    main()
