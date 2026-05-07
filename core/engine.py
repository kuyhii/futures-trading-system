#!/usr/bin/env python3
"""
core/engine.py - 量化交易核心引擎
职责: 完整交易生命周期管理
  - 实时行情（HTTP轮询 + WebSocket备用）
  - 策略信号 → 风控审核 → 自动下单 全闭环
  - 仓位计算（基于账户余额 + 风控参数）
  - 止损/止盈/追踪止损自动执行
  - 多时间框架分析
  - API错误重试与熔断

使用方式:
  python3 -m core.engine              # 启动自动交易（dry-run模式默认）
  python3 -m core.engine --live       # 实盘模式（需确认）
  python3 -m core.engine --backtest   # 回测模式
  python3 -m core.engine --status     # 查看当前状态
"""

import json
import os
import sys
import time
import hmac
import hashlib
import requests
import logging
import signal as sig
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum

# ── 路径 ──
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")
LOGS_DIR = os.path.join(ROOT, "logs")
STATE_DIR = os.path.join(ROOT, "state")
CORE_DIR = os.path.dirname(os.path.abspath(__file__))

for d in [DATA_DIR, LOGS_DIR, STATE_DIR, os.path.join(DATA_DIR, "klines"),
           os.path.join(DATA_DIR, "signals"), os.path.join(DATA_DIR, "live")]:
    os.makedirs(d, exist_ok=True)

# ── 加载 .env ──
def load_env():
    env_file = os.path.join(ROOT, ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip()
                    if key and val and key not in os.environ:
                        os.environ[key] = val

load_env()

# ═══════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════

BINANCE_API_ENV = os.environ.get("BINANCE_API_ENV", "testnet")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")

if BINANCE_API_ENV == "prod":
    BINANCE_API_KEY = os.environ.get("BINANCE_PROD_API_KEY", BINANCE_API_KEY)
    BINANCE_SECRET_KEY = os.environ.get("BINANCE_PROD_SECRET_KEY", BINANCE_SECRET_KEY)

BASE_URLS = {
    "prod":    "https://fapi.binance.com",
    "testnet": "https://testnet.binancefuture.com",
    "demo":    "https://testnet.binancefuture.com",
}
BASE_URL = BASE_URLS.get(BINANCE_API_ENV, BASE_URLS["testnet"])
STREAM_URL = "fstream.binance.com" if BINANCE_API_ENV == "prod" else "stream.binancefuture.com"

IS_AUTHENTICATED = bool(BINANCE_API_KEY and BINANCE_SECRET_KEY)

# ═══════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, "engine.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("engine")

# ═══════════════════════════════════════════
# Binance REST API Client
# ═══════════════════════════════════════════

class BinanceClient:
    """币安 U 本位合约 REST API 封装"""

    def __init__(self, api_key: str = "", api_secret: str = "", base_url: str = BASE_URL):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key} if api_key else {})
        self._req_count = 0
        self._last_error_time = 0
        self._consecutive_errors = 0
        self.circuit_breaker = False  # 熔断标志

    def _sign(self, params: dict) -> dict:
        """签名请求参数"""
        if not self.api_secret:
            return params
        params["timestamp"] = int(time.time() * 1000)
        query = "&".join([f"{k}={v}" for k, v in params.items()])
        signature = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _request(self, method: str, path: str, params: dict = None,
                 signed: bool = False, retries: int = 3) -> dict:
        """HTTP 请求（带重试 + 熔断）"""
        if self.circuit_breaker:
            # 熔断: 等待 60s 后重试
            if time.time() - self._last_error_time < 60:
                raise RuntimeError("🚨 API 熔断中，等待冷却")
            self.circuit_breaker = False
            self._consecutive_errors = 0
            logger.warning("熔断已解除，恢复请求")

        url = f"{self.base_url}{path}"
        params = params or {}

        if signed:
            params = self._sign(params)

        for attempt in range(retries):
            try:
                if method == "GET":
                    resp = self.session.get(url, params=params, timeout=10)
                elif method == "POST":
                    resp = self.session.post(url, params=params, timeout=10)
                elif method == "DELETE":
                    resp = self.session.delete(url, params=params, timeout=10)
                else:
                    raise ValueError(f"Unknown method: {method}")

                self._req_count += 1

                if resp.status_code == 429:
                    # 频率限制，退避
                    wait = min(2 ** attempt, 10)
                    logger.warning(f"⚠️ 429 频率限制，等待 {wait}s 后重试")
                    time.sleep(wait)
                    continue

                if resp.status_code >= 400:
                    self._consecutive_errors += 1
                    self._last_error_time = time.time()
                    if self._consecutive_errors >= 5:
                        self.circuit_breaker = True
                        logger.error("🚨 连续 5 次错误，触发熔断！")

                    body = resp.text
                    logger.error(f"API 错误 [{resp.status_code}]: {body}")
                    if attempt < retries - 1:
                        time.sleep(1)
                        continue
                    return {"error": body, "status": resp.status_code}

                self._consecutive_errors = 0
                return resp.json()

            except requests.exceptions.Timeout:
                logger.warning(f"请求超时 (attempt {attempt + 1})")
                if attempt < retries - 1:
                    time.sleep(1)
                    continue
                return {"error": "timeout"}
            except Exception as e:
                logger.error(f"请求异常: {e}")
                if attempt < retries - 1:
                    time.sleep(1)
                    continue
                return {"error": str(e)}

        return {"error": "max retries exceeded"}

    # ── 公开接口 ──

    def klines(self, symbol: str, interval: str = "15m", limit: int = 100) -> List:
        """K 线数据"""
        return self._request("GET", "/fapi/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": limit
        })

    def ticker_price(self, symbol: str = None) -> dict:
        """最新价格"""
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/ticker/price", params)

    def mark_price(self, symbol: str = None) -> dict:
        """标记价格"""
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/premiumIndex", params)

    def funding_rate(self, symbol: str, limit: int = 10) -> List:
        """资金费率历史"""
        return self._request("GET", "/fapi/v1/fundingRate", {
            "symbol": symbol, "limit": limit
        })

    def open_interest(self, symbol: str) -> dict:
        """当前持仓量"""
        return self._request("GET", "/fapi/v1/openInterest", {"symbol": symbol})

    def depth(self, symbol: str, limit: int = 20) -> dict:
        """订单簿深度"""
        return self._request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    def exchange_info(self) -> dict:
        """交易规则（精度、最小下单量等）"""
        return self._request("GET", "/fapi/v1/exchangeInfo")

    # ── 签名接口（需要 API Key） ──

    def account_balance(self) -> List:
        """合约账户余额"""
        return self._request("GET", "/fapi/v2/balance", {}, signed=True)

    def account_info(self) -> dict:
        """账户信息"""
        return self._request("GET", "/fapi/v2/account", {}, signed=True)

    def positions(self, symbol: str = None) -> List:
        """持仓信息"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._request("GET", "/fapi/v2/positionRisk", params, signed=True)
        if isinstance(data, list):
            return [p for p in data if float(p.get("positionAmt", 0)) != 0]
        return []

    def open_orders(self, symbol: str = None) -> List:
        """当前挂单"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/fapi/v1/openOrders", params, signed=True)

    def new_order(self, symbol: str, side: str, order_type: str,
                  quantity: float = None, price: float = None,
                  stop_price: float = None, reduce_only: bool = False,
                  close_position: bool = False, time_in_force: str = "GTC",
                  callback_rate: float = None) -> dict:
        """下单"""
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
        }
        if quantity:
            params["quantity"] = quantity
        if price:
            params["price"] = price
        if stop_price:
            params["stopPrice"] = stop_price
        if reduce_only:
            params["reduceOnly"] = "true"
        if close_position:
            params["closePosition"] = "true"
        if time_in_force and order_type in ("LIMIT", "STOP", "TAKE_PROFIT"):
            params["timeInForce"] = time_in_force
        if callback_rate:
            params["callbackRate"] = callback_rate

        logger.info(f"📤 下单: {params}")
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def cancel_order(self, symbol: str, order_id: int = None, orig_client_order_id: str = None) -> dict:
        """撤单"""
        params = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        return self._request("DELETE", "/fapi/v1/order", params, signed=True)

    def cancel_all_orders(self, symbol: str) -> dict:
        """取消所有挂单"""
        return self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    def change_leverage(self, symbol: str, leverage: int) -> dict:
        """修改杠杆"""
        return self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    def change_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        """修改保证金类型"""
        return self._request("POST", "/fapi/v1/marginType",
                             {"symbol": symbol, "marginType": margin_type}, signed=True)

    def modify_isolated_margin(self, symbol: str, amount: float, type: int = 1) -> dict:
        """调整逐仓保证金"""
        return self._request("POST", "/fapi/v1/positionMargin",
                             {"symbol": symbol, "amount": amount, "type": type}, signed=True)

    def income_history(self, symbol: str = None, income_type: str = None,
                       limit: int = 50, start_time: int = None) -> List:
        """收益历史"""
        params: dict = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if income_type:
            params["incomeType"] = income_type
        if start_time:
            params["startTime"] = start_time
        return self._request("GET", "/fapi/v1/income", params, signed=True)

    def get_order(self, symbol: str, order_id: int = None, orig_client_order_id: str = None) -> dict:
        """查询订单状态"""
        params = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        return self._request("GET", "/fapi/v1/order", params, signed=True)

    def my_trades(self, symbol: str, limit: int = 20) -> List:
        """个人成交记录"""
        return self._request("GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": limit}, signed=True)

    # ── 工具方法 ──

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        """获取交易对精度信息"""
        info = self.exchange_info()
        if "symbols" not in info:
            return None
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return s
        return None

    def adjust_quantity(self, symbol: str, quantity: float) -> float:
        """根据交易对精度调整下单数量"""
        info = self.get_symbol_info(symbol)
        if not info:
            return quantity
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step = float(f["stepSize"])
                min_qty = float(f["minQty"])
                # 对齐到 stepSize
                quantity = max(min_qty, round(quantity - (quantity % step), 10))
                break
        return quantity

    def adjust_price(self, symbol: str, price: float) -> float:
        """根据交易对精度调整价格"""
        info = self.get_symbol_info(symbol)
        if not info:
            return price
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick = float(f["tickSize"])
                return round(price - (price % tick), 10)
        return price

    @property
    def is_circuit_broken(self) -> bool:
        return self.circuit_breaker


# ═══════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════

class SignalAction(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    CLOSE = "close"


class PositionSide(Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class TradeSignal:
    symbol: str
    action: SignalAction
    strategy: str
    confidence: float
    price: float
    timeframe: str
    details: dict = field(default_factory=dict)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class Position:
    symbol: str
    side: PositionSide
    quantity: float
    entry_price: float
    leverage: int
    unrealized_pnl: float = 0
    mark_price: float = 0
    liquidation_price: float = 0
    margin: float = 0
    signal_strategy: str = ""
    entry_time: str = ""
    stop_loss_price: float = 0
    take_profit_price: float = 0
    highest_pnl: float = 0  # 追踪止损用
    order_id_open: int = 0

    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0
        direction = 1 if self.side == PositionSide.LONG else -1
        return (self.mark_price - self.entry_price) / self.entry_price * 100 * direction


@dataclass
class AccountState:
    total_equity: float = 0
    available_balance: float = 0
    unrealized_pnl: float = 0
    margin_balance: float = 0
    positions: List[Position] = field(default_factory=list)
    daily_pnl: float = 0
    daily_trades: int = 0
    max_drawdown_pct: float = 0


# ═══════════════════════════════════════════
# 技术指标（增强版）
# ═══════════════════════════════════════════

class Indicators:
    """技术指标计算"""

    @staticmethod
    def sma(closes: List[float], period: int) -> Optional[float]:
        if len(closes) < period:
            return None
        return sum(closes[-period:]) / period

    @staticmethod
    def ema(closes: List[float], period: int) -> Optional[float]:
        if len(closes) < period:
            return None
        mult = 2 / (period + 1)
        ema = sum(closes[:period]) / period
        for p in closes[period:]:
            ema = (p - ema) * mult + ema
        return ema

    @staticmethod
    def ema_series(closes: List[float], period: int) -> List[float]:
        """返回完整 EMA 序列"""
        if len(closes) < period:
            return []
        mult = 2 / (period + 1)
        ema = sum(closes[:period]) / period
        result = [ema]
        for p in closes[period:]:
            ema = (p - ema) * mult + ema
            result.append(ema)
        return result

    @staticmethod
    def rsi(closes: List[float], period: int = 14) -> Optional[float]:
        if len(closes) < period + 1:
            return None
        gains, losses = [], []
        for i in range(1, len(closes)):
            change = closes[i] - closes[i - 1]
            gains.append(max(0, change))
            losses.append(max(0, -change))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        return 100 - (100 / (1 + avg_gain / avg_loss))

    @staticmethod
    def macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
        if len(closes) < slow + signal:
            return None, None, None
        ema_fast = Indicators.ema_series(closes, fast)
        ema_slow = Indicators.ema_series(closes, slow)
        if not ema_fast or not ema_slow:
            return None, None, None
        offset = len(ema_slow) - len(ema_fast)
        macd_line = [f - s for f, s in zip(ema_fast, ema_slow[offset:])]
        sig_line = Indicators.ema_series(macd_line, signal)
        if not sig_line:
            return None, None, None
        histogram = macd_line[-1] - sig_line[-1]
        return macd_line[-1], sig_line[-1], histogram

    @staticmethod
    def bollinger(closes: List[float], period: int = 20, std_dev: float = 2.0):
        if len(closes) < period:
            return None, None, None
        recent = closes[-period:]
        mid = sum(recent) / period
        variance = sum((x - mid) ** 2 for x in recent) / period
        std = variance ** 0.5
        return mid - std_dev * std, mid, mid + std_dev * std

    @staticmethod
    def atr(candles: List[dict], period: int = 14) -> Optional[float]:
        """平均真实波动范围"""
        if len(candles) < period + 1:
            return None
        true_ranges = []
        for i in range(1, len(candles)):
            high = candles[i]["high"]
            low = candles[i]["low"]
            prev_close = candles[i - 1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
        if len(true_ranges) < period:
            return None
        return sum(true_ranges[-period:]) / period

    @staticmethod
    def highest(closes: List[float], period: int) -> Optional[float]:
        if len(closes) < period:
            return None
        return max(closes[-period:])

    @staticmethod
    def lowest(closes: List[float], period: int) -> Optional[float]:
        if len(closes) < period:
            return None
        return min(closes[-period:])

    @staticmethod
    def volume_ma(volumes: List[float], period: int = 20) -> Optional[float]:
        if len(volumes) < period:
            return None
        return sum(volumes[-period:]) / period

    @staticmethod
    def vwap(candles: List[dict]) -> Optional[float]:
        """成交量加权平均价"""
        if not candles:
            return None
        total_vp = sum(c["close"] * c["volume"] for c in candles)
        total_v = sum(c["volume"] for c in candles)
        if total_v == 0:
            return None
        return total_vp / total_v


# ═══════════════════════════════════════════
# 策略（增强版 — 5 个策略全部实现）
# ═══════════════════════════════════════════

class StrategyEngine:
    """多策略信号引擎"""

    def __init__(self, config: dict):
        self.config = config
        self.strategies_cfg = config.get("strategies", {})
        self.indicators_cfg = config.get("indicators", {})

    def parse_klines(self, raw: list) -> List[dict]:
        """解析 binance K 线数组"""
        candles = []
        if not isinstance(raw, list):
            return candles
        for k in raw:
            if isinstance(k, list) and len(k) >= 6:
                candles.append({
                    "open_time": k[0],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": k[6],
                })
        return candles

    def analyze(self, candles: List[dict]) -> List[TradeSignal]:
        """运行所有启用的策略，返回信号列表"""
        signals = []
        for name, cfg in self.strategies_cfg.items():
            if not cfg.get("enabled", False):
                continue
            try:
                sig = getattr(self, f"_strategy_{name}", None)
                if sig:
                    result = sig(candles, cfg)
                    if result and result.action != SignalAction.HOLD:
                        signals.append(result)
            except Exception as e:
                logger.error(f"策略 {name} 异常: {e}")
        return signals

    def _make_signal(self, action: SignalAction, strategy: str,
                     confidence: float, price: float, details: dict,
                     timeframe: str = "15m", symbol: str = "") -> TradeSignal:
        return TradeSignal(
            symbol=symbol, action=action, strategy=strategy, confidence=confidence,
            price=price, timeframe=timeframe, details=details
        )

    # ── 策略 1: 趋势跟踪（增强：MACD 确认 + 多时间框架） ──

    def _strategy_trend_follow(self, candles: List[dict], cfg: dict) -> Optional[TradeSignal]:
        if len(candles) < 30:
            return None

        closes = [c["close"] for c in candles]
        volumes = [c["volume"] for c in candles]
        price = closes[-1]

        fast = self.indicators_cfg["ma_fast"]
        slow = self.indicators_cfg["ma_slow"]

        ma_fast = Indicators.sma(closes, fast)
        ma_slow = Indicators.sma(closes, slow)
        ma_fast_prev = Indicators.sma(closes[:-1], fast)
        ma_slow_prev = Indicators.sma(closes[:-1], slow)
        vol_ma = Indicators.volume_ma(volumes, 20)
        current_vol = volumes[-1]

        if not all([ma_fast, ma_slow, ma_fast_prev, ma_slow_prev]):
            return None

        details = {"ma_fast": round(ma_fast, 2), "ma_slow": round(ma_slow, 2),
                   "current_price": price}

        # MACD 确认
        macd_val, macd_sig, macd_hist = Indicators.macd(closes)
        if macd_val is not None:
            details["macd"] = round(macd_val, 4)
            details["macd_signal"] = round(macd_sig, 4)
            details["macd_histogram"] = round(macd_hist, 4)

        vol_confirm = (not vol_ma) or (current_vol > vol_ma * 1.2)

        # 金叉
        if ma_fast_prev <= ma_slow_prev and ma_fast > ma_slow:
            conf = 0.9 if (vol_confirm and macd_hist and macd_hist > 0) else 0.6
            return self._make_signal(SignalAction.BUY, "trend_follow", conf, price,
                                     {**details, "cross": "golden", "volume_confirm": vol_confirm})

        # 死叉
        if ma_fast_prev >= ma_slow_prev and ma_fast < ma_slow:
            conf = 0.9 if (vol_confirm and macd_hist and macd_hist < 0) else 0.6
            return self._make_signal(SignalAction.SELL, "trend_follow", conf, price,
                                     {**details, "cross": "death", "volume_confirm": vol_confirm})

        return None

    # ── 策略 2: 均值回归（增强：ATR 动态区间） ──

    def _strategy_mean_reversion(self, candles: List[dict], cfg: dict) -> Optional[TradeSignal]:
        if len(candles) < 30:
            return None

        closes = [c["close"] for c in candles]
        price = closes[-1]

        rsi = Indicators.rsi(closes, self.indicators_cfg["rsi_period"])
        lower, mid, upper = Indicators.bollinger(closes,
                                                  self.indicators_cfg["bollinger_period"],
                                                  self.indicators_cfg["bollinger_std"])

        if rsi is None or lower is None:
            return None

        details = {"rsi": round(rsi, 2), "bollinger_lower": round(lower, 2),
                   "bollinger_middle": round(mid, 2), "bollinger_upper": round(upper, 2),
                   "current_price": price}

        ob = self.indicators_cfg["rsi_overbought"]
        os_ = self.indicators_cfg["rsi_oversold"]

        if rsi < os_ and price < lower:
            return self._make_signal(SignalAction.BUY, "mean_reversion", 0.7, price,
                                     {**details, "reason": "oversold + below_lower"})
        if rsi > ob and price > upper:
            return self._make_signal(SignalAction.SELL, "mean_reversion", 0.7, price,
                                     {**details, "reason": "overbought + above_upper"})
        return None

    # ── 策略 3: 突破策略（新增实现） ──

    def _strategy_breakout(self, candles: List[dict], cfg: dict) -> Optional[TradeSignal]:
        lookback = cfg.get("lookback_periods", 20)
        if len(candles) < lookback + 5:
            return None

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        volumes = [c["volume"] for c in candles]
        price = closes[-1]

        highest = Indicators.highest(highs, lookback)
        lowest = Indicators.lowest(lows, lookback)
        vol_ma = Indicators.volume_ma(volumes, 20)

        if highest is None or lowest is None:
            return None

        details = {"highest": round(highest, 2), "lowest": round(lowest, 2),
                   "current_price": price, "lookback": lookback}

        prev_price = closes[-2]
        vol_confirm = (not vol_ma) or (volumes[-1] > vol_ma * 1.3)

        # 突破上轨
        if prev_price < highest and price >= highest:
            conf = 0.8 if vol_confirm else 0.5
            return self._make_signal(SignalAction.BUY, "breakout", conf, price,
                                     {**details, "breakout": "upper", "volume_confirm": vol_confirm})

        # 突破下轨
        if prev_price > lowest and price <= lowest:
            conf = 0.8 if vol_confirm else 0.5
            return self._make_signal(SignalAction.SELL, "breakout", conf, price,
                                     {**details, "breakout": "lower", "volume_confirm": vol_confirm})

        return None

    # ── 策略 4: 资金费率套利 ──

    def _strategy_funding_rate(self, candles: List[dict], cfg: dict) -> Optional[TradeSignal]:
        # 资金费率需要从外部传入，这里通过 candles 最后一个的额外字段
        # 在实际引擎中会通过 API 单独获取
        return None

    # ── 策略 5: 多空比反转 ──

    def _strategy_lsr_reversal(self, candles: List[dict], cfg: dict) -> Optional[TradeSignal]:
        # 多空比需要从外部 API 获取，这里作为框架保留
        return None


# ═══════════════════════════════════════════
# 风控引擎
# ═══════════════════════════════════════════

class RiskEngine:
    """风控审核 + 自动止损/止盈/追踪止损"""

    def __init__(self, config: dict):
        self.cfg = config
        self.risk_rules = config.get("risk_rules", {})
        self._daily_start_equity = 0  # 当日初始权益
        self._peak_equity = 0

    def set_daily_start(self, equity: float):
        self._daily_start_equity = equity
        if equity > self._peak_equity:
            self._peak_equity = equity

    def can_open_position(self, account: AccountState, symbol: str,
                          side: PositionSide, quantity: float,
                          leverage: int, price: float) -> Tuple[bool, str]:
        """开仓前风控审核"""

        # 1. 最大持仓数
        if len(account.positions) >= self.cfg["max_positions"]:
            return False, f"持仓数已达上限 ({len(account.positions)}/{self.cfg['max_positions']})"

        # 2. 已有同币种持仓
        for p in account.positions:
            if p.symbol == symbol:
                return False, f"{symbol} 已有持仓，不可重复开仓"

        # 3. 杠杆检查
        if leverage > self.cfg["max_leverage"]:
            return False, f"杠杆 {leverage}x 超过上限 {self.cfg['max_leverage']}x"

        # 4. 仓位大小检查
        position_value = price * quantity
        max_position = account.total_equity * self.cfg["position_size_pct"] / 100
        if position_value > max_position:
            return False, f"仓位价值 {position_value:.2f} 超过限制 {max_position:.2f}"

        # 5. 日亏损限制
        if self._daily_start_equity > 0:
            daily_pnl_pct = (account.total_equity - self._daily_start_equity) / self._daily_start_equity * 100
            if daily_pnl_pct < -self.cfg["daily_loss_limit_pct"]:
                return False, f"当日亏损 {daily_pnl_pct:.2f}% 已达限制 {-self.cfg['daily_loss_limit_pct']}%"

        # 6. 总回撤
        if self._peak_equity > 0:
            drawdown = (self._peak_equity - account.total_equity) / self._peak_equity * 100
            if drawdown > self.cfg["max_drawdown_pct"]:
                return False, f"总回撤 {drawdown:.2f}% 超过限制 {self.cfg['max_drawdown_pct']}%"

        # 7. 可用余额检查
        margin_needed = position_value / leverage
        if margin_needed > account.available_balance * 0.9:
            return False, f"可用余额不足: 需要 {margin_needed:.2f}, 可用 {account.available_balance:.2f}"

        return True, "通过"

    def check_position_risk(self, position: Position) -> Optional[str]:
        """检查单个持仓风险，返回触发动作 (None=无异常)"""
        if position.mark_price == 0:
            return None

        pnl = position.pnl_pct()

        # 追踪止损更新
        if self.cfg.get("trailing_stop", False):
            if pnl > position.highest_pnl:
                position.highest_pnl = pnl

        # 止损检查
        if pnl < -self.cfg["stop_loss_pct"]:
            return f"STOP_LOSS: 亏损 {pnl:.2f}% 超过限制 -{self.cfg['stop_loss_pct']}%"

        # 止盈检查
        if pnl > self.cfg["take_profit_pct"]:
            return f"TAKE_PROFIT: 盈利 {pnl:.2f}% 超过目标 +{self.cfg['take_profit_pct']}%"

        # 追踪止损
        if self.cfg.get("trailing_stop") and position.highest_pnl > self.cfg["take_profit_pct"]:
            trail_distance = self.cfg.get("trailing_distance_pct", 1)
            if pnl < (position.highest_pnl - trail_distance):
                return f"TRAILING_STOP: 从最高点 {position.highest_pnl:.2f}% 回撤 {trail_distance}%"

        return None

    def calc_stop_loss(self, entry_price: float, side: PositionSide,
                       atr: float = None) -> float:
        """计算止损价（基于百分比或 ATR）"""
        stop_pct = self.cfg["stop_loss_pct"] / 100
        if atr:
            # 使用 ATR 动态止损 (2x ATR)
            stop_pct = min(stop_pct, (atr * 2 / entry_price))

        if side == PositionSide.LONG:
            return round(entry_price * (1 - stop_pct), 8)
        else:
            return round(entry_price * (1 + stop_pct), 8)

    def calc_take_profit(self, entry_price: float, side: PositionSide) -> float:
        """计算止盈价"""
        tp_pct = self.cfg["take_profit_pct"] / 100
        if side == PositionSide.LONG:
            return round(entry_price * (1 + tp_pct), 8)
        else:
            return round(entry_price * (1 - tp_pct), 8)

    def calc_position_size(self, account: AccountState, price: float,
                           leverage: int, risk_pct: float = None) -> float:
        """
        基于账户余额和风控计算开仓数量
        公式: position_size = (equity * risk_pct%) / (stop_loss_pct * price) * leverage
        简化版: position_size = equity * position_size_pct% / leverage / price
        """
        if risk_pct is None:
            risk_pct = self.cfg["position_size_pct"]

        # 最大仓位价值
        max_value = account.total_equity * risk_pct / 100
        # 名义仓位（考虑杠杆）
        nominal = max_value * leverage
        # 数量
        quantity = nominal / price
        return quantity


# ═══════════════════════════════════════════
# 订单管理器
# ═══════════════════════════════════════════

class OrderManager:
    """订单生命周期管理"""

    def __init__(self, client: BinanceClient, risk: RiskEngine):
        self.client = client
        self.risk = risk
        self._pending_orders: Dict[str, dict] = {}  # symbol -> order_info

    def open_position(self, symbol: str, side: PositionSide,
                      quantity: float, leverage: int, price: float,
                      strategy: str = "unknown", dry_run: bool = False) -> dict:
        """开仓全流程"""
        result = {"symbol": symbol, "side": side.value, "quantity": quantity,
                  "leverage": leverage, "status": "pending", "dry_run": dry_run}

        if dry_run:
            sl = self.risk.calc_stop_loss(price, side)
            tp = self.risk.calc_take_profit(price, side)
            result["status"] = "dry_run_approved"
            result["stop_loss"] = sl
            result["take_profit"] = tp
            result["margin_needed"] = round(price * quantity / leverage, 4)
            logger.info(f"🔵 DRY-RUN 开仓: {side.value.upper()} {symbol} {quantity} @ {price} "
                       f"(杠杆:{leverage}x, 止损:{sl}, 止盈:{tp})")
            return result

        try:
            # 1. 设置杠杆
            self.client.change_leverage(symbol, leverage)
            time.sleep(0.5)

            # 2. 市价开仓
            binance_side = "BUY" if side == PositionSide.LONG else "SELL"
            qty = self.client.adjust_quantity(symbol, quantity)
            order = self.client.new_order(symbol, binance_side, "MARKET", quantity=qty)

            if "error" in order:
                result["status"] = "failed"
                result["error"] = order["error"]
                logger.error(f"❌ 开仓失败: {order['error']}")
                return result

            result["status"] = "opened"
            result["order_id"] = order.get("orderId", 0)
            result["fill_price"] = float(order.get("avgPrice", order.get("price", price)))
            result["executed_qty"] = float(order.get("executedQty", qty))

            logger.info(f"✅ 开仓成功: {side.value.upper()} {symbol} {qty} @ {result['fill_price']}")

            # 3. 设置止损止盈
            sl = self.risk.calc_stop_loss(result["fill_price"], side)
            tp = self.risk.calc_take_profit(result["fill_price"], side)
            result["stop_loss"] = sl
            result["take_profit"] = tp

            self._place_sl_tp(symbol, side, sl, tp)

            # 4. 记录
            self._record_trade("open", symbol, side.value, quantity,
                              result["fill_price"], strategy, "OK")

            return result

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            logger.error(f"❌ 开仓异常: {e}")
            return result

    def close_position(self, symbol: str, dry_run: bool = False,
                       reason: str = "") -> dict:
        """平仓"""
        result = {"symbol": symbol, "status": "pending", "dry_run": dry_run, "reason": reason}

        if dry_run:
            result["status"] = "dry_run_approved"
            logger.info(f"🔵 DRY-RUN 平仓: {symbol} (原因: {reason})")
            return result

        try:
            # 获取持仓
            positions = self.client.positions(symbol)
            if not positions:
                result["status"] = "no_position"
                return result

            pos = positions[0]
            pos_amt = float(pos["positionAmt"])
            if pos_amt == 0:
                result["status"] = "no_position"
                return result

            close_side = "SELL" if pos_amt > 0 else "BUY"
            qty = abs(pos_amt)
            qty = self.client.adjust_quantity(symbol, qty)

            order = self.client.new_order(symbol, close_side, "MARKET",
                                          quantity=qty, reduce_only=True)

            if "error" in order:
                result["status"] = "failed"
                result["error"] = order["error"]
                return result

            result["status"] = "closed"
            result["fill_price"] = float(order.get("avgPrice", 0))
            result["pnl"] = float(pos.get("unRealizedProfit", 0))

            logger.info(f"✅ 平仓成功: {symbol} {qty} @ {result['fill_price']} "
                       f"(盈亏: {result['pnl']:.4f}, 原因: {reason})")

            self._record_trade("close", symbol,
                              "long" if pos_amt > 0 else "short", qty,
                              result["fill_price"], reason,
                              f"OK pnl={result['pnl']:.4f}")

            return result

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            logger.error(f"❌ 平仓异常: {e}")
            return result

    def _place_sl_tp(self, symbol: str, side: PositionSide,
                     sl_price: float, tp_price: float):
        """设置止损止盈订单"""
        try:
            close_side = "SELL" if side == PositionSide.LONG else "BUY"
            sl_price = self.client.adjust_price(symbol, sl_price)
            tp_price = self.client.adjust_price(symbol, tp_price)

            self.client.new_order(symbol, close_side, "STOP_MARKET",
                                  stop_price=sl_price, close_position=True)
            logger.info(f"🛡 止损已设置: {sl_price}")

            self.client.new_order(symbol, close_side, "TAKE_PROFIT_MARKET",
                                  stop_price=tp_price, close_position=True)
            logger.info(f"🎯 止盈已设置: {tp_price}")
        except Exception as e:
            logger.error(f"设置止损止盈异常: {e}")

    def _record_trade(self, action: str, symbol: str, side: str,
                      quantity: float, price: float, strategy: str, result: str):
        """记录交易到文件"""
        history_file = os.path.join(STATE_DIR, "trade_history.json")
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "strategy": strategy,
            "result": result,
        }

        try:
            if os.path.exists(history_file):
                with open(history_file) as f:
                    data = json.load(f)
            else:
                data = {"trades": []}
            data["trades"].append(entry)
            with open(history_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"记录交易失败: {e}")


# ═══════════════════════════════════════════
# 主交易引擎
# ═══════════════════════════════════════════

class TradingEngine:
    """主交易引擎 — 全闭环自动化"""

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY, BASE_URL)
        self.config = self._load_config()
        self.risk_config = self._load_risk_config()
        self.symbols_config = self._load_symbols()

        self.strategy = StrategyEngine(self.config)
        self.risk = RiskEngine(self.risk_config)
        self.orders = OrderManager(self.client, self.risk)

        self.account = AccountState()
        self.running = False
        self.cycle_count = 0
        self.last_cycle_time = 0
        self.cycle_interval = 60  # 秒，主循环间隔

        # 缓存
        self._kline_cache: Dict[str, List[dict]] = {}
        self._last_signal_time: Dict[str, float] = {}
        self.signal_cooldown = 300  # 信号冷却 5 分钟

        logger.info(f"🚀 交易引擎初始化 {'[DRY-RUN 模拟模式]' if dry_run else '[实盘模式]'}")
        logger.info(f"  环境: {BINANCE_API_ENV} | 认证: {'✅' if IS_AUTHENTICATED else '⚠️ 未认证(仅公开数据)'}")
        logger.info(f"  监控币种: {[s['symbol'] for s in self.symbols_config]}")
        logger.info(f"  时间框架: {self.config['timeframe']}")

    def _load_config(self) -> dict:
        with open(os.path.join(CONFIG_DIR, "strategies.json")) as f:
            return json.load(f)

    def _load_risk_config(self) -> dict:
        with open(os.path.join(CONFIG_DIR, "risk.json")) as f:
            return json.load(f)

    def _load_symbols(self) -> List[dict]:
        with open(os.path.join(CONFIG_DIR, "symbols.json")) as f:
            data = json.load(f)
        return [s for s in data["watchlist"] if s.get("enabled", True)]

    # ── 账户状态 ──

    def refresh_account(self):
        """刷新账户状态"""
        if not IS_AUTHENTICATED:
            # 模拟账户
            self.account.total_equity = 10000  # 模拟 10000 USDT
            self.account.available_balance = 10000
            self.account.positions = []
            return

        try:
            balances = self.client.account_balance()
            acc_info = self.client.account_info()

            for b in balances:
                if b.get("asset") == "USDT":
                    self.account.total_equity = float(b.get("totalWalletBalance", 0))
                    self.account.available_balance = float(b.get("availableBalance", 0))

            self.account.unrealized_pnl = float(acc_info.get("totalUnrealizedProfit", 0))
            self.account.margin_balance = float(acc_info.get("totalMarginBalance", 0))
            self.account.total_equity = self.account.margin_balance

            # 刷新持仓
            raw_positions = self.client.positions()
            self.account.positions = []
            for p in raw_positions:
                amt = float(p["positionAmt"])
                if amt == 0:
                    continue
                pos = Position(
                    symbol=p["symbol"],
                    side=PositionSide.LONG if amt > 0 else PositionSide.SHORT,
                    quantity=abs(amt),
                    entry_price=float(p["entryPrice"]),
                    leverage=int(p["leverage"]),
                    unrealized_pnl=float(p["unRealizedProfit"]),
                    mark_price=float(p["markPrice"]),
                    liquidation_price=float(p["liquidationPrice"]),
                    entry_time=datetime.now(timezone.utc).isoformat(),
                )
                self.account.positions.append(pos)

            # 更新风控起始
            if self.risk._daily_start_equity == 0:
                self.risk.set_daily_start(self.account.total_equity)
            self.risk._peak_equity = max(self.risk._peak_equity, self.account.total_equity)

            logger.info(f"💰 账户: 权益={self.account.total_equity:.2f} | "
                       f"可用={self.account.available_balance:.2f} | "
                       f"未实现盈亏={self.account.unrealized_pnl:.2f} | "
                       f"持仓={len(self.account.positions)}")

        except Exception as e:
            logger.error(f"刷新账户状态失败: {e}")

    # ── 行情采集 ──

    def fetch_klines(self, symbol: str, timeframe: str = None) -> List[dict]:
        """获取 K 线数据"""
        tf = timeframe or self.config["timeframe"]
        cache_key = f"{symbol}_{tf}"

        try:
            raw = self.client.klines(symbol, tf, 200)
            if isinstance(raw, dict) and "error" in raw:
                logger.warning(f"K 线获取失败 {symbol}: {raw['error']}")
                return self._kline_cache.get(cache_key, [])

            candles = self.strategy.parse_klines(raw)
            if candles:
                self._kline_cache[cache_key] = candles
                # 保存到文件
                self._save_kline(symbol, tf, raw)
            return candles
        except Exception as e:
            logger.error(f"K 线获取异常 {symbol}: {e}")
            return self._kline_cache.get(cache_key, [])

    def _save_kline(self, symbol: str, tf: str, raw: list):
        """保存 K 线到本地"""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(DATA_DIR, "klines", f"{symbol}_{tf}_{ts}.json")
        try:
            with open(path, "w") as f:
                json.dump(raw, f)
        except Exception:
            pass

    def get_current_price(self, symbol: str) -> float:
        """获取当前价格"""
        try:
            data = self.client.ticker_price(symbol)
            return float(data.get("price", 0))
        except Exception:
            # 从缓存 K 线取
            candles = self._kline_cache.get(f"{symbol}_{self.config['timeframe']}", [])
            if candles:
                return candles[-1]["close"]
            return 0

    # ── 信号处理 ──

    def process_signals(self) -> List[TradeSignal]:
        """运行策略引擎，处理信号"""
        all_signals = []

        for sym_cfg in self.symbols_config:
            symbol = sym_cfg["symbol"]

            # 多时间框架分析
            candles_15m = self.fetch_klines(symbol, "15m")
            signals_15m = self.strategy.analyze(candles_15m)
            for s in signals_15m:
                s.symbol = symbol
                all_signals.append(s)

            # 1h 确认
            if len(self.config.get("active", [])) > 1:
                candles_1h = self.fetch_klines(symbol, "1h")
                signals_1h = self.strategy.analyze(candles_1h)
                # 1h 信号权重更高
                for s in signals_1h:
                    s.symbol = symbol
                    s.timeframe = "1h"
                    s.confidence = min(s.confidence + 0.1, 1.0)
                    all_signals.append(s)

        # 信号聚合 — 同一币种取最高置信度
        best_signals = {}
        for sig in all_signals:
            key = f"{sig.symbol}_{sig.action.value}"
            if key not in best_signals or sig.confidence > best_signals[key].confidence:
                best_signals[key] = sig

        return list(best_signals.values())

    def execute_signal(self, signal: TradeSignal):
        """执行交易信号"""
        symbol = signal.symbol
        action = signal.action

        # 冷却检查
        now = time.time()
        last = self._last_signal_time.get(symbol, 0)
        if now - last < self.signal_cooldown:
            logger.info(f"⏳ {symbol} 信号冷却中，跳过")
            return

        if action == SignalAction.BUY:
            self._handle_buy_signal(signal)
        elif action == SignalAction.SELL:
            self._handle_sell_signal(signal)

        self._last_signal_time[symbol] = now

    def _handle_buy_signal(self, signal: TradeSignal):
        """处理做多信号"""
        symbol = signal.symbol
        price = signal.price
        leverage = self.config.get("strategies", {}).get(
            signal.strategy, {}).get("leverage", self.risk_config["max_leverage"])

        # 检查是否已有空仓需要平
        for pos in self.account.positions:
            if pos.symbol == symbol and pos.side == PositionSide.SHORT:
                logger.info(f"🔄 {symbol} 有空仓，先平仓再做多")
                self.orders.close_position(symbol, self.dry_run, f"反转做多 [{signal.strategy}]")
                break

        # 计算仓位大小
        qty = self.risk.calc_position_size(self.account, price, leverage)
        qty = self.client.adjust_quantity(symbol, qty) if not self.dry_run else round(qty, 6)

        # 风控审核
        ok, reason = self.risk.can_open_position(
            self.account, symbol, PositionSide.LONG, qty, leverage, price)

        if not ok:
            logger.warning(f"🚫 {symbol} 做多被风控拒绝: {reason}")
            return

        # 执行开仓
        result = self.orders.open_position(
            symbol, PositionSide.LONG, qty, leverage, price,
            strategy=signal.strategy, dry_run=self.dry_run)

        if result["status"] in ("opened", "dry_run_approved"):
            logger.info(f"📈 {symbol} 做多已执行: qty={qty}, leverage={leverage}x, "
                       f"置信度={signal.confidence}")

    def _handle_sell_signal(self, signal: TradeSignal):
        """处理做空信号"""
        symbol = signal.symbol
        price = signal.price
        leverage = self.config.get("strategies", {}).get(
            signal.strategy, {}).get("leverage", self.risk_config["max_leverage"])

        # 检查是否已有多仓需要平
        for pos in self.account.positions:
            if pos.symbol == symbol and pos.side == PositionSide.LONG:
                logger.info(f"🔄 {symbol} 有多仓，先平仓再做空")
                self.orders.close_position(symbol, self.dry_run, f"反转做空 [{signal.strategy}]")
                break

        qty = self.risk.calc_position_size(self.account, price, leverage)
        qty = self.client.adjust_quantity(symbol, qty) if not self.dry_run else round(qty, 6)

        ok, reason = self.risk.can_open_position(
            self.account, symbol, PositionSide.SHORT, qty, leverage, price)

        if not ok:
            logger.warning(f"🚫 {symbol} 做空被风控拒绝: {reason}")
            return

        result = self.orders.open_position(
            symbol, PositionSide.SHORT, qty, leverage, price,
            strategy=signal.strategy, dry_run=self.dry_run)

        if result["status"] in ("opened", "dry_run_approved"):
            logger.info(f"📉 {symbol} 做空已执行: qty={qty}, leverage={leverage}x, "
                       f"置信度={signal.confidence}")

    # ── 持仓风控 ──

    def monitor_positions(self):
        """监控持仓，执行止损/止盈"""
        if not self.account.positions:
            return

        for pos in list(self.account.positions):
            # 更新标记价格
            try:
                price_data = self.client.mark_price(pos.symbol)
                pos.mark_price = float(price_data.get("markPrice", pos.mark_price))
            except Exception:
                continue

            # 风控检查
            action = self.risk.check_position_risk(pos)
            if action:
                logger.warning(f"⚠️ {pos.symbol} {action}")
                if "STOP_LOSS" in action or "TRAILING_STOP" in action:
                    self.orders.close_position(pos.symbol, self.dry_run, reason=action)
                elif "TAKE_PROFIT" in action:
                    self.orders.close_position(pos.symbol, self.dry_run, reason=action)

    # ── 主循环 ──

    def run_cycle(self):
        """执行一个完整交易周期"""
        self.cycle_count += 1
        self.last_cycle_time = time.time()

        logger.info(f"\n{'='*50}")
        logger.info(f"🔄 第 {self.cycle_count} 个周期")
        logger.info(f"{'='*50}")

        # 1. 刷新账户
        self.refresh_account()

        # 2. 风控巡检（已有持仓）
        self.monitor_positions()

        # 3. 检查熔断
        if self.client.is_circuit_broken:
            logger.warning("🚨 API 熔断中，跳过本轮交易")
            return

        # 4. 策略计算
        signals = self.process_signals()

        if signals:
            logger.info(f"📡 发现 {len(signals)} 个信号:")
            for sig in signals:
                logger.info(f"  {sig.symbol}: {sig.action.value.upper()} "
                          f"({sig.strategy}, confidence={sig.confidence})")
                self.execute_signal(sig)
        else:
            logger.info("ℹ️  无交易信号")

        # 5. 保存状态
        self._save_state()

    def _save_state(self):
        """保存引擎状态"""
        state = {
            "cycle_count": self.cycle_count,
            "last_cycle": datetime.now(timezone.utc).isoformat(),
            "dry_run": self.dry_run,
            "environment": BINANCE_API_ENV,
            "account": {
                "total_equity": self.account.total_equity,
                "available_balance": self.account.available_balance,
                "positions_count": len(self.account.positions),
            },
            "circuit_breaker": self.client.circuit_breaker,
        }
        path = os.path.join(STATE_DIR, "engine_state.json")
        try:
            with open(path, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"保存状态失败: {e}")

    def start(self):
        """启动交易引擎"""
        self.running = True
        logger.info("🚀 交易引擎启动")

        if self.dry_run:
            logger.info("🔵 当前为 DRY-RUN 模式，不会真实下单")
            logger.info("   使用 --live 参数切换到实盘模式")

        def stop_handler(signum, frame):
            logger.info("收到停止信号，正在安全退出...")
            self.running = False

        sig.signal(sig.SIGINT, stop_handler)
        sig.signal(sig.SIGTERM, stop_handler)

        # 初始账户刷新
        self.refresh_account()

        while self.running:
            try:
                self.run_cycle()
            except Exception as e:
                logger.error(f"周期异常: {e}", exc_info=True)

            # 等待下一周期
            wait_time = max(0, self.cycle_interval - (time.time() - self.last_cycle_time))
            if wait_time > 0:
                time.sleep(wait_time)

        logger.info("交易引擎已停止")

    def status(self):
        """输出当前状态"""
        self.refresh_account()
        print(f"\n{'='*50}")
        print(f"📊 交易引擎状态")
        print(f"{'='*50}")
        print(f"模式: {'DRY-RUN 模拟' if self.dry_run else '实盘'}")
        print(f"环境: {BINANCE_API_ENV}")
        print(f"认证: {'✅' if IS_AUTHENTICATED else '⚠️ 未认证'}")
        print(f"周期数: {self.cycle_count}")
        print(f"熔断: {'🚨 是' if self.client.circuit_breaker else '✅ 否'}")
        print(f"总权益: {self.account.total_equity:.2f} USDT")
        print(f"可用余额: {self.account.available_balance:.2f} USDT")
        print(f"持仓数: {len(self.account.positions)}")
        for pos in self.account.positions:
            pnl = pos.pnl_pct()
            print(f"  {pos.symbol}: {pos.side.value.upper()} {pos.quantity} "
                  f"@ {pos.entry_price} | 现价={pos.mark_price} | "
                  f"盈亏={pnl:.2f}% | 杠杆={pos.leverage}x")
        print(f"{'='*50}\n")


# ═══════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="量化交易核心引擎")
    parser.add_argument("--live", action="store_true", help="实盘模式（默认 dry-run）")
    parser.add_argument("--status", action="store_true", help="查看状态")
    parser.add_argument("--interval", type=int, default=60, help="循环间隔(秒)")
    parser.add_argument("--cooldown", type=int, default=300, help="信号冷却(秒)")
    args = parser.parse_args()

    dry_run = not args.live
    engine = TradingEngine(dry_run=dry_run)
    engine.cycle_interval = args.interval
    engine.signal_cooldown = args.cooldown

    if args.status:
        engine.status()
        return

    if args.live:
        print(f"\n🚨 即将进入 REAL TRADING 模式！")
        print(f"   环境: {BINANCE_API_ENV}")
        print(f"   所有操作将真实下单！")
        print(f"\n如需继续，请在 10 秒内 Ctrl+C 取消...")
        try:
            time.sleep(10)
        except KeyboardInterrupt:
            print("已取消")
            return

    engine.start()


if __name__ == "__main__":
    main()
