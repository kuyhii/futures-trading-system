#!/usr/bin/env python3
"""
core/engine.py - 量化交易核心引擎 V2（多策略优化版）
职责: 完整交易生命周期管理
  - 实时行情（HTTP轮询 + WebSocket备用）
  - 多策略信号 → 风控审核 → 自动下单 全闭环
  - 信号聚合：加权投票 + 冲突检测 + 多级别确认
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
import threading
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

# ── 中文翻译工具 ──
from core.notify import notifier, _side_cn, _strategy_cn

# ── K线数据管理器 ──
from core.kline_manager import KlineManager

# ── CZSC 缠论策略 ──
try:
    from core.strategies.czsc_strategy import generate_czsc_signal as _czsc_gen_signal
    CZSC_STRATEGY_AVAILABLE = True
except ImportError:
    CZSC_STRATEGY_AVAILABLE = False
    _czsc_gen_signal = None

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
        self.circuit_breaker = False
        self._circuit_cooldown_ms = 60_000  # 初始冷却 60 秒
        self._max_circuit_cooldown_ms = 900_000  # 最大冷却 15 分钟

    def _sign(self, params: dict) -> dict:
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
        if self.circuit_breaker:
            cooldown_s = self._circuit_cooldown_ms / 1000
            if time.time() - self._last_error_time < cooldown_s:
                raise RuntimeError(f"🚨 API 熔断中，等待冷却 {cooldown_s:.0f}s")
            self.circuit_breaker = False
            logger.warning(f"熔断已解除，恢复请求（上次冷却 {cooldown_s:.0f}s）")
            self._circuit_cooldown_ms = 60_000  # 重置为初始值

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
                    wait = min(2 ** attempt, 10)
                    logger.warning(f"⚠️ 429 频率限制，等待 {wait}s 后重试")
                    time.sleep(wait)
                    continue

                if resp.status_code >= 400:
                    self._consecutive_errors += 1
                    self._last_error_time = time.time()
                    if self._consecutive_errors >= 5:
                        self.circuit_breaker = True
                        # 递增冷却：60s → 120s → 240s → ... → 最大 15 分钟
                        self._circuit_cooldown_ms = min(
                            self._circuit_cooldown_ms * 2,
                            self._max_circuit_cooldown_ms
                        )
                        logger.error(f"🚨 连续 5 次错误，触发熔断！冷却 {self._circuit_cooldown_ms/1000:.0f}s")
                    body = resp.text
                    logger.error(f"API 错误 [{resp.status_code}]: {body}")
                    if attempt < retries - 1:
                        time.sleep(1)
                        continue
                    return {"error": body, "status": resp.status_code}

                self._consecutive_errors = 0
                return resp.json()

            except requests.exceptions.Timeout:
                logger.warning(f"⏳ 请求超时 (第 {attempt + 1} 次重试)")
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
    def klines(self, symbol: str, interval: str = "2m", limit: int = 100) -> List:
        return self._request("GET", "/fapi/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": limit
        })

    def ticker_price(self, symbol: str = None) -> dict:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/ticker/price", params)

    def mark_price(self, symbol: str = None) -> dict:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/premiumIndex", params)

    def funding_rate(self, symbol: str, limit: int = 10) -> List:
        return self._request("GET", "/fapi/v1/fundingRate", {
            "symbol": symbol, "limit": limit
        })

    def open_interest(self, symbol: str) -> dict:
        return self._request("GET", "/fapi/v1/openInterest", {"symbol": symbol})

    def depth(self, symbol: str, limit: int = 20) -> dict:
        return self._request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    def exchange_info(self) -> dict:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    # ── 签名接口 ──
    def account_balance(self) -> List:
        return self._request("GET", "/fapi/v2/balance", {}, signed=True)

    def account_info(self) -> dict:
        return self._request("GET", "/fapi/v2/account", {}, signed=True)

    def positions(self, symbol: str = None) -> List:
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._request("GET", "/fapi/v2/positionRisk", params, signed=True)
        if isinstance(data, list):
            return [p for p in data if float(p.get("positionAmt", 0)) != 0]
        return []

    def open_orders(self, symbol: str = None) -> List:
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/fapi/v1/openOrders", params, signed=True)

    def new_order(self, symbol: str, side: str, order_type: str,
                  quantity: float = None, price: float = None,
                  stop_price: float = None, reduce_only: bool = False,
                  close_position: bool = False, time_in_force: str = "GTC",
                  callback_rate: float = None) -> dict:
        params = {
            "symbol": symbol, "side": side, "type": order_type,
        }
        if quantity: params["quantity"] = quantity
        if price: params["price"] = price
        if stop_price: params["stopPrice"] = stop_price
        if reduce_only: params["reduceOnly"] = "true"
        if close_position: params["closePosition"] = "true"
        if time_in_force and order_type in ("LIMIT", "STOP", "TAKE_PROFIT"):
            params["timeInForce"] = time_in_force
        if callback_rate: params["callbackRate"] = callback_rate

        logger.info(f"📤 下单: {params}")
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def cancel_order(self, symbol: str, order_id: int = None, orig_client_order_id: str = None) -> dict:
        params = {"symbol": symbol}
        if order_id: params["orderId"] = order_id
        if orig_client_order_id: params["origClientOrderId"] = orig_client_order_id
        return self._request("DELETE", "/fapi/v1/order", params, signed=True)

    def cancel_all_orders(self, symbol: str) -> dict:
        return self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    def change_leverage(self, symbol: str, leverage: int) -> dict:
        return self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    def change_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        return self._request("POST", "/fapi/v1/marginType",
                             {"symbol": symbol, "marginType": margin_type}, signed=True)

    def modify_isolated_margin(self, symbol: str, amount: float, type: int = 1) -> dict:
        return self._request("POST", "/fapi/v1/positionMargin",
                             {"symbol": symbol, "amount": amount, "type": type}, signed=True)

    def income_history(self, symbol: str = None, income_type: str = None,
                       limit: int = 50, start_time: int = None) -> List:
        params: dict = {"limit": limit}
        if symbol: params["symbol"] = symbol
        if income_type: params["incomeType"] = income_type
        if start_time: params["startTime"] = start_time
        return self._request("GET", "/fapi/v1/income", params, signed=True)

    def get_order(self, symbol: str, order_id: int = None, orig_client_order_id: str = None) -> dict:
        params = {"symbol": symbol}
        if order_id: params["orderId"] = order_id
        if orig_client_order_id: params["origClientOrderId"] = orig_client_order_id
        return self._request("GET", "/fapi/v1/order", params, signed=True)

    def my_trades(self, symbol: str, limit: int = 20) -> List:
        return self._request("GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": limit}, signed=True)

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        info = self.exchange_info()
        if "symbols" not in info:
            return None
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return s
        return None

    def adjust_quantity(self, symbol: str, quantity: float) -> float:
        info = self.get_symbol_info(symbol)
        if not info:
            return quantity
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step = float(f["stepSize"])
                min_qty = float(f["minQty"])
                quantity = max(min_qty, round(quantity - (quantity % step), 10))
                break
        return quantity

    def adjust_price(self, symbol: str, price: float) -> float:
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
    highest_pnl: float = 0  # 追踪止损用，开仓时 pnl=0

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
# 技术指标
# ═══════════════════════════════════════════

class Indicators:
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
        if not candles:
            return None
        total_vp = sum(c["close"] * c["volume"] for c in candles)
        total_v = sum(c["volume"] for c in candles)
        if total_v == 0:
            return None
        return total_vp / total_v


# ═══════════════════════════════════════════
# 多策略信号引擎（V2 优化版）
# ═══════════════════════════════════════════

class StrategyEngine:
    """多策略信号引擎 V2 — 支持加权投票 + 冲突检测"""

    def __init__(self, config: dict):
        self.config = config
        self.strategies_cfg = config.get("strategies", {})
        self.indicators_cfg = config.get("indicators", {})
        self.agg_cfg = config.get("signal_aggregation", {})
        self._strategy_errors: Dict[str, int] = {}  # 记录各策略连续错误次数

    def parse_klines(self, raw: list) -> List[dict]:
        candles = []
        if not isinstance(raw, list):
            return candles
        for k in raw:
            if isinstance(k, list) and len(k) >= 6:
                candles.append({
                    "open_time": k[0], "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
                    "close_time": k[6],
                })
        return candles

    def analyze(self, candles: List[dict], symbol: str = "") -> List[TradeSignal]:
        """
        运行所有启用的策略，返回信号列表
        所有策略统一使用 2m K 线数据
        """
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
                self._strategy_errors[name] = self._strategy_errors.get(name, 0) + 1
                err_count = self._strategy_errors[name]
                logger.error(f"❌ 策略 {name} 异常 (连续 {err_count} 次): {e}")
                if err_count >= 3:
                    logger.warning(f"🔇 策略 {name} 连续错误过多，临时禁用")
                    cfg["enabled"] = False
        return signals

    def aggregate_signals(self, signals: List[TradeSignal], symbol: str) -> Optional[TradeSignal]:
        """
        V2 信号聚合：加权投票 + 冲突检测

        逻辑:
          1. 收集同一币种所有信号
          2. 检测冲突（同时有 BUY 和 SELL）
          3. 按方向分组，计算加权置信度
          4. 选择获胜方向（最高加权分）
          5. 如果多策略一致 → 提升最终置信度
        """
        if not signals:
            return None

        # 获取策略权重配置
        strategy_weights = {}
        for name, cfg in self.strategies_cfg.items():
            strategy_weights[name] = cfg.get("weight", 1.0)

        # 按方向分组
        buy_signals = [s for s in signals if s.action == SignalAction.BUY]
        sell_signals = [s for s in signals if s.action == SignalAction.SELL]

        # 冲突检测
        if buy_signals and sell_signals:
            logger.warning(f"⚡ 信号冲突({symbol}): {len(buy_signals)}个做多 vs {len(sell_signals)}个做空")
            # 分别计算加权分
            buy_score = sum(s.confidence * strategy_weights.get(s.strategy, 1.0) for s in buy_signals)
            sell_score = sum(s.confidence * strategy_weights.get(s.strategy, 1.0) for s in sell_signals)
            logger.info(f"📊 加权分: 做多={buy_score:.2f} vs 做空={sell_score:.2f}")
            if buy_score >= sell_score:
                winner_signals = buy_signals
                loser_signals = sell_signals
            else:
                winner_signals = sell_signals
                loser_signals = buy_signals
            logger.info(f"🏆 冲突解决: {'做多' if winner_signals[0].action == SignalAction.BUY else '做空'} 获胜")
        else:
            winner_signals = buy_signals or sell_signals
            loser_signals = []

        # 计算加权置信度
        total_weight = sum(strategy_weights.get(s.strategy, 1.0) for s in winner_signals)
        weighted_conf = sum(
            s.confidence * strategy_weights.get(s.strategy, 1.0) for s in winner_signals
        ) / total_weight

        # 多策略一致 → 置信度加成
        agreement_bonus = 0.0
        if len(winner_signals) >= 2:
            agreement_bonus = min(0.15, len(winner_signals) * 0.05)
            logger.info(f"🤝 {symbol} {len(winner_signals)} 个策略一致: {[s.strategy for s in winner_signals]} "
                       f"(加成 +{agreement_bonus:.0%})")

        final_confidence = min(0.95, weighted_conf + agreement_bonus)

        # 构建聚合信号
        best_signal = winner_signals[0]
        strategies_involved = [s.strategy for s in winner_signals]
        strategies_rejected = [s.strategy for s in loser_signals]

        details = {
            **best_signal.details,
            "aggregated": True,
            "strategies_agree": strategies_involved,
            "strategies_disagree": strategies_rejected,
            "raw_confidence": round(weighted_conf, 2),
            "agreement_bonus": round(agreement_bonus, 2),
            "strategy_count": len(winner_signals),
        }

        return TradeSignal(
            symbol=symbol,
            action=best_signal.action,
            strategy=",".join(strategies_involved),
            confidence=round(final_confidence, 2),
            price=best_signal.price,
            timeframe=best_signal.timeframe,
            details=details,
        )

    def _make_signal(self, action: SignalAction, strategy: str,
                     confidence: float, price: float, details: dict,
                     timeframe: str = "2m", symbol: str = "") -> TradeSignal:
        return TradeSignal(
            symbol=symbol, action=action, strategy=strategy, confidence=confidence,
            price=price, timeframe=timeframe, details=details
        )

    # ── 策略 1: 趋势跟踪 ──
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

        details = {"ma_fast": round(ma_fast, 2), "ma_slow": round(ma_slow, 2), "current_price": price}
        macd_val, macd_sig, macd_hist = Indicators.macd(closes)
        if macd_val is not None:
            details["macd"] = round(macd_val, 4)
            details["macd_signal"] = round(macd_sig, 4)
            details["macd_histogram"] = round(macd_hist, 4)
        vol_confirm = (not vol_ma) or (current_vol > vol_ma * 1.2)

        if ma_fast_prev <= ma_slow_prev and ma_fast > ma_slow:
            conf = 0.85 if (vol_confirm and macd_hist and macd_hist > 0) else 0.6
            return self._make_signal(SignalAction.BUY, "trend_follow", conf, price,
                                     {**details, "cross": "golden", "volume_confirm": vol_confirm})
        if ma_fast_prev >= ma_slow_prev and ma_fast < ma_slow:
            conf = 0.85 if (vol_confirm and macd_hist and macd_hist < 0) else 0.6
            return self._make_signal(SignalAction.SELL, "trend_follow", conf, price,
                                     {**details, "cross": "death", "volume_confirm": vol_confirm})
        return None

    # ── 策略 2: 均值回归 ──
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
                                     {**details, "reason": "oversold+below_lower"})
        if rsi > ob and price > upper:
            return self._make_signal(SignalAction.SELL, "mean_reversion", 0.7, price,
                                     {**details, "reason": "overbought+above_upper"})
        return None

    # ── 策略 3: 突破策略 ──
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
        if prev_price < highest and price >= highest:
            conf = 0.75 if vol_confirm else 0.5
            return self._make_signal(SignalAction.BUY, "breakout", conf, price,
                                     {**details, "breakout": "upper", "volume_confirm": vol_confirm})
        if prev_price > lowest and price <= lowest:
            conf = 0.75 if vol_confirm else 0.5
            return self._make_signal(SignalAction.SELL, "breakout", conf, price,
                                     {**details, "breakout": "lower", "volume_confirm": vol_confirm})
        return None

    # ── 策略 4: 资金费率（框架保留） ──
    def _strategy_funding_rate(self, candles: List[dict], cfg: dict) -> Optional[TradeSignal]:
        return None

    # ── 策略 5: 多空比反转（框架保留） ──
    def _strategy_lsr_reversal(self, candles: List[dict], cfg: dict) -> Optional[TradeSignal]:
        return None

    # ── 策略 6: CZSC 缠论策略 ──
    def _strategy_czsc(self, candles: List[dict], cfg: dict) -> Optional[TradeSignal]:
        if not CZSC_STRATEGY_AVAILABLE or _czsc_gen_signal is None:
            return None
        try:
            min_conf = cfg.get("min_confidence", 0.65)
            sig = _czsc_gen_signal(candles, min_confidence=min_conf)
            if not sig:
                return None
            action = SignalAction.BUY if sig["action"] == "BUY" else SignalAction.SELL
            return self._make_signal(
                action, "czsc", sig["confidence"], candles[-1]["close"],
                {"reason": sig["reason"], "czsc_details": sig.get("czsc_details", {})}
            )
        except Exception as e:
            logger.error(f"CZSC 策略异常: {e}")
            return None


# ═══════════════════════════════════════════
# 风控引擎
# ═══════════════════════════════════════════

class RiskEngine:
    """风控审核 + 自动止损/止盈/追踪止损"""

    def __init__(self, config: dict):
        self.cfg = config
        self.risk_rules = config.get("risk_rules", {})
        self._daily_start_equity = 0
        self._peak_equity = 0

    def set_daily_start(self, equity: float):
        self._daily_start_equity = equity
        if equity > self._peak_equity:
            self._peak_equity = equity

    def can_open_position(self, account: AccountState, symbol: str,
                          side: PositionSide, quantity: float,
                          leverage: int, price: float) -> Tuple[bool, str]:
        if len(account.positions) >= self.cfg["max_positions"]:
            return False, f"持仓数已达上限 ({len(account.positions)}/{self.cfg['max_positions']})"
        for p in account.positions:
            if p.symbol == symbol:
                return False, f"{symbol} 已有持仓，不可重复开仓"
        if leverage > self.cfg["max_leverage"]:
            return False, f"杠杆 {leverage}x 超过上限 {self.cfg['max_leverage']}x"
        # 保证金检查：使用固定保证金配置
        margin_needed_check = price * quantity / leverage
        fixed_margin = self.cfg.get("fixed_margin_usdt", 50)
        if margin_needed_check > fixed_margin * 1.01:  # 允许 1% 浮动（精度误差）
            return False, f"保证金 {margin_needed_check:.2f} 超过固定保证金 {fixed_margin} USDT"
        if self._daily_start_equity > 0:
            daily_pnl_pct = (account.total_equity - self._daily_start_equity) / self._daily_start_equity * 100
            if daily_pnl_pct < -self.cfg["daily_loss_limit_pct"]:
                return False, f"当日亏损 {daily_pnl_pct:.2f}% 已达限制 {-self.cfg['daily_loss_limit_pct']}%"
        if self._peak_equity > 0:
            drawdown = (self._peak_equity - account.total_equity) / self._peak_equity * 100
            if drawdown > self.cfg["max_drawdown_pct"]:
                return False, f"总回撤 {drawdown:.2f}% 超过限制 {self.cfg['max_drawdown_pct']}%"
        if margin_needed_check > account.available_balance * 0.9:
            return False, f"可用余额不足: 需要 {margin_needed_check:.2f}, 可用 {account.available_balance:.2f}"
        return True, "通过"

    def check_position_risk(self, position: Position) -> Optional[str]:
        if position.mark_price == 0:
            return None
        pnl = position.pnl_pct()
        if self.cfg.get("trailing_stop", False):
            if pnl > position.highest_pnl:
                position.highest_pnl = pnl
        if pnl < -self.cfg["stop_loss_pct"]:
            return f"STOP_LOSS: 亏损 {pnl:.2f}% 超过限制 -{self.cfg['stop_loss_pct']}%"
        if pnl > self.cfg["take_profit_pct"]:
            return f"TAKE_PROFIT: 盈利 {pnl:.2f}% 超过目标 +{self.cfg['take_profit_pct']}%"
        if self.cfg.get("trailing_stop") and position.highest_pnl > self.cfg["take_profit_pct"]:
            trail_distance = self.cfg.get("trailing_distance_pct", 1)
            if pnl < (position.highest_pnl - trail_distance):
                return f"TRAILING_STOP: 从最高点 {position.highest_pnl:.2f}% 回撤 {trail_distance}%"
        return None

    def calc_stop_loss(self, entry_price: float, side: PositionSide, atr: float = None) -> float:
        stop_pct = self.cfg["stop_loss_pct"] / 100
        if atr:
            stop_pct = min(stop_pct, (atr * 2 / entry_price))
        if side == PositionSide.LONG:
            return round(entry_price * (1 - stop_pct), 8)
        else:
            return round(entry_price * (1 + stop_pct), 8)

    def calc_take_profit(self, entry_price: float, side: PositionSide) -> float:
        tp_pct = self.cfg["take_profit_pct"] / 100
        if side == PositionSide.LONG:
            return round(entry_price * (1 + tp_pct), 8)
        else:
            return round(entry_price * (1 - tp_pct), 8)

    def calc_position_size(self, account: AccountState, price: float,
                           leverage: int, risk_pct: float = None) -> float:
        """
        基于固定保证金计算开仓数量
        皇上旨意：统一下单保证金为 50 USDT
        公式: 名义仓位 = 固定保证金 × 杠杆
              数量 = 名义仓位 / 价格
        """
        # 固定保证金 50 USDT
        fixed_margin = self.cfg.get("fixed_margin_usdt", 50)
        # 名义仓位 = 保证金 × 杠杆
        nominal = fixed_margin * leverage
        # 数量 = 名义仓位 / 价格
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
        self._pending_orders: Dict[str, dict] = {}

    def open_position(self, symbol: str, side: PositionSide,
                      quantity: float, leverage: int, price: float,
                      strategy: str = "unknown", dry_run: bool = False,
                      account: AccountState = None) -> dict:
        result = {"symbol": symbol, "side": side.value, "quantity": quantity,
                  "leverage": leverage, "status": "pending", "dry_run": dry_run}
        if dry_run:
            sl = self.risk.calc_stop_loss(price, side)
            tp = self.risk.calc_take_profit(price, side)
            margin_needed = round(price * quantity / leverage, 4)
            if account is not None:
                position = Position(
                    symbol=symbol, side=side, quantity=quantity, entry_price=price,
                    leverage=leverage, mark_price=price, unrealized_pnl=0,
                    margin=margin_needed, signal_strategy=strategy,
                    entry_time=datetime.now(timezone.utc).isoformat(),
                    stop_loss_price=sl, take_profit_price=tp, highest_pnl=0,
                )
                account.positions.append(position)
                account.available_balance = max(0, account.available_balance - margin_needed)
                logger.info(f"🔵 DRY-RUN 开仓: {side.value.upper()} {symbol} {quantity} @ {price} "
                           f"(杠杆:{leverage}x, 止损:{sl}, 止盈:{tp}, 保证金:{margin_needed})")
            else:
                logger.info(f"🔵 DRY-RUN 开仓信号: {side.value.upper()} {symbol} {quantity} @ {price} "
                           f"(杠杆:{leverage}x, 止损:{sl}, 止盈:{tp})")
            result["status"] = "dry_run_approved"
            result["stop_loss"] = sl
            result["take_profit"] = tp
            result["margin_needed"] = margin_needed
            self._record_trade("open", symbol, side.value, quantity, price, strategy, "DRY-RUN")
            try:
                notifier.trade_opened(symbol, side.value, quantity, price,
                                     leverage, strategy, sl, tp, dry_run=True)
            except Exception as e:
                logger.warning(f"通知发送失败: {e}")
            return result

        try:
            self.client.change_leverage(symbol, leverage)
            time.sleep(0.5)
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
            result["fill_price"] = float(order.get("avgPrice", 0) or order.get("price", 0) or price)
            result["executed_qty"] = float(order.get("executedQty", qty))
            logger.info(f"✅ 开仓成功: {side.value.upper()} {symbol} {qty} @ {result['fill_price']}")
            
            # 先计算止损止盈，再发送通知
            sl = self.risk.calc_stop_loss(result["fill_price"], side)
            tp = self.risk.calc_take_profit(result["fill_price"], side)
            result["stop_loss"] = sl
            result["take_profit"] = tp
            self._place_sl_tp(symbol, side, sl, tp)
            
            try:
                notifier.trade_opened(symbol, side.value, quantity, result['fill_price'],
                                     leverage, strategy, sl, tp, dry_run=False)
            except Exception as e:
                logger.warning(f"通知发送失败: {e}")
            
            self._record_trade("open", symbol, side.value, quantity,
                              result["fill_price"], strategy, "OK")
            return result
        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            logger.error(f"❌ 开仓异常: {e}")
            return result

    def close_position(self, symbol: str, dry_run: bool = False,
                       reason: str = "", account: AccountState = None,
                       client: BinanceClient = None) -> dict:
        result = {"symbol": symbol, "status": "pending", "dry_run": dry_run, "reason": reason}
        if dry_run:
            if account is None:
                result["status"] = "dry_run_approved"
                logger.info(f"🔵 DRY-RUN 平仓信号: {symbol} (原因: {reason})")
                self._record_trade("close", symbol, "unknown", 0, 0, reason, "DRY-RUN")
                return result
            pos = None
            pos_idx = -1
            for i, p in enumerate(account.positions):
                if p.symbol == symbol:
                    pos = p
                    pos_idx = i
                    break
            if pos is None:
                result["status"] = "no_position"
                logger.info(f"🔵 DRY-RUN 平仓: {symbol} 无虚拟持仓")
                return result
            exit_price = pos.mark_price
            if client is not None and exit_price == 0:
                try:
                    price_data = client.ticker_price(symbol)
                    exit_price = float(price_data.get("price", pos.entry_price))
                except Exception:
                    exit_price = pos.entry_price
            direction = 1 if pos.side == PositionSide.LONG else -1
            pnl = (exit_price - pos.entry_price) / pos.entry_price * pos.quantity * pos.leverage * direction
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100 * direction
            account.available_balance += pos.margin + pnl
            account.positions.pop(pos_idx)
            result["status"] = "dry_run_closed"
            result["fill_price"] = exit_price
            result["pnl"] = round(pnl, 4)
            result["pnl_pct"] = round(pnl_pct, 4)
            result["entry_price"] = pos.entry_price
            emoji = "✅" if pnl > 0 else "❌"
            logger.info(f"🔵 DRY-RUN {emoji} 平仓: {symbol} {pos.side.value.upper()} "
                       f"{pos.quantity} @ {exit_price:.4f} "
                       f"(入场:{pos.entry_price}, 盈亏:{pnl:+.4f} USDT / {pnl_pct:+.2f}%, 原因: {reason})")
            self._record_trade("close", symbol, pos.side.value, pos.quantity,
                              exit_price, reason, f"DRY-RUN pnl={pnl:+.4f}")
            try:
                notifier.trade_closed(symbol, pos.side.value, pos.quantity,
                                     pos.entry_price, exit_price, pnl, pnl_pct, reason, dry_run=True)
            except Exception as e:
                logger.warning(f"通知发送失败: {e}")
            return result

        try:
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
            try:
                entry_price = float(pos.get("entryPrice", 0))
                pnl_pct_val = (result["fill_price"] - entry_price) / entry_price * 100 * (1 if pos_amt > 0 else -1)
                notifier.trade_closed(symbol, "long" if pos_amt > 0 else "short", qty,
                                     entry_price, result["fill_price"], result["pnl"], pnl_pct_val,
                                     reason, dry_run=False)
            except Exception as e:
                logger.warning(f"通知发送失败: {e}")
            return result
        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            logger.error(f"❌ 平仓异常: {e}")
            return result

    def _place_sl_tp(self, symbol: str, side: PositionSide,
                     sl_price: float, tp_price: float):
        """
        设置止损和止盈
        币安已迁移止损止盈到 Algo Order API (fapi/v1/algoOrder)
        """
        try:
            close_side = "SELL" if side == PositionSide.LONG else "BUY"
            sl_price = self.client.adjust_price(symbol, sl_price)
            tp_price = self.client.adjust_price(symbol, tp_price)
            
            # 使用 Algo Order API 设置止损
            sl_params = {
                "symbol": symbol,
                "side": close_side,
                "positionSide": "BOTH",
                "type": "STOP_MARKET",
                "stopPrice": sl_price,
                "closePosition": "true",
                "workingType": "CONTRACT_PRICE",
            }
            sl_result = self.client._request("POST", "/fapi/v1/algoOrder", sl_params)
            if "code" in sl_result and sl_result["code"] != 200:
                logger.warning(f"⚠️ 止损设置失败: {sl_result.get('msg', sl_result)}")
            else:
                logger.info(f"🛡 止损已设置: {sl_price}")
            
            # 使用 Algo Order API 设置止盈
            tp_params = {
                "symbol": symbol,
                "side": close_side,
                "positionSide": "BOTH",
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": tp_price,
                "closePosition": "true",
                "workingType": "CONTRACT_PRICE",
            }
            tp_result = self.client._request("POST", "/fapi/v1/algoOrder", tp_params)
            if "code" in tp_result and tp_result["code"] != 200:
                logger.warning(f"⚠️ 止盈设置失败: {tp_result.get('msg', tp_result)}")
            else:
                logger.info(f"🎯 止盈已设置: {tp_price}")
        except Exception as e:
            logger.error(f"设置止损止盈异常: {e}")

    def _record_trade(self, action: str, symbol: str, side: str,
                      quantity: float, price: float, strategy: str, result: str):
        history_file = os.path.join(STATE_DIR, "trade_history.json")
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action, "symbol": symbol, "side": side,
            "quantity": quantity, "price": price,
            "strategy": strategy, "result": result,
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
# 主交易引擎 V2
# ═══════════════════════════════════════════

class TradingEngine:
    """主交易引擎 V2 — 多策略聚合 + 风控独立巡检"""

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
        self.cycle_interval = 60

        # ── 动态品种池定时更新 ──
        self._last_symbols_update = 0.0
        self._symbols_update_interval = 86400  # 24h

        self.risk_interval = 15
        self._risk_thread: Optional[threading.Thread] = None
        self._risk_stop_event = threading.Event()

        # 信号置信度阈值（与风控文档一致：≥0.7）
        self.min_confidence = 0.7

        self._kline_cache: Dict[str, List[dict]] = {}
        self._last_signal_time: Dict[str, float] = {}
        self.signal_cooldown = 300

        # ── 线程锁：保护共享数据 ──
        self._account_lock = threading.Lock()      # 保护 self.account
        self._kline_cache_lock = threading.Lock()  # 保护 self._kline_cache
        self._signal_time_lock = threading.Lock()  # 保护 self._last_signal_time

        # ── K线数据管理器（本地采集+合成） ──
        self.symbols_list = [s["symbol"] for s in self.symbols_config]
        self.kline_manager = KlineManager(
            symbols=self.symbols_list,
            api_key=BINANCE_API_KEY,
            api_secret=BINANCE_SECRET_KEY,
            base_url=BASE_URL,
        )
        logger.info("📊 K线数据管理器已初始化（本地采集+合成模式）")

        # ── 策略自检 ──
        self._strategy_status: Dict[str, dict] = {}

        logger.info(f"🚀 交易引擎 V2 初始化 {'[DRY-RUN 模拟模式]' if dry_run else '[实盘模式]'}")
        logger.info(f"  环境: {BINANCE_API_ENV} | 认证: {'✅ 已认证' if IS_AUTHENTICATED else '⚠️ 未认证'}")
        logger.info(f"  监控币种: {[s['symbol'] for s in self.symbols_config]}")
        logger.info(f"  时间框架: {self.config['timeframe']}")
        logger.info(f"  活跃策略: {self.config.get('active', [])}")
        logger.info(f"  风控巡检频率: {self.risk_interval}s | 最低信号置信度: {self.min_confidence}")

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

    def reload_symbols(self) -> int:
        """
        从 config/symbols.json 重新加载交易品种池（支持动态更新）
        返回: 加载的 enabled 币种数量
        """
        old_symbols = [s["symbol"] for s in self.symbols_config]
        self.symbols_config = self._load_symbols()
        new_symbols = [s["symbol"] for s in self.symbols_config]

        added = [s for s in new_symbols if s not in old_symbols]
        removed = [s for s in old_symbols if s not in new_symbols]

        if added:
            logger.info(f"✅ 品种池新增: {added}")
        if removed:
            logger.info(f"🗑️ 品种池移除: {removed}")
        if not added and not removed:
            logger.info("🔄 品种池无变化")

        # 同步更新 kline_manager 的品种列表
        self.symbols_list = new_symbols
        self.kline_manager.update_symbols(new_symbols)

        logger.info(f"  当前监控币种: {new_symbols} ({len(new_symbols)} 个)")
        return len(new_symbols)

    def _update_symbols_pool(self):
        """
        调用外部脚本更新动态品种池，然后重新加载
        该方法由主循环每日自动调用
        """
        import subprocess
        script_path = os.path.join(ROOT, "scripts", "update_symbols_pool.py")
        # 脚本 V2 默认模式：同时获取实盘+模拟盘数据进行双验证
        cmd_args = [sys.executable, script_path]

        logger.info("🔄 开始每日品种池自动更新...")
        result = subprocess.run(
            cmd_args,
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            logger.info("✅ 品种池脚本执行成功")
            self.reload_symbols()
        else:
            logger.error(f"❌ 品种池脚本执行失败 (exit={result.returncode})")
            logger.error(f"stderr: {result.stderr[:500]}")

    def self_check_strategies(self) -> dict:
        """
        系统启动时策略自检
        检查每个策略是否可用，记录状态
        """
        logger.info("🔍 开始策略自检...")
        active = self.config.get("active", [])
        for name in active:
            cfg = self.config.get("strategies", {}).get(name, {})
            enabled = cfg.get("enabled", False)
            available = False
            status = "未启用"

            if enabled:
                if name in ("trend_follow", "mean_reversion", "breakout"):
                    available = True
                    status = "✅ 就绪"
                elif name == "czsc":
                    available = CZSC_STRATEGY_AVAILABLE
                    status = "✅ 就绪" if available else "❌ CZSC 库不可用"
                elif name in ("funding_rate", "lsr_reversal"):
                    available = False
                    status = "⏸️ 待实现"

            self._strategy_status[name] = {
                "enabled": enabled,
                "available": available,
                "status": status,
                "errors": 0,
            }
            logger.info(f"  {name}: {status}")

        summary = {
            "total": len(active),
            "enabled": sum(1 for s in self._strategy_status.values() if s["enabled"]),
            "available": sum(1 for s in self._strategy_status.values() if s["available"]),
        }
        logger.info(f"📋 自检完成: {summary['enabled']}/{summary['total']} 启用, "
                    f"{summary['available']}/{summary['total']} 就绪")
        return summary

    def refresh_account(self):
        if not IS_AUTHENTICATED:
            self.account.total_equity = 10000
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
            raw_positions = self.client.positions()
            with self._account_lock:
                self.account.positions = []
                for p in raw_positions:
                    amt = float(p["positionAmt"])
                    if amt == 0:
                        continue
                    pos = Position(
                        symbol=p["symbol"], side=PositionSide.LONG if amt > 0 else PositionSide.SHORT,
                        quantity=abs(amt), entry_price=float(p["entryPrice"]),
                        leverage=int(p["leverage"]), unrealized_pnl=float(p["unRealizedProfit"]),
                        mark_price=float(p["markPrice"]),
                        liquidation_price=float(p["liquidationPrice"]),
                        entry_time=datetime.now(timezone.utc).isoformat(),
                    )
                    self.account.positions.append(pos)
            if self.risk._daily_start_equity == 0:
                self.risk.set_daily_start(self.account.total_equity)
            self.risk._peak_equity = max(self.risk._peak_equity, self.account.total_equity)
            logger.info(f"💰 账户: 权益={self.account.total_equity:.2f} | "
                       f"可用={self.account.available_balance:.2f} | "
                       f"未实现盈亏={self.account.unrealized_pnl:.2f} | "
                       f"持仓={len(self.account.positions)}")
        except Exception as e:
            logger.error(f"刷新账户状态失败: {e}")

    def fetch_klines(self, symbol: str, timeframe: str = None) -> List[dict]:
        """
        从本地 K 线管理器读取数据
        - 1m: 从本地 1m JSONL 读取，不足时从 API 补充
        - 2m: 从本地 2m JSONL 读取（由 1m 合成）
        - 其他时间框架: 仍从 API 获取（兼容 CZSC 的 15m 确认）
        """
        tf = timeframe or self.config["timeframe"]
        cache_key = f"{symbol}_{tf}"

        # 1m 和 2m 走本地数据管理器
        if tf in ("1m", "2m"):
            try:
                if tf == "1m":
                    raw_klines = self.kline_manager.get_klines_1m(symbol, limit=200)
                else:
                    raw_klines = self.kline_manager.get_klines_2m(symbol, limit=200)

                if raw_klines:
                    self._kline_cache[cache_key] = raw_klines
                    return raw_klines
                else:
                    logger.warning(f"本地 {tf} K 线为空 {symbol}，尝试从 API 回退")
            except Exception as e:
                logger.warning(f"本地 {tf} K 线读取失败 {symbol}: {e}，从 API 回退")

        # 回退：从 API 获取（兼容 15m 等其他时间框架）
        try:
            raw = self.client.klines(symbol, tf, 200)
            if isinstance(raw, dict) and "error" in raw:
                logger.warning(f"K 线获取失败 {symbol}: {raw['error']}")
                return self._kline_cache.get(cache_key, [])
            candles = self.strategy.parse_klines(raw)
            if candles:
                self._kline_cache[cache_key] = candles
            return candles
        except Exception as e:
            logger.error(f"K 线获取异常 {symbol}: {e}")
            return self._kline_cache.get(cache_key, [])

    def get_current_price(self, symbol: str) -> float:
        try:
            data = self.client.ticker_price(symbol)
            return float(data.get("price", 0))
        except Exception:
            candles = self._kline_cache.get(f"{symbol}_{self.config['timeframe']}", [])
            if candles:
                return candles[-1]["close"]
            return 0

    def process_signals(self) -> List[TradeSignal]:
        """
        V2 信号处理:
          1. 每个币种获取 2m K 线（本地数据）
          2. 所有策略运行
          3. 信号聚合（加权投票 + 冲突检测）
          4. 过滤低置信度
        """
        final_signals = []

        for sym_cfg in self.symbols_config:
            symbol = sym_cfg["symbol"]
            logger.info(f"📡 分析 {symbol}...")

            # 获取 2m K 线（本地数据管理器）
            candles_2m = self.fetch_klines(symbol, "2m")

            if not candles_2m:
                logger.warning(f"  {symbol}: 2m K 线数据为空，跳过")
                continue

            # 运行所有策略
            signals = self.strategy.analyze(candles_2m, symbol=symbol)

            # 记录各策略信号状态
            strategy_names = [n for n, c in self.config.get("strategies", {}).items() if c.get("enabled")]
            signaled = set(s.strategy for s in signals)
            silent = [n for n in strategy_names if n not in signaled and n in ("trend_follow", "mean_reversion", "breakout", "czsc")]
            if signals:
                logger.info(f"  {symbol}: {len(signals)} 个策略发出信号 → {[s.strategy for s in signals]}")
            if silent:
                logger.debug(f"  {symbol}: 无信号策略 → {silent}")

            if signals:
                # 信号聚合
                aggregated = self.strategy.aggregate_signals(signals, symbol)
                if aggregated:
                    logger.info(f"  {symbol} {_side_cn(aggregated.action.value)} "
                               f"(置信度={aggregated.confidence:.0%}, "
                               f"策略={_strategy_cn(aggregated.strategy)})")
                    final_signals.append(aggregated)
                else:
                    logger.info(f"  {symbol}: 信号聚合后无有效结果")

            # 记录 CZSC 分析结果
            czsc_signals = [s for s in signals if s.strategy == "czsc"]
            if czsc_signals:
                czsc = czsc_signals[0]
                details = czsc.details.get("czsc_details", {})
                logger.info(f"  {symbol} CZSC: bi_dir={details.get('bi_direction', '?')}, "
                           f"trend={details.get('trend_bias', '?')}, "
                           f"fx={details.get('fx_type', '?')}, "
                           f"decision={details.get('decision_dir', '?')}, "
                           f"conf={czsc.confidence:.0%}")

        # 全局过滤低置信度
        filtered = [s for s in final_signals if s.confidence >= self.min_confidence]
        low_conf = [s for s in final_signals if s.confidence < self.min_confidence]
        
        logger.info(f"📊 过滤完成: 通过 {len(filtered)} 个, 过滤 {len(low_conf)} 个")
        
        if low_conf:
            for s in low_conf:
                logger.info(f"🔽 {s.symbol} {_side_cn(s.action.value)} 置信度 {s.confidence:.0%} "
                           f"低于阈值 {self.min_confidence:.0%}，已过滤")

        return filtered

    def execute_signal(self, signal: TradeSignal):
        symbol = signal.symbol
        action = signal.action

        now = time.time()
        with self._signal_time_lock:
            last = self._last_signal_time.get(symbol, 0)
        if now - last < self.signal_cooldown:
            logger.debug(f"⏳ {symbol} 信号冷却中，跳过（{now - last:.0f}s < {self.signal_cooldown}s）")
            return

        logger.info(f"▶️ 执行信号: {symbol} {_side_cn(action.value)} ({_strategy_cn(signal.strategy)}, {signal.confidence:.0%})")

        if action == SignalAction.BUY:
            self._handle_buy_signal(signal)
        elif action == SignalAction.SELL:
            self._handle_sell_signal(signal)

        with self._signal_time_lock:
            self._last_signal_time[symbol] = now

    def _handle_buy_signal(self, signal: TradeSignal):
        symbol = signal.symbol
        price = signal.price
        # 从配置中获取该策略的杠杆
        leverages = [self.config.get("strategies", {}).get(s, {}).get("leverage", self.risk_config["max_leverage"])
                     for s in signal.strategy.split(",")]
        leverage = max(leverages)  # 取最大

        for pos in self.account.positions:
            if pos.symbol == symbol and pos.side == PositionSide.SHORT:
                logger.info(f"🔄 {symbol} 有空仓，先平仓再做多")
                self.orders.close_position(symbol, self.dry_run,
                                           reason=f"反转做多 [{signal.strategy}]",
                                           account=self.account, client=self.client)
                break

        qty = self.risk.calc_position_size(self.account, price, leverage)
        qty = self.client.adjust_quantity(symbol, qty) if not self.dry_run else round(qty, 6)

        ok, reason = self.risk.can_open_position(
            self.account, symbol, PositionSide.LONG, qty, leverage, price)
        if not ok:
            logger.warning(f"🚫 {symbol} 做多被风控拒绝: {reason}")
            try:
                notifier.risk_warning(symbol, f"做多被拒绝: {reason}", level="warning")
            except Exception:
                pass
            return

        result = self.orders.open_position(
            symbol, PositionSide.LONG, qty, leverage, price,
            strategy=signal.strategy, dry_run=self.dry_run, account=self.account)
        if result["status"] in ("opened", "dry_run_approved"):
            logger.info(f"📈 {symbol} 做多已执行: qty={qty}, leverage={leverage}x, "
                       f"置信度={signal.confidence}")

    def _handle_sell_signal(self, signal: TradeSignal):
        symbol = signal.symbol
        price = signal.price
        leverages = [self.config.get("strategies", {}).get(s, {}).get("leverage", self.risk_config["max_leverage"])
                     for s in signal.strategy.split(",")]
        leverage = max(leverages)

        for pos in self.account.positions:
            if pos.symbol == symbol and pos.side == PositionSide.LONG:
                logger.info(f"🔄 {symbol} 有多仓，先平仓再做空")
                self.orders.close_position(symbol, self.dry_run,
                                           reason=f"反转做空 [{signal.strategy}]",
                                           account=self.account, client=self.client)
                break

        qty = self.risk.calc_position_size(self.account, price, leverage)
        qty = self.client.adjust_quantity(symbol, qty) if not self.dry_run else round(qty, 6)

        ok, reason = self.risk.can_open_position(
            self.account, symbol, PositionSide.SHORT, qty, leverage, price)
        if not ok:
            logger.warning(f"🚫 {symbol} 做空被风控拒绝: {reason}")
            try:
                notifier.risk_warning(symbol, f"做空被拒绝: {reason}", level="warning")
            except Exception:
                pass
            return

        result = self.orders.open_position(
            symbol, PositionSide.SHORT, qty, leverage, price,
            strategy=signal.strategy, dry_run=self.dry_run, account=self.account)
        if result["status"] in ("opened", "dry_run_approved"):
            logger.info(f"📉 {symbol} 做空已执行: qty={qty}, leverage={leverage}x, "
                       f"置信度={signal.confidence}")

    def monitor_positions(self):
        with self._account_lock:
            positions = list(self.account.positions)
        if not positions:
            return
        for pos in positions:
            try:
                price_data = self.client.mark_price(pos.symbol)
                pos.mark_price = float(price_data.get("markPrice", pos.mark_price))
            except Exception:
                continue
            action = self.risk.check_position_risk(pos)
            if action:
                logger.warning(f"⚠️ {pos.symbol} {action}")
                risk_level = "critical" if "STOP_LOSS" in action else "warning"
                try:
                    notifier.risk_warning(pos.symbol, action, level=risk_level)
                except Exception:
                    pass
                if any(kw in action for kw in ("STOP_LOSS", "TRAILING_STOP", "TAKE_PROFIT")):
                    self.orders.close_position(pos.symbol, self.dry_run, reason=action,
                                               account=self.account, client=self.client)

    def run_cycle(self):
        self.cycle_count += 1
        self.last_cycle_time = time.time()
        logger.info(f"\n{'='*50}")
        logger.info(f"🔄 第 {self.cycle_count} 个周期 (策略数: {len([s for s in self.config.get('active', []) if self.config.get('strategies',{}).get(s,{}).get('enabled')])})")
        logger.info(f"{'='*50}")

        self.refresh_account()

        if self.client.is_circuit_broken:
            logger.warning("🚨 API 熔断中，跳过本轮交易")
            return

        signals = self.process_signals()

        logger.info(f"📊 process_signals 返回: {len(signals)} 个信号")

        if signals:
            logger.info(f"📝 发现 {len(signals)} 个聚合信号:")
            for sig in signals:
                logger.info(f"  {sig.symbol}: {_side_cn(sig.action.value)} "
                          f"({_strategy_cn(sig.strategy)}, 置信度={sig.confidence:.0%})")
                self.execute_signal(sig)
        else:
            logger.info("ℹ️  无交易信号")

        self._save_state()

    def _save_state(self):
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
            "strategy_status": self._strategy_status,
        }
        path = os.path.join(STATE_DIR, "engine_state.json")
        try:
            with open(path, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"保存状态失败: {e}")

    def _risk_monitor_loop(self):
        logger.info(f"🛡 风控巡检线程启动（每 {self.risk_interval}s）")
        while not self._risk_stop_event.is_set():
            try:
                with self._account_lock:
                    has_positions = bool(self.account.positions)
                if has_positions:
                    self.refresh_account()
                    self.monitor_positions()
                else:
                    logger.debug("🛡 风控巡检：无持仓，跳过")
            except Exception as e:
                logger.error(f"风控巡检异常: {e}")
            self._risk_stop_event.wait(self.risk_interval)
        logger.info("🛡 风控巡检线程已停止")

    def start(self):
        self.running = True
        logger.info("🚀 交易引擎 V2 启动")

        if self.dry_run:
            logger.info("🔵 当前为 DRY-RUN 模式，不会真实下单")
            logger.info("   使用 --live 参数切换到实盘模式")

        def stop_handler(signum, frame):
            logger.info("收到停止信号，正在安全退出...")
            self.running = False
            self._risk_stop_event.set()
            self.kline_manager.stop()

        sig.signal(sig.SIGINT, stop_handler)
        sig.signal(sig.SIGTERM, stop_handler)

        # 策略自检
        self.self_check_strategies()

        self.refresh_account()

        # ── K线数据管理器初始化 ──
        logger.info("📥 初始化 K 线数据管理器...")
        try:
            self.kline_manager.initial_load()
        except Exception as e:
            logger.error(f"K 线初始加载失败: {e}")

        # 启动后台更新线程（每 60 秒更新所有品种 1m K 线）
        self.kline_manager.start_background_update(interval=60)
        # 启动后台清理线程（每 12 小时清理超过 24 小时的数据，缠论策略需要更长历史）
        self.kline_manager.start_background_cleanup(interval=43200, max_age_hours=24.0)

        self._risk_thread = threading.Thread(target=self._risk_monitor_loop, daemon=True)
        self._risk_thread.start()

        while self.running:
            try:
                self.run_cycle()
            except Exception as e:
                logger.error(f"周期异常: {e}", exc_info=True)

            # ── 每日动态品种池更新 ──
            now = time.time()
            if now - self._last_symbols_update >= self._symbols_update_interval:
                try:
                    self._update_symbols_pool()
                    self._last_symbols_update = now
                except Exception as e:
                    logger.error(f"品种池更新失败: {e}", exc_info=True)

            wait_time = max(0, self.cycle_interval - (time.time() - self.last_cycle_time))
            if wait_time > 0:
                time.sleep(wait_time)

        self._risk_stop_event.set()
        if self._risk_thread and self._risk_thread.is_alive():
            self._risk_thread.join(timeout=5)

        # 停止 K 线管理器后台线程
        self.kline_manager.stop()

        logger.info("交易引擎已停止")

    def status(self):
        self.refresh_account()
        print(f"\n{'='*50}")
        print(f"📊 交易引擎 V2 状态")
        print(f"{'='*50}")
        print(f"模式: {'DRY-RUN 模拟' if self.dry_run else '实盘'}")
        print(f"环境: {BINANCE_API_ENV}")
        print(f"认证: {'✅ 已认证' if IS_AUTHENTICATED else '⚠️ 未认证'}")
        print(f"周期数: {self.cycle_count}")
        print(f"熔断: {'🚨 是' if self.client.circuit_breaker else '✅ 否'}")
        print(f"总权益: {self.account.total_equity:.2f} USDT")
        print(f"可用余额: {self.account.available_balance:.2f} USDT")
        print(f"持仓数: {len(self.account.positions)}")
        for pos in self.account.positions:
            pnl = pos.pnl_pct()
            print(f"  {pos.symbol}: {pos.side.value.upper()} {pos.quantity} "
                  f"@ {pos.entry_price} | 现价={pos.mark_price} | "
                  f"盈亏={pnl:.2f}% | 杠杆={pos.leverage}x | 策略={pos.signal_strategy}")
        print(f"\n策略状态:")
        for name, info in self._strategy_status.items():
            print(f"  {name}: {info['status']} (启用={info['enabled']}, 错误={info['errors']})")
        print(f"{'='*50}\n")


# ═══════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="量化交易核心引擎 V2")
    parser.add_argument("--live", action="store_true", help="实盘模式（默认 dry-run）")
    parser.add_argument("--status", action="store_true", help="查看状态")
    parser.add_argument("--self-check", action="store_true", help="仅执行策略自检")
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

    if args.self_check:
        engine.self_check_strategies()
        return

    if args.live:
        print(f"\n🚨 即将进入实盘交易模式！")
        print(f"   环境: {BINANCE_API_ENV}")
        print(f"   所有操作将真实下单！")
        print(f"\n如需取消，请在 10 秒内 Ctrl+C...")
        try:
            time.sleep(10)
        except KeyboardInterrupt:
            print("已取消")
            return

    engine.start()


if __name__ == "__main__":
    main()
