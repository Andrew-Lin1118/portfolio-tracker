# -*- coding: utf-8 -*-
"""
fetch_fedwatch.py — 從 investing.com 的 Fed Rate Monitor 抓 FOMC 會議利率機率分布
=================================================================================
原始來源 = CME FedWatch（30-day Fed Funds futures），但 CME 官方 API 需要付費 OAuth。
investing.com 的 Fed Rate Monitor 也用相同方法計算機率，且免 token、可程式化擷取。

抓的內容：
- 每場未來 FOMC 會議（13 場左右）的：
    meeting_date, future_price, target_rate_probabilities (range → %)
- 從 FRED DBnomics 抓 DFEDTARU / DFEDTARL 拿到「目前」聯邦資金目標利率上下限
- 計算 next-meeting 隱含預期：最可能 range / 機率 / 與當前利率比較（升降息）

輸出：data/fedwatch.json

依賴：beautifulsoup4
排程建議：每日 1-2 次（盤後機率才會大變動）
"""
import os, sys, json, re, datetime, io
import urllib.request, urllib.error, urllib.parse

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from bs4 import BeautifulSoup

OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fedwatch.json')

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
HEADERS = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

INVESTING_URL = 'https://www.investing.com/central-banks/fed-rate-monitor'

_MONTHS = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
           'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', errors='ignore')


def _fetch_investing_with_fallback():
    """investing.com Fed Rate Monitor 多來源備援。
    背景：直連在某些 IP 段（GH Actions ubuntu runner）會被 Cloudflare 擋，
    本機家用 IP 通常 OK。透過 CORS proxy 包一層可繞過。
    順序：直連 → allorigins.win → codetabs.com（任一成功就停）。"""
    sources = [
        ('direct',         INVESTING_URL),
        ('allorigins.win', f'https://api.allorigins.win/raw?url={urllib.parse.quote(INVESTING_URL)}'),
        ('codetabs.com',   f'https://api.codetabs.com/v1/proxy?quest={urllib.parse.quote(INVESTING_URL)}'),
    ]
    last_err = None
    for name, url in sources:
        try:
            print(f'  [investing source:{name}] fetching ...', flush=True)
            html = _http_get(url, timeout=25)
            # 檢查確實是 Fed Rate Monitor 內容（不是 Cloudflare 擋頁或 proxy 自己的頁）
            if 'fedRateInnerTabOpen' in html or 'Fed Rate Monitor' in html:
                print(f'    ✓ via {name} ({len(html):,} bytes)', flush=True)
                return html
            last_err = RuntimeError(f'{name} 內容非 Fed Rate Monitor 頁面')
            print(f'    ✗ {name} 內容檢查失敗 (前 60 字: {html[:60]!r})', flush=True)
        except Exception as e:
            last_err = e
            print(f'    ✗ {name} {type(e).__name__}: {str(e)[:120]}', flush=True)
    raise last_err if last_err else RuntimeError('all investing.com sources failed')


def _parse_meeting_date(txt):
    """\"Meeting Time:Jun 17, 2026 02:00PM ET...\" → \"2026-06-17\""""
    if not txt:
        return None
    m = re.search(r'([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})', txt)
    if not m:
        return None
    mo = _MONTHS.get(m.group(1))
    if not mo:
        return None
    return f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}"


def _parse_future_price(txt):
    """\"...Future Price:96.365\" → 96.365"""
    if not txt:
        return None
    m = re.search(r'Future\s+Price\s*:\s*([0-9.]+)', txt)
    return float(m.group(1)) if m else None


def _to_pct(s):
    s = (s or '').strip().replace('%', '').replace('—', '0').replace('-', '0')
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def fetch_fedwatch_from_investing():
    """回傳 list of {date, future_price, implied_rate, probabilities, most_likely}"""
    print('  [investing.com] fetching Fed Rate Monitor...', flush=True)
    html = _fetch_investing_with_fallback()
    soup = BeautifulSoup(html, 'html.parser')

    meetings = []
    # 每場會議在 .fedRateInnerTabOpen.cardBlock 裡，含 meeting time 文字 + 機率 table
    for blk in soup.find_all(class_='fedRateInnerTabOpen'):
        # 找 meeting time / future price 文字
        info_txt = blk.get_text(separator=' ', strip=True)
        meeting_date = _parse_meeting_date(info_txt)
        future_price = _parse_future_price(info_txt)
        if not meeting_date:
            continue
        # 找 .fedRateTbl
        tbl = blk.find(class_='fedRateTbl')
        if not tbl:
            continue
        probabilities = []
        for tr in tbl.find_all('tr'):
            cells = [c.get_text(strip=True) for c in tr.find_all(['th', 'td'])]
            if len(cells) < 2:
                continue
            label = cells[0]
            # 跳過表頭
            if 'Target Rate' in label or 'Probability' in label:
                continue
            # 預期 label 形如 "3.50 - 3.75"
            m = re.match(r'(\d+\.\d+)\s*-\s*(\d+\.\d+)', label)
            if not m:
                continue
            lo = float(m.group(1))
            hi = float(m.group(2))
            cur_pct = _to_pct(cells[1]) if len(cells) > 1 else 0.0
            probabilities.append({
                'range_low':  lo,
                'range_high': hi,
                'range':      f'{lo:.2f}-{hi:.2f}',
                'prob':       cur_pct,
            })
        if not probabilities:
            continue

        # most-likely range
        ml = max(probabilities, key=lambda p: p['prob'])
        # implied rate = 100 - futures price (大約等於 expected_rate)
        implied = (100 - future_price) if future_price else None
        # 期望值：sum(midpoint × prob)
        total_prob = sum(p['prob'] for p in probabilities)
        if total_prob > 0:
            expected = sum(((p['range_low'] + p['range_high']) / 2) * p['prob'] for p in probabilities) / total_prob
        else:
            expected = None

        meetings.append({
            'date':              meeting_date,
            'future_price':      future_price,
            'implied_rate':      round(implied, 3) if implied is not None else None,
            'expected_rate':     round(expected, 3) if expected is not None else None,
            'most_likely_range': ml['range'],
            'most_likely_low':   ml['range_low'],
            'most_likely_high':  ml['range_high'],
            'most_likely_prob':  ml['prob'],
            'probabilities':     probabilities,
        })
    print(f'  [investing.com] parsed {len(meetings)} meetings', flush=True)
    return meetings


def _fetch_fred_dbnomics(series_id):
    url = f'https://api.db.nomics.world/v22/series/FRED/{series_id}?observations=1'
    text = _http_get(url, timeout=15)
    data = json.loads(text)
    docs = data.get('series', {}).get('docs', [])
    if not docs:
        return None, None
    doc = docs[0]
    periods = doc.get('period', []) or []
    values = doc.get('value', []) or []
    for i in range(len(values) - 1, -1, -1):
        v = values[i]
        if v in (None, 'NA'):
            continue
        try:
            return float(v), (periods[i] if i < len(periods) else None)
        except (ValueError, TypeError):
            continue
    return None, None


def _fetch_fred_csv_direct(series_id):
    import csv as _csv, ssl
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}'
    req = urllib.request.Request(url, headers=HEADERS)
    # 對 Windows ssl issue 容忍
    ctx = ssl.create_default_context()
    try:
        text = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', errors='ignore')
    except Exception:
        ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        text = urllib.request.urlopen(req, timeout=20, context=ctx).read().decode('utf-8', errors='ignore')
    rows = list(_csv.reader(io.StringIO(text)))
    for row in reversed(rows[1:]):
        if len(row) < 2 or row[1] in ('.', '', None):
            continue
        try:
            return float(row[1]), row[0].strip()
        except ValueError:
            continue
    return None, None


def _fetch_fred_stooq(series_id):
    import csv as _csv
    url = f'https://stooq.com/q/d/l/?s={series_id.lower()}.fr&i=d'
    text = _http_get(url, timeout=15)
    if not text or 'No data' in text[:60]:
        return None, None
    rows = list(_csv.reader(io.StringIO(text)))
    for row in reversed(rows[1:]):
        if len(row) < 5:
            continue
        try:
            return float(row[4]), row[0].strip()
        except ValueError:
            continue
    return None, None


def fetch_current_ffr_range():
    """多重來源備援：DBnomics → FRED CSV → Stooq。回傳 {lower, upper, date}"""
    out = {'lower': None, 'upper': None, 'date': None}
    sources = [
        ('DBnomics', _fetch_fred_dbnomics),
        ('FRED CSV', _fetch_fred_csv_direct),
        ('Stooq',    _fetch_fred_stooq),
    ]
    for series_id, key in [('DFEDTARL', 'lower'), ('DFEDTARU', 'upper')]:
        for name, fn in sources:
            try:
                v, dt = fn(series_id)
                if v is not None:
                    out[key] = v
                    if not out['date']:
                        out['date'] = dt
                    print(f'  [{name}] {series_id} = {v} ({dt})', flush=True)
                    break
            except Exception as e:
                print(f'  [{name}] {series_id} 失敗: {type(e).__name__}: {str(e)[:120]}', flush=True)
        else:
            print(f'  [WARN] {series_id} 三個來源全失敗', flush=True)
    return out


def main():
    print('抓 FedWatch 機率分布 + 當前 FFR 目標利率...', flush=True)
    meetings = []
    try:
        meetings = fetch_fedwatch_from_investing()
    except Exception as e:
        print(f'  ✗ investing.com 抓取失敗: {type(e).__name__}: {str(e)[:200]}', flush=True)

    current = fetch_current_ffr_range()
    # 若 FRED 全部失敗，從 investing.com 機率分布反推：
    #   下次會議「最可能 band（通常 >50% 機率）」≈ 當前利率（假設市場最可能預期是維持不變）
    if current['upper'] is None and meetings:
        first = meetings[0]
        ml_prob = first.get('most_likely_prob') or 0
        if ml_prob >= 50:    # ≥50% 才視為「市場預期維持」
            current['lower']    = first.get('most_likely_low')
            current['upper']    = first.get('most_likely_high')
            current['date']     = '(從 FedWatch 反推)'
            current['inferred'] = True
            print(f"  current FFR target range (derived): {current['lower']}-{current['upper']}%", flush=True)
        else:
            print(f"  WARN: 無法可靠反推當前利率（next meeting 最可能僅 {ml_prob:.1f}% 機率）", flush=True)
    elif current['upper'] is not None:
        print(f"  current FFR target range: {current['lower']}-{current['upper']}% ({current['date']})", flush=True)

    # 計算 next meeting 相對當前的變化提示
    next_meeting = meetings[0] if meetings else None
    inferred_change = None
    if next_meeting and current['upper'] is not None:
        cur_upper = current['upper']
        next_high = next_meeting.get('most_likely_high')
        if next_high is not None:
            diff_bps = round((next_high - cur_upper) * 100)
            if   diff_bps <= -25: inferred_change = f'預期降息 {abs(diff_bps)}bp'
            elif diff_bps >=  25: inferred_change = f'預期升息 {diff_bps}bp'
            else:                 inferred_change = '預期維持不變'

    payload = {
        'generated':         datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'source':            'investing.com Fed Rate Monitor + FRED DBnomics',
        'current_ffr':       current,        # {lower, upper, date}
        'next_meeting':      next_meeting,   # 可能是 None
        'inferred_change':   inferred_change,
        'meetings':          meetings,       # 全部會議（前端要用可整段 render）
    }
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f'\n寫入 → {OUT_FILE}（{len(meetings)} 場會議）', flush=True)


if __name__ == '__main__':
    main()
