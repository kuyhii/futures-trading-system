#!/usr/bin/env python3
"""
core/models.py — 数据模型
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List
from datetime import datetime, timezone


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
    highest_pnl: float = 0

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
