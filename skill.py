"""
================================================================================
  Trading / Monitoring Platform Building Skill  (建置手冊 v1)
================================================================================

  來源專案:
    1. 策略監控平台  — fmx-dashboard.html + server.py + strategy-fmx-live.py
       (富邦期貨 FMX 台指期 自動化槓桿策略)
    2. 台股交易平台  — stock-dashboard.html + stock_*.py
       (富邦證券 Fubon SDK 下單/查詢/持倉)
    3. BTC 交易平台  — pionex_bot/ + portfolio-tracker-*.html
       (Pionex 網格 / 持倉追蹤)

  用法:
    - 啟動新專案前先通讀 CHECKLIST
    - 直接 `from skill import PY_PATHS, NWIN_FLAG, build_watchdog, ...`
      複用常數與樣板
    - 遇到 bug 先對照 PITFALLS 看是不是已知坑

  維護規則:
    - 每次遇到「又踩到的坑」→ 在 PITFALLS 新增條目
    - 架構有重大變更 → 更新 ARCHITECTURE
    - 版本用 `SKILL_VERSION` 標記
"""

from __future__ import annotations
import os
import sys
import json
import time
import subprocess
import threading
from typing import Callable, Optional

SKILL_VERSION = "1.2.0"   # +H 章節(交易所簽名/時鐘) +D3-D8(portfolio-tracker) +I(GitHub Pages git) +Section11


# ═══════════════════════════════════════════════════════════════════════════
#  1. 檔案系統 / 路徑常數
# ═══════════════════════════════════════════════════════════════════════════

# 可用的 Python 解譯器（按優先順序）
# 關鍵：絕不使用 C:\Python310-Trading\ 那套（欠缺 yaml / fubon_neo）
PY_PATHS = {
    # python_stock.exe 已加入 Surfshark Bypasser → 走真實 IP（富邦 SDK 要的）
    "preferred_stock":  r"C:\Users\User\AppData\Local\Programs\Python\Python310\python_stock.exe",
    "preferred":        r"C:\Users\User\AppData\Local\Programs\Python\Python310\python.exe",
    # 絕對禁用（套件不完整，會造成 watchdog 無限 crash loop）
    "blacklist_prefixes": (r"c:\python310-trading",),
}


def resolve_python() -> str:
    """
    解析出該用哪個 Python。新專案呼叫 subprocess 前必呼叫此函式，
    不要直接用 sys.executable（當使用者打 `python x.py` 時可能解析到
    錯誤的 Python，例如 C:\\Python310-Trading）。
    """
    stock = PY_PATHS["preferred_stock"]
    main  = PY_PATHS["preferred"]
    if os.path.exists(stock):
        return stock
    if os.path.exists(main):
        return main
    # 最後才看 sys.executable，並檢查黑名單
    exe = sys.executable or ""
    low = exe.lower()
    if any(low.startswith(bl) for bl in PY_PATHS["blacklist_prefixes"]):
        # 被黑名單了，寧可 fallback 到 preferred（即使不存在也不用 trading）
        return main
    return exe


# Windows 專用：防止 subprocess 跳出黑色 CMD 視窗
NWIN_FLAG = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# UTF-8 environment（避免中文路徑/輸出亂碼）
def make_env() -> dict:
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


# ═══════════════════════════════════════════════════════════════════════════
#  2. 架構 (ARCHITECTURE)
# ═══════════════════════════════════════════════════════════════════════════
"""
┌─────────────────── 單檔 HTTP Server ───────────────────┐
│  server.py  (http.server BaseHTTPRequestHandler)      │
│    ├─ Thread: daemon_watchdog  → 行情/持倉 daemon     │
│    ├─ Thread: strategy_watchdog→ 策略主程序           │
│    ├─ REST endpoints                                  │
│    │    ├─ GET  /fmx/positions       (60s cache)      │
│    │    ├─ GET  /fmx/market_quotes   (12s cache)      │
│    │    ├─ POST /fmx/start|stop|...  → subprocess     │
│    │    ├─ GET  /stock/quote?symbol= → subprocess     │
│    │    └─ POST /stock/order         → subprocess     │
│    └─ Static serve: *.html / *.js / *.png             │
└────────────────────────────────────────────────────────┘

設計原則:
  1. **subprocess per SDK call**
       富邦 SDK 有連線狀態/鎖問題，長駐 Python 程序重複呼叫會卡住。
       每個 API 請求 fork 一支 *.py，讓 SDK 重新登入 → 簡單、隔離、好 debug。
       代價：每次呼叫 1~3 秒延遲（靠 cache 緩解）。

  2. **State 檔單一來源**
       fmx_state.json / positions cache file / live_quotes.json
       所有 process 都讀寫同一個 JSON 檔，不要用 multiprocessing.Manager。

  3. **Watchdog 分層**
       - daemon watchdog  → 行情/持倉背景 daemon（每 15 秒檢查）
       - strategy watchdog → 策略主程序（每 30 秒檢查，尊重 bot_running 旗標）
       都要有 **fast-fail cooldown**（下面 SECTION 5）避免無限 crash 迴圈。

  4. **前端單 HTML + polling**
       不要引入 React/Vue/build step。純 HTML + fetch() 每 N 秒 poll 一次。
       所有狀態透過 REST 取得，沒有 WebSocket（富邦行情 daemon 自己寫 JSON 檔）。
"""


# ═══════════════════════════════════════════════════════════════════════════
#  3. subprocess 樣板 (複製即用)
# ═══════════════════════════════════════════════════════════════════════════

def run_sdk_subprocess(
    script_path: str,
    args: tuple = (),
    timeout: int = 45,
    cwd: Optional[str] = None,
) -> dict:
    """
    執行 SDK script，回傳最後一行有效 JSON。

    為何取「最後一行」? fubon SDK 心跳/連線訊息會先印到 stdout，
    真正的結果在最後一行。

    要點:
      - capture_output=True + errors='replace' 避免編碼崩潰
      - creationflags=NWIN_FLAG 不要彈 CMD
      - timeout 務必設定，否則 SDK 卡死會拖垮整個 server
    """
    py = resolve_python()
    try:
        proc = subprocess.run(
            [py, script_path, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=make_env(),
            creationflags=NWIN_FLAG,
            cwd=cwd,
        )
        out = (proc.stdout or "").strip()
        if not out:
            tail = (proc.stderr or "").strip()[-500:]
            return {"status": "fail", "error": f"no stdout: {tail}"}
        for line in reversed(out.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return {"status": "fail", "error": "no valid JSON line", "raw": out[:300]}
    except subprocess.TimeoutExpired:
        return {"status": "fail", "error": f"subprocess timeout ({timeout}s)"}
    except Exception as e:
        return {"status": "fail", "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
#  4. Cache 樣板 (TTL cache for SDK responses)
# ═══════════════════════════════════════════════════════════════════════════

class TTLCache:
    """
    建議參數:
      - 持倉 positions  → 60 秒
      - 行情 quotes     → 12 秒
      - yfinance  OK    → 3600 秒
      - yfinance  fail  → 180 秒 (短 TTL 避免 Yahoo 抽風時把整個 UI 拖死)
    """
    def __init__(self, ok_ttl: int, err_ttl: Optional[int] = None):
        self._ok_ttl  = ok_ttl
        self._err_ttl = err_ttl or max(60, ok_ttl // 10)
        self._data = {}   # key -> (ts, result, is_ok)
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            item = self._data.get(key)
        if not item:
            return None
        ts, result, is_ok = item
        ttl = self._ok_ttl if is_ok else self._err_ttl
        if time.time() - ts > ttl:
            return None
        return result

    def set(self, key: str, result, is_ok: bool = True):
        with self._lock:
            self._data[key] = (time.time(), result, is_ok)


# ═══════════════════════════════════════════════════════════════════════════
#  5. Watchdog 樣板 (重中之重 — 沒加 cooldown 會爆炸)
# ═══════════════════════════════════════════════════════════════════════════

def build_watchdog(
    *,
    name: str,
    script: str,
    check_alive: Callable[[], bool],
    should_run: Callable[[], bool] = lambda: True,
    build_args: Callable[[], list] = lambda: [],
    check_interval: int = 15,
    fast_fail_window: float = 10.0,
    fast_fail_limit: int = 5,
    cooldown_sec: int = 300,
    cwd: Optional[str] = None,
):
    """
    回傳一個可 `threading.Thread(target=...)` 的 watchdog 函式。

    關鍵保護:
      - fast_fail: 如果啟動後 `fast_fail_window` 秒內就死掉，累計失敗次數。
        達到 `fast_fail_limit` 次 → 冷卻 `cooldown_sec` 秒才再試。
        避免因為設定錯誤 / 套件缺失造成的 crash loop（曾因 yaml 缺失每秒啟 2 次）。
      - should_run: 尊重使用者的 bot_running 旗標，按停止就不要自動重拉。
      - check_alive: 外部探測（例如 wmic、process list），
        server 重啟後 Popen handle 遺失時的備援。
    """
    proc_holder: dict = {"proc": None}
    lock = threading.Lock()

    def _loop():
        time.sleep(3)
        print(f"[{name}] watchdog started (using {resolve_python()})", flush=True)
        fast_fail = 0
        last_start = 0.0
        cooldown_until = 0.0
        while True:
            try:
                now = time.time()
                if now < cooldown_until:
                    time.sleep(min(check_interval, cooldown_until - now))
                    continue
                if not should_run():
                    fast_fail = 0
                    time.sleep(check_interval)
                    continue

                with lock:
                    proc = proc_holder["proc"]
                    tracked_alive = (proc is not None and proc.poll() is None)

                if tracked_alive or check_alive():
                    fast_fail = 0
                    time.sleep(check_interval)
                    continue

                # 快速失敗偵測
                if proc is not None and last_start and (now - last_start) < fast_fail_window:
                    fast_fail += 1
                    print(f"[{name}] fast-fail #{fast_fail} (returncode={proc.poll()})", flush=True)
                    if fast_fail >= fast_fail_limit:
                        cooldown_until = now + cooldown_sec
                        print(f"[{name}] {fast_fail_limit} fast-fails → cooldown {cooldown_sec}s", flush=True)
                        fast_fail = 0
                        continue

                args = [resolve_python(), script] + list(build_args())
                new = subprocess.Popen(
                    args,
                    cwd=cwd,
                    env=make_env(),
                    stdout=subprocess.DEVNULL,   # 絕對不可 PIPE 而不讀！Windows pipe 64KB 塞滿會卡住
                    stderr=subprocess.DEVNULL,
                    creationflags=NWIN_FLAG,
                )
                with lock:
                    proc_holder["proc"] = new
                last_start = time.time()
                print(f"[{name}] PID {new.pid} started", flush=True)
            except Exception as e:
                print(f"[{name}] watchdog error: {e}", flush=True)
            time.sleep(check_interval)

    return _loop, proc_holder


# ═══════════════════════════════════════════════════════════════════════════
#  6. State 檔操作 (讀寫 JSON 的安全 pattern)
# ═══════════════════════════════════════════════════════════════════════════

_state_locks: dict = {}
_state_meta_lock = threading.Lock()

def _lock_for(path: str) -> threading.Lock:
    with _state_meta_lock:
        if path not in _state_locks:
            _state_locks[path] = threading.Lock()
        return _state_locks[path]

def read_state(path: str, default: Optional[dict] = None) -> dict:
    if default is None:
        default = {}
    if not os.path.exists(path):
        return dict(default)
    try:
        with _lock_for(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return dict(default)

def write_state(path: str, state: dict) -> None:
    """
    原子寫入：tmp → rename。避免讀端讀到半截 JSON。
    """
    with _lock_for(path):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


# ═══════════════════════════════════════════════════════════════════════════
#  6.5 交易所 API 健康診斷 (Binance / 富邦 / 其他簽名 API)
# ═══════════════════════════════════════════════════════════════════════════
#  教訓來源：2026-04-22 Pionex Bot 「餘額顯示 --」事件。
#  症狀：/api/price 正常、/api/status 的 balance=None，log 全是 [-1021]
#  Timestamp outside recvWindow + RemoteDisconnected。
#
#  根因其實很單純：Windows Time 服務沒啟動，時鐘靠 Local CMOS Clock 漂了 5.3 秒
#  → Binance 簽名請求超出 recvWindow=5000ms → 伺服器直接切斷連線。
#
#  為什麼沒立刻診斷出來：
#    - 公開端點（/fapi/v1/ticker/price）不驗簽，照常運作，誤以為「網路正常」
#    - 舊 log 有 -2015（VPN 斷過）混淆了時間順序
#    - RemoteDisconnected 聽起來像網路問題，其實是 Binance 主動切斷簽名連線
#
#  判讀順序：clock → ip → procs → dashboard。先排除簡單的（時鐘）再查複雜的。

_PY_VPN_DEFAULT = r"C:\Python310-Trading\python.exe"


def check_clock_drift(api_url: str = "https://fapi.binance.com/fapi/v1/time",
                      recv_window_ms: int = 5000) -> dict:
    """比對本機時鐘 vs 交易所 server time。

    回傳 {"ok": bool, "diff_ms": int, "server_ms": int, "local_ms": int, "msg": str}

    判讀:
      abs(diff) < 3000ms  → OK（遠低於 recvWindow）
      abs(diff) < recv_window_ms → WARN（接近邊界）
      else → FAIL（簽名請求會被 Binance 拒絕 / 切斷連線）

    踩過的雷: 對應 PITFALLS [H1] — Windows Time 服務預設可能沒跑，
             `w32tm /query /status` 看 Source 是 "Local CMOS Clock" 就是漂移來源。
    """
    import urllib.request
    try:
        with urllib.request.urlopen(api_url, timeout=8) as r:
            srv = json.loads(r.read())["serverTime"]
    except Exception as e:
        return {"ok": False, "msg": f"無法打 {api_url}: {type(e).__name__}: {e}"}
    loc = int(time.time() * 1000)
    diff = loc - srv
    adiff = abs(diff)
    if adiff < 3000:
        return {"ok": True, "diff_ms": diff, "server_ms": srv, "local_ms": loc,
                "msg": f"時鐘正常 (偏差 {adiff/1000:.2f}s)"}
    if adiff < recv_window_ms:
        return {"ok": True, "diff_ms": diff, "server_ms": srv, "local_ms": loc,
                "msg": f"時鐘接近邊界 ({adiff/1000:.2f}s / {recv_window_ms}ms)，建議跑 fix_clock_sync()"}
    return {"ok": False, "diff_ms": diff, "server_ms": srv, "local_ms": loc,
            "msg": f"時鐘漂移 {adiff/1000:.2f}s > recvWindow — 簽名請求會被拒"}


def check_vpn_exit_ip(python_exe: str = _PY_VPN_DEFAULT) -> dict:
    """從指定的 python.exe 實際打外網，看 Binance/Fubon 會看到什麼 IP。

    不要用瀏覽器 / 不要用 cmd 的 curl — 那些走的路由可能和 subprocess 不同。
    關鍵：Surfshark Bypasser 的「Route via VPN」規則是**按 exe 路徑**綁，
    同一隻 python.exe 不同位置會有不同路由結果。

    回傳 {"ok": bool, "ip": str, "via_vpn": bool, "msg": str}
    """
    if not os.path.exists(python_exe):
        return {"ok": False, "ip": "", "via_vpn": False,
                "msg": f"找不到 {python_exe}"}
    code = (
        "import urllib.request,sys\n"
        "try:\n"
        "  print(urllib.request.urlopen('https://api.ipify.org',timeout=8).read().decode())\n"
        "except Exception as e:\n"
        "  print('ERR:'+type(e).__name__+':'+str(e)); sys.exit(1)\n"
    )
    try:
        out = subprocess.check_output(
            [python_exe, "-c", code], timeout=15,
            stderr=subprocess.STDOUT, creationflags=NWIN_FLAG,
        ).decode("utf-8", errors="replace").strip()
    except subprocess.TimeoutExpired:
        return {"ok": False, "ip": "", "via_vpn": False,
                "msg": "15 秒超時 — VPN 可能斷線 / DNS 問題"}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "ip": "", "via_vpn": False,
                "msg": f"python.exe 失敗: {e.output.decode(errors='replace')[:200]}"}
    if out.startswith("ERR:"):
        return {"ok": False, "ip": "", "via_vpn": False, "msg": out}
    # 台灣 Hinet / 中華電信常見開頭（Bypasser 失效 / VPN 斷的徵兆）
    tw_prefixes = ("114.", "1.34.", "1.160.", "1.161.", "61.", "203.")
    via_vpn = not out.startswith(tw_prefixes)
    return {"ok": via_vpn, "ip": out, "via_vpn": via_vpn,
            "msg": f"IP={out} {'(VPN)' if via_vpn else '(台灣真實 IP — VPN 沒生效)'}"}


_FIX_CLOCK_BAT = r"""@echo off
chcp 65001 >nul
echo ===== Before =====
w32tm /query /status
echo.
echo ===== Configure NTP servers =====
w32tm /config /manualpeerlist:"time.windows.com,0x8 time.nist.gov,0x8 tock.stdtime.gov.tw,0x8 time.google.com,0x8" /syncfromflags:manual /reliable:YES /update
echo.
echo ===== Set W32Time to auto-start =====
sc config w32time start= auto
net start w32time
echo.
echo ===== Force resync =====
w32tm /resync /force
echo.
echo ===== After =====
w32tm /query /status
w32tm /query /source
echo.
echo ====== DONE - auto close in 8 seconds ======
timeout /t 8 /nobreak >nul
"""


def fix_clock_sync(verify_after: bool = True) -> dict:
    """把修復 bat 寫到 C:\\Windows\\Temp\\ 並用 UAC 提示跑起來。

    踩過的雷：bat 放在含中文字路徑（J:\\股市相關\\...）透過 PowerShell
    Start-Process -ArgumentList 傳進 cmd /c 時，中文被轉碼吃掉 → 整個
    指令被切成單字母（看到螢幕上全是 'cp' 'ho' '2tm' 不是命令的錯誤）。
    解法：bat 放純 ASCII 路徑，直接 Start-Process -FilePath，不透過 cmd /c。

    會做:
      1. 設 4 個 NTP（time.windows.com / time.nist.gov / tock.stdtime.gov.tw /
         time.google.com）
      2. sc config w32time start= auto（預設是 Manual / 停止，這才是元兇）
      3. net start w32time + w32tm /resync /force
      4. 8 秒後 bat 視窗自動關閉
    """
    bat_path = r"C:\Windows\Temp\fix_clock.bat"
    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(_FIX_CLOCK_BAT)
    try:
        subprocess.run([
            "powershell", "-NoProfile", "-Command",
            f"Start-Process -FilePath '{bat_path}' -Verb RunAs",
        ], timeout=10, creationflags=NWIN_FLAG)
    except Exception as e:
        return {"ok": False, "msg": f"彈 UAC 失敗: {e}（手動右鍵 {bat_path} 以系統管理員身份執行）"}
    if not verify_after:
        return {"ok": True, "msg": "已觸發 UAC；請在彈出視窗點「是」"}
    time.sleep(12)
    return check_clock_drift()


def diagnose_exchange_api(python_exe: str = _PY_VPN_DEFAULT,
                          api_url: str = "https://fapi.binance.com/fapi/v1/time") -> dict:
    """交易所 API 一鍵健檢 — 餘額 / 持倉抓不到時先跑這個。

    判讀順序和常見組合：
      clock FAIL + ip OK       → 只修時鐘（fix_clock_sync）
      clock OK   + ip FAIL     → 重連 VPN / 檢查 Bypasser
      clock FAIL + ip FAIL     → 兩個都修
      clock OK   + ip OK       → 可能是 API key 失效 / 白名單變更 / -2015
    """
    c = check_clock_drift(api_url=api_url)
    i = check_vpn_exit_ip(python_exe=python_exe)
    suggestions = []
    if not c["ok"]:
        suggestions.append("fix_clock_sync()  # 修時鐘")
    if not i["ok"]:
        suggestions.append("重連 Surfshark 並確認 Bypasser 含 " + python_exe)
    if c["ok"] and i["ok"]:
        suggestions.append("clock + ip 都正常 → 檢查 API key 是否失效 / IP 白名單")
    return {"clock": c, "ip": i, "ok": c["ok"] and i["ok"], "next_steps": suggestions}


# ═══════════════════════════════════════════════════════════════════════════
#  7. PITFALLS — 過去踩過的坑清單
# ═══════════════════════════════════════════════════════════════════════════
PITFALLS = """
╔═══════════════════════════════════════════════════════════════════════════╗
║                          踩過的坑 (按類別)                                 ║
╚═══════════════════════════════════════════════════════════════════════════╝

[A] Windows bat 檔
  A1. **必須 CRLF (\\r\\n) 換行，不可用 LF**
      症狀: `WORKDIR` 變 `RKDIR`、`:loop` 變 `oop`、
            `'LISTENING" > nul' 不是內部或外部命令`。
      原因: CMD bat 解析器只靠 CR 切行。
      解法: 用 Windows 記事本/ Notepad++ 存 CRLF；或 printf '\\r\\n' 寫入；
            Claude 的 Write tool 會寫 LF，需在外層手動補 CR。

  A2. **bat 內不要放中文字**
      症狀: 檔案被當 UTF-8 儲存，但 CMD 以 CP950 讀取，
            每個 3-byte 中文字吃掉後面一個 ASCII byte → 整個指令錯位。
      解法: 所有路徑/訊息都用 ASCII；必要時用 `%~dp0` 取得 bat 自己所在目錄。

  A3. **不要依賴 PATH 裡的 python**
      症狀: `python server.py` 解析到 `C:\\Python310-Trading\\python.exe`
            （沒裝 yaml / fubon_neo）→ daemon/strategy 每次啟動都 crash。
      解法: bat 裡寫死 `set "PYTHON=C:\\...\\python_stock.exe"` 並 `if not exist` 檢查。

[B] Python subprocess
  B1. **必加 CREATE_NO_WINDOW (Windows)**
      症狀: 每次背景呼叫都閃一個黑色 CMD 視窗，極度惱人。
      解法: `subprocess.Popen(..., creationflags=NWIN_FLAG)`（本檔已封裝）。

  B2. **stdout=PIPE 但不讀 → 64KB 後阻塞**
      症狀: daemon 看似活著但完全沒輸出、查詢全部 timeout。
      解法: 長駐程序用 `stdout=subprocess.DEVNULL`，讓它寫自己的 log 檔。

  B3. **sys.executable 不可信**
      見 A3 & resolve_python()。

[C] Watchdog
  C1. **沒有 fast-fail cooldown → 無限 crash loop**
      症狀: `[daemon] PID xxxx 啟動成功 / 結束... / 啟動成功 / 結束...` 每秒刷屏。
      解法: 用 build_watchdog() 的 fast_fail_limit + cooldown_sec。

  C2. **Popen handle 會在 server 重啟後遺失**
      解法: watchdog 同時用 wmic / psutil 掃描進程名稱當備援（例:
            `wmic process where "name='python.exe' or name='python_stock.exe'" get commandline`）。

[D] 前端 dashboard
  D1. **SDK 資料到了但 DOM 沒更新**
      症狀: statContracts 永遠顯示「—」，但 `_sdkContracts` 變數是對的。
      原因: 下游 render 路徑只在 `_sdkContracts == null` 時才更新 DOM，
            SDK 一設 `_sdkContracts` 就永遠鎖住不更新。
      解法: 資料一進來就直接 `document.getElementById(id).textContent = ...`，
            不要依賴下游 render。

  D2. **Yahoo Finance 自訂日期範圍**
      用 `period1=<unix>&period2=<unix>` 而非 `range=`。
      注意轉成秒：`Math.floor(new Date('2020-01-01').getTime()/1000)`。

  D3. **自動更新 timer 被 UI 設定值 gate 住 → 頁面報價永遠凍結**
      症狀: 手動按「一鍵更新」有新資料；等 N 秒後自動沒更新。
      原因: setInterval callback 最前面加了
            `if (!googleSheetUrl) return;`
            只要使用者沒填 Google Sheet，interval 每次都立刻 return，
            prices.json 也一直不重讀。
      解法: auto-refresh 的 timer 主體（fetch prices.json + yfFetch）**絕不可**
            依賴 UI 輸入框的狀態作 early-return。顯示/隱藏倒數 UI 和
            實際資料刷新是兩件事，要分開。

  D4. **港股與美股的 market-open 偵測混用 → 港股漲跌一律顯示 "-"**
      症狀: 港股（7709.HK / 9747.HK）今日漲跌顯示 "-"，
            但 prices.json 裡當天確實有資料。
      原因: 用「隨機抽取幾支美股，看今日收盤是否≠昨收」來判斷市場是否開盤。
            HK（01:30-08:00 UTC）和 US（13:30-20:00 UTC）市場時段完全不重疊：
              - HK 交易時段 → 美股樣本全部還是昨收 → 誤判 "市場未開"
              - US 交易時段 → HK 已收盤 → 港股漲跌也被帶歪
      解法: .HK 標的完全脫離美股 market-open 偵測路徑，改用港股時區邏輯：
              const hkHour = parseInt(new Date().toLocaleString('en-US',
                {timeZone:'Asia/Hong_Kong', hour:'numeric', hour12:false}));
              const hkMin  = parseInt(new Date().toLocaleString('en-US',
                {timeZone:'Asia/Hong_Kong', minute:'numeric', hour12:false}));
              const hkWeekday = new Date().toLocaleDateString('en-US',
                {timeZone:'Asia/Hong_Kong', weekday:'short'});
              opened = !['Sat','Sun'].includes(hkWeekday)
                       && ((hkHour === 9 && hkMin >= 30) || (hkHour >= 10 && hkHour < 16));
      進階: 比對 prices.json 今日快照 price vs prev_close；
            差值 > 0.001 可直接確認今天已開過盤。

  D5. **GitHub Actions 定時任務不覆蓋港股交易時段 → HK 股價凍結在昨收**
      症狀: 港股交易中（UTC 01:30-08:00），頁面 HK 個股仍顯示昨日收盤價，
            漲跌幅 0.00%。
      原因: update-prices.yml 的 schedule 是 `*/5 13-21 * * 1-5`（UTC），
            對應美股交易時段，港股交易時段完全沒有 Actions 跑，
            prices.json 快照不更新。
      解法: 在前端 silentRefreshPrices 對 proxy ETF（含 .HK）呼叫
            Yahoo Finance chart API 取即時 regularMarketPrice，
            直接覆蓋 prices.json 快照值。
            速率控制：整個頁面每 10s refresh 一次，但 yfFetch 僅每 6 次才執行
            （≈60s），避免 Yahoo Finance 頻率限制。

  D6. **prevClose 資料源選錯 → 盤中漲跌幅計算錯誤**
      症狀: 盤中漲跌幅顯示 0.00% 或異常數字。
      原因: Yahoo Finance v8/chart API 回傳兩個欄位：
              meta.previousClose      → 某些情況下盤中回傳的是「當前價」
              meta.chartPreviousClose → 永遠是「上一交易日官方收盤」，跨空白
                                        交易日也正確
      解法: 一律用 meta.chartPreviousClose 作為前一收盤基準。
            prices.json 的 prev_closes 欄位亦由 fast_info.previous_close 填充，
            較可靠。避免用 meta.previousClose。

  D7. **yfFetch 沒節流 → 頻繁呼叫觸發 Yahoo Finance 429**
      症狀: 自動更新縮短到 10s 後，yfFetch 開始大量回傳錯誤，報價停更。
      解法: 設 counter，每 N 次 auto-refresh 才執行一次 yfFetch（例如每 6 次
            ≈ 60 秒）。silentRefreshPrices(skipProxyFetch=false) 加入 flag，
            timer 傳入 skipProxy = (count % 6 !== 0)。
            非 yfFetch 週期照常重讀 prices.json + Google Sheet（很輕量）。

  D8. **prevClose 備援補丁放在錯誤的執行順序 → 補進去又被清掉**
      症狀: 加了 prevClose fallback（從 prices.json prev_closes 補入
            _tmpPrevClose），卻沒有生效；debug 發現值是 null。
      原因: fallback 跑在 _marketOpenStale 區塊**之前**；
            _marketOpenStale 後段會重置 _tmpPrevClose 中 proxy ETF 的 key，
            把剛補進去的值清空。
      解法: prevClose 備援程式碼必須放在 _marketOpenStale 整個 block **之後**，
            即 `window._prevCloseCache = _tmpPrevClose` 賦值前的最後一個 try。

[E] Windows 檔案關聯 / 啟動
  E1. **Antigravity (VS Code-based editor) 自動開啟 .py**
      症狀: 雙擊 bat 後跳出 Antigravity 視窗，server.py 被編輯器開啟而非執行。
      解法1: `HKCU\\SOFTWARE\\Classes\\.py\\OpenWithProgids` 移除 `Antigravity.py`。
      解法2: bat 裡明確用 `"%PYTHON%" server.py` 而不是直接 `server.py`。

  E2. **開機兩個啟動項搶 port 3000**
      症狀: 其中一個起來後另一個 crash → 被 Antigravity fallback 接走。
      解法: bat 開頭先 netstat 檢查 port 3000，被占用就 wait-loop 直到空出。

  E3. **session restore 自動重開 server.py**
      VS Code / Antigravity 預設 `window.restoreWindows: "all"` 會記住上次開的檔，
      解法: settings.json 設 `"window.restoreWindows": "none"`。

[F] 網路 / VPN
  F1. **Surfshark Bypasser 走真實 IP**
      富邦 SDK 要走台灣真實 IP（不能經海外 VPN）。把 `python_stock.exe`
      加入 Surfshark Bypasser（複製 `python.exe` → 改名為 `python_stock.exe`）。

  F2. **Surfshark Dedicated IP 給 BTC bot**
      交易所白名單用固定 IP，必須手動在 Surfshark app 連到 Dedicated IP server。
      `86.104.213.158` 不會自動重連，斷線後要手動回來。

  F3. **Tailscale 與 Surfshark 衝突**
      tailscaled `RouteAll: true` 會搶全部 route，BTC bot 的 VPN 就失效。
      解法: 關 RouteAll 或只在需要時啟動 tailscaled。

[G] Fubon SDK
  G1. **`Unable to connect to wss://neoapi.fbs.com.tw/...`**
      多半是伺服器端短暫抽風或網路問題。WebSocket 本身 TLS/101 handshake 正常
      就表示鏈路 OK。等幾分鐘 / 換條線重試。

  G2. **config.yaml 缺失 → strategy 秒死**
      確認 fubon_bot/config.yaml 存在且格式正確；
      確認執行 strategy 的 Python 有 `pyyaml`。

[H] 交易所 API 簽名端點 (Binance / 其他需要 HMAC 簽名的)
  H1. **[-1021] Timestamp for this request is outside of the recvWindow**
      症狀: log 持續出現這個錯，且 /api/status 的 balance=None，但
            /api/price（公開端點）正常 200。
      真正根因: **Windows Time 服務沒啟動**，時鐘來源是 "Local CMOS Clock"，
                漂移幾秒就超出 Binance recvWindow (5000ms)。
      診斷一條命令:
        w32tm /query /status   # Source = Local CMOS Clock → 就是這個問題
      或從 Python:
        from skill import check_clock_drift; print(check_clock_drift())
      修法: `from skill import fix_clock_sync; fix_clock_sync()` (會彈 UAC)
      重點: 修完要 `sc config w32time start= auto`，否則下次重開機又漂。

  H2. **RemoteDisconnected('Remote end closed connection without response')**
      症狀: urllib 呼叫簽名端點回傳這個，但公開端點通。
      真正根因: 其實和 H1 同根 — Binance 收到時間戳超界的簽名請求時，
                有時不回 JSON -1021，而是直接 TCP RST。看起來像網路問題，
                其實是簽名被拒的「重症版」。
      診斷:
        1) check_clock_drift() 先看時鐘差距
        2) 如果時鐘正常 → check_vpn_exit_ip() 看 VPN 出口 IP
      絕對不要: 以為是防火牆 / SSL / requests 套件壞掉就瞎拆套件重灌。

  H3. **[-2015] Invalid API-key, IP, or permissions, request ip: x.x.x.x**
      症狀: 錯誤訊息直接印出 Binance 看到的 IP。
      真正根因: Binance API key 設了 IP 白名單，但目前出口 IP 不在其中。
      常見觸發: Surfshark VPN 斷線或 Bypasser 規則失效，導致從台灣真實 IP 送簽名。
      診斷:
        from skill import check_vpn_exit_ip
        print(check_vpn_exit_ip())
      修法: 重連 VPN → 確認 Bypasser 「Route via VPN」含正確 python.exe 路徑。

  H4. **混淆 "其他功能都正常只有餘額/持倉不動" vs "完全連不到"**
      關鍵區分: /api/price（公開端點）是否 200。
        - price 200 + balance=None  → 簽名端點問題 (H1/H2/H3)
        - price 500 + balance=None  → 網路 / DNS / VPN 本身掛了
      不要因為「能看到價格」就排除網路問題 — 公開和簽名走不同檢查。

  H5. **舊 log 混淆新問題**
      web_server 跑久了 log 累積一堆歷史錯誤（-2015 / 10053 等）。
      debug 時一定要**用 timestamp filter**，只看最新的錯誤類型，
      不要被昨天的 -2015（VPN 斷過）誤導今天的 -1021（時鐘漂）。

[I] GitHub Pages / prices.json 自動更新 git 衝突
  I1. **git push 被拒：GitHub Actions 在你推送後又 commit 了 prices.json**
      症狀:
        ! [rejected] main -> main (fetch first)
        hint: Updates were rejected because the remote contains work
              that you do not have locally.
      原因: update-prices.yml 每 5 分鐘 commit & push prices.json；
            本機同時 commit 後遠端已前進一個 commit，形成衝突。
      解法（每次推送前的標準流程）:
        git stash                       # 暫存其他未 commit 的修改
        git pull --rebase origin main   # 把遠端的 prices.json commit 接進來
        git stash pop                   # 恢復暫存的修改
        git push origin main
      原因 stash 是必要的：pull --rebase 若 working tree 有 unstaged 改動
      會直接失敗（"You have unstaged changes"）。

  I2. **prices.json 不應在本機手動 commit**
      GitHub Actions 是 prices.json 的唯一寫入者；本機若有修改只是
      測試用，push 前應確認 `git diff HEAD -- data/prices.json` 為空。
      誤把本機測試版 prices.json 推上去會讓所有使用者看到假資料，
      直到下一次 Actions 覆蓋為止（最多 5 分鐘）。
"""


# ═══════════════════════════════════════════════════════════════════════════
#  8. 新專案 CHECKLIST (建新專案時照這清單跑一遍)
# ═══════════════════════════════════════════════════════════════════════════
CHECKLIST = """
▣ 環境準備
  [ ] 確認使用 C:\\Users\\User\\AppData\\Local\\Programs\\Python\\Python310\\python_stock.exe
      (富邦類別專案) 或 python.exe
  [ ] 需要走真實 IP 的程式 (富邦 SDK) → 確認加入 Surfshark Bypasser
  [ ] 需要固定 IP 的程式 (交易所 API) → 確認 Surfshark Dedicated IP 已連線
  [ ] **Windows Time 服務設為自動啟動** (w32tm /query /status → Source 不可
      是 "Local CMOS Clock")。沒做這步交易所 API 會定時漂到 recvWindow 外。
      若首次發現: `python skill.py --diagnose` → `fix_clock_sync()`。

▣ 專案目錄結構 (建議)
  project/
    server.py                 # 主 HTTP dispatcher + watchdogs
    state.json                # 單一來源狀態
    start_server.bat          # 純 ASCII + CRLF，用 %~dp0
    dashboard.html            # 單檔前端 + polling
    bot/
      strategy-live.py        # 策略主 loop
      quote_daemon.py         # 行情/持倉 daemon (寫 JSON)
      api_*.py                # 一次性 SDK 呼叫 (positions / order / ...)
      config.yaml
    logs/
    skill.py                  # ← 這份手冊

▣ server.py 必備
  [ ] `from skill import resolve_python, NWIN_FLAG, make_env, run_sdk_subprocess`
  [ ] daemon_watchdog 用 build_watchdog() 包起來
  [ ] strategy_watchdog 加 should_run=lambda: read_state()['bot_running']
  [ ] 每個 /api/* 有 TTLCache
  [ ] CORS headers（手機/Tailscale 連進來要用）

▣ start_server.bat 必備
  [ ] CRLF 換行（用 xxd 看有沒有 0D 0A）
  [ ] 純 ASCII（不要中文 echo）
  [ ] `set "PYTHON=..."` 寫死路徑 + `if not exist` 檢查
  [ ] 用 `%~dp0` 作 WORKDIR
  [ ] 先 netstat 檢查 port，有人占就 wait loop
  [ ] crash 後 5 秒重啟 + goto loop

▣ 前端
  [ ] 所有 stat card 資料進來就 **立刻** 寫 DOM，不透過中間 render path
  [ ] polling interval 不要低於 5 秒（富邦 SDK 很慢）
  [ ] 自訂日期用 period1/period2 unix timestamp
  [ ] manifest.json + icon-192/512 + sw.js（PWA，可加桌面捷徑）

▣ portfolio-tracker / GitHub Pages 模式（無 server.py）
  [ ] update-prices.yml schedule 確認覆蓋目標市場時段（美股 13-21 UTC）
  [ ] 港股標的（.HK）在 silentRefreshPrices 中單獨做 yfFetch，不依賴 Actions 快照
  [ ] auto-refresh setInterval 回呼不可有 `if (!googleSheetUrl) return`（見 D3）
  [ ] HK / US market-open 偵測完全分離，各用各時區（見 D4）
  [ ] prevClose 一律用 meta.chartPreviousClose，不用 meta.previousClose（見 D6）
  [ ] yfFetch 節流：10s refresh × 每 6 次才打 Yahoo = 60s/次（見 D7）
  [ ] prevClose 備援 code 放在 _marketOpenStale block 之後（見 D8）
  [ ] 本機 push 前跑 git stash → pull --rebase → stash pop → push（見 I1）
  [ ] data/prices.json 只由 GitHub Actions 寫入，本機不 commit（見 I2）

▣ 部署前測試
  [ ] 殺掉 Python 後等 30 秒，watchdog 會自動復活
  [ ] 故意改壞 config → 確認 fast-fail cooldown 生效，不會無限刷屏
  [ ] 手機從 Tailscale IP 連 `http://100.x.x.x:3000` 可訪問
  [ ] 重開機後 startup 自動把 server 拉起來
"""


# ═══════════════════════════════════════════════════════════════════════════
#  9. 開機自動啟動 (Windows)
# ═══════════════════════════════════════════════════════════════════════════
AUTOSTART_NOTES = r'''
建議方案 A: Startup 資料夾 + VBS (靜音啟動)
  路徑: %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
  檔案: AutoStart.vbs
    Set oShell = CreateObject("WScript.Shell")
    oShell.Run Chr(34) & "J:\股市相關\start_server.bat" & Chr(34), 0, False
  (Chr(34) = 雙引號，避開 VBS 字串逸出)
  優點: 開機自動啟；bat 視窗完全隱藏。

建議方案 B: Task Scheduler (可重開機後延遲)
  用 schtasks 或 GUI 建立，Trigger: At log on, Delay: 30s
  Action: J:\股市相關\start_server.bat
  Settings: Do not start a new instance (避免雙開搶 port)

禁忌:
  - 不要同時放 Startup 資料夾 + Task Scheduler 兩個 → 會雙開搶 port。
  - 不要用「Run at startup」把 python.exe 直接當命令（沒有 watchdog，crash 不會重啟）。
'''


# ═══════════════════════════════════════════════════════════════════════════
#  10. 最小可運作範本 (複製就能跑)
# ═══════════════════════════════════════════════════════════════════════════
MINIMAL_SERVER_TEMPLATE = r'''
# server.py — 新專案最小範本
import os, sys, json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 把 skill.py 的專案根目錄加入 sys.path 以便 import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skill import (
    resolve_python, NWIN_FLAG, make_env,
    run_sdk_subprocess, TTLCache, build_watchdog,
    read_state, write_state,
)

DIR   = os.path.dirname(os.path.abspath(__file__))
BOT   = os.path.join(DIR, "bot")
STATE = os.path.join(DIR, "state.json")

positions_cache = TTLCache(ok_ttl=60)

class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/positions":
            cached = positions_cache.get("pos")
            if cached is not None:
                return self._json(200, cached)
            result = run_sdk_subprocess(
                os.path.join(BOT, "api_positions.py"), timeout=30,
            )
            positions_cache.set("pos", result, is_ok=(result.get("status") != "fail"))
            return self._json(200, result)
        return self._json(404, {"error": "not found"})

def main():
    loop_fn, _ = build_watchdog(
        name="quote",
        script=os.path.join(BOT, "quote_daemon.py"),
        check_alive=lambda: False,   # 簡化: 只信 Popen handle
        should_run=lambda: True,
        cwd=BOT,
    )
    threading.Thread(target=loop_fn, daemon=True).start()

    srv = ThreadingHTTPServer(("0.0.0.0", 3000), H)
    print(f"[server] PY = {resolve_python()}")
    print("[server] running on http://0.0.0.0:3000")
    srv.serve_forever()

if __name__ == "__main__":
    main()
'''


MINIMAL_BAT_TEMPLATE = (
    # 用 \r\n 確保 CRLF
    '@echo off\r\n'
    'title Server Watchdog\r\n'
    'set "PYTHON=C:\\Users\\User\\AppData\\Local\\Programs\\Python\\Python310\\python_stock.exe"\r\n'
    'if not exist "%PYTHON%" set "PYTHON=C:\\Users\\User\\AppData\\Local\\Programs\\Python\\Python310\\python.exe"\r\n'
    'if not exist "%PYTHON%" (\r\n'
    '    echo [ERROR] Python not found.\r\n'
    '    pause\r\n'
    '    exit /b 1\r\n'
    ')\r\n'
    'set "WORKDIR=%~dp0"\r\n'
    '\r\n'
    ':loop\r\n'
    'netstat -ano | findstr ":3000 " | findstr "LISTENING" >nul\r\n'
    'if %errorlevel% == 0 (\r\n'
    '    echo [%date% %time%] Port 3000 in use, waiting 10s...\r\n'
    '    timeout /t 10 /nobreak >nul\r\n'
    '    goto loop\r\n'
    ')\r\n'
    '\r\n'
    'echo [%date% %time%] Starting server.py with %PYTHON%\r\n'
    'cd /d "%WORKDIR%"\r\n'
    '"%PYTHON%" server.py\r\n'
    'echo [%date% %time%] server.py exited (code %errorlevel%), restarting in 5s...\r\n'
    'timeout /t 5 /nobreak >nul\r\n'
    'goto loop\r\n'
)


def dump_templates(target_dir: str) -> None:
    """
    把最小範本寫到新專案資料夾。bat 會用 CRLF 確保 CMD 正確解析。
    """
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(target_dir, "server.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(MINIMAL_SERVER_TEMPLATE.lstrip())
    bat_path = os.path.join(target_dir, "start_server.bat")
    # 用二進位寫確保 CRLF 不被改
    with open(bat_path, "wb") as f:
        f.write(MINIMAL_BAT_TEMPLATE.encode("ascii"))
    print(f"Wrote template to {target_dir}")


# ═══════════════════════════════════════════════════════════════════════════
#  11. GitHub Actions 資料管線 + 單檔前端架構 (portfolio-tracker 模式)
# ═══════════════════════════════════════════════════════════════════════════
GITHUB_ACTIONS_PIPELINE_NOTES = """
適用場景: 持倉追蹤、報價展示類前端，不需要下單，不需要 server.py。
          整個系統 = 一個 HTML 檔 + GitHub Actions 定時更新 JSON 快照。

┌──────────────────────────────────────────────────────────────────────────┐
│  GitHub Actions (ubuntu-latest)                                          │
│    update-prices.yml                                                     │
│      schedule: */5 13-21 * * 1-5  (UTC = 美股交易時段)                   │
│               */15 * * * 0,6      (週末全天 for BTC)                     │
│    steps:                                                                │
│      python data/update_prices.py  → data/prices.json                   │
│      git add data/prices.json && git commit && git push                  │
└──────────────────────────────────────────────────────────────────────────┘
         ↓ GitHub Pages 靜態伺服
┌──────────────────────────────────────────────────────────────────────────┐
│  portfolio-tracker-vN.html                                               │
│    init()                                                                │
│      └─ silentRefreshPrices()    讀 prices.json + Google Sheet + yfFetch │
│    startAutoRefresh()                                                    │
│      setInterval(10s)                                                    │
│        每次: silentRefreshPrices(skipProxyFetch)                          │
│             skip = (count % 6 !== 0)  → yfFetch 僅每 60s 一次           │
│    loadAnalysis() (手動觸發或靜默初始化)                                  │
│      └─ 設定 prevClose, tech 資料, marketOpenedToday                     │
└──────────────────────────────────────────────────────────────────────────┘

重點設計決策
─────────────
1. prices.json 是快照，不是即時來源
   - GitHub Actions 只在美股時段跑（13:00-21:00 UTC）
   - 港股（01:30-08:00 UTC）和收盤後 → prices.json 不更新
   - 前端必須自己對 proxy ETF / HK 股做 yfFetch 補強（D5）

2. 兩套 market-open 偵測必須完全分離（D4）
   港股時區: Asia/Hong_Kong，09:30-16:00 HKT = 01:30-08:00 UTC
   美股時區: America/New_York，09:30-16:00 ET = 13:30-20:00 UTC
   → 兩個時段完全不重疊，絕對不可互相當樣本

3. prevClose 資料優先級
   優先: prices.json prev_closes（GitHub Actions fetch_prev_close）
   次位: Yahoo Finance meta.chartPreviousClose（跨空白交易日準確）
   禁用: meta.previousClose（盤中可能返回現價而非昨收）

4. auto-refresh timer 不可 gate 在 UI 設定（D3）
   prices.json 必須獨立於 Google Sheet URL 重讀；
   Google Sheet 只是補充來源，未設定時自然跳過即可。

5. yfFetch 節流避免 Yahoo Finance 429（D7）
   auto-refresh 10s × 6 = 60s 才打一次 Yahoo；
   其餘 5 次只讀 prices.json + Google Sheet（輕量）。

關鍵資料流
──────────
  prices.json.prices         → h.currentPrice（現價快照）
  prices.json.prev_closes    → _prevCloseCache（昨收備援）
  prices.json.rates          → exchangeRates（USD/TWD、HKD/TWD）
  meta.regularMarketPrice    → yfFetch 即時報價（覆蓋快照）
  meta.chartPreviousClose    → yfFetch 昨收（最準，寫入 _prevCloseCache）

git push 流程（本機修改後）
──────────────────────────
  git stash
  git pull --rebase origin main   # 吸收 Actions 自動 commit 的 prices.json
  git stash pop
  git push origin main
"""

# ═══════════════════════════════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Trading platform skill / template generator")
    p.add_argument("--show", choices=["pitfalls", "checklist", "python", "pipeline"],
                   help="顯示內容")
    p.add_argument("--dump", metavar="DIR", help="把最小範本寫到指定資料夾")
    p.add_argument("--diagnose", action="store_true",
                   help="交易所 API 健檢 (時鐘 + VPN 出口 IP)")
    p.add_argument("--fix-clock", action="store_true",
                   help="修復 Windows 時鐘同步 (會彈 UAC)")
    args = p.parse_args()

    if args.show == "pitfalls":
        print(PITFALLS)
    elif args.show == "checklist":
        print(CHECKLIST)
    elif args.show == "python":
        print(f"resolved python = {resolve_python()}")
    elif args.show == "pipeline":
        print(GITHUB_ACTIONS_PIPELINE_NOTES)
    elif args.dump:
        dump_templates(args.dump)
    elif args.diagnose:
        print(f"[diagnose] python = {_PY_VPN_DEFAULT}")
        result = diagnose_exchange_api()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        for step in result["next_steps"]:
            print(f"  → {step}")
    elif args.fix_clock:
        print(json.dumps(fix_clock_sync(), ensure_ascii=False, indent=2))
    else:
        print(f"Trading Platform Skill v{SKILL_VERSION}")
        print("Usage:")
        print("  python skill.py --show pitfalls   # 看踩過的坑")
        print("  python skill.py --show checklist  # 新專案檢查清單")
        print("  python skill.py --show python     # 看會用到哪個 python")
        print("  python skill.py --dump NEW_DIR    # 寫出最小可運作範本")
        print("  python skill.py --diagnose        # 交易所 API 健檢 (時鐘+VPN)")
        print("  python skill.py --fix-clock       # 修時鐘同步 (彈 UAC)")
        print("  python skill.py --show pipeline   # GitHub Actions 資料管線架構")
