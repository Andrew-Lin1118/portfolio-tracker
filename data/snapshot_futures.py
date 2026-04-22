#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
snapshot_futures.py — 每日收盤後快照期貨權益數

寫入 data/futures_history.json，供 portfolio-tracker-v13.html 計算「昨日漲跌(期貨)」：
    prev_day_pnl = today's today_balance - yesterday's today_balance

觸發來源：
  1. server.py 內的 _futures_snapshot_scheduler（每日 14:05 TW 自動）
  2. 手動：python data/snapshot_futures.py [--commit]

格式（snapshots 以日期升冪排序，保留最近 400 筆）：
  {
    "generated": "2026-04-23T14:00:05+08:00",
    "snapshots": [
      {
        "date": "2026-04-22",
        "ts":   "2026-04-22T14:00:05+08:00",
        "today_balance":     1234567.0,
        "yesterday_balance": 1230000.0,
        "equity":            1240000.0,
        "available":          800000.0,
        "unrealized_pnl":       5433.0,
        "realized_pnl":            0.0
      }
    ]
  }
"""
import os
import sys
import json
import subprocess
import datetime
import argparse

BASE_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FMX_POSITIONS = os.path.join(BASE_DIR, 'fubon_bot', 'fmx_positions.py')
HISTORY_FILE  = os.path.join(BASE_DIR, 'data', 'futures_history.json')

NWIN_FLAG = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

# 與 server.py 一致：優先使用 python_stock.exe（含 Surfshark Bypasser）
_py_dir   = os.path.dirname(sys.executable)
_py_stock = os.path.join(_py_dir, 'python_stock.exe')
PY = _py_stock if os.path.exists(_py_stock) else sys.executable


def _log(msg):
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    print(f'[snapshot {ts}] {msg}', flush=True)


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _run_fmx_positions(timeout=60):
    """執行 fubon_bot/fmx_positions.py，取最後一行 JSON。"""
    try:
        proc = subprocess.run(
            [PY, FMX_POSITIONS],
            capture_output=True, text=True,
            encoding='utf-8', errors='replace',
            timeout=timeout,
            creationflags=NWIN_FLAG,
        )
        out = (proc.stdout or '').strip()
        if not out:
            stderr = (proc.stderr or '').strip()[-300:]
            _log(f'fmx_positions.py 無輸出；stderr={stderr}')
            return None
        for line in reversed(out.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        _log(f'fmx_positions.py 輸出無法解析為 JSON（前 200 字）：{out[:200]}')
        return None
    except subprocess.TimeoutExpired:
        _log(f'fmx_positions.py 逾時（{timeout}s）')
        return None
    except Exception as e:
        _log(f'fmx_positions.py 例外：{e}')
        return None


def _load_history():
    if not os.path.exists(HISTORY_FILE):
        return {'generated': '', 'snapshots': []}
    try:
        with open(HISTORY_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {'generated': '', 'snapshots': []}
        if not isinstance(data.get('snapshots'), list):
            data['snapshots'] = []
        return data
    except Exception as e:
        _log(f'讀取 history 失敗（{e}），將重建')
        return {'generated': '', 'snapshots': []}


def _save_history(history):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp = HISTORY_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HISTORY_FILE)


def snapshot(commit=False):
    """快照一次，成功回傳 True。"""
    result = _run_fmx_positions()
    if not result:
        return False
    if result.get('error'):
        _log(f'fmx_positions 回傳 error：{result.get("error")}')
        return False

    margin = result.get('margin') or {}
    if not margin:
        _log('margin 欄位為空（可能 SDK 未登入或未持倉），跳過')
        return False

    now = datetime.datetime.now()
    date_str = now.strftime('%Y-%m-%d')
    ts_str   = now.isoformat(timespec='seconds')

    entry = {
        'date':              date_str,
        'ts':                ts_str,
        'today_balance':     _num(margin.get('today_balance')),
        'yesterday_balance': _num(margin.get('yesterday_balance')),
        'equity':            _num(margin.get('equity')),
        'available':         _num(margin.get('available')),
        'unrealized_pnl':    _num(margin.get('unrealized_pnl')),
        'realized_pnl':      _num(margin.get('realized_pnl')),
    }

    history = _load_history()
    snaps = history['snapshots']
    idx = next((i for i, s in enumerate(snaps) if s.get('date') == date_str), None)
    if idx is not None:
        snaps[idx] = entry
        action = '更新'
    else:
        snaps.append(entry)
        action = '新增'

    # 依日期升冪，保留最近 400 天
    snaps.sort(key=lambda s: s.get('date', ''))
    if len(snaps) > 400:
        snaps[:] = snaps[-400:]

    history['generated'] = ts_str
    _save_history(history)

    _log(f'{action} {date_str}: equity={entry["equity"]} today_balance={entry["today_balance"]}')

    if commit:
        _git_commit_push(date_str)
    return True


def _git(args, check=True):
    return subprocess.run(
        ['git', '-C', BASE_DIR] + list(args),
        capture_output=True, text=True,
        encoding='utf-8', errors='replace',
        check=check, creationflags=NWIN_FLAG,
    )


def _git_commit_push(date_str):
    """只處理 data/futures_history.json；保留使用者其他未提交變更。"""
    try:
        _git(['add', 'data/futures_history.json'])
        diff = _git(['diff', '--cached', '--quiet'], check=False)
        if diff.returncode == 0:
            _log('檔案無變動，略過 commit')
            return
        _git(['commit', '-m', f'chore: futures equity snapshot {date_str}'])
        # rebase 失敗就把 commit 留在本地，等下次再試
        _git(['pull', '--rebase', 'origin', 'main'], check=False)
        _git(['push', 'origin', 'main'])
        _log('git push 完成')
    except subprocess.CalledProcessError as e:
        err = (e.stderr or '')[-300:] if hasattr(e, 'stderr') else str(e)
        _log(f'git 操作失敗：{err}')
    except Exception as e:
        _log(f'git 例外：{e}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--commit', action='store_true',
                    help='快照後自動 git commit + push（讓 GitHub Pages 取得最新資料）')
    args = ap.parse_args()
    ok = snapshot(commit=args.commit)
    sys.exit(0 if ok else 1)
