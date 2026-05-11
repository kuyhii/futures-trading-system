#!/usr/bin/env python3
"""
core/indicators.py — 技术指标计算
"""

from typing import Optional, List


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
