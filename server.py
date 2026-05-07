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
GET  /stock/kline?symbol=&interval=&range= → K-line OHLCV (subprocess, cache 120s)
GET  /stock/positions         → SDK stock positions (subprocess, cache 60s)
POST /stock/order             → SDK stock order (subprocess)
POST /stock/cancel            → SDK cancel order (subprocess)

GET  /sub/quote?symbol=&market= → 複委託 quote (e01 COM / Yahoo fallback, cache 20s)
GET  /sub/positions             → 複委託 持倉+多幣別餘額 (cache 60s)
GET  /sub/balance               → 多幣別餘額 only
POST /sub/order                 → 複委託 下單
POST /sub/cancel                → 複委託 委託取消
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
import datetime
import math
import socket

# Windows：禁止子行程彈出 CMD 視窗（CREATE_NO_WINDOW）
# 其他平台為 0（忽略 creationflags）。參見 skill.py pitfall B1。
NWIN_FLAG = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

PORT = int(os.environ.get('PORT', 3000))
DIR  = os.path.dirname(os.path.abspath(__file__))
BOT    = os.path.join(DIR, 'fubon_bot')
E01BOT = os.path.join(DIR, 'fubon_e01_bot')

# 優先使用 python_stock.exe（已加入 Surfshark Bypasser，走真實 IP）
# 若不存在則 fallback 到目前的 Python（直接在 PC 跑時也能正常運作）
_py_dir   = os.path.dirname(sys.executable)
_py_stock = os.path.join(_py_dir, 'python_stock.exe')
PY = _py_stock if os.path.exists(_py_stock) else sys.executable

# Surfshark VPN python：路由經 VPN，出口 IP 是 Binance 白名單（86.104.213.158）。
# 用於 crypto fetcher（呼叫 Binance API），其餘子程序維持 PY（真實台灣 IP）。
_py_vpn = r"C:\Python310-Trading\python.exe"
PY_VPN = _py_vpn if os.path.exists(_py_vpn) else PY

STATE_FILE       = os.path.join(DIR, 'fmx_state.json')
LIVE_QUOTES_FILE = os.path.join(DIR, 'fmx_live_quotes.json')
STRATEGY_SCRIPT  = os.path.join(BOT, 'strategy-fmx-live.py')
CONFIG_FILE      = os.path.join(BOT, 'config.yaml')
SUB_HOLDINGS_FILE = os.path.join(E01BOT, 'subbrokerage_holdings.json')

# ── subprocess environment ────────────────────────────────
_ENV = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}

# ── strategy process tracker ─────────────────────────────
_strategy_proc      = None   # Popen object for the running strategy loop
_strategy_proc_lock = threading.Lock()

# ── daemon process tracker ────────────────────────────────
_daemon_proc      = None
_daemon_proc_lock = threading.Lock()
DAEMON_SCRIPT     = os.path.join(BOT, 'fmx_quote_daemon.py')

# ── futopt archive scheduler ───────────────────────────────
ARCHIVE_SCRIPT      = os.path.join(BOT, 'futopt_archive.py')
_archive_lock       = threading.Lock()

_archive_last_snapshot = 0.0    # 最後一次 snapshot 的 epoch（無論成功）
_archive_last_backfill = None   # 最後一次 TAIFEX 回補的日期

# ── futures equity snapshot scheduler ─────────────────────
# 交易時段內每 FUT_SNAPSHOT_INTERVAL_MIN 分鐘（預設 15 min）快照 margin 欄位
# 到 data/futures_history.json，供前端計算「昨日漲跌（期貨）」＝ today_balance
# 日差，並讓手機 / GitHub Pages 版在交易時段也能看到接近即時的權益。
# 若設定 autopush，每次更新後會 git commit & push（snapshot_futures.py 內部會
# 判斷若檔案無變動則略過 commit，降低 git 噪音）。
# 盤中（13:45~14:05 結算前）台指權益會劇烈變動，收盤後 14:05 也會跑一次確保
# 最終日結值被寫入。
FUT_SNAPSHOT_SCRIPT = os.path.join(DIR, 'data', 'snapshot_futures.py')
_fut_snap_last_run  = 0.0        # 最後一次快照成功的 epoch
# 環境變數 FUT_SNAPSHOT_AUTOPUSH=1 時自動 git commit & push（預設開啟）
_fut_snap_autopush  = os.environ.get('FUT_SNAPSHOT_AUTOPUSH', '1') != '0'
# 交易時段快照間隔；非交易時段跳過（避免無意義的 SDK 呼叫 & commit）
_fut_snap_interval  = int(os.environ.get('FUT_SNAPSHOT_INTERVAL_MIN', '15')) * 60

# ── crypto balance fetcher（幣安 / 派網 淨值）─────────
# 每 10 分鐘更新一次 data/crypto_balance.json；前端 /crypto/balance 直接吃檔。
CRYPTO_FETCH_SCRIPT = os.path.join(DIR, 'data', 'fetch_crypto_balance.py')
CRYPTO_BALANCE_FILE = os.path.join(DIR, 'data', 'crypto_balance.json')
FUT_TRADES_FILE     = os.path.join(DIR, 'data', 'fut_trades.json')
CRYPTO_REFRESH_SEC  = 600        # 10 分鐘重新 fetch 一次
_crypto_last_fetch  = 0          # 上次 fetch 的 unix ts
# 環境變數 CRYPTO_AUTOPUSH=1 時自動 git commit & push（預設關閉，避免餘額頻繁刷 commit）
_crypto_autopush    = os.environ.get('CRYPTO_AUTOPUSH', '0') != '0'

# ── stock transaction ledger（股票買賣流水 / 已實現損益來源）────
STOCK_TXNS_FILE = os.path.join(DIR, 'stocker_txns.json')

# ── asset history snapshot（每日資產快照，前端 _saveAssetSnapshot POST 寫入）────
# schema: { "YYYY-MM-DD": {v, vt, c, ct, p}, ... }（與 v13 localStorage 同 schema）
ASSET_HISTORY_FILE = os.path.join(DIR, 'data', 'asset_history.json')

# ── Futu/Moomoo quote bridge ──────────────────────────────────────────────
# Optional dependency: moomoo-api (import name: moomoo) or futu-api (import name: futu).
# The frontend treats this as best-effort and falls back to Yahoo/Sheet if unavailable.
FUTU_HOST = os.environ.get('FUTU_HOST', '127.0.0.1')
FUTU_PORT = int(os.environ.get('FUTU_PORT', '11111'))
_futu_quote_cache = {'key': None, 'ts': 0.0, 'data': None}
_futu_quote_lock = threading.Lock()


def _import_futu_api():
    try:
        import futu as api  # type: ignore
        return api, 'futu'
    except Exception:
        pass
    try:
        import moomoo as api  # type: ignore
        return api, 'moomoo'
    except Exception:
        return None, None


def _safe_float(v):
    try:
        if v is None:
            return None
        n = float(v)
        if not math.isfinite(n):
            return None
        return n
    except Exception:
        return None


def _is_futu_opend_reachable(timeout=1.5):
    try:
        with socket.create_connection((FUTU_HOST, FUTU_PORT), timeout=timeout):
            return True
    except Exception:
        return False


def _futu_code_from_symbol(symbol):
    s = str(symbol or '').strip().upper()
    if not s:
        return None
    if s.startswith(('US.', 'HK.')):
        return s
    if s.endswith('.HK'):
        raw = s[:-3]
        if raw.isdigit():
            return 'HK.' + raw.zfill(5)
        return None
    if s.endswith('.US'):
        s = s[:-3]
    if '.' in s:
        return None
    return 'US.' + s


def _futu_session_quote(row, phase, label, price_key, change_key, rate_key):
    price = _safe_float(row.get(price_key))
    if price is None or price <= 0:
        return None
    return {
        'label': label,
        'phase': phase,
        'price': price,
        'changePoints': _safe_float(row.get(change_key)),
        'changePct': _safe_float(row.get(rate_key)),
    }


def _futu_et_time():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo('America/New_York')).time()
    except Exception:
        # Fallback for older Windows timezone databases; daylight-saving accuracy is best-effort.
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).time()


def _pick_futu_extended(row):
    et_now = _futu_et_time()
    pre_start = datetime.time(4, 0)
    regular_start = datetime.time(9, 30)
    regular_end = datetime.time(16, 0)
    after_end = datetime.time(20, 0)

    # Pick the active US extended session. During regular trading hours, do not
    # show stale pre/after/overnight as the primary extended quote.
    if pre_start <= et_now < regular_start:
        return _futu_session_quote(row, 'pre', '盤前', 'pre_price', 'pre_change_val', 'pre_change_rate')
    if regular_end <= et_now < after_end:
        return _futu_session_quote(row, 'after', '盤後', 'after_price', 'after_change_val', 'after_change_rate')
    if et_now >= after_end or et_now < pre_start:
        return _pick_futu_24h(row)
    return None


def _pick_futu_24h(row):
    return _futu_session_quote(
        row,
        'overnight',
        '24H',
        'overnight_price',
        'overnight_change_val',
        'overnight_change_rate',
    )

def _futopt_archive_scheduler():
    """
    snapshot-all（日盤+夜盤 SDK bars）：
      • 交易時段內每 30 分鐘跑一次（邊交易邊累積，避免漏掉夜盤）
      • 覆蓋範圍：週一~五 08:30 ~ 次日 05:30（夜盤跨日）
    TAIFEX 日 K 回補：
      • 每日 14:00+ 跑一次（前一交易日的 CSV 已經公開）
    """
    global _archive_last_snapshot, _archive_last_backfill
    time.sleep(30)

    def _in_trading_window(now: datetime.datetime) -> bool:
        """判斷是否在交易時段（或剛結束）：
        日盤 週一~五 08:30-14:00；夜盤 週一~五 14:55-次日 05:30。
        """
        wd = now.weekday()   # Mon=0..Sun=6
        hm = now.hour * 60 + now.minute
        # 週一 00:00~05:30（夜盤尾巴）不算，因為前週五晚上不交易
        if wd == 5 or wd == 6:
            # 週六、日：整日無交易（週六 00:00~05:30 是週五夜盤，但週五晚上不開盤）
            return False
        # 週一 00:00~05:30：前一天（日）無夜盤 → 不交易
        if wd == 0 and hm < 5 * 60 + 30:
            return False
        # 平日 08:30~次日 05:30
        if hm >= 8 * 60 + 30:
            return True
        if hm <= 5 * 60 + 30:
            return True
        return False

    while True:
        try:
            now = datetime.datetime.now()

            # ── snapshot-all：30 分鐘間隔 ──
            if _in_trading_window(now) and (time.time() - _archive_last_snapshot) >= 1800:
                _log_archive(f'snapshot-all（{now.strftime("%H:%M")}）')
                try:
                    r = subprocess.run(
                        [PY, ARCHIVE_SCRIPT, '--mode', 'snapshot-all'],
                        env=_ENV, capture_output=True, text=True,
                        encoding='utf-8', timeout=180,
                        creationflags=NWIN_FLAG,
                    )
                    _archive_last_snapshot = time.time()
                    if r.returncode != 0:
                        _log_archive(f'snapshot 失敗 (rc={r.returncode}): {r.stderr[:300]}')
                except Exception as e:
                    _log_archive(f'snapshot 例外: {e}')

            # ── TAIFEX 日 K 回補：每日 14:00 一次，回補昨日 ──
            today_str = now.date().isoformat()
            if (now.hour >= 14 and now.weekday() < 5
                    and _archive_last_backfill != today_str):
                yday = (now.date() - datetime.timedelta(days=1)).isoformat()
                try:
                    subprocess.run(
                        [PY, ARCHIVE_SCRIPT, '--mode', 'backfill-daily-all',
                         '--start', yday, '--end', yday, '--force'],
                        env=_ENV, capture_output=True, text=True,
                        encoding='utf-8', timeout=180,
                        creationflags=NWIN_FLAG,
                    )
                    _archive_last_backfill = today_str
                    _log_archive(f'TAIFEX 日 K 回補 {yday} 完成')
                except Exception as e:
                    _log_archive(f'TAIFEX 回補錯誤: {e}')
        except Exception as e:
            print(f'[archive scheduler] err: {e}', flush=True)
        time.sleep(300)   # 5 分鐘檢查一次


def _log_archive(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f'[archive {ts}] {msg}', flush=True)


def _futures_snapshot_scheduler():
    """
    交易時段內每 _fut_snap_interval 秒（預設 15 min）快照期貨 margin 欄位，
    寫入 data/futures_history.json；若設定 autopush 則 git commit + push。
    非交易時段（週末、台指封關）跳過，避免無意義的 SDK 呼叫 & commit 噪音。
    snapshot_futures.py 內部會比對 'git diff --cached'，無變動時自動略過 commit。

    交易時段（_in_futures_trading_window）：
      • 日盤：週一~五 08:45 ~ 14:10  (延後到 14:10 涵蓋結算後 5 min)
      • 夜盤：週一~五 15:00 ~ 次日 05:05
    """
    global _fut_snap_last_run
    time.sleep(60)   # 等 server 啟動完成
    interval_min = _fut_snap_interval // 60
    print(f'[fut-snap] 排程已啟動（交易時段每 {interval_min} 分鐘；'
          f'autopush={_fut_snap_autopush}）', flush=True)
    while True:
        try:
            now = datetime.datetime.now()
            if _in_futures_trading_window(now) and (time.time() - _fut_snap_last_run) >= _fut_snap_interval:
                _log_fut_snap(f'觸發快照 {now.strftime("%H:%M")} (autopush={_fut_snap_autopush})')
                args = [PY, FUT_SNAPSHOT_SCRIPT]
                if _fut_snap_autopush:
                    args.append('--commit')
                try:
                    r = subprocess.run(
                        args, env=_ENV, capture_output=True, text=True,
                        encoding='utf-8', errors='replace',
                        timeout=180, creationflags=NWIN_FLAG,
                    )
                    if r.returncode == 0:
                        _fut_snap_last_run = time.time()
                        tail = (r.stdout or '').strip().splitlines()[-3:]
                        for line in tail:
                            _log_fut_snap(line)
                    else:
                        _log_fut_snap(f'rc={r.returncode}；stderr={(r.stderr or "")[-300:]}')
                except Exception as e:
                    _log_fut_snap(f'subprocess 例外：{e}')
        except Exception as e:
            print(f'[fut-snap] scheduler err: {e}', flush=True)
        time.sleep(60)   # 1 分鐘檢查一次（實際觸發間隔由 _fut_snap_interval 控制）


def _in_futures_trading_window(now: datetime.datetime) -> bool:
    """台指期交易時段：平日 08:45–14:10 (日盤+結算 buffer) 或 15:00–次日 05:05 (夜盤)。"""
    wd = now.weekday()   # Mon=0..Sun=6
    hm = now.hour * 60 + now.minute
    # 週六 05:05 後 ~ 週日整日：無夜盤
    if wd == 5 and hm > 5 * 60 + 5:
        return False
    if wd == 6:
        return False
    # 週一 00:00~05:05：前一天（日）無夜盤
    if wd == 0 and hm < 5 * 60 + 5:
        return False
    # 夜盤尾（週二~週六 00:00~05:05）
    if hm <= 5 * 60 + 5:
        return True
    # 日盤 08:45~14:10
    if 8 * 60 + 45 <= hm <= 14 * 60 + 10:
        return True
    # 夜盤 15:00~23:59
    if hm >= 15 * 60:
        return True
    return False


def _log_fut_snap(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f'[fut-snap {ts}] {msg}', flush=True)


def _log_crypto(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f'[crypto {ts}] {msg}', flush=True)


def _crypto_balance_scheduler():
    """
    每 CRYPTO_REFRESH_SEC 秒呼叫 fetch_crypto_balance.py，更新 data/crypto_balance.json。
    腳本內部處理：Binance 現貨 + Binance 合約 + Pionex 總淨值（USD）。
    """
    global _crypto_last_fetch
    time.sleep(20)   # 等 server 啟動完成
    print(f'[crypto] 排程已啟動（每 {CRYPTO_REFRESH_SEC}s 更新 crypto_balance.json, '
          f'autopush={_crypto_autopush}）', flush=True)
    while True:
        try:
            now = time.time()
            if (now - _crypto_last_fetch) >= CRYPTO_REFRESH_SEC:
                args = [PY_VPN, CRYPTO_FETCH_SCRIPT]
                if _crypto_autopush:
                    args.append('--commit')
                try:
                    r = subprocess.run(
                        args, env=_ENV, capture_output=True, text=True,
                        encoding='utf-8', errors='replace',
                        timeout=90, creationflags=NWIN_FLAG,
                    )
                    _crypto_last_fetch = now
                    if r.returncode == 0:
                        # fetch_crypto_balance.py 最後一行應該是 JSON summary
                        tail = (r.stdout or '').strip().splitlines()
                        if tail:
                            try:
                                d = json.loads(tail[-1])
                                tot = d.get('total_usd')
                                _log_crypto(f'更新成功 total_usd={tot}')
                            except Exception:
                                _log_crypto('更新成功（無法解析 stdout）')
                    else:
                        _log_crypto(f'rc={r.returncode}; stderr={(r.stderr or "")[-250:]}')
                except Exception as e:
                    _log_crypto(f'subprocess 例外：{e}')
        except Exception as e:
            print(f'[crypto] scheduler err: {e}', flush=True)
        time.sleep(60)   # 每 1 分鐘檢查一次


def _find_orphan_daemons():
    """掃描系統中所有 fmx_quote_daemon.py 的 PID（不含當前 server.py 自己）。"""
    pids = []
    try:
        out = subprocess.check_output(
            ['wmic', 'process', 'where',
             "name='python.exe' or name='python_stock.exe'",
             'get', 'ProcessId,CommandLine'],
            text=True, errors='replace', timeout=5,
            creationflags=NWIN_FLAG,
        )
        for line in out.splitlines():
            if 'fmx_quote_daemon' in line:
                parts = line.strip().split()
                if parts:
                    try:
                        pids.append(int(parts[-1]))
                    except ValueError:
                        pass
    except Exception as e:
        print(f'[daemon] 掃描殭屍失敗: {e}', flush=True)
    return pids


def _kill_orphan_daemons():
    """殺掉所有現存的 fmx_quote_daemon.py 進程（server 啟動時用）。"""
    pids = _find_orphan_daemons()
    if not pids:
        return 0
    killed = 0
    for pid in pids:
        try:
            subprocess.run(
                ['taskkill', '/F', '/PID', str(pid)],
                capture_output=True, text=True, timeout=5,
                creationflags=NWIN_FLAG,
            )
            print(f'[daemon] 清除殭屍 PID {pid}', flush=True)
            killed += 1
        except Exception as e:
            print(f'[daemon] 殺 PID {pid} 失敗: {e}', flush=True)
    return killed


def _daemon_watchdog():
    """背景執行緒：確保 fmx_quote_daemon.py 持續運行"""
    global _daemon_proc
    time.sleep(3)   # 等 server 啟動完成
    # ── 啟動時先清理任何殘留的舊 daemon（避免 server.py 重啟累積殭屍）──
    n = _kill_orphan_daemons()
    if n > 0:
        print(f'[daemon] 清除 {n} 個舊 daemon，準備啟動新 daemon…', flush=True)
        time.sleep(2)   # 給作業系統時間釋放檔案鎖
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
                    creationflags=NWIN_FLAG,
                )
                with _daemon_proc_lock:
                    _daemon_proc = new_proc
                print(f'[daemon] PID {new_proc.pid} 啟動成功', flush=True)
        except Exception as e:
            print(f'[daemon] watchdog 錯誤: {e}', flush=True)
        time.sleep(15)   # 每 15 秒檢查一次


def _run_subprocess(script, args=(), timeout=45, cwd=None):
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
            cwd=cwd,
            creationflags=NWIN_FLAG,
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

_taifex_quote_cache = {}
_taifex_quote_lock  = threading.Lock()
TAIFEX_QUOTE_TTL    = 3

# ── stock/positions cache ─────────────────────────────────
_stock_pos_cache = None
_stock_pos_ts    = 0.0
_stock_pos_lock  = threading.Lock()
STOCK_POS_TTL    = 60  # seconds

# ── stock/kline cache  (keyed by "symbol:interval:range") ──
_stock_kline_cache = {}   # key → (ts, result)
_stock_kline_lock  = threading.Lock()
STOCK_KLINE_TTL    = 120  # seconds (2 min for intraday, enough for UI)

# ── futopt/kline cache（期貨 K 線，MXFR1/TXFR1/MTXR1） ──
_futopt_kline_cache = {}
_futopt_kline_lock  = threading.Lock()
FUTOPT_KLINE_TTL_INTRADAY = 60   # 分鐘 K：60 秒（即時）
FUTOPT_KLINE_TTL_DAILY    = 600  # 日 K+：10 分鐘

# ── sub (複委託) positions cache ──────────────────────────
_sub_pos_cache = None
_sub_pos_ts    = 0.0
_sub_pos_src_mtime = None
_sub_pos_lock  = threading.Lock()
SUB_POS_TTL    = 60   # seconds

# 複委託 quote cache (symbol|market → result)，yfinance 後援，TTL 較長
_sub_quote_cache = {}
_sub_quote_lock  = threading.Lock()
SUB_QUOTE_TTL    = 20

# 複委託 balance 快取（跟 positions 同一份 JSON，30s 內不重複 spawn）
_sub_bal_cache   = None
_sub_bal_ts      = 0.0
_sub_bal_src_mtime = None
_sub_bal_lock    = threading.Lock()
SUB_BAL_TTL      = 30

# 今日委託快取（讀本機 orders.jsonl，10s 就夠 — 下單後才需要新鮮度）
_sub_ord_cache   = None
_sub_ord_ts      = 0.0
_sub_ord_lock    = threading.Lock()
SUB_ORD_TTL      = 10

def _safe_mtime(path: str):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


_PY32 = 'C:/py32e01/python.exe'

def _run_e01(script, args=(), timeout=45):
    """Fork a script inside fubon_e01_bot/ using 32-bit Python (required for COM DLL).

    Uses a `-c` bootstrap that explicitly sys.path.insert(0, E01BOT) before
    runpy.run_path(). This works around a CPython bug on Windows where sys.path[0]
    is silently dropped when the script path contains characters outside the
    system ANSI code page (cp950 on zh-TW systems) — e.g. the "股市相關" folder.
    Passing the script path via -c (CreateProcessW Unicode cmdline) avoids the
    Py_GetPath() ANSI decode path entirely.
    """
    py = _PY32 if os.path.exists(_PY32) else PY
    script_path = os.path.join(E01BOT, script)
    # runpy.run_path preserves __name__=='__main__' semantics so argparse etc. still work.
    extra_args = list(args)
    boot = (
        "import sys, runpy;"
        f"sys.path.insert(0, r'{E01BOT}');"
        f"sys.argv = [r'{script_path}'] + {extra_args!r};"
        "runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    cmd = [py, '-c', boot]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding='utf-8', errors='replace',
            timeout=timeout, env=_ENV, cwd=E01BOT,
            creationflags=NWIN_FLAG,
        )
        out = (proc.stdout or '').strip()
        if not out:
            tail = (proc.stderr or '').strip()[-500:]
            return {'status': 'fail', 'error': f'no stdout: {tail}'}
        for line in reversed(out.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return {'status': 'fail', 'error': 'no valid JSON', 'raw': out[:300]}
    except subprocess.TimeoutExpired:
        return {'status': 'fail', 'error': f'timeout ({timeout}s)'}
    except Exception as e:
        return {'status': 'fail', 'error': str(e)}

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


# ── 最近日誌 (從 fmx_live.log 讀取末尾 N 筆有意義的記錄) ───────
import re as _re_log
_LIVE_LOG_PATH  = os.path.join(BOT, 'logs', 'fmx_live.log')
_LOG_LINE_RE    = _re_log.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+\[(\w+)\]\s+(.+)$'
)
_LOG_TS_FMT = '%Y-%m-%d %H:%M:%S'
_RECENT_LOG_KEEP_DAYS = 3
_RECENT_LOG_MIN_COUNT = 20
_RECENT_LOG_MAX_COUNT = 500
_RECENT_LOG_READ_BYTES = 2 * 1024 * 1024
# 關注關鍵字：只留「進出場 / 槓桿變化 / 部位更新」相關的訊息
_LOG_KEEP_KEYWORDS = (
    'tick ▶',              # 每 5 分鐘 tick 摘要（含 口數 + 槓桿率）
    '買進', '賣出', '下單', '委託', '成交', '已成交',
    '加倉', '減倉', '加槓桿', '降槓桿', '平倉', '清倉', '轉倉',
)
# 黑名單：策略啟動/設定訊息，含關鍵字但不是真正的進出場事件
_LOG_SKIP_SUBSTR = (
    '循環啟動',       # "5 分鐘 Intraday 全策略循環啟動"
    '目標槓桿:',      # "目標槓桿:5.0x  DCA:-5.0%..."
    '下單模式',       # "【⚡ 實際下單模式 ⚡】"
    'sdk.futopt 方法',
    '持倉全欄位',
    '持倉到期日',
    'query_hybrid_position',
    '倉位狀態已儲存',
)
# 精簡 tick 摘要：從 "tick ▶ 9口  現價:38028  均攤:36668  lev:4.96x  eq:690,651 ..."
# 只擷取 口數 + lev 做為精簡訊息
_TICK_RE = _re_log.compile(
    r'tick\s*▶\s*(\d+)\s*口.*?lev[:：]\s*([\d.]+)x', _re_log.IGNORECASE
)

def _condense_msg(msg: str) -> str:
    """將冗長 tick 訊息壓縮成只含 口數 / 槓桿率。"""
    m = _TICK_RE.search(msg)
    if m:
        return f'▶ {m.group(1)} 口 · 槓桿 {m.group(2)}x'
    return msg

def _read_recent_logs(n: int = 20) -> list:
    cutoff = datetime.datetime.now() - datetime.timedelta(days=_RECENT_LOG_KEEP_DAYS)
    """
    從 fmx_live.log 讀取最近 N 筆「進出場 / 槓桿變化」的日誌。
    回傳: [{time, level, msg}, ...] 舊→新（前端會再 reverse 顯示新→舊）
    """
    path = _LIVE_LOG_PATH
    if not os.path.exists(path):
        return []
    try:
        # 從檔尾往前讀 256KB（進場事件稀少，tick 每 5 分鐘一筆，需要較大 buffer）
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > _RECENT_LOG_READ_BYTES:
                f.seek(size - _RECENT_LOG_READ_BYTES)
                f.readline()
            raw = f.read().decode('utf-8', errors='replace')
    except Exception as e:
        return [{'time': '', 'level': 'warn', 'msg': f'日誌讀取失敗: {e}'}]

    out = []
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        m = _LOG_LINE_RE.match(line)
        if not m:
            continue
        ts, level, msg = m.group(1), m.group(2), m.group(3)
        try:
            log_dt = datetime.datetime.strptime(ts, _LOG_TS_FMT)
        except ValueError:
            log_dt = None
        # 先過黑名單
        if any(sk in msg for sk in _LOG_SKIP_SUBSTR):
            continue
        # 再篩關鍵字
        if not any(kw in msg for kw in _LOG_KEEP_KEYWORDS):
            continue
        out.append({
            'time':  ts,
            'level': level.lower(),
            'msg':   _condense_msg(msg),
        })
        enough_count = len(out) >= max(n, _RECENT_LOG_MIN_COUNT)
        older_than_cutoff = (log_dt is not None and log_dt < cutoff)
        if len(out) >= _RECENT_LOG_MAX_COUNT:
            break
        if enough_count and older_than_cutoff:
            break
    return list(reversed(out))


def _read_recent_trades(n: int = 10) -> list:
    """讀 data/fut_trades.json 取最近 n 筆實際成交（含 price = 成交點數）。
    回傳依時間新→舊：[{date, time, buy_sell, symbol, lots, price, order_type, order_no}, ...]
    若檔案不存在或損壞，回傳空 list。"""
    if not os.path.exists(FUT_TRADES_FILE):
        return []
    try:
        with open(FUT_TRADES_FILE, 'r', encoding='utf-8') as f:
            doc = json.load(f)
    except Exception:
        return []
    trades = doc.get('trades') if isinstance(doc, dict) else None
    if not isinstance(trades, list):
        return []
    # fut_trades.json 是依時間舊→新；取尾端 n 筆，再 reverse 成新→舊
    last_n = trades[-n:] if len(trades) > n else trades[:]
    out = []
    for t in reversed(last_n):
        try:
            out.append({
                'date':       t.get('date', ''),                # '2026/05/04'
                'time':       t.get('time', ''),                # 部分來源不一定有；可空
                'buy_sell':   t.get('buy_sell', ''),            # 'Buy' / 'Sell'
                'symbol':     t.get('symbol', ''),              # 'FITM' / 'TMFK6' 等
                'lots':       float(t.get('orig_lots', 0) or 0),
                'price':      float(t.get('price', 0) or 0),    # 成交點數
                'order_type': t.get('order_type', ''),          # 'New' / 'Close'
                'order_no':   t.get('order_no', ''),
                'expiry':     t.get('expiry', ''),
            })
        except Exception:
            continue
    return out


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
                    creationflags=NWIN_FLAG,
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
            text=True, errors='replace', timeout=5,
            creationflags=NWIN_FLAG,
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
        elif p == '/taifex/futures_quotes':
            self._taifex_futures_quotes(parsed)
        elif p == '/futu/quotes':
            self._futu_quotes(parsed)
        elif p == '/fmx/runtime':
            self._fmx_runtime()
        elif p == '/fmx/config':
            self._fmx_config()
        elif p == '/stock/quote':
            self._stock_quote(parsed)
        elif p == '/stock/kline':
            self._stock_kline(parsed)
        elif p in ('/stock/txns', '/stock/txns/'):
            self._stock_txns_get()
        elif p == '/futopt/kline':
            self._futopt_kline(parsed)
        elif p in ('/stock/positions', '/stock/positions/'):
            self._stock_positions(parsed)
        elif p == '/sub/quote':
            self._sub_quote(parsed)
        elif p in ('/sub/positions', '/sub/positions/'):
            self._sub_positions(parsed)
        elif p == '/sub/balance':
            self._sub_balance(parsed)
        elif p in ('/sub/orders', '/sub/orders/'):
            self._sub_orders(parsed)
        elif p == '/crypto/balance':
            self._crypto_balance(parsed)
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path

        if p == '/fmx/control':
            self._fmx_control()
        elif p == '/fmx/sync':
            self._fmx_sync()
        elif p == '/fmx/snapshot_now':
            self._fmx_snapshot_now()
        elif p == '/crypto/refresh':
            self._crypto_refresh()
        elif p == '/crypto/pionex_twd':
            self._crypto_pionex_twd()
        elif p == '/stock/order':
            self._stock_order()
        elif p == '/stock/cancel':
            self._stock_cancel()
        elif p in ('/stock/txns', '/stock/txns/'):
            self._stock_txns_post()
        elif p == '/sub/order':
            self._sub_order()
        elif p == '/sub/gui_order':
            self._sub_gui_order()
        elif p == '/sub/cancel':
            self._sub_cancel()
        elif p == '/sub/sync_profit_query':
            self._sub_sync_profit_query()
        elif p == '/asset/snapshot/save':
            self._asset_snapshot_save()
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

    def _futu_quotes(self, parsed):
        params = self._qs(parsed)
        raw_symbols = params.get('symbols', [''])[0]
        symbols = []
        for part in raw_symbols.replace(';', ',').split(','):
            sym = part.strip().upper()
            if sym and sym not in symbols:
                symbols.append(sym)
        if not symbols:
            return self._send_json({'ok': False, 'error': 'missing symbols', 'quotes': {}})

        code_to_symbol = {}
        codes = []
        for sym in symbols[:400]:
            code = _futu_code_from_symbol(sym)
            if code and code not in code_to_symbol:
                code_to_symbol[code] = sym
                codes.append(code)
        if not codes:
            return self._send_json({'ok': False, 'error': 'no supported Futu symbols', 'quotes': {}})

        cache_key = ','.join(codes)
        now = time.time()
        with _futu_quote_lock:
            if (_futu_quote_cache.get('key') == cache_key
                    and _futu_quote_cache.get('data')
                    and now - float(_futu_quote_cache.get('ts') or 0) < 8):
                return self._send_json(_futu_quote_cache['data'])

        api, api_name = _import_futu_api()
        if api is None:
            return self._send_json({
                'ok': False,
                'error': 'moomoo-api/futu-api is not installed',
                'install': 'pip install moomoo-api',
                'quotes': {},
            })
        if not _is_futu_opend_reachable():
            return self._send_json({
                'ok': False,
                'error': f'Futu/Moomoo OpenD is not reachable at {FUTU_HOST}:{FUTU_PORT}',
                'quotes': {},
            })

        quote_ctx = None
        try:
            quote_ctx = api.OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
            ret, data = quote_ctx.get_market_snapshot(codes)
            if ret != getattr(api, 'RET_OK', 0):
                return self._send_json({'ok': False, 'error': str(data), 'quotes': {}})

            records = data.to_dict('records') if hasattr(data, 'to_dict') else []
            quotes = {}
            for row in records:
                code = str(row.get('code') or row.get('stock_code') or '').upper()
                sym = code_to_symbol.get(code)
                if not sym:
                    continue
                last_price = _safe_float(row.get('last_price'))
                prev_close = _safe_float(row.get('prev_close_price'))
                regular_change = None
                regular_change_pct = None
                if last_price is not None and prev_close is not None and prev_close > 0:
                    regular_change = last_price - prev_close
                    regular_change_pct = regular_change / prev_close * 100

                quotes[sym] = {
                    'symbol': sym,
                    'code': code,
                    'name': row.get('name'),
                    'source': api_name,
                    'last_price': last_price,
                    'prev_close_price': prev_close,
                    'prev_prev_close_price': None,
                    'open_price': _safe_float(row.get('open_price')),
                    'high_price': _safe_float(row.get('high_price')),
                    'low_price': _safe_float(row.get('low_price')),
                    'volume': _safe_float(row.get('volume')),
                    'regular_change_val': regular_change,
                    'regular_change_rate': regular_change_pct,
                    'extended': _pick_futu_extended(row),
                    'raw_time': row.get('update_time') or row.get('last_trade_time'),
                }

            for code in codes:
                sym = code_to_symbol.get(code)
                if not sym or sym not in quotes:
                    continue
                prev_close = quotes[sym].get('prev_close_price')
                if prev_close is None:
                    continue
                try:
                    end = datetime.date.today().strftime('%Y-%m-%d')
                    start = (datetime.date.today() - datetime.timedelta(days=14)).strftime('%Y-%m-%d')
                    ret_k, kl = quote_ctx.request_history_kline(
                        code,
                        start=start,
                        end=end,
                        ktype=getattr(api.KLType, 'K_DAY'),
                        autype=getattr(api.AuType, 'QFQ'),
                        max_count=10,
                    )
                    if ret_k != getattr(api, 'RET_OK', 0) or not hasattr(kl, 'to_dict'):
                        continue
                    bars = kl.to_dict('records')
                    closes = [_safe_float(r.get('close')) for r in bars]
                    closes = [c for c in closes if c is not None and c > 0]
                    if len(closes) < 2:
                        continue
                    idx = None
                    for i in range(len(closes) - 1, -1, -1):
                        if abs(closes[i] - prev_close) < max(0.01, prev_close * 0.0001):
                            idx = i
                            break
                    if idx is not None and idx > 0:
                        quotes[sym]['prev_prev_close_price'] = closes[idx - 1]
                    elif len(closes) >= 2:
                        quotes[sym]['prev_prev_close_price'] = closes[-2]
                except Exception:
                    continue

            payload = {
                'ok': True,
                'source': api_name,
                'host': FUTU_HOST,
                'port': FUTU_PORT,
                'ts': int(time.time() * 1000),
                'quotes': quotes,
            }
            with _futu_quote_lock:
                _futu_quote_cache.update({'key': cache_key, 'ts': time.time(), 'data': payload})
            self._send_json(payload)
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e), 'quotes': {}})
        finally:
            try:
                if quote_ctx is not None:
                    quote_ctx.close()
            except Exception:
                pass

    # ── /proxy ────────────────────────────────────────────
    def _proxy(self, parsed):
        params = self._qs(parsed)
        target = params.get('url', [''])[0]
        if not target:
            self.send_error(400, 'Missing url parameter')
            return
        try:
            hdrs = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
                              ' AppleWebKit/537.36 (KHTML, like Gecko)'
                              ' Chrome/124.0 Safari/537.36',
                'Accept': 'application/json, text/html, */*',
                'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
            }
            # TWSE / TPEx MIS API 需要正確的 Referer，否則回傳 403 或空 msgArray
            if 'twse.com.tw' in target or 'mis.twse' in target:
                hdrs['Referer'] = 'https://www.twse.com.tw/'
                hdrs['Origin']  = 'https://www.twse.com.tw'
            elif 'tpex.org.tw' in target or 'tpcj.org.tw' in target:
                hdrs['Referer'] = 'https://www.tpex.org.tw/'
            req = urllib.request.Request(target, headers=hdrs)
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self._cors_headers()
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(502, str(e))

    # ── /taifex/futures_quotes ─────────────────────────────
    def _taifex_futures_quotes(self, parsed):
        """Read-only TAIFEX MIS quote list bridge for dashboard display."""
        global _taifex_quote_cache
        params = self._qs(parsed)
        market_type = params.get('market_type', ['0'])[0]  # 0=day, 1=after-hours
        kind_id = params.get('kind_id', ['1'])[0]          # 1=equity index futures
        cache_key = f'{market_type}:{kind_id}'
        now = time.time()

        with _taifex_quote_lock:
            entry = _taifex_quote_cache.get(cache_key)
            if entry and (now - entry[0]) < TAIFEX_QUOTE_TTL:
                self._send_json(entry[1])
                return

        url = 'https://mis.taifex.com.tw/futures/api/getQuoteList'
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/124.0 Safari/537.36',
            'Referer': 'https://mis.taifex.com.tw/futures/RegularSession/EquityIndices/FuturesDomestic/',
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json;charset=UTF-8',
        }

        rows = []
        quote_count = None
        try:
            for page_no in range(1, 13):
                payload = json.dumps({
                    'MarketType': str(market_type),
                    'SymbolType': 'F',
                    'KindID': str(kind_id),
                    'CID': '',
                    'ExpireMonth': '',
                    'PageNo': str(page_no),
                    'PageSize': '50',
                }).encode('utf-8')
                req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=8) as resp:
                    raw = resp.read().decode('utf-8', errors='replace')
                data = json.loads(raw)
                if data.get('RtCode') not in (None, '0', 0):
                    continue
                rt = data.get('RtData') or {}
                page_rows = rt.get('QuoteList') or []
                if quote_count is None:
                    try:
                        quote_count = int(rt.get('QuoteCount') or 0)
                    except Exception:
                        quote_count = 0
                if not page_rows:
                    break
                rows.extend(page_rows)
                if quote_count and len(rows) >= quote_count:
                    break

            result = {
                'status': 'ok',
                'source': 'taifex_mis_quote_list',
                'ts': datetime.datetime.now().isoformat(timespec='seconds'),
                'market_type': str(market_type),
                'kind_id': str(kind_id),
                'RtData': {
                    'QuoteCount': str(quote_count or len(rows)),
                    'QuoteList': rows,
                },
            }
            with _taifex_quote_lock:
                _taifex_quote_cache[cache_key] = (time.time(), result)
            self._send_json(result)
        except Exception as e:
            self._send_json({'status': 'error', 'error': str(e), 'RtData': {'QuoteList': []}}, 502)

    # ── /yfundamentals ────────────────────────────────────
    def _fundamentals(self, parsed):
        params = self._qs(parsed)
        symbol = params.get('symbol', [''])[0].upper()
        if not symbol:
            self.send_error(400, 'Missing symbol')
            return
        self._send_json(get_fundamentals(symbol))

    # ── /fmx/state ────────────────────────────────────────
    def _fmx_config(self):
        """讀取 fubon_bot/config.yaml 回傳當前 live bot 的策略參數，供 simulator 套用"""
        try:
            import yaml
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
            # 過濾掉敏感欄位（api_key、cert_pass、personal_id 等）
            sensitive = {'api_key', 'cert_pass', 'cert_path', 'personal_id',
                         'account', 'futures_account', 'branch_no', 'futures_branch_no'}
            safe = {k: v for k, v in cfg.items() if k not in sensitive}
            self._send_json(safe)
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def _fmx_state(self):
        st = _read_state()
        # 附加最近 20 筆策略日誌，供前端右側「最近日誌」面板使用
        try:
            if isinstance(st, dict):
                st['recent_logs'] = _read_recent_logs(20)
        except Exception:
            pass
        # 附加最近 10 筆實際成交（含成交點數），供前端「最近交易」面板使用
        try:
            if isinstance(st, dict):
                st['recent_trades'] = _read_recent_trades(10)
        except Exception:
            pass
        self._send_json(st)

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
            # 防止與 start_bot.bat watchdog 衝突：如果偵測到外部 bot 在跑，拒絕 spawn
            # 過去經驗：兩個 bot 用同一憑證 → 富邦端拒絕後登 session →「憑證匯入錯誤」全失敗
            external = _find_strategy_procs()
            if external:
                self._send_json({
                    'status': 'error',
                    'message': f'已有 strategy-fmx-live.py 在跑 (PID {external})。'
                               f'若要重啟請先在該視窗 Ctrl+C 或重啟 start_bot.bat。'
                               f'禁止從 dashboard 重複啟動以免雙憑證衝突。'
                }, 409)
                return
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
                    creationflags=NWIN_FLAG,
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
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                                       creationflags=NWIN_FLAG)
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
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=NWIN_FLAG)
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
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=NWIN_FLAG)
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
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=NWIN_FLAG)
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
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=NWIN_FLAG)
            self._send_json({'status': 'ok', 'message': '轉倉指令已送出（背景進行中）'})

        elif action == 'reset':
            args = [PY, STRATEGY_SCRIPT, '--reset']
            if dry_run:
                args.append('--dry-run')
            subprocess.Popen(args, cwd=BOT, env=_ENV,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=NWIN_FLAG)
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

    # ── POST /fmx/snapshot_now ───────────────────────────
    # 手動觸發期貨權益快照（寫入 data/futures_history.json）
    # body: { "commit": true }  → 快照後 git commit + push
    def _fmx_snapshot_now(self):
        global _fut_snap_last_date
        body = self._read_body()
        do_commit = bool(body.get('commit', False))
        if not os.path.exists(FUT_SNAPSHOT_SCRIPT):
            self._send_json({'status': 'fail', 'error': f'snapshot 腳本不存在：{FUT_SNAPSHOT_SCRIPT}'})
            return
        args = [PY, FUT_SNAPSHOT_SCRIPT]
        if do_commit:
            args.append('--commit')
        try:
            r = subprocess.run(
                args, env=_ENV, capture_output=True, text=True,
                encoding='utf-8', errors='replace',
                timeout=180, creationflags=NWIN_FLAG,
            )
            ok = (r.returncode == 0)
            if ok:
                _fut_snap_last_date = datetime.datetime.now().date().isoformat()
            self._send_json({
                'status':  'ok' if ok else 'fail',
                'rc':      r.returncode,
                'stdout':  (r.stdout or '')[-1500:],
                'stderr':  (r.stderr or '')[-500:],
                'commit':  do_commit,
            })
        except Exception as e:
            self._send_json({'status': 'fail', 'error': str(e)})

    # ── GET /crypto/balance ──────────────────────────────
    # 讀取 data/crypto_balance.json（由 scheduler 定期更新），附上檔案年齡資訊。
    def _crypto_balance(self, parsed):
        try:
            if not os.path.exists(CRYPTO_BALANCE_FILE):
                self._send_json({
                    'status':  'fail',
                    'error':   'crypto_balance.json 尚未產生，請等待下一次排程或 POST /crypto/refresh',
                    'total_usd': 0,
                    'exchanges': {},
                })
                return
            with open(CRYPTO_BALANCE_FILE, encoding='utf-8') as f:
                data = json.load(f)
            mtime = os.path.getmtime(CRYPTO_BALANCE_FILE)
            data['_file_mtime'] = datetime.datetime.fromtimestamp(mtime).isoformat(timespec='seconds')
            data['_age_seconds'] = int(time.time() - mtime)
            self._send_json(data)
        except Exception as e:
            self._send_json({'status': 'fail', 'error': str(e)})

    # ── POST /crypto/refresh ─────────────────────────────
    # 手動觸發 fetch_crypto_balance.py，body 可選 {"commit": true}
    def _crypto_refresh(self):
        global _crypto_last_fetch
        body = self._read_body()
        do_commit = bool(body.get('commit', False))
        if not os.path.exists(CRYPTO_FETCH_SCRIPT):
            self._send_json({'status': 'fail', 'error': f'腳本不存在：{CRYPTO_FETCH_SCRIPT}'})
            return
        args = [PY, CRYPTO_FETCH_SCRIPT]
        if do_commit:
            args.append('--commit')
        try:
            r = subprocess.run(
                args, env=_ENV, capture_output=True, text=True,
                encoding='utf-8', errors='replace',
                timeout=90, creationflags=NWIN_FLAG,
            )
            ok = (r.returncode == 0)
            if ok:
                _crypto_last_fetch = time.time()
            self._send_json({
                'status':  'ok' if ok else 'fail',
                'rc':      r.returncode,
                'stdout':  (r.stdout or '')[-1500:],
                'stderr':  (r.stderr or '')[-500:],
                'commit':  do_commit,
            })
        except Exception as e:
            self._send_json({'status': 'fail', 'error': str(e)})

    # ── POST /crypto/pionex_twd ──────────────────────────
    # body: { "value": 12345, "commit": true }
    # 更新 data/crypto_balance.json 的 manual_pionex_twd 欄位，讓手機版透過
    # 此檔案跨裝置同步（localStorage 是 per-device，手機上會吃不到）。
    def _crypto_pionex_twd(self):
        body = self._read_body()
        try:
            val = float(body.get('value') or 0)
        except (TypeError, ValueError):
            self._send_json({'status': 'fail', 'error': 'value 不是有效數字'})
            return
        if val < 0 or val > 1e12:
            self._send_json({'status': 'fail', 'error': 'value 超出合理範圍'})
            return
        do_commit = bool(body.get('commit', True))   # 預設 commit，方便 GitHub Pages 同步
        try:
            data = {}
            if os.path.exists(CRYPTO_BALANCE_FILE):
                with open(CRYPTO_BALANCE_FILE, encoding='utf-8') as f:
                    data = json.load(f) or {}
            data['manual_pionex_twd']        = val
            data['manual_pionex_twd_ts']     = datetime.datetime.now().isoformat(timespec='seconds')
            tmp = CRYPTO_BALANCE_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CRYPTO_BALANCE_FILE)
        except Exception as e:
            self._send_json({'status': 'fail', 'error': f'寫檔失敗：{e}'})
            return

        commit_log = ''
        if do_commit:
            try:
                subprocess.run(['git', '-C', DIR, 'add', 'data/crypto_balance.json'],
                               check=True, capture_output=True, text=True, creationflags=NWIN_FLAG)
                diff = subprocess.run(['git', '-C', DIR, 'diff', '--cached', '--quiet'],
                                      capture_output=True, creationflags=NWIN_FLAG)
                if diff.returncode != 0:
                    subprocess.run(['git', '-C', DIR, 'commit', '-m', f'chore(crypto): pionex_twd={val:.0f}'],
                                   check=True, capture_output=True, text=True, creationflags=NWIN_FLAG)
                    subprocess.run(['git', '-C', DIR, 'pull', '--rebase', 'origin', 'main'],
                                   capture_output=True, text=True, creationflags=NWIN_FLAG)
                    pr = subprocess.run(['git', '-C', DIR, 'push', 'origin', 'main'],
                                        capture_output=True, text=True, creationflags=NWIN_FLAG)
                    commit_log = (pr.stderr or pr.stdout or '')[-200:]
                else:
                    commit_log = '無變動，略過 commit'
            except subprocess.CalledProcessError as e:
                commit_log = f'git 失敗：{(e.stderr or str(e))[-200:]}'
            except Exception as e:
                commit_log = f'git 例外：{e}'

        self._send_json({
            'status':            'ok',
            'manual_pionex_twd': val,
            'commit':            do_commit,
            'git':               commit_log,
        })

    # ── GET /stock/quote ──────────────────────────────────
    def _stock_quote(self, parsed):
        params = self._qs(parsed)
        symbol = params.get('symbol', ['2330'])[0].strip()
        result = _run_subprocess('stock_quote.py', ['--symbol', symbol], timeout=30)
        self._send_json(result)

    # ── GET /stock/kline ──────────────────────────────────
    def _stock_kline(self, parsed):
        global _stock_kline_cache
        params   = self._qs(parsed)
        symbol   = params.get('symbol',   ['2330'])[0].strip()
        interval = params.get('interval', ['5'])[0].strip()
        range_   = params.get('range',    ['1d'])[0].strip()

        cache_key = f'{symbol}:{interval}:{range_}'
        now = time.time()

        with _stock_kline_lock:
            entry = _stock_kline_cache.get(cache_key)
            if entry and (now - entry[0]) < STOCK_KLINE_TTL:
                self._send_json(entry[1])
                return

        result = _run_subprocess(
            'stock_kline.py',
            ['--symbol', symbol, '--interval', interval, '--range', range_],
            timeout=45,
        )
        with _stock_kline_lock:
            _stock_kline_cache[cache_key] = (time.time(), result)

        self._send_json(result)

    # ── GET /futopt/kline ─────────────────────────────────
    # 期貨 K 線：intraday 走 Fubon SDK（真實 MXF/TXF 價），日 K+ 走 yfinance ^TWII
    def _futopt_kline(self, parsed):
        global _futopt_kline_cache
        params   = self._qs(parsed)
        symbol   = params.get('symbol',   ['MXFR1'])[0].strip()
        interval = params.get('interval', ['5'])[0].strip()
        range_   = params.get('range',    ['1d'])[0].strip()
        yf_fb    = params.get('yf',       ['^TWII'])[0].strip()

        is_intraday = interval in ('1','5','15','30','60')
        ttl = FUTOPT_KLINE_TTL_INTRADAY if is_intraday else FUTOPT_KLINE_TTL_DAILY
        cache_key = f'{symbol}:{interval}:{range_}:{yf_fb}'
        now = time.time()

        with _futopt_kline_lock:
            entry = _futopt_kline_cache.get(cache_key)
            if entry and (now - entry[0]) < ttl:
                self._send_json(entry[1])
                return

        result = _run_subprocess(
            'futopt_kline.py',
            ['--symbol', symbol, '--interval', interval,
             '--range', range_, '--yf-fallback', yf_fb],
            timeout=45,
        )
        with _futopt_kline_lock:
            _futopt_kline_cache[cache_key] = (time.time(), result)

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

    # ── GET /stock/txns ──────────────────────────────────
    def _stock_txns_get(self):
        try:
            if not os.path.exists(STOCK_TXNS_FILE):
                self._send_json({'status': 'ok', 'transactions': [], 'count': 0})
                return
            with open(STOCK_TXNS_FILE, encoding='utf-8') as f:
                data = json.load(f)
            txns = data if isinstance(data, list) else data.get('transactions', [])
            if not isinstance(txns, list):
                txns = []
            self._send_json({'status': 'ok', 'transactions': txns, 'count': len(txns)})
        except Exception as e:
            self._send_json({'status': 'fail', 'error': f'讀取交易紀錄失敗：{e}'}, status=500)

    def _normalize_stock_txn(self, raw):
        market = str(raw.get('bourse') or raw.get('market') or 'US').strip().upper()
        symbol = str(raw.get('sym') or raw.get('symbol') or '').strip().upper()
        typ = str(raw.get('type') or raw.get('side') or '').strip().lower()
        if typ in ('buy', 'b'):
            typ = 'buy'
        elif typ in ('sell', 's'):
            typ = 'sell'
        currency = str(raw.get('currency') or '').strip().upper()
        if not currency:
            currency = 'TWD' if market == 'TW' else 'HKD' if market == 'HK' else 'USD'
        date = str(raw.get('date') or datetime.datetime.now().strftime('%Y-%m-%d')).strip()[:10]

        try:
            qty = float(raw.get('qty'))
            price = float(raw.get('price'))
            fee = float(raw.get('fee') or 0)
        except (TypeError, ValueError):
            raise ValueError('qty / price / fee 必須是數字')
        if not symbol:
            raise ValueError('symbol required')
        if typ not in ('buy', 'sell'):
            raise ValueError('type must be buy or sell')
        if qty <= 0:
            raise ValueError('qty 必須大於 0')
        if price < 0 or fee < 0:
            raise ValueError('price / fee 不可為負數')

        item = {
            'date': date,
            'bourse': market,
            'sym': symbol,
            'currency': currency,
            'type': typ,
            'qty': qty,
            'price': price,
            'fee': fee,
            'client_id': str(raw.get('client_id') or '').strip(),
            'source': str(raw.get('source') or 'portfolio-tracker-v13').strip(),
            'recorded_at': str(raw.get('recorded_at') or datetime.datetime.now().isoformat(timespec='seconds')),
        }
        optional_number_keys = (
            'proceeds', 'sell_proceeds', 'buy_avg', 'avg_cost', 'cost_basis',
            'cost_basis_orig', 'cost_basis_twd', 'realized', 'realized_orig',
            'realized_twd', 'pnl_orig', 'pnl_twd', 'realized_pct',
            'fx_pnl_twd', 'fx_pnl', 'fx_rate',
        )
        for key in optional_number_keys:
            if key not in raw or raw.get(key) in (None, ''):
                continue
            try:
                item[key] = float(raw.get(key))
            except (TypeError, ValueError):
                pass
        if raw.get('report_name'):
            item['report_name'] = str(raw.get('report_name')).strip()
        return item

    # ── POST /stock/txns ─────────────────────────────────
    # body: {date,bourse/symbol,type,qty,price,fee,currency,client_id,dry_run}
    # 也支援編輯：body 加入 action='update' / 'delete' 並帶 client_id
    def _stock_txns_post(self):
        body = self._read_body()
        action = (body.get('action') or '').lower()

        # ── action=update：依 client_id 找到該筆並更新欄位 ──
        if action == 'update':
            cid = str(body.get('client_id') or '').strip()
            if not cid:
                self._send_json({'status': 'fail', 'error': 'client_id required for update'}, status=400)
                return
            try:
                normalized = self._normalize_stock_txn(body.get('transaction') or body)
            except Exception as e:
                self._send_json({'status': 'fail', 'error': str(e)}, status=400)
                return
            try:
                txns = []
                if os.path.exists(STOCK_TXNS_FILE):
                    with open(STOCK_TXNS_FILE, encoding='utf-8') as f:
                        data = json.load(f)
                    txns = data if isinstance(data, list) else data.get('transactions', [])
                    if not isinstance(txns, list):
                        txns = []
                found = False
                for i, t in enumerate(txns):
                    if isinstance(t, dict) and str(t.get('client_id') or '') == cid:
                        # 保留原 client_id 與 source、recorded_at；只覆寫使用者編輯欄位
                        merged = dict(t)
                        for k in ('date', 'bourse', 'sym', 'currency', 'type', 'qty', 'price', 'fee'):
                            if k in normalized:
                                merged[k] = normalized[k]
                        merged['client_id'] = cid
                        merged['updated_at'] = datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z'
                        txns[i] = merged
                        found = True
                        break
                if not found:
                    self._send_json({'status': 'fail', 'error': f'client_id {cid} 找不到'}, status=404)
                    return
                tmp = STOCK_TXNS_FILE + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(txns, f, ensure_ascii=False, indent=2)
                os.replace(tmp, STOCK_TXNS_FILE)
                self._send_json({'status': 'ok', 'action': 'update', 'transaction': txns[i], 'count': len(txns)})
            except Exception as e:
                self._send_json({'status': 'fail', 'error': f'更新交易紀錄失敗：{e}'}, status=500)
            return

        # ── action=delete：依 client_id 刪除 ──
        if action == 'delete':
            cid = str(body.get('client_id') or '').strip()
            if not cid:
                self._send_json({'status': 'fail', 'error': 'client_id required for delete'}, status=400)
                return
            try:
                txns = []
                if os.path.exists(STOCK_TXNS_FILE):
                    with open(STOCK_TXNS_FILE, encoding='utf-8') as f:
                        data = json.load(f)
                    txns = data if isinstance(data, list) else data.get('transactions', [])
                    if not isinstance(txns, list):
                        txns = []
                before = len(txns)
                txns = [t for t in txns if not (isinstance(t, dict) and str(t.get('client_id') or '') == cid)]
                if len(txns) == before:
                    self._send_json({'status': 'fail', 'error': f'client_id {cid} 找不到'}, status=404)
                    return
                tmp = STOCK_TXNS_FILE + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(txns, f, ensure_ascii=False, indent=2)
                os.replace(tmp, STOCK_TXNS_FILE)
                self._send_json({'status': 'ok', 'action': 'delete', 'client_id': cid, 'count': len(txns)})
            except Exception as e:
                self._send_json({'status': 'fail', 'error': f'刪除交易紀錄失敗：{e}'}, status=500)
            return

        # ── 預設：新增（append）──
        try:
            raw_items = body.get('transactions') if isinstance(body.get('transactions'), list) else [body]
            items = [self._normalize_stock_txn(x or {}) for x in raw_items]
        except Exception as e:
            self._send_json({'status': 'fail', 'error': str(e)}, status=400)
            return

        if body.get('dry_run'):
            self._send_json({'status': 'ok', 'dry_run': True, 'transactions': items, 'count': len(items)})
            return

        try:
            txns = []
            if os.path.exists(STOCK_TXNS_FILE):
                with open(STOCK_TXNS_FILE, encoding='utf-8') as f:
                    data = json.load(f)
                txns = data if isinstance(data, list) else data.get('transactions', [])
                if not isinstance(txns, list):
                    txns = []

            seen_client_ids = {str(t.get('client_id')) for t in txns if isinstance(t, dict) and t.get('client_id')}
            appended = []
            for item in items:
                cid = item.get('client_id')
                if cid and cid in seen_client_ids:
                    continue
                txns.append(item)
                appended.append(item)
                if cid:
                    seen_client_ids.add(cid)

            tmp = STOCK_TXNS_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(txns, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STOCK_TXNS_FILE)
            self._send_json({'status': 'ok', 'transactions': appended, 'appended': len(appended), 'count': len(txns)})
        except Exception as e:
            self._send_json({'status': 'fail', 'error': f'寫入交易紀錄失敗：{e}'}, status=500)

    # ══════════════════════════════════════════════════════════
    # 複委託 (Fubon e01) 路由
    # ══════════════════════════════════════════════════════════
    def _sub_quote(self, parsed):
        global _sub_quote_cache
        params = self._qs(parsed)
        symbol = params.get('symbol', [''])[0].strip().upper()
        market = params.get('market', ['US'])[0].strip().upper()
        real   = bool(params.get('real'))
        if not symbol:
            self._send_json({'status': 'fail', 'error': 'symbol required'}); return

        key = f'{symbol}|{market}|{"r" if real else "d"}'
        now = time.time()
        with _sub_quote_lock:
            entry = _sub_quote_cache.get(key)
            if entry and (now - entry[0]) < SUB_QUOTE_TTL:
                self._send_json(entry[1]); return

        args = ['--symbol', symbol, '--market', market]
        if real:
            args.append('--real')
        result = _run_e01('e01_quote.py', args, timeout=15)
        with _sub_quote_lock:
            _sub_quote_cache[key] = (time.time(), result)
        self._send_json(result)

    def _sub_positions(self, parsed):
        global _sub_pos_cache, _sub_pos_ts, _sub_pos_src_mtime
        params  = self._qs(parsed)
        refresh = bool(params.get('refresh'))
        real    = bool(params.get('real'))
        now     = time.time()
        src_mtime = _safe_mtime(SUB_HOLDINGS_FILE)
        with _sub_pos_lock:
            cache_valid = (
                not refresh and _sub_pos_cache
                and (now - _sub_pos_ts) < SUB_POS_TTL
                and _sub_pos_src_mtime == src_mtime
            )
            if cache_valid:
                self._send_json(_sub_pos_cache); return
            args = ['--real'] if real else []
            result = _run_e01('e01_positions.py', args, timeout=45)
            _sub_pos_cache = result
            _sub_pos_ts    = time.time()
            _sub_pos_src_mtime = src_mtime
        self._send_json(result)

    def _sub_balance(self, parsed):
        global _sub_bal_cache, _sub_bal_ts, _sub_bal_src_mtime
        params  = self._qs(parsed)
        refresh = bool(params.get('refresh'))
        real    = bool(params.get('real'))
        now     = time.time()
        src_mtime = _safe_mtime(SUB_HOLDINGS_FILE)
        with _sub_bal_lock:
            cache_valid = (
                not refresh and _sub_bal_cache
                and (now - _sub_bal_ts) < SUB_BAL_TTL
                and _sub_bal_src_mtime == src_mtime
            )
            if cache_valid:
                self._send_json(_sub_bal_cache); return
            args = ['--real'] if real else []
            result = _run_e01('e01_bank_balance.py', args, timeout=30)
            _sub_bal_cache = result
            _sub_bal_ts    = time.time()
            _sub_bal_src_mtime = src_mtime
        self._send_json(result)

    # ── asset history snapshot ────────────────────────────
    def _asset_snapshot_save(self):
        """
        前端 _saveAssetSnapshot 觸發：
          POST /asset/snapshot/save  body: {"date":"YYYY-MM-DD", "v":..,"vt":..,"c":..,"ct":..,"p":..}
        merge 寫入 data/asset_history.json（同 schema 同 key 直接覆寫當日）
        """
        body = self._read_body()
        date = str(body.get('date') or '').strip()
        if not date or len(date) != 10 or date[4] != '-' or date[7] != '-':
            self._send_json({'status': 'fail', 'error': 'invalid date (need YYYY-MM-DD)'}, status=400)
            return
        try:
            entry = {
                'v':  float(body.get('v')  or 0),
                'vt': float(body.get('vt') or 0),
                'c':  float(body.get('c')  or 0),
                'ct': float(body.get('ct') or 0),
                'p':  float(body.get('p')  or 0),
            }
        except (TypeError, ValueError) as e:
            self._send_json({'status': 'fail', 'error': f'invalid number: {e}'}, status=400)
            return
        if entry['v'] <= 0:
            self._send_json({'status': 'fail', 'error': 'v must be > 0'}, status=400)
            return

        try:
            hist = {}
            if os.path.exists(ASSET_HISTORY_FILE):
                with open(ASSET_HISTORY_FILE, encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    hist = raw
            hist[date] = entry
            tmp = ASSET_HISTORY_FILE + '.tmp'
            os.makedirs(os.path.dirname(ASSET_HISTORY_FILE), exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(hist, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, ASSET_HISTORY_FILE)
            self._send_json({'status': 'ok', 'date': date, 'count': len(hist)})
        except Exception as e:
            self._send_json({'status': 'fail', 'error': f'write failed: {e}'}, status=500)

    def _sub_orders(self, parsed):
        """今日委託（從本機 orders.jsonl 讀取，10s 快取）"""
        global _sub_ord_cache, _sub_ord_ts
        params  = self._qs(parsed)
        refresh = bool(params.get('refresh'))
        real    = bool(params.get('real'))
        now     = time.time()
        with _sub_ord_lock:
            if not refresh and _sub_ord_cache and (now - _sub_ord_ts) < SUB_ORD_TTL:
                self._send_json(_sub_ord_cache); return
            args = ['--real'] if real else []
            result = _run_e01('e01_orders.py', args, timeout=30)
            _sub_ord_cache = result
            _sub_ord_ts    = time.time()
        self._send_json(result)

    def _sub_order(self):
        body = self._read_body()
        symbol = str(body.get('symbol', '')).strip().upper()
        real = bool(body.get('real'))
        dry_run = bool(body.get('dry_run'))
        if not symbol:
            self._send_json({'status': 'fail', 'error': 'symbol required'}); return
        if real and dry_run:
            self._send_json({'status': 'fail', 'error': 'real 與 dry_run 不可同時為 true'}, status=400); return
        if real:
            confirm_real = str(body.get('confirm_real', '')).strip().upper()
            if confirm_real != 'REAL':
                self._send_json({'status': 'fail', 'error': 'real order requires confirm_real=REAL'}, status=400); return
        args = [
            '--side',   str(body.get('side', 'buy')),
            '--symbol', symbol,
            '--market', str(body.get('market', 'US')).upper(),
            '--price',  str(body.get('price', 0)),
            '--qty',    str(body.get('qty', 1)),
            '--tif',    str(body.get('tif', 'ROD')).upper(),
        ]
        if body.get('market_order'):
            args.append('--market-order')
        if dry_run:
            args.append('--dry-run')
        if real:
            args.append('--real')
        # 下單後清持倉、今日委託快取以便 UI 立刻看到變化
        global _sub_pos_cache, _sub_ord_cache
        with _sub_pos_lock:
            _sub_pos_cache = None
        with _sub_ord_lock:
            _sub_ord_cache = None
        self._send_json(_run_e01('e01_order.py', args, timeout=40))

    def _sub_sync_profit_query(self):
        global _sub_pos_cache, _sub_pos_ts, _sub_pos_src_mtime
        global _sub_bal_cache, _sub_bal_ts, _sub_bal_src_mtime
        body = self._read_body()
        args = []
        if body.get('query') is False:
            args.append('--no-query')
        result = _run_e01('e01_profit_query_sync.py', args, timeout=120)
        if result.get('status') == 'ok':
            with _sub_pos_lock:
                _sub_pos_cache = None
                _sub_pos_ts = 0.0
                _sub_pos_src_mtime = None
            with _sub_bal_lock:
                _sub_bal_cache = None
                _sub_bal_ts = 0.0
                _sub_bal_src_mtime = None
        self._send_json(result)

    def _sub_gui_order(self):
        """[DISABLED 2026-04] 富邦複委託 API 尚未開放，暫停 GUI 自動下單串接。

        之前版本會呼叫 e01_gui_order.py 自動填欄位 + 按買/賣 + 按立即下單 (auto_confirm)。
        因 e01 為 High integrity，server 為 Medium，UIPI 會擋 SendInput；
        需要 Admin server 才能動。目前先關閉端點，避免誤用。

        恢復方式 (API 釋出後)：
          1. 把本函式 body 還原成 git 歷史中 2026-04 之前的版本
             （git log -p J:\\股市相關\\server.py | grep -A50 _sub_gui_order）
          2. dashboard 的 oMode select 加回 gui_auto/real 選項
          3. 記得 server 要用 Admin 啟動 (server_as_admin.bat)
        """
        self._send_json({
            'status': 'fail',
            'error': '富邦複委託 API 尚未開放，GUI 自動下單已停用。請使用 dashboard 的 DRY 模式。',
            'code': 'sub_gui_order_disabled',
        }, status=503)

    def _sub_cancel(self):
        body = self._read_body()
        order_no = str(body.get('order_no', '')).strip()
        if not order_no:
            self._send_json({'status': 'fail', 'error': 'order_no required'}); return
        args = ['--order-no', order_no]
        if body.get('real'):
            args.append('--real')
        global _sub_pos_cache, _sub_ord_cache
        with _sub_pos_lock:
            _sub_pos_cache = None
        with _sub_ord_lock:
            _sub_ord_cache = None
        self._send_json(_run_e01('e01_cancel.py', args, timeout=30))

    def log_message(self, fmt, *args):
        pass  # silence access logs


if __name__ == '__main__':
    # 啟動 daemon watchdog 執行緒（確保 fmx_quote_daemon 持續運行）
    if os.path.exists(DAEMON_SCRIPT):
        t = threading.Thread(target=_daemon_watchdog, daemon=True, name='daemon-watchdog')
        t.start()
    else:
        print(f'[daemon] 找不到 {DAEMON_SCRIPT}，跳過自動啟動', flush=True)

    # 【已停用】strategy watchdog 執行緒
    # 原因：與 fubon_bot/start_bot.bat 的 watchdog 衝突，會導致雙 spawn 雙下單。
    # 現在改由 start_bot.bat 單一管控源頭，server.py 不再自動重啟策略。
    # Dashboard 的「啟動策略」按鈕仍可用（手動 spawn 一次性，crash 後不會自動拉起）。
    # 若要恢復 watchdog，取消下方註解即可。
    if os.path.exists(STRATEGY_SCRIPT):
        print('[strategy] watchdog 已停用（避免與 start_bot.bat 衝突）', flush=True)
        # ts = threading.Thread(target=_strategy_watchdog, daemon=True, name='strategy-watchdog')
        # ts.start()
    else:
        print(f'[strategy] 找不到 {STRATEGY_SCRIPT}，跳過自動守護', flush=True)

    # 啟動 futopt archive 排程執行緒（每日 14:00+ 自動 snapshot 並回補 TAIFEX 日 K）
    if os.path.exists(ARCHIVE_SCRIPT):
        ta = threading.Thread(target=_futopt_archive_scheduler, daemon=True,
                              name='futopt-archive')
        ta.start()
        print(f'[archive] 排程已啟動（每 10 分鐘檢查；觸發條件：平日 >= 14:00）', flush=True)
    else:
        print(f'[archive] 找不到 {ARCHIVE_SCRIPT}，跳過自動累積', flush=True)

    # 啟動 futures equity 快照排程（每日 14:05+ 自動寫入 data/futures_history.json）
    if os.path.exists(FUT_SNAPSHOT_SCRIPT):
        tfs = threading.Thread(target=_futures_snapshot_scheduler, daemon=True,
                               name='fut-snapshot')
        tfs.start()
    else:
        print(f'[fut-snap] 找不到 {FUT_SNAPSHOT_SCRIPT}，跳過自動快照', flush=True)

    # 啟動加密資產淨值排程（每 10 分鐘更新 data/crypto_balance.json）
    if os.path.exists(CRYPTO_FETCH_SCRIPT):
        tcr = threading.Thread(target=_crypto_balance_scheduler, daemon=True,
                               name='crypto-balance')
        tcr.start()
    else:
        print(f'[crypto] 找不到 {CRYPTO_FETCH_SCRIPT}，跳過加密資產淨值', flush=True)

    with http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler) as srv:
        print(f'[server] running on http://0.0.0.0:{PORT}  (press Ctrl+C to stop)')
        srv.serve_forever()
