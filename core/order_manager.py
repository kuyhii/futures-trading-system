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
from .notify import notifier, _fmt_price

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
            self._set_leverage_safe(symbol, leverage)
            time.sleep(0.5)
            binance_side = "BUY" if side == PositionSide.LONG else "SELL"
            pos_side = "LONG" if side == PositionSide.LONG else "SHORT"
            order = self.client.new_order(symbol, binance_side, "MARKET", quantity=quantity, position_side=pos_side)
            if "error" in order:
                result["status"] = "failed"
                result["error"] = order["error"]
                logger.error(f"❌ 开仓失败: {order['error']}")
                return result
            result["status"] = "opened"
            result["order_id"] = order.get("orderId", 0)

            # 获取实际成交价：多重回退确保不为 0
            api_price = 0.0
            # 1. avgPrice（币安标准格式）
            if order.get("avgPrice") and float(order["avgPrice"]) > 0:
                api_price = float(order["avgPrice"])
            # 2. fills[0].price（逐笔成交）
            elif order.get("fills") and len(order["fills"]) > 0:
                api_price = float(order["fills"][0].get("price", 0))
            # 3. price 字段（某些测试网格式）
            elif order.get("price") and float(order["price"]) > 0:
                api_price = float(order["price"])
            # 4. 回退：输入价格
            if api_price <= 0:
                api_price = price
                logger.warning(f"⚠️ API未返回成交价，使用信号价格 {_fmt_price(price)}")
            fill_price = api_price
            result["fill_price"] = fill_price
            result["executed_qty"] = float(order.get("executedQty", quantity))

            logger.info(f"✅ 开仓成功: {side.value.upper()} {symbol} {result['executed_qty']} @ {_fmt_price(fill_price)}")

            # 验证止损止盈价格有效性
            sl = self.risk.calc_stop_loss(api_price, side)
            tp = self.risk.calc_take_profit(api_price, side)
            if sl <= 0 or tp <= 0:
                logger.error(f"❌ 止损止盈价格异常: sl={_fmt_price(sl)}, tp={_fmt_price(tp)}, entry={_fmt_price(api_price)}")
                result["status"] = "failed"
                result["error"] = "止损止盈价格计算异常"
                return result
            result["stop_loss"] = sl
            result["take_profit"] = tp
            self._place_sl_tp(symbol, side, sl, tp)

            try:
                notifier.trade_opened(symbol, side.value, quantity, result['fill_price'],
                                     leverage, strategy, sl, tp, dry_run=False)
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
            pos_side = "LONG" if pos_amt > 0 else "SHORT"
            qty = abs(pos_amt)
            qty = self.client.adjust_quantity(symbol, qty)
            order = self.client.new_order(symbol, close_side, "MARKET",
                                          quantity=qty, reduce_only=True,
                                          position_side=pos_side)
            if "error" in order:
                result["status"] = "failed"
                result["error"] = order["error"]
                return result
            result["status"] = "closed"
            result["fill_price"] = float(order.get("avgPrice", 0))
            result["pnl"] = float(pos.get("unRealizedProfit", 0))
            logger.info(f"✅ 平仓成功: {symbol} {qty} @ {_fmt_price(result['fill_price'])} "
                       f"(盈亏: {result['pnl']:.2f}U, 原因: {reason})")
            self._record_trade("close", symbol,
                              "long" if pos_amt > 0 else "short", qty,
                              result["fill_price"], reason,
                              f"OK pnl={result['pnl']:.2f}")
            try:
                entry_price = float(pos.get("entryPrice", 0))
                pnl_pct_val = (result["fill_price"] - entry_price) / entry_price * 100 * (1 if pos_amt > 0 else -1)
                notifier.trade_closed(symbol, "long" if pos_amt > 0 else "short", qty,
                                     entry_price, result["fill_price"], result["pnl"], pnl_pct_val,
                                     reason, dry_run=False)
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
            pos_side = "LONG" if side == PositionSide.LONG else "SHORT"  # 双向持仓模式
            sl_price = self.client.adjust_price(symbol, sl_price)
            tp_price = self.client.adjust_price(symbol, tp_price)

            # ── 测试网 Algo Order API 格式 ──
            # algotype=CONDITIONAL, type=STOP_MARKET/TAKE_PROFIT_MARKET, triggerprice(小写!)
            # 生产环境用 Algo Order API
            sl_params = {
                "algotype": "CONDITIONAL",
                "symbol": symbol,
                "side": close_side,
                "positionSide": pos_side,
                "type": "STOP_MARKET",
                "triggerprice": sl_price,
                "closePosition": "true",
                "workingType": "CONTRACT_PRICE",
            }
            sl_result = self.client._request("POST", "/fapi/v1/algoOrder", sl_params, signed=True)
            if "error" in sl_result:
                logger.warning(f"⚠️ 止损设置失败: {sl_result.get('error', '')}")
            else:
                logger.info(f"🛡 止损已设置: {_fmt_price(sl_price)}")

            tp_params = {
                "algotype": "CONDITIONAL",
                "symbol": symbol,
                "side": close_side,
                "positionSide": pos_side,
                "type": "TAKE_PROFIT_MARKET",
                "triggerprice": tp_price,
                "closePosition": "true",
                "workingType": "CONTRACT_PRICE",
            }
            tp_result = self.client._request("POST", "/fapi/v1/algoOrder", tp_params, signed=True)
            if "error" in tp_result:
                logger.warning(f"⚠️ 止盈设置失败: {tp_result.get('error', '')}")
            else:
                logger.info(f"🎯 止盈已设置: {_fmt_price(tp_price)}")
        except Exception as e:
            logger.error(f"设置止损止盈异常: {e}")


    def _set_leverage_safe(self, symbol: str, target_leverage: int):
        """安全设置杠杆：尝试目标值，失败后逐级降级"""
        for lev in [target_leverage, 10, 5]:
            result = self.client.change_leverage(symbol, lev)
            if "error" not in result:
                logger.info(f"✅ {symbol} 杠杆已设为 {lev}x")
                return lev
            logger.warning(f"⚠️ {symbol} 杠杆 {lev}x 失败: {result.get('error', '')}")
        logger.error(f"❌ {symbol} 所有杠杆级别均失败")
        return target_leverage

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
