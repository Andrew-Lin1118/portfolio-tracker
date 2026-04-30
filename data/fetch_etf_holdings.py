# -*- coding: utf-8 -*-
"""
fetch_etf_holdings.py
======================
抓 ETF 成分股與比重（含 USD 槓桿 ETF 的 swap 結構），寫入 data/etf_holdings.json。

涵蓋：
  - USD（ProShares Ultra Semiconductors 2x，含 swap + 直接持股）
  - SOXX（iShares Semiconductor，1x 對照）
  - SMH（VanEck Semiconductor，1x 對照，有 TSM）
  - XSD（SPDR Semiconductor Equal Weight，等權重對照）
  - PSI（Invesco Dynamic Semiconductors，動量篩選）

資料源：stockanalysis.com（公開、無需 API key）
排程：每週一次足矣（ETF 持股變動緩慢）。
"""
import os, sys, json, datetime, re, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(ROOT, 'etf_holdings.json')

ETFS = [
    {'symbol': 'USD',  'name': 'ProShares Ultra Semiconductors',          'leverage': 2, 'tracks': 'Dow Jones U.S. Semiconductors Index'},
    {'symbol': 'SOXX', 'name': 'iShares Semiconductor ETF',               'leverage': 1, 'tracks': 'ICE Semiconductor Index'},
    {'symbol': 'SMH',  'name': 'VanEck Semiconductor ETF',                'leverage': 1, 'tracks': 'MVIS US Listed Semiconductor 25 Index'},
    {'symbol': 'XSD',  'name': 'SPDR S&P Semiconductor ETF (Equal Wt.)',  'leverage': 1, 'tracks': 'S&P Semiconductor Select Industry Index'},
    {'symbol': 'PSI',  'name': 'Invesco Dynamic Semiconductors ETF',      'leverage': 1, 'tracks': 'Dynamic Semiconductor Intellidex Index'},
]

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'})
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', errors='ignore')


def parse_holdings(html):
    """
    回傳 {'equity': [...], 'swaps': [...]}
    equity: [{symbol, name, weight}] — 一般股票持股
    swaps:  [{counterparty, weight, target}] — swap 合約（USD 等槓桿 ETF）
    """
    equity, swaps = [], []
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S)
    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
        if len(cells) < 4:
            continue
        sym = re.sub(r'<[^>]+>', '', cells[1]).strip()
        name_html = cells[2]
        name = re.sub(r'<[^>]+>', '', name_html).strip().replace('&amp;', '&')
        weight_text = re.sub(r'<[^>]+>', '', cells[3]).strip()
        if not weight_text.endswith('%'):
            continue
        try:
            weight = float(weight_text.rstrip('%').replace(',', ''))
        except ValueError:
            continue

        # swap 列特徵：sym='n/a' 且 name 含 'SWAP'
        if (not sym or sym.lower() == 'n/a') and 'SWAP' in name.upper():
            # 嘗試從 name 切出對手方 + 標的
            target = None
            counter = name
            # 常見格式：「<INDEX> SWAP <COUNTERPARTY>」
            m = re.match(r'^(.*?)\s+SWAP\s+(.+)$', name, re.I)
            if m:
                target = m.group(1).strip()
                counter = m.group(2).strip()
            swaps.append({
                'counterparty': counter,
                'weight':       weight,
                'target':       target,
            })
        elif sym and len(sym) <= 6 and re.match(r'^[A-Z0-9.\-]+$', sym):
            equity.append({
                'symbol': sym,
                'name':   name,
                'weight': weight,
            })
    return {'equity': equity, 'swaps': swaps}


def fetch_etf(symbol):
    url = f'https://stockanalysis.com/etf/{symbol.lower()}/holdings/'
    print(f'  fetching {symbol} ...', flush=True)
    try:
        html = http_get(url)
        out = parse_holdings(html)
        print(f'    [OK] equity {len(out["equity"])}, swap {len(out["swaps"])}', flush=True)
        return out
    except Exception as e:
        print(f'    [ERR] {symbol}: {type(e).__name__}: {str(e)[:120]}', flush=True)
        return None


def derive_djussc_from_usd(usd_data):
    """
    從 USD（ProShares Ultra Semiconductors 2x）的直接持股推算 DJ U.S. Semi Index 權重。
    依槓桿 ETF 標準作業：直接股票部分按指數比例持有，normalize 到 100% 即為指數權重。
    """
    if not usd_data or not usd_data.get('equity'):
        return None
    equity = usd_data['equity']
    total = sum(e['weight'] for e in equity)
    if total <= 0:
        return None
    out = []
    for e in equity:
        out.append({
            'symbol': e['symbol'],
            'name':   e['name'],
            'weight': round(e['weight'] / total * 100, 4),
        })
    out.sort(key=lambda x: -x['weight'])
    return out


def main():
    print('抓取 ETF 持股...', flush=True)
    result = {
        'generated': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'etfs': {},
    }
    for meta in ETFS:
        sym = meta['symbol']
        h = fetch_etf(sym)
        if h is None:
            continue
        eq_total = sum(x['weight'] for x in h['equity'])
        sw_total = sum(x['weight'] for x in h['swaps'])
        result['etfs'][sym] = {
            **meta,
            'equity':       h['equity'],
            'swaps':        h['swaps'],
            'equity_total': round(eq_total, 2),
            'swap_total':   round(sw_total, 2),
            'total_exposure': round(eq_total + sw_total, 2),
        }
        print(f'    Sum equity {eq_total:.1f}% + swap {sw_total:.1f}% = {eq_total+sw_total:.1f}%', flush=True)

    # 推算 DJ U.S. Semi Index 成分股（從 USD 直接持股）
    usd = result['etfs'].get('USD')
    if usd:
        idx_equity = derive_djussc_from_usd(usd)
        if idx_equity:
            result['etfs']['DJUSSC'] = {
                'symbol': 'DJUSSC',
                'name':   'Dow Jones U.S. Semiconductors Index',
                'leverage': 1,
                'tracks':  '(Index 本身)',
                'derived_from': 'USD direct equity (normalized to 100%)',
                'note':    'DJ U.S. Semi Index 權重推算：USD 直接持股 / 39.6% × 100%。槓桿 ETF 直接股票部分按指數比例持有，normalize 即為指數權重。',
                'equity':       idx_equity,
                'swaps':        [],
                'equity_total': round(sum(e['weight'] for e in idx_equity), 2),
                'swap_total':   0.0,
                'total_exposure': round(sum(e['weight'] for e in idx_equity), 2),
            }
            print(f'  derived DJUSSC: {len(idx_equity)} constituents from USD direct equity', flush=True)

    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'\n寫入 → {OUT_FILE}（{len(result["etfs"])} 個 ETF/Index）', flush=True)


if __name__ == '__main__':
    main()
