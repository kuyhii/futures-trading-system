# 🔍 量化交易系统全面审查报告

**任务ID:** JJC-20260510-002
**审查时间:** 2026-05-10 10:10 UTC
**系统路径:** /root/projects/futures-trading-system/
**审查范围:** 风控系统、策略配置、交易引擎、配置参数、通知系统、安全性

---

## 一、系统架构概览

```
core/engine.py (1200+行)  ← 核心交易引擎（主循环）
├── BinanceClient          ← API 封装（HTTP轮询，带重试+熔断）
├── StrategyEngine         ← 5种策略信号生成（3种已启用）
├── RiskEngine             ← 风控审核+止损/止盈/追踪止损
├── OrderManager           ← 开仓/平仓/止损止盈下单
└── TradingEngine          ← 主循环：刷新账户→风控巡检→策略计算→执行信号
```

**当前运行状态:**
- 模式: DRY-RUN 模拟（testnet 环境）
- 已执行 202 个周期（约 3.5 小时），最后已停止
- 测试网 API 已认证，持有 3 个真实 testnet 持仓
- 总权益: ~4,942 USDT

---

## 二、风控系统审查

### 2.1 止损/止盈参数

| 参数 | 配置值 | 评价 |
|------|--------|------|
| 止损 | -2% | 🟢 合理，保守设置 |
| 止盈 | +4% | 🟢 盈亏比 1:2 |
| 追踪止损 | 1% | 🟢 合理 |
| 最大杠杆 | 5x | 🟢 保守设置 |
| 单笔仓位 | 10% | 🟢 合理 |
| 最大持仓 | 3个 | 🟢 合理 |
| 日亏损限制 | -5% | 🟢 合理 |
| 总回撤限制 | -15% | 🟢 合理 |

### 2.2 🔴 严重问题：RENDERUSDT 浮亏 -34.7% 未被真实平仓

**问题描述：** RENDERUSDT SHORT 持仓浮亏已超 -34%，引擎每 60 秒检测到止损条件并记录 "DRY-RUN 平仓"，但 **从未实际执行平仓**。

**根本原因（代码证据：`core/engine.py` Line 479-486）：**
```python
def close_position(self, symbol: str, dry_run: bool = False, reason: str = "") -> dict:
    if dry_run:
        result["status"] = "dry_run_approved"
        logger.info(f"🔵 DRY-RUN 平仓: {symbol} (原因: {reason})")
        return result   # ← 直接返回，不执行任何 API 调用！
```

**后果：**
- 引擎日志中 "DRY-RUN 平仓" 出现 **651 次**（每周期 3 个持仓 × ~217 周期）
- 每次只写日志，不执行币安 API 平仓调用
- testnet 上的真实持仓（BNBUSDT/LTCUSDT/RENDERUSDT）持续存在，RENDERUSDT 浮亏不断扩大

**实盘风险：🔴 致命** — 如果切换到 `--live` 模式，止损逻辑才真正执行。但 DRY-RUN 模式下持仓风险完全失控。

### 2.3 🔴 严重问题：风控检查间隔不合理

**当前配置：** 主循环间隔 60 秒（`engine.py:633: self.cycle_interval = 60`）

**问题：** 
- 60 秒对于止损检查来说太慢了。加密货币市场可能在几秒内发生大幅波动
- 对于 20x 杠杆（如 RENDERUSDT 当前实际杠杆），2% 止损 ≈ 0.1% 价格波动，60 秒间隔内完全可能穿过止损线
- 建议：风控巡检应独立于主循环，至少 10-15 秒一次

### 2.4 ⚠️ 风险：最大持仓数与策略信号冲突

**现象：** 引擎已生成 BTCUSDT/ETHUSDT 交易信号，但因"持仓数已达上限 (3/3)"被拒绝。
- 3 个持仓（BNBUSDT/LTCUSDT/RENDERUSDT）并非由引擎策略产生，而是 testnet 上已有的老持仓
- 引擎不会主动清理"非策略持仓"，导致风控 max_positions 检查永远阻止新信号

### 2.5 ⚠️ 风险：实际杠杆与配置不符

**实测 testnet 持仓杠杆：**
| 币种 | 实际杠杆 | 配置上限 | 状态 |
|------|----------|----------|------|
| BNBUSDT | 10x | 5x | 🔴 超限 |
| LTCUSDT | 10x | 5x | 🔴 超限 |
| RENDERUSDT | 20x | 5x | 🔴 严重超限 |

**注意：** 这些是 testnet 上已存在的持仓，引擎的杠杆检查只在开仓前执行，不会检查已有持仓的杠杆合规性。

---

## 三、策略配置审查

### 3.1 已启用策略参数

| 策略 | 状态 | 核心参数 |
|------|------|----------|
| trend_follow | ✅ 启用 | MA(9/21) + MACD(12/26/9) + 放量确认(1.2x) |
| mean_reversion | ✅ 启用 | RSI(14) <30/>70 + 布林带(20, 2σ) |
| breakout | ✅ 启用 | 20周期高低点突破 + 放量确认(1.3x) |
| funding_rate | ❌ 禁用 | 阈值 0.1% |
| lsr_reversal | ❌ 禁用 | 大户多空比 70%/30% |

**评价：** 🟢 参数设置合理，但有以下问题：

### 3.2 ⚠️ 信号置信度阈值未配置

**问题：** 代码中没有最低置信度阈值检查（`core/engine.py` 未设置 `min_confidence`）。
- breakout 策略的置信度低至 0.5（无放量确认时）仍会执行
- trend_follow 无放量确认时置信度 0.6 也会执行
- 建议：设置 `min_confidence = 0.7`，低于此值的信号仅记录不执行

### 3.3 ⚠️ 信号冷却期 300 秒评价

**当前：** 5 分钟冷却（`engine.py:641: self.signal_cooldown = 300`）
- 对于 15m K 线 → 🟢 合理（每根 K 线最多触发 3 次）
- 但冷却是按币种而不是按策略，同一币种的不同策略信号会互相阻塞

### 3.4 🔴 为什么长时间没有新交易信号

**根因分析：**
1. **持仓占满 3 个槽位** — 所有新信号都被 `max_positions` 风控拒绝
2. **监控币种仅 BTC/ETH** — `symbols.json` 中只启用了 BTCUSDT 和 ETHUSDT，BNB/LTC/RENDER 不在监控列表中
3. **市场处于震荡期** — 从日志看，大部分周期输出"无交易信号"，说明 MA/RSI/突破条件多数时间不满足
4. **策略同质化** — 3 个策略都基于 15m K 线，使用相似的输入数据，信号容易重叠

### 3.5 ⚠️ 策略在不同行情下的表现差异

| 策略 | 趋势行情 | 震荡行情 | 突破行情 |
|------|----------|----------|----------|
| trend_follow | ✅ 优秀 | ❌ 频繁假信号 | ⚠️ 一般 |
| mean_reversion | ❌ 逆势亏损 | ✅ 优秀 | ❌ 可能踏空 |
| breakout | ✅ 优秀 | ❌ 假突破多 | ✅ 优秀 |

**建议：** 增加行情识别模块，动态选择活跃策略。

---

## 四、交易引擎审查

### 4.1 核心逻辑流程 🟢

```
run_cycle():
  1. refresh_account()      ← 拉取账户余额+持仓
  2. monitor_positions()     ← 止损/止盈/追踪止损检查
  3. 熔断检查
  4. process_signals()       ← 获取K线 → 策略分析 → 信号聚合
  5. execute_signal()        ← 风控审核 → 开仓/平仓
  6. save_state()            ← 持久化状态
```

### 4.2 🔴 开仓逻辑缺陷

**仓位计算（`engine.py:673-684`）：**
```python
def calc_position_size(self, account, price, leverage, risk_pct=None):
    max_value = account.total_equity * risk_pct / 100  # 10000 * 10% = 1000
    nominal = max_value * leverage                      # 1000 * 3 = 3000
    quantity = nominal / price                          # 3000 / 80000 = 0.0375 BTC
```

**问题：**
- 公式使用了 `total_equity` 而非 `available_balance`
- 当已有持仓占用保证金时，可能导致仓位计算偏大
- 未考虑已有持仓的保证金占用

### 4.3 🔴 持仓管理缺陷

**问题 1：Position 对象缺少关键追踪字段**
- `entry_time` 由引擎刷新账户时设置为当前时间，不是真实开仓时间
- 缺少 `real_entry_price`（区分开仓价和后续加减仓均价）
- 缺少 `signal_strategy` 字段记录是哪个策略触发的开仓

**问题 2：追踪止损初始值**
```python
@dataclass
class Position:
    highest_pnl: float = 0  # 追踪止损用
```
- `highest_pnl` 初始为 0，如果持仓一开仓就盈利，会正确更新
- 但如果开仓后立即亏损，`highest_pnl` 一直为 0，追踪止损永远不会触发
- **这是逻辑 bug**：应该初始化为开仓时的 PnL（即 0），但只在盈利后才激活追踪止损

### 4.4 🔴 平仓逻辑：DRY-RUN vs 实盘

| 模式 | 止损/止盈触发后 | 实际行为 |
|------|----------------|----------|
| DRY-RUN | 记录日志 | ❌ 不调用 API |
| 实盘 | 记录日志 + API 调用 | ✅ 调用 API 平仓 |

**风险：** 如果从 DRY-RUN 直接切到实盘，中间没有过渡期，已存在的 testnet 持仓可能在实盘模式下被引擎平仓，导致非预期交易。

### 4.5 ⚠️ 错误处理和异常恢复

**熔断机制（`engine.py:96-105`）：**
- 连续 5 次 API 错误触发熔断
- 熔断后冷却 60 秒
- 🟢 有基本保护

**但缺少：**
- 没有心跳/存活检测
- 没有自动重启机制
- 引擎被 kill 后状态不保存（`engine_state.json` 仅记录元信息，不保存持仓快照）

---

## 五、配置参数审查

### 5.1 .env 文件

```
BINANCE_API_ENV=testnet          ← 🟢 正确
BINANCE_API_KEY=VQACnrav13...    ← ⚠️ 明文存储
BINANCE_SECRET_KEY=2mbMVCRw...   ← ⚠️ 明文存储
BINANCE_PROD_API_KEY=            ← 🟢 空，安全
DEFAULT_LEVERAGE=3               ← 🟢 保守
QTY_PRECISION_BTC=3              ← 🟢 合理
QTY_PRECISION_ETH=2              ← 🟢 合理
```

### 5.2 🔴 API 密钥硬编码在 .env 文件中

**问题：** `.env` 文件包含明文 API Key/Secret，虽然 PLAN.md 说不提交到 Git，但文件权限和访问控制不明确。

### 5.3 ⚠️ 币种选择逻辑矛盾

| 配置文件 | 监控币种 | 实际持仓 |
|----------|----------|----------|
| symbols.json | BTCUSDT, ETHUSDT | BNBUSDT, LTCUSDT, RENDERUSDT |
| strategies.json | BTCUSDT, ETHUSDT | — |

**引擎只监控 BTC/ETH，但实际持仓是 BNB/LTC/RENDER。**
- 引擎不会对 BNB/LTC/RENDER 进行策略分析
- 引擎对这些持仓只做风控检查（止损/止盈）
- 这些持仓是 testnet 上的"遗留持仓"

### 5.4 ⚠️ API 调用频率

**估算每个周期的 API 调用：**
- 账户余额: 2 次（balance + account）
- 持仓查询: 1 次
- K 线获取: 4 次（BTC 15m + 1h, ETH 15m + 1h）
- 标记价格: N 次（每个持仓 1 次）
- **合计：每周期约 7-10 次**

60 秒周期 × 10 次 = 约 10 次/分钟，币安限制为 2400 次/分钟，**🟢 安全**。

---

## 六、通知系统审查

### 6.1 🔴 Telegram 通知未接入引擎

**核心问题：** `core/notify.py` 已实现完整的 Telegram 推送功能，但 **引擎从未调用它**。

**证据：**
```python
# core/engine.py — 没有任何 import notify 的代码
# core/engine.py — 没有任何 notifier.xxx() 调用
```

**notify.py 的环境变量要求：**
```python
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")    # 未设置
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")        # 未设置
```

### 6.2 启用步骤

需要在 `.env` 中添加：
```
TELEGRAM_BOT_TOKEN=<your_bot_token>
TELEGRAM_CHAT_ID=<your_chat_id>
```

然后在 `core/engine.py` 中导入并调用：
```python
from core.notify import notifier

# 在开仓/平仓/风控警告处添加：
notifier.trade_opened(...)
notifier.trade_closed(...)
notifier.risk_warning(...)
```

### 6.3 ⚠️ 通知日志

- `logs/notifications.log` 文件不存在（因为从未使用）
- notify.py 使用了 `async/await`（aiohttp），但引擎是同步的，依赖 `requests` 回退

---

## 七、安全性审查

### 7.1 🔴 测试网 API 密钥暴露

**.env 文件中的 API Key 是测试网密钥：**
```
BINANCE_API_KEY=VQACnrav136fYn7LPPgy1mde1GtA60wD5B6YEU1nGJ1ovMCZIewzOc2BYg1riRjw
BINANCE_SECRET_KEY=2mbMVCRwl9192r7FNySunrGaatmkhq6XQN8Fu1zGbY3pFQypbO8OagqxxhhT0eiF
```

**风险：** 虽然是测试网，但密钥已暴露。切换到实盘时如果忘记更换密钥，可能导致实盘资金风险。

### 7.2 🟢 权限检查

- 测试网环境 → 🟢 安全（不涉及真实资金）
- `BINANCE_PROD_API_KEY` 为空 → 🟢 安全
- `BINANCE_API_ENV=testnet` → 🟢 安全

### 7.3 ⚠️ 系统崩溃恢复

**当前机制：**
- 引擎被 kill 后停止运行（通过 SIGINT/SIGTERM 处理）
- 无自动重启
- 无看门狗进程
- `engine_state.json` 仅保存周期计数和账户快照，不保存策略状态

### 7.4 ⚠️ .env 文件权限

```
建议: chmod 600 .env  # 仅 owner 可读写
```

---

## 八、参数优化建议

### 8.1 止损/止盈优化

| 参数 | 当前值 | 建议值 | 理由 |
|------|--------|--------|------|
| 止损 | 2% | 1.5-2.5%（按 ATR 动态） | 固定百分比不适应不同波动率 |
| 止盈 | 4% | 3-6%（按 ATR 动态） | 配合动态止损 |
| 追踪止损触发点 | 止盈后 | 盈利 >1.5% 后激活 | 当前逻辑有 bug |
| 追踪止损距离 | 1% | 0.5-1.5%（按 ATR） | 固定距离不适应不同币种 |

### 8.2 信号过滤优化

```json
// 建议在 strategies.json 中添加：
{
  "min_confidence": 0.7,
  "signal_cooldown_per_strategy": true,
  "market_regime_detection": true
}
```

### 8.3 监控币种扩展

```json
// symbols.json 建议增加：
{"symbol": "BNBUSDT", "enabled": true, "priority": 3},
{"symbol": "SOLUSDT", "enabled": true, "priority": 4}
```

---

## 九、实盘前必须解决的问题清单

### 🔴 必须解决（否则不可实盘）

1. **[P0] 清理 testnet 遗留持仓** — 当前 BNBUSDT/LTCUSDT/RENDERUSDT 是手动开仓的老持仓，非引擎策略产生，且杠杆超限。实盘前必须清仓重新开始。

2. **[P0] 修复 DRY-RUN 平仓逻辑** — 当前 dry_run=True 时完全不执行平仓，导致风控形同虚设。建议增加 `dry_run_execute` 选项，在模拟模式下也模拟执行完整的开平仓流程（记录虚拟成交）。

3. **[P0] 接入 Telegram 通知** — 实盘交易没有实时通知等于盲开。需在 engine.py 中导入 notify 模块并在关键事件处调用。

4. **[P0] 解决持仓槽位占满问题** — 引擎需要有能力检测并处理"非策略持仓"，要么纳入监控列表，要么自动平仓释放槽位。

5. **[P1] 修复追踪止损初始值逻辑** — 开仓即亏损时追踪止损永不触发。

### ⚠️ 强烈建议解决

6. **[P1] 风控巡检独立于主循环** — 将止损检查频率提升到 10-15 秒。

7. **[P1] 设置最低信号置信度阈值** — 拒绝 <0.7 的信号。

8. **[P1] 增加行情识别模块** — 区分趋势/震荡/突破行情，动态启用对应策略。

9. **[P1] 添加 ATR 动态止损** — 替代固定百分比止损。

10. **[P2] 配置自动重启机制** — systemd 或 cron 监控引擎存活状态。

11. **[P2] 加强 API 密钥管理** — .env 权限设为 600，考虑密钥轮换。

12. **[P2] 将 BNB/LTC/RENDER 纳入监控列表** — 或清掉这些持仓。

---

## 十、证据文件路径

| 证据 | 路径 |
|------|------|
| 核心引擎代码 | `/root/projects/futures-trading-system/core/engine.py` |
| 回测引擎代码 | `/root/projects/futures-trading-system/core/backtest.py` |
| 通知模块代码 | `/root/projects/futures-trading-system/core/notify.py` |
| 风控配置 | `/root/projects/futures-trading-system/config/risk.json` |
| 策略配置 | `/root/projects/futures-trading-system/config/strategies.json` |
| 币种配置 | `/root/projects/futures-trading-system/config/symbols.json` |
| 引擎运行日志 | `/root/projects/futures-trading-system/logs/engine.log` |
| 引擎实时日志 | `/root/projects/futures-trading-system/logs/engine_live.log` |
| 引擎状态 | `/root/projects/futures-trading-system/state/engine_state.json` |
| 交易历史 | `/root/projects/futures-trading-system/state/trade_history.json` |
| 每日报告 | `/root/projects/futures-trading-system/data/reports/daily_2026-05-10.json` |
| 环境变量 | `/root/projects/futures-trading-system/.env` |

---

## 十一、总结

系统代码框架完整，5 种策略架构齐全，风控规则设计合理。但存在 **4 个实盘前必须解决的严重问题**，核心在于 DRY-RUN 模式下风控"只报警不执行"导致遗留持仓失控，以及通知系统未接入。

建议优先级：清理持仓 → 修复 DRY-RUN 逻辑 → 接入通知 → 优化风控频率 → 小资金 testnet 验证 48 小时 → 再考虑实盘。

---

*审查人: 中书省 | 任务ID: JJC-20260510-002*
