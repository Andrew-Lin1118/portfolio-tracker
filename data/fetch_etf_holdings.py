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
  - 00631L（元大台灣50正2：MoneyDJ 申報持倉 = 臺股期貨 + 現股；
    期貨部位以 taiex_weights.json（TAIFEX 加權指數權重 Top30）回推個股曝險，
    與 USD swap 拆解 / RAM 推導同一套邏輯，存成 1x 比重 + leverage 2）
  - 00981A（主動統一台股增長：主動式 ETF 每日揭露持股，leverage 1）

資料源：stockanalysis.com（美股）、moneydj.com Basic0007B（台股，UTF-8）
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
    (re.compile(r'MICRON', re.I),   ('MU',        'Micron Technology, Inc.')),
    (re.compile(r'SAMSUNG', re.I),  ('005930.KS', 'Samsung Electronics Co., Ltd.')),
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
        symbol = s.get('target_symbol')
        name = s.get('target_name')
        add(symbol, name, s.get('weight'))

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


# ─────────────────────────── 台股 ETF（MoneyDJ） ───────────────────────────

def fetch_moneydj_tw_holdings(etfid):
    """
    抓 MoneyDJ Basic0007B（申報持股明細，含股票代碼）。
    回傳 {'stocks': [{symbol,name,weight}], 'futures': [{name,weight}], 'asof': 'YYYY-MM-DD'}
    """
    url = f'https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid={etfid}'
    html = http_get(url, timeout=25)
    m = re.search(r'資料日期[：:]\s*([\d/]+)', html)
    asof = m.group(1).replace('/', '-') if m else None
    stocks, futures = [], []
    for tb in re.findall(r'<table[^>]*>(.*?)</table>', html, re.S):
        heads = ''.join(re.sub(r'<[^>]+>', '', h) for h in re.findall(r'<th[^>]*>(.*?)</th>', tb, re.S))
        if '個股名稱' not in heads:
            continue
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', tb, re.S):
            cells = [re.sub(r'<[^>]+>', '', c).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
            if len(cells) < 2:
                continue
            try:
                w = float(cells[1].replace(',', ''))
            except ValueError:
                continue
            if w <= 0:
                continue
            m2 = re.match(r'^(.*?)\((\w+)\.(TW|TWO)\)$', cells[0])
            if m2:
                stocks.append({'symbol': f'{m2.group(2)}.{m2.group(3)}',
                               'name': m2.group(1).strip().rstrip('*').strip(),
                               'weight': w})
            elif '期貨' in cells[0]:
                futures.append({'name': cells[0], 'weight': w})
        break
    return {'stocks': stocks, 'futures': futures, 'asof': asof}


def load_taiex_weights():
    """讀本地 taiex_weights.json（fetch_taiex_weights.py 產出，TAIFEX 加權指數權重 Top30）。"""
    try:
        with open(os.path.join(ROOT, 'taiex_weights.json'), encoding='utf-8') as f:
            d = json.load(f)
        items = [{'symbol': it['code'] + '.TW', 'name': it['name'], 'w': float(it['w'])}
                 for it in (d.get('items') or []) if float(it.get('w') or 0) > 0]
        return items, d.get('asof')
    except Exception:
        return [], None


def build_00631l(hold, taiex_items, taiex_asof):
    """
    00631L 成分曝險（與 USD swap 拆解同邏輯）：
      現股直接持有 + 臺股期貨部位 × 加權指數成分股權重。
    合併後除以 2 存成 1x 比重、leverage=2（與 RAM 相同存法，前端共用計算）。
    """
    if not hold or not (hold['stocks'] or hold['futures']):
        return None

    buckets = {}

    def add(symbol, name, weight):
        if not symbol or weight <= 0:
            return
        if symbol not in buckets:
            buckets[symbol] = {'symbol': symbol, 'name': name or symbol, 'weight': 0.0}
        buckets[symbol]['weight'] += float(weight)

    for s in hold['stocks']:
        add(s['symbol'], s['name'], s['weight'])

    fut_total = sum(f['weight'] for f in hold['futures'])
    covered = sum(it['w'] for it in taiex_items)
    if fut_total > 0 and taiex_items:
        for it in taiex_items:
            add(it['symbol'], it['name'], fut_total * it['w'] / 100)
        residual = max(0.0, 100.0 - covered) * fut_total / 100
        if residual > 0.05:
            add('其他', f'加權指數其餘成分股（Top{len(taiex_items)} 以外）', residual)
    elif fut_total > 0:
        # 沒有權重檔時退而求其次：期貨腿整包列一列，不拆個股
        add('臺股期貨', '臺股期貨（無加權指數權重檔，未拆解）', fut_total)

    combined = sorted(buckets.values(), key=lambda x: -x['weight'])
    total = sum(b['weight'] for b in combined)
    if total <= 0:
        return None
    equity = [{'symbol': b['symbol'], 'name': b['name'], 'weight': round(b['weight'] / 2, 4)}
              for b in combined]
    fut_names = '、'.join(f"{f['name']} {f['weight']:.2f}%" for f in hold['futures']) or '無'
    direct_names = '、'.join(f"{s['name']} {s['weight']:.2f}%" for s in hold['stocks']) or '無'
    return {
        'symbol': '00631L',
        'name': '元大台灣50單日正向2倍',
        'leverage': 2,
        'tracks': '台灣50指數單日正向2倍',
        'derived_from': 'MoneyDJ 申報持倉（現股 + 臺股期貨），期貨部位以 TAIFEX 加權指數權重回推',
        'source_holding': '00631L',
        'note': (f'00631L 實際持倉（{hold["asof"] or "近期"}）：現股 {direct_names}；期貨 {fut_names}。'
                 f'期貨部位按 TAIFEX 加權指數成分股權重（資料日 {taiex_asof or "-"}，Top{len(taiex_items)} '
                 f'涵蓋 {covered:.1f}%，其餘打包為「其他」）回推個股曝險，再與現股合併；'
                 f'左欄為除以 2 的 1x 化比重，右欄 ×2 即為每 1 元淨值的實際曝險。'
                 f'（台指期追蹤加權指數，與台灣50指數相關性 >99%，視為近似。）'),
        'equity': equity,
        'swaps': [],
        'equity_total': round(total / 2, 2),
        'swap_total': 0.0,
        'total_exposure': round(total, 2),
    }


def build_00981a(hold):
    """00981A 主動式 ETF：每日揭露持股直接作為成分曝險（leverage 1）。"""
    if not hold or not hold['stocks']:
        return None
    equity = sorted(({'symbol': s['symbol'], 'name': s['name'], 'weight': round(s['weight'], 4)}
                     for s in hold['stocks']), key=lambda x: -x['weight'])
    tot = round(sum(e['weight'] for e in equity), 2)
    return {
        'symbol': '00981A',
        'name': '主動統一台股增長',
        'leverage': 1,
        'tracks': '主動式選股（統一投信，無追蹤指數）',
        'source_holding': '00981A',
        'note': (f'主動式 ETF 每日揭露持股（MoneyDJ 申報資料，資料日期 {hold["asof"] or "-"}）。'
                 f'持股合計 {tot:.2f}%，其餘為現金等部位。'),
        'equity': equity,
        'swaps': [],
        'equity_total': tot,
        'swap_total': 0.0,
        'total_exposure': tot,
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

    # ── 台股 ETF：00631L（正2，期貨拆解）、00981A（主動式，直接揭露）──
    taiex_items, taiex_asof = load_taiex_weights()
    tw_builders = {
        '00631L': lambda: build_00631l(fetch_moneydj_tw_holdings('00631L.TW'), taiex_items, taiex_asof),
        '00981A': lambda: build_00981a(fetch_moneydj_tw_holdings('00981A.TW')),
    }
    old = {}
    try:
        with open(OUT_FILE, encoding='utf-8') as f:
            old = (json.load(f).get('etfs') or {})
    except Exception:
        pass
    for key, build in tw_builders.items():
        print(f'  fetching {key} (MoneyDJ) ...', flush=True)
        entry = None
        try:
            entry = build()
        except Exception as e:
            print(f'    [ERR] {key}: {type(e).__name__}: {str(e)[:120]}', flush=True)
        if entry:
            result['etfs'][key] = entry
            print(f'    [OK] {key}: {len(entry["equity"])} 檔，合計曝險 {entry["total_exposure"]}%', flush=True)
        elif key in old:
            result['etfs'][key] = old[key]
            print(f'    [WARN] {key} 本次抓取失敗，沿用舊資料', flush=True)

    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'\n寫入 → {OUT_FILE}（{len(result["etfs"])} 個 ETF/Index）', flush=True)


if __name__ == '__main__':
    main()
