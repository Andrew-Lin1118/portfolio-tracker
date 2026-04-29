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
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; portfolio-tracker/1.0)'}

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


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', errors='ignore')


def fetch_fred_csv(series_id):
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}'
    text = http_get(url)
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
    print('抓取 FRED 經濟指標...', flush=True)
    indicators = {}
    for sid, name, kind in SERIES:
        ind = build_indicator(sid, name, kind)
        if ind:
            print(f'  ✓ {sid:10s} {ind["date"]} = {ind["value"]}'
                  + (f' (上期 {ind["prev"]}, 月變 {ind["mom"]:+.2f}pt)' if ind['prev'] is not None else ''),
                  flush=True)
            indicators[sid] = ind
        else:
            indicators[sid] = None

    payload = {
        'generated': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'indicators': indicators,
    }
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f'\n寫入 → {OUT_FILE}', flush=True)


if __name__ == '__main__':
    main()
