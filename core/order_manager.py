#!/usr/bin/env python3
"""
core/order_manager.py — 订单生命周期管理
"""

import json
import os
import time
import logging
from typing import Dict
from .models import Position, PositionSide, AccountState
from .notify import notifier

logger = logging.getLogger("engine.order_manager")

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")


class OrderManager:
    """订单生命周期管理"""

    def __init__(self, client, risk_engine):
        self.client = client
        self.risk = risk_engine
        self._pending_orders: Dict[str, dict] = {}

    def open_position(self, symbol: str, side: PositionSide,
                      quantity: float, leverage: int, price: float,
                      strategy: str = "unknown",
                      account: AccountState = None) -> dict:
        result = {"symbol": symbol, "side": side.value, "quantity": quantity,
                  "leverage": leverage, "status": "pending"}

        try:
            self.client.change_leverage(symbol, leverage)
            time.sleep(0.5)
            binance_side = "BUY" if side == PositionSide.LONG else "SELL"
            qty = self.client.adjust_quantity(symbol, quantity)
            order = self.client.new_order(symbol, binance_side, "MARKET", quantity=qty)
            if "error" in order:
                result["status"] = "failed"
                result["error"] = order["error"]
                logger.error(f"❌ 开仓失败: {order['error']}")
                return result
            result["status"] = "opened"
            result["order_id"] = order.get("orderId", 0)
            result["fill_price"] = float(order.get("avgPrice", 0) or order.get("price", 0) or price)
            result["executed_qty"] = float(order.get("executedQty", qty))
            logger.info(f"✅ 开仓成功: {side.value.upper()} {symbol} {qty} @ {result['fill_price']}")

            sl = self.risk.calc_stop_loss(result["fill_price"], side)
            tp = self.risk.calc_take_profit(result["fill_price"], side)
            result["stop_loss"] = sl
            result["take_profit"] = tp
            self._place_sl_tp(symbol, side, sl, tp)

            try:
                notifier.trade_opened(symbol, side.value, quantity, result['fill_price'],
                                     leverage, strategy, sl, tp)
            except Exception as e:
                logger.warning(f"通知发送失败: {e}")

            self._record_trade("open", symbol, side.value, quantity,
                              result["fill_price"], strategy, "OK")
            return result
        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            logger.error(f"❌ 开仓异常: {e}")
            return result

    def close_position(self, symbol: str,
                       reason: str = "",
                       account: AccountState = None,
                       client=None) -> dict:
        result = {"symbol": symbol, "status": "pending", "reason": reason}

        try:
            positions = self.client.positions(symbol)
            if not positions:
                result["status"] = "no_position"
                return result
            pos = positions[0]
            pos_amt = float(pos["positionAmt"])
            if pos_amt == 0:
                result["status"] = "no_position"
                return result
            close_side = "SELL" if pos_amt > 0 else "BUY"
            qty = abs(pos_amt)
            qty = self.client.adjust_quantity(symbol, qty)
            order = self.client.new_order(symbol, close_side, "MARKET",
                                          quantity=qty, reduce_only=True)
            if "error" in order:
                result["status"] = "failed"
                result["error"] = order["error"]
                return result
            result["status"] = "closed"
            result["fill_price"] = float(order.get("avgPrice", 0))
            result["pnl"] = float(pos.get("unRealizedProfit", 0))
            logger.info(f"✅ 平仓成功: {symbol} {qty} @ {result['fill_price']} "
                       f"(盈亏: {result['pnl']:.4f}, 原因: {reason})")
            self._record_trade("close", symbol,
                              "long" if pos_amt > 0 else "short", qty,
                              result["fill_price"], reason,
                              f"OK pnl={result['pnl']:.4f}")
            try:
                entry_price = float(pos.get("entryPrice", 0))
                pnl_pct_val = (result["fill_price"] - entry_price) / entry_price * 100 * (1 if pos_amt > 0 else -1)
                notifier.trade_closed(symbol, "long" if pos_amt > 0 else "short", qty,
                                     entry_price, result["fill_price"], result["pnl"], pnl_pct_val,
                                     reason)
            except Exception as e:
                logger.warning(f"通知发送失败: {e}")
            return result
        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            logger.error(f"❌ 平仓异常: {e}")
            return result

    def _place_sl_tp(self, symbol: str, side: PositionSide,
                     sl_price: float, tp_price: float):
        try:
            close_side = "SELL" if side == PositionSide.LONG else "BUY"
            sl_price = self.client.adjust_price(symbol, sl_price)
            tp_price = self.client.adjust_price(symbol, tp_price)

            sl_params = {
                "symbol": symbol, "side": close_side,
                "positionSide": "BOTH", "type": "STOP_MARKET",
                "stopPrice": sl_price, "closePosition": "true",
                "workingType": "CONTRACT_PRICE",
            }
            sl_result = self.client._request("POST", "/fapi/v1/algoOrder", sl_params, signed=True)
            if "error" in sl_result:
                logger.warning(f"⚠️ 止损设置失败: {sl_result.get('error', '')}")
            else:
                logger.info(f"🛡 止损已设置: {sl_price}")

            tp_params = {
                "symbol": symbol, "side": close_side,
                "positionSide": "BOTH", "type": "TAKE_PROFIT_MARKET",
                "stopPrice": tp_price, "closePosition": "true",
                "workingType": "CONTRACT_PRICE",
            }
            tp_result = self.client._request("POST", "/fapi/v1/algoOrder", tp_params, signed=True)
            if "error" in tp_result:
                logger.warning(f"⚠️ 止盈设置失败: {tp_result.get('error', '')}")
            else:
                logger.info(f"🎯 止盈已设置: {tp_price}")
        except Exception as e:
            logger.error(f"设置止损止盈异常: {e}")

    def _record_trade(self, action: str, symbol: str, side: str,
                      quantity: float, price: float, strategy: str, result: str):
        history_file = os.path.join(STATE_DIR, "trade_history.json")
        entry = {
            "time": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
            "action": action, "symbol": symbol, "side": side,
            "quantity": quantity, "price": price,
            "strategy": strategy, "result": result,
        }
        try:
            if os.path.exists(history_file):
                with open(history_file) as f:
                    data = json.load(f)
            else:
                data = {"trades": []}
            data["trades"].append(entry)
            with open(history_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"记录交易失败: {e}")
