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


class Notifier:
    """统一通知管理器"""

    def __init__(self):
        self.enabled = NOTIFY_ENABLED
        self.token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self._last_alert_time = 0
        self._alert_cooldown = 60  # 同一类型告警 60 秒内不重复

    def _log_notify(self, text: str):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(notify_log, "a") as f:
            f.write(f"[{ts}] {text}\n")

    async def _send_telegram(self, text: str, parse_mode: str = "HTML"):
        """发送 Telegram 消息"""
        if not self.enabled:
            return False

        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        self._log_notify(f"Telegram OK: {text[:80]}...")
                        return True
                    else:
                        self._log_notify(f"Telegram ERR {resp.status}: {text[:80]}...")
                        return False
        except ImportError:
            # 如果没有 aiohttp，用 requests 同步发送
            try:
                import requests
                url = f"https://api.telegram.org/bot{self.token}/sendMessage"
                payload = {
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                }
                resp = requests.post(url, json=payload, timeout=10)
                if resp.status_code == 200:
                    self._log_notify(f"Telegram OK: {text[:80]}...")
                    return True
                else:
                    self._log_notify(f"Telegram ERR {resp.status_code}: {text[:80]}...")
                    return False
            except Exception as e:
                logger.error(f"Telegram 发送失败: {e}")
                return False
        except Exception as e:
            logger.error(f"Telegram 发送失败: {e}")
            return False

    def send_sync(self, text: str, parse_mode: str = "HTML"):
        """同步发送（阻塞）"""
        if not self.enabled:
            logger.info(f"[通知] {text[:100]}...")
            return

        try:
            import requests
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                self._log_notify(f"Telegram OK: {text[:80]}...")
            else:
                self._log_notify(f"Telegram ERR {resp.status_code}: {text[:80]}...")
        except Exception as e:
            logger.error(f"通知发送失败: {e}")
            self._log_notify(f"通知发送失败: {e}")

    # ── 通知模板 ──

    def trade_opened(self, symbol: str, side: str, quantity: float,
                     price: float, leverage: int, strategy: str,
                     stop_loss: float = 0, take_profit: float = 0,
                     dry_run: bool = True):
        """开仓通知"""
        emoji = "📈" if side == "long" else "📉"
        mode = "🔵 模拟" if dry_run else "🟢 实盘"
        text = (
            f"{mode} {emoji} <b>开仓信号</b>\n"
            f"币种: <b>{symbol}</b>\n"
            f"方向: {side.upper()}\n"
            f"数量: {quantity:.6f}\n"
            f"价格: {price:,.2f} USDT\n"
            f"杠杆: {leverage}x\n"
            f"策略: {strategy}\n"
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
        text = (
            f"{mode} {emoji} <b>平仓</b>\n"
            f"币种: <b>{symbol}</b>\n"
            f"方向: {side.upper()}\n"
            f"入场: {entry_price:,.2f} → 出场: {exit_price:,.2f}\n"
            f"盈亏: <b>{pnl:+.2f} USDT ({pnl_pct:+.2f}%)</b>\n"
            f"原因: {reason}"
        )
        self.send_sync(text)

    def risk_warning(self, symbol: str, message: str, level: str = "warning"):
        """风控警告"""
        if level == "critical":
            emoji = "🚨"
        elif level == "warning":
            emoji = "⚠️"
        else:
            emoji = "ℹ️"

        text = f"{emoji} <b>风控{level.upper()}</b>\n币种: <b>{symbol}</b>\n{message}"
        self.send_sync(text)

    def signal_alert(self, symbol: str, action: str, strategy: str,
                     confidence: float, price: float):
        """交易信号提醒（未执行）"""
        emoji = "📡"
        text = (
            f"{emoji} <b>交易信号</b>\n"
            f"币种: <b>{symbol}</b>\n"
            f"方向: {action.upper()}\n"
            f"策略: {strategy}\n"
            f"置信度: {confidence:.0%}\n"
            f"价格: {price:,.2f}"
        )
        self.send_sync(text)

    def daily_summary(self, trades: int, wins: int, losses: int,
                      pnl: float, win_rate: float, equity: float,
                      max_dd: float):
        """每日总结"""
        emoji = "📊"
        text = (
            f"{emoji} <b>每日交易总结</b>\n"
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
