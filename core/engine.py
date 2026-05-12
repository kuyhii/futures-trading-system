#!/usr/bin/env python3
"""
core/engine.py — 量化交易主引擎（模块化编排器）

职责: 模块初始化 + 交易循环编排 + 状态管理
所有功能已拆分为独立模块：
  - binance_client.py  → API 客户端
  - models.py          → 数据模型
  - indicators.py      → 技术指标
  - strategy_engine.py → 策略引擎（趋势跟踪 + 均值回归 + 突破）
  - risk_engine.py     → 风控引擎
  - order_manager.py   → 订单管理
  - notify.py          → 通知模块
  - kline_manager.py   → K线管理器

使用方式:
  python3 -m core.engine              # 启动自动交易
  python3 -m core.engine --env prod   # 临时切换实盘模式
  python3 -m core.engine --status     # 查看状态
"""

import json
import os
import sys
import time
import threading
import logging
import signal as sig
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any, Tuple

# ── 路径 ──
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")
LOGS_DIR = os.path.join(ROOT, "logs")
STATE_DIR = os.path.join(ROOT, "state")

for d in [DATA_DIR, LOGS_DIR, STATE_DIR, os.path.join(DATA_DIR, "klines"),
           os.path.join(DATA_DIR, "signals"), os.path.join(DATA_DIR, "live")]:
    os.makedirs(d, exist_ok=True)

# ── 加载 .env ──
def load_env():
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

load_env()

# ── 导入模块 ──
from core.notify import notifier, _side_cn, _strategy_cn
from core.kline_manager import KlineManager
from core.binance_client import BinanceClient
from core.models import SignalAction, PositionSide, TradeSignal, Position, AccountState
from core.strategy_engine import StrategyEngine
from core.risk_engine import RiskEngine
from core.order_manager import OrderManager

# ── 环境配置 ──
BINANCE_API_ENV = os.environ.get("BINANCE_API_ENV", "testnet")

if BINANCE_API_ENV == "prod":
    BINANCE_API_KEY = os.environ.get("BINANCE_PROD_API_KEY", "")
    BINANCE_SECRET_KEY = os.environ.get("BINANCE_PROD_SECRET_KEY", "")
else:
    BINANCE_API_KEY = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    BINANCE_SECRET_KEY = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "")

BASE_URLS = {
    "prod":    "https://fapi.binance.com",
    "testnet": "https://testnet.binancefuture.com",
}
BASE_URL = BASE_URLS.get(BINANCE_API_ENV, BASE_URLS["testnet"])
STREAM_URL = "fstream.binance.com" if BINANCE_API_ENV == "prod" else "stream.binancefuture.com"

MODE_LABELS = {
    "prod": "实盘模式（真金白银）",
    "testnet": "测试网模式（模拟实盘）",
}
MODE_LABEL = MODE_LABELS.get(BINANCE_API_ENV, MODE_LABELS["testnet"])
IS_AUTHENTICATED = bool(BINANCE_API_KEY and BINANCE_SECRET_KEY)

# ── 日志 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, "engine.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("engine")


class TradingEngine:
    """主交易引擎 — 模块化编排器"""

    def __init__(self):
        self.client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY, BASE_URL)
        self.config = self._load_config()
        self.risk_config = self._load_risk_config()
        self.symbols_config = self._load_symbols()

        self.strategy = StrategyEngine(self.config)
        self.risk = RiskEngine(self.risk_config)
        self.orders = OrderManager(self.client, self.risk)

        self.account = AccountState()
        self.running = False
        self.cycle_count = 0
        self.last_cycle_time = 0
        self.cycle_interval = 60
        self._account_stale = True  # 标记账户数据是否新鲜

        self._last_symbols_update = 0.0
        self._symbols_update_interval = 86400  # 24h
        # 品种池更新前30分钟停止开仓 + 后5分钟恢复（秒）
        self._symbols_update_cooldown_pre = 1800   # 前30分钟
        self._symbols_update_cooldown_post = 300   # 后5分钟
        # 定时任务时间戳
        self._next_biancsw_analysis = 0.0   # biancsw 分析触发时间
        self._next_param_tuning = 0.0      # 策略调参触发时间

        self.risk_interval = 15
        self._risk_thread: Optional[threading.Thread] = None
        self._risk_stop_event = threading.Event()

        self.min_confidence = 0.7

        self._kline_cache: Dict[str, List[dict]] = {}
        self._last_signal_time: Dict[str, float] = {}
        self.signal_cooldown = 300

        self._account_lock = threading.Lock()
        self._kline_cache_lock = threading.Lock()
        self._signal_time_lock = threading.Lock()

        self.symbols_list = [s["symbol"] for s in self.symbols_config]
        self.kline_manager = KlineManager(
            symbols=self.symbols_list,
            api_key=BINANCE_API_KEY,
            api_secret=BINANCE_SECRET_KEY,
            base_url=BASE_URL,
        )
        logger.info("📊 K线数据管理器已初始化")

        self._strategy_status: Dict[str, dict] = {}

        logger.info(f"🚀 交易引擎初始化 [{MODE_LABEL}]")
        logger.info(f"  环境: {BINANCE_API_ENV} | API: {BASE_URL} | 认证: {'✅' if IS_AUTHENTICATED else '⚠️'}")
        logger.info(f"  监控币种: {[s['symbol'] for s in self.symbols_config]}")
        logger.info(f"  活跃策略: {self.config.get('active', [])}")
        logger.info(f"  风控巡检: {self.risk_interval}s | 置信度阈值: {self.min_confidence}")

    def _load_config(self) -> dict:
        with open(os.path.join(CONFIG_DIR, "strategies.json")) as f:
            return json.load(f)

    def _load_risk_config(self) -> dict:
        with open(os.path.join(CONFIG_DIR, "risk.json")) as f:
            return json.load(f)

    def _load_symbols(self) -> List[dict]:
        with open(os.path.join(CONFIG_DIR, "symbols.json")) as f:
            data = json.load(f)
        return [s for s in data["watchlist"] if s.get("enabled", True)]

    def reload_symbols(self) -> int:
        old_symbols = [s["symbol"] for s in self.symbols_config]
        self.symbols_config = self._load_symbols()
        new_symbols = [s["symbol"] for s in self.symbols_config]
        added = [s for s in new_symbols if s not in old_symbols]
        removed = [s for s in old_symbols if s not in new_symbols]
        if added: logger.info(f"✅ 品种池新增: {added}")
        if removed: logger.info(f"🗑️ 品种池移除: {removed}")
        if not added and not removed: logger.info("🔄 品种池无变化")
        self.symbols_list = new_symbols
        self.kline_manager.update_symbols(new_symbols)

        # ── 清理被移除品种的历史数据 ──
        self._cleanup_removed_symbols(removed)

        # ── 记录更新时间，触发开仓冷却 ──
        self._last_symbols_update = time.time()
        logger.info(f"🚫 品种池更新冷却已启动：前后 {self._symbols_update_cooldown}s 内禁止开新仓")

        logger.info(f"  当前监控币种: {new_symbols} ({len(new_symbols)} 个)")
        return len(new_symbols)

    def _update_symbols_pool(self):
        import subprocess
        script_path = os.path.join(ROOT, "scripts", "update_symbols_pool.py")
        cmd_args = [sys.executable, script_path]
        logger.info("🔄 开始每日品种池自动更新...")
        result = subprocess.run(cmd_args, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            logger.info("✅ 品种池脚本执行成功")
            self.reload_symbols()
        else:
            logger.error(f"❌ 品种池脚本执行失败 (exit={result.returncode})")
            logger.error(f"stderr: {result.stderr[:500]}")

    def _cleanup_removed_symbols(self, removed: list):
        """清理被移除品种的1m和2m历史数据文件"""
        kline_1m_dir = os.path.join(ROOT, "data", "klines", "1m")
        kline_2m_dir = os.path.join(ROOT, "data", "klines", "2m")
        cleaned = 0
        for symbol in removed:
            for tf_dir in [kline_1m_dir, kline_2m_dir]:
                filepath = os.path.join(tf_dir, f"{symbol}.jsonl")
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                        cleaned += 1
                        logger.info(f"🗑️ 已清理历史数据: {filepath}")
                    except Exception as e:
                        logger.error(f"清理文件失败 {filepath}: {e}")
        if cleaned:
            logger.info(f"📊 历史数据清理完成: 共删除 {cleaned} 个文件")


    def _check_pre_update_tasks(self):
        """品种池更新前定时任务调度（每日 UTC 00:00 更新）"""
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()

        # 下一个 UTC 00:00
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_ts = next_midnight.timestamp()

        # T-25min = 触发 biancsw 数据分析
        t25 = midnight_ts - 25 * 60
        if now_ts >= t25 and self._next_biancsw_analysis == 0:
            self._run_biancsw_analysis()
            self._next_biancsw_analysis = now_ts

        # T-10min = 触发策略参数调整
        t10 = midnight_ts - 10 * 60
        if now_ts >= t10 and self._next_param_tuning == 0:
            self._tune_strategy_params()
            self._next_param_tuning = now_ts

        # 过了00:05，重置下次任务标记
        post5 = midnight_ts + 5 * 60
        if now_ts >= post5:
            self._next_biancsw_analysis = 0.0
            self._next_param_tuning = 0.0
            logger.info('🔄 品种池更新定时任务标记已重置')

    def _run_biancsw_analysis(self):
        """T-25min: 调用 biancsw 分析交易数据，生成报告"""
        logger.info('📊 开始 biancsw 交易数据分析（T-25min）...')
        try:
            history_file = os.path.join(STATE_DIR, 'trade_history.json')
            report_dir = os.path.join(ROOT, 'reports')
            os.makedirs(report_dir, exist_ok=True)
            now_str = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
            report_file = os.path.join(report_dir, f'biancsw_analysis_{now_str}.md')

            if os.path.exists(history_file):
                with open(history_file, 'r') as f:
                    data = json.load(f)
                trades = data.get('trades', [])
                live_trades = [t for t in trades if 'DRY-RUN' not in str(t.get('result', ''))]

                opens = [t for t in live_trades if t['action'] == 'open']
                closes = [t for t in live_trades if t['action'] == 'close']

                pnls = []
                for t in closes:
                    r = str(t.get('result', ''))
                    if 'pnl=' in r:
                        try:
                            pnls.append(float(r.split('pnl=')[1].strip()))
                        except:
                            pass

                total_pnl = sum(pnls)
                wins = [p for p in pnls if p > 0]
                losses = [p for p in pnls if p < 0]
                win_rate = len(wins) / len(pnls) * 100 if pnls else 0

                from collections import Counter
                strat_counts = Counter()
                for t in opens:
                    for s in t.get('strategy', '').split(','):
                        strat_counts[s.strip()] += 1

                lines = []
                lines.append('# 📊 biancsw 交易数据分析报告')
                lines.append(f'> 生成时间: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}')
                lines.append('')
                lines.append('## 总览')
                lines.append(f'- 总交易: {len(live_trades)} 条（开仓 {len(opens)} / 平仓 {len(closes)}）')
                lines.append(f'- 已实现盈亏: {total_pnl:+.2f} USDT')
                lines.append(f'- 胜率: {win_rate:.1f}% ({len(wins)}胜 {len(losses)}负)')
                lines.append('')
                lines.append('## 策略分布')
                for s, c in strat_counts.most_common():
                    lines.append(f'- {s}: {c} 次')
                lines.append('')
                lines.append('## 盈亏明细')
                for t in closes:
                    r = str(t.get('result', ''))
                    if 'pnl=' in r:
                        try:
                            pnl = float(r.split('pnl=')[1].strip())
                            lines.append(f'- {t["time"][:19]} | {t["symbol"]} | {t["side"]} | {pnl:+.2f} USDT')
                        except:
                            pass

                with open(report_file, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(lines))
                logger.info(f'✅ biancsw 分析报告已保存: {report_file}')
            else:
                logger.warning('⚠️ 无交易历史数据，跳过分析')
        except Exception as e:
            logger.error(f'❌ biancsw 分析失败: {e}')

    def _tune_strategy_params(self):
        """T-10min: 根据分析报告调整策略参数"""
        logger.info('⚙️ 开始策略参数调整（T-10min）...')
        try:
            report_dir = os.path.join(ROOT, 'reports')
            config_dir = os.path.join(ROOT, 'config')
            strategies_file = os.path.join(config_dir, 'strategies.json')

            reports = sorted([f for f in os.listdir(report_dir) if f.startswith('biancsw_analysis_')])
            if not reports:
                logger.warning('⚠️ 无分析报告，跳过调参')
                return

            latest_report = os.path.join(report_dir, reports[-1])

            with open(strategies_file, 'r') as f:
                strat_cfg = json.load(f)

            import shutil
            backup_file = strategies_file + f'.bak.{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")}'
            shutil.copy2(strategies_file, backup_file)

            tuned_changes = []
            for name, cfg in strat_cfg.get('strategies', {}).items():
                old_min_conf = cfg.get('min_confidence', 0.6)
                tuned_changes.append(f'{name}: min_confidence={old_min_conf} (保持)')

            now_str = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
            tune_report_file = os.path.join(report_dir, f'param_tuning_{now_str}.md')

            lines = []
            lines.append('# ⚙️ 策略参数调整报告')
            lines.append(f'> 生成时间: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}')
            lines.append(f'> 参考分析: {reports[-1]}')
            lines.append('')
            lines.append('## 当前策略参数')
            for name, cfg in strat_cfg.get('strategies', {}).items():
                lines.append(f'### {name}')
                lines.append(f'- enabled: {cfg.get("enabled", False)}')
                lines.append(f'- weight: {cfg.get("weight", 1.0)}')
                lines.append(f'- min_confidence: {cfg.get("min_confidence", 0.5)}')
                lines.append(f'- leverage: {cfg.get("leverage", 20)}x')
                lines.append('')
            lines.append('## 调整建议')
            for c in tuned_changes:
                lines.append(f'- {c}')
            lines.append('')
            lines.append('*参数未自动修改，请人工审核后手动调整*')

            with open(tune_report_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
            logger.info(f'✅ 策略调参报告已保存: {tune_report_file}')
        except Exception as e:
            logger.error(f'❌ 策略调参失败: {e}')

    def self_check_strategies(self) -> dict:
        logger.info("🔍 开始策略自检...")
        active = self.config.get("active", [])
        for name in active:
            cfg = self.config.get("strategies", {}).get(name, {})
            enabled = cfg.get("enabled", False)
            available = False
            status = "未启用"
            if enabled:
                if name in ("trend_follow", "mean_reversion", "breakout"):
                    available = True
                    status = "✅ 就绪"
            self._strategy_status[name] = {
                "enabled": enabled, "available": available,
                "status": status, "errors": 0,
            }
            logger.info(f"  {name}: {status}")
        summary = {
            "total": len(active),
            "enabled": sum(1 for s in self._strategy_status.values() if s["enabled"]),
            "available": sum(1 for s in self._strategy_status.values() if s["available"]),
        }
        logger.info(f"📋 自检完成: {summary['enabled']}/{summary['total']} 启用")
        return summary

    def refresh_account(self):
        if not IS_AUTHENTICATED:
            self.account.total_equity = 10000
            self.account.available_balance = 10000
            self.account.positions = []
            return
        try:
            acc_info = self.client.account_info()
            if isinstance(acc_info, dict) and "error" in acc_info:
                logger.warning(f"⚠️ 账户信息查询失败: {acc_info['error']}")
                self._account_stale = True
                return
            self.account.unrealized_pnl = float(acc_info.get("totalUnrealizedProfit", 0))
            self.account.margin_balance = float(acc_info.get("totalMarginBalance", 0))
            self.account.total_equity = self.account.margin_balance
            balances = self.client.account_balance()
            if isinstance(balances, dict) and "error" in balances:
                logger.warning(f"⚠️ 余额查询失败: {balances['error']}")
            elif isinstance(balances, list):
                for b in balances:
                    if isinstance(b, dict) and b.get("asset") == "USDT":
                        self.account.available_balance = float(b.get("availableBalance", 0))
            raw_positions = self.client.positions()
            if isinstance(raw_positions, dict) and "error" in raw_positions:
                logger.warning(f"⚠️ 持仓查询失败: {raw_positions['error']}")
                raw_positions = []
            with self._account_lock:
                self.account.positions = []
                for p in raw_positions:
                    if not isinstance(p, dict): continue
                    amt = float(p["positionAmt"])
                    if amt == 0: continue
                    pos = Position(
                        symbol=p["symbol"], side=PositionSide.LONG if amt > 0 else PositionSide.SHORT,
                        quantity=abs(amt), entry_price=float(p["entryPrice"]),
                        leverage=int(p["leverage"]), unrealized_pnl=float(p["unRealizedProfit"]),
                        mark_price=float(p["markPrice"]),
                        liquidation_price=float(p["liquidationPrice"]),
                        entry_time=datetime.now(timezone.utc).isoformat(),
                    )
                    self.account.positions.append(pos)
            if self.risk._daily_start_equity == 0:
                self.risk.set_daily_start(self.account.total_equity)
            self.risk._peak_equity = max(self.risk._peak_equity, self.account.total_equity)
            self._account_stale = False  # 数据已更新
            logger.info(f"💰 账户: 权益={self.account.total_equity:.2f} | 可用={self.account.available_balance:.2f} | 未实现盈亏={self.account.unrealized_pnl:.2f} | 持仓={len(self.account.positions)}")
        except Exception as e:
            logger.error(f"刷新账户状态失败: {e}")
            self._account_stale = True  # 数据已过期

    def _calc_dry_run_pnl(self, pos: Position) -> float:
        if pos.entry_price == 0 or pos.mark_price == 0:
            return 0
        direction = 1 if pos.side == PositionSide.LONG else -1
        pnl_pct = (pos.mark_price - pos.entry_price) / pos.entry_price * 100 * direction
        nominal = pos.entry_price * pos.quantity
        return round(nominal * pnl_pct / 100, 4)

    def fetch_klines(self, symbol: str, timeframe: str = None) -> List[dict]:
        tf = timeframe or self.config["timeframe"]
        cache_key = f"{symbol}_{tf}"
        if tf in ("1m", "2m"):
            try:
                if tf == "1m":
                    raw_klines = self.kline_manager.get_klines_1m(symbol, limit=200)
                else:
                    raw_klines = self.kline_manager.get_klines_2m(symbol, limit=200)
                if raw_klines:
                    with self._kline_cache_lock:
                        self._kline_cache[cache_key] = raw_klines
                    return raw_klines
                else:
                    logger.warning(f"本地 {tf} K 线为空 {symbol}，API回退")
            except Exception as e:
                logger.warning(f"本地 {tf} K 线读取失败 {symbol}: {e}，API回退")
        try:
            raw = self.client.klines(symbol, tf, 200)
            if isinstance(raw, dict) and "error" in raw:
                logger.warning(f"K线获取失败 {symbol}: {raw['error']}")
                with self._kline_cache_lock:
                    return self._kline_cache.get(cache_key, [])
            candles = self.strategy.parse_klines(raw)
            if candles:
                with self._kline_cache_lock:
                    self._kline_cache[cache_key] = candles
            return candles
        except Exception as e:
            logger.error(f"K线获取异常 {symbol}: {e}")
            with self._kline_cache_lock:
                return self._kline_cache.get(cache_key, [])

    def get_current_price(self, symbol: str) -> float:
        try:
            data = self.client.ticker_price(symbol)
            return float(data.get("price", 0))
        except Exception:
            with self._kline_cache_lock:
                candles = self._kline_cache.get(f"{symbol}_{self.config['timeframe']}", [])
            if candles:
                return candles[-1]["close"]
            return 0

    def process_signals(self) -> List[TradeSignal]:
        final_signals = []
        for sym_cfg in self.symbols_config:
            symbol = sym_cfg["symbol"]
            logger.info(f"📡 分析 {symbol}...")
            candles_2m = self.fetch_klines(symbol, "2m")
            if not candles_2m:
                logger.warning(f"  {symbol}: 2m K 线数据为空，跳过")
                continue
            signals = self.strategy.analyze(candles_2m, symbol=symbol)
            if signals:
                logger.info(f"  {symbol}: {len(signals)} 个策略发出信号 → {[s.strategy for s in signals]}")
            if signals:
                aggregated = self.strategy.aggregate_signals(signals, symbol)
                if aggregated:
                    logger.info(f"  {symbol} {_side_cn(aggregated.action.value)} "
                               f"(置信度={aggregated.confidence:.0%}, 策略={_strategy_cn(aggregated.strategy)})")
                    final_signals.append(aggregated)
        filtered = [s for s in final_signals if s.confidence >= self.min_confidence]
        low_conf = [s for s in final_signals if s.confidence < self.min_confidence]
        logger.info(f"📊 过滤完成: 通过 {len(filtered)} 个, 过滤 {len(low_conf)} 个")
        if low_conf:
            for s in low_conf:
                logger.info(f"🔽 {s.symbol} {_side_cn(s.action.value)} 置信度 {s.confidence:.0%} 低于阈值 {self.min_confidence:.0%}，已过滤")
        return filtered

    def execute_signal(self, signal: TradeSignal):
        symbol = signal.symbol
        action = signal.action
        now = time.time()

        # ── 品种池更新冷却：前30分钟停止开仓，后5分钟恢复 ──
        now_utc = datetime.now(timezone.utc)
        next_midnight = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_ts = next_midnight.timestamp()
        pre_cutoff = midnight_ts - self._symbols_update_cooldown_pre  # T-30min

        # 检查是否在 T-30min 到 T+5min 之间
        if now.timestamp() >= pre_cutoff:
            # T-30min 到 T：品种池更新前冷却
            remaining = pre_cutoff - now.timestamp() + self._symbols_update_cooldown_pre
            if now.timestamp() < midnight_ts:
                logger.info(f"⏳ {symbol} 品种池更新前冷却中（T-{remaining/60:.0f}min），停止开仓")
                return
            # T 到 T+5min：品种池更新后冷却
            post_elapsed = now.timestamp() - self._last_symbols_update
            if 0 < post_elapsed < self._symbols_update_cooldown_post:
                remaining = self._symbols_update_cooldown_post - post_elapsed
                logger.info(f"⏳ {symbol} 品种池更新后冷却中（剩余{remaining:.0f}s），停止开仓")
                return

        with self._signal_time_lock:
            last = self._last_signal_time.get(symbol, 0)
        if now - last < self.signal_cooldown:
            logger.debug(f"⏳ {symbol} 信号冷却中，跳过")
            return
        logger.info(f"▶️ 执行信号: {symbol} {_side_cn(action.value)} ({_strategy_cn(signal.strategy)}, {signal.confidence:.0%})")
        if action == SignalAction.BUY:
            self._handle_buy_signal(signal)
        elif action == SignalAction.SELL:
            self._handle_sell_signal(signal)
        with self._signal_time_lock:
            self._last_signal_time[symbol] = now

    def _handle_buy_signal(self, signal: TradeSignal):
        symbol = signal.symbol
        # 获取最新价格，防止信号价格过时
        price = self.get_current_price(symbol)
        if price <= 0:
            logger.warning(f"⚠️ {symbol} 获取价格失败，跳过信号")
            return
        leverages = [self.config.get("strategies", {}).get(s, {}).get("leverage", self.risk_config["max_leverage"])
                     for s in signal.strategy.split(",")]
        leverage = max(leverages)
        for pos in self.account.positions:
            if pos.symbol == symbol and pos.side == PositionSide.SHORT:
                logger.info(f"🔄 {symbol} 有空仓，先平仓再做多")
                self.orders.close_position(symbol, reason=f"反转做多 [{signal.strategy}]",
                                           account=self.account, client=self.client)
                break
        qty = self.risk.calc_position_size(self.account, price, leverage)
        qty = self.client.adjust_quantity(symbol, qty)
        ok, reason = self.risk.can_open_position(self.account, symbol, PositionSide.LONG, qty, leverage, price)
        if not ok:
            logger.warning(f"🚫 {symbol} 做多被风控拒绝: {reason}")
            try: notifier.risk_warning(symbol, f"做多被拒绝: {reason}", level="warning")
            except Exception: pass
            return
        result = self.orders.open_position(symbol, PositionSide.LONG, qty, leverage, price,
                                           strategy=signal.strategy, account=self.account)
        if result["status"] == "opened":
            logger.info(f"📈 {symbol} 做多已执行: qty={qty}, leverage={leverage}x")

    def _handle_sell_signal(self, signal: TradeSignal):
        symbol = signal.symbol
        # 获取最新价格
        price = self.get_current_price(symbol)
        if price <= 0:
            logger.warning(f"⚠️ {symbol} 获取价格失败，跳过信号")
            return
        leverages = [self.config.get("strategies", {}).get(s, {}).get("leverage", self.risk_config["max_leverage"])
                     for s in signal.strategy.split(",")]
        leverage = max(leverages)
        for pos in self.account.positions:
            if pos.symbol == symbol and pos.side == PositionSide.LONG:
                logger.info(f"🔄 {symbol} 有多仓，先平仓再做空")
                self.orders.close_position(symbol, reason=f"反转做空 [{signal.strategy}]",
                                           account=self.account, client=self.client)
                break
        qty = self.risk.calc_position_size(self.account, price, leverage)
        qty = self.client.adjust_quantity(symbol, qty)
        ok, reason = self.risk.can_open_position(self.account, symbol, PositionSide.SHORT, qty, leverage, price)
        if not ok:
            logger.warning(f"🚫 {symbol} 做空被风控拒绝: {reason}")
            try: notifier.risk_warning(symbol, f"做空被拒绝: {reason}", level="warning")
            except Exception: pass
            return
        result = self.orders.open_position(symbol, PositionSide.SHORT, qty, leverage, price,
                                           strategy=signal.strategy, account=self.account)
        if result["status"] == "opened":
            logger.info(f"📉 {symbol} 做空已执行: qty={qty}, leverage={leverage}x")

    def monitor_positions(self):
        with self._account_lock:
            positions = list(self.account.positions)
        if not positions:
            return
        for pos in positions:
            try:
                price_data = self.client.mark_price(pos.symbol)
                pos.mark_price = float(price_data.get("markPrice", pos.mark_price))
            except Exception:
                continue
            action = self.risk.check_position_risk(pos)
            if action:
                logger.warning(f"⚠️ {pos.symbol} {action}")
                risk_level = "critical" if "STOP_LOSS" in action else "warning"
                try: notifier.risk_warning(pos.symbol, action, level=risk_level)
                except Exception: pass
                if any(kw in action for kw in ("STOP_LOSS", "TRAILING_STOP", "TAKE_PROFIT")):
                    self.orders.close_position(pos.symbol, reason=action,
                                               account=self.account, client=self.client)

    def run_cycle(self):
        self.cycle_count += 1
        self.last_cycle_time = time.time()
        logger.info(f"\n{'='*50}")
        logger.info(f"🔄 第 {self.cycle_count} 个周期")
        logger.info(f"{'='*50}")

        # ── 品种池更新前定时任务 ──
        self._check_pre_update_tasks()

        # 标记账户过期，等待 refresh_account 刷新
        self._account_stale = True
        self.refresh_account()
        if self.client.is_circuit_broken:
            logger.warning("🚨 API 熔断中，跳过本轮")
            return
        if self._account_stale:
            logger.warning("⚠️ 账户数据过期，跳过本轮交易")
            return
        signals = self.process_signals()
        logger.info(f"📊 process_signals 返回: {len(signals)} 个信号")
        if signals:
            logger.info(f"📝 发现 {len(signals)} 个聚合信号:")
            for sig in signals:
                logger.info(f"  {sig.symbol}: {_side_cn(sig.action.value)} ({_strategy_cn(sig.strategy)}, {sig.confidence:.0%})")
                self.execute_signal(sig)
        else:
            logger.info("ℹ️  无交易信号")
        self._save_state()

    def _save_state(self):
        state = {
            "cycle_count": self.cycle_count,
            "last_cycle": datetime.now(timezone.utc).isoformat(),
            "environment": BINANCE_API_ENV,
            "mode_label": MODE_LABEL,
            "account": {
                "total_equity": self.account.total_equity,
                "available_balance": self.account.available_balance,
                "positions_count": len(self.account.positions),
            },
            "circuit_breaker": self.client.circuit_breaker,
            "strategy_status": self._strategy_status,
        }
        path = os.path.join(STATE_DIR, "engine_state.json")
        try:
            with open(path, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"保存状态失败: {e}")

    def _risk_monitor_loop(self):
        logger.info(f"🛡 风控巡检线程启动（每 {self.risk_interval}s）")
        while not self._risk_stop_event.is_set():
            try:
                with self._account_lock:
                    has_positions = bool(self.account.positions)
                if has_positions:
                    self.refresh_account()
                    self.monitor_positions()
                else:
                    logger.debug("🛡 风控巡检：无持仓，跳过")
            except Exception as e:
                logger.error(f"风控巡检异常: {e}")
            self._risk_stop_event.wait(self.risk_interval)
        logger.info("🛡 风控巡检线程已停止")

    def startup_pipeline(self, full_init: bool = True):
        """
        V3.1 启动管道：品种池更新 → K线补齐 → 2m 合成
        
        full_init=True:  全量初始化（更新品种池+补齐K线+合成）
        full_init=False: 快速启动（用本地缓存）
        """
        logger.info(f"\n{'='*50}")
        logger.info(f"🔧 启动管道：{'全量初始化' if full_init else '快速启动'}")
        logger.info(f"{'='*50}")

        # Step 1: 品种池更新
        if full_init:
            logger.info("Step 1/4: 更新品种池...")
            try:
                self._update_symbols_pool()
            except Exception as e:
                logger.warning(f"品种池更新失败，回退本地缓存: {e}")
                logger.info("  使用本地 symbols.json")
        else:
            logger.info("Step 1/4: 跳过品种池更新（快速启动）")

        # 加载当前品种池
        self.symbols_config = self._load_symbols()
        self.symbols_list = [s["symbol"] for s in self.symbols_config]
        self.kline_manager.symbols = self.symbols_list
        logger.info(f"  品种池: {len(self.symbols_list)} 个 → {[s for s in self.symbols_list[:5]]}...")

        # Step 2: 补齐 1m K线（确保最近 60 分钟）
        logger.info("Step 2/4: 补齐 1m K线（最近 60 分钟）...")
        total_klines = 0
        for symbol in self.symbols_list:
            try:
                n = self.kline_manager.ensure_60min_filled(symbol)
                total_klines += n
            except Exception as e:
                logger.warning(f"  {symbol}: K线补齐失败: {e}")
        logger.info(f"  补齐完成: 共获取 {total_klines} 根 1m K线")

        # Step 3: 合成 2m K线
        logger.info("Step 3/4: 合成 2m K线...")
        synthesized = 0
        for symbol in self.symbols_list:
            try:
                self.kline_manager._synthesize_2m_for_symbol(symbol)
                # 验证合成结果
                k2m = self.kline_manager.get_klines_2m(symbol, limit=5)
                if k2m:
                    synthesized += 1
            except Exception as e:
                logger.warning(f"  {symbol}: 2m 合成失败: {e}")
        logger.info(f"  合成完成: {synthesized}/{len(self.symbols_list)} 个品种")

        # Step 4: 策略自检
        logger.info("Step 4/4: 策略自检...")
        self.self_check_strategies()
        self.refresh_account()
        logger.info(f"{'='*50}")
        logger.info(f"✅ 启动管道完成，准备开始交易")
        logger.info(f"{'='*50}\n")

    def start(self, full_init: bool = True):
        self.running = True
        logger.info(f"🚀 交易引擎启动 [{MODE_LABEL}]")
        if BINANCE_API_ENV == "prod":
            logger.warning("🚨 实盘模式！")
        else:
            logger.info("📌 测试网模式：虚拟资金模拟交易")

        def stop_handler(signum, frame):
            logger.info("收到停止信号，正在安全退出...")
            self.running = False
            self._risk_stop_event.set()
            self.kline_manager.stop()

        sig.signal(sig.SIGINT, stop_handler)
        sig.signal(sig.SIGTERM, stop_handler)

        # V3.1 启动管道
        self.startup_pipeline(full_init=full_init)

        # 启动后台线程
        self.kline_manager.start_background_update(interval=60)
        self.kline_manager.start_background_cleanup(interval=43200, max_age_hours=24.0)
        self._risk_thread = threading.Thread(target=self._risk_monitor_loop, daemon=True)
        self._risk_thread.start()

        while self.running:
            try:
                self.run_cycle()
            except Exception as e:
                logger.error(f"周期异常: {e}", exc_info=True)
            now = time.time()
            if now - self._last_symbols_update >= self._symbols_update_interval:
                try:
                    self._update_symbols_pool()
                    self._last_symbols_update = now
                except Exception as e:
                    logger.error(f"品种池更新失败: {e}", exc_info=True)
            wait_time = max(0, self.cycle_interval - (time.time() - self.last_cycle_time))
            if wait_time > 0:
                time.sleep(wait_time)

    def stop(self):
        self.running = False
        self._risk_stop_event.set()
        if self._risk_thread and self._risk_thread.is_alive():
            self._risk_thread.join(timeout=5)
        self.kline_manager.stop()
        logger.info("交易引擎已停止")

    def status(self):
        self.refresh_account()
        print(f"\n{'='*50}")
        print(f"📊 交易引擎状态")
        print(f"{'='*50}")
        print(f"模式: {MODE_LABEL}")
        print(f"环境: {BINANCE_API_ENV}")
        print(f"认证: {'✅' if IS_AUTHENTICATED else '⚠️'}")
        print(f"周期数: {self.cycle_count}")
        print(f"熔断: {'🚨' if self.client.circuit_breaker else '✅'}")
        print(f"总权益: {self.account.total_equity:.2f} USDT")
        print(f"可用余额: {self.account.available_balance:.2f} USDT")
        print(f"持仓数: {len(self.account.positions)}")
        for pos in self.account.positions:
            pnl = pos.pnl_pct()
            print(f"  {pos.symbol}: {pos.side.value.upper()} {pos.quantity} @ {pos.entry_price} | 现价={pos.mark_price} | 盈亏={pnl:.2f}% | 杠杆={pos.leverage}x")
        print(f"\n策略状态:")
        for name, info in self._strategy_status.items():
            print(f"  {name}: {info['status']} (启用={info['enabled']}, 错误={info['errors']})")
        print(f"{'='*50}\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="量化交易引擎 V3.1")
    parser.add_argument("--env", choices=["testnet", "prod"], help="临时切换环境")
    parser.add_argument("--full-init", action="store_true", default=True, help="全量初始化（更新品种池+补齐K线+合成）")
    parser.add_argument("--quick-start", action="store_true", help="快速启动（用本地缓存）")
    parser.add_argument("--status", action="store_true", help="查看状态后退出")
    parser.add_argument("--self-check", action="store_true", help="策略自检后退出")
    args = parser.parse_args()

    if args.env:
        os.environ["BINANCE_API_ENV"] = args.env

    full_init = not args.quick_start
    engine = TradingEngine()

    if args.status:
        engine.status()
        return
    if args.self_check:
        engine.self_check_strategies()
        return

    try:
        engine.start(full_init=full_init)
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
