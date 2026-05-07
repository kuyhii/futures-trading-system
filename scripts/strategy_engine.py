#!/usr/bin/env python3
"""
strategy_engine.py - 策略引擎（Phase 2）
职责: 技术指标计算 + 策略信号生成
数据源: data/klines/ 目录下的 K 线数据
信号输出: data/signals/ 目录

使用方式:
  python3 scripts/strategy_engine.py                  # 分析所有配置币种
  python3 scripts/strategy_engine.py BTCUSDT 15m      # 分析指定币种和时间框架
"""

import json
import os
import sys
import math
from datetime import datetime, timezone

# ── 路径配置 ──
WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(WORKSPACE, "config")
DATA_DIR = os.path.join(WORKSPACE, "data", "klines")
SIGNALS_DIR = os.path.join(WORKSPACE, "data", "signals")
LOG_FILE = os.path.join(WORKSPACE, "logs", "system.log")

os.makedirs(SIGNALS_DIR, exist_ok=True)

# ── 工具函数 ──
def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [strategy] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def load_config():
    with open(os.path.join(CONFIG_DIR, "strategies.json")) as f:
        return json.load(f)

def load_risk_config():
    with open(os.path.join(CONFIG_DIR, "risk.json")) as f:
        return json.load(f)

def load_symbols():
    with open(os.path.join(CONFIG_DIR, "symbols.json")) as f:
        data = json.load(f)
    return [s for s in data["watchlist"] if s.get("enabled", True)]

def get_latest_kline(symbol, timeframe):
    """获取最新的 K 线数据文件"""
    prefix = f"{symbol}_{timeframe}_"
    files = sorted([f for f in os.listdir(DATA_DIR) if f.startswith(prefix)])
    if not files:
        return None
    filepath = os.path.join(DATA_DIR, files[-1])
    with open(filepath) as f:
        return json.load(f)

def parse_klines(raw_data):
    """解析 K 线数据为 OHLCV 列表
    binance K 线格式: [
        [open_time, open, high, low, close, volume, close_time, ...],
        ...
    ]
    """
    candles = []
    if isinstance(raw_data, list):
        for k in raw_data:
            if isinstance(k, list) and len(k) >= 6:
                candles.append({
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                })
    elif isinstance(raw_data, dict):
        # 可能是单条数据
        if "klines" in raw_data:
            for k in raw_data["klines"]:
                if isinstance(k, list) and len(k) >= 6:
                    candles.append({
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    })
    return candles

# ── 技术指标计算 ──

def calc_ma(closes, period):
    """简单移动平均线"""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period

def calc_ema(closes, period):
    """指数移动平均线"""
    if len(closes) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calc_rsi(closes, period=14):
    """相对强弱指标"""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(0, change))
        losses.append(max(0, -change))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_macd(closes, fast=12, slow=26, signal=9):
    """MACD 指标"""
    if len(closes) < slow + signal:
        return None, None, None

    ema_fast = []
    ema_slow = []
    multiplier_fast = 2 / (fast + 1)
    multiplier_slow = 2 / (slow + 1)

    # 计算 EMA
    ef = sum(closes[:fast]) / fast
    es = sum(closes[:slow]) / slow
    ema_fast.append(ef)
    ema_slow.append(es)

    for price in closes[1:]:
        ef = (price - ef) * multiplier_fast + ef
        es = (price - es) * multiplier_slow + es
        ema_fast.append(ef)
        ema_slow.append(es)

    # MACD 线
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]

    # Signal 线
    if len(macd_line) < signal:
        return None, None, None
    sig = sum(macd_line[:signal]) / signal
    signal_line = [sig]
    for val in macd_line[signal:]:
        sig = (val - sig) * (2 / (signal + 1)) + sig
        signal_line.append(sig)

    histogram = macd_line[-1] - signal_line[-1]
    return macd_line[-1], signal_line[-1], histogram

def calc_bollinger(closes, period=20, std_dev=2.0):
    """布林带"""
    if len(closes) < period:
        return None, None, None
    recent = closes[-period:]
    middle = sum(recent) / period
    variance = sum((x - middle) ** 2 for x in recent) / period
    std = math.sqrt(variance)
    return middle - std_dev * std, middle, middle + std_dev * std

def calc_volume_ma(volumes, period=20):
    """成交量均线"""
    if len(volumes) < period:
        return None
    return sum(volumes[-period:]) / period

# ── 策略实现 ──

def strategy_trend_follow(candles, config):
    """趋势跟踪: MA 金叉/死叉 + 成交量确认"""
    if len(candles) < 30:
        return None

    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]
    fast_period = config["indicators"]["ma_fast"]
    slow_period = config["indicators"]["ma_slow"]

    ma_fast = calc_ma(closes, fast_period)
    ma_slow = calc_ma(closes, slow_period)

    # 前一期 MA
    ma_fast_prev = calc_ma(closes[:-1], fast_period)
    ma_slow_prev = calc_ma(closes[:-1], slow_period)

    vol_ma = calc_volume_ma(volumes, 20)
    current_vol = volumes[-1]

    if not all([ma_fast, ma_slow, ma_fast_prev, ma_slow_prev]):
        return None

    signal = {"type": "trend_follow", "action": "hold", "confidence": 0}

    # 金叉: 快线从下穿到上
    if ma_fast_prev <= ma_slow_prev and ma_fast > ma_slow:
        if vol_ma and current_vol > vol_ma * 1.2:
            signal = {"type": "trend_follow", "action": "buy", "confidence": 0.8}
            log(f"📈 趋势跟踪金叉 + 放量: MA{fast_period}={ma_fast:.2f} > MA{slow_period}={ma_slow:.2f}, 量={current_vol:.0f}")
        else:
            signal = {"type": "trend_follow", "action": "buy", "confidence": 0.5}

    # 死叉: 快线从上穿到下
    elif ma_fast_prev >= ma_slow_prev and ma_fast < ma_slow:
        signal = {"type": "trend_follow", "action": "sell", "confidence": 0.8}

    signal["details"] = {
        "ma_fast": round(ma_fast, 2),
        "ma_slow": round(ma_slow, 2),
        "current_price": closes[-1],
    }
    return signal

def strategy_mean_reversion(candles, config):
    """均值回归: RSI 超买超卖 + 布林带"""
    if len(candles) < 30:
        return None

    closes = [c["close"] for c in candles]
    rsi = calc_rsi(closes, config["indicators"]["rsi_period"])
    lower, middle, upper = calc_bollinger(closes,
        config["indicators"]["bollinger_period"],
        config["indicators"]["bollinger_std"])

    if rsi is None or lower is None:
        return None

    signal = {"type": "mean_reversion", "action": "hold", "confidence": 0}

    # RSI 超卖 + 价格触及下轨 → 做多
    if rsi < config["indicators"]["rsi_oversold"] and closes[-1] < lower:
        signal = {"type": "mean_reversion", "action": "buy", "confidence": 0.7}
        log(f"📉 均值回归超卖信号: RSI={rsi:.1f}, 价格={closes[-1]:.2f} < 下轨={lower:.2f}")

    # RSI 超买 + 价格触及上轨 → 做空
    elif rsi > config["indicators"]["rsi_overbought"] and closes[-1] > upper:
        signal = {"type": "mean_reversion", "action": "sell", "confidence": 0.7}
        log(f"📈 均值回归超买信号: RSI={rsi:.1f}, 价格={closes[-1]:.2f} > 上轨={upper:.2f}")

    signal["details"] = {
        "rsi": round(rsi, 2),
        "bollinger_lower": round(lower, 2),
        "bollinger_middle": round(middle, 2),
        "bollinger_upper": round(upper, 2),
        "current_price": closes[-1],
    }
    return signal

def strategy_funding_rate(funding_data, config):
    """资金费率套利（需要额外传入资金费率数据）"""
    if not funding_data or "fundingRate" not in str(funding_data):
        return None

    threshold = config["strategies"]["funding_rate"]["threshold"]

    # 解析资金费率
    rate = 0
    if isinstance(funding_data, dict) and "fundingRate" in funding_data:
        rate = float(funding_data["fundingRate"])
    elif isinstance(funding_data, list) and len(funding_data) > 0:
        rate = float(funding_data[-1].get("fundingRate", 0))

    signal = {"type": "funding_rate", "action": "hold", "confidence": 0}

    if rate > threshold:
        signal = {"type": "funding_rate", "action": "sell", "confidence": 0.6}
        log(f"💰 资金费率偏高: {rate:.4%}, 反向做空信号")
    elif rate < -threshold:
        signal = {"type": "funding_rate", "action": "buy", "confidence": 0.6}
        log(f"💰 资金费率偏低: {rate:.4%}, 反向做多信号")

    signal["details"] = {"funding_rate": rate}
    return signal

# ── 主引擎 ──

def analyze_symbol(symbol, timeframe, config):
    """分析单个币种的所有策略"""
    log(f"分析 {symbol} ({timeframe})...")

    raw = get_latest_kline(symbol, timeframe)
    if not raw:
        log(f"  ⚠️  无 K 线数据，跳过")
        return None

    candles = parse_klines(raw)
    if len(candles) < 30:
        log(f"  ⚠️  K 线数据不足 ({len(candles)} 条)，至少需要 30 条")
        return None

    signals = []

    # 运行启用的策略
    for strategy_name, strategy_cfg in config["strategies"].items():
        if not strategy_cfg.get("enabled", False):
            continue

        sig = None
        if strategy_name == "trend_follow":
            sig = strategy_trend_follow(candles, config)
        elif strategy_name == "mean_reversion":
            sig = strategy_mean_reversion(candles, config)
        elif strategy_name == "funding_rate":
            sig = strategy_funding_rate(raw, config)

        if sig and sig["action"] != "hold":
            signals.append(sig)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "candles_count": len(candles),
        "current_price": candles[-1]["close"] if candles else None,
        "signals": signals,
    }

def main():
    config = load_config()
    timeframe = config["timeframe"]

    # 命令行参数覆盖
    if len(sys.argv) >= 3:
        symbols = [{"symbol": sys.argv[1]}]
        timeframe = sys.argv[2] if len(sys.argv) >= 3 else timeframe
    else:
        symbols = load_symbols()

    log("=" * 50)
    log("策略引擎启动")
    log(f"监控 {len(symbols)} 个币种, 时间框架: {timeframe}")
    log("=" * 50)

    all_results = []
    active_signals = []

    for sym_cfg in symbols:
        symbol = sym_cfg["symbol"]
        result = analyze_symbol(symbol, timeframe, config)
        if result:
            all_results.append(result)
            active_signals.extend(result["signals"])

    # 保存分析结果
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(SIGNALS_DIR, f"analysis_{ts}.json")
    with open(output_file, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "timeframe": timeframe,
            "results": all_results,
            "summary": {
                "total_symbols": len(all_results),
                "active_signals": len(active_signals),
                "buy_signals": len([s for s in active_signals if s["action"] == "buy"]),
                "sell_signals": len([s for s in active_signals if s["action"] == "sell"]),
            }
        }, f, indent=2)
    log(f"分析结果已保存: {output_file}")

    # 输出信号摘要
    if active_signals:
        log(f"🚨 发现 {len(active_signals)} 个活跃信号:")
        for sig in active_signals:
            log(f"  {sig['type']}: {sig['action'].upper()} (confidence: {sig['confidence']})")
    else:
        log("ℹ️  无活跃交易信号")

    log("=" * 50)

    return active_signals

if __name__ == "__main__":
    signals = main()
    # 如果有信号，输出 JSON 供其他脚本读取
    if signals and "--json" in sys.argv:
        print(json.dumps(signals, indent=2))
