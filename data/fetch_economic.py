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
import urllib.request, urllib.error, urllib.parse

# 確保 stdout 為 UTF-8（Windows cmd 預設 cp950，會卡 ✓✗ 等 unicode 字元）
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

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


def http_get(url, timeout=15, force_https_unverified=False):
    import ssl
    req = urllib.request.Request(url, headers=HEADERS)
    if force_https_unverified:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, timeout=timeout, context=ctx).read().decode('utf-8', errors='ignore')
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', errors='ignore')


def http_get_retry(url, attempts=3, timeout=15):
    """重試 N 次，每次失敗 sleep 2 秒"""
    last_err = None
    import time
    for i in range(attempts):
        try:
            return http_get(url, timeout=timeout, force_https_unverified=(i == attempts - 1))
        except Exception as e:
            last_err = e
            print(f'    [retry {i+1}/{attempts}] {type(e).__name__}: {str(e)[:120]}', flush=True)
            if i < attempts - 1:
                time.sleep(2)
    raise last_err


def _parse_fred_csv(text):
    """FRED CSV 格式 → [{date, value}]"""
    rows = list(csv.reader(io.StringIO(text)))
    out = []
    for row in rows[1:]:
        if len(row) < 2 or row[1] in ('.', '', None):
            continue
        try:
            d = row[0].strip()
            v = float(row[1])
            out.append({'date': d, 'value': v})
        except ValueError:
            continue
    return out


def _fetch_dbnomics(series_id):
    """DBnomics 是 FRED 的免費鏡像 API（CORS 友善、不需 key）。
    URL 格式：https://api.db.nomics.world/v22/series/FRED/{ID}?observations=1
    回傳 JSON 結構：series.docs[0].period[] + value[]
    """
    url = f'https://api.db.nomics.world/v22/series/FRED/{series_id}?observations=1'
    print(f'    [dbnomics] {url}', flush=True)
    text = http_get_retry(url, attempts=3, timeout=15)
    import json as _j
    try:
        data = _j.loads(text)
    except Exception:
        raise RuntimeError('DBnomics response not JSON')
    docs = data.get('series', {}).get('docs', [])
    if not docs:
        raise RuntimeError('DBnomics no series.docs')
    doc = docs[0]
    periods = doc.get('period', []) or []
    values  = doc.get('value', []) or []
    out = []
    for p, v in zip(periods, values):
        try:
            if v is None or v == 'NA': continue
            f = float(v)
            if f != f: continue
            # period 可能是 "2026-04" → 轉成 "2026-04-01"
            if isinstance(p, str) and len(p) == 7 and p[4] == '-':
                p = p + '-01'
            out.append({'date': p, 'value': f})
        except Exception:
            continue
    return out


def _fetch_fred_csv(series_id):
    """直連 FRED CSV，多個 URL 備援"""
    urls = [
        f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}',
        f'https://fred.stlouisfed.org/data/{series_id}.csv',
    ]
    last_err = None
    for u in urls:
        try:
            print(f'    [fred-csv] {u}', flush=True)
            text = http_get_retry(u, attempts=2, timeout=20)
            return _parse_fred_csv(text)
        except Exception as e:
            last_err = e
            print(f'      ✗ {type(e).__name__}: {str(e)[:120]}', flush=True)
    raise last_err if last_err else RuntimeError('FRED CSV all URLs failed')


def _fetch_fred_via_proxy(series_id):
    """透過免註冊 CORS proxy 拉 FRED CSV。
    背景：DBnomics 已停止索引 FRED 系列（providers 列表沒 FRED），
    且 FRED 直連對部份 IP 段（家用 / 部份 GH Actions region）會 timeout。
    主備援：allorigins.win → codetabs.com → corsproxy.io。
    每個 proxy 試 4 次（allorigins 對 FRED 偶有 520/522 transient）。"""
    import time as _t
    target = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}'
    proxies = [
        ('allorigins.win', f'https://api.allorigins.win/raw?url={urllib.parse.quote(target)}'),
        ('codetabs.com',   f'https://api.codetabs.com/v1/proxy?quest={urllib.parse.quote(target)}'),
        ('corsproxy.io',   f'https://corsproxy.io/?url={urllib.parse.quote(target)}'),
    ]
    last_err = None
    for name, proxy_url in proxies:
        for attempt in range(1, 5):  # 每個 proxy 最多 4 次
            try:
                print(f'    [proxy:{name} {attempt}/4] {target}', flush=True)
                text = http_get(proxy_url, timeout=25)
                head = text[:200].lstrip().lower()
                if not (head.startswith('observation_date') or head.startswith('date')):
                    last_err = RuntimeError(f'{name} 回應非 CSV')
                    print(f'      ✗ {name} 回應非 CSV head={head[:60]!r}', flush=True)
                    break  # 換 proxy（這個 proxy 結構不對，重試也沒用）
                data = _parse_fred_csv(text)
                if data:
                    return data
                last_err = RuntimeError(f'{name} CSV 解析後為空')
                break
            except Exception as e:
                last_err = e
                print(f'      ✗ {type(e).__name__}: {str(e)[:120]}', flush=True)
                if attempt < 4:
                    _t.sleep(3 * attempt)  # 3s, 6s, 9s 漸增 backoff
    raise last_err if last_err else RuntimeError('all FRED proxies failed')


def _fetch_stooq(series_id):
    """Stooq 鏡像 FRED 月資料：https://stooq.com/q/d/l/?s={id}.fr&i=m"""
    url = f'https://stooq.com/q/d/l/?s={series_id.lower()}.fr&i=m'
    print(f'    [stooq] {url}', flush=True)
    text = http_get_retry(url, attempts=2, timeout=15)
    if not text or 'No data' in text[:60]:
        raise RuntimeError('Stooq no data')
    # Stooq CSV header: Date,Open,High,Low,Close,Volume → 取 Close
    rows = list(csv.reader(io.StringIO(text)))
    out = []
    for row in rows[1:]:
        if len(row) < 5: continue
        try:
            d = row[0].strip()
            v = float(row[4])
            out.append({'date': d, 'value': v})
        except ValueError:
            continue
    return out


def fetch_fred_csv(series_id):
    """多重來源備援：FRED via proxy → DBnomics → FRED 直連 → Stooq

    順序設計：
      ① CORS proxy + FRED CSV：目前唯一穩定通道（2026-05 起 DBnomics 拋棄 FRED）
      ② DBnomics：保留以防服務恢復
      ③ FRED 直連：GH Actions / 部份 IP 可能可通
      ④ Stooq：FRED 月資料鏡像（2024+ 後限 API key）
    """
    sources = [
        ('FRED via Proxy',  lambda: _fetch_fred_via_proxy(series_id)),
        ('DBnomics',        lambda: _fetch_dbnomics(series_id)),
        ('FRED CSV direct', lambda: _fetch_fred_csv(series_id)),
        ('Stooq',           lambda: _fetch_stooq(series_id)),
    ]
    for name, fn in sources:
        try:
            data = fn()
            if data and len(data) > 0:
                print(f'    [OK via {name}] {len(data)} 筆', flush=True)
                return data
        except Exception as e:
            print(f'    ✗ {name} 失敗：{type(e).__name__}: {str(e)[:120]}', flush=True)
    raise RuntimeError(f'all sources failed for {series_id}')


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
