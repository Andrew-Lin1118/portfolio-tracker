# -*- coding: utf-8 -*-
"""
fetch_economic.py
==================
抓取 FRED 經濟指標並產生 data/economic_data.json，供前端「總體經濟 → 經濟指標」使用。

指標：
  - 失業率 (U-3)：UNRATE，level（直接顯示百分比）
  - CPI 年增率：CPIAUCSL，YoY（自行算 12 個月差）
  - 核心 CPI 年增率：CPILFESL，YoY
  - PCE 年增率：PCEPI，YoY
  - 核心 PCE 年增率：PCEPILFE，YoY
  - PPI 年增率：PPIFIS，YoY

資料來源：
  https://fred.stlouisfed.org/graph/fredgraph.csv?id=XXX
  （後端直連，避開 CORS proxy 不穩問題）

執行：
  python data/fetch_economic.py

排程建議：每日一次即可（FRED 為月資料，月底/月初更新）。
"""
import os, sys, json, datetime, csv, io
import urllib.request, urllib.error

OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'economic_data.json')
# 用真實瀏覽器 UA，避免被 FRED 拒絕
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/csv,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}

# 指標清單：(FRED ID, 顯示名稱, 計算方式)
# kind: 'level' = 直接用最後值；'yoy' = 自行算 12 個月年增率
SERIES = [
    ('UNRATE',   '失業率 (U-3)',     'level'),
    ('CPIAUCSL', 'CPI 年增率',       'yoy'),
    ('CPILFESL', '核心 CPI 年增率',  'yoy'),
    ('PCEPI',    'PCE 年增率',       'yoy'),
    ('PCEPILFE', '核心 PCE 年增率',  'yoy'),
    ('PPIFIS',   'PPI 年增率',       'yoy'),
]


def http_get(url, timeout=12):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', errors='ignore')


def http_get_retry(url, attempts=3, timeout=12):
    """重試 N 次，每次失敗 sleep 2 秒"""
    last_err = None
    import time
    for i in range(attempts):
        try:
            return http_get(url, timeout=timeout)
        except Exception as e:
            last_err = e
            print(f'    [retry {i+1}/{attempts}] {type(e).__name__}: {str(e)[:80]}', flush=True)
            if i < attempts - 1:
                time.sleep(2)
    raise last_err


def fetch_fred_csv(series_id):
    # FRED CSV：直連最穩；若失敗備援抓 series JSON 端點（非官方）
    primary = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}'
    text = http_get_retry(primary)
    rows = list(csv.reader(io.StringIO(text)))
    out = []
    for row in rows[1:]:  # 跳過 header
        if len(row) < 2 or row[1] in ('.', '', None):
            continue
        try:
            d = row[0].strip()
            v = float(row[1])
            out.append({'date': d, 'value': v})
        except ValueError:
            continue
    return out


def compute_yoy(series):
    """假設月資料：第 i 期 vs 第 i-12 期"""
    out = []
    for i in range(len(series)):
        cur = series[i]
        if i >= 12:
            prev = series[i - 12]
            yoy = (cur['value'] - prev['value']) / prev['value'] * 100 if prev['value'] != 0 else None
            out.append({'date': cur['date'], 'value': yoy})
        else:
            out.append({'date': cur['date'], 'value': None})
    return out


def build_indicator(series_id, name, kind):
    try:
        raw = fetch_fred_csv(series_id)
        if not raw:
            return None

        if kind == 'yoy':
            yoy_series = compute_yoy(raw)
            valid = [p for p in yoy_series if p['value'] is not None]
            if not valid:
                return None
            last  = valid[-1]
            prev  = valid[-2] if len(valid) >= 2 else None
            history = valid[-36:]  # 近 36 個月
        else:  # level
            last  = raw[-1]
            prev  = raw[-2] if len(raw) >= 2 else None
            history = raw[-36:]

        return {
            'id':        series_id,
            'name':      name,
            'kind':      kind,
            'date':      last['date'],
            'value':     round(last['value'], 3),
            'prev':      round(prev['value'], 3) if prev else None,
            'mom':       round(last['value'] - prev['value'], 3) if prev else None,  # 月變化（pt）
            'history':   [{'date': h['date'], 'value': round(h['value'], 3)} for h in history],
        }
    except Exception as e:
        print(f'  [{series_id}] 失敗：{e}', flush=True)
        return None


def main():
    print('抓取 FRED 經濟指標（並行）...', flush=True)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    indicators = {sid: None for sid, _, _ in SERIES}

    # 6 個 series 並行抓（耗時從 6×~5s = 30s 縮到 ~5s）
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(build_indicator, sid, name, kind): sid for sid, name, kind in SERIES}
        for fut in as_completed(futures, timeout=180):
            sid = futures[fut]
            try:
                ind = fut.result()
                if ind:
                    indicators[sid] = ind
                    print(f'  ✓ {sid:10s} {ind["date"]} = {ind["value"]}'
                          + (f' (上期 {ind["prev"]}, 月變 {ind["mom"]:+.2f}pt)' if ind['prev'] is not None else ''),
                          flush=True)
                else:
                    print(f'  ✗ {sid:10s} 無資料', flush=True)
            except Exception as e:
                print(f'  ✗ {sid:10s} 例外：{e}', flush=True)

    # 即使全部失敗，也要寫出 JSON（含 generated + 全 null indicators）
    payload = {
        'generated': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'indicators': indicators,
    }
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    ok_count = sum(1 for v in indicators.values() if v is not None)
    print(f'\n寫入 → {OUT_FILE}（{ok_count}/{len(SERIES)} 成功）', flush=True)


if __name__ == '__main__':
    main()
