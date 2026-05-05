"""
Daily brief — Stage 1: 持倉/觀察清單原型新聞抓取

來源：
  1. yfinance.Ticker.news（主要：US 股、ETF、KR 股、crypto）
  2. Google News RSS（補充：覆蓋率不足的標的）

輸出：data/news_raw.json
  {
    "generated": "ISO timestamp",
    "lookback_hours": 24,
    "symbols": {
      "<SYMBOL>": {
        "news": [{"title", "url", "publisher", "ts", "source"}],
        "errors": []
      }
    }
  }

排程建議：GH Actions cron `0 23 * * *` (UTC) = TW 7:00
"""
import json
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent
LOOKBACK_HOURS = 24
HTTP_TIMEOUT = 15
SLEEP_BETWEEN = 0.4

# 22 prototypes（持倉 + 觀察清單覆蓋）
PROTO_SYMBOLS = [
    "NVDA", "AMD", "TSM", "AVGO", "MU", "COHR", "AAOI", "AXTI",
    "STX", "SNDK", "WDC", "LITE", "META", "GOOGL", "ORCL", "PLTR",
    "TSLA", "ASTS", "NBIS", "SOXX",
    "005930.KS", "000660.KS",
]
CRYPTO_SYMBOLS = ["BTC-USD", "ETH-USD"]
ALL_SYMBOLS = PROTO_SYMBOLS + CRYPTO_SYMBOLS

GNEWS_BASE = "https://news.google.com/rss/search"
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0",
}

# 標的對應的 Google News 搜尋詞（無對應就用 symbol 本身）
GNEWS_QUERY = {
    "005930.KS": "Samsung Electronics 005930",
    "000660.KS": "SK Hynix 000660",
    "BTC-USD":   "Bitcoin BTC price",
    "ETH-USD":   "Ethereum ETH price",
    "SOXX":      "iShares Semiconductor ETF SOXX",
    "ASTS":      "AST SpaceMobile ASTS",
    "NBIS":      "Nebius NBIS",
    "AAOI":      "Applied Optoelectronics AAOI",
    "AXTI":      "AXT Inc AXTI",
    "LITE":      "Lumentum LITE",
    "SNDK":      "SanDisk SNDK",
}


def _parse_epoch(value):
    """支援 epoch (int/float)、ISO 8601 字串"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _parse_rfc822(s):
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _yf_news(symbol):
    """yfinance.Ticker.news → list of normalized dicts；新版返回 nested {content:{}}, 舊版 flat"""
    try:
        items = yf.Ticker(symbol).news or []
    except Exception as e:
        return [], [f"yfinance: {type(e).__name__}: {e}"]

    out = []
    cutoff = time.time() - LOOKBACK_HOURS * 3600
    for raw in items:
        node = raw.get("content") if isinstance(raw.get("content"), dict) else raw
        title = node.get("title")

        url = None
        click_through = node.get("clickThroughUrl")
        if isinstance(click_through, dict):
            url = click_through.get("url")
        if not url:
            canonical = node.get("canonicalUrl")
            if isinstance(canonical, dict):
                url = canonical.get("url")
        if not url:
            url = node.get("link")

        publisher = ""
        provider = node.get("provider")
        if isinstance(provider, dict):
            publisher = provider.get("displayName") or ""
        if not publisher:
            publisher = node.get("publisher") or ""

        ts_raw = (
            node.get("pubDate")
            or node.get("displayTime")
            or raw.get("providerPublishTime")
        )
        ts = _parse_epoch(ts_raw)

        if not title or not url or not ts:
            continue
        if ts < cutoff:
            continue

        out.append({
            "title": title.strip(),
            "url": url,
            "publisher": publisher.strip(),
            "ts": _iso(ts),
            "source": "yfinance",
        })
    return out, []


def _gnews_rss(query):
    url = f"{GNEWS_BASE}?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        req = urllib.request.Request(url, headers=HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            xml_text = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return [], [f"gnews-fetch: {type(e).__name__}: {e}"]

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return [], [f"gnews-parse: {e}"]

    cutoff = time.time() - LOOKBACK_HOURS * 3600
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        source_el = item.find("source")
        publisher = (
            source_el.text.strip()
            if source_el is not None and source_el.text
            else "Google News"
        )
        ts = _parse_rfc822(pub_date)
        if not title or not link or not ts:
            continue
        if ts < cutoff:
            continue
        out.append({
            "title": title,
            "url": link,
            "publisher": publisher,
            "ts": _iso(ts),
            "source": "gnews",
        })
    return out, []


def _dedupe(items):
    """以正規化 title 開頭 + publisher 為 key 去重；保留先出現的（已依時間排好）"""
    seen = set()
    out = []
    for it in items:
        key = (it["title"].lower()[:100], it["publisher"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def fetch_for_symbol(symbol):
    yf_items, yf_err = _yf_news(symbol)
    query = GNEWS_QUERY.get(symbol, symbol)
    g_items, g_err = _gnews_rss(query)
    merged = _dedupe(sorted(yf_items + g_items, key=lambda x: x["ts"], reverse=True))
    return merged, yf_err + g_err


def main():
    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lookback_hours": LOOKBACK_HOURS,
        "symbols": {},
    }

    for s in ALL_SYMBOLS:
        items, errors = fetch_for_symbol(s)
        out["symbols"][s] = {"news": items, "errors": errors}
        suffix = f" (errors: {errors})" if errors else ""
        print(f"[{s:<12}] {len(items):>3} items{suffix}", flush=True)
        time.sleep(SLEEP_BETWEEN)

    target = ROOT / "news_raw.json"
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(v["news"]) for v in out["symbols"].values())
    print(f"\nWrote {target} — {len(ALL_SYMBOLS)} symbols, {total} news items total", flush=True)


if __name__ == "__main__":
    main()
