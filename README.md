# 📈 自动量化合约交易系统

> 基于币安 U 本位合约的自动化量化交易框架，由 OpenClaw Agent 驱动。

## ✨ 核心功能

- **5 种交易策略**: 趋势跟踪、均值回归、突破、资金费率套利、多空比反转
- **全闭环自动化**: 数据采集 → 策略计算 → 风控审核 → 自动下单
- **风控体系**: 止损/止盈/追踪止损、仓位管理、回撤限制、熔断机制
- **回测引擎**: 历史数据回测，胜率/盈亏比/夏普比率统计
- **多时间框架**: 15m 主信号 + 1h 确认
- **通知推送**: Telegram 实时推送开仓/平仓/风控警告
- **Dry-Run 模式**: 默认模拟交易，安全验证后再切实盘

## 📁 项目结构

```
futures-trading-system/
├── core/                    # 核心引擎（新版）
│   ├── engine.py            # 主交易引擎（全闭环）
│   ├── backtest.py          # 回测引擎
│   └── notify.py            # Telegram 通知模块
├── scripts/                 # 脚本工具（兼容旧版）
│   ├── fetch_data.sh        # 数据采集
│   ├── strategy_engine.py   # 独立策略计算
│   ├── check_risk.sh        # 风控检查
│   ├── trade.sh             # 交易执行
│   ├── orchestrator.py      # 旧版调度器
│   └── daily_report.py      # 每日报告
├── config/
│   ├── strategies.json      # 策略配置
│   ├── risk.json            # 风控参数
│   └── symbols.json         # 监控币种列表
├── skills/                  # Binance 官方 API 参考
│   ├── binance/             # 完整 API 文档（24个模块）
│   ├── binance-agentic-wallet/
│   └── binance-tokenized-securities-info/
├── data/                    # 运行时数据（.gitignore）
│   ├── klines/              # K 线缓存
│   ├── signals/             # 策略信号
│   └── reports/             # 回测/日报
├── state/                   # 状态文件
│   ├── trade_history.json   # 交易记录
│   ├── daily_pnl.json       # 每日盈亏
│   └── engine_state.json    # 引擎状态
└── logs/                    # 日志目录
```

## 🚀 快速开始

### 1. 配置环境

```bash
cp .env.example .env
# 编辑 .env，填入币安 API Key
```

### 2. 测试连通性

```bash
bash scripts/test_connectivity.sh
```

### 3. 模拟交易（Dry-Run）

```bash
python3 -m core.engine
# 默认 dry-run 模式，不会真实下单
```

### 4. 回测策略

```bash
# 默认回测 BTCUSDT 15m
python3 -m core.backtest

# 指定参数
python3 -m core.backtest --symbol ETHUSDT --interval 1h --bars 1000 --capital 10000

# 指定策略
python3 -m core.backtest --strategies trend_follow,breakout
```

### 5. 实盘交易

```bash
# ⚠️ 谨慎！先确保风控参数正确
python3 -m core.engine --live
```

## ⚙️ 策略说明

| 策略 | 逻辑 | 适用场景 | 推荐杠杆 |
|------|------|---------|---------|
| 趋势跟踪 | MA 金叉/死叉 + MACD 确认 + 放量 | 趋势行情 | 3x |
| 均值回归 | RSI 超买超卖 + 布林带触及 | 震荡行情 | 3x |
| 突破策略 | N 日高低点突破 + 成交量确认 | 突破行情 | 3x |
| 资金费率 | 极端费率反向开仓 | 极端情绪 | 2x |
| 多空比反转 | 大户多空比极端值 | 情绪反转 | 2x |

## 🛡️ 风控参数

| 规则 | 默认值 | 说明 |
|------|--------|------|
| 单笔止损 | -2% | 每笔亏损上限 |
| 单笔止盈 | +4% | 盈亏比 ≥ 1:2 |
| 最大杠杆 | 5x | 全局上限 |
| 单笔仓位 | 10% | 占总资金比例 |
| 最大持仓 | 3 个 | 同时持仓币种数 |
| 日亏损限制 | -5% | 当日亏损上限 |
| 总回撤限制 | -15% | 总资金回撤上限 |
| 追踪止损 | 1% | 从最高点回撤 1% 触发 |

## 📊 命令行参考

### 主引擎

```bash
python3 -m core.engine                    # Dry-Run 模拟
python3 -m core.engine --live             # 实盘模式
python3 -m core.engine --status           # 查看状态
python3 -m core.engine --interval 30      # 30秒/周期
python3 -m core.engine --cooldown 600     # 10分钟信号冷却
```

### 回测

```bash
python3 -m core.backtest                                    # 默认
python3 -m core.backtest --symbol BTCUSDT --interval 15m    # 指定币种/周期
python3 -m core.backtest --bars 1000 --capital 5000         # 数据量/资金
python3 -m core.backtest --leverage 3 --commission 0.0004   # 杠杆/手续费
python3 -m core.backtest --strategies trend_follow          # 指定策略
```

### 旧版脚本（兼容）

```bash
bash scripts/test_connectivity.sh              # 连通性测试
bash scripts/fetch_data.sh all BTCUSDT         # 数据采集
python3 scripts/strategy_engine.py             # 策略计算
bash scripts/check_risk.sh                     # 风控巡检
bash scripts/trade.sh status                   # 账户状态
python3 scripts/orchestrator.py full           # 完整周期
python3 scripts/daily_report.py                # 每日报告
```

## 🔧 配置币安 API

1. 登录 [币安](https://www.binance.com)
2. 进入 API 管理
3. 创建新 API Key
4. **仅开启「合约交易」权限**（❌ 不要开提现权限）
5. 绑定 IP 白名单（推荐）
6. 填入 `.env` 文件

### 测试网

币安提供测试网环境，无需真实资金：
- 测试网地址: https://testnet.binancefuture.com
- 测试网 Key 获取: 测试网后台注册

## ⚠️ 安全注意事项

1. **API 权限**: 仅开启合约交易，绝不开启提现
2. **IP 白名单**: 务必绑定服务器 IP
3. **密钥管理**: 存入 `.env`，不提交到 Git
4. **Dry-Run 优先**: 先模拟验证，再小资金实盘
5. **紧急停止**: `Ctrl+C` 停止引擎，`bash scripts/trade.sh cancel-all` 取消所有挂单
6. **日志审计**: 所有操作记录在 `logs/` 目录

## 📈 性能指标（回测参考）

> 以下为示例数据，实际表现取决于市场条件和参数调优

```
BTCUSDT 15m | 500 根K线 | 初始资金 10000 USDT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
总收益率:     +12.34%
最大回撤:     -3.45%
交易次数:     28
胜率:         57.1% (16胜 / 12负)
平均盈利:     +45.2 USDT
平均亏损:     -23.1 USDT
盈亏比:       2.15
夏普比率:     1.82
```

## 🤝 贡献

本项目由 OpenClaw Agent 开发和维护。

## 📄 许可

仅供学习研究，不构成投资建议。交易有风险，入市需谨慎。
