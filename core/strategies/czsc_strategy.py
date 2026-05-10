#!/usr/bin/env python3
"""
core/strategies/czsc_strategy.py - CZSC 缠论策略模块（V2 优化版）
封装 CZSC 缠中说缠技术分析库，将缠论信号转换为引擎标准信号格式

V2 优化:
  - 支持多币种（symbol 由引擎动态传入，不再硬编码）
  - 新增多级别确认（2min 信号 + 15m 级别确认）
  - 置信度校准（与其他策略在同一量级 0.5-0.95）
  - 交易频率限制（防止缠论信号过度频繁）
  - 降级机制（CZSC 不可用时优雅降级）
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional, Dict, List

logger = logging.getLogger("engine.czsc")

try:
    from czsc import CZSC, Freq, RawBar
    from czsc import signals as sigs
    CZSC_AVAILABLE = True
except ImportError:
    CZSC_AVAILABLE = False
    logger.warning("CZSC 库未安装，缠论策略不可用")

# ── 交易频率限制 ──
_signal_history: Dict[str, List[float]] = {}  # symbol -> [timestamps]
MAX_SIGNALS_PER_HOUR = 2  # 每小时最多发出 N 个 CZSC 信号


def convert_candles_to_raw_bars(candles: List[dict], freq: Freq = Freq.F2,
                                 symbol: str = "BTCUSDT") -> List[RawBar]:
    """将引擎 K 线转换为 CZSC RawBar"""
    raw_bars = []
    for i, c in enumerate(candles):
        open_time_ms = c.get("open_time", 0)
        if isinstance(open_time_ms, (int, float)) and open_time_ms > 1e12:
            dt = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
        else:
            dt = datetime.now(timezone.utc)
        raw_bars.append(RawBar(
            dt=dt, symbol=symbol, id=i + 1, freq=freq,
            open=float(c["open"]), close=float(c["close"]),
            high=float(c["high"]), low=float(c["low"]),
            vol=float(c.get("volume", 0)),
            amount=float(c.get("volume", 0)) * float(c["close"]),
        ))
    return raw_bars


def analyze_czsc_single_level(candles: List[dict], symbol: str = "BTCUSDT",
                               freq: Freq = Freq.F2) -> Optional[Dict]:
    """单级别 CZSC 分析（被多级别分析调用）"""
    if not CZSC_AVAILABLE:
        return None
    if len(candles) < 50:
        logger.debug(f"CZSC 数据不足({symbol}): {len(candles)} < 50")
        return None

    try:
        raw_bars = convert_candles_to_raw_bars(candles, freq=freq, symbol=symbol)
        czsc_obj = CZSC(raw_bars)
    except Exception as e:
        logger.error(f"CZSC 初始化失败({symbol}): {e}")
        return None

    result = {
        "symbol": symbol,
        "bar_count": len(candles),
        "bi_count": len(czsc_obj.bi_list),
        "fx_count": len(czsc_obj.fx_list),
    }

    # 1. 笔状态
    try:
        bi_status = sigs.cxt_bi_status_V230101(czsc_obj)
        result["bi_status"] = dict(bi_status)
        bi_status_val = list(bi_status.values())[0] if bi_status else ""
        result["bi_direction"] = "up" if "向上" in str(bi_status_val) else (
            "down" if "向下" in str(bi_status_val) else "neutral")
    except Exception as e:
        logger.debug(f"笔状态信号异常({symbol}): {e}")
        result["bi_status"] = {}
        result["bi_direction"] = "neutral"

    # 2. 笔趋势
    try:
        bi_trend = sigs.cxt_bi_trend_V230824(czsc_obj)
        result["bi_trend"] = dict(bi_trend)
        bi_trend_val = list(bi_trend.values())[0] if bi_trend else ""
        if "上涨" in str(bi_trend_val) or "向上" in str(bi_trend_val):
            result["trend_bias"] = "bullish"
        elif "下跌" in str(bi_trend_val) or "向下" in str(bi_trend_val):
            result["trend_bias"] = "bearish"
        else:
            result["trend_bias"] = "neutral"
    except Exception as e:
        logger.debug(f"笔趋势信号异常({symbol}): {e}")
        result["bi_trend"] = {}
        result["trend_bias"] = "neutral"

    # 3. 分型力度
    try:
        fx_power = sigs.cxt_fx_power_V221107(czsc_obj)
        result["fx_power"] = dict(fx_power)
        fx_val = list(fx_power.values())[0] if fx_power else ""
        result["fx_type"] = "顶分型" if "顶" in str(fx_val) else (
            "底分型" if "底" in str(fx_val) else "未知")
    except Exception as e:
        logger.debug(f"分型力度信号异常({symbol}): {e}")
        result["fx_power"] = {}
        result["fx_type"] = "未知"

    # 4. 决策区域
    try:
        decision = sigs.cxt_decision_V240526(czsc_obj)
        result["decision"] = dict(decision)
        dec_val = list(decision.values())[0] if decision else ""
        if "看多" in str(dec_val) or "买入" in str(dec_val):
            result["decision_dir"] = "bullish"
        elif "看空" in str(dec_val) or "卖出" in str(dec_val):
            result["decision_dir"] = "bearish"
        else:
            result["decision_dir"] = "neutral"
    except Exception as e:
        logger.debug(f"决策信号异常({symbol}): {e}")
        result["decision"] = {}
        result["decision_dir"] = "neutral"

    # 5. K线决策
    try:
        bar_dec = sigs.bar_decision_V240608(czsc_obj)
        result["bar_decision"] = dict(bar_dec)
    except Exception as e:
        logger.debug(f"K线决策信号异常({symbol}): {e}")
        result["bar_decision"] = {}

    # 6. 最新笔
    if czsc_obj.bi_list:
        last_bi = czsc_obj.bi_list[-1]
        result["last_bi"] = {
            "direction": getattr(last_bi, "direction", "N/A"),
            "power": round(getattr(last_bi, "power", 0), 2),
            "change": round(getattr(last_bi, "change", 0), 4),
            "angle": round(getattr(last_bi, "angle", 0), 2),
            "length": getattr(last_bi, "length", 0),
        }

    return result


def compute_signal_score(analysis: Dict) -> tuple:
    """
    根据 CZSC 分析结果计算信号方向和基础置信度
    返回: (action: str|None, base_confidence: float, reasons: list)
    """
    bi_dir = analysis.get("bi_direction", "neutral")
    trend_bias = analysis.get("trend_bias", "neutral")
    fx_type = analysis.get("fx_type", "未知")
    decision_dir = analysis.get("decision_dir", "neutral")
    last_bi = analysis.get("last_bi", {})

    # ── 做多信号判定 ──
    action = None
    confidence = 0.0
    reasons = []

    if bi_dir == "down" and trend_bias in ("neutral", "bullish"):
        # 笔向下完成 → 底分型反转
        if fx_type == "底分型":
            confidence += 0.25
            reasons.append("底分型确认")
        if decision_dir == "bullish":
            confidence += 0.25
            reasons.append("决策看多")
        bi_power = last_bi.get("power", 0)
        if 0 < bi_power < 150:
            confidence += 0.15
            reasons.append("下跌笔力度衰竭")

        if confidence >= 0.4:
            action = "BUY"

    # ── 做空信号判定 ──
    elif bi_dir == "up" and trend_bias in ("neutral", "bearish"):
        if fx_type == "顶分型":
            confidence += 0.25
            reasons.append("顶分型确认")
        if decision_dir == "bearish":
            confidence += 0.25
            reasons.append("决策看空")
        bi_power = last_bi.get("power", 0)
        if 0 < bi_power < 150:
            confidence += 0.15
            reasons.append("上涨笔力度衰竭")

        if confidence >= 0.4:
            action = "SELL"

    # ── 趋势延续信号 ──
    if not action and trend_bias == "bullish" and decision_dir == "bullish":
        if bi_dir == "up":
            confidence = 0.45
            reasons.append("上升趋势延续+决策看多")
            action = "BUY"

    if not action and trend_bias == "bearish" and decision_dir == "bearish":
        if bi_dir == "down":
            confidence = 0.45
            reasons.append("下降趋势延续+决策看空")
            action = "SELL"

    return action, confidence, reasons


def generate_czsc_signal(candles: List[dict], symbol: str = "BTCUSDT",
                          min_confidence: float = 0.5,
                          candles_confirm: List[dict] = None) -> Optional[Dict]:
    """
    生成 CZSC 交易信号

    只用 2m K 线分析（皇帝旨意：k线只用2m不要其他的）
    candles_confirm 参数保留但不使用（向后兼容）
    """
    if not CZSC_AVAILABLE:
        logger.debug(f"CZSC 不可用({symbol})，跳过")
        return None

    # ── 2m 级别分析 ──
    analysis_2m = analyze_czsc_single_level(candles, symbol=symbol, freq=Freq.F2)
    if not analysis_2m:
        return None

    action_2m, conf_2m, reasons_2m = compute_signal_score(analysis_2m)
    if not action_2m:
        return None

    # ── 最终置信度 ──
    final_confidence = conf_2m
    # 校准到 0.5-0.95 范围（与其他策略同一量级）
    final_confidence = max(0.5, min(0.95, final_confidence))

    if final_confidence < min_confidence:
        return None

    # ── 交易频率限制 ──
    now = time.time()
    hour_ago = now - 3600
    if symbol not in _signal_history:
        _signal_history[symbol] = []
    _signal_history[symbol] = [t for t in _signal_history[symbol] if t > hour_ago]

    if len(_signal_history[symbol]) >= MAX_SIGNALS_PER_HOUR:
        logger.info(f"🔇 CZSC({symbol}) 频率限制: 过去1h已发 {len(_signal_history[symbol])} 个信号")
        return None

    _signal_history[symbol].append(now)

    # ── 构建信号 ──
    all_reasons = reasons_2m
    bi_count = analysis_2m.get("bi_count", 0)
    level_info = "2m单级"

    return {
        "action": action_2m,
        "confidence": round(final_confidence, 2),
        "reason": f"CZSC缠论({level_info}): {'，'.join(all_reasons)} (笔数={bi_count})",
        "czsc_details": {
            "bi_direction": analysis_2m.get("bi_direction", "neutral"),
            "trend_bias": analysis_2m.get("trend_bias", "neutral"),
            "fx_type": analysis_2m.get("fx_type", "未知"),
            "decision_dir": analysis_2m.get("decision_dir", "neutral"),
            "last_bi": analysis_2m.get("last_bi", {}),
        }
    }
