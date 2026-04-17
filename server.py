#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Portfolio Tracker Backend
─────────────────────────────────────────────────────────────
Static file server + API proxy + Fubon SDK bridge

Endpoints
─────────
GET  /proxy?url=...           → CORS proxy for Yahoo Finance chart
GET  /yfundamentals?symbol=   → PE/FPE/PEG/EPS via yfinance

GET  /fmx/state               → fmx_state.json
GET  /fmx/positions           → SDK positions  (subprocess, cache 60s)
GET  /fmx/quote?symbol=       → fmx_live_quotes.json (written by daemon)
GET  /fmx/market_quotes       → SDK market quotes (subprocess, cache 12s)
POST /fmx/control             → update fmx_state.json + optional action
POST /fmx/sync                → manual sync fmx_state.json

GET  /stock/quote?symbol=     → SDK stock quote (subprocess)
GET  /stock/positions         → SDK stock positions (subprocess, cache 60s)
POST /stock/order             → SDK stock order (subprocess)
POST /stock/cancel            → SDK cancel order (subprocess)
"""
import http.server
import urllib.request
import urllib.parse
import json
import os
import sys
import subprocess
import threading
import time

PORT = int(os.environ.get('PORT', 3000))
DIR  = os.path.dirname(os.path.abspath(__file__))
BOT  = os.path.join(DIR, 'fubon_bot')

# 優先使用 python_stock.exe（已加入 Surfshark Bypasser，走真實 IP）
# 若不存在則 fallback 到目前的 Python（直接在 PC 跑時也能正常運作）
_py_dir   = os.path.dirname(sys.executable)
_py_stock = os.path.join(_py_dir, 'python_stock.exe')
PY = _py_stock if os.path.exists(_py_stock) else sys.executable

STATE_FILE       = os.path.join(DIR, 'fmx_state.json')
LIVE_QUOTES_FILE = os.path.join(DIR, 'fmx_live_quotes.json')
STRATEGY_SCRIPT  = os.path.join(BOT, 'strategy-fmx-live.py')
CONFIG_FILE      = os.path.join(BOT, 'config.yaml')

# ── subprocess environment ────────────────────────────────
_ENV = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}

# ── strategy process tracker ─────────────────────────────
_strategy_proc      = None   # Popen object for the running strategy loop
_strategy_proc_lock = threading.Lock()

# ── daemon process tracker ────────────────────────────────
_daemon_proc      = None
_daemon_proc_lock = threading.Lock()
DAEMON_SCRIPT     = os.path.join(BOT, 'fmx_quote_daemon.py')

def _daemon_watchdog():
    """背景執行緒：確保 fmx_quote_daemon.py 持續運行"""
    global _daemon_proc
    time.sleep(3)   # 等 server 啟動完成
    print('[daemon] watchdog 啟動', flush=True)
    while True:
        try:
            with _daemon_proc_lock:
                proc = _daemon_proc
                dead = (proc is None or proc.poll() is not None)
            if dead:
                if proc is not None:
                    print(f'[daemon] 偵測到 daemon 結束（returncode={proc.poll()}），重新啟動…', flush=True)
                else:
                    print('[daemon] 首次啟動 fmx_quote_daemon.py…', flush=True)
                # 注意：不可用 stdout=PIPE 而不讀，否則 Windows 64KB 管道緩衝
                # 滿了之後 daemon 的 print() 會阻塞，造成 daemon 看起來卡住。
                # daemon 本身會寫 logs/fmx_daemon.log，stdout 直接丟棄即可。
                new_proc = subprocess.Popen(
                    [PY, DAEMON_SCRIPT],
                    env=_ENV,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                with _daemon_proc_lock:
                    _daemon_proc = new_proc
                print(f'[daemon] PID {new_proc.pid} 啟動成功', flush=True)
        except Exception as e:
            print(f'[daemon] watchdog 錯誤: {e}', flush=True)
        time.sleep(15)   # 每 15 秒檢查一次


def _run_subprocess(script, args=(), timeout=45):
    """
    Run fubon_bot/<script> with args, return parsed JSON.
    fmx_positions.py emits multiple JSON lines; we take the LAST one.
    """
    cmd = [PY, os.path.join(BOT, script)] + list(args)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
            env=_ENV,
        )
        out = (proc.stdout or '').strip()
        if not out:
            stderr = (proc.stderr or '').strip()[-500:]
            return {'error': f'no stdout: {stderr}', 'status': 'fail'}
        # Take last non-empty JSON line (heartbeat scripts emit multiple lines)
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line:
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {'error': 'no valid JSON line', 'raw': out[:300], 'status': 'fail'}
    except subprocess.TimeoutExpired:
        return {'error': f'subprocess timeout ({timeout}s)', 'status': 'fail'}
    except Exception as e:
        return {'error': str(e), 'status': 'fail'}


# ── fmx/positions cache ───────────────────────────────────
_fmx_pos_cache = None
_fmx_pos_ts    = 0.0
_fmx_pos_lock  = threading.Lock()
FMX_POS_TTL    = 60   # seconds

# ── fmx/market_quotes cache ───────────────────────────────
_fmx_mq_cache  = None
_fmx_mq_ts     = 0.0
_fmx_mq_lock   = threading.Lock()
FMX_MQ_TTL     = 12   # seconds

# ── stock/positions cache ─────────────────────────────────
_stock_pos_cache = None
_stock_pos_ts    = 0.0
_stock_pos_lock  = threading.Lock()
STOCK_POS_TTL    = 60  # seconds

# ── yfinance cache ────────────────────────────────────────
# cache: {symbol: (ts, result)}; 成功 1 小時、失敗 3 分鐘（避免 yahoo 短暫抽風鎖死）
_yf_cache = {}
_yf_lock  = threading.Lock()
_YF_OK_TTL   = 3600
_YF_ERR_TTL  = 180
_YF_TIMEOUT  = 20   # 單次 yfinance 呼叫最長 20 秒，超過視為失敗
import concurrent.futures as _yf_fut_mod
_yf_executor = _yf_fut_mod.ThreadPoolExecutor(max_workers=2, thread_name_prefix='yf')


def _get_fundamentals_impl(symbol):
    """實際執行 yfinance 查詢（同步，可能會卡住）"""
    try:
        import yfinance as yf
        import pandas as pd
        t    = yf.Ticker(symbol)
        info = t.info

        eps_cur_q  = None
        eps_next_q = None
        try:
            ee = t.earnings_estimate
            if ee is not None:
                if '0q' in ee.index:
                    eps_cur_q  = float(ee.loc['0q',  'avg'])
                if '+1q' in ee.index:
                    eps_next_q = float(ee.loc['+1q', 'avg'])
        except Exception:
            pass

        rev_fwd = None
        try:
            re = t.revenue_estimate
            if re is not None and '+1y' in re.index:
                rev_fwd = float(re.loc['+1y', 'growth'])
        except Exception:
            pass

        hist_avg_pe = None
        try:
            hist = t.history(period='5y', interval='3mo')['Close']
            hist.index = hist.index.tz_localize(None)
            fin = t.quarterly_financials
            for row in ['Diluted EPS', 'Basic EPS', 'EPS']:
                if row in fin.index:
                    eps_s = fin.loc[row].sort_index()
                    eps_s.index = pd.to_datetime(eps_s.index).tz_localize(None)
                    pe_list = []
                    for date, price in hist.items():
                        ttm = eps_s[eps_s.index <= date].head(4).sum()
                        if ttm > 0:
                            pe_list.append(price / ttm)
                    if pe_list:
                        hist_avg_pe = round(sum(pe_list) / len(pe_list), 2)
                    break
        except Exception:
            pass

        result = {
            'pe':           info.get('trailingPE'),
            'fpe':          info.get('forwardPE'),
            'peg':          info.get('trailingPegRatio'),
            'ps':           info.get('priceToSalesTrailing12Months'),
            'pb':           info.get('priceToBook'),
            'rev_yoy':      info.get('revenueGrowth'),
            'rev_fwd':      rev_fwd,
            'hist_avg_pe':  hist_avg_pe,
            'eps_ttm':      info.get('trailingEps'),
            'eps_cur_q':    eps_cur_q,
            'eps_next_q2':  eps_next_q,
            'eps_cur_y':    info.get('epsCurrentYear'),
            'eps_next_y':   info.get('epsForward'),
        }
    except Exception as e:
        result = {'error': str(e)}
    return result


def get_fundamentals(symbol):
    """對外入口：帶 TTL cache 與 per-call timeout 保護 yfinance 可能卡住的問題"""
    now = time.time()
    with _yf_lock:
        cached = _yf_cache.get(symbol)
        if cached:
            ts, data = cached
            is_err  = isinstance(data, dict) and data.get('error')
            ttl     = _YF_ERR_TTL if is_err else _YF_OK_TTL
            if now - ts < ttl:
                return data
    try:
        fut    = _yf_executor.submit(_get_fundamentals_impl, symbol)
        result = fut.result(timeout=_YF_TIMEOUT)
    except _yf_fut_mod.TimeoutError:
        # 不要殺 thread（yfinance 可能在 C extension 裡），任它背景自己結束
        result = {'error': f'yfinance timeout after {_YF_TIMEOUT}s', 'timeout': True}
    except Exception as e:
        result = {'error': str(e)}
    with _yf_lock:
        _yf_cache[symbol] = (time.time(), result)
    return result


# ── state file helpers ────────────────────────────────────
_state_lock = threading.Lock()

def _read_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        return {'error': str(e)}

def _write_state(updates: dict):
    """Merge updates into fmx_state.json atomically."""
    with _state_lock:
        try:
            st = _read_state() if os.path.exists(STATE_FILE) else {}
            if 'error' in st and len(st) == 1:
                st = {}
            st.update(updates)
            import datetime
            st['last_updated'] = datetime.date.today().isoformat()
            tmp = STATE_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_FILE)
            return st
        except Exception as e:
            return {'error': str(e)}


def _strategy_watchdog():
    """背景執行緒：若使用者已按過「啟動策略」(bot_running=true)，
    但 strategy 程序卻消失（crash / OOM / 系統重啟），自動重啟之。

    設計要點：
      1. 只在 `bot_running=true` 時重啟；使用者按「停止」後不會被再拉起。
      2. 透過 `_strategy_proc.poll()` 優先判斷；若為 None（例如 server 重啟
         後丟失 Popen handle），退回到 wmic 掃描。
      3. 用 DEVNULL 重導 stdout/stderr（同 daemon 的 Windows pipe 教訓）。
      4. 每 30 秒檢查一次；避免與 start/stop action 競爭用 `_strategy_proc_lock`。
    """
    global _strategy_proc
    time.sleep(10)   # 等 daemon watchdog、server 都啟動完成
    print('[strategy] watchdog 啟動', flush=True)
    while True:
        try:
            st = _read_state() if os.path.exists(STATE_FILE) else {}
            want_running = bool(st.get('bot_running', False))
            if not want_running:
                time.sleep(30)
                continue

            # 檢查是否真的有 strategy 在跑
            with _strategy_proc_lock:
                proc = _strategy_proc
                tracked_alive = (proc is not None and proc.poll() is None)

            if tracked_alive:
                time.sleep(30)
                continue

            # Popen handle 無效 → 用 wmic 做第二層確認
            external_pids = _find_strategy_procs()
            if external_pids:
                # 外部仍存活（通常是 server 重啟後遺失 handle），保留不動
                time.sleep(30)
                continue

            # 真的死了，重啟
            print('[strategy] 偵測到策略程序不存在但 bot_running=true，自動重啟…', flush=True)
            settings = st.get('settings', {}) if isinstance(st.get('settings'), dict) else {}
            dry = bool(settings.get('dry_run', st.get('dry_run', False)))
            args = [PY, STRATEGY_SCRIPT]
            if dry:
                args.append('--dry-run')
            with _strategy_proc_lock:
                _strategy_proc = subprocess.Popen(
                    args,
                    cwd=BOT,
                    env=_ENV,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            print(f'[strategy] PID {_strategy_proc.pid} 自動重啟成功 (dry_run={dry})', flush=True)
        except Exception as e:
            print(f'[strategy] watchdog 錯誤: {e}', flush=True)
        time.sleep(30)


def _find_strategy_procs():
    """Return list of PIDs running strategy-fmx-live.py (excluding current process)."""
    pids = []
    try:
        out = subprocess.check_output(
            ['wmic', 'process', 'where',
             "name='python.exe' or name='python_stock.exe'",
             'get', 'ProcessId,CommandLine'],
            text=True, errors='replace', timeout=5
        )
        for line in out.splitlines():
            if 'strategy-fmx-live' in line:
                parts = line.strip().split()
                if parts:
                    try:
                        pids.append(int(parts[-1]))
                    except ValueError:
                        pass
    except Exception:
        pass
    return pids


# ── HTTP handler ──────────────────────────────────────────
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    # ── routing ───────────────────────────────────────────
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path

        if p == '/proxy':
            self._proxy(parsed)
        elif p == '/yfundamentals':
            self._fundamentals(parsed)
        elif p == '/fmx/state':
            self._fmx_state()
        elif p in ('/fmx/positions', '/fmx/positions/'):
            self._fmx_positions(parsed)
        elif p == '/fmx/quote':
            self._fmx_quote(parsed)
        elif p == '/fmx/market_quotes':
            self._fmx_market_quotes(parsed)
        elif p == '/fmx/runtime':
            self._fmx_runtime()
        elif p == '/stock/quote':
            self._stock_quote(parsed)
        elif p in ('/stock/positions', '/stock/positions/'):
            self._stock_positions(parsed)
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path

        if p == '/fmx/control':
            self._fmx_control()
        elif p == '/fmx/sync':
            self._fmx_sync()
        elif p == '/stock/order':
            self._stock_order()
        elif p == '/stock/cancel':
            self._stock_cancel()
        else:
            self.send_error(404, f'Not found: {p}')

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    # ── helpers ───────────────────────────────────────────
    def _cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _qs(self, parsed):
        return urllib.parse.parse_qs(parsed.query)

    # ── /proxy ────────────────────────────────────────────
    def _proxy(self, parsed):
        params = self._qs(parsed)
        target = params.get('url', [''])[0]
        if not target:
            self.send_error(400, 'Missing url parameter')
            return
        try:
            req = urllib.request.Request(target, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
                              ' AppleWebKit/537.36 (KHTML, like Gecko)'
                              ' Chrome/124.0 Safari/537.36',
                'Accept': 'application/json, */*',
                'Accept-Language': 'en-US,en;q=0.9',
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self._cors_headers()
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(502, str(e))

    # ── /yfundamentals ────────────────────────────────────
    def _fundamentals(self, parsed):
        params = self._qs(parsed)
        symbol = params.get('symbol', [''])[0].upper()
        if not symbol:
            self.send_error(400, 'Missing symbol')
            return
        self._send_json(get_fundamentals(symbol))

    # ── /fmx/state ────────────────────────────────────────
    def _fmx_state(self):
        self._send_json(_read_state())

    # ── /fmx/runtime ─────────────────────────────────────
    # 回傳策略/ daemon 程序運行狀態：pid / alive / bot_running / last_run 等
    def _fmx_runtime(self):
        global _strategy_proc, _daemon_proc
        result = {
            'server_pid': os.getpid(),
            'bot_running': False,
            'strategy_alive': False,
            'strategy_pid': None,
            'strategy_pids_external': [],
            'daemon_alive': False,
            'daemon_pid': None,
            'strategy_last_run': None,
            'sdk_synced_ts': None,
            'daemon_ts': None,
        }
        try:
            st = _read_state() if os.path.exists(STATE_FILE) else {}
            result['bot_running']        = bool(st.get('bot_running', False))
            result['strategy_last_run']  = st.get('strategy_last_run')
            result['sdk_synced_ts']      = st.get('sdk_synced_ts') or st.get('save_ts')
        except Exception:
            pass

        # Popen handle 判斷
        try:
            with _strategy_proc_lock:
                p = _strategy_proc
                if p is not None and p.poll() is None:
                    result['strategy_alive'] = True
                    result['strategy_pid']   = p.pid
        except Exception:
            pass

        try:
            with _daemon_proc_lock:
                p = _daemon_proc
                if p is not None and p.poll() is None:
                    result['daemon_alive'] = True
                    result['daemon_pid']   = p.pid
        except Exception:
            pass

        # 外部程序（server 重啟後遺失 handle 時才有意義）
        try:
            external = _find_strategy_procs()
            result['strategy_pids_external'] = external
            if not result['strategy_alive'] and external:
                result['strategy_alive'] = True
                result['strategy_pid']   = external[0]
        except Exception:
            pass

        # daemon 最近寫 quotes 的時間
        try:
            if os.path.exists(LIVE_QUOTES_FILE):
                with open(LIVE_QUOTES_FILE, encoding='utf-8') as f:
                    q = json.load(f)
                result['daemon_ts']     = q.get('ts')
                result['daemon_status'] = q.get('status')
                result['daemon_ok_syms'] = q.get('ok_syms')
        except Exception:
            pass

        self._send_json(result)

    # ── /fmx/positions ───────────────────────────────────
    def _fmx_positions(self, parsed):
        global _fmx_pos_cache, _fmx_pos_ts
        params  = self._qs(parsed)
        refresh = bool(params.get('refresh'))
        now     = time.time()

        with _fmx_pos_lock:
            if not refresh and _fmx_pos_cache and (now - _fmx_pos_ts) < FMX_POS_TTL:
                self._send_json(_fmx_pos_cache)
                return
            result = _run_subprocess('fmx_positions.py', timeout=45)
            _fmx_pos_cache = result
            _fmx_pos_ts    = time.time()

        self._send_json(result)

    # ── /fmx/quote ───────────────────────────────────────
    def _fmx_quote(self, parsed):
        params = self._qs(parsed)
        symbol = params.get('symbol', ['MXFR1'])[0].upper()
        try:
            with open(LIVE_QUOTES_FILE, encoding='utf-8') as f:
                data = json.load(f)
            # fmx_live_quotes.json = {quotes: {MXFR1:{...}, TXFR1:{...}...}, ts, status, ...}
            quotes = data.get('quotes', {}) if isinstance(data, dict) else {}
            if not quotes and isinstance(data, dict):
                # fallback: root is the quotes dict itself
                quotes = {k: v for k, v in data.items() if isinstance(v, dict)}
            q = quotes.get(symbol) or quotes.get(symbol.replace('R1', ''))
            if q is None:
                q = next(iter(quotes.values()), {})
            ts     = data.get('ts')     if isinstance(data, dict) else None
            status = data.get('status') if isinstance(data, dict) else None
            self._send_json({'symbol': symbol, 'ts': ts, 'status': status, **q})
        except FileNotFoundError:
            self._send_json({'error': 'fmx_live_quotes.json 不存在，請確認 fmx_quote_daemon 已啟動', 'last': None})
        except Exception as e:
            self._send_json({'error': str(e), 'last': None})

    # ── /fmx/market_quotes ───────────────────────────────
    def _fmx_market_quotes(self, parsed):
        global _fmx_mq_cache, _fmx_mq_ts
        params  = self._qs(parsed)
        refresh = bool(params.get('refresh'))
        now     = time.time()

        with _fmx_mq_lock:
            if not refresh and _fmx_mq_cache and (now - _fmx_mq_ts) < FMX_MQ_TTL:
                self._send_json(_fmx_mq_cache)
                return
            result = _run_subprocess('fmx_market_quotes.py', timeout=40)
            _fmx_mq_cache = result
            _fmx_mq_ts    = time.time()

        self._send_json(result)

    # ── POST /fmx/control ─────────────────────────────────
    def _fmx_control(self):
        global _strategy_proc
        body    = self._read_body()
        action  = body.get('action', 'settings')
        dry_run = body.get('dry_run', False)
        settings = body.get('settings', {})  # 'start'/'once'/'settings' 附帶的設定

        # ── 合併設定到 fmx_state.json ──────────────────────────
        WRITABLE = {
            'dry_run', 'target_leverage', 'max_leverage',
            'dca_threshold_pct', 'pyramid_trigger',
            'trend_filter', 'high_period', 'low_period',
            'fee_per_lot', 'initial_capital',
        }
        state_updates = {k: v for k, v in settings.items() if k in WRITABLE}
        if state_updates:
            _write_state(state_updates)

        # ── 同步設定到 config.yaml ─────────────────────────────
        if settings and os.path.exists(CONFIG_FILE):
            try:
                import yaml
                with open(CONFIG_FILE, encoding='utf-8') as f:
                    cfg = yaml.safe_load(f) or {}
                YAML_MAP = {
                    'dry_run': 'dry_run',
                    'target_leverage': 'target_leverage',
                    'max_leverage': 'max_leverage',
                    'dca_threshold_pct': 'dca_threshold_pct',
                    'pyramid_trigger': 'pyramid_trigger',
                    'trend_filter': 'trend_filter',
                    'high_period': 'high_period',
                    'low_period': 'low_period',
                    'fee_per_lot': 'fee_per_lot',
                }
                changed = False
                for js_key, yaml_key in YAML_MAP.items():
                    if js_key in settings:
                        cfg[yaml_key] = settings[js_key]
                        changed = True
                if changed:
                    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
            except Exception as e:
                pass  # config.yaml 更新失敗不影響主流程

        # ── action 路由 ────────────────────────────────────────
        if action == 'settings':
            self._send_json({'status': 'ok', 'message': '設定已儲存'})

        elif action == 'start':
            # 先殺掉所有殘留的 strategy 程序
            for pid in _find_strategy_procs():
                try:
                    subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                except Exception:
                    pass
            with _strategy_proc_lock:
                if _strategy_proc and _strategy_proc.poll() is None:
                    _strategy_proc.terminate()
                    try: _strategy_proc.wait(timeout=5)
                    except Exception: _strategy_proc.kill()
                args = [PY, STRATEGY_SCRIPT]
                if settings.get('dry_run', dry_run):
                    args.append('--dry-run')
                _strategy_proc = subprocess.Popen(
                    args,
                    cwd=BOT,
                    env=_ENV,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            _write_state({'bot_running': True})
            mode = '模擬' if settings.get('dry_run', dry_run) else '實單'
            self._send_json({'status': 'ok', 'message': f'策略已啟動（{mode}模式）', 'pid': _strategy_proc.pid})

        elif action == 'stop':
            with _strategy_proc_lock:
                stopped = False
                if _strategy_proc and _strategy_proc.poll() is None:
                    _strategy_proc.terminate()
                    try: _strategy_proc.wait(timeout=5)
                    except Exception: _strategy_proc.kill()
                    stopped = True
                    _strategy_proc = None
                # 也嘗試殺掉其他殘留的 strategy 程序
                for pid in _find_strategy_procs():
                    try:
                        subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                        stopped = True
                    except Exception:
                        pass
            _write_state({'bot_running': False})
            self._send_json({'status': 'ok', 'message': '策略已停止' if stopped else '策略本來就未執行'})

        elif action == 'once':
            args = [PY, STRATEGY_SCRIPT, '--once']
            if settings.get('dry_run', dry_run):
                args.append('--dry-run')
            subprocess.Popen(args, cwd=BOT, env=_ENV,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._send_json({'status': 'ok', 'message': '執行一次（背景進行中）'})

        elif action == 'add_leverage':
            lots       = int(body.get('lots', 1))
            order_type = body.get('order_type', 'limit')
            spread     = float(body.get('spread', 5))
            args = [PY, STRATEGY_SCRIPT, '--add-leverage',
                    '--lots', str(lots),
                    '--order-type', order_type,
                    '--spread', str(spread)]
            if dry_run:
                args.append('--dry-run')
            subprocess.Popen(args, cwd=BOT, env=_ENV,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._send_json({'status': 'ok', 'message': f'加槓桿 {lots} 口（背景進行中）'})

        elif action == 'deleverage':
            lots       = int(body.get('lots', 1))
            order_type = body.get('order_type', 'limit')
            spread     = float(body.get('spread', 5))
            args = [PY, STRATEGY_SCRIPT, '--reduce-leverage',
                    '--lots', str(lots),
                    '--order-type', order_type,
                    '--spread', str(spread)]
            if dry_run:
                args.append('--dry-run')
            subprocess.Popen(args, cwd=BOT, env=_ENV,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._send_json({'status': 'ok', 'message': f'降槓桿 {lots} 口（背景進行中）'})

        elif action == 'rollover':
            lots       = body.get('lots')
            order_type = body.get('order_type', 'limit')
            spread     = float(body.get('spread', 5))
            args = [PY, STRATEGY_SCRIPT, '--rollover',
                    '--order-type', order_type,
                    '--spread', str(spread)]
            if lots:
                args += ['--lots', str(int(lots))]
            if dry_run:
                args.append('--dry-run')
            subprocess.Popen(args, cwd=BOT, env=_ENV,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._send_json({'status': 'ok', 'message': '轉倉指令已送出（背景進行中）'})

        elif action == 'reset':
            args = [PY, STRATEGY_SCRIPT, '--reset']
            if dry_run:
                args.append('--dry-run')
            subprocess.Popen(args, cwd=BOT, env=_ENV,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._send_json({'status': 'ok', 'message': '重置倉位指令已送出（背景進行中）'})

        else:
            self._send_json({'status': 'ok', 'message': f'{action} 已收到'})

    # ── POST /fmx/sync ────────────────────────────────────
    def _fmx_sync(self):
        body = self._read_body()
        updates = {}
        if body.get('contracts') is not None:
            updates['contracts'] = float(body['contracts'])
        if body.get('avg_cost') is not None:
            updates['avg_cost'] = float(body['avg_cost'])
        if body.get('cur_price') is not None:
            updates['last_price'] = float(body['cur_price'])
        if body.get('equity') is not None:
            updates['equity'] = float(body['equity'])
        import datetime
        updates['sdk_synced_ts'] = datetime.datetime.now().isoformat(timespec='seconds')
        st = _write_state(updates)
        self._send_json({'status': 'ok', 'message': '手動同步完成', 'state': st})

    # ── GET /stock/quote ──────────────────────────────────
    def _stock_quote(self, parsed):
        params = self._qs(parsed)
        symbol = params.get('symbol', ['2330'])[0].strip()
        result = _run_subprocess('stock_quote.py', ['--symbol', symbol], timeout=30)
        self._send_json(result)

    # ── GET /stock/positions ──────────────────────────────
    def _stock_positions(self, parsed):
        global _stock_pos_cache, _stock_pos_ts
        params  = self._qs(parsed)
        refresh = bool(params.get('refresh'))
        now     = time.time()

        with _stock_pos_lock:
            if not refresh and _stock_pos_cache and (now - _stock_pos_ts) < STOCK_POS_TTL:
                self._send_json(_stock_pos_cache)
                return
            result = _run_subprocess('stock_positions.py', timeout=45)
            _stock_pos_cache = result
            _stock_pos_ts    = time.time()

        self._send_json(result)

    # ── POST /stock/order ─────────────────────────────────
    def _stock_order(self):
        body = self._read_body()
        args = [
            '--side',   str(body.get('side', 'buy')),
            '--symbol', str(body.get('symbol', '')),
            '--price',  str(body.get('price', 0)),
            '--lots',   str(body.get('lots', 1)),
        ]
        if body.get('market'):
            args.append('--market')
        if body.get('dry_run'):
            args.append('--dry-run')
        result = _run_subprocess('stock_order.py', args, timeout=40)
        self._send_json(result)

    # ── POST /stock/cancel ────────────────────────────────
    def _stock_cancel(self):
        body = self._read_body()
        order_no = str(body.get('order_no', '')).strip()
        if not order_no:
            self._send_json({'error': 'order_no required', 'status': 'fail'})
            return
        result = _run_subprocess('stock_cancel.py', ['--order-no', order_no], timeout=30)
        self._send_json(result)

    def log_message(self, fmt, *args):
        pass  # silence access logs


if __name__ == '__main__':
    # 啟動 daemon watchdog 執行緒（確保 fmx_quote_daemon 持續運行）
    if os.path.exists(DAEMON_SCRIPT):
        t = threading.Thread(target=_daemon_watchdog, daemon=True, name='daemon-watchdog')
        t.start()
    else:
        print(f'[daemon] 找不到 {DAEMON_SCRIPT}，跳過自動啟動', flush=True)

    # 啟動 strategy watchdog 執行緒（在 bot_running=true 時自動重啟掛掉的策略）
    if os.path.exists(STRATEGY_SCRIPT):
        ts = threading.Thread(target=_strategy_watchdog, daemon=True, name='strategy-watchdog')
        ts.start()
    else:
        print(f'[strategy] 找不到 {STRATEGY_SCRIPT}，跳過自動守護', flush=True)

    with http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler) as srv:
        print(f'[server] running on http://0.0.0.0:{PORT}  (press Ctrl+C to stop)')
        srv.serve_forever()
