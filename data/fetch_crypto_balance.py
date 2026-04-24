#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_crypto_balance.py — 抓取幣安 + 派網帳戶淨值（USDT）

輸出：
  1. stdout 單行 JSON（供 server.py 即時回應）
  2. data/crypto_balance.json（給前端 fallback 讀取，GitHub Pages 也看得到）

資料來源：
  - Binance Spot  :  GET /api/v3/account            → sum(free+locked) × 現價
  - Binance Futures: GET /fapi/v2/account          → totalMarginBalance
  - Pionex        :  GET /api/v1/account/balances  → sum(free+frozen) × 現價

使用：
  python data/fetch_crypto_balance.py            # 只輸出 JSON
  python data/fetch_crypto_balance.py --commit   # 另外 git push data/crypto_balance.json

Credentials 讀取順序（擇一即可）：
  1. ./crypto_config.yaml（專屬設定，gitignored）
  2. ./pionex_bot/config.yaml 的 api_key / api_secret（既有的幣安合約金鑰）

範例 crypto_config.yaml（複製貼上即可，記得填金鑰並加進 .gitignore）：
  binance:
    api_key: "..."
    api_secret: "..."
    fetch_spot: true
    fetch_futures: true
  pionex:
    api_key: "..."
    api_secret: "..."
"""
import os
import sys
import json
import time
import hmac
import hashlib
import argparse
import datetime
import subprocess
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    print(json.dumps({'error': '缺少 requests 套件，請 pip install requests'}))
    sys.exit(1)

BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_FILE     = os.path.join(BASE_DIR, 'data', 'crypto_balance.json')
CRYPTO_CONF  = os.path.join(BASE_DIR, 'crypto_config.yaml')
PIONEX_CONF  = os.path.join(BASE_DIR, 'pionex_bot', 'config.yaml')

NWIN_FLAG = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

# 穩定幣直接用 1:1 USD 換算（避免多一次 API 呼叫）
STABLES = {'USDT', 'USDC', 'BUSD', 'DAI', 'FDUSD', 'TUSD', 'USDP', 'USDD'}


# ══════════════════════════════════════════════════════════════
#  設定檔
# ══════════════════════════════════════════════════════════════
def _load_yaml(path):
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        with open(path, encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        return _parse_simple_yaml(path)
    except Exception:
        return {}


def _parse_simple_yaml(path):
    """最小的 yaml 解析（只支援 key: value 與 key.sub: value，供沒裝 PyYAML 時 fallback）。"""
    result, stack = {}, []
    with open(path, encoding='utf-8') as f:
        for line in f:
            raw = line.rstrip('\n')
            if not raw.strip() or raw.strip().startswith('#'):
                continue
            indent = len(raw) - len(raw.lstrip(' '))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1] if stack else result
            stripped = raw.strip()
            if ':' in stripped:
                k, _, v = stripped.partition(':')
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if v == '':
                    parent[k] = {}
                    stack.append((indent, parent[k]))
                else:
                    if v.lower() in ('true', 'false'):
                        v = (v.lower() == 'true')
                    else:
                        try: v = float(v) if '.' in v else int(v)
                        except ValueError: pass
                    parent[k] = v
    return result


def load_config():
    """合併 crypto_config.yaml + pionex_bot/config.yaml（前者優先）。
    僅讀取 Binance；派網因為公開 API 不含 bot/earn 鎖倉，改由前端手動輸入 TWD。"""
    cfg_crypto = _load_yaml(CRYPTO_CONF)
    cfg_pionex = _load_yaml(PIONEX_CONF)

    out = {'binance': {}}

    # Binance：先 crypto_config，再 fallback pionex_bot（沿用現成的金鑰）
    bn = cfg_crypto.get('binance') or {}
    out['binance']['api_key']       = bn.get('api_key')       or cfg_pionex.get('api_key')
    out['binance']['api_secret']    = bn.get('api_secret')    or cfg_pionex.get('api_secret')
    out['binance']['fetch_spot']    = bn.get('fetch_spot', True)
    out['binance']['fetch_futures'] = bn.get('fetch_futures', True)
    out['binance']['proxy']         = bn.get('proxy')         or cfg_pionex.get('proxy')

    return out


# ══════════════════════════════════════════════════════════════
#  Binance
# ══════════════════════════════════════════════════════════════
def _binance_session(api_key, proxy=None):
    s = requests.Session()
    s.headers.update({'X-MBX-APIKEY': api_key})
    if proxy:
        s.proxies = {'http': proxy, 'https': proxy}
    return s


def _binance_sign(api_secret, params):
    params['timestamp'] = int(time.time() * 1000)
    qs = urlencode(params)
    sig = hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    params['signature'] = sig
    return params


def _binance_get(session, base_url, path, api_secret, params=None):
    params = dict(params or {})
    _binance_sign(api_secret, params)
    r = session.get(base_url + path, params=params, timeout=10)
    data = r.json()
    if isinstance(data, dict) and data.get('code') and int(data.get('code', 0)) < 0:
        raise RuntimeError(f"Binance API {data.get('code')}: {data.get('msg')}")
    return data


def _binance_spot_prices(session):
    """GET /api/v3/ticker/price → {asset(USDT-quoted symbol): price}"""
    try:
        r = session.get('https://api.binance.com/api/v3/ticker/price', timeout=10)
        rows = r.json() if r.ok else []
        # symbol like "BTCUSDT" → {"BTC": 62000.0}
        out = {}
        for it in rows:
            sym = it.get('symbol', '')
            if sym.endswith('USDT'):
                asset = sym[:-4]
                try: out[asset] = float(it.get('price', 0))
                except: pass
        return out
    except Exception:
        return {}


def fetch_binance_spot(api_key, api_secret, proxy=None):
    session  = _binance_session(api_key, proxy)
    data     = _binance_get(session, 'https://api.binance.com', '/api/v3/account', api_secret)
    balances = data.get('balances', [])
    prices   = _binance_spot_prices(session)

    total_usd = 0.0
    details   = []
    for b in balances:
        asset = b.get('asset', '')
        try:
            amt = float(b.get('free', 0)) + float(b.get('locked', 0))
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        if asset in STABLES:
            usd = amt
        else:
            px = prices.get(asset)
            if px is None or px <= 0:
                continue
            usd = amt * px
        if usd < 0.5:  # 塵埃過濾
            continue
        total_usd += usd
        details.append({'asset': asset, 'amount': amt, 'usd': round(usd, 2)})
    details.sort(key=lambda x: -x['usd'])
    return {
        'equity_usd': round(total_usd, 2),
        'wallet_usd': round(total_usd, 2),
        'assets':     details[:20],   # 只保留前 20 大
    }


def fetch_binance_futures(api_key, api_secret, proxy=None):
    session = _binance_session(api_key, proxy)
    data    = _binance_get(session, 'https://fapi.binance.com', '/fapi/v2/account', api_secret)
    def _f(k):
        try: return float(data.get(k, 0))
        except (TypeError, ValueError): return 0.0
    equity      = _f('totalMarginBalance')    # 權益（含未實現損益）
    wallet      = _f('totalWalletBalance')    # 錢包（本金）
    unrealized  = _f('totalUnrealizedProfit')
    return {
        'equity_usd':     round(equity, 2),
        'wallet_usd':     round(wallet, 2),
        'unrealized_usd': round(unrealized, 2),
    }


# 派網（Pionex）：
#   公開 API 的 /api/v1/account/balances 只回現貨錢包，
#   doc 明文「excludes bot and earn account」。網格/DCA/Earn 鎖倉無法 API 取得。
#   解法：前端（portfolio-tracker-v13.html）提供「派網總資產」TWD 手動輸入框，
#   由使用者直接看 App 填，這支腳本不再觸碰派網 API。


# ══════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════
def build_report():
    cfg = load_config()
    exchanges = {}
    errors = {}

    bn = cfg['binance']
    if bn.get('api_key') and bn.get('api_secret'):
        if bn.get('fetch_spot'):
            try:
                exchanges['binance_spot'] = fetch_binance_spot(bn['api_key'], bn['api_secret'], bn.get('proxy'))
            except Exception as e:
                errors['binance_spot'] = f'{type(e).__name__}: {e}'[:200]
        if bn.get('fetch_futures'):
            try:
                exchanges['binance_futures'] = fetch_binance_futures(bn['api_key'], bn['api_secret'], bn.get('proxy'))
            except Exception as e:
                errors['binance_futures'] = f'{type(e).__name__}: {e}'[:200]
    else:
        errors['binance'] = '未設定 api_key / api_secret（crypto_config.yaml 或 pionex_bot/config.yaml）'

    # 派網：交由前端手動輸入 TWD（Pionex 公開 API 不含 bot/earn 鎖倉）

    total_usd = round(sum((ex.get('equity_usd') or 0) for ex in exchanges.values()), 2)
    return {
        'generated': datetime.datetime.now().isoformat(timespec='seconds'),
        'total_usd': total_usd,
        'exchanges': exchanges,
        'errors':    errors,
    }


def save_report(report):
    """若本次所有交易所都失敗（exchanges 空），保留上一次的 total_usd/exchanges，
    只更新 errors + generated + last_success_ts，避免 Binance 短暫抽風時畫面瞬間掉 0。
    另：永遠保留前一版的 manual_pionex_twd（跨裝置同步的派網手動金額），
    避免 fetch 覆寫使用者在電腦版輸入的值。"""
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)

    prev = None
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE, encoding='utf-8') as f:
                prev = json.load(f)
        except Exception:
            prev = None

    if not report.get('exchanges') and isinstance(prev, dict) and prev.get('exchanges'):
        merged = dict(prev)
        merged['errors']          = report.get('errors', {})
        merged['last_error_ts']   = report.get('generated')
        if 'last_success_ts' not in merged:
            merged['last_success_ts'] = prev.get('generated')
        report = merged
    elif report.get('exchanges'):
        # 本次成功：記錄最新成功時間
        report['last_success_ts'] = report.get('generated')

    # 保留前一版的 manual_pionex_twd（不會被 fetch 邏輯產出）
    if isinstance(prev, dict) and prev.get('manual_pionex_twd') is not None and report.get('manual_pionex_twd') is None:
        report['manual_pionex_twd'] = prev['manual_pionex_twd']

    tmp = OUT_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_FILE)


def git_commit_push():
    try:
        subprocess.run(['git', '-C', BASE_DIR, 'add', 'data/crypto_balance.json'],
                       check=True, capture_output=True, text=True, creationflags=NWIN_FLAG)
        diff = subprocess.run(['git', '-C', BASE_DIR, 'diff', '--cached', '--quiet'],
                              capture_output=True, creationflags=NWIN_FLAG)
        if diff.returncode == 0:
            return  # 無變動
        date_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        subprocess.run(['git', '-C', BASE_DIR, 'commit', '-m', f'chore: crypto balance {date_str}'],
                       check=True, capture_output=True, text=True, creationflags=NWIN_FLAG)
        subprocess.run(['git', '-C', BASE_DIR, 'pull', '--rebase', 'origin', 'main'],
                       check=False, capture_output=True, text=True, creationflags=NWIN_FLAG)
        subprocess.run(['git', '-C', BASE_DIR, 'push', 'origin', 'main'],
                       check=True, capture_output=True, text=True, creationflags=NWIN_FLAG)
    except subprocess.CalledProcessError as e:
        print(f'[crypto] git 操作失敗：{(e.stderr or "")[-200:]}', file=sys.stderr)
    except Exception as e:
        print(f'[crypto] git 例外：{e}', file=sys.stderr)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--commit', action='store_true',
                    help='同時 git push data/crypto_balance.json（讓手機透過 GitHub Pages 看得到）')
    args = ap.parse_args()
    rep = build_report()
    save_report(rep)
    print(json.dumps(rep, ensure_ascii=False))
    if args.commit:
        git_commit_push()
