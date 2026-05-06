"""
全球市值前 30 大企業抓取（每日更新）

來源：yfinance Ticker.info / fast_info
候選池：~70 檔涵蓋全球候選；依 USD marketCap 排序取前 30
輸出：data/global_top_caps.json

排程：併入 daily-brief.yml workflow（每天 TW 7am 跑）
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "global_top_caps.json"
TARGET_TOP_N = 30

# 候選池：涵蓋全球可能進前 30 的標的（撒寬一點避免漏網）
CANDIDATES = [
    # ── US 大型科技 / 半導體 ──
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "ORCL", "NFLX",
    "ADBE", "CRM", "AMD", "INTC", "IBM", "CSCO", "QCOM", "MU", "AMAT", "LRCX",
    "TXN", "PANW", "NOW", "SHOP",
    # ── US 金融 ──
    "BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "BLK", "C",
    # ── US 醫療 ──
    "LLY", "UNH", "JNJ", "MRK", "ABBV", "PFE", "TMO", "ABT", "DHR", "BMY",
    # ── US 消費 / 零售 ──
    "WMT", "COST", "HD", "PG", "KO", "PEP", "MCD", "NKE", "DIS", "SBUX", "MCK",
    # ── US 能源 / 工業 ──
    "XOM", "CVX", "CAT", "BA", "GE", "RTX", "HON",
    # ── US 通訊 / 電信 ──
    "T", "VZ", "TMUS", "NEE",
    # ── 亞洲 ──
    "TSM",          # 台積電 ADR
    "005930.KS",    # 三星電子
    "0700.HK",      # 騰訊
    "9988.HK",      # 阿里巴巴 港股
    "BABA",         # 阿里巴巴 ADR
    "0941.HK",      # 中國移動
    "1299.HK",      # 友邦保險 AIA
    "TM",           # Toyota ADR
    "BHP",          # BHP Group
    "BYDDF",        # BYD
    "1810.HK",      # 小米
    # ── 歐洲 ──
    "ASML",         # 艾司摩爾
    "NVO",          # Novo Nordisk
    "NVS",          # Novartis
    "RHHBY",        # Roche
    "SAP",          # SAP
    "MC.PA",        # LVMH
    "SHEL",         # Shell
    "BP",           # BP
    "AZN",          # AstraZeneca
    "TM",           # Toyota
    # ── 中東 ──
    "2222.SR",      # Saudi Aramco（yfinance 支援度待驗）
]


def _safe_get(info, *keys):
    for k in keys:
        v = info.get(k) if isinstance(info, dict) else None
        if v is not None and v != 0:
            return v
    return None


def fetch_one(symbol):
    """回傳 normalized dict 或 None。"""
    try:
        t = yf.Ticker(symbol)
        # 用 fast_info 拿 market_cap / last_price（比 info 快）
        cap = None
        price = None
        currency = "USD"
        try:
            fi = t.fast_info
            cap = getattr(fi, "market_cap", None) or fi.get("market_cap") if hasattr(fi, "get") else getattr(fi, "market_cap", None)
            price = getattr(fi, "last_price", None) or (fi.get("last_price") if hasattr(fi, "get") else None)
            currency = getattr(fi, "currency", None) or (fi.get("currency") if hasattr(fi, "get") else None) or "USD"
        except Exception:
            pass

        # fallback 到 info（會比較慢、但欄位較全）
        info = {}
        if cap is None or price is None:
            try:
                info = t.info or {}
            except Exception:
                info = {}
            if cap is None:
                cap = _safe_get(info, "marketCap")
            if price is None:
                price = _safe_get(info, "currentPrice", "regularMarketPrice", "previousClose")
            if currency in (None, ""):
                currency = info.get("currency") or "USD"

        if not cap:
            return None

        # 漲跌（嘗試從 fast_info regular_market_previous_close 推出）
        prev_close = None
        try:
            prev_close = getattr(t.fast_info, "regular_market_previous_close", None)
        except Exception:
            pass
        if prev_close is None and info:
            prev_close = _safe_get(info, "regularMarketPreviousClose", "previousClose")
        change_pct = None
        if price and prev_close and prev_close > 0:
            change_pct = (float(price) - float(prev_close)) / float(prev_close) * 100

        name = (info.get("shortName")
                or info.get("longName")
                or symbol)

        return {
            "symbol":      symbol,
            "name":        name,
            "market_cap":  float(cap),                 # 報價幣別
            "price":       float(price) if price else 0.0,
            "currency":    currency or "USD",
            "change_pct":  round(change_pct, 2) if change_pct is not None else None,
            "country":     info.get("country", ""),
            "sector":      info.get("sector", ""),
            "industry":    info.get("industry", ""),
            "website":     info.get("website", ""),
        }
    except Exception as e:
        print(f"  [warn] {symbol}: {type(e).__name__}: {e}", flush=True)
        return None


# 用「即期匯率」把 cap 統一換算 USD（USD/local）— 簡化用 yfinance fetch 的價作參考
# 為避免逐筆要 fx，這裡直接用幾個主要幣別到 USD 的近似（需更新可從 prices.json 拉）
FX_TO_USD = {
    "USD": 1.0,
    "TWD": 1 / 31.5,
    "HKD": 1 / 7.85,
    "KRW": 1 / 1380.0,
    "JPY": 1 / 150.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "CNY": 1 / 7.2,
    "SAR": 1 / 3.75,    # 沙特里亞爾（pegged）— Aramco 2222.SR
    "AUD": 0.66,        # 澳洲（BHP 等若以 AUD 計）
    "CHF": 1.13,        # 瑞郎（Roche / Nestlé）
    "DKK": 0.145,       # 丹麥克朗（Novo Nordisk 主市場）
    "INR": 1 / 83.0,    # 印度盧比
}


def cap_to_usd(cap, currency):
    rate = FX_TO_USD.get((currency or "USD").upper(), 1.0)
    return cap * rate


def main():
    print(f"=== 抓取 {len(CANDIDATES)} 個候選 ===", flush=True)
    results = []
    for s in CANDIDATES:
        r = fetch_one(s)
        if r and r["market_cap"] > 0:
            r["market_cap_usd"] = round(cap_to_usd(r["market_cap"], r["currency"]), 0)
            results.append(r)
            try:
                print(f"  OK {s:<12} {str(r['name'])[:30]:<30} cap=${r['market_cap_usd']/1e9:>7.1f}B  ({r['currency']})", flush=True)
            except UnicodeEncodeError:
                print(f"  OK {s:<12} cap=${r['market_cap_usd']/1e9:>7.1f}B  ({r['currency']})", flush=True)
        time.sleep(0.25)

    # 依 USD market_cap 排序、取 top N
    results.sort(key=lambda x: x["market_cap_usd"], reverse=True)
    top = results[:TARGET_TOP_N]

    # 加排名
    for i, r in enumerate(top):
        r["rank"] = i + 1

    final = {
        "generated":       datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "candidate_count": len(CANDIDATES),
        "fetched_count":   len(results),
        "top":             top,
    }
    OUT_PATH.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH} — top {len(top)} of {len(results)} candidates")


if __name__ == "__main__":
    main()
