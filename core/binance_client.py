#!/usr/bin/env python3
"""
core/binance_client.py — 币安 U 本位合约 REST API 封装
"""

import time
import hmac
import hashlib
import requests
import logging
from typing import Optional, List

logger = logging.getLogger("engine.binance_client")


class BinanceClient:
    """币安 U 本位合约 REST API 封装"""

    def __init__(self, api_key: str = "", api_secret: str = "", base_url: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key} if api_key else {})
        self._req_count = 0
        self._last_error_time = 0
        self._consecutive_errors = 0
        self.circuit_breaker = False
        self._circuit_cooldown_ms = 60_000
        self._max_circuit_cooldown_ms = 900_000

    def _sign(self, params: dict) -> dict:
        if not self.api_secret:
            return params
        params["timestamp"] = int(time.time() * 1000)
        query = "&".join([f"{k}={v}" for k, v in params.items()])
        signature = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _request(self, method: str, path: str, params: dict = None,
                 signed: bool = False, retries: int = 3) -> dict:
        if self.circuit_breaker:
            cooldown_s = self._circuit_cooldown_ms / 1000
            if time.time() - self._last_error_time < cooldown_s:
                return {"error": f"API 熔断中，等待冷却 {cooldown_s:.0f}s", "circuit_breaker": True}
            self.circuit_breaker = False
            self._consecutive_errors = 0  # 重置错误计数，防止恢复后立刻再次熔断
            logger.warning(f"熔断已解除，恢复请求（上次冷却 {cooldown_s:.0f}s）")
            self._circuit_cooldown_ms = 60_000

        url = f"{self.base_url}{path}"
        params = params or {}
        if signed:
            params = self._sign(params)

        for attempt in range(retries):
            try:
                if method == "GET":
                    resp = self.session.get(url, params=params, timeout=10)
                elif method == "POST":
                    resp = self.session.post(url, params=params, timeout=10)
                elif method == "DELETE":
                    resp = self.session.delete(url, params=params, timeout=10)
                else:
                    raise ValueError(f"Unknown method: {method}")

                self._req_count += 1

                if resp.status_code == 429:
                    wait = min(2 ** attempt, 10)
                    logger.warning(f"⚠️ 429 频率限制，等待 {wait}s 后重试")
                    time.sleep(wait)
                    continue

                if resp.status_code >= 400:
                    self._consecutive_errors += 1
                    self._last_error_time = time.time()
                    if self._consecutive_errors >= 5:
                        self.circuit_breaker = True
                        self._circuit_cooldown_ms = min(
                            self._circuit_cooldown_ms * 2,
                            self._max_circuit_cooldown_ms
                        )
                        logger.error(f"🚨 连续 5 次错误，触发熔断！冷却 {self._circuit_cooldown_ms/1000:.0f}s")
                    body = resp.text
                    logger.error(f"API 错误 [{resp.status_code}]: {body}")
                    if attempt < retries - 1:
                        time.sleep(1)
                        continue
                    return {"error": body, "status": resp.status_code}

                self._consecutive_errors = 0
                return resp.json()

            except requests.exceptions.Timeout:
                logger.warning(f"⏳ 请求超时 (第 {attempt + 1} 次重试)")
                if attempt < retries - 1:
                    time.sleep(1)
                    continue
                return {"error": "timeout"}
            except Exception as e:
                logger.error(f"请求异常: {e}")
                if attempt < retries - 1:
                    time.sleep(1)
                    continue
                return {"error": str(e)}

        return {"error": "max retries exceeded"}

    # ── 公开接口 ──
    def klines(self, symbol: str, interval: str = "2m", limit: int = 100) -> List:
        return self._request("GET", "/fapi/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": limit
        })

    def ticker_price(self, symbol: str = None) -> dict:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/ticker/price", params)

    def mark_price(self, symbol: str = None) -> dict:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/premiumIndex", params)

    def funding_rate(self, symbol: str, limit: int = 10) -> List:
        return self._request("GET", "/fapi/v1/fundingRate", {
            "symbol": symbol, "limit": limit
        })

    def open_interest(self, symbol: str) -> dict:
        return self._request("GET", "/fapi/v1/openInterest", {"symbol": symbol})

    def depth(self, symbol: str, limit: int = 20) -> dict:
        return self._request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    def exchange_info(self) -> dict:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    # ── 签名接口 ──
    def account_balance(self) -> List:
        return self._request("GET", "/fapi/v2/balance", {}, signed=True)

    def account_info(self) -> dict:
        return self._request("GET", "/fapi/v2/account", {}, signed=True)

    def positions(self, symbol: str = None) -> List:
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._request("GET", "/fapi/v2/positionRisk", params, signed=True)
        if isinstance(data, list):
            return [p for p in data if float(p.get("positionAmt", 0)) != 0]
        return []

    def open_orders(self, symbol: str = None) -> List:
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/fapi/v1/openOrders", params, signed=True)

    def new_order(self, symbol: str, side: str, order_type: str,
                  quantity: float = None, price: float = None,
                  stop_price: float = None, reduce_only: bool = False,
                  close_position: bool = False, time_in_force: str = "GTC",
                  callback_rate: float = None) -> dict:
        params = {
            "symbol": symbol, "side": side, "type": order_type,
        }
        if quantity: params["quantity"] = quantity
        if price: params["price"] = price
        if stop_price: params["stopPrice"] = stop_price
        if reduce_only: params["reduceOnly"] = "true"
        if close_position: params["closePosition"] = "true"
        if time_in_force and order_type in ("LIMIT", "STOP", "TAKE_PROFIT"):
            params["timeInForce"] = time_in_force
        if callback_rate: params["callbackRate"] = callback_rate

        logger.info(f"📤 下单: {params}")
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def cancel_order(self, symbol: str, order_id: int = None, orig_client_order_id: str = None) -> dict:
        params = {"symbol": symbol}
        if order_id: params["orderId"] = order_id
        if orig_client_order_id: params["origClientOrderId"] = orig_client_order_id
        return self._request("DELETE", "/fapi/v1/order", params, signed=True)

    def cancel_all_orders(self, symbol: str) -> dict:
        return self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    def change_leverage(self, symbol: str, leverage: int) -> dict:
        return self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    def change_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        return self._request("POST", "/fapi/v1/marginType",
                             {"symbol": symbol, "marginType": margin_type}, signed=True)

    def modify_isolated_margin(self, symbol: str, amount: float, type: int = 1) -> dict:
        return self._request("POST", "/fapi/v1/positionMargin",
                             {"symbol": symbol, "amount": amount, "type": type}, signed=True)

    def income_history(self, symbol: str = None, income_type: str = None,
                       limit: int = 50, start_time: int = None) -> List:
        params: dict = {"limit": limit}
        if symbol: params["symbol"] = symbol
        if income_type: params["incomeType"] = income_type
        if start_time: params["startTime"] = start_time
        return self._request("GET", "/fapi/v1/income", params, signed=True)

    def get_order(self, symbol: str, order_id: int = None, orig_client_order_id: str = None) -> dict:
        params = {"symbol": symbol}
        if order_id: params["orderId"] = order_id
        if orig_client_order_id: params["origClientOrderId"] = orig_client_order_id
        return self._request("GET", "/fapi/v1/order", params, signed=True)

    def my_trades(self, symbol: str, limit: int = 20) -> List:
        return self._request("GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": limit}, signed=True)

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        info = self.exchange_info()
        if isinstance(info, dict) and ("error" in info or "symbols" not in info):
            return None
        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                return s
        return None

    def adjust_quantity(self, symbol: str, quantity: float) -> float:
        info = self.get_symbol_info(symbol)
        if not info:
            return quantity
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step = float(f["stepSize"])
                min_qty = float(f["minQty"])
                quantity = max(min_qty, round(quantity - (quantity % step), 10))
                break
        return quantity

    def adjust_price(self, symbol: str, price: float) -> float:
        info = self.get_symbol_info(symbol)
        if not info:
            return price
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick = float(f["tickSize"])
                return round(price - (price % tick), 10)
        return price

    @property
    def is_circuit_broken(self) -> bool:
        return self.circuit_breaker
