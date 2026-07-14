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


def _add_months(d, months):
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return datetime.date(y, m, 1)


def _next_business_day(d):
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


def _first_weekday(year, month, weekday):
    d = datetime.date(year, month, 1)
    while d.weekday() != weekday:
        d += datetime.timedelta(days=1)
    return d


def _last_business_day(year, month):
    if month == 12:
        d = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        d = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


# BLS 官方 CPI 發布時程（reference month → (發布日, 台北時間)）
# 美東 08:30 發布：EDT = 台北 20:30；EST（11月初~3月初冬令）= 台北 21:30。
# 來源：https://www.bls.gov/schedule/news_release/cpi.htm（2026-07-14 抓取，每年更新一次）
CPI_OFFICIAL_RELEASES = {
    '2026-06': ('2026-07-14', '20:30'),
    '2026-07': ('2026-08-12', '20:30'),
    '2026-08': ('2026-09-11', '20:30'),
    '2026-09': ('2026-10-14', '20:30'),
    '2026-10': ('2026-11-10', '21:30'),
    '2026-11': ('2026-12-10', '21:30'),
}


def estimate_next_update(series_id, obs_date):
    """Monthly FRED observation date -> estimated next release time in Taiwan."""
    try:
        base = datetime.date.fromisoformat(str(obs_date)[:10])
    except Exception:
        return None
    # CPI（含核心）優先查 BLS 官方時程：下一個 reference month = obs + 1 個月
    if series_id in ('CPIAUCSL', 'CPILFESL'):
        nxt = _add_months(base, 1)
        official = CPI_OFFICIAL_RELEASES.get(f'{nxt.year:04d}-{nxt.month:02d}')
        if official:
            return {'date': official[0], 'time_tpe': official[1], 'note': '官方時程'}
    target = _add_months(base, 2)
    if series_id == 'UNRATE':
        d = _first_weekday(target.year, target.month, 4)  # first Friday
    elif series_id in ('PCEPI', 'PCEPILFE'):
        d = _last_business_day(target.year, target.month)
    elif series_id == 'PPIFIS':
        d = _next_business_day(datetime.date(target.year, target.month, 11))
    else:
        d = _next_business_day(datetime.date(target.year, target.month, 10))
    return {
        'date': d.isoformat(),
        'time_tpe': '20:30',
        'note': '預估',
    }


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
        # 2026-07-14: 4 次×3 proxy 會把整體 as_completed timeout 撐爆 → 降為各 2 次
        for attempt in range(1, 3):
            try:
                print(f'    [proxy:{name} {attempt}/2] {target}', flush=True)
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
                if attempt < 2:
                    _t.sleep(3)
    raise last_err if last_err else RuntimeError('all FRED proxies failed')


def _fetch_fred_api(series_id):
    """FRED 官方 API（最穩）。需要免費 API key：https://fred.stlouisfed.org/docs/api/api_key.html
    設環境變數 FRED_API_KEY（GH Actions 加 repo secret）即自動啟用；沒設則跳過此層。"""
    key = os.environ.get('FRED_API_KEY', '').strip()
    if not key:
        raise RuntimeError('FRED_API_KEY 未設定（跳過）')
    url = (f'https://api.stlouisfed.org/fred/series/observations?series_id={series_id}'
           f'&api_key={key}&file_type=json&observation_start=2019-01-01')
    print(f'    [fred-api] series={series_id}', flush=True)
    text = http_get_retry(url, attempts=2, timeout=20)
    obs = json.loads(text).get('observations', [])
    out = []
    for o in obs:
        v = o.get('value')
        if v in ('.', '', None):
            continue
        try:
            out.append({'date': o.get('date'), 'value': float(v)})
        except (TypeError, ValueError):
            continue
    return out


def _fetch_fred_direct_cffi(series_id):
    """curl_cffi Chrome TLS 指紋直連 FRED。
    FRED（Akamai）常對非瀏覽器 TLS 指紋 timeout/擋，urllib 直連在 GH Actions 不穩，
    但 impersonate=chrome 通常可過。curl_cffi 未安裝時跳過此層。"""
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        raise RuntimeError('curl_cffi 未安裝（跳過）')
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}'
    print(f'    [fred-cffi] {url}', flush=True)
    r = curl_requests.get(url, impersonate='chrome', timeout=20, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f'HTTP {r.status_code}')
    head = (r.text or '')[:200].lstrip().lower()
    if not (head.startswith('observation_date') or head.startswith('date')):
        raise RuntimeError(f'非 CSV head={head[:60]!r}')
    return _parse_fred_csv(r.text)


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
    """多重來源備援：FRED API → curl_cffi 直連 → CORS proxy → DBnomics → urllib 直連 → Stooq

    順序設計（2026-07-14 重排）：
      ① FRED 官方 API：有 FRED_API_KEY 才啟用，最穩；沒 key 立即跳過（零成本）
      ② curl_cffi Chrome 指紋直連：GH Actions 可過 Akamai；未安裝立即跳過
      ③ CORS proxy + FRED CSV：allorigins/codetabs/corsproxy（2026-07-14 集體故障過，降級為備援）
      ④ DBnomics：保留以防服務恢復（2026-05 起已拋棄 FRED）
      ⑤ urllib 直連：部份 IP 可通
      ⑥ Stooq：FRED 月資料鏡像（更新有數小時延遲）
    """
    sources = [
        ('FRED API',        lambda: _fetch_fred_api(series_id)),
        ('FRED cffi',       lambda: _fetch_fred_direct_cffi(series_id)),
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
            history = valid[-60:]  # 近 60 個月（5 年）
        else:  # level
            last  = raw[-1]
            prev  = raw[-2] if len(raw) >= 2 else None
            history = raw[-60:]

        next_update = estimate_next_update(series_id, last['date'])
        return {
            'id':        series_id,
            'name':      name,
            'kind':      kind,
            'date':      last['date'],
            'value':     round(last['value'], 3),
            'prev':      round(prev['value'], 3) if prev else None,
            'mom':       round(last['value'] - prev['value'], 3) if prev else None,  # 月變化（pt）
            'next_update': next_update['date'] if next_update else None,
            'next_update_time_tpe': next_update['time_tpe'] if next_update else None,
            'next_update_note': next_update['note'] if next_update else None,
            'history':   [{'date': h['date'], 'value': round(h['value'], 3)} for h in history],
        }
    except Exception as e:
        print(f'  [{series_id}] 失敗：{e}', flush=True)
        return None


def _load_existing_indicators():
    """讀現有 economic_data.json 的 indicators（merge 用，避免本次失敗把上次 success 蓋成 null）。
    14 天內的舊資料才採用，避免上游服務長期失效時前端顯示過時資料。"""
    try:
        with open(OUT_FILE, encoding='utf-8') as f:
            d = json.load(f)
        gen = d.get('generated', '')
        if gen:
            try:
                gen_dt = datetime.datetime.fromisoformat(gen.replace('Z', '+00:00'))
                age_days = (datetime.datetime.now(datetime.timezone.utc) - gen_dt).days
                if age_days > 14:
                    print(f'  [merge] 上次資料 {age_days} 天前，太舊不沿用', flush=True)
                    return {}
            except Exception:
                pass
        return d.get('indicators', {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main():
    print('抓取 FRED 經濟指標（並行）...', flush=True)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from concurrent.futures import TimeoutError as FuturesTimeout

    # ★ 先讀現有 JSON 當底圖（merge 模式）：本次失敗的 series 沿用上次 success
    existing = _load_existing_indicators()
    indicators = {sid: existing.get(sid) for sid, _, _ in SERIES}

    def _accept(sid, ind):
        if ind:
            indicators[sid] = ind
            print(f'  ✓ {sid:10s} {ind["date"]} = {ind["value"]}'
                  + (f' (上期 {ind["prev"]}, 月變 {ind["mom"]:+.2f}pt)' if ind['prev'] is not None else ''),
                  flush=True)
        else:
            kept = '（沿用上次）' if indicators[sid] else ''
            print(f'  ✗ {sid:10s} 本次無資料{kept}', flush=True)

    # 6 個 series 並行抓（耗時從 6×~5s = 30s 縮到 ~5s）
    # ⚠ 不用 with：as_completed 逾時後 with 出口的 shutdown(wait=True) 會等所有
    #   retry 鏈跑完，2026-07-14 曾因此 TimeoutError 直接 crash、一個指標都沒寫。
    pool = ThreadPoolExecutor(max_workers=6)
    futures = {pool.submit(build_indicator, sid, name, kind): sid for sid, name, kind in SERIES}
    timed_out = False
    try:
        for fut in as_completed(futures, timeout=200):
            sid = futures[fut]
            try:
                _accept(sid, fut.result())
            except Exception as e:
                print(f'  ✗ {sid:10s} 例外：{e}', flush=True)
    except FuturesTimeout:
        timed_out = True
        print('  ⚠ 整體逾時（200s）：寫出已完成部分，未完成 series 沿用上次', flush=True)
        for fut, sid in futures.items():
            if fut.done() and not fut.cancelled():
                try:
                    _accept(sid, fut.result(timeout=0))
                except Exception:
                    pass
        pool.shutdown(wait=False, cancel_futures=True)
    else:
        pool.shutdown(wait=True)

    # 即使全部失敗，也要寫出 JSON
    payload = {
        'generated': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'indicators': indicators,
    }
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    ok_count = sum(1 for v in indicators.values() if v is not None)
    print(f'\n寫入 → {OUT_FILE}（{ok_count}/{len(SERIES)} 成功）', flush=True)
    if timed_out:
        # 殘餘 retry 執行緒非 daemon，會卡住 process 收尾 → JSON 已寫完，直接離開
        sys.stdout.flush()
        os._exit(0)


if __name__ == '__main__':
    main()
