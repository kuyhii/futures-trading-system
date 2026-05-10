# 📈 自动量化合约交易系统

> 基于币安 U 本位合约的自动化量化交易框架，由 OpenClaw Agent 驱动。
> 版本: V2 — 多策略加权投票 + 动态品种池 + CZSC缠论 + Telegram通知

## ✨ 核心功能

- **4 种活跃策略**: 趋势跟踪、均值回归、突破、CZSC缠论
- **加权投票信号聚合**: 多策略同时分析，一致信号自动加成，冲突信号加权决断
- **策略隔离保护**: 单策略连续错误自动禁用，不影响其他策略
- **全闭环自动化**: 数据采集 → 策略计算 → 风控审核 → 自动下单 → 通知推送
- **动态交易品种池 V2**: 实盘+模拟盘双重验证，每日自动筛选。实盘成交额>1000万U ∪ 涨幅前20 ∪ 跌幅前20，与模拟盘取交集（模拟盘涨幅/跌幅取前35）
- **风控体系**: 止损/止盈/追踪止损、仓位管理、回撤限制、熔断机制、保证金检查
- **双级别分析**: 2m 主信号 + 15m 级别确认（CZSC缠论）
- **通知推送**: Telegram 实时推送开仓/平仓/风控警告/每日报告
- **Dry-Run 模式**: 默认模拟交易，虚拟成交完整模拟开平仓流程
- **回测引擎**: 历史数据回测，胜率/盈亏比/夏普比率统计

## ⚡ 当前系统参数

| 参数 | 值 |
|------|-----|
| K线周期 | 2分钟 |
| 杠杆倍数 | 20x（全策略统一） |
| 下单保证金 | 50 USDT（固定） |
| 风控巡检 | 15秒独立线程 |
| 信号置信度 | ≥0.7（最低阈值） |
| 交易模式 | DRY-RUN（testnet测试网） |

## 📁 项目结构

```
futures-trading-system/
├── core/                    # 核心引擎
│   ├── engine.py            # V2 主交易引擎（多策略+动态品种池）
│   ├── backtest.py          # 回测引擎
│   ├── notify.py            # Telegram 通知模块
│   └── strategies/          # 策略模块
│       ├── trend_follow.py  # 趋势跟踪
│       ├── mean_reversion.py# 均值回归
│       ├── breakout.py      # 突破策略
│       └── czsc_strategy.py # CZSC缠论策略
├── secrets/                 # 🔒 API 密钥（.gitignore 排除，永不提交）
│   └── api_keys.env         # 实盘+模拟盘 API Key 专用文件
├── scripts/
│   ├── update_symbols_pool.py  # 动态品种池筛选脚本 V2（双重验证）
│   ├── strategy_engine.py      # 独立策略计算
│   ├── daily_report.py         # 每日报告
│   └── orchestrator.py         # 调度器
├── config/
│   ├── strategies.json      # 策略配置（权重/优先级/聚合）
│   ├── risk.json            # 风控参数
│   └── symbols.json         # 动态品种池（每日自动更新）
├── data/                    # 运行时数据（.gitignore）
├── state/                   # 状态文件（.gitignore）
├── logs/                    # 日志目录（.gitignore）
└── .env                     # 环境变量（.gitignore，绝不提交）
```

## 🚀 快速开始

### 1. 配置环境

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入币安 API Key 和 Telegram 通知配置
```

### 2. 模拟交易（Dry-Run）

```bash
python3 -m core.engine
# 默认 dry-run 模式，虚拟成交，不会真实下单
```

### 3. 回测策略

```bash
python3 -m core.backtest
python3 -m core.backtest --symbol BTCUSDT --interval 2m --bars 1000 --capital 10000
python3 -m core.backtest --strategies trend_follow,czsc
```

### 4. 动态品种池

```bash
# 手动执行品种筛选
python3 scripts/update_symbols_pool.py

# 每日 UTC 00:00 自动执行（cron）
```

### 5. 实盘交易

```bash
# ⚠️ 谨慎！先确保风控参数正确，建议小资金测试
python3 -m core.engine --live
```

## 🧠 策略说明

| 策略 | 逻辑 | K线周期 | 权重 | 适用场景 |
|------|------|---------|------|---------|
| 趋势跟踪 | MA金叉/死叉 + MACD + 放量 | 2m | 1.0 | 趋势行情 |
| 均值回归 | RSI超买超卖 + 布林带 | 2m | 0.8 | 震荡行情 |
| 突破策略 | N周期高低点突破 + 成交量 | 2m | 0.9 | 突破行情 |
| CZSC缠论 | 分型/笔/中枢 + 多级别确认 | 2m+15m | 1.0 | 全场景 |

### 信号聚合规则
- 多策略同时发出同向信号 → 置信度加成
- 策略间信号冲突 → 加权投票决断
- 置信度低于 0.7 → 自动过滤
- 单策略连续3次错误 → 自动禁用

## 🛡️ 风控参数

| 规则 | 值 | 说明 |
|------|-----|------|
| 单笔止损 | -2% | 每笔亏损上限 |
| 单笔止盈 | +4% | 盈亏比 ≥ 1:2 |
| 最大杠杆 | 20x | 全局上限 |
| 下单保证金 | 50 USDT | 固定金额 |
| 最大持仓 | 3 个 | 同时持仓币种数 |
| 日亏损限制 | -5% | 当日亏损上限 |
| 总回撤限制 | -15% | 总资金回撤上限 |
| 追踪止损 | 1% | 从最高点回撤触发 |
| 风控巡检 | 15秒 | 独立线程，快于主循环 |

## 📊 Telegram 通知

系统自动推送以下事件到 Telegram：
- 📈 开仓通知（币种/方向/价格/策略）
- 📉 平仓通知（原因：止盈/止损/信号反转）
- ⚠️ 风控警告（接近止损/止盈线）
- 📊 每日汇总报告
- 🔧 系统状态

## ⚙️ 命令行参考

### 主引擎
```bash
python3 -m core.engine              # Dry-Run
python3 -m core.engine --live       # 实盘
python3 -m core.engine --status     # 查看状态
```

### 回测
```bash
python3 -m core.backtest
python3 -m core.backtest --symbol ETHUSDT --interval 2m --bars 1000
```

### 品种池
```bash
python3 scripts/update_symbols_pool.py   # 手动筛选
```

## 🔧 配置币安 API

### API 密钥管理

> 🔒 **所有 API 密钥统一存放在 `secrets/api_keys.env` 文件中**
> - 此文件已被 `.gitignore` 永久排除，**绝不提交到 Git**
> - 其他工具/脚本只通过读取此文件调用 API，不硬编码密钥
> - 修改前请备份：`cp secrets/api_keys.env secrets/api_keys.env.bak.日期`

```bash
# 编辑密钥文件
nano secrets/api_keys.env

# 填入以下信息：
# BINANCE_PROD_API_KEY=<实盘 API Key>
# BINANCE_PROD_SECRET_KEY=<实盘 Secret Key>
# BINANCE_TESTNET_API_KEY=<模拟盘 API Key>
# BINANCE_TESTNET_SECRET_KEY=<模拟盘 Secret Key>
# TELEGRAM_BOT_TOKEN=<Bot Token>
# TELEGRAM_CHAT_ID=<Chat ID>
```

### 获取 API Key

1. 登录 [币安](https://www.binance.com)
2. 进入 API 管理
3. 创建新 API Key
4. **仅开启「合约交易」权限**（❌ 不要开提现权限）
5. 绑定 IP 白名单（推荐）

### 测试网
- 测试网地址: https://testnet.binancefuture.com

### ⚠️ 当前阶段说明（测试系统）
> 当前为测试系统，使用 testnet 测试网环境。
> 品种筛选采用「实盘+模拟盘双重验证」：模拟盘未上线的币种即使实盘热门也不纳入。
> **后期切换实盘时**，需修改筛选逻辑，取消模拟盘验证要求。

## 🔒 安全注意事项

1. **API 权限**: 仅开启合约交易，绝不开启提现
2. **IP 白名单**: 务必绑定服务器 IP
3. **密钥管理**: 所有密钥存入 `secrets/api_keys.env`，**.gitignore 已排除，绝不提交到 Git**
4. **Dry-Run 优先**: 先模拟验证，再小资金实盘
5. **紧急停止**: `Ctrl+C` 停止引擎
6. **日志审计**: 所有操作记录在 `logs/` 目录
7. **每次 git push 前**: 必须确认 .gitignore 配置完整，隐私文件不会泄露

## 📈 更新日志

### V2（2026-05-10）
- 🔧 多策略加权投票信号聚合系统
- 🔧 策略隔离保护（错误自动禁用）
- 🔧 CZSC缠论策略集成（分型/笔/中枢）
- 🔧 动态交易品种池（每日自动筛选）
- 🔧 Telegram 通知完整接入
- 🔧 DRY-RUN 虚拟成交完整模拟
- 🔧 风控独立线程（15秒巡检）
- 🔧 参数统一：2m K线、20x杠杆、50U固定保证金
- 🔧 全面汉化（日志/通知/输出）

## 🤝 贡献

本项目由 OpenClaw Agent 开发和维护。

## 📄 许可

仅供学习研究，不构成投资建议。交易有风险，入市需谨慎。
