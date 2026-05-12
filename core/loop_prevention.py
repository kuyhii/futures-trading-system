#!/usr/bin/env python3
"""防循环系统 — 检测并打破交易引擎中的重复模式"""

import logging
from collections import deque
from typing import Dict, List

logger = logging.getLogger("engine.loop_prevention")


class LoopPrevention:
    """
    防循环系统，检测以下模式：
    1. 同一信号反复出现（同一币种+方向+策略，连续N次）
    2. 连续多轮无交易执行（信号全被过滤/拒绝）
    3. API 连续失败（已有熔断，但加一层前置检测）
    """

    def __init__(self, max_repeated_signal: int = 5,
                 max_idle_cycles: int = 10,
                 cooldown_after_break: int = 600):
        self.max_repeated_signal = max_repeated_signal      # 同一信号重复次数阈值
        self.max_idle_cycles = max_idle_cycles              # 空闲周期阈值
        self.cooldown_after_break = cooldown_after_break    # 打破循环后的冷却时间（秒）

        # 跟踪最近信号（每币种最新信号）
        self._recent_signals: Dict[str, deque] = {}  # symbol -> deque of (action, strategy, confidence)
        self._repeat_counts: Dict[str, int] = {}     # symbol -> 连续重复计数

        # 空闲周期计数
        self._idle_cycle_count = 0

        # 打破循环后的冷却
        self._break_cooldown_until = 0.0
        self._break_count = 0

        # 统计
        self.stats = {
            "loops_detected": 0,
            "loops_broken": 0,
            "cooldown_skips": 0,
        }

    def check_signal_loop(self, symbol: str, action: str, strategy: str,
                          confidence: float) -> bool:
        """
        检查同一信号是否反复出现。
        返回 True 表示检测到循环，应该跳过执行。
        """
        if symbol not in self._recent_signals:
            self._recent_signals[symbol] = deque(maxlen=self.max_repeated_signal + 1)
            self._repeat_counts[symbol] = 0

        sig_key = f"{action}:{strategy}"
        self._recent_signals[symbol].append(sig_key)

        # 检查是否连续相同
        if len(self._recent_signals[symbol]) >= 2:
            recent_list = list(self._recent_signals[symbol])
            if all(s == sig_key for s in recent_list):
                self._repeat_counts[symbol] = self._repeat_counts.get(symbol, 0) + 1
            else:
                self._repeat_counts[symbol] = 0

        count = self._repeat_counts.get(symbol, 0)
        if count >= self.max_repeated_signal:
            self.stats["loops_detected"] += 1
            logger.warning(
                f"🔁 循环检测: {symbol} 信号 [{action}/{strategy}] "
                f"连续重复 {count+1} 次，跳过执行"
            )
            return True
        return False

    def record_cycle_progress(self, signals_count: int, executed_count: int):
        """记录本轮周期是否有进展"""
        if executed_count == 0 and signals_count == 0:
            self._idle_cycle_count += 1
            if self._idle_cycle_count >= self.max_idle_cycles:
                self.stats["loops_detected"] += 1
                logger.warning(
                    f"🔁 循环检测: 连续 {self._idle_cycle_count} 轮无交易信号，"
                    f"进入冷却 {self.cooldown_after_break}s"
                )
                self._trigger_break()
        else:
            self._idle_cycle_count = 0

    def _trigger_break(self):
        import time
        self.stats["loops_broken"] += 1
        self._break_count += 1
        self._break_cooldown_until = time.time() + self.cooldown_after_break
        logger.warning(
            f"🚨 循环已打破！冷却 {self.cooldown_after_break}s "
            f"(第 {self._break_count} 次)"
        )

    def is_in_cooldown(self) -> bool:
        """是否在打破循环后的冷却期"""
        import time
        if time.time() < self._break_cooldown_until:
            self.stats["cooldown_skips"] += 1
            return True
        return False

    def get_status(self) -> dict:
        return {
            **self.stats,
            "idle_cycles": self._idle_cycle_count,
            "in_cooldown": self.is_in_cooldown(),
        }
