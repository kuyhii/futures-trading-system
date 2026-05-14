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

    # ============================================================
    # 已有增强指标
    # ============================================================

    @staticmethod
    def adx(candles: List[dict], period: int = 14) -> Optional[dict]:
        """ADX 趋势强度指标（Wilders 平滑）
        返回 {"adx": float, "plus_di": float, "minus_di": float} 或 None
        """
        if len(candles) < period * 2 + 1:
            return None
        trs, plus_dms, minus_dms = [], [], []
        for i in range(1, len(candles)):
            high = candles[i]["high"]
            low = candles[i]["low"]
            prev_close = candles[i - 1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            up_move = high - candles[i - 1]["high"]
            down_move = candles[i - 1]["low"] - low
            plus_dm = up_move if (up_move > down_move and up_move > 0) else 0
            minus_dm = down_move if (down_move > up_move and down_move > 0) else 0
            trs.append(tr)
            plus_dms.append(plus_dm)
            minus_dms.append(minus_dm)
        atr = sum(trs[:period])
        plus_dm_smooth = sum(plus_dms[:period])
        minus_dm_smooth = sum(minus_dms[:period])
        dx_values = []
        for i in range(period, len(trs)):
            atr = atr - atr / period + trs[i]
            plus_dm_smooth = plus_dm_smooth - plus_dm_smooth / period + plus_dms[i]
            minus_dm_smooth = minus_dm_smooth - minus_dm_smooth / period + minus_dms[i]
            if atr == 0:
                dx_values.append(0)
                continue
            plus_di = (plus_dm_smooth / atr) * 100
            minus_di = (minus_dm_smooth / atr) * 100
            di_sum = plus_di + minus_di
            if di_sum == 0:
                dx_values.append(0)
            else:
                dx_values.append(abs(plus_di - minus_di) / di_sum * 100)
        if len(dx_values) < period:
            return None
        adx = sum(dx_values[-period:]) / period
        if atr == 0:
            return {"adx": adx, "plus_di": 0, "minus_di": 0}
        return {
            "adx": round(adx, 2),
            "plus_di": round((plus_dm_smooth / atr) * 100, 2),
            "minus_di": round((minus_dm_smooth / atr) * 100, 2),
        }

    @staticmethod
    def stochastic(candles: List[dict], k_period: int = 14, d_period: int = 3) -> Optional[dict]:
        """KD 随机指标 — 返回 {"k": float, "d": float} 或 None"""
        if len(candles) < k_period + d_period - 1:
            return None
        k_values = []
        for i in range(k_period - 1, len(candles)):
            window = candles[i - k_period + 1:i + 1]
            highest = max(c["high"] for c in window)
            lowest = min(c["low"] for c in window)
            close = candles[i]["close"]
            if highest == lowest:
                k_values.append(50.0)
            else:
                k_values.append((close - lowest) / (highest - lowest) * 100)
        if len(k_values) < d_period:
            return None
        d_val = sum(k_values[-d_period:]) / d_period
        k_raw = k_values[-1]
        return {"k": round(max(0, min(100, k_raw)), 2), "d": round(max(0, min(100, d_val)), 2)}

    @staticmethod
    def obv(candles: List[dict]) -> Optional[List[float]]:
        """能量潮 (On-Balance Volume) — 返回 OBV 序列或 None"""
        if len(candles) < 2:
            return None
        obv = [0.0]
        for i in range(1, len(candles)):
            if candles[i]["close"] > candles[i - 1]["close"]:
                obv.append(obv[-1] + candles[i]["volume"])
            elif candles[i]["close"] < candles[i - 1]["close"]:
                obv.append(obv[-1] - candles[i]["volume"])
            else:
                obv.append(obv[-1])
        return obv

    @staticmethod
    def ema_series_float(values: List[float], period: int) -> List[float]:
        """对任意数值序列计算 EMA 序列（通用版）"""
        if len(values) < period:
            return []
        mult = 2 / (period + 1)
        ema = sum(values[:period]) / period
        result = [ema]
        for v in values[period:]:
            ema = (v - ema) * mult + ema
            result.append(ema)
        return result

    @staticmethod
    def ema_val(closes: List[float], period: int) -> Optional[float]:
        """EMA 最新值"""
        if len(closes) < period:
            return None
        mult = 2 / (period + 1)
        ema = sum(closes[:period]) / period
        for p in closes[period:]:
            ema = (p - ema) * mult + ema
        return ema

    # ============================================================
    # V4 新增指标 — ea-python 精华提取
    # ============================================================

    @staticmethod
    def cci(candles: List[dict], period: int = 14) -> Optional[float]:
        """CCI 顺势指标 (Commodity Channel Index)
        CCI > +100 → 多头强势（趋势启动）
        CCI < -100 → 空头强势（趋势启动）
        用于突破策略的趋势确认过滤器
        """
        if len(candles) < period:
            return None
        tp = [(c["high"] + c["low"] + c["close"]) / 3 for c in candles[-period:]]
        mean_tp = sum(tp) / len(tp)
        mean_dev = sum(abs(x - mean_tp) for x in tp) / len(tp)
        if mean_dev == 0:
            return 0.0
        return round((tp[-1] - mean_tp) / (0.015 * mean_dev), 2)

    @staticmethod
    def emv(candles: List[dict], period: int = 14) -> Optional[float]:
        """EMV 简易波动指标 (Ease of Movement)
        量价复合指标，比 OBV 更能反映趋势中的量能效率
        EMV > 0 → 价格上涨轻松（多头趋势）
        EMV < 0 → 价格下跌轻松（空头趋势）
        用于突破策略的量能确认
        """
        if len(candles) < period + 1:
            return None
        em_values = []
        for i in range(1, len(candles)):
            high = candles[i]["high"]
            low = candles[i]["low"]
            prev_high = candles[i - 1]["high"]
            prev_low = candles[i - 1]["low"]
            volume = candles[i]["volume"]
            if volume == 0:
                continue
            mid_move = (high + low) / 2 - (prev_high + prev_low) / 2
            box_ratio = (high - low) / volume
            em_values.append(mid_move * box_ratio)
        if len(em_values) < period:
            return None
        return round(sum(em_values[-period:]), 6)

    @staticmethod
    def dual_thrust_range(candles: List[dict], window: int = 20) -> Optional[dict]:
        """Dual Thrust 通道范围计算
        HH = window根最高价, LL = window根最低价
        HC = window根收盘最高, LC = window根收盘最低
        Range = max(HH-LC, HC-LL) — 比简单高低点更反映真实波动
        上轨 = Open + K1*Range, 下轨 = Open - K2*Range
        用于突破策略的通道计算
        """
        if len(candles) < window + 1:
            return None
        window_candles = candles[-(window + 1):-1]  # 不含当前蜡烛
        highs = [c["high"] for c in window_candles]
        lows = [c["low"] for c in window_candles]
        closes = [c["close"] for c in window_candles]
        if not highs:
            return None
        hh = max(highs)
        ll = min(lows)
        hc = max(closes)
        lc = min(closes)
        rng = max(hh - lc, hc - ll)
        return {"range": round(rng, 8), "hh": round(hh, 8), "ll": round(ll, 8),
                "hc": round(hc, 8), "lc": round(lc, 8)}
