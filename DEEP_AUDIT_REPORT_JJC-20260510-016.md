# 🔍 自动量化合约交易系统 · 全面深度检查报告
**任务 ID:** JJC-20260510-016  
**检查时间:** 2026-05-10 20:16 UTC  
**系统路径:** `/root/projects/futures-trading-system/`

---

## 一、模块检查结果总览

| # | 模块 | 状态 | 问题数 | 备注 |
|---|------|------|--------|------|
| 1 | 架构完整性 | ⚠️ 风险 | 2 | 方法名不匹配 + 旧数据残留 |
| 2 | K线数据层 | 🔴 问题 | 3 | 方法不存在 + 时间戳未重置 + 旧文件残留 |
| 3 | 核心引擎 | ⚠️ 风险 | 3 | 线程安全 + 品种池过大 + 冷却机制 |
| 4 | 策略模块 | ✅ 正常 | 0 | 4策略全可用，CZSC多级别确认正常 |
| 5 | 风控引擎 | ✅ 正常 | 0 | 固定50U保证金逻辑正确 |
| 6 | 订单管理 | ✅ 正常 | 0 | DRY-RUN/实盘路径完整 |
| 7 | 通知模块 | ✅ 正常 | 0 | Telegram推送正常 |
| 8 | 配置文件 | ⚠️ 风险 | 1 | 品种池155个过大 |
| 9 | 脚本 | ✅ 正常 | 0 | update_symbols_pool.py 逻辑完整 |
| 10 | .gitignore 安全性 | ✅ 正常 | 0 | secrets/ 已排除 |
| 11 | 代码质量 | ⚠️ 风险 | 3 | 未使用导入 + 线程不安全 |
| 12 | 回测引擎 | ⚠️ 风险 | 2 | 数据源不一致 + 持仓跟踪不完整 |

---

## 二、问题详细列表（按严重程度排序）

### 🔴 P0 — 严重问题（必须修复）

#### P0-1: `KlineManager.update_symbols()` 调用了不存在的方法
- **文件:** `core/kline_manager.py` 第 347-348 行
- **问题:** `update_symbols()` 方法中调用了 `self._fetch_and_save_1m(sym)` 和 `self._synthesize_2m(sym)`，但这两个方法根本不存在。正确的方法名是 `self.initial_fetch_1m_klines(sym)` 和 `self._synthesize_2m_for_symbol(sym)`。
- **影响:** 当每日品种池自动更新引入新品种时，`update_symbols()` 会抛出 `AttributeError` 崩溃。
- **验证:**
  ```
  $ python3 -c "from core.kline_manager import KlineManager; km = KlineManager(['BTCUSDT']); km._fetch_and_save_1m('ETHUSDT')"
  AttributeError: 'KlineManager' object has no attribute '_fetch_and_save_1m'
  ```
- **修复:** 将第 347-348 行改为：
  ```python
  self.initial_fetch_1m_klines(sym)
  self._synthesize_2m_for_symbol(sym)
  ```

#### P0-2: 新增品种时 `_last_update_time` 未重置
- **文件:** `core/kline_manager.py` 第 320-349 行 (`update_symbols` 方法)
- **问题:** 新增品种加入 `self.symbols` 列表后，`_last_update_time` 字典中没有清除该品种的旧时间戳。如果之前采集过该品种后又被移除，时间戳残留会导致增量更新使用错误的时间起点。
- **影响:** 新增品种可能遗漏 K 线数据或使用错误的时间范围。
- **修复:** 在 `update_symbols` 的 `if added:` 分支中添加：
  ```python
  for sym in added:
      self._last_update_time.pop(sym, None)  # 清除旧时间戳
  ```

#### P0-3: 1,305 个旧格式 JSON 文件残留（41MB 磁盘浪费）
- **位置:** `data/klines/*.json`（共 1,305 个文件）
- **问题:** 系统已迁移到 `data/klines/1m/*.jsonl` + `data/klines/2m/*.jsonl` 的新格式，但旧的 `{SYMBOL}_{TIMEFRAME}_{TIMESTAMP}.json` 文件未被清理，占用约 41MB 磁盘空间。这些文件不再被任何代码使用。
- **影响:** 磁盘空间浪费，目录混乱，增加备份/迁移负担。
- **修复建议:** 清理命令：
  ```bash
  rm -f /root/projects/futures-trading-system/data/klines/*.json
  ```

---

### ⚠️ P1 — 中等风险（建议修复）

#### P1-1: 线程安全 — 共享数据无锁保护
- **文件:** `core/engine.py`（风控线程 vs 主线程）、`core/kline_manager.py`（更新/清理线程）
- **问题:** 
  - 主线程和风控巡检线程（`_risk_monitor_loop`）同时访问 `self.account` 对象。风控线程调用 `refresh_account()` 修改 `account.positions`，而主线程在 `run_cycle()` 中也读写 `account.positions`。无 `threading.Lock` 保护。
  - `KlineManager` 的更新线程和清理线程同时读写同一个 JSONL 文件，无文件锁保护。
- **影响:** 理论上可能出现数据竞争（race condition），但在 Python GIL 下实际触发概率较低。不过 `_strategy_errors` 字典在 `analyze()` 中被修改，虽然目前只在主线程调用。
- **修复建议:** 对 `self.account` 和 JSONL 文件操作添加 `threading.Lock`。

#### P1-2: 品种池过大（155 个品种），每轮分析耗时过长
- **文件:** `config/symbols.json`
- **问题:** 当前 watchlist 有 155 个 enabled 品种。引擎每个 cycle 对所有品种逐一分析，每个品种需要获取 2m + 15m K 线数据、运行 4 个策略。cycle_interval=60s 可能不够。
- **影响:** 60 秒内无法完成全部 155 个品种的分析 → 品种间处理间隔拉长 → 信号延迟。
- **验证:** 引擎初始化时 `KlineManager` 加载了 155 个品种。
- **修复建议:** 考虑分批处理、或限制每轮分析品种数量（如前 20 高优先级品种）。

#### P1-3: 回测引擎数据源与实盘引擎不一致
- **文件:** `core/backtest.py` vs `core/engine.py`
- **问题:** 
  - 回测引擎 `load_klines()` 从 `data/klines/{symbol}_{tf}_{timestamp}.json`（旧格式）读取或从 API 拉取
  - 实盘引擎 `fetch_klines()` 从 `KlineManager`（JSONL 新格式）读取
  - 两者可能获取到不同的数据，导致回测结果与实盘表现不一致
- **影响:** 回测结果参考性降低
- **修复建议:** 回测引擎应优先从 KlineManager 读取 2m JSONL 数据

#### P1-4: 回测引擎持仓 `_entry_bar` 属性未设置
- **文件:** `core/backtest.py` 第 289-300 行 (`_open_position`)
- **问题:** `_open_position` 创建 `Position` 对象时未设置 `_entry_bar` 属性，但 `_close_position`（第 321 行）和 `_check_sl_tp` 中通过 `getattr(self.position, '_entry_bar', bar_idx)` 访问，总是 fallback 到当前 bar_idx，导致 `bars_held` 始终为 0。
- **影响:** 回测报告中 `bars_held` 字段不准确，影响策略分析
- **修复:** 在 `_open_position` 中添加：
  ```python
  self.position._entry_bar = bar_idx
  ```

#### P1-5: `engine.py` 中 CZSC 策略传入了 15m 确认 K 线但策略已只使用 2m
- **文件:** `core/engine.py` `process_signals()` 方法
- **说明:** 代码中 `candles_confirm=candles_15m` 传入了 15m K 线给 CZSC，`czsc_strategy.py` 中也正确使用 `candles_confirm` 做多级别确认。这实际上**是正确的设计**，不是 bug。但如果皇上旨意要求"CZSC 策略只用 2m 数据"，则需要移除 15m 确认逻辑。
- **当前状态:** ✅ 2m 为主 + 15m 确认是可选增强，符合当前设计

---

### ℹ️ P2 — 轻微问题（可选优化）

#### P2-1: 未使用的导入
- `core/engine.py`: `timedelta` (行30), `Any` (行31), `asdict` (行32)
- `core/kline_manager.py`: `Any` (行23), `OrderedDict` (行24)
- `core/notify.py`: `json` (行16), `Optional` (行20)
- `core/backtest.py`: `Dict` (行21), `Tuple` (行21), `Indicators` (行33), `RiskEngine` (行33), `AccountState` (行33)

#### P2-2: 信号冷却机制可能遗漏快速反转
- **文件:** `core/engine.py` `execute_signal()` 
- **问题:** `signal_cooldown = 300`（5 分钟），同币种 5 分钟内不重复发信号。如果市场快速反转，可能被错过。
- **当前值:** 来自 risk.json `signal_cooldown_seconds: 300`

#### P2-3: `.env` 文件中 API Key 明文存储
- **文件:** `.env`
- **说明:** API Key 明文存储。`.gitignore` 已排除 `.env`，但本地文件仍明文。`secrets/api_keys.env` 也是如此。
- **风险等级:** 中等（本地服务器，非公开）

---

## 三、系统整体评估

### ✅ 正常运作的部分
1. **模块导入:** 所有 5 个核心模块 + 策略模块均可正常导入
2. **策略自检:** 4/4 策略全部就绪（trend_follow, mean_reversion, breakout, czsc）
3. **固定保证金:** 引擎和回测均正确使用 `fixed_margin_usdt: 50`
4. **风控逻辑:** 止损 2%、止盈 4%、追踪止损、最大持仓 3 个、日亏损限制 5%、总回撤 15% — 全部正确实现
5. **信号聚合:** 加权投票 + 冲突检测 + 多策略一致加成 — 逻辑完整
6. **DRY-RUN/实盘双路径:** 开仓/平仓在两种模式下均有完整实现
7. **通知模块:** Telegram 推送配置完整，各事件触发点已覆盖
8. **`.gitignore`:** `.env`, `secrets/`, `data/klines/`, `logs/`, `state/` 全部排除

### ⚠️ 需要关注的风险
1. **P0-1 方法不存在** — 品种池动态更新时会崩溃（但当前未触发，因为品种池今日已更新过）
2. **品种池 155 个** — 60 秒 cycle 可能无法覆盖全部品种
3. **线程安全** — 无锁保护，Python GIL 下风险较低但不能忽视

### 系统整体评分: **7.5/10**

- 架构设计: 8/10（模块化清晰，职责分离好）
- 代码质量: 7/10（有一些小问题但无致命 bug 除 P0-1 外）
- 风控完备: 8/10（多层风控，固定保证金一致）
- 运维安全: 7/10（.gitignore 完善，但线程安全需加强）
- 数据管理: 6/10（新旧格式共存，旧文件需清理）

---

## 四、修复优先级建议

| 优先级 | 问题 | 修复难度 | 建议 |
|--------|------|----------|------|
| 🔴 P0 | 方法不存在导致崩溃 | 低（改 2 行） | **立即修复** |
| 🔴 P0 | 新增品种时间戳残留 | 低（加 1 行） | **立即修复** |
| 🔴 P0 | 旧 JSON 文件清理 | 极低（1 条命令） | **建议执行** |
| ⚠️ P1 | 线程安全加锁 | 中（需重构） | 排期修复 |
| ⚠️ P1 | 品种池分批处理 | 中 | 排期优化 |
| ⚠️ P1 | 回测数据源一致 | 低 | 排期修复 |
| ⚠️ P1 | 回测 bars_held=0 | 低（加 1 行） | 建议修复 |
| ℹ️ P2 | 清理未使用导入 | 极低 | 可顺手做 |

---

## 五、证据引用

| 问题 | 文件 | 行号 | 关键代码 |
|------|------|------|----------|
| P0-1 | kline_manager.py | 347-348 | `self._fetch_and_save_1m(sym)` — 方法不存在 |
| P0-2 | kline_manager.py | 320-349 | `update_symbols()` 未操作 `_last_update_time` |
| P0-3 | data/klines/ | — | 1,305 个 .json 文件共 41MB |
| P1-1 | engine.py | 1635-1640 | `_risk_thread` 无锁访问 `self.account` |
| P1-2 | symbols.json | — | `"total_symbols": 155` |
| P1-3 | backtest.py | 144-160 | `load_klines()` 从旧 JSON 读取 |
| P1-4 | backtest.py | 289-300 | `_open_position` 未设 `_entry_bar` |
| P2-1 | engine.py | 30 | `from datetime import datetime, timezone, timedelta` — timedelta 未使用 |
| P2-2 | engine.py | 1142 | `self.signal_cooldown = 300` |

---

**报告完成。** 建议在启动自动交易前先修复 P0-1 和 P0-2，否则品种池动态更新功能会在引入新品种时崩溃。
