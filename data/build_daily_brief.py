"""
Daily brief — Stage 2: 用 Claude API 把 news_raw.json 變成中文每日簡報

流程：
  1. 讀 news_raw.json（fetch_news.py 產出）
  2. 讀 symbol_mapping.json（leveraged ETF/proxy → 原型）
  3. 讀 earnings_analysis.json 抽 highlights/warnings 給 LLM 引用
  4. 組 prompt（system + cached context + 當日新聞）→ Claude API
  5. 解析回 JSON、把 URL 接回（用 id 對應）→ data/daily_brief.json

執行：
  $env:ANTHROPIC_API_KEY="sk-ant-..."
  python data/build_daily_brief.py

排程：fetch_news.py → build_daily_brief.py 串在同一個 GH Actions step。
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 12000
MAX_NEWS_PER_SYMBOL = 25  # 送 LLM 前每標的最多保留多少篇

NEWS_RAW_PATH = ROOT / "news_raw.json"
MAPPING_PATH = ROOT / "symbol_mapping.json"
EARNINGS_PATH = ROOT / "earnings_analysis.json"
HOLDINGS_PATH = ROOT / "symbols.json"
OUT_PATH = ROOT / "daily_brief.json"


SYSTEM_PROMPT = """\
你是一名資深賣方研究分析師，把英文財經新聞濃縮成中文摘要，並評估對股價影響。

任務：依使用者持倉清單，整理過去 24 小時新聞，產出每檔重點 + 評分 + 跨持倉訊號。

輸出格式（嚴格 valid JSON，不要 markdown fence、不要解釋）：
{
  "session_label": "昨夜 NY 盤後 + 今日亞股盤前",
  "global_themes": ["1~3 個全球重點主題（中文，每個 1 句）"],
  "cross_holdings_signals": [
    {"theme":"主題標題", "tickers":["NVDA","AMD"], "severity":"low|medium|high", "direction":"+|-|0", "note":"中文說明 1~2 句"}
  ],
  "by_symbol": {
    "<TICKER>": {
      "headlines": [
        {"id": <int>, "summary": "一句中文摘要 ≤ 60 字", "sentiment": "+|-|0"}
      ],
      "theme_summary": "2~3 句中文，總結這檔當日重點",
      "score": -2,
      "linked_warnings_used": ["列出此檔有引用到的 earnings warnings 文字"]
    }
  }
}

評分準則 score：
  +2 強利多（重磅併購、超預期財報、政策利好）
  +1 偏多（分析師上調、產品進展、訂單利好）
   0 中性 / 雜訊 / 無重要新聞
  -1 偏空（分析師下調、競爭壓力、需求疑慮）
  -2 強利空（財報miss、訴訟、地緣政治衝擊）

規則：
  - 只用提供的 headlines 內容，不編造未提及事件
  - 每篇 headline 的 summary 限 60 字內、口語化中文
  - 同主題重複新聞用 cross_holdings_signals 抓出，不要每檔都重述
  - 若某 ticker 無重要新聞：theme_summary = "當日無重要新聞"、score = 0、headlines = []
  - 槓桿 ETF / HK proxy 不要單獨列入 by_symbol（會由前端自動連結原型）
  - 嚴格 JSON：不能有尾逗號、字串裡不能有未跳脫的雙引號
"""


def _load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _build_context_block(mapping, holdings, earnings):
    """組裝 cached prompt 區段：持倉、mapping、可引用的 earnings warnings"""
    lines = []

    lines.append("== 使用者持倉清單 ==")
    lines.append(", ".join(holdings))
    lines.append("")

    lines.append("== 槓桿 ETF / HK proxy → 原型對應 ==")
    for sym, info in mapping.get("leveraged_etf", {}).items():
        lines.append(f"  {sym} → {info['proto']} ({info['name']})")
    for sym, info in mapping.get("hk_proxy", {}).items():
        lines.append(f"  {sym} → {info['proto']} ({info['name']})")
    lines.append("")

    lines.append("== 各標的最新財報重點（可在分析時引用，不要自行擴寫）==")
    for sym, info in earnings.get("data", {}).items():
        h = info.get("highlights") or []
        w = info.get("warnings") or []
        announce = info.get("last_announce_date") or "?"
        if not (h or w):
            continue
        lines.append(f"[{sym}] 最近發布 {announce}")
        for s in h:
            lines.append(f"  + {s}")
        for s in w:
            lines.append(f"  ! {s}")
    return "\n".join(lines)


def _build_news_block(news_raw):
    """組裝當日新聞區段，給每篇分配 id 用於回傳對應"""
    lines = ["== 過去 24h 新聞（依標的分組）=="]
    id_to_url = {}  # id → {url, ticker}
    next_id = 1

    for sym, sdata in news_raw["symbols"].items():
        items = sdata.get("news", [])
        if not items:
            lines.append(f"\n=== {sym} ===\n  (無新聞)")
            continue

        # 已依時間排序（fetch_news.py 排好），取前 N 篇
        items = items[:MAX_NEWS_PER_SYMBOL]
        lines.append(f"\n=== {sym} ===")
        for it in items:
            title = it["title"].replace("\n", " ").strip()
            pub = it["publisher"] or "Unknown"
            ts = it["ts"]
            lines.append(f"  [{next_id}] ({ts}, {pub}) {title}")
            id_to_url[next_id] = {"url": it["url"], "ticker": sym, "ts": ts, "publisher": pub, "title": title}
            next_id += 1

    return "\n".join(lines), id_to_url


def _strip_json_fence(text):
    """LLM 偶爾包 ```json ... ```，保險拆掉"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def call_claude(system_prompt, context_block, news_block):
    """串流模式 — 字元累積到 chunk_size 印一個 dot，避免使用者覺得卡住"""
    client = anthropic.Anthropic(timeout=600.0)  # 10 分鐘 timeout 保險

    print("[claude] sending request (streaming)...", flush=True)

    full_text = []
    chunk_size = 80
    char_count = 0
    dots = 0

    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": context_block,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": news_block,
                    },
                ],
            }
        ],
    ) as stream:
        for text in stream.text_stream:
            full_text.append(text)
            char_count += len(text)
            while char_count >= chunk_size:
                print(".", end="", flush=True)
                char_count -= chunk_size
                dots += 1
                if dots % 50 == 0:
                    print("", flush=True)  # 每 50 dots 換行
        print("", flush=True)
        final = stream.get_final_message()

    usage = final.usage
    print(
        f"[claude] in={usage.input_tokens} cache_read={getattr(usage,'cache_read_input_tokens',0)} "
        f"cache_create={getattr(usage,'cache_creation_input_tokens',0)} out={usage.output_tokens}",
        flush=True,
    )

    return "".join(full_text), {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0),
    }


def merge_urls_back(brief_json, id_to_url):
    """把 LLM 回傳的 headlines[].id 對回 url/publisher/title"""
    by_symbol = brief_json.get("by_symbol", {}) or {}
    for sym, sdata in by_symbol.items():
        merged = []
        for h in sdata.get("headlines", []) or []:
            hid = h.get("id")
            ref = id_to_url.get(hid)
            if not ref:
                continue
            merged.append({
                "title": ref["title"],
                "url": ref["url"],
                "publisher": ref["publisher"],
                "ts": ref["ts"],
                "summary": h.get("summary", ""),
                "sentiment": h.get("sentiment", "0"),
            })
        sdata["headlines"] = merged
    return brief_json


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(2)

    news_raw = _load_json(NEWS_RAW_PATH)
    mapping = _load_json(MAPPING_PATH)
    earnings = _load_json(EARNINGS_PATH)
    holdings = _load_json(HOLDINGS_PATH)

    context_block = _build_context_block(mapping, holdings, earnings)
    news_block, id_to_url = _build_news_block(news_raw)

    print(f"[input] {len(holdings)} holdings, {len(id_to_url)} news items "
          f"(capped {MAX_NEWS_PER_SYMBOL}/symbol)", flush=True)

    raw_text, usage = call_claude(SYSTEM_PROMPT, context_block, news_block)
    cleaned = _strip_json_fence(raw_text)

    try:
        brief = json.loads(cleaned)
    except json.JSONDecodeError as e:
        debug_path = ROOT / "daily_brief_raw_response.txt"
        debug_path.write_text(raw_text, encoding="utf-8")
        print(f"ERROR: failed to parse JSON: {e}", file=sys.stderr)
        print(f"raw response saved to {debug_path}", file=sys.stderr)
        sys.exit(3)

    brief = merge_urls_back(brief, id_to_url)

    today_brief = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "based_on_news_generated": news_raw.get("generated"),
        "model": MODEL,
        "usage": usage,
        **brief,
    }

    # ── 7 天滾動歸檔（schema v2: { schema_version, latest_date, daily: { "YYYY-MM-DD": {...} } }）──
    # 用 TW 時區（GH Actions 在 TW 7am 跑 = UTC 23:00 前一天）取「商業日」
    try:
        from zoneinfo import ZoneInfo
        today_tw = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
    except Exception:
        today_tw = datetime.utcnow().strftime("%Y-%m-%d")

    daily_dict = {}
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                if existing.get("schema_version") == 2 and isinstance(existing.get("daily"), dict):
                    daily_dict = dict(existing["daily"])
                else:
                    # 舊 flat schema → 遷移：把現有當作一筆歷史保留
                    old_date = (existing.get("generated") or "").split("T")[0] or today_tw
                    legacy_blob = {k: v for k, v in existing.items()
                                   if k not in ("schema_version", "latest_date", "daily")}
                    if legacy_blob:
                        daily_dict[old_date] = legacy_blob
        except Exception as e:
            print(f"[warn] failed reading existing daily_brief: {e}", flush=True)

    daily_dict[today_tw] = today_brief

    # 只留最新 7 天（依日期 desc）
    keep_dates = sorted(daily_dict.keys(), reverse=True)[:7]
    daily_dict = {d: daily_dict[d] for d in keep_dates}

    final = {
        "schema_version": 2,
        "latest_date": today_tw,
        "daily": daily_dict,
    }
    OUT_PATH.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH} (schema v2 — 保留 {len(daily_dict)} 天: {', '.join(keep_dates)})", flush=True)
    print(f"  today ({today_tw}): "
          f"global_themes={len(today_brief.get('global_themes', []))} · "
          f"cross_signals={len(today_brief.get('cross_holdings_signals', []))} · "
          f"by_symbol={len(today_brief.get('by_symbol', {}))}", flush=True)


if __name__ == "__main__":
    main()
