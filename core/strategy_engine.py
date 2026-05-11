#!/usr/bin/env python3
"""
core/strategy_engine.py — 策略引擎（趋势跟踪 + 均值回归 + 突破）
"""

import logging
from typing import Optional, List, Dict
from .models import TradeSignal, SignalAction

logger = logging.getLogger("engine.strategy")


class StrategyEngine:
    """策略信号引擎 — 支持3个活跃策略"""

    def __init__(self, config: dict):
        self.config = config
        self.strategies_cfg = config.get("strategies", {})
        self.indicators_cfg = config.get("indicators", {})
        self.agg_cfg = config.get("signal_aggregation", {})
        self._strategy_errors: Dict[str, int] = {}

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
                self._strategy_errors[name] = self._strategy_errors.get(name, 0) + 1
                err_count = self._strategy_errors[name]
                logger.error(f"❌ 策略 {name} 异常 (连续 {err_count} 次): {e}")
                if err_count >= 3:
                    logger.warning(f"🔇 策略 {name} 连续错误过多，临时禁用")
                    cfg["enabled"] = False
        return signals

    def aggregate_signals(self, signals: List[TradeSignal], symbol: str) -> Optional[TradeSignal]:
        """信号聚合：加权投票 + 冲突检测"""
        if not signals:
            return None

        strategy_weights = {}
        for name, cfg in self.strategies_cfg.items():
            strategy_weights[name] = cfg.get("weight", 1.0)

        buy_signals = [s for s in signals if s.action == SignalAction.BUY]
        sell_signals = [s for s in signals if s.action == SignalAction.SELL]

        if buy_signals and sell_signals:
            logger.warning(f"⚡ 信号冲突({symbol}): {len(buy_signals)}个做多 vs {len(sell_signals)}个做空")
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

        total_weight = sum(strategy_weights.get(s.strategy, 1.0) for s in winner_signals)
        weighted_conf = sum(
            s.confidence * strategy_weights.get(s.strategy, 1.0) for s in winner_signals
        ) / total_weight

        agreement_bonus = 0.0
        if len(winner_signals) >= 2:
            agreement_bonus = min(0.15, len(winner_signals) * 0.05)
            logger.info(f"🤝 {symbol} {len(winner_signals)} 个策略一致: {[s.strategy for s in winner_signals]} "
                       f"(加成 +{agreement_bonus:.0%})")

        final_confidence = min(0.95, weighted_conf + agreement_bonus)

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
        from .indicators import Indicators
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
        from .indicators import Indicators
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
        from .indicators import Indicators
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
