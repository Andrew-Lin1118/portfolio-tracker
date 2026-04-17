#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FMX 微台 動態槓桿策略 — 富邦期貨實單執行程式
═══════════════════════════════════════════════════════════
策略規則：
  1. 初始建倉  : 用目標槓桿買入微台 (MXFR1)
  2. DCA 攤平  : 從上次買點跌超過 5%，加買 1 口（槓桿 < 上限）
  3. 動態加碼  : 槓桿 < 下限 且 在 200EMA 之上，補回目標槓桿
  4. 獲利降槓桿: 價格高於均攤成本 且 槓桿 > 目標值，賣至目標槓桿

使用方式:
  python strategy-fmx-live.py              # 正式下單（dry_run 依 config）
  python strategy-fmx-live.py --dry-run    # 強制模擬，不下單
  python strategy-fmx-live.py --status     # 只顯示目前倉位
  python strategy-fmx-live.py --reset      # 重置倉位，重新建倉
  python strategy-fmx-live.py --once       # 執行一次後離開（適合排程器）

⚠️  注意事項：
  - 預設 dry_run: true，需在 config.yaml 改為 false 才會真實下單
  - 期貨帳號須在 config.yaml 的 futures_account 欄位填寫
  - 維持保證金 (maint_margin) 請依富邦最新公告調整
═══════════════════════════════════════════════════════════
"""

import os, sys, time, math, json, logging, argparse, datetime, traceback
import urllib.request

import yaml
import yfinance as yf
import pandas as pd

from fubon_neo.sdk import FubonSDK, Order, FutOptOrder
from fubon_neo.constant import (
    BSAction, MarketType, PriceType, TimeInForce, OrderType,
    FutOptOrderType, FutOptMarketType, FutOptPriceType,
)

# ── 台指期貨交易時段 ────────────────────────────────────────────────────────
FUT_OPEN_DAY   = datetime.time(8, 45)   # 日盤開盤
FUT_CLOSE_DAY  = datetime.time(13, 45)  # 日盤收盤
FUT_OPEN_NIGHT = datetime.time(15, 0)   # 夜盤開盤
FUT_CLOSE_NIGHT= datetime.time(5, 0)    # 夜盤收盤（隔日凌晨）

# ── 路徑 ────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
# STATE_FILE 放在上層 (J:\股市相關\)，讓 server.py 可直接讀取
STATE_FILE  = os.path.join(BASE_DIR, "..", "fmx_state.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")


# ════════════════════════════════════════════════════════════════════════════
class FMXLiveBot:
    """FMX 微台 動態槓桿策略 — 富邦期貨實單執行"""

    # ── 初始化 ───────────────────────────────────────────────────────────────
    def __init__(self, config_path: str, force_dry_run: bool = False):
        with open(config_path, encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)

        self.dry_run = force_dry_run or self.cfg.get("dry_run", True)

        # 策略參數
        self.initial_capital  = float(self.cfg.get("initial_capital",  500_000))
        self.multiplier       = 10          # FMX 微台：每點 10 NTD
        self.target_leverage  = float(self.cfg.get("target_leverage",  5.0))
        self.dca_threshold    = float(self.cfg.get("dca_threshold_pct", 5.0))
        self.max_leverage     = float(self.cfg.get("max_leverage",      6.0))
        self.pyramid_trigger  = float(self.cfg.get("pyramid_trigger",   5.0))
        self.fee_per_lot      = float(self.cfg.get("fee_per_lot",       50))
        self.maint_margin     = float(self.cfg.get("maint_margin",      22_000))
        self.symbol           = self.cfg.get("futures_symbol", "MXFR1")
        self.ema_period       = 200

        # SDK
        self.sdk: FubonSDK | None = None
        self.account = None

        # Logging
        log_file = self.cfg.get("fmx_log_file", "logs/fmx_live.log")
        log_file = os.path.join(BASE_DIR, log_file)
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fmt = "%(asctime)s [%(levelname)s] %(message)s"
        logging.basicConfig(
            level=logging.INFO, format=fmt,
            handlers=[
                logging.FileHandler(log_file, encoding="utf-8"),
                logging.StreamHandler(sys.stdout),
            ],
        )
        self.log = logging.getLogger("FMXLive")

        mode = "【模擬模式 — 不會實際下單】" if self.dry_run else "【⚡ 實際下單模式 ⚡】"
        self.log.info("=" * 62)
        self.log.info(f"  FMX 微台動態槓桿策略執行程式  {mode}")
        self.log.info(f"  帳號:{self.cfg.get('futures_account','?')}  標的:{self.symbol}")
        self.log.info(
            f"  目標槓桿:{self.target_leverage}x  DCA:-{self.dca_threshold}%  "
            f"上限:{self.max_leverage}x  加碼觸發:{self.pyramid_trigger}x"
        )
        self.log.info("=" * 62)

    # ── 登入 ─────────────────────────────────────────────────────────────────
    def login(self) -> None:
        self.sdk = FubonSDK()
        result = self.sdk.apikey_login(
            self.cfg["personal_id"],
            self.cfg["api_key"],
            self.cfg["cert_path"],
            self.cfg["cert_pass"],
        )
        if not result.is_success:
            raise RuntimeError(f"登入失敗: {result.message}")

        # 尋找期貨帳號
        target_acc = str(self.cfg.get("futures_account", "")).replace("-", "").strip()
        for acc in result.data:
            acc_type = getattr(acc, "account_type", "").lower()
            acc_no   = str(getattr(acc, "account", "")).replace("-", "").strip()
            # 期貨帳號 type 為 'futopt' 或 'future'
            if acc_type in ("futopt", "future", "futures"):
                if not target_acc or target_acc in acc_no or acc_no in target_acc:
                    self.account = acc
                    break

        if not self.account:
            # fallback: 列出所有帳號幫助排查
            all_acc = [(getattr(a, "account_type","?"), getattr(a, "account","?"))
                       for a in result.data]
            raise RuntimeError(
                f"找不到期貨帳號 '{target_acc}'。\n"
                f"登入後可見帳號: {all_acc}\n"
                f"請確認 config.yaml 的 futures_account 欄位。"
            )

        name = getattr(self.account, "name", "?")
        acc  = getattr(self.account, "account", "?")
        self.log.info(f"登入成功 ▶ {name} / 期貨帳號:{acc}")

        # ── 一次性診斷：列出 SDK 可用方法 ──────────────────────────────────
        try:
            futopt_methods = [m for m in dir(self.sdk.futopt)
                              if not m.startswith("_")]
            self.log.info(f"  sdk.futopt 方法: {futopt_methods}")
        except Exception as e:
            self.log.warning(f"  sdk.futopt dir() 失敗: {e}")
        try:
            acct_methods = [m for m in dir(self.sdk.futopt_accounting)
                            if not m.startswith("_")]
            self.log.info(f"  sdk.futopt_accounting 方法: {acct_methods}")
        except Exception as e:
            self.log.warning(f"  sdk.futopt_accounting dir() 失敗: {e}")
        try:
            mt_members = [m for m in dir(FutOptMarketType) if not m.startswith("_")]
            self.log.info(f"  FutOptMarketType 成員: {mt_members}")
        except Exception as e:
            self.log.warning(f"  FutOptMarketType dir() 失敗: {e}")

    @staticmethod
    def _third_wednesday(year: int, month: int) -> datetime.date:
        """計算指定月份的第三個星期三（TAIFEX 到期日）"""
        first = datetime.date(year, month, 1)
        # weekday(): Mon=0 … Wed=2 … Sun=6
        days_to_wed = (2 - first.weekday()) % 7
        first_wed   = first + datetime.timedelta(days=days_to_wed)
        return first_wed + datetime.timedelta(weeks=2)

    def _resolve_order_symbol(self) -> str:
        """
        取得 Fubon 實際下單代碼。
        優先順序：
        0. 持倉到期日（expiry_date），最準確
        1. query_hybrid_position 回傳的 _actual_order_symbol（需以數字結尾）
        2. config.yaml 的 order_symbol（手動指定）
        3. 月份代碼自動轉換（MXFR1 → 當前近月），含近月到期自動滾倉
        """
        month_codes = {1:'F',2:'G',3:'H',4:'J',5:'K',6:'M',
                       7:'N',8:'Q',9:'U',10:'V',11:'X',12:'Z'}
        sym    = self.symbol
        prefix = sym[:-2] if (sym.endswith("R1") or sym.endswith("R2")) else sym[:3]

        # 0. 由到期日推算合約代碼（最可靠）
        expiry = getattr(self, "_position_expiry", "").strip()
        if expiry:
            try:
                ec = expiry.replace("/", "").replace("-", "")
                year  = int(ec[:4])
                month = int(ec[4:6])
                code  = month_codes.get(month, 'J')
                result = f"{prefix}{code}{year % 10}"
                self.log.info(f"  代碼（到期日 {expiry}）: {result}  ({year}年{month}月合約)")
                return result
            except Exception as e:
                self.log.warning(f"  到期日 {expiry!r} 解析失敗: {e}")

        # 1. get_future_quote() 已取得代碼，但需驗證格式（必須以數字結尾）
        actual = getattr(self, "_actual_order_symbol", "").strip()
        if actual and actual[-1].isdigit():
            self.log.info(f"  下單代碼: {actual}（來自持倉查詢）")
            return actual
        elif actual:
            self.log.warning(f"  持倉代碼 {actual!r} 格式不符（缺年份），嘗試從尾字推算月份")
            # 例：FITM → 尾字 M = TAIFEX 6月代碼 → MXFM6
            tail = actual[-1].upper()
            code_to_month = {'F':1,'G':2,'H':3,'J':4,'K':5,'M':6,
                             'N':7,'Q':8,'U':9,'V':10,'X':11,'Z':12}
            if tail in code_to_month:
                now    = datetime.datetime.now()
                m_num  = code_to_month[tail]
                yr     = now.year
                # 若推算月份已過期（< 本月），試下一年
                if m_num < now.month:
                    yr += 1
                result = f"{prefix}{tail}{yr % 10}"
                self.log.info(f"  由尾碼 {tail!r} 推算: {actual} → {result} ({yr}年{m_num}月)")
                return result
            self.log.warning(f"  尾碼 {tail!r} 無法辨識，改用月份代碼轉換")

        # 2. config.yaml 手動指定
        explicit = self.cfg.get("order_symbol", "").strip()
        if explicit:
            self.log.info(f"  下單代碼: {explicit}（來自 config.yaml）")
            return explicit

        # 3. 月份代碼轉換（MXFR1 → 近月合約），含近月到期自動滾倉
        if not sym.endswith("R1") and not sym.endswith("R2"):
            return sym
        now   = datetime.datetime.now()
        year  = now.year
        month = now.month
        # 若今天 >= 本月第三個星期三（到期日），滾到下個月合約
        exp_this = self._third_wednesday(year, month)
        if now.date() >= exp_this:
            month += 1
            if month > 12:
                month = 1
                year += 1
            self.log.info(f"  近月到期（{exp_this}），自動滾倉至下月")
        code   = month_codes.get(month, 'J')
        result = f"{prefix}{code}{year % 10}"
        self.log.info(f"  代碼轉換: {sym} → {result} ({year}年{month}月合約, 到期:{exp_this})")
        return result

    def _ensure_login(self) -> None:
        if self.sdk is None or self.account is None:
            self.login()

    # ── 報價 ─────────────────────────────────────────────────────────────────
    def get_future_quote(self) -> dict | None:
        """
        取得微台即時報價。
        主要來源: sdk.futopt_accounting.query_hybrid_position（同時回傳 market_price 和實際代碼）
        回退來源: fmx_state.json 最後已知價格
        """
        # ── 主要：用持倉查詢取得 market_price ───────────────────────
        try:
            hp = self.sdk.futopt_accounting.query_hybrid_position(self.account)
            if hp.is_success and hp.data:
                positions = hp.data if isinstance(hp.data, list) else [hp.data]
                for p in positions:
                    sym = str(getattr(p, "symbol", "") or "")
                    market_px = getattr(p, "market_price", None)
                    avg_px    = getattr(p, "price", None)

                    # ── 診斷：印出全部欄位（找到 expiry_date）─────────────
                    try:
                        all_fields = {
                            a: str(getattr(p, a, ""))[:60]
                            for a in dir(p)
                            if not a.startswith("_") and not callable(getattr(p, a, None))
                        }
                        self.log.info(f"  持倉全欄位 [{sym}]: {all_fields}")
                    except Exception:
                        pass

                    if market_px is not None:
                        last = float(str(market_px).replace(",", ""))
                        if last > 0:
                            lots = int(getattr(p, "tradable_lot", 0) or 0)

                            # ── 捕捉到期日（供 _resolve_order_symbol 使用）──
                            expiry = (
                                getattr(p, "expiry_date",     None) or
                                getattr(p, "contract_month",  None) or
                                getattr(p, "maturity_date",   None) or
                                getattr(p, "maturity",        None) or
                                getattr(p, "delivery_date",   None)
                            )
                            if expiry:
                                self._position_expiry = str(expiry).strip()
                                self.log.info(f"  持倉到期日: {self._position_expiry}")

                            self.log.info(
                                f"query_hybrid_position: {sym} lots={lots} "
                                f"market={last}  avg={avg_px}  expiry={expiry}"
                            )
                            # 記錄實際代碼供下單用
                            if sym and sym not in ("", "N/A"):
                                self._actual_order_symbol = sym
                            return {
                                "last": last,
                                "bid":  None,
                                "ask":  None,
                                "ref":  float(avg_px) if avg_px else None,
                                "lots": lots,
                            }
        except Exception as e:
            self.log.warning(f"query_hybrid_position 例外: {type(e).__name__}: {e}")

        # ── 回退：fmx_state.json 最後已知價格 ────────────────────────
        try:
            state = self.load_state()
            last = state.get("last_price") or state.get("avg_cost")
            if last:
                self.log.warning(f"使用 fmx_state.json 回退價格: {last}")
                return {"last": float(last), "bid": None, "ask": None, "ref": None}
        except Exception:
            pass

        self.log.error("無法取得任何報價")
        return None

    # ── 倉位查詢 ─────────────────────────────────────────────────────────────
    def get_future_position(self) -> int:
        """查詢目前持有微台口數（多頭淨口數）"""
        try:
            r = self.sdk.futopt.inventories(self.account)
            if r.is_success:
                for inv in r.data:
                    sym = getattr(inv, "stock_no", "") or getattr(inv, "symbol", "")
                    if self.symbol.startswith("MXF") and "MXF" in sym.upper():
                        # 取多頭可用口數
                        qty = getattr(inv, "tradable_qty", None) or getattr(inv, "net_qty", 0)
                        return int(qty)
        except AttributeError:
            try:
                r = self.sdk.accounting.inventories(self.account)
                if r.is_success:
                    for inv in r.data:
                        if self.symbol[:3] in getattr(inv, "stock_no", ""):
                            return int(inv.tradable_qty)
            except Exception as e:
                self.log.error(f"倉位查詢失敗: {e}")
        except Exception as e:
            self.log.error(f"倉位查詢失敗: {e}")
        return 0

    # ── 下單 ─────────────────────────────────────────────────────────────────
    def place_future_order(
        self, side: str, contracts: int, price: float,
        order_type: str = "limit", spread: float = 1.0
    ) -> bool:
        """
        送出期貨委託。
        side       : 'buy' | 'sell'
        contracts  : 口數
        price      : 參考現價
        order_type : 'market' (市價) | 'limit' (限價，預設)
        spread     : 限價單超出市價的點數（buy+spread, sell-spread），預設 1 點
        """
        if contracts <= 0:
            return False

        if side == "buy":
            buy_sell    = BSAction.Buy
            limit_price = price + spread
        else:
            buy_sell    = BSAction.Sell
            limit_price = price - spread

        is_market = order_type == "market"

        if self.dry_run:
            order_desc = "市價" if is_market else f"限價 {limit_price:.0f}"
            self.log.info(
                f"  [模擬] {side.upper()} {self.symbol} "
                f"@ {order_desc}  × {contracts} 口"
            )
            return True

        # ── 決定實際下單代碼 ──────────────────────────────────────────
        # Fubon API 使用數字滾動代碼：MXF0=近月, MXF1=次月（非 TAIFEX 月份字母）
        # 優先嘗試 MXF0，若失敗再嘗試 MXFJ6 等其他格式
        order_symbol = self._resolve_order_symbol()
        self.log.info(f"  下單代碼: {order_symbol}（設定: {self.symbol}）")

        # ── 判斷日盤 / 夜盤 ──────────────────────────────────────────────
        now_t = datetime.datetime.now().time()
        is_night = now_t >= datetime.time(15, 0) or now_t <= datetime.time(5, 0)
        fut_market = FutOptMarketType.FutureNight if is_night else FutOptMarketType.Future
        self.log.info(f"  交易時段: {'夜盤' if is_night else '日盤'} ({fut_market})")

        # ── 開/平倉類型 ───────────────────────────────────────────────────
        # buy = 加槓桿 → 開新倉(New)；sell = 降槓桿 → 平倉(Close)
        fut_order_type = FutOptOrderType.Close if side == "sell" else FutOptOrderType.New

        # ── convert_symbol：持倉代碼 → TAIMEX 實際下單代碼 ─────────────────
        # 正確簽名（已驗證）: convert_symbol(stock_symbol: str, expiry_date: str)
        # 無 account 參數！
        # 輸入必須是 TAIMEX 持倉代碼（如 'FITM'），NOT TAIFEX 格式（MXFJ6 會 ValueError）
        # 輸出：如 'TMFD6'（TAIMEX夜盤合約代碼，TMF+D=Apr+6=2026）
        expiry_str = getattr(self, "_position_expiry", "").replace("/", "").replace("-", "")
        if not expiry_str:
            # 自動計算當前近月（與 _resolve_order_symbol 相同邏輯）
            now = datetime.datetime.now()
            yr, mo = now.year, now.month
            exp_this = self._third_wednesday(yr, mo)
            if now.date() >= exp_this:
                mo += 1
                if mo > 12:
                    mo = 1; yr += 1
            expiry_str = f"{yr}{mo:02d}"
            self.log.info(f"  到期日未知，自動計算近月: {expiry_str}")

        # 持倉代碼：FITM（來自 query_hybrid_position 的 symbol 欄位）
        pos_symbol = getattr(self, "_actual_order_symbol", "FITM") or "FITM"

        converted_sym = None
        for expiry_fmt in [expiry_str, expiry_str[:6]]:
            # 優先用 pos_symbol('FITM')，再試其他格式
            for cs_stock in [pos_symbol, pos_symbol[:3], "FITM", order_symbol]:
                cs_args = (cs_stock, expiry_fmt)
                try:
                    cs_result = self.sdk.futopt.convert_symbol(*cs_args)
                    self.log.info(f"  convert_symbol{cs_args!r} → {cs_result!r}")
                    if cs_result is None:
                        continue
                    if hasattr(cs_result, "is_success"):
                        if cs_result.is_success and cs_result.data:
                            converted_sym = str(cs_result.data).strip()
                            self.log.info(f"  ✅ TAIMEX代碼: {converted_sym}")
                            break
                        else:
                            self.log.warning(f"    convert_symbol 失敗: {getattr(cs_result,'message','')}")
                    else:
                        s = str(cs_result).strip()
                        if s and len(s) >= 3:
                            converted_sym = s
                            self.log.info(f"  ✅ TAIMEX代碼: {converted_sym}")
                            break
                except TypeError as e:
                    self.log.warning(f"  convert_symbol{cs_args!r} 參數錯誤: {e}")
                except Exception as e:
                    self.log.warning(f"  convert_symbol{cs_args!r} 失敗: {type(e).__name__}: {e}")
            if converted_sym:
                break

        # ── 路徑 A：close_position_record（用 order_no 直接平倉）──────────
        # futopt_accounting.close_position_record 可能支援傳入持倉物件來平倉
        if side == "sell":
            try:
                sp = self.sdk.futopt_accounting.query_single_position(self.account)
                if sp.is_success and sp.data:
                    sp_list = sp.data if isinstance(sp.data, list) else [sp.data]
                    # 取第一口可平倉的 lot
                    first_lot = next(
                        (p for p in sp_list if int(getattr(p, "tradable_lot", 0) or 0) > 0),
                        None
                    )
                    if first_lot:
                        order_no = getattr(first_lot, "order_no", None)
                        self.log.info(f"  [路徑A] close_position_record 嘗試 order_no={order_no}")
                        for cpr_args in [
                            (self.account, first_lot),
                            (self.account, order_no),
                            (first_lot,),
                        ]:
                            try:
                                cpr = self.sdk.futopt_accounting.close_position_record(*cpr_args)
                                self.log.info(f"    close_position_record{[str(a)[:40] for a in cpr_args]} → is_success={getattr(cpr,'is_success','?')} msg={getattr(cpr,'message','')}")
                                if hasattr(cpr, "is_success") and cpr.is_success:
                                    self.log.info("  ✅ [路徑A] close_position_record 成功！")
                                    return True
                            except TypeError as e:
                                self.log.info(f"    close_position_record TypeError: {e}")
                            except Exception as e:
                                self.log.info(f"    close_position_record {type(e).__name__}: {e}")
            except Exception as e:
                self.log.warning(f"  [路徑A] close_position_record 流程失敗: {type(e).__name__}: {e}")

        # ── 路徑 B：query_estimate_margin 診斷（估算委託保證金）────────────
        try:
            test_order = FutOptOrder(
                buy_sell=buy_sell, symbol=order_symbol, lot=int(contracts),
                market_type=fut_market, price_type=FutOptPriceType.Limit,
                time_in_force=TimeInForce.ROD, order_type=fut_order_type,
                price=f"{int(round(price))}",
            )
            em = self.sdk.futopt.query_estimate_margin(self.account, test_order)
            self.log.info(f"  query_estimate_margin: is_success={getattr(em,'is_success','?')} msg={getattr(em,'message','?')} data={str(getattr(em,'data',''))[:80]}")
        except Exception as e:
            self.log.info(f"  query_estimate_margin 失敗: {type(e).__name__}: {e}")

        # ── 建立委託候選序列（依序嘗試，直到成功） ─────────────────────────
        cur_price_str  = f"{int(round(price))}"
        lim_price_str  = f"{int(round(limit_price))}"
        # 也試浮點格式（部分 TAIMEX 端點可能需要）
        cur_price_flt  = f"{float(price):.1f}"

        # 安全取得 FutOptMarketType.Future
        market_types = [fut_market]
        try:
            _day_market = FutOptMarketType.Future
            if _day_market != fut_market:
                market_types.append(_day_market)
        except AttributeError:
            pass
        self.log.info(f"  市場類型: {[str(m).split('.')[-1] for m in market_types]}")

        # sym_list: 轉換代碼優先，再嘗試 TAIFEX 格式
        sym_list = []
        if converted_sym and converted_sym not in (order_symbol, ""):
            sym_list.append(converted_sym)
        sym_list.append(order_symbol)
        # 若持倉顯示 FITM，也納入嘗試（它在 close_position 方法中可能有效）
        if "FITM" not in sym_list:
            sym_list.append("FITM")
        self.log.info(f"  下單代碼候選: {sym_list}")

        candidates = []
        try:
            # ① 限價單（夜盤 TAIMEX 不支援市價，限價最穩）
            #    轉換代碼（TMFD6）優先，次為 TAIFEX 格式，最後 FITM
            for sym in sym_list:
                for mt in market_types:
                    for ot in [fut_order_type, FutOptOrderType.Auto]:
                        for px in [cur_price_str, lim_price_str]:
                            candidates.append(dict(
                                sym=sym, mt=mt, pt=FutOptPriceType.Limit,
                                tif=TimeInForce.ROD, ot=ot, px=px,
                            ))
            # ② 市價單（日盤 TAIFEX 支援；夜盤會回 "Price should be empty" 可跳過）
            if not is_night:
                for sym in sym_list:
                    for mt in market_types:
                        for ot in [fut_order_type, FutOptOrderType.Auto]:
                            for tif in [TimeInForce.IOC, TimeInForce.ROD]:
                                candidates.append(dict(
                                    sym=sym, mt=mt, pt=FutOptPriceType.Market,
                                    tif=tif, ot=ot, px="",
                                ))
            self.log.info(f"  候選總數={len(candidates)}  (夜盤={is_night})")
        except Exception as e:
            self.log.error(f"  建立候選序列失敗: {type(e).__name__}: {e}")
            self.log.error(traceback.format_exc())
            return False

        r = None
        last_msg = ""
        for idx, params in enumerate(candidates):
            mt_label  = str(params["mt"]).split(".")[-1]
            pt_label  = str(params["pt"]).split(".")[-1]
            tif_label = str(params["tif"]).split(".")[-1]
            ot_label  = str(params["ot"]).split(".")[-1]
            px_label  = params["px"] or "(空)"
            sym_label = params["sym"]
            self.log.info(
                f"  [嘗試 {idx+1}/{len(candidates)}] "
                f"sym={sym_label}  Market={mt_label}  PT={pt_label}  "
                f"price={px_label}  OT={ot_label}  TIF={tif_label}"
            )
            try:
                order = FutOptOrder(
                    buy_sell      = buy_sell,
                    symbol        = params["sym"],
                    lot           = int(contracts),
                    market_type   = params["mt"],
                    price_type    = params["pt"],
                    time_in_force = params["tif"],
                    order_type    = params["ot"],
                    price         = params["px"],
                )
            except Exception as e:
                self.log.warning(f"    FutOptOrder 建立失敗: {type(e).__name__}: {e}")
                continue

            try:
                r = self.sdk.futopt.place_order(self.account, order)
                msg = getattr(r, "message", "") or ""
                last_msg = msg
                self.log.info(f"    → is_success={r.is_success} msg={msg}")
                if r.is_success:
                    break
                fatal_keywords = ["帳號", "account", "登入", "login"]
                if any(k in msg.lower() for k in fatal_keywords):
                    self.log.warning(f"    致命錯誤，停止: {msg}")
                    break
            except Exception as e:
                self.log.error(f"    place_order 例外: {type(e).__name__}: {e}")
                r = None

        if r is None:
            self.log.error("  [❌ 所有組合均失敗]")
            return False

        if r.is_success:
            ono = getattr(r.data, "order_no", "N/A") if r.data else "N/A"
            order_desc = "市價" if is_market else f"限價 {limit_price:.0f}"
            self.log.info(
                f"  [✅ 委託成功] {side.upper()} {order_symbol} "
                f"@ {order_desc} × {contracts}口  委託單號:{ono}"
            )
            return True
        else:
            self.log.error(f"  [❌ 委託失敗] {last_msg}")
            return False

    def manual_adjust(
        self, side: str, lots: int = 1,
        order_type: str = "limit", spread: float = 5.0
    ) -> None:
        """
        手動加/減槓桿：直接下指定口數的單。
        side: 'buy' (加槓桿) | 'sell' (降槓桿)
        """
        self._ensure_login()

        # 取即時報價
        q = self.get_future_quote()
        if q is None or q.get("last") is None:
            self.log.error("無法取得即時報價，取消下單")
            return

        price = float(q["last"])

        self.log.info(
            f"{'⬆ 手動加槓桿' if side=='buy' else '⬇ 手動降槓桿'}：{lots} 口  "
            f"現價 {price:.0f}  委託類型 {order_type}  spread ±{spread}"
        )

        success = self.place_future_order(side, lots, price, order_type=order_type, spread=spread)

        if success:
            state = self.load_state()
            if state.get("initialized"):
                if side == "buy":
                    signal = {"action": "MANUAL_BUY",  "qty":  lots}
                else:
                    signal = {"action": "MANUAL_SELL", "qty": -lots}
                new_state = self.apply_signal(signal, price, state)
                new_st    = self._recalc(price, new_state)
                self.save_state(new_state, runtime={
                    "last_price":  price,
                    "leverage":    new_st["leverage"],
                    "equity":      new_st["equity"],
                    "unrealized":  new_st["unrealized"],
                    "liq_price":   new_st["liq_price"],
                    "last_signal": "MANUAL_BUY" if side == "buy" else "MANUAL_SELL",
                    "last_reason": f"手動{'加' if side=='buy' else '降'}槓桿 {lots} 口 @ {price:.0f}",
                })
                self.log.info(
                    f"  執行後：{new_state['contracts']} 口  "
                    f"均攤:{new_state['avg_cost']:.0f}  "
                    f"槓桿:{new_st['leverage']:.2f}x"
                )

    def manual_rollover(
        self, lots: int | None = None,
        order_type: str = "limit", spread: float = 5.0
    ) -> None:
        """
        轉倉：將近月倉位全數或指定口數平倉，並以相同口數建立次月倉位。
        lots      : 轉倉口數，None = 全部持倉
        order_type: 'limit' | 'market'
        spread    : 限價偏移點數
        """
        self._ensure_login()

        # ── 取即時報價與持倉月份 ──────────────────────────────────────────
        q = self.get_future_quote()
        if q is None or q.get("last") is None:
            self.log.error("轉倉失敗：無法取得即時報價")
            return

        price = float(q["last"])
        state = self.load_state()
        cur_lots = lots if lots is not None else int(state.get("contracts", 0))
        if cur_lots <= 0:
            self.log.error(f"轉倉失敗：持倉口數 {cur_lots}，無法轉倉")
            return

        # ── 計算近月 / 次月到期日 ──────────────────────────────────────────
        month_codes = {1:'F',2:'G',3:'H',4:'J',5:'K',6:'M',
                       7:'N',8:'Q',9:'U',10:'V',11:'X',12:'Z'}

        # 近月：從 _position_expiry 或自動計算
        cur_expiry = getattr(self, "_position_expiry", "").replace("/", "").replace("-", "")
        if cur_expiry and len(cur_expiry) >= 6:
            near_yr  = int(cur_expiry[:4])
            near_mo  = int(cur_expiry[4:6])
        else:
            now      = datetime.datetime.now()
            near_yr  = now.year
            near_mo  = now.month
            exp_this = self._third_wednesday(near_yr, near_mo)
            if now.date() >= exp_this:
                near_mo += 1
                if near_mo > 12: near_mo = 1; near_yr += 1
        near_expiry_str = f"{near_yr}{near_mo:02d}"

        # 次月
        next_mo = near_mo + 1
        next_yr = near_yr
        if next_mo > 12: next_mo = 1; next_yr += 1
        next_expiry_str = f"{next_yr}{next_mo:02d}"
        near_code = month_codes.get(near_mo, 'J')
        next_code = month_codes.get(next_mo, 'K')
        sym_prefix = self.symbol[:-2] if (self.symbol.endswith("R1") or self.symbol.endswith("R2")) else self.symbol[:3]
        near_sym = f"{sym_prefix}{near_code}{near_yr % 10}"
        next_sym = f"{sym_prefix}{next_code}{next_yr % 10}"

        self.log.info(
            f"🔄 轉倉 {cur_lots} 口：{near_sym}（{near_expiry_str}）"
            f" → {next_sym}（{next_expiry_str}）  現價:{price:.0f}  委託:{order_type}"
        )

        if self.dry_run:
            sp = f"限價 ±{spread}" if order_type == "limit" else "市價"
            self.log.info(
                f"  [模擬] SELL {near_sym} × {cur_lots} 口  {sp}\n"
                f"  [模擬] BUY  {next_sym} × {cur_lots} 口  {sp}"
            )
            # 模擬更新狀態：expiry 改為次月
            state["last_updated"] = str(datetime.date.today())
            self.save_state(state, runtime={
                "last_price":  price,
                "last_signal": "ROLLOVER",
                "last_reason": f"[模擬] 轉倉 {cur_lots}口 {near_expiry_str}→{next_expiry_str}",
            })
            return

        # ── Step 1：平近月倉 ──────────────────────────────────────────────
        # 暫存近月到期日，供 place_future_order → _resolve_order_symbol 使用
        saved_expiry = getattr(self, "_position_expiry", "")
        self._position_expiry = near_expiry_str
        self.log.info(f"  Step 1: 平近月 {near_sym} {cur_lots} 口...")
        sell_ok = self.place_future_order("sell", cur_lots, price, order_type=order_type, spread=spread)

        if not sell_ok:
            self.log.error("  近月平倉失敗，轉倉中止（不開次月新倉）")
            self._position_expiry = saved_expiry
            return

        # 近月平倉損益
        avg_cost = float(state.get("avg_cost", price))
        roll_pnl = cur_lots * (price - avg_cost) * self.multiplier - cur_lots * self.fee_per_lot
        self.log.info(f"  近月平倉完成，損益估算: {roll_pnl:+.0f}")

        # ── Step 2：開次月新倉 ────────────────────────────────────────────
        self._position_expiry = next_expiry_str
        self.log.info(f"  Step 2: 開次月 {next_sym} {cur_lots} 口...")
        buy_ok = self.place_future_order("buy", cur_lots, price, order_type=order_type, spread=spread)

        if not buy_ok:
            self.log.error("  次月開倉失敗！倉位已平但未重建，請手動處理！")
            state["contracts"]    = 0
            state["realized_pnl"] = float(state.get("realized_pnl", 0)) + roll_pnl
            state["last_updated"] = str(datetime.date.today())
            self.save_state(state, runtime={
                "last_price":  price,
                "last_signal": "ROLLOVER_PARTIAL",
                "last_reason": f"次月開倉失敗，近月已平倉！請立即手動補倉 {cur_lots} 口 {next_expiry_str}",
            })
            self._position_expiry = next_expiry_str  # 保持次月，方便後續補倉
            return

        # ── Step 3：更新狀態 ──────────────────────────────────────────────
        # 轉倉：口數不變，均攤成本以次月當前價重設，已實現加入買賣差損益
        buy_fee  = cur_lots * self.fee_per_lot
        state["realized_pnl"] = float(state.get("realized_pnl", 0)) + roll_pnl - buy_fee
        state["avg_cost"]     = price          # 以轉倉時的成交價為新均攤
        state["last_buy_px"]  = price
        state["last_updated"] = str(datetime.date.today())
        new_st = self._recalc(price, state)
        self.save_state(state, runtime={
            "last_price":  price,
            "leverage":    new_st["leverage"],
            "equity":      new_st["equity"],
            "unrealized":  new_st["unrealized"],
            "liq_price":   new_st["liq_price"],
            "last_signal": "ROLLOVER",
            "last_reason": f"轉倉完成 {cur_lots}口 {near_expiry_str}→{next_expiry_str} @ {price:.0f}",
        })
        self.log.info(
            f"  ✅ 轉倉完成！{cur_lots}口 {near_expiry_str} → {next_expiry_str}"
            f"  新均攤:{price:.0f}  槓桿:{new_st['leverage']:.2f}x"
        )

    # ── Yahoo Finance 台指歷史 + EMA ────────────────────────────────────────
    def fetch_twii_with_ema(self) -> dict | None:
        """
        回傳包含以下欄位的 dict（失敗回傳 None）：
          close, ema200, rolling_high_10, rolling_low_60
        """
        try:
            self.log.info("正在從 Yahoo Finance 取得 ^TWII 歷史資料...")
            df = yf.Ticker("^TWII").history(period="15mo", interval="1d")[["Close","High","Low"]].dropna()
            if len(df) < 210:
                self.log.warning("歷史資料不足，無法計算 200EMA")
                return None
            df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()
            # 滾動高低（前 N 日，不含當日）
            # 使用 High/Low 實際高低點，分K只要觸及就立即觸發
            high_p = int(self.cfg.get("high_period", 10))
            low_p  = int(self.cfg.get("low_period",  100))
            df["RH"] = df["High"].shift(1).rolling(high_p).max()   # 前N日最高點
            df["RL"] = df["Low"].shift(1).rolling(low_p).min()     # 前M日最低點
            last = df.iloc[-1]
            above = "年線之上" if last["Close"] > last["EMA200"] else "年線之下"
            self.log.info(
                f"台指收盤:{last['Close']:.0f}  200EMA:{last['EMA200']:.0f}({above})  "
                f"{high_p}日最高點:{last['RH']:.0f}  {low_p}日最低點:{last['RL']:.0f}"
            )
            return {
                "close":           float(last["Close"]),
                "ema200":          float(last["EMA200"]),
                "rolling_high_10": float(last["RH"]) if not pd.isna(last["RH"]) else None,
                "rolling_low_60":  float(last["RL"]) if not pd.isna(last["RL"]) else None,
            }
        except Exception as e:
            self.log.error(f"Yahoo Finance 取得失敗: {e}")
            return None

    # ── 倉位狀態（持久化）────────────────────────────────────────────────────
    def load_state(self) -> dict:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save_state(self, state: dict, runtime: dict | None = None) -> None:
        """
        儲存倉位狀態（+ 執行期間即時數值供 dashboard 讀取）
        runtime: dict 可含 last_price, ema200, leverage, equity,
                         unrealized, liq_price, last_signal, last_reason
        """
        merged = dict(state)
        if runtime:
            merged.update(runtime)
        # 附加策略參數供 dashboard 顯示
        merged.setdefault("target_leverage",   self.target_leverage)
        merged.setdefault("max_leverage",      self.max_leverage)
        merged.setdefault("initial_capital",   self.initial_capital)
        merged.setdefault("dry_run",           self.dry_run)
        merged["save_ts"] = datetime.datetime.now().isoformat(timespec="seconds")
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        self.log.info(f"倉位狀態已儲存 → {STATE_FILE}")

    def init_state(self, price: float) -> dict:
        contracts = math.floor(
            self.initial_capital * self.target_leverage / (price * self.multiplier)
        )
        avg_cost = price if contracts > 0 else 0
        realized = -(contracts * self.fee_per_lot) if contracts > 0 else 0
        state = {
            "initialized":  True,
            "contracts":    contracts,
            "avg_cost":     avg_cost,
            "last_buy_px":  price,
            "realized_pnl": realized,
            "created_date": str(datetime.date.today()),
            "last_updated": str(datetime.date.today()),
            "account":      self.cfg.get("futures_account", "?"),
        }
        self.log.info(
            f"🔵 初始建倉 {contracts} 口 @ {price:.0f}，"
            f"均攤成本 {avg_cost:.0f}，"
            f"槓桿 {self._calc_leverage(price, state):.2f}x"
        )
        return state

    # ── 核心計算 ─────────────────────────────────────────────────────────────
    def _recalc(self, price: float, state: dict) -> dict:
        contracts = state["contracts"]
        avg_cost  = state["avg_cost"]
        realized  = state["realized_pnl"]
        unreal    = contracts * (price - avg_cost) * self.multiplier
        equity    = self.initial_capital + realized + unreal
        exposure  = contracts * price * self.multiplier
        leverage  = (exposure / equity) if equity > 0 else 0
        total_maint = contracts * self.maint_margin
        liq_price = (
            avg_cost + (total_maint - equity) / (contracts * self.multiplier)
            if contracts > 0 else None
        )
        return {
            "equity": equity, "exposure": exposure,
            "leverage": leverage, "liq_price": liq_price,
            "unrealized": unreal,
        }

    def _calc_leverage(self, price: float, state: dict) -> float:
        return self._recalc(price, state)["leverage"]

    def evaluate_signal(
        self, price: float, ema200: float | None, state: dict
    ) -> dict | None:
        """
        依策略規則評估訊號。
        回傳 None = HOLD；否則回傳操作字典。
        """
        st        = self._recalc(price, state)
        equity    = st["equity"]
        exposure  = st["exposure"]
        leverage  = st["leverage"]
        contracts = state["contracts"]
        avg_cost  = state["avg_cost"]
        last_buy  = state["last_buy_px"]

        # 規則 4：降槓桿（價格 > 均攤 且 槓桿超標）
        if contracts > 0 and price > avg_cost and leverage > self.target_leverage + 0.15:
            target_exp = equity * self.target_leverage
            to_sell = min(
                contracts - 1,
                math.floor((exposure - target_exp) / (price * self.multiplier))
            )
            if to_sell > 0:
                return {
                    "action": "DELEVERAGE",
                    "qty":    -to_sell,
                    "reason": (
                        f"降槓桿：賣 {to_sell} 口  "
                        f"槓桿 {leverage:.2f}x → 目標 {self.target_leverage}x  "
                        f"均攤成本 {avg_cost:.0f}  獲利 {((price/avg_cost-1)*100):.1f}%"
                    ),
                }

        # 規則 2：DCA（從上次買點跌超過門檻）
        if last_buy > 0:
            drop_pct = (1 - price / last_buy) * 100
            if drop_pct >= self.dca_threshold and leverage < self.max_leverage:
                new_contracts = contracts + 1
                new_avg = (contracts * avg_cost + price) / new_contracts
                return {
                    "action": "DCA",
                    "qty":    1,
                    "reason": (
                        f"攤平：加買 1 口  "
                        f"距上次買點 {last_buy:.0f} 跌幅 {drop_pct:.1f}%  "
                        f"均攤成本 {avg_cost:.0f} → {new_avg:.0f}  "
                        f"槓桿 {leverage:.2f}x（上限 {self.max_leverage}x）"
                    ),
                }

        # 規則 3：動態加碼（槓桿低且在年線之上且獲利）
        above_ema = (ema200 is not None and price > ema200)
        if (contracts > 0 and price > avg_cost
                and leverage < self.pyramid_trigger and above_ema):
            target_exp = equity * self.target_leverage
            to_buy = math.floor((target_exp - exposure) / (price * self.multiplier))
            if to_buy > 0:
                new_contracts = contracts + to_buy
                new_avg = (contracts * avg_cost + to_buy * price) / new_contracts
                return {
                    "action": "PYRAMID",
                    "qty":    to_buy,
                    "reason": (
                        f"動態加碼：買 {to_buy} 口  "
                        f"槓桿 {leverage:.2f}x < 下限 {self.pyramid_trigger}x  "
                        f"年線 {ema200:.0f} 之上  "
                        f"均攤成本 {avg_cost:.0f} → {new_avg:.0f}"
                    ),
                }

        return None   # HOLD

    def apply_signal(self, signal: dict, price: float, state: dict) -> dict:
        """將成交訊號套用至倉位狀態"""
        qty   = signal["qty"]
        state = dict(state)

        if qty > 0:
            new_total         = state["contracts"] + qty
            state["avg_cost"] = (state["contracts"] * state["avg_cost"] + qty * price) / new_total
            state["contracts"] = new_total
            state["realized_pnl"] -= qty * self.fee_per_lot
            state["last_buy_px"]   = price
        else:
            sell_qty = abs(qty)
            profit   = sell_qty * (price - state["avg_cost"]) * self.multiplier
            state["realized_pnl"]  += profit - sell_qty * self.fee_per_lot
            state["contracts"]     -= sell_qty

        state["last_updated"] = str(datetime.date.today())
        return state

    # ── 市場時段判斷 ─────────────────────────────────────────────────────────
    @staticmethod
    def is_trading_session() -> bool:
        """是否在期貨交易時段（日盤或夜盤）"""
        now = datetime.datetime.now()
        if now.weekday() >= 5:   # 週六/日
            return False
        t = now.time()
        # 日盤 08:45-13:45
        if FUT_OPEN_DAY <= t <= FUT_CLOSE_DAY:
            return True
        # 夜盤 15:00-翌日05:00
        if t >= FUT_OPEN_NIGHT or t <= FUT_CLOSE_NIGHT:
            return True
        return False

    # ── 主執行（單次）────────────────────────────────────────────────────────
    def run_once(self, reset: bool = False, status_only: bool = False) -> None:
        """執行一次策略判斷（適合排程器呼叫）"""
        self._ensure_login()

        # 取得行情（含 EMA + 滾動高低）
        mkt = self.fetch_twii_with_ema()
        if mkt is None:
            # 備援：從 SDK 取得即時報價
            q = self.get_future_quote()
            price  = q["last"] if q else None
            ema200 = rh = rl = None
        else:
            price  = mkt["close"]
            ema200 = mkt["ema200"]
            rh     = mkt["rolling_high_10"]   # 前 10 日最高
            rl     = mkt["rolling_low_60"]    # 前 60 日最低

        if price is None:
            self.log.error("無法取得行情，終止")
            return

        # 濾網設定
        trend_filter = self.cfg.get("trend_filter", True)
        high_period  = int(self.cfg.get("high_period", 10))
        low_period   = int(self.cfg.get("low_period",  100))

        # 載入倉位
        state = self.load_state()

        if reset or not state.get("initialized"):
            # 濾網：初始建倉需突破 N 日高
            if trend_filter and rh is not None and price <= rh:
                self.log.info(
                    f"⏳ 濾網：現價 {price:.0f} 未突破 {high_period}日高 {rh:.0f}，"
                    f"暫不建倉，等待順勢進場"
                )
                return
            state = self.init_state(price)
            self.save_state(state)
            if not reset:
                return

        # 顯示現況
        st = self._recalc(price, state)
        liq_margin = (
            f"  強平距離:{((price - st['liq_price']) / price * 100):.1f}%"
            if st["liq_price"] else ""
        )
        filter_str = ""
        if trend_filter:
            filter_str = (
                f"  {high_period}日高:{rh:.0f}({'✅突破' if rh and price>rh else '❌未突破'})"
                f"  {low_period}日低:{rl:.0f}({'⚠破底!' if rl and price<rl else '✅安全'})"
            ) if rh and rl else ""
        self.log.info(
            f"倉位：{state['contracts']} 口  "
            f"均攤:{state['avg_cost']:.0f}  "
            f"現價:{price:.0f}  "
            f"槓桿:{st['leverage']:.2f}x  "
            f"淨值:{st['equity']:,.0f}  "
            f"未實現:{st['unrealized']:+,.0f}"
            + liq_margin + filter_str
        )

        if status_only:
            return

        # 防止同日策略重複執行（manual_adjust 不會更新 strategy_last_run，不受此限制）
        today = str(datetime.date.today())
        if state.get("strategy_last_run") == today and not reset:
            self.log.info(f"今日策略（{today}）已執行，跳過。若要強制執行請加 --reset")
            return

        # ── 濾網：60日破底 → 立即全數清倉 ──────────────────────────────────
        if trend_filter and state["contracts"] > 0 and rl is not None and price < rl:
            qty = state["contracts"]
            self.log.warning(
                f"🚨 {low_period}日破底濾網觸發！"
                f"現價 {price:.0f} < {low_period}日低 {rl:.0f}，"
                f"清倉 {qty} 口 → 現金保護"
            )
            success = self.place_future_order("sell", qty, price)
            if success:
                profit = qty * (price - state["avg_cost"]) * self.multiplier
                state["realized_pnl"] += profit - qty * self.fee_per_lot
                state["contracts"]     = 0
                state["last_updated"]  = today
                self.save_state(state, runtime={
                    "last_price":  price, "ema200": ema200,
                    "leverage": 0, "equity": self.initial_capital + state["realized_pnl"],
                    "unrealized": 0, "liq_price": None,
                    "last_signal": "EXIT",
                    "last_reason": f"{low_period}日破底清倉（{price:.0f}<{rl:.0f}），持現金等待",
                    "rolling_high": rh, "rolling_low": rl,
                    "strategy_last_run": today,
                })
            return

        # 評估訊號
        signal = self.evaluate_signal(price, ema200, state)

        labels = {
            "DCA":        "📣 攤平加碼",
            "PYRAMID":    "📈 動態加碼",
            "DELEVERAGE": "🔻 降槓桿",
        }

        # ── 濾網：現金等待中 → 等 10 日創高重新進場 ────────────────────────
        if state["contracts"] == 0 and trend_filter:
            if rh is not None and price > rh:
                target = math.floor(
                    self.initial_capital * self.target_leverage / (price * self.multiplier)
                )
                self.log.info(
                    f"✅ {high_period}日創高進場！{price:.0f} > {rh:.0f}，"
                    f"重新建倉 {target} 口"
                )
                success = self.place_future_order("buy", target, price)
                if success:
                    state = self.init_state(price)
                    state["last_updated"] = today
                    self.save_state(state, runtime={
                        "last_price": price, "ema200": ema200,
                        "last_signal": "ENTRY",
                        "last_reason": f"{high_period}日創高重新進場（{price:.0f}>{rh:.0f}）",
                        "rolling_high": rh, "rolling_low": rl,
                        "strategy_last_run": today,
                    })
            else:
                self.log.info(
                    f"⏳ 現金等待：{high_period}日高 {rh:.0f}，"
                    f"需突破 {rh:.0f} 才進場（現價 {price:.0f}）"
                )
                self.save_state(state, runtime={
                    "last_price": price, "ema200": ema200,
                    "last_signal": "CASH", "last_reason": f"等待{high_period}日創高進場",
                    "rolling_high": rh, "rolling_low": rl,
                    "strategy_last_run": today,
                })
            return

        if signal is None:
            self.log.info("今日無操作訊號（HOLD）")
            state["last_updated"] = today
            self.save_state(state, runtime={
                "last_price":   price,
                "ema200":       ema200,
                "leverage":     st["leverage"],
                "equity":       st["equity"],
                "unrealized":   st["unrealized"],
                "liq_price":    st["liq_price"],
                "last_signal":  "HOLD",
                "last_reason":  "",
                "rolling_high": rh,
                "rolling_low":  rl,
                "strategy_last_run": today,
            })
            return

        label = labels.get(signal["action"], signal["action"])
        qty   = abs(signal["qty"])
        side  = "buy" if signal["qty"] > 0 else "sell"

        self.log.info(f"\n{'='*55}")
        self.log.info(f"⚡ 訊號：{label}")
        self.log.info(f"   操作：{'買入' if side=='buy' else '賣出'} {qty} 口 {self.symbol}")
        self.log.info(f"   理由：{signal['reason']}")
        self.log.info(f"{'='*55}")

        # 下單
        success = self.place_future_order(side, qty, price)

        if success:
            new_state = self.apply_signal(signal, price, state)
            new_st = self._recalc(price, new_state)
            self.log.info(
                f"執行後：{new_state['contracts']} 口  "
                f"均攤:{new_state['avg_cost']:.0f}  "
                f"槓桿:{new_st['leverage']:.2f}x  "
                f"淨值:{new_st['equity']:,.0f}"
            )
            self.save_state(new_state, runtime={
                "last_price":   price,
                "ema200":       ema200,
                "leverage":     new_st["leverage"],
                "equity":       new_st["equity"],
                "unrealized":   new_st["unrealized"],
                "liq_price":    new_st["liq_price"],
                "last_signal":  signal["action"],
                "last_reason":  signal["reason"],
                "strategy_last_run": today,
            })
        else:
            self.log.error("下單失敗，倉位狀態未更新")

    # ── 主執行（循環模式）────────────────────────────────────────────────────
    def run_loop(self, reset: bool = False) -> None:
        """收盤後循環執行（適合全天掛機）"""
        loop_sec = int(self.cfg.get("fmx_loop_seconds", 3600))
        self.log.info(f"循環模式啟動，每 {loop_sec//60} 分鐘檢查一次")

        while True:
            try:
                self.run_once(reset=reset)
                reset = False    # 重置只第一次
            except KeyboardInterrupt:
                self.log.info("使用者中斷")
                break
            except Exception as e:
                self.log.error(f"執行錯誤: {e}", exc_info=True)
                try:
                    self.sdk = None
                    self.account = None
                    time.sleep(60)
                    self._ensure_login()
                except Exception as le:
                    self.log.error(f"重新登入失敗: {le}")
            time.sleep(loop_sec)


# ════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="FMX 微台動態槓桿策略 — 富邦期貨實單")
    parser.add_argument("--config",          default=CONFIG_FILE, help="設定檔路徑")
    parser.add_argument("--dry-run",         action="store_true", help="模擬模式，不實際下單")
    parser.add_argument("--reset",           action="store_true", help="重置倉位，重新建倉")
    parser.add_argument("--status",          action="store_true", help="僅顯示倉位狀態")
    parser.add_argument("--once",            action="store_true", help="執行一次後離開（適合排程）")
    # 手動加/減槓桿 / 轉倉
    parser.add_argument("--add-leverage",    action="store_true", help="手動加槓桿 N 口")
    parser.add_argument("--reduce-leverage", action="store_true", help="手動降槓桿 N 口")
    parser.add_argument("--rollover",        action="store_true", help="轉倉：近月平倉 → 次月開倉（預設全部口數）")
    parser.add_argument("--lots",            type=int,   default=None,    help="手動操作口數（預設 None=全部）")
    parser.add_argument("--order-type",      choices=["limit","market"],
                                             default="limit",              help="委託類型")
    parser.add_argument("--spread",          type=float, default=5.0,     help="限價委託偏移點數（預設 5）")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"找不到設定檔: {args.config}")
        sys.exit(1)

    bot = FMXLiveBot(args.config, force_dry_run=args.dry_run)

    if args.add_leverage:
        lots = args.lots if args.lots is not None else 1
        bot.manual_adjust("buy",  lots=lots, order_type=args.order_type, spread=args.spread)
        return

    if args.reduce_leverage:
        lots = args.lots if args.lots is not None else 1
        bot.manual_adjust("sell", lots=lots, order_type=args.order_type, spread=args.spread)
        return

    if args.rollover:
        bot.manual_rollover(lots=args.lots, order_type=args.order_type, spread=args.spread)
        return

    if args.status:
        bot._ensure_login()
        bot.run_once(status_only=True)
        return

    if args.once or args.reset:
        bot.run_once(reset=args.reset)
    else:
        bot.run_loop(reset=args.reset)


if __name__ == "__main__":
    main()
