#!/usr/bin/env python3
"""
core/risk_engine.py — 风控引擎
"""

import logging
from typing import Optional, Tuple
from .models import Position, AccountState, PositionSide

logger = logging.getLogger("engine.risk")


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
                          leverage: int, price: float,
                          margin_usdt: float = None) -> Tuple[bool, str]:
        if len(account.positions) >= self.cfg["max_positions"]:
            return False, f"持仓数已达上限 ({len(account.positions)}/{self.cfg['max_positions']})"
        for p in account.positions:
            if p.symbol == symbol:
                return False, f"{symbol} 已有持仓，不可重复开仓"
        if leverage > self.cfg["max_leverage"]:
            return False, f"杠杆 {leverage}x 超过上限 {self.cfg['max_leverage']}x"
        margin_needed_check = price * quantity / leverage
        # 反转信号允许更高保证金（默认100U），普通信号使用固定保证金（50U）
        if margin_usdt is not None:
            allowed_margin = margin_usdt
        else:
            allowed_margin = self.cfg.get("fixed_margin_usdt", 50)
        if margin_needed_check > allowed_margin * 1.05:
            return False, f"保证金 {margin_needed_check:.2f} 超过允许值 {allowed_margin} USDT"
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
                           leverage: int, risk_pct: float = None,
                           margin_usdt: float = None) -> float:
        if margin_usdt is not None:
            fixed_margin = margin_usdt
        else:
            fixed_margin = self.cfg.get("fixed_margin_usdt", 50)
        nominal = fixed_margin * leverage
        quantity = nominal / price
        return quantity
