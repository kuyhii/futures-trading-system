# 币安 U 本位合约自动量化交易系统 - 总体方案

> 老八发起，Agent a 制定方案，交予太子 taizi 主导开发。

## 一、系统定位

- **市场**: 仅币安 U 本位合约（USDS-M Futures）
- **平台**: OpenClaw 驱动，以 Agent 为核心
- **数据源**: binance-cli（官方 CLI）+ Web3 API（Smart Money/市场排名）
- **阶段**: V1 最小可行版，后期可扩展

---

## 二、系统架构

```
┌─────────────────────────────────────────────────────┐
│                    用户（老八 via Telegram）            │
│              指令 / 策略配置 / 监控面板                  │
└──────────────────────┬──────────────────────────────┘
                       │
              ┌────────▼────────┐
              │   决策 Agent     │  ← 太子 taizi（总指挥/策略引擎核心）
              │  (策略引擎核心)   │
              └────────┬────────┘
                       │
          ┌────────────┼─────────────────┐
          ▼            ▼                 ▼
   ┌──────────┐  ┌──────────┐    ┌──────────────┐
   │ 数据层    │  │ 风控层    │    │  执行层       │
   │ Data     │  │ Risk Mgmt│    │  Execution   │
   └──────────┘  └──────────┘    └──────────────┘
          │            │                 │
   ┌──────┴──────┐     │          ┌──────┴──────┐
   │binance-cli  │     │          │binance-cli  │
   │futures-usds │     │          │new-order    │
   │kline/oi/fund│     │          │cancel/modify│
   │Web3 signal  │     │          │             │
   │Web3 rank    │     │          │             │
   └─────────────┘     │          └─────────────┘
                       │
                ┌──────┴──────┐
                │ 仓位/杠杆    │
                │ 止损/止盈    │
                │ 最大回撤     │
                │ 单笔限额     │
                └─────────────┘
```

---

## 三、分层详细设计

### 3.1 数据层（Data Layer）

**职责**: 采集行情数据，提供给策略引擎

| 数据类型 | 来源 | 具体接口 | 用途 |
|---------|------|---------|------|
| K 线数据 | binance-cli | `futures-usds kline-candlestick-data` | 技术指标计算 |
| 标记价格 | binance-cli | `futures-usds mark-price` | 实时价格监控 |
| 订单簿 | binance-cli | `futures-usds order-book` | 盘口分析 |
| 资金费率 | binance-cli | `futures-usds get-funding-rate-history` | 资金费率策略 |
| 持仓量 | binance-cli | `futures-usds open-interest-statistics` | 多空力量判断 |
| 多空比 | binance-cli | `futures-usds long-short-ratio` | 情绪指标 |
| 大户多空比 | binance-cli | `futures-usds top-trader-long-short-ratio-accounts` | 聪明钱方向 |
| 成交量分布 | binance-cli | `futures-usds taker-buy-sell-volume` | 买卖力量 |
| Smart Money | Web3 API | `trading-signal` skill | 链上聪明钱信号 |
| 热门币种 | Web3 API | `crypto-market-rank` skill | 选币参考 |

**采集频率**:
- K 线: 1m/5m/15m/1h（按需拉取）
- 标记价格/资金费率: 每 5 分钟
- 持仓量/多空比: 每 15 分钟
- Smart Money/排名: 每 30 分钟

### 3.2 策略引擎（Strategy Engine）

**V1 内置策略**:

| 策略 | 逻辑 | 触发条件 |
|------|------|---------|
| 趋势跟踪 | MA 交叉 + 成交量确认 | 金叉/死叉 + 放量 |
| 均值回归 | RSI 超买超卖 + 布林带 | RSI >70 做空, RSI <30 做多 |
| 资金费率套利 | 极端费率反向开仓 | 费率 >0.1% 反向 |
| 突破策略 | 突破 N 日高低点 | 价格突破 + 持仓量配合 |
| 多空比反转 | 大户多空比极端值反转 | 多空比 >70% 反向 |

**策略配置** (`config/strategies.json`):
```json
{
  "active": ["trend_follow", "mean_reversion"],
  "symbols": ["BTCUSDT", "ETHUSDT"],
  "timeframe": "15m",
  "indicators": {
    "ma_fast": 9,
    "ma_slow": 21,
    "rsi_period": 14,
    "rsi_overbought": 70,
    "rsi_oversold": 30
  }
}
```

### 3.3 风控层（Risk Management）

**硬性规则**:

| 规则 | 参数 | 说明 |
|------|------|------|
| 单笔止损 | -2% | 每笔亏损不超过本金 2% |
| 单笔止盈 | +4% | 盈亏比 ≥ 1:2 |
| 最大杠杆 | 5x | 新手保守设置，可调 |
| 单笔仓位 | ≤ 10% | 单笔不超过总资金 10% |
| 最大持仓 | 3 个 | 同时最多 3 个合约 |
| 日最大亏损 | -5% | 当日亏损达 5% 停止交易 |
| 总回撤 | -15% | 总资金回撤 15% 清仓暂停 |
| 强制平仓 | 启用 | 触发止损自动平仓 |

**风控配置** (`config/risk.json`):
```json
{
  "max_leverage": 5,
  "position_size_pct": 10,
  "stop_loss_pct": 2,
  "take_profit_pct": 4,
  "max_positions": 3,
  "daily_loss_limit_pct": 5,
  "max_drawdown_pct": 15,
  "trailing_stop": true,
  "trailing_distance_pct": 1
}
```

### 3.4 执行层（Execution Layer）

**通过 `binance-cli` 执行交易**:

| 操作 | binance-cli 命令 |
|------|-----------------|
| 开多仓 | `binance-cli futures-usds new-order --symbol BTCUSDT --side BUY --type MARKET --quantity 0.001` |
| 开空仓 | `binance-cli futures-usds new-order --symbol BTCUSDT --side SELL --type MARKET --quantity 0.001` |
| 限价开仓 | `binance-cli futures-usds new-order --symbol BTCUSDT --side BUY --type LIMIT --price 42000 --quantity 0.001` |
| 设置止损止盈 | `binance-cli futures-usds new-order --symbol BTCUSDT --side SELL --type STOP_MARKET --stopPrice 41000 --closePosition true` |
| 平仓 | `binance-cli futures-usds new-order --symbol BTCUSDT --side SELL --type MARKET --quantity 0.001 --reduceOnly true` |
| 查询持仓 | `binance-cli futures-usds position-information-v3` |
| 查询余额 | `binance-cli futures-usds futures-account-balance-v3` |
| 修改杠杆 | `binance-cli futures-usds change-initial-leverage --symbol BTCUSDT --leverage 5` |
| 修改保证金类型 | `binance-cli futures-usds change-margin-type --symbol BTCUSDT --margin-type ISOLATED` |
| 取消所有挂单 | `binance-cli futures-usds cancel-all-open-orders --symbol BTCUSDT` |
| 查询当前挂单 | `binance-cli futures-usds current-all-open-orders` |
| 查询历史成交 | `binance-cli futures-usds account-trade-list --symbol BTCUSDT --limit 50` |

**订单类型**:
- `MARKET`: 市价单
- `LIMIT`: 限价单
- `STOP_MARKET`: 止损市价单
- `TAKE_PROFIT_MARKET`: 止盈市价单
- `STOP`: 限价止损单
- `TRAILING_STOP_MARKET`: 追踪止损

---

## 四、OpenClaw 集成方案

### 4.1 Agent 角色分工（三省六部制）

```
太子 (taizi)          → 总指挥 / 策略审批
  └─ 中书 (zhongshu)  → 策略生成，交易计划
       └─ 门下 (menxia) → 风控审核，交易批准/否决
            └─ 尚书 (shangshu) → 执行调度
                 ├─ 户部 (hubu) → 资金管理，余额监控
                 ├─ 吏部 (libu) → 策略管理，参数调整
                 ├─ 兵部 (bingbu) → 信号分析，指标计算
                 ├─ 刑部 (xingbu) → 风控执行，强制平仓
                 └─ 工部 (gongbu) → 系统维护，数据记录
```

> V1 阶段可以先在 taizi 中跑通全部功能，后续再拆分到三省六部 Agent。

### 4.2 工作空间结构

```
/root/.openclaw/workspace-taizi/futures-trading-system/
├── PLAN.md              ← 本方案文件
├── config/
│   ├── strategies.json  ← 策略配置
│   ├── risk.json        ← 风控配置
│   └── symbols.json     ← 监控币种列表
├── data/
│   ├── klines/          ← K 线缓存
│   ├── signals/         ← 信号记录
│   └── positions/       ← 持仓快照
├── logs/
│   ├── trades.log       ← 交易日志
│   ├── risk.log         ← 风控日志
│   └── system.log       ← 系统日志
├── scripts/
│   ├── fetch_data.sh         ← 数据采集脚本
│   ├── check_risk.sh         ← 风控检查脚本
│   ├── trade.sh              ← 交易执行脚本
│   ├── test_connectivity.sh  ← 连通性测试
│   ├── strategy_engine.py    ← 策略引擎（技术指标+信号）
│   ├── orchestrator.py       ← 主调度器（周期编排）
│   └── daily_report.py       ← 每日报告生成
└── state/
    ├── daily_pnl.json        ← 每日盈亏
    └── trade_history.json    ← 交易历史
```

### 4.3 定时任务（Cron Jobs）

| 任务 | 频率 | 说明 |
|------|------|------|
| 行情采集 | 每 5 分钟 | 拉取 K 线、价格、持仓量 |
| 策略计算 | 每 5 分钟 | 运行策略引擎，产生交易信号 |
| 风控巡检 | 每 1 分钟 | 检查止损、止盈、回撤 |
| Smart Money | 每 30 分钟 | 链上聪明钱信号 |
| 每日报告 | 每天 00:00 UTC | 生成每日交易报告 |
| 余额监控 | 每 10 分钟 | 监控合约余额变化 |

### 4.4 通知机制

通过 Telegram 自动推送:
- ✅ 开仓/平仓通知
- ⚠️ 触发止损/止盈
- 🚨 风控警告（回撤接近限制）
- 📊 每日交易总结
- 📈 策略信号提醒

---

## 五、币安插件本地路径（已安装）

从 binance-skills-hub 安装了 14 个技能，位于：

```
/root/.openclaw/workspace/a/.agents/skills/
```

### 核心技能

| 技能 | 路径 | 用途 |
|------|------|------|
| **binance（核心）** | `/root/.openclaw/workspace/a/.agents/skills/binance/` | 币安全功能，含 futures-usds 合约交易 |
| binance/references/ | `/root/.openclaw/workspace/a/.agents/skills/binance/references/` | 26 个参考文档（auth.md、futures-usds.md、wallet.md 等） |
| **trading-signal** | `/root/.openclaw/workspace/a/.agents/skills/trading-signal/` | Smart Money 链上交易信号 |
| **crypto-market-rank** | `/root/.openclaw/workspace/a/.agents/skills/crypto-market-rank/` | 市场排名、热门币种、社交情绪 |

### 其他技能

| 技能 | 路径 |
|------|------|
| binance-agentic-wallet | `/root/.openclaw/workspace/a/.agents/skills/binance-agentic-wallet/` |
| binance-tokenized-securities-info | `/root/.openclaw/workspace/a/.agents/skills/binance-tokenized-securities-info/` |
| query-token-info | `/root/.openclaw/workspace/a/.agents/skills/query-token-info/` |
| query-token-audit | `/root/.openclaw/workspace/a/.agents/skills/query-token-audit/` |
| query-address-info | `/root/.openclaw/workspace/a/.agents/skills/query-address-info/` |
| meme-rush | `/root/.openclaw/workspace/a/.agents/skills/meme-rush/` |
| fiat | `/root/.openclaw/workspace/a/.agents/skills/fiat/` |
| p2p | `/root/.openclaw/workspace/a/.agents/skills/p2p/` |
| payment-assistant | `/root/.openclaw/workspace/a/.agents/skills/payment-assistant/` |
| square-post | `/root/.openclaw/workspace/a/.agents/skills/square-post/` |
| onchain-pay-open-api | `/root/.openclaw/workspace/a/.agents/skills/onchain-pay-open-api/` |

### 核心工具

- `binance-cli` 已安装：`/root/.npm-global/bin/binance-cli`
- 环境变量：`BINANCE_API_KEY`、`BINANCE_SECRET_KEY`、`BINANCE_API_ENV`（prod|testnet|demo）

---

## 六、V1 实施步骤

### Phase 0: 环境准备 ✅ 已完成
- [x] 确认 binance-cli 已安装
- [x] 确认 Binance Skills 已安装（14 个技能）
- [x] 创建工作空间目录结构（config/ data/ logs/ scripts/ state/）
- [ ] 配置币安 API Key（需要用户提供）
- [ ] 创建 binance-cli profile（testnet 或 demo 环境优先）
- [x] 测试 API 连通性（公开接口已验证通过）
- [x] 创建配置文件模板（strategies.json / risk.json / symbols.json）
- [x] 创建状态文件（daily_pnl.json / trade_history.json）

### Phase 1: 数据管道 ✅ 已完成
- [x] 编写数据采集脚本（fetch_data.sh）
- [x] K 线数据拉取和缓存（支持 1m/5m/15m/1h）
- [x] 实时价格轮询（标记价格）
- [x] 资金费率 / 持仓量 / 大户多空比监控

### Phase 2: 策略引擎 ✅ 已完成
- [x] 技术指标计算（MA, EMA, RSI, MACD, 布林带, 成交量均线）
- [x] 趋势跟踪策略实现（MA 金叉/死叉 + 放量确认）
- [x] 均值回归策略实现（RSI 超买超卖 + 布林带）
- [x] 资金费率策略框架（极端费率反向开仓）
- [x] 信号生成和记录（JSON 格式输出到 data/signals/）

### Phase 3: 风控系统 ✅ 已完成
- [x] 止损/止盈逻辑（百分比阈值检查）
- [x] 仓位管理（杠杆/最大持仓数检查）
- [x] 回撤监控（日亏损/总回撤）
- [x] 强制平仓机制（check_risk.sh 自动检测并标记）
- [x] 追踪止损支持

### Phase 4: 交易执行 ✅ 已完成
- [x] binance-cli 下单封装（trade.sh）
- [x] 订单管理（开仓/平仓/限价/止损/止盈/撤单）
- [x] 杠杆/保证金管理
- [x] 交易记录持久化（JSON + 日志双写）
- [x] 账户状态/持仓/挂单查询

### Phase 5: 联调测试 ✅ 框架完成
- [x] 主调度器（orchestrator.py）编排完整周期
- [x] 定时任务已配置（cron jobs：数据采集/策略/风控/日报）
- [ ] 使用 testnet/demo 环境测试（需 API Key）
- [ ] 策略回测（可选，后续扩展）
- [ ] 模拟交易验证（需 API Key）
- [ ] 风控规则验证（需 API Key）

### Phase 6: 实盘上线 ⏸ 等待 API Key
- [ ] 切换到 prod 环境
- [ ] 小资金试运行
- [ ] 逐步增加策略复杂度

---

## 七、安全注意事项

1. **API 权限**: 仅开启「合约交易」权限，**不开提现权限**
2. **IP 白名单**: 在币安后台绑定服务器 IP
3. **密钥管理**: API Key/Secret 存入环境变量，不写入文件
4. **确认机制**: 实盘交易前必须用户确认 `CONFIRM`
5. **日志审计**: 所有操作留痕，可追溯
6. **紧急停止**: 提供一键清仓+停止交易的能力

---

## 八、当前状态

### ✅ 已完成（Phase 0-5 代码框架）
| 模块 | 文件 | 状态 |
|------|------|------|
| 配置模板 | config/strategies.json, risk.json, symbols.json | ✅ |
| 连通性测试 | scripts/test_connectivity.sh | ✅ |
| 数据采集 | scripts/fetch_data.sh | ✅ |
| 策略引擎 | scripts/strategy_engine.py | ✅ |
| 风控系统 | scripts/check_risk.sh | ✅ |
| 交易执行 | scripts/trade.sh | ✅ |
| 调度器 | scripts/orchestrator.py | ✅ |
| 每日报告 | scripts/daily_report.py | ✅ |
| 状态文件 | state/daily_pnl.json, trade_history.json | ✅ |
| 定时任务 | Cron Jobs（已配置，需 API Key 启用） | ✅ |

### ⏸ 等待提供
1. **币安 API Key 和 Secret**（用于 U 本位合约）
2. **环境选择**: testnet（测试网） 或 prod（实盘）
3. **初始交易对**（默认 BTCUSDT + ETHUSDT）
4. **初始资金量和风险偏好**（默认保守配置）

### 🔜 下一步
- 提供 API Key → 启用 Cron Jobs → 开始实盘/模拟运行
- 提供 API Key → 运行 test_connectivity.sh 完整验证 → 小资金测试
