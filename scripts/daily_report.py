#!/usr/bin/env python3
"""
daily_report.py - 每日交易报告（Phase 5）
职责: 汇总当日交易、盈亏、策略表现
生成时间: 每天 00:00 UTC

使用方式:
  python3 scripts/daily_report.py
"""

import json
import os
from datetime import datetime, timezone

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(WORKSPACE, "state")
DATA_DIR = os.path.join(WORKSPACE, "data")
LOGS_DIR = os.path.join(WORKSPACE, "logs")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOGS_DIR, "system.log")

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [report] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def read_trade_history():
    """读取交易历史"""
    history_file = os.path.join(STATE_DIR, "trade_history.json")
    if not os.path.exists(history_file):
        return []
    try:
        with open(history_file) as f:
            data = json.load(f)
        return data.get("trades", [])
    except (json.JSONDecodeError, KeyError):
        # 尝试读取 .log 格式
        log_file = history_file + ".log"
        if os.path.exists(log_file):
            trades = []
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            trades.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            return trades
    return []

def read_daily_pnl():
    """读取当日盈亏"""
    daily_file = os.path.join(STATE_DIR, "daily_pnl.json")
    if not os.path.exists(daily_file):
        return {"total_pnl": 0, "trades_count": 0, "date": ""}
    with open(daily_file) as f:
        return json.load(f)

def count_signals():
    """统计今日信号数量"""
    signals_dir = os.path.join(DATA_DIR, "signals")
    if not os.path.exists(signals_dir):
        return 0
    today_prefix = datetime.now(timezone.utc).strftime("%Y%m%d")
    files = [f for f in os.listdir(signals_dir) if f.startswith(f"analysis_{today_prefix}")]
    total_signals = 0
    for f in files:
        try:
            with open(os.path.join(signals_dir, f)) as fh:
                data = json.load(fh)
                total_signals += data.get("summary", {}).get("active_signals", 0)
        except (json.JSONDecodeError, KeyError):
            pass
    return total_signals

def generate_report():
    log("=" * 60)
    log("📊 每日交易报告")
    log("=" * 60)

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    trades = read_trade_history()
    daily_pnl = read_daily_pnl()
    signal_count = count_signals()

    # 筛选今日交易
    today_trades = [t for t in trades if t.get("time", "").startswith(today)]

    # 统计
    total_trades = len(today_trades)
    buys = len([t for t in today_trades if t.get("side") == "long"])
    sells = len([t for t in today_trades if t.get("side") == "short"])
    successes = len([t for t in today_trades if "OK" in t.get("result", "")])
    failures = len([t for t in today_trades if "FAILED" in t.get("result", "")])

    # 按币种统计
    symbol_stats = {}
    for t in today_trades:
        sym = t.get("symbol", "UNKNOWN")
        if sym not in symbol_stats:
            symbol_stats[sym] = {"trades": 0, "buys": 0, "sells": 0}
        symbol_stats[sym]["trades"] += 1
        if t.get("side") == "long":
            symbol_stats[sym]["buys"] += 1
        elif t.get("side") == "short":
            symbol_stats[sym]["sells"] += 1

    # 生成报告
    report = {
        "date": today,
        "generated_at": now.isoformat(),
        "summary": {
            "total_trades": total_trades,
            "buy_trades": buys,
            "sell_trades": sells,
            "successful": successes,
            "failed": failures,
            "signals_generated": signal_count,
            "daily_pnl": daily_pnl.get("total_pnl", 0),
        },
        "by_symbol": symbol_stats,
        "trades": today_trades,
    }

    # 保存报告
    report_file = os.path.join(REPORTS_DIR, f"daily_{today}.json")
    with open(report_file, "w") as f:
        json.dump(report, f, indent=2)

    # 输出文本摘要
    log(f"📅 日期: {today}")
    log(f"📊 总交易数: {total_trades}")
    log(f"  买入: {buys} | 卖出: {sells}")
    log(f"  成功: {successes} | 失败: {failures}")
    log(f"📡 信号数: {signal_count}")
    log(f"💰 当日盈亏: {daily_pnl.get('total_pnl', 0)}")

    if symbol_stats:
        log("📈 按币种:")
        for sym, stats in symbol_stats.items():
            log(f"  {sym}: {stats['trades']} 笔 (多:{stats['buys']} 空:{stats['sells']})")

    log(f"📄 报告已保存: {report_file}")
    log("=" * 60)

    return report

if __name__ == "__main__":
    generate_report()
