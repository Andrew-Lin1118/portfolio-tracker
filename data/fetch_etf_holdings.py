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
  - DRAM/RAM（Roundhill memory stocks；RAM derives 2x component exposure from DRAM）

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
    {'symbol': 'DRAM', 'name': 'Roundhill Memory ETF',                     'leverage': 1, 'tracks': 'Global memory stocks'},
]

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

CASH_OR_COLLATERAL_SYMBOLS = {'B.0', 'USD', 'TWD', 'HKD', 'KRW', 'JPY', 'CNY'}

SYMBOL_ALIASES = {
    'KRX: 000660': '000660.KS',
    'KRX: 005930': '005930.KS',
    '603986.C1': '603986.SS',
    '2408.TT': '2408.TW',
    '2344.TT': '2344.TW',
    '2337.TT': '2337.TW',
    '8299.TT': '8299.TWO',
}

SWAP_TARGETS = [
    (re.compile(r'MICRON', re.I), ('MU', 'Micron Technology, Inc.')),
    (re.compile(r'SAMSUNG', re.I), ('005930.KS', 'Samsung Electronics Co., Ltd.')),
    (re.compile(r'SK\s*HYNIX', re.I), ('000660.KS', 'SK hynix Inc.')),
]


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'})
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', errors='ignore')


def normalize_symbol(sym):
    sym = re.sub(r'\s+', ' ', (sym or '').strip())
    if not sym:
        return ''
    if sym in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[sym]
    if sym.startswith('KRX: '):
        return sym.split(':', 1)[1].strip() + '.KS'
    return sym


def is_cash_or_collateral(sym, name):
    s = normalize_symbol(sym).upper()
    n = (name or '').upper()
    if s in CASH_OR_COLLATERAL_SYMBOLS:
        return True
    if 'TREASURY BILL' in n or 'GOVERNMENT' in n:
        return True
    if n in {'NEW TAIWAN DOLLAR', 'SOUTH KOREA WON', 'CHINESE YUAN'}:
        return True
    return False


def swap_target_from_name(name):
    for rx, target in SWAP_TARGETS:
        if rx.search(name or ''):
            return target
    return (None, None)


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
        raw_sym = re.sub(r'<[^>]+>', ' ', cells[1]).strip()
        sym = normalize_symbol(raw_sym)
        name_html = cells[2]
        name = re.sub(r'<[^>]+>', '', name_html).strip().replace('&amp;', '&')
        weight_text = re.sub(r'<[^>]+>', '', cells[3]).strip()
        if not weight_text.endswith('%'):
            continue
        try:
            weight = float(weight_text.rstrip('%').replace(',', ''))
        except ValueError:
            continue

        is_swap = 'SWAP' in name.upper() or '.TRS' in sym.upper()
        if is_swap:
            # 嘗試從 name 切出對手方 + 標的
            target = None
            counter = name
            # 常見格式：「<INDEX> SWAP <COUNTERPARTY>」
            m = re.match(r'^(.*?)\s+SWAP\s+(.+)$', name, re.I)
            if m:
                target = m.group(1).strip()
                counter = m.group(2).strip()
            target_symbol, target_name = swap_target_from_name(name)
            swaps.append({
                'counterparty': counter,
                'weight':       weight,
                'target':       target,
                'target_symbol': target_symbol,
                'target_name':   target_name,
            })
        elif sym and sym.lower() != 'n/a' and not is_cash_or_collateral(sym, name) and re.match(r'^[A-Z0-9.\-]+$', sym, re.I):
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


def derive_ram_from_dram(dram_data):
    """
    RAM holds 2x daily exposure to DRAM. Build a component-stock view by
    combining DRAM direct stock positions and named single-stock swaps.
    """
    if not dram_data:
        return None

    buckets = {}

    def add(symbol, name, weight):
        if not symbol or not weight:
            return
        if symbol not in buckets:
            buckets[symbol] = {'symbol': symbol, 'name': name or symbol, 'weight': 0.0}
        buckets[symbol]['weight'] += float(weight)

    for e in dram_data.get('equity') or []:
        add(e.get('symbol'), e.get('name'), e.get('weight'))

    for s in dram_data.get('swaps') or []:
        add(s.get('target_symbol'), s.get('target_name'), s.get('weight'))

    equity = [
        {'symbol': v['symbol'], 'name': v['name'], 'weight': round(v['weight'], 4)}
        for v in buckets.values()
    ]
    equity.sort(key=lambda x: -x['weight'])
    if not equity:
        return None

    equity_total = round(sum(e['weight'] for e in equity), 2)
    return {
        'symbol': 'RAM',
        'name': 'Roundhill T-REX 2X Long DRAM Daily Target ETF',
        'leverage': 2,
        'tracks': 'Roundhill Memory ETF (DRAM)',
        'derived_from': 'DRAM direct holdings and single-name swaps',
        'source_holding': 'RAM',
        'note': 'RAM 為 2x DRAM daily target。成分曝險先用 DRAM 的直接股票部位加上可辨識的單名 swap 聚合，再乘上 RAM 2x 槓桿；例如 MU 不是只看直接持股，而是 MU 直接持股 + Micron swap。',
        'equity': equity,
        'swaps': [],
        'equity_total': equity_total,
        'swap_total': 0.0,
        'total_exposure': round(equity_total * 2, 2),
    }


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
                'note':    'DJ U.S. Semi Index weights are derived from USD direct equity holdings normalized to 100%.',
                'equity':       idx_equity,
                'swaps':        [],
                'equity_total': round(sum(e['weight'] for e in idx_equity), 2),
                'swap_total':   0.0,
                'total_exposure': round(sum(e['weight'] for e in idx_equity), 2),
            }
            print(f'  derived DJUSSC: {len(idx_equity)} constituents from USD direct equity', flush=True)

    dram = result['etfs'].get('DRAM')
    ram = derive_ram_from_dram(dram)
    if ram:
        result['etfs']['RAM'] = ram
        print(f'  derived RAM: {len(ram["equity"])} constituents from DRAM economic exposure', flush=True)

    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'\n寫入 → {OUT_FILE}（{len(result["etfs"])} 個 ETF/Index）', flush=True)


if __name__ == '__main__':
    main()
