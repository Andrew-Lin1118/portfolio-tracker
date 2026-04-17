# -*- coding: utf-8 -*-
"""
fmx_market_quotes.py — /fmx/market_quotes 端點的資料來源。

設計：
  - 主要：讀取 J:\股市相關\fmx_live_quotes.json （fmx_quote_daemon 已寫入且正確）
  - 輸出：單行 JSON 至 stdout，供 server.py 的 _run_subprocess() 解析
  - 格式：{"quotes": {MXFR1:{...}, TXFR1:{...}, ...}, "ts": ..., "status": "ok",
           "source": "daemon", "age_sec": N}

修正重點（對應前一版 SDK 直查的 bug）：
  1. MXFR1 日盤漲跌/漲跌% 錯誤 → 改用 daemon 已正確計算的 change/change_pct
     （daemon 使用 _session='day' 時正確取日盤結算價為 ref）
  2. MTXR1 顯示 202604W4 週選 → daemon 會過濾週選並以月份期貨 MTXE6 (202605) 回傳
     （若 daemon 該檔查無資料，會自動 fallback 為 TXF 資料，但 month 仍為正確月份）

若 fmx_live_quotes.json 不存在、或資料超過 FRESH_MAX_SEC 秒未更新，回傳
fail 狀態讓前端顯示「查詢失敗」而非錯誤數字。
"""
from __future__ import annotations
import json
import os
import sys
import time
from datetime import datetime

# ── 常數 ────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
LIVE_QUOTES_FILE = os.path.join(ROOT, 'fmx_live_quotes.json')

# 日盤 daemon 每 ~8.5s poll 一次；給 120s 容忍度避免抽風時馬上報錯
FRESH_MAX_SEC = 120

# 夜盤／非交易時段給較寬鬆
FRESH_MAX_SEC_OFFHOURS = 600

# 必要欄位（若 daemon JSON 少掉這些欄位，視為資料不完整）
REQUIRED_FIELDS = ('last', 'change', 'change_pct', 'month')

# 輸出鍵序（讓前端表格對齊）
OUTPUT_ORDER = ('MXFR1', 'TXFR1', 'MTXR1', 'GDF', 'BRF', 'ZEF', 'ZFF', 'TF', 'TE')


def _parse_ts(ts_str):
    """把 ISO-8601 字串轉 epoch 秒；失敗回傳 None。"""
    if not ts_str or not isinstance(ts_str, str):
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return time.mktime(datetime.strptime(ts_str, fmt).timetuple())
        except ValueError:
            continue
    return None


def _is_trading_hours(now=None):
    """粗估：週一到週五 08:45-13:45 日盤、15:00-05:00 夜盤。"""
    t = time.localtime(now) if now is not None else time.localtime()
    # Mon=0 ... Sun=6；週末給 off-hours 寬鬆門檻
    if t.tm_wday >= 5:
        return False
    hm = t.tm_hour * 60 + t.tm_min
    day_start, day_end = 8 * 60 + 45, 13 * 60 + 45
    night_a, night_b = 15 * 60, 24 * 60
    night_c, night_d = 0, 5 * 60
    return (day_start <= hm <= day_end) or (night_a <= hm < night_b) or (0 <= hm <= night_d and night_c == 0)


def _normalize_quote(sym: str, q: dict) -> dict:
    """
    複製 daemon 的 quote dict，確保必要欄位存在；
    對 MTX 特別處理：如果 symbol 是 TXFxx（表示 daemon MTX 查詢失敗 fallback 到 TXF），
    標註 symbol_source='TXF_fallback'，month 保持正確。
    """
    if not isinstance(q, dict):
        return {'error': 'invalid quote object'}

    out = {
        'symbol':      q.get('symbol'),
        'last':        q.get('last'),
        'bid':         q.get('bid'),
        'ask':         q.get('ask'),
        'ref':         q.get('ref'),
        'open':        q.get('open'),
        'change':      q.get('change'),
        'change_pct':  q.get('change_pct'),
        'volume':      q.get('volume'),
        'open_interest': q.get('open_interest'),
        'high':        q.get('high'),
        'low':         q.get('low'),
        'month':       q.get('month'),
        'session':     q.get('_session') or q.get('session'),
    }

    # MTXR1 若 daemon 把 symbol 設成 TXFxx，標記 fallback
    if sym == 'MTXR1' and isinstance(out['symbol'], str) and out['symbol'].startswith('TXF'):
        out['symbol_source'] = 'TXF_fallback'

    # 標示月份型態（排除週選；正常月份應為 6 碼 YYYYMM）
    m = out.get('month')
    if isinstance(m, str) and ('W' in m.upper()):
        # 理論上不該發生（daemon 已過濾），但為保險：標記異常
        out['month_warn'] = 'weekly_contract_detected'

    return out


def _emit(payload):
    """輸出單行 JSON 至 stdout（server.py 會取 last non-empty line）。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.write('\n')
    sys.stdout.flush()


def main():
    # ── 讀 fmx_live_quotes.json ──────────────────────────────
    if not os.path.exists(LIVE_QUOTES_FILE):
        _emit({
            'status': 'fail',
            'error':  'fmx_live_quotes.json 不存在，請確認 fmx_quote_daemon 已啟動',
            'source': 'none',
        })
        return 0

    try:
        with open(LIVE_QUOTES_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        _emit({
            'status': 'fail',
            'error':  f'讀取 fmx_live_quotes.json 失敗: {e}',
            'source': 'none',
        })
        return 0

    if not isinstance(data, dict):
        _emit({
            'status': 'fail',
            'error':  'fmx_live_quotes.json 格式不正確（非 JSON 物件）',
            'source': 'none',
        })
        return 0

    quotes = data.get('quotes')
    if not isinstance(quotes, dict):
        # 舊版格式 fallback：根層直接就是 quotes dict
        quotes = {k: v for k, v in data.items() if isinstance(v, dict) and 'last' in v}

    # ── 檢查新鮮度 ─────────────────────────────────────────
    ts_str = data.get('ts')
    ts_epoch = _parse_ts(ts_str)
    now = time.time()
    age = (now - ts_epoch) if ts_epoch else None

    threshold = FRESH_MAX_SEC if _is_trading_hours() else FRESH_MAX_SEC_OFFHOURS

    if age is not None and age > threshold:
        _emit({
            'status': 'stale',
            'error':  f'資料已 {int(age)} 秒未更新（門檻 {threshold}s），fmx_quote_daemon 可能已停止',
            'source': 'daemon',
            'ts':     ts_str,
            'age_sec': int(age),
            'quotes': {k: _normalize_quote(k, v) for k, v in quotes.items()},
        })
        return 0

    # ── 正常回傳 ───────────────────────────────────────────
    out_quotes = {}
    # 依 OUTPUT_ORDER 優先，其餘附加在後
    for key in OUTPUT_ORDER:
        if key in quotes:
            out_quotes[key] = _normalize_quote(key, quotes[key])
    for key, val in quotes.items():
        if key not in out_quotes:
            out_quotes[key] = _normalize_quote(key, val)

    # 檢查必要欄位完整性
    incomplete = [
        k for k, v in out_quotes.items()
        if not isinstance(v, dict) or any(v.get(f) is None for f in REQUIRED_FIELDS)
    ]

    payload = {
        'status':  'ok' if not incomplete else 'partial',
        'source':  'daemon',
        'ts':      ts_str,
        'age_sec': int(age) if age is not None else None,
        'night':   bool(data.get('night')),
        'poll':    data.get('poll'),
        'ok_syms': data.get('ok_syms'),
        'quotes':  out_quotes,
    }
    if incomplete:
        payload['incomplete'] = incomplete

    _emit(payload)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        # 任何未預期例外 → 一行 JSON 錯誤訊息，不要噴 traceback 到 stdout
        sys.stdout.write(json.dumps({
            'status': 'fail',
            'error':  f'fmx_market_quotes 未預期例外: {type(e).__name__}: {e}',
            'source': 'none',
        }, ensure_ascii=False))
        sys.stdout.write('\n')
        sys.stdout.flush()
        sys.exit(1)
