#!/usr/bin/env python3
"""
core/kline_manager.py - K线数据管理器

职责:
  1. 持续采集1分钟K线（从 Binance API）
  2. 合成2分钟K线
  3. 数据清理（保留6小时）
  4. 提供本地数据读取接口

存储格式: JSONL（每行一根K线），按品种分文件
  data/klines/1m/{SYMBOL}.jsonl
  data/klines/2m/{SYMBOL}.jsonl
"""

import json
import os
import time
import threading
import logging
import requests
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any
from collections import OrderedDict

logger = logging.getLogger("kline_manager")

# ── 路径 ──
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data", "klines")
KLINE_1M_DIR = os.path.join(DATA_DIR, "1m")
KLINE_2M_DIR = os.path.join(DATA_DIR, "2m")

for d in [KLINE_1M_DIR, KLINE_2M_DIR]:
    os.makedirs(d, exist_ok=True)

# ── Binance API ──
BINANCE_API_ENV = os.environ.get("BINANCE_API_ENV", "testnet")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")

BASE_URLS = {
    "prod":    "https://fapi.binance.com",
    "testnet": "https://testnet.binancefuture.com",
    "demo":    "https://testnet.binancefuture.com",
}
BASE_URL = BASE_URLS.get(BINANCE_API_ENV, BASE_URLS["testnet"])

# ── K线字段常量 ──
KLINE_FIELDS = ["open_time", "open", "high", "low", "close", "volume", "close_time"]


def _load_env():
    """加载 .env 文件（如果尚未加载）"""
    env_file = os.path.join(ROOT, ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip()
                    if key and val and key not in os.environ:
                        os.environ[key] = val


_load_env()
# Re-read after loading .env
BINANCE_API_ENV = os.environ.get("BINANCE_API_ENV", "testnet")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BASE_URL = BASE_URLS.get(BINANCE_API_ENV, BASE_URLS["testnet"])


def _binance_request(path: str, params: dict = None, retries: int = 3) -> dict:
    """发送请求到 Binance API"""
    url = f"{BASE_URL}{path}"
    params = params or {}
    if BINANCE_API_KEY:
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    else:
        headers = {}

    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                wait = min(2 ** attempt, 10)
                logger.warning(f"429 频率限制，等待 {wait}s 后重试")
                time.sleep(wait)
                continue
            else:
                logger.warning(f"API 错误 [{resp.status_code}]: {resp.text[:200]}")
                if attempt < retries - 1:
                    time.sleep(1)
                    continue
                return {"error": resp.text, "status": resp.status_code}
        except requests.exceptions.Timeout:
            logger.warning(f"请求超时 (第 {attempt + 1} 次重试)")
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


def _parse_binance_kline(raw: list) -> Optional[dict]:
    """将 Binance API 返回的 K 线数组解析为 dict"""
    if not raw or len(raw) < 7:
        return None
    return {
        "open_time": int(raw[0]),
        "open": float(raw[1]),
        "high": float(raw[2]),
        "low": float(raw[3]),
        "close": float(raw[4]),
        "volume": float(raw[5]),
        "close_time": int(raw[6]),
    }


def _kline_to_jsonl_line(kline: dict) -> str:
    """将 K 线 dict 转为 JSONL 行"""
    return json.dumps(kline, separators=(",", ":")) + "\n"


# ═══════════════════════════════════════════
# 本地文件读写
# ═══════════════════════════════════════════

def _get_file_path(timeframe: str, symbol: str) -> str:
    """获取 K 线文件路径"""
    if timeframe == "1m":
        return os.path.join(KLINE_1M_DIR, f"{symbol}.jsonl")
    elif timeframe == "2m":
        return os.path.join(KLINE_2M_DIR, f"{symbol}.jsonl")
    else:
        raise ValueError(f"不支持的时间框架: {timeframe}")


def _read_klines_file(timeframe: str, symbol: str, limit: int = 500) -> List[dict]:
    """从 JSONL 文件读取最近 N 根 K 线"""
    filepath = _get_file_path(timeframe, symbol)
    if not os.path.exists(filepath):
        return []
    try:
        lines = []
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(line)
        # 取最后 limit 行
        tail = lines[-limit:] if len(lines) > limit else lines
        result = []
        for line in tail:
            try:
                kline = json.loads(line)
                result.append(kline)
            except json.JSONDecodeError:
                continue
        return result
    except Exception as e:
        logger.error(f"读取 {filepath} 失败: {e}")
        return []


def _append_kline(timeframe: str, symbol: str, kline: dict):
    """追加一根 K 线到 JSONL 文件（自动去重）"""
    filepath = _get_file_path(timeframe, symbol)
    try:
        # 检查最后一行是否已有相同 open_time 的 K 线
        last_open_time = None
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            with open(filepath, "rb") as f:
                # 找到最后一行
                try:
                    f.seek(-2, os.SEEK_END)
                    while f.read(1) != b"\n":
                        f.seek(-2, os.SEEK_CUR)
                    last_line = f.readline().decode().strip()
                    if last_line:
                        last_kline = json.loads(last_line)
                        last_open_time = last_kline.get("open_time")
                except (OSError, json.JSONDecodeError):
                    pass

        if last_open_time == kline["open_time"]:
            return  # 去重，不重复写入

        with open(filepath, "a") as f:
            f.write(_kline_to_jsonl_line(kline))
    except Exception as e:
        logger.error(f"写入 {filepath} 失败: {e}")


def _append_klines_batch(timeframe: str, symbol: str, klines: List[dict]):
    """批量追加 K 线到 JSONL 文件（自动去重）"""
    if not klines:
        return
    filepath = _get_file_path(timeframe, symbol)

    # 获取最后已记录的 open_time 用于去重
    last_open_time = None
    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        try:
            with open(filepath, "rb") as f:
                f.seek(-2, os.SEEK_END)
                while f.read(1) != b"\n":
                    f.seek(-2, os.SEEK_CUR)
                last_line = f.readline().decode().strip()
                if last_line:
                    last_kline = json.loads(last_line)
                    last_open_time = last_kline.get("open_time")
        except (OSError, json.JSONDecodeError):
            pass

    # 过滤掉已存在的 K 线
    new_klines = []
    for k in klines:
        if last_open_time is None or k["open_time"] > last_open_time:
            new_klines.append(k)

    if not new_klines:
        return

    with open(filepath, "a") as f:
        for k in new_klines:
            f.write(_kline_to_jsonl_line(k))


# ═══════════════════════════════════════════
# 2分钟K线合成
# ═══════════════════════════════════════════

def synthesize_2m_kline(kline_1m_a: dict, kline_1m_b: dict) -> dict:
    """
    将两根 1m K 线合并为一根 2m K 线

    规则:
      - open = 第一根1m的open
      - high = 两根1m的high最大值
      - low = 两根1m的low最小值
      - close = 第二根1m的close
      - volume = 两根1m的volume之和
      - open_time = 第一根的open_time
      - close_time = 第二根的close_time
    """
    return {
        "open_time": kline_1m_a["open_time"],
        "open": kline_1m_a["open"],
        "high": max(kline_1m_a["high"], kline_1m_b["high"]),
        "low": min(kline_1m_a["low"], kline_1m_b["low"]),
        "close": kline_1m_b["close"],
        "volume": kline_1m_a["volume"] + kline_1m_b["volume"],
        "close_time": kline_1m_b["close_time"],
    }


def synthesize_2m_from_1m(klines_1m: List[dict]) -> List[dict]:
    """从 1m K 线列表合成 2m K 线列表"""
    if len(klines_1m) < 2:
        return []

    result = []
    i = 0
    while i + 1 < len(klines_1m):
        try:
            k2m = synthesize_2m_kline(klines_1m[i], klines_1m[i + 1])
            result.append(k2m)
            i += 2
        except (KeyError, IndexError):
            i += 1
            continue

    return result


# ═══════════════════════════════════════════
# K线管理器类
# ═══════════════════════════════════════════

class KlineManager:
    """
    K线数据管理器

    功能:
      1. 从 Binance API 采集 1m K 线（每 60 秒）
      2. 合成 2m K 线
      3. 数据清理（每 12 小时，保留 6 小时）
      4. 提供 get_klines_1m / get_klines_2m 读取接口
    """

    def __init__(self, symbols: List[str], api_key: str = "", api_secret: str = "",
                 base_url: str = BASE_URL):
        self.symbols = symbols
        self.api_key = api_key or BINANCE_API_KEY
        self.api_secret = api_secret or BINANCE_SECRET_KEY
        self.base_url = base_url or BASE_URL
        self._running = False
        self._update_thread: Optional[threading.Thread] = None
        self._cleanup_thread: Optional[threading.Thread] = None
        self._update_stop_event = threading.Event()
        self._cleanup_stop_event = threading.Event()

        # 线程锁：保护共享数据
        self._symbols_lock = threading.Lock()        # 保护 self.symbols
        self._update_time_lock = threading.Lock()    # 保护 self._last_update_time

        # 上次采集时间戳（用于增量更新）
        self._last_update_time: Dict[str, int] = {}

        # 统计
        self.stats = {
            "total_fetches": 0,
            "total_errors": 0,
            "last_update": None,
        }

        logger.info(f"📊 K线管理器初始化: {len(symbols)} 个品种")
        for s in symbols[:5]:
            logger.info(f"  - {s}")
        if len(symbols) > 5:
            logger.info(f"  ... 共 {len(symbols)} 个")

    def update_symbols(self, new_symbols: List[str]):
        """
        动态更新交易品种列表（跟随品种池变化）
        - 新增品种：立即开始采集
        - 移除品种：停止采集，保留历史数据不删除
        """
        with self._symbols_lock:
            old_set = set(self.symbols)
            self.symbols = new_symbols
        new_set = set(new_symbols)
        added = new_set - old_set
        removed = old_set - new_set

        if added:
            logger.info(f"✅ K线采集新增品种: {sorted(added)}")
            # 新增品种立即加载历史数据
            for sym in added:
                try:
                    with self._update_time_lock:
                        self._last_update_time[sym] = 0  # 重置时间戳，全量加载
                    self.initial_fetch_1m_klines(sym, limit=360)
                    self._synthesize_2m_for_symbol(sym)
                    logger.info(f"  {sym}: 加载 1m+合成2m 完成")
                except Exception as e:
                    logger.warning(f"  {sym} 初始加载失败: {e}")
        if removed:
            logger.info(f"🗑️ K线采集移除品种: {sorted(removed)}")
            # 清除旧时间戳，防止品种重新加入时K线数据断层
            with self._update_time_lock:
                for sym in removed:
                    self._last_update_time.pop(sym, None)

    def _fetch_1m_klines_from_api(self, symbol: str, limit: int = 100) -> List[dict]:
        """
        从 Binance API 获取 1m K 线

        如果有上次采集时间戳，则从那个时间点开始增量获取
        """
        params = {"symbol": symbol, "interval": "1m", "limit": limit}

        # 增量获取：从上次采集时间之后开始
        with self._update_time_lock:
            if symbol in self._last_update_time:
                params["startTime"] = self._last_update_time[symbol]

        raw = _binance_request("/fapi/v1/klines", params)
        if isinstance(raw, dict) and "error" in raw:
            self.stats["total_errors"] += 1
            logger.warning(f"1m K 线获取失败 {symbol}: {raw['error']}")
            return []

        klines = []
        for item in raw:
            k = _parse_binance_kline(item)
            if k:
                klines.append(k)

        if klines:
            with self._update_time_lock:
                self._last_update_time[symbol] = klines[-1]["open_time"]
            self.stats["total_fetches"] += 1

        return klines

    def update_1m_klines(self, symbol: str) -> int:
        """
        更新单个品种的 1m K 线数据

        返回: 新增的 K 线数量
        """
        # 先尝试增量获取（从上次时间戳之后）
        klines = self._fetch_1m_klines_from_api(symbol, limit=5)

        if not klines:
            return 0

        # 追加到本地文件
        _append_klines_batch("1m", symbol, klines)

        # 读取最新数据合成 2m K 线
        self._synthesize_2m_for_symbol(symbol)

        return len(klines)

    def initial_fetch_1m_klines(self, symbol: str, limit: int = 360) -> int:
        """
        初始获取（首次启动时）：获取最近 N 根 1m K 线

        返回: 获取的 K 线数量
        """
        raw = _binance_request("/fapi/v1/klines", {"symbol": symbol, "interval": "1m", "limit": limit})
        if isinstance(raw, dict) and "error" in raw:
            self.stats["total_errors"] += 1
            logger.warning(f"初始 1m K 线获取失败 {symbol}: {raw['error']}")
            return 0

        klines = []
        for item in raw:
            k = _parse_binance_kline(item)
            if k:
                klines.append(k)

        if klines:
            # 全量写入（覆盖旧文件，确保无重复）
            filepath = _get_file_path("1m", symbol)
            with open(filepath, "w") as f:
                for kline in klines:
                    f.write(_kline_to_jsonl_line(kline))

            self._last_update_time[symbol] = klines[-1]["open_time"]
            self.stats["total_fetches"] += 1

            # 合成 2m K 线
            _write_2m_from_1m_file(symbol)

            logger.info(f"  {symbol}: 初始加载 {len(klines)} 根 1m K 线")

        return len(klines)

    def _synthesize_2m_for_symbol(self, symbol: str):
        """读取 1m 文件，合成 2m 并写入"""
        _write_2m_from_1m_file(symbol)

    def get_klines_1m(self, symbol: str, limit: int = 200) -> List[dict]:
        """读取最近 N 根 1m K 线（优先本地，不足时从 API 补充）"""
        klines = _read_klines_file("1m", symbol, limit=limit)

        # 如果本地数据不足，从 API 补充
        if len(klines) < limit:
            api_klines = self._fetch_1m_klines_from_api(symbol, limit=limit)
            if api_klines:
                # 合并去重
                existing_times = {k["open_time"] for k in klines}
                for k in api_klines:
                    if k["open_time"] not in existing_times:
                        klines.append(k)
                        existing_times.add(k["open_time"])
                # 按 open_time 排序
                klines.sort(key=lambda x: x["open_time"])
                # 重新写入
                _append_klines_batch("1m", symbol, api_klines)

        return klines[-limit:]

    def get_klines_2m(self, symbol: str, limit: int = 200) -> List[dict]:
        """读取最近 N 根 2m K 线（优先本地，不足时从 1m 合成）"""
        klines = _read_klines_file("2m", symbol, limit=limit)

        # 如果本地数据不足，重新合成
        if len(klines) < limit:
            klines_1m = _read_klines_file("1m", symbol, limit=limit * 2)
            if klines_1m:
                klines_2m = synthesize_2m_from_1m(klines_1m)
                if klines_2m:
                    # 写入 2m 文件
                    _write_2m_klines_file(symbol, klines_2m)
                    klines = klines_2m[-limit:]

        return klines[-limit:]

    def clean_old_data(self, max_age_hours: float = 6.0):
        """
        清理超过指定小时的旧数据

        参数:
            max_age_hours: 保留的最大时间（小时），默认 6 小时
        """
        cutoff_time = int((time.time() - max_age_hours * 3600) * 1000)
        total_cleaned = 0

        for tf_dir, timeframe in [(KLINE_1M_DIR, "1m"), (KLINE_2M_DIR, "2m")]:
            for filename in os.listdir(tf_dir):
                if not filename.endswith(".jsonl"):
                    continue
                filepath = os.path.join(tf_dir, filename)
                try:
                    cleaned = 0
                    kept_lines = []
                    with open(filepath, "r") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                kline = json.loads(line)
                                if kline.get("open_time", 0) >= cutoff_time:
                                    kept_lines.append(line)
                                else:
                                    cleaned += 1
                            except json.JSONDecodeError:
                                continue

                    if cleaned > 0:
                        with open(filepath, "w") as f:
                            for line in kept_lines:
                                f.write(line + "\n")
                        total_cleaned += cleaned
                        logger.info(f"🧹 清理 {filename}: 删除 {cleaned} 根，保留 {len(kept_lines)} 根")
                except Exception as e:
                    logger.error(f"清理 {filepath} 失败: {e}")

        if total_cleaned > 0:
            logger.info(f"🧹 数据清理完成: 共删除 {total_cleaned} 根过期 K 线")

    def initial_load(self):
        """
        系统启动时：初始加载所有品种的 1m K 线数据
        并合成 2m K 线
        """
        logger.info("📥 开始初始加载 K 线数据...")
        total = 0
        for symbol in self.symbols:
            try:
                count = self.initial_fetch_1m_klines(symbol, limit=360)
                total += count
            except Exception as e:
                logger.error(f"初始加载 {symbol} 失败: {e}")
                self.stats["total_errors"] += 1
        logger.info(f"✅ 初始加载完成: 共 {total} 根 K 线")
        return total

    def start_background_update(self, interval: int = 60):
        """
        启动后台更新线程（每 interval 秒更新所有品种的 1m K 线）
        """
        if self._running:
            logger.warning("后台更新已在运行中")
            return

        self._running = True
        self._update_stop_event.clear()

        self._update_thread = threading.Thread(
            target=self._update_loop,
            args=(interval,),
            daemon=True,
            name="kline_update"
        )
        self._update_thread.start()
        logger.info(f"⏱ K线后台更新线程启动（每 {interval}s）")

    def start_background_cleanup(self, interval: int = 43200, max_age_hours: float = 6.0):
        """
        启动后台清理线程（每 interval 秒清理一次）
        """
        self._cleanup_stop_event.clear()

        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            args=(interval, max_age_hours),
            daemon=True,
            name="kline_cleanup"
        )
        self._cleanup_thread.start()
        logger.info(f"🧹 K线后台清理线程启动（每 {interval}s，保留 {max_age_hours}h）")

    def _update_loop(self, interval: int):
        """后台更新循环"""
        with self._symbols_lock:
            sym_count = len(self.symbols)
        logger.info(f"🔄 K线更新循环启动（间隔 {interval}s，品种数 {sym_count}）")
        while not self._update_stop_event.is_set():
            try:
                updated_count = 0
                with self._symbols_lock:
                    symbols_snapshot = list(self.symbols)
                for symbol in symbols_snapshot:
                    try:
                        n = self.update_1m_klines(symbol)
                        if n > 0:
                            updated_count += n
                    except Exception as e:
                        logger.error(f"更新 {symbol} 失败: {e}")
                        self.stats["total_errors"] += 1

                if updated_count > 0:
                    self.stats["last_update"] = datetime.now(timezone.utc).isoformat()
                    logger.debug(f"📊 K线更新: 新增 {updated_count} 根")

            except Exception as e:
                logger.error(f"K线更新循环异常: {e}")

            self._update_stop_event.wait(interval)

        logger.info("🔄 K线更新循环已停止")

    def _cleanup_loop(self, interval: int, max_age_hours: float):
        """后台清理循环"""
        logger.info(f"🧹 K线清理循环启动（间隔 {interval}s，保留 {max_age_hours}h）")
        while not self._cleanup_stop_event.is_set():
            try:
                self.clean_old_data(max_age_hours=max_age_hours)
            except Exception as e:
                logger.error(f"K线清理异常: {e}")

            self._cleanup_stop_event.wait(interval)

        logger.info("🧹 K线清理循环已停止")

    def stop(self):
        """停止所有后台线程"""
        self._running = False
        self._update_stop_event.set()
        self._cleanup_stop_event.set()

        if self._update_thread and self._update_thread.is_alive():
            self._update_thread.join(timeout=5)
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5)

        logger.info("🛑 K线管理器已停止")

    def get_stats(self) -> dict:
        """获取统计信息"""
        stats = self.stats.copy()
        stats["symbols"] = len(self.symbols)
        stats["running"] = self._running

        # 统计各品种文件行数
        file_stats = {}
        for symbol in self.symbols:
            for tf, tf_dir in [("1m", KLINE_1M_DIR), ("2m", KLINE_2M_DIR)]:
                filepath = os.path.join(tf_dir, f"{symbol}.jsonl")
                if os.path.exists(filepath):
                    try:
                        with open(filepath, "r") as f:
                            line_count = sum(1 for _ in f)
                        file_stats[f"{symbol}_{tf}"] = line_count
                    except Exception:
                        file_stats[f"{symbol}_{tf}"] = 0
        stats["file_stats"] = file_stats
        return stats


# ═══════════════════════════════════════════
# 辅助函数：从1m文件合成并写入2m文件
# ═══════════════════════════════════════════

def _write_2m_from_1m_file(symbol: str):
    """读取1m文件，合成2m，写入2m文件"""
    filepath_1m = _get_file_path("1m", symbol)
    if not os.path.exists(filepath_1m):
        return

    klines_1m = _read_klines_file("1m", symbol, limit=1000)
    if len(klines_1m) < 2:
        return

    klines_2m = synthesize_2m_from_1m(klines_1m)
    if klines_2m:
        _write_2m_klines_file(symbol, klines_2m)


def _write_2m_klines_file(symbol: str, klines_2m: List[dict]):
    """将 2m K 线写入文件（覆盖写入，确保去重）"""
    filepath = _get_file_path("2m", symbol)
    try:
        with open(filepath, "w") as f:
            for k in klines_2m:
                f.write(_kline_to_jsonl_line(k))
    except Exception as e:
        logger.error(f"写入 2m K 线文件失败 {symbol}: {e}")
