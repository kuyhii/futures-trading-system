#!/usr/bin/env python3
"""
core/backtest.py - 回测引擎
职责: 使用历史 K 线数据验证策略表现
  - 加载历史数据（本地缓存或从 API 拉取）
  - 模拟交易执行
  - 统计收益、最大回撤、胜率、夏普比率等

使用方式:
  python3 -m core.backtest                          # 默认回测 BTCUSDT 15m 200根K线
  python3 -m core.backtest --symbol BTCUSDT --interval 1h --bars 500
  python3 -m core.backtest --symbol ETHUSDT --strategies trend_follow,breakout
"""

import json
import os
import sys
import math
import argparse
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field

# ── 路径 ──
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")
LOGS_DIR = os.path.join(ROOT, "logs")
STATE_DIR = os.path.join(ROOT, "state")

# ── 导入核心组件 ──
from core.engine import (
    BinanceClient, StrategyEngine, Indicators, RiskEngine,
    SignalAction, PositionSide, TradeSignal, Position, AccountState
)

# ── 日志 ──
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [backtest] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, "backtest.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("backtest")


# ═══════════════════════════════════════════
# 回测数据模型
# ═══════════════════════════════════════════

@dataclass
class BacktestTrade:
    """回测中的交易记录"""
    entry_time: str
    exit_time: str
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    leverage: int
    pnl: float
    pnl_pct: float
    strategy: str
    bars_held: int
    max_drawdown: float  # 持仓期间最大回撤
    max_profit: float    # 持仓期间最大盈利


@dataclass
class BacktestResult:
    """回测结果汇总"""
    symbol: str
    timeframe: str
    start_date: str
    end_date: str
    total_bars: int
    initial_capital: float
    final_capital: float
    total_return_pct: float
    max_drawdown_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_pnl: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    sharpe_ratio: float
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)


# ═══════════════════════════════════════════
# 回测引擎
# ═══════════════════════════════════════════

class BacktestEngine:
    """策略回测引擎"""

    def __init__(self, symbol: str = "BTCUSDT", timeframe: str = "15m",
                 bars: int = 500, initial_capital: float = 10000,
                 strategies: List[str] = None, leverage: int = 5,
                 commission_rate: float = 0.0004):
        self.symbol = symbol
        self.timeframe = timeframe
        self.bars = bars
        self.initial_capital = initial_capital
        self.leverage = leverage
        self.commission_rate = commission_rate  # 单边手续费

        # 加载配置
        with open(os.path.join(CONFIG_DIR, "strategies.json")) as f:
            self.config = json.load(f)

        # 过滤启用的策略
        if strategies:
            for name in self.config["strategies"]:
                enabled = name in strategies
                self.config["strategies"][name]["enabled"] = enabled
        else:
            # 只保留默认启用的
            pass

        self.strategy = StrategyEngine(self.config)
        self.risk_cfg = self._load_risk_config()

        # 回测状态
        self.equity = initial_capital
        self.position: Optional[Position] = None
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[float] = [initial_capital]
        self.peak_equity = initial_capital
        self.max_drawdown = 0

    def _load_risk_config(self) -> dict:
        with open(os.path.join(CONFIG_DIR, "risk.json")) as f:
            return json.load(f)

    def load_klines(self) -> List[dict]:
        """加载 K 线数据"""
        # 1. 尝试从本地缓存加载
        kline_dir = os.path.join(DATA_DIR, "klines")
        files = sorted([
            f for f in os.listdir(kline_dir)
            if f.startswith(f"{self.symbol}_{self.timeframe}_")
        ], reverse=True)

        if files:
            try:
                with open(os.path.join(kline_dir, files[0])) as f:
                    raw = json.load(f)
                candles = self._parse_klines(raw)
                if candles:
                    logger.info(f"从本地缓存加载 {len(candles)} 根 K 线")
                    return candles
            except Exception as e:
                logger.warning(f"本地缓存加载失败: {e}")

        # 2. 从 API 拉取
        logger.info(f"从 API 拉取 {self.bars} 根 K 线 ({self.symbol} {self.timeframe})...")
        client = BinanceClient()
        raw = client.klines(self.symbol, self.timeframe, self.bars)
        if isinstance(raw, dict) and "error" in raw:
            logger.error(f"API 获取失败: {raw['error']}")
            return []

        candles = self._parse_klines(raw)
        if candles:
            logger.info(f"成功加载 {len(candles)} 根 K 线")
        return candles

    def _parse_klines(self, raw: list) -> List[dict]:
        candles = []
        if not isinstance(raw, list):
            return candles
        for k in raw:
            if isinstance(k, list) and len(k) >= 6:
                candles.append({
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                })
        return candles

    def run(self) -> BacktestResult:
        """执行回测"""
        candles = self.load_klines()
        if len(candles) < 50:
            logger.error("K 线数据不足，无法回测（至少需要 50 根）")
            return None

        logger.info(f"开始回测: {self.symbol} {self.timeframe} | "
                    f"初始资金: {self.initial_capital} | "
                    f"杠杆: {self.leverage}x | "
                    f"手续费: {self.commission_rate * 100:.2f}% | "
                    f"K 线数: {len(candles)}")
        logger.info(f"启用策略: {[k for k, v in self.config['strategies'].items() if v.get('enabled')]}")

        # 滑窗回测
        window_start = 30  # 前 30 根用于初始化指标
        for i in range(window_start, len(candles)):
            window = candles[:i + 1]
            current = candles[i]
            bar_time = str(current.get("time", f"bar_{i}"))

            # 策略信号
            signals = self.strategy.analyze(window)

            # 检查止损止盈
            if self.position:
                self._check_sl_tp(current, i)
                # 更新持仓期间最大盈亏
                if self.position:  # may have been closed by _check_sl_tp
                    pnl_pct = self._calc_position_pnl(current)
                    self.position.highest_pnl = max(
                        getattr(self.position, 'highest_pnl', 0), pnl_pct)

            # 处理信号
            for sig in signals:
                if not self.position:
                    # 开仓
                    self._open_position(sig, current, i)
                    break
                else:
                    # 检查是否需要平仓
                    if (sig.action == SignalAction.BUY and self.position.side == PositionSide.SHORT) or \
                       (sig.action == SignalAction.SELL and self.position.side == PositionSide.LONG):
                        self._close_position(current, i, f"反转信号 [{sig.strategy}]")
                        # 反向开仓
                        self._open_position(sig, current, i)
                        break

            # 更新权益曲线
            if self.position:
                pnl = self._calc_position_pnl(current)
                self.equity = self.initial_capital + sum(t.pnl for t in self.trades)
            else:
                self.equity = self.initial_capital + sum(t.pnl for t in self.trades)

            self.equity_curve.append(self.equity)
            self.peak_equity = max(self.peak_equity, self.equity)
            dd = (self.peak_equity - self.equity) / self.peak_equity * 100
            self.max_drawdown = max(self.max_drawdown, dd)

        # 如果最后还有持仓，强制平仓
        if self.position:
            self._close_position(candles[-1], len(candles) - 1, "回测结束强制平仓")

        return self._build_result(candles)

    def _calc_position_pnl(self, candle: dict) -> float:
        """计算当前持仓盈亏百分比"""
        if not self.position:
            return 0
        price = candle["close"]
        if self.position.side == PositionSide.LONG:
            return (price - self.position.entry_price) / self.position.entry_price * 100
        else:
            return (self.position.entry_price - price) / self.position.entry_price * 100

    def _check_sl_tp(self, candle: dict, bar_idx: int):
        """检查止损止盈"""
        if not self.position:
            return

        pnl = self._calc_position_pnl(candle)
        sl_pct = self.risk_cfg["stop_loss_pct"]
        tp_pct = self.risk_cfg["take_profit_pct"]

        if pnl <= -sl_pct:
            self._close_position(candle, bar_idx, f"止损 ({pnl:.2f}%)")
        elif pnl >= tp_pct:
            self._close_position(candle, bar_idx, f"止盈 ({pnl:.2f}%)")

    def _open_position(self, signal: TradeSignal, candle: dict, bar_idx: int):
        """模拟开仓"""
        price = candle["close"]
        # V2: 使用固定保证金（与引擎一致）
        fixed_margin = self.risk_cfg.get("fixed_margin_usdt", 50)
        nominal = fixed_margin * self.leverage
        quantity = nominal / price

        self.position = Position(
            symbol=self.symbol,
            side=PositionSide.LONG if signal.action == SignalAction.BUY else PositionSide.SHORT,
            quantity=quantity,
            entry_price=price,
            leverage=self.leverage,
            mark_price=price,
        )

        # 扣除开仓手续费
        fee = nominal * self.commission_rate
        self.equity -= fee

        logger.debug(f"  📈 开仓: {self.position.side.value.upper()} {self.symbol} "
                    f"qty={quantity:.4f} @ {price} ({signal.strategy})")

    def _close_position(self, candle: dict, bar_idx: int, reason: str = ""):
        """模拟平仓"""
        if not self.position:
            return

        price = candle["close"]
        entry = self.position.entry_price
        qty = self.position.quantity
        side = self.position.side

        # 计算盈亏
        if side == PositionSide.LONG:
            pnl_pct = (price - entry) / entry * 100
        else:
            pnl_pct = (entry - price) / entry * 100

        # 名义价值 * 盈亏百分比 = 实际盈亏
        nominal = entry * qty
        pnl = nominal * pnl_pct / 100

        # 扣除手续费
        exit_nominal = price * qty
        fee_exit = exit_nominal * self.commission_rate
        pnl -= fee_exit

        trade = BacktestTrade(
            entry_time=f"bar_{bar_idx - (bar_idx - getattr(self.position, '_entry_bar', bar_idx))}",
            exit_time=f"bar_{bar_idx}",
            symbol=self.symbol,
            side=side.value,
            entry_price=entry,
            exit_price=price,
            quantity=qty,
            leverage=self.leverage,
            pnl=pnl,
            pnl_pct=pnl_pct,
            strategy=getattr(self.position, 'signal_strategy', 'unknown'),
            bars_held=bar_idx - getattr(self.position, '_entry_bar', bar_idx),
            max_drawdown=0,
            max_profit=getattr(self.position, 'highest_pnl', pnl_pct),
        )

        self.trades.append(trade)
        self.position = None

        emoji = "✅" if pnl > 0 else "❌"
        logger.debug(f"  {emoji} 平仓: {side.value.upper()} {self.symbol} "
                    f"@ {price} | 盈亏={pnl:+.2f} ({pnl_pct:+.2f}%) | 原因: {reason}")

    def _build_result(self, candles: List[dict]) -> BacktestResult:
        """构建回测结果"""
        final = self.initial_capital + sum(t.pnl for t in self.trades)
        total_return = (final - self.initial_capital) / self.initial_capital * 100

        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]

        avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0

        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        # 夏普比率（假设无风险利率 0）
        if len(self.equity_curve) > 2:
            returns = [
                (self.equity_curve[i] - self.equity_curve[i - 1]) / self.equity_curve[i - 1]
                for i in range(1, len(self.equity_curve))
            ]
            avg_ret = sum(returns) / len(returns)
            std_ret = math.sqrt(sum((r - avg_ret) ** 2 for r in returns) / len(returns))
            sharpe = (avg_ret / std_ret * math.sqrt(252 * 96)) if std_ret > 0 else 0
            # 252天 * 96(15m bars per day)
        else:
            sharpe = 0

        result = BacktestResult(
            symbol=self.symbol,
            timeframe=self.timeframe,
            start_date=candles[0].get("time", "N/A") if candles else "N/A",
            end_date=candles[-1].get("time", "N/A") if candles else "N/A",
            total_bars=len(candles),
            initial_capital=self.initial_capital,
            final_capital=final,
            total_return_pct=total_return,
            max_drawdown_pct=self.max_drawdown,
            total_trades=len(self.trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=len(wins) / len(self.trades) * 100 if self.trades else 0,
            avg_pnl=sum(t.pnl for t in self.trades) / len(self.trades) if self.trades else 0,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            sharpe_ratio=sharpe,
            trades=self.trades,
            equity_curve=self.equity_curve,
        )

        return result


# ═══════════════════════════════════════════
# 报告输出
# ═══════════════════════════════════════════

def print_report(result: BacktestResult):
    """打印回测报告"""
    if result is None:
        print("❌ 回测失败")
        return

    print(f"\n{'='*60}")
    print(f"📊 回测报告")
    print(f"{'='*60}")
    print(f"  币种: {result.symbol}")
    print(f"  时间框架: {result.timeframe}")
    print(f"  K 线数: {result.total_bars}")
    print(f"  初始资金: {result.initial_capital:,.2f} USDT")
    print(f"  最终资金: {result.final_capital:,.2f} USDT")
    print(f"  总收益率: {result.total_return_pct:+.2f}%")
    print(f"  最大回撤: {result.max_drawdown_pct:.2f}%")
    print(f"")
    print(f"  交易次数: {result.total_trades}")
    print(f"  胜率: {result.win_rate:.1f}% ({result.winning_trades}胜 / {result.losing_trades}负)")
    print(f"  平均盈利: {result.avg_win:+.2f} USDT")
    print(f"  平均亏损: {result.avg_loss:+.2f} USDT")
    print(f"  盈亏比: {result.profit_factor:.2f}")
    print(f"  夏普比率: {result.sharpe_ratio:.2f}")
    print(f"")

    if result.trades:
        print(f"  交易明细 (前 10 笔):")
        print(f"  {'序号':<4} {'方向':<4} {'入场价':>12} {'出场价':>12} {'盈亏%':>8} {'盈亏':>10}")
        print(f"  {'-'*55}")
        for i, t in enumerate(result.trades[:10]):
            emoji = "✅" if t.pnl > 0 else "❌"
            direction = "多" if t.side == "long" else "空"
            print(f"  {i+1:<4} {direction:<4} {t.entry_price:>12.2f} {t.exit_price:>12.2f} "
                  f"{t.pnl_pct:>+7.2f}% {t.pnl:>+10.2f}")

        if len(result.trades) > 10:
            print(f"  ... 共 {len(result.trades)} 笔交易")

    print(f"{'='*60}\n")

    # 保存报告
    report_path = os.path.join(DATA_DIR, "reports",
                               f"backtest_{result.symbol}_{result.timeframe}_"
                               f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)

    # 序列化 trades
    trades_data = []
    for t in result.trades:
        trades_data.append({
            "entry_time": t.entry_time,
            "exit_time": t.exit_time,
            "symbol": t.symbol,
            "side": t.side,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "quantity": t.quantity,
            "leverage": t.leverage,
            "pnl": t.pnl,
            "pnl_pct": t.pnl_pct,
            "strategy": t.strategy,
            "bars_held": t.bars_held,
        })

    with open(report_path, "w") as f:
        json.dump({
            "symbol": result.symbol,
            "timeframe": result.timeframe,
            "initial_capital": result.initial_capital,
            "final_capital": result.final_capital,
            "total_return_pct": result.total_return_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "total_trades": result.total_trades,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "sharpe_ratio": result.sharpe_ratio,
            "trades": trades_data,
        }, f, indent=2)

    print(f"📄 报告已保存: {report_path}")


# ═══════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="策略回测引擎")
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对")
    parser.add_argument("--interval", default="15m", help="时间框架 (1m/5m/15m/1h/4h)")
    parser.add_argument("--bars", type=int, default=500, help="K 线数量")
    parser.add_argument("--capital", type=float, default=10000, help="初始资金")
    parser.add_argument("--leverage", type=int, default=5, help="杠杆倍数")
    parser.add_argument("--commission", type=float, default=0.0004, help="手续费率")
    parser.add_argument("--strategies", default=None,
                        help="指定策略，逗号分隔 (默认使用配置文件)")
    args = parser.parse_args()

    strategies = [s.strip() for s in args.strategies.split(",")] if args.strategies else None

    engine = BacktestEngine(
        symbol=args.symbol,
        timeframe=args.interval,
        bars=args.bars,
        initial_capital=args.capital,
        strategies=strategies,
        leverage=args.leverage,
        commission_rate=args.commission,
    )

    result = engine.run()
    print_report(result)


if __name__ == "__main__":
    main()
