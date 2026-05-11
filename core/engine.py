#!/usr/bin/env python3
"""
core/engine.py — 量化交易主引擎（模块化编排器）

职责: 模块初始化 + 交易循环编排 + 状态管理
所有功能已拆分为独立模块：
  - binance_client.py  → API 客户端
  - models.py          → 数据模型
  - indicators.py      → 技术指标
  - strategy_engine.py → 策略引擎（趋势跟踪 + 均值回归 + 突破）
  - risk_engine.py     → 风控引擎
  - order_manager.py   → 订单管理
  - notify.py          → 通知模块
  - kline_manager.py   → K线管理器

使用方式:
  python3 -m core.engine              # 启动自动交易
  python3 -m core.engine --env prod   # 临时切换实盘模式
  python3 -m core.engine --status     # 查看状态
"""

import json
import os
import sys
import time
import threading
import logging
import signal as sig
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any, Tuple

# ── 路径 ──
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")
LOGS_DIR = os.path.join(ROOT, "logs")
STATE_DIR = os.path.join(ROOT, "state")

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

# ── 导入模块 ──
from core.notify import notifier, _side_cn, _strategy_cn
from core.kline_manager import KlineManager
from core.binance_client import BinanceClient
from core.models import SignalAction, PositionSide, TradeSignal, Position, AccountState
from core.strategy_engine import StrategyEngine
from core.risk_engine import RiskEngine
from core.order_manager import OrderManager

# ── 环境配置 ──
BINANCE_API_ENV = os.environ.get("BINANCE_API_ENV", "testnet")

if BINANCE_API_ENV == "prod":
    BINANCE_API_KEY = os.environ.get("BINANCE_PROD_API_KEY", "")
    BINANCE_SECRET_KEY = os.environ.get("BINANCE_PROD_SECRET_KEY", "")
else:
    BINANCE_API_KEY = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    BINANCE_SECRET_KEY = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "")

BASE_URLS = {
    "prod":    "https://fapi.binance.com",
    "testnet": "https://testnet.binancefuture.com",
}
BASE_URL = BASE_URLS.get(BINANCE_API_ENV, BASE_URLS["testnet"])
STREAM_URL = "fstream.binance.com" if BINANCE_API_ENV == "prod" else "stream.binancefuture.com"

MODE_LABELS = {
    "prod": "实盘模式（真金白银）",
    "testnet": "测试网模式（模拟实盘）",
}
MODE_LABEL = MODE_LABELS.get(BINANCE_API_ENV, MODE_LABELS["testnet"])
IS_AUTHENTICATED = bool(BINANCE_API_KEY and BINANCE_SECRET_KEY)

# ── 日志 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, "engine.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("engine")


class TradingEngine:
    """主交易引擎 — 模块化编排器"""

    def __init__(self):
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

        self._last_symbols_update = 0.0
        self._symbols_update_interval = 86400  # 24h

        self.risk_interval = 15
        self._risk_thread: Optional[threading.Thread] = None
        self._risk_stop_event = threading.Event()

        self.min_confidence = 0.7

        self._kline_cache: Dict[str, List[dict]] = {}
        self._last_signal_time: Dict[str, float] = {}
        self.signal_cooldown = 300

        self._account_lock = threading.Lock()
        self._kline_cache_lock = threading.Lock()
        self._signal_time_lock = threading.Lock()

        self.symbols_list = [s["symbol"] for s in self.symbols_config]
        self.kline_manager = KlineManager(
            symbols=self.symbols_list,
            api_key=BINANCE_API_KEY,
            api_secret=BINANCE_SECRET_KEY,
            base_url=BASE_URL,
        )
        logger.info("📊 K线数据管理器已初始化")

        self._strategy_status: Dict[str, dict] = {}

        logger.info(f"🚀 交易引擎初始化 [{MODE_LABEL}]")
        logger.info(f"  环境: {BINANCE_API_ENV} | API: {BASE_URL} | 认证: {'✅' if IS_AUTHENTICATED else '⚠️'}")
        logger.info(f"  监控币种: {[s['symbol'] for s in self.symbols_config]}")
        logger.info(f"  活跃策略: {self.config.get('active', [])}")
        logger.info(f"  风控巡检: {self.risk_interval}s | 置信度阈值: {self.min_confidence}")

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
        old_symbols = [s["symbol"] for s in self.symbols_config]
        self.symbols_config = self._load_symbols()
        new_symbols = [s["symbol"] for s in self.symbols_config]
        added = [s for s in new_symbols if s not in old_symbols]
        removed = [s for s in old_symbols if s not in new_symbols]
        if added: logger.info(f"✅ 品种池新增: {added}")
        if removed: logger.info(f"🗑️ 品种池移除: {removed}")
        if not added and not removed: logger.info("🔄 品种池无变化")
        self.symbols_list = new_symbols
        self.kline_manager.update_symbols(new_symbols)
        logger.info(f"  当前监控币种: {new_symbols} ({len(new_symbols)} 个)")
        return len(new_symbols)

    def _update_symbols_pool(self):
        import subprocess
        script_path = os.path.join(ROOT, "scripts", "update_symbols_pool.py")
        cmd_args = [sys.executable, script_path]
        logger.info("🔄 开始每日品种池自动更新...")
        result = subprocess.run(cmd_args, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            logger.info("✅ 品种池脚本执行成功")
            self.reload_symbols()
        else:
            logger.error(f"❌ 品种池脚本执行失败 (exit={result.returncode})")
            logger.error(f"stderr: {result.stderr[:500]}")

    def self_check_strategies(self) -> dict:
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
            self._strategy_status[name] = {
                "enabled": enabled, "available": available,
                "status": status, "errors": 0,
            }
            logger.info(f"  {name}: {status}")
        summary = {
            "total": len(active),
            "enabled": sum(1 for s in self._strategy_status.values() if s["enabled"]),
            "available": sum(1 for s in self._strategy_status.values() if s["available"]),
        }
        logger.info(f"📋 自检完成: {summary['enabled']}/{summary['total']} 启用")
        return summary

    def refresh_account(self):
        if not IS_AUTHENTICATED:
            self.account.total_equity = 10000
            self.account.available_balance = 10000
            self.account.positions = []
            return
        try:
            acc_info = self.client.account_info()
            self.account.unrealized_pnl = float(acc_info.get("totalUnrealizedProfit", 0))
            self.account.margin_balance = float(acc_info.get("totalMarginBalance", 0))
            self.account.total_equity = self.account.margin_balance
            balances = self.client.account_balance()
            for b in balances:
                if b.get("asset") == "USDT":
                    self.account.available_balance = float(b.get("availableBalance", 0))
            raw_positions = self.client.positions()
            with self._account_lock:
                self.account.positions = []
                for p in raw_positions:
                    amt = float(p["positionAmt"])
                    if amt == 0: continue
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
            logger.info(f"💰 账户: 权益={self.account.total_equity:.2f} | 可用={self.account.available_balance:.2f} | 未实现盈亏={self.account.unrealized_pnl:.2f} | 持仓={len(self.account.positions)}")
        except Exception as e:
            logger.error(f"刷新账户状态失败: {e}")

    def _calc_dry_run_pnl(self, pos: Position) -> float:
        if pos.entry_price == 0 or pos.mark_price == 0:
            return 0
        direction = 1 if pos.side == PositionSide.LONG else -1
        pnl_pct = (pos.mark_price - pos.entry_price) / pos.entry_price * 100 * direction
        nominal = pos.entry_price * pos.quantity
        return round(nominal * pnl_pct / 100, 4)

    def fetch_klines(self, symbol: str, timeframe: str = None) -> List[dict]:
        tf = timeframe or self.config["timeframe"]
        cache_key = f"{symbol}_{tf}"
        if tf in ("1m", "2m"):
            try:
                if tf == "1m":
                    raw_klines = self.kline_manager.get_klines_1m(symbol, limit=200)
                else:
                    raw_klines = self.kline_manager.get_klines_2m(symbol, limit=200)
                if raw_klines:
                    with self._kline_cache_lock:
                        self._kline_cache[cache_key] = raw_klines
                    return raw_klines
                else:
                    logger.warning(f"本地 {tf} K 线为空 {symbol}，API回退")
            except Exception as e:
                logger.warning(f"本地 {tf} K 线读取失败 {symbol}: {e}，API回退")
        try:
            raw = self.client.klines(symbol, tf, 200)
            if isinstance(raw, dict) and "error" in raw:
                logger.warning(f"K线获取失败 {symbol}: {raw['error']}")
                with self._kline_cache_lock:
                    return self._kline_cache.get(cache_key, [])
            candles = self.strategy.parse_klines(raw)
            if candles:
                with self._kline_cache_lock:
                    self._kline_cache[cache_key] = candles
            return candles
        except Exception as e:
            logger.error(f"K线获取异常 {symbol}: {e}")
            with self._kline_cache_lock:
                return self._kline_cache.get(cache_key, [])

    def get_current_price(self, symbol: str) -> float:
        try:
            data = self.client.ticker_price(symbol)
            return float(data.get("price", 0))
        except Exception:
            with self._kline_cache_lock:
                candles = self._kline_cache.get(f"{symbol}_{self.config['timeframe']}", [])
            if candles:
                return candles[-1]["close"]
            return 0

    def process_signals(self) -> List[TradeSignal]:
        final_signals = []
        for sym_cfg in self.symbols_config:
            symbol = sym_cfg["symbol"]
            logger.info(f"📡 分析 {symbol}...")
            candles_2m = self.fetch_klines(symbol, "2m")
            if not candles_2m:
                logger.warning(f"  {symbol}: 2m K 线数据为空，跳过")
                continue
            signals = self.strategy.analyze(candles_2m, symbol=symbol)
            if signals:
                logger.info(f"  {symbol}: {len(signals)} 个策略发出信号 → {[s.strategy for s in signals]}")
            if signals:
                aggregated = self.strategy.aggregate_signals(signals, symbol)
                if aggregated:
                    logger.info(f"  {symbol} {_side_cn(aggregated.action.value)} "
                               f"(置信度={aggregated.confidence:.0%}, 策略={_strategy_cn(aggregated.strategy)})")
                    final_signals.append(aggregated)
        filtered = [s for s in final_signals if s.confidence >= self.min_confidence]
        low_conf = [s for s in final_signals if s.confidence < self.min_confidence]
        logger.info(f"📊 过滤完成: 通过 {len(filtered)} 个, 过滤 {len(low_conf)} 个")
        if low_conf:
            for s in low_conf:
                logger.info(f"🔽 {s.symbol} {_side_cn(s.action.value)} 置信度 {s.confidence:.0%} 低于阈值 {self.min_confidence:.0%}，已过滤")
        return filtered

    def execute_signal(self, signal: TradeSignal):
        symbol = signal.symbol
        action = signal.action
        now = time.time()
        with self._signal_time_lock:
            last = self._last_signal_time.get(symbol, 0)
        if now - last < self.signal_cooldown:
            logger.debug(f"⏳ {symbol} 信号冷却中，跳过")
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
        leverages = [self.config.get("strategies", {}).get(s, {}).get("leverage", self.risk_config["max_leverage"])
                     for s in signal.strategy.split(",")]
        leverage = max(leverages)
        for pos in self.account.positions:
            if pos.symbol == symbol and pos.side == PositionSide.SHORT:
                logger.info(f"🔄 {symbol} 有空仓，先平仓再做多")
                self.orders.close_position(symbol, reason=f"反转做多 [{signal.strategy}]",
                                           account=self.account, client=self.client)
                break
        qty = self.risk.calc_position_size(self.account, price, leverage)
        qty = self.client.adjust_quantity(symbol, qty)
        ok, reason = self.risk.can_open_position(self.account, symbol, PositionSide.LONG, qty, leverage, price)
        if not ok:
            logger.warning(f"🚫 {symbol} 做多被风控拒绝: {reason}")
            try: notifier.risk_warning(symbol, f"做多被拒绝: {reason}", level="warning")
            except Exception: pass
            return
        result = self.orders.open_position(symbol, PositionSide.LONG, qty, leverage, price,
                                           strategy=signal.strategy, account=self.account)
        if result["status"] == "opened":
            logger.info(f"📈 {symbol} 做多已执行: qty={qty}, leverage={leverage}x")

    def _handle_sell_signal(self, signal: TradeSignal):
        symbol = signal.symbol
        price = signal.price
        leverages = [self.config.get("strategies", {}).get(s, {}).get("leverage", self.risk_config["max_leverage"])
                     for s in signal.strategy.split(",")]
        leverage = max(leverages)
        for pos in self.account.positions:
            if pos.symbol == symbol and pos.side == PositionSide.LONG:
                logger.info(f"🔄 {symbol} 有多仓，先平仓再做空")
                self.orders.close_position(symbol, reason=f"反转做空 [{signal.strategy}]",
                                           account=self.account, client=self.client)
                break
        qty = self.risk.calc_position_size(self.account, price, leverage)
        qty = self.client.adjust_quantity(symbol, qty)
        ok, reason = self.risk.can_open_position(self.account, symbol, PositionSide.SHORT, qty, leverage, price)
        if not ok:
            logger.warning(f"🚫 {symbol} 做空被风控拒绝: {reason}")
            try: notifier.risk_warning(symbol, f"做空被拒绝: {reason}", level="warning")
            except Exception: pass
            return
        result = self.orders.open_position(symbol, PositionSide.SHORT, qty, leverage, price,
                                           strategy=signal.strategy, account=self.account)
        if result["status"] == "opened":
            logger.info(f"📉 {symbol} 做空已执行: qty={qty}, leverage={leverage}x")

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
                try: notifier.risk_warning(pos.symbol, action, level=risk_level)
                except Exception: pass
                if any(kw in action for kw in ("STOP_LOSS", "TRAILING_STOP", "TAKE_PROFIT")):
                    self.orders.close_position(pos.symbol, reason=action,
                                               account=self.account, client=self.client)

    def run_cycle(self):
        self.cycle_count += 1
        self.last_cycle_time = time.time()
        logger.info(f"\n{'='*50}")
        logger.info(f"🔄 第 {self.cycle_count} 个周期")
        logger.info(f"{'='*50}")
        self.refresh_account()
        if self.client.is_circuit_broken:
            logger.warning("🚨 API 熔断中，跳过本轮")
            return
        signals = self.process_signals()
        logger.info(f"📊 process_signals 返回: {len(signals)} 个信号")
        if signals:
            logger.info(f"📝 发现 {len(signals)} 个聚合信号:")
            for sig in signals:
                logger.info(f"  {sig.symbol}: {_side_cn(sig.action.value)} ({_strategy_cn(sig.strategy)}, {sig.confidence:.0%})")
                self.execute_signal(sig)
        else:
            logger.info("ℹ️  无交易信号")
        self._save_state()

    def _save_state(self):
        state = {
            "cycle_count": self.cycle_count,
            "last_cycle": datetime.now(timezone.utc).isoformat(),
            "environment": BINANCE_API_ENV,
            "mode_label": MODE_LABEL,
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
        logger.info(f"🚀 交易引擎启动 [{MODE_LABEL}]")
        if BINANCE_API_ENV == "prod":
            logger.warning("🚨 实盘模式！")
        else:
            logger.info("📌 测试网模式：虚拟资金模拟交易")

        def stop_handler(signum, frame):
            logger.info("收到停止信号，正在安全退出...")
            self.running = False
            self._risk_stop_event.set()
            self.kline_manager.stop()

        sig.signal(sig.SIGINT, stop_handler)
        sig.signal(sig.SIGTERM, stop_handler)

        self.self_check_strategies()
        self.refresh_account()

        logger.info("📥 初始化 K 线数据管理器...")
        try:
            self.kline_manager.initial_load()
        except Exception as e:
            logger.error(f"K 线初始加载失败: {e}")

        self.kline_manager.start_background_update(interval=60)
        self.kline_manager.start_background_cleanup(interval=43200, max_age_hours=24.0)
        self._risk_thread = threading.Thread(target=self._risk_monitor_loop, daemon=True)
        self._risk_thread.start()

        while self.running:
            try:
                self.run_cycle()
            except Exception as e:
                logger.error(f"周期异常: {e}", exc_info=True)
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

    def stop(self):
        self.running = False
        self._risk_stop_event.set()
        if self._risk_thread and self._risk_thread.is_alive():
            self._risk_thread.join(timeout=5)
        self.kline_manager.stop()
        logger.info("交易引擎已停止")

    def status(self):
        self.refresh_account()
        print(f"\n{'='*50}")
        print(f"📊 交易引擎状态")
        print(f"{'='*50}")
        print(f"模式: {MODE_LABEL}")
        print(f"环境: {BINANCE_API_ENV}")
        print(f"认证: {'✅' if IS_AUTHENTICATED else '⚠️'}")
        print(f"周期数: {self.cycle_count}")
        print(f"熔断: {'🚨' if self.client.circuit_breaker else '✅'}")
        print(f"总权益: {self.account.total_equity:.2f} USDT")
        print(f"可用余额: {self.account.available_balance:.2f} USDT")
        print(f"持仓数: {len(self.account.positions)}")
        for pos in self.account.positions:
            pnl = pos.pnl_pct()
            print(f"  {pos.symbol}: {pos.side.value.upper()} {pos.quantity} @ {pos.entry_price} | 现价={pos.mark_price} | 盈亏={pnl:.2f}% | 杠杆={pos.leverage}x")
        print(f"\n策略状态:")
        for name, info in self._strategy_status.items():
            print(f"  {name}: {info['status']} (启用={info['enabled']}, 错误={info['errors']})")
        print(f"{'='*50}\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="量化交易引擎")
    parser.add_argument("--env", choices=["testnet", "prod"], help="临时切换环境")
    parser.add_argument("--status", action="store_true", help="查看状态后退出")
    parser.add_argument("--self-check", action="store_true", help="策略自检后退出")
    args = parser.parse_args()

    if args.env:
        os.environ["BINANCE_API_ENV"] = args.env

    engine = TradingEngine()

    if args.status:
        engine.status()
        return
    if args.self_check:
        engine.self_check_strategies()
        return

    try:
        engine.start()
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
