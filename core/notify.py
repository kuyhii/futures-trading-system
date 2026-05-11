#!/usr/bin/env python3
"""
core/notify.py - 通知模块
职责: 交易信号/风控警告/每日报告的推送通知
  - Telegram Bot 推送
  - 控制台输出
  - 文件记录

使用方式:
  from core.notify import notifier
  notifier.trade_opened(...)
  notifier.risk_warning(...)
  notifier.daily_report(...)
"""

import json
import os
import logging
import threading
import queue
from datetime import datetime, timezone
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(ROOT, "logs")
STATE_DIR = os.path.join(ROOT, "state")

logger = logging.getLogger("notify")

# ── 配置 ──
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
NOTIFY_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# ── 通知日志 ──
notify_log = os.path.join(LOGS_DIR, "notifications.log")

# ── 中英文映射 ──
SIDE_MAP = {
    "long": "做多",
    "short": "做空",
    "buy": "买入",
    "sell": "卖出",
}

STRATEGY_MAP = {
    "trend_follow": "趋势跟踪",
    "mean_reversion": "均值回归",
    "breakout": "突破",
    "czsc": "缠论",
    "funding_rate": "资金费率",
    "lsr_reversal": "多空比反转",
}

def _side_cn(side: str) -> str:
    return SIDE_MAP.get(side.lower(), side.upper())

def _strategy_cn(strategy: str) -> str:
    parts = [STRATEGY_MAP.get(s.strip().lower(), s.strip()) for s in strategy.split(",")]
    return "，".join(parts)


class Notifier:
    """统一通知管理器（异步发送，不阻塞主循环）"""

    def __init__(self):
        self.enabled = NOTIFY_ENABLED
        self.token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self._last_alert_time = 0
        self._alert_cooldown = 60

        # ── 异步发送队列 ──
        self._send_queue = queue.Queue(maxsize=100)
        self._send_thread = threading.Thread(target=self._send_loop, daemon=True, name="notify_sender")
        self._send_thread.start()

    def _log_notify(self, text: str):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(notify_log, "a") as f:
            f.write(f"[{ts}] {text}\n")

    def _send_loop(self):
        """后台发送线程：从队列中取消息并发送，不阻塞主循环"""
        while True:
            try:
                text, parse_mode = self._send_queue.get(timeout=5)
                self._do_send(text, parse_mode)
                self._send_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"通知发送线程异常: {e}")

    def _do_send(self, text: str, parse_mode: str = "HTML"):
        """实际发送 Telegram 消息"""
        if not self.enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode}
            import requests
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                self._log_notify(f"OK: {text[:80]}")
            else:
                self._log_notify(f"ERR {resp.status_code}: {text[:80]}")
        except Exception as e:
            logger.error(f"Telegram 发送失败: {e}")

    def send_sync(self, text: str, parse_mode: str = "HTML"):
        """异步发送：将消息放入队列，立即返回，不阻塞主循环"""
        if not self.enabled:
            logger.info(f"[通知] {text[:100]}")
            return
        try:
            self._send_queue.put_nowait((text, parse_mode))
        except queue.Full:
            logger.warning("通知队列已满，丢弃消息")

    # ── 通知模板 ──

    def trade_opened(self, symbol: str, side: str, quantity: float,
                     price: float, leverage: int, strategy: str,
                     stop_loss: float = 0, take_profit: float = 0,
                     dry_run: bool = True):
        """开仓通知"""
        emoji = "📈" if side == "long" else "📉"
        mode = "🔵 模拟" if dry_run else "🟢 实盘"
        side_cn = _side_cn(side)
        strategy_cn = _strategy_cn(strategy)
        text = (
            f"{mode} {emoji} <b>开仓</b>\n"
            f"币种: <b>{symbol}</b>\n"
            f"方向: {side_cn}\n"
            f"数量: {quantity:.6f}\n"
            f"价格: {price:,.2f} USDT\n"
            f"杠杆: {leverage}x\n"
            f"策略: {strategy_cn}\n"
        )
        if stop_loss:
            text += f"止损: {stop_loss:,.2f}\n"
        if take_profit:
            text += f"止盈: {take_profit:,.2f}\n"
        self.send_sync(text)

    def trade_closed(self, symbol: str, side: str, quantity: float,
                     entry_price: float, exit_price: float,
                     pnl: float, pnl_pct: float, reason: str = "",
                     dry_run: bool = True):
        """平仓通知"""
        emoji = "✅" if pnl > 0 else "❌"
        mode = "🔵 模拟" if dry_run else "🟢 实盘"
        side_cn = _side_cn(side)
        reason_map = {
            "stop_loss": "止损",
            "take_profit": "止盈",
            "trailing_stop": "追踪止损",
            "signal_reverse": "信号反转",
            "emergency": "紧急平仓",
        }
        reason_cn = reason_map.get(reason.lower(), reason)
        text = (
            f"{mode} {emoji} <b>平仓</b>\n"
            f"币种: <b>{symbol}</b>\n"
            f"方向: {side_cn}\n"
            f"入场: {entry_price:,.2f} → 出场: {exit_price:,.2f}\n"
            f"盈亏: <b>{pnl:+.2f} USDT ({pnl_pct:+.2f}%)</b>\n"
            f"原因: {reason_cn}"
        )
        self.send_sync(text)

    def risk_warning(self, symbol: str, message: str, level: str = "warning"):
        """风控警告"""
        level_map = {"critical": "🚨 严重", "warning": "⚠️ 警告", "info": "ℹ️ 提示"}
        emoji = level_map.get(level.lower(), "⚠️")
        text = f"{emoji} <b>风控</b>\n币种: <b>{symbol}</b>\n{message}"
        self.send_sync(text)

    def signal_alert(self, symbol: str, action: str, strategy: str,
                     confidence: float, price: float):
        """交易信号提醒"""
        side_cn = _side_cn(action)
        strategy_cn = _strategy_cn(strategy)
        text = (
            f"📡 <b>交易信号</b>\n"
            f"币种: <b>{symbol}</b>\n"
            f"方向: {side_cn}\n"
            f"策略: {strategy_cn}\n"
            f"置信度: {confidence:.0%}\n"
            f"价格: {price:,.2f}"
        )
        self.send_sync(text)

    def daily_summary(self, trades: int, wins: int, losses: int,
                      pnl: float, win_rate: float, equity: float,
                      max_dd: float):
        """每日总结"""
        text = (
            f"📊 <b>每日交易总结</b>\n"
            f"交易次数: {trades} ({wins}胜 {losses}负)\n"
            f"胜率: {win_rate:.1f}%\n"
            f"盈亏: <b>{pnl:+.2f} USDT</b>\n"
            f"权益: {equity:,.2f} USDT\n"
            f"最大回撤: {max_dd:.2f}%"
        )
        self.send_sync(text)

    def system_status(self, status: str, message: str = ""):
        """系统状态通知"""
        emoji = "✅" if "ok" in status.lower() else "⚠️"
        text = f"{emoji} <b>系统状态: {status}</b>\n{message}"
        self.send_sync(text)


# ── 全局实例 ──
notifier = Notifier()
