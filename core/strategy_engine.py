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
        """信号聚合：加权投票 + 冲突检测 + 最少共识过滤"""
        if not signals:
            return None

        strategy_weights = {}
        for name, cfg in self.strategies_cfg.items():
            strategy_weights[name] = cfg.get("weight", 1.0)

        # 最少共识数过滤：只有1个策略信号且配置要求>=2时直接拒绝
        min_agree = self.agg_cfg.get("min_agreement_count", 1)
        if len(signals) < min_agree:
            logger.info(f"🚫 {symbol} 仅 {len(signals)} 个策略信号，低于最低共识数 {min_agree}，已过滤")
            return None

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
        # 使用 EMA 替代 SMA，对近期价格更敏感
        ma_fast = Indicators.ema_val(closes, fast)
        ma_slow = Indicators.ema_val(closes, slow)
        ma_fast_prev = Indicators.ema_val(closes[:-1], fast)
        ma_slow_prev = Indicators.ema_val(closes[:-1], slow)
        vol_ma = Indicators.volume_ma(volumes, 20)
        current_vol = volumes[-1]
        if not all([ma_fast, ma_slow, ma_fast_prev, ma_slow_prev]):
            return None
        # ADX 趋势强度过滤：ADX < 25 说明无趋势，信号不可靠
        adx_data = Indicators.adx(candles)
        adx_ok = adx_data and adx_data["adx"] >= 25

        details = {"ema_fast": round(ma_fast, 2), "ema_slow": round(ma_slow, 2), "current_price": price}
        macd_val, macd_sig, macd_hist = Indicators.macd(closes)
        if macd_val is not None:
            details["macd"] = round(macd_val, 4)
            details["macd_signal"] = round(macd_sig, 4)
            details["macd_histogram"] = round(macd_hist, 4)
        if adx_data:
            details["adx"] = adx_data["adx"]
            details["plus_di"] = adx_data["plus_di"]
            details["minus_di"] = adx_data["minus_di"]
        vol_confirm = (not vol_ma) or (current_vol > vol_ma * 1.2)

        if ma_fast_prev <= ma_slow_prev and ma_fast > ma_slow:
            if adx_ok and vol_confirm and macd_hist and macd_hist > 0:
                conf = 0.85
            elif adx_ok or vol_confirm:
                conf = 0.7
            else:
                conf = 0.6
            return self._make_signal(SignalAction.BUY, "trend_follow", conf, price,
                                     {**details, "cross": "golden", "volume_confirm": vol_confirm, "adx_ok": adx_ok})
        if ma_fast_prev >= ma_slow_prev and ma_fast < ma_slow:
            if adx_ok and vol_confirm and macd_hist and macd_hist < 0:
                conf = 0.85
            elif adx_ok or vol_confirm:
                conf = 0.7
            else:
                conf = 0.6
            return self._make_signal(SignalAction.SELL, "trend_follow", conf, price,
                                     {**details, "cross": "death", "volume_confirm": vol_confirm, "adx_ok": adx_ok})
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
        # KD 随机指标：RSI + Stochastic 双重超买超卖确认
        stoch = Indicators.stochastic(candles)
        if rsi is None or lower is None:
            return None
        details = {"rsi": round(rsi, 2), "bollinger_lower": round(lower, 2),
                   "bollinger_middle": round(mid, 2), "bollinger_upper": round(upper, 2),
                   "current_price": price}
        if stoch:
            details["stoch_k"] = stoch["k"]
            details["stoch_d"] = stoch["d"]
        ob = self.indicators_cfg["rsi_overbought"]
        os_ = self.indicators_cfg["rsi_oversold"]
        # Stochastic 确认逻辑：KD < 20 超卖，KD > 80 超买
        stoch_oversold = stoch and stoch["k"] < 20 and stoch["d"] < 20
        stoch_overbought = stoch and stoch["k"] > 80 and stoch["d"] > 80
        if rsi < os_ and price < lower:
            # RSI + 布林带 + 可选 KD 确认
            conf = 0.8 if stoch_oversold else 0.7
            return self._make_signal(SignalAction.BUY, "mean_reversion", conf, price,
                                     {**details, "reason": "oversold+below_lower", "stoch_confirm": stoch_oversold})
        if rsi > ob and price > upper:
            conf = 0.8 if stoch_overbought else 0.7
            return self._make_signal(SignalAction.SELL, "mean_reversion", conf, price,
                                     {**details, "reason": "overbought+above_upper", "stoch_confirm": stoch_overbought})
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
        # 修复：用前 lookback 根的最高/最低（不含当前蜡烛），否则突破时 highest=当前high >= close 永远不触发
        highest = Indicators.highest(highs[:-1], lookback)
        lowest = Indicators.lowest(lows[:-1], lookback)
        vol_ma = Indicators.volume_ma(volumes, 20)
        if highest is None or lowest is None:
            return None
        # ADX 趋势强度过滤：ADX < 25 震荡期突破多为假突破
        adx_data = Indicators.adx(candles)
        adx_ok = adx_data and adx_data["adx"] >= 25
        # OBV 能量潮：突破方向需要资金流配合
        obv = Indicators.obv(candles)
        obv_confirm = False
        if obv and len(obv) >= 5:
            recent_obv = obv[-5:]
            obv_confirm = recent_obv[-1] > recent_obv[0]  # OBV 上升 = 资金流入
        details = {"highest": round(highest, 2), "lowest": round(lowest, 2),
                   "current_price": price, "lookback": lookback}
        if adx_data:
            details["adx"] = adx_data["adx"]
        details["obv_confirm"] = obv_confirm
        prev_price = closes[-2]
        vol_confirm = (not vol_ma) or (volumes[-1] > vol_ma * 1.3)
        if prev_price < highest and price >= highest:
            # 量确认 + ADX + OBV 三重确认
            if vol_confirm and adx_ok and obv_confirm:
                conf = 0.85
            elif vol_confirm and (adx_ok or obv_confirm):
                conf = 0.7
            elif vol_confirm:
                conf = 0.6
            else:
                conf = 0.5
            return self._make_signal(SignalAction.BUY, "breakout", conf, price,
                                     {**details, "breakout": "upper", "volume_confirm": vol_confirm, "adx_ok": adx_ok})
        if prev_price > lowest and price <= lowest:
            obv_down = obv and len(obv) >= 5 and obv[-1] < obv[-5]  # OBV 下降 = 资金流出
            details["obv_down"] = obv_down
            if vol_confirm and adx_ok and obv_down:
                conf = 0.85
            elif vol_confirm and (adx_ok or obv_down):
                conf = 0.7
            elif vol_confirm:
                conf = 0.6
            else:
                conf = 0.5
            return self._make_signal(SignalAction.SELL, "breakout", conf, price,
                                     {**details, "breakout": "lower", "volume_confirm": vol_confirm, "adx_ok": adx_ok})
        return None
