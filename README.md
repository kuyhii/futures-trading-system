# 📈 自动量化合约交易系统 V4.1

> 基于币安 U 本位合约的模块化量化交易框架，由 OpenClaw Agent 驱动。
> **版本: V4.1 — positionSide 双向持仓修复 + 通知推送修复**
> **运行状态: 实盘运行中 | 严格共识: ≥ 2 策略一致才开仓**

## ✨ 核心功能

- **3 大活跃策略**: 趋势跟踪、均值回归、突破
- **13 种技术指标**: SMA、EMA、RSI、MACD、布林带、ATR、VWAP、**ADX**（趋势强度）、**KD**（随机指标）、**OBV**（能量潮）、**CCI**（顺势指标）、**EMV**（简易波动）、**Dual Thrust**（通道突破）
- **严格共识机制**: 同方向 ≥ 2 个策略同意才开仓（可配置），冲突信号按加权分决断
- **策略隔离保护**: 单策略连续错误自动禁用，不影响其他策略
- **全闭环自动化**: 数据采集 → 策略计算 → 风控审核 → 自动下单 → 通知推送
- **动态交易品种池**: 实盘+模拟盘双重验证，每日自动筛选，上限 35 个品种（排除BNB、贵金属）
- **风控体系**: 止损/止盈/追踪止损、**ATR 海龟仓位管理**（高波动自动减仓）、回撤限制、熔断机制
- **K 线本地存储**: 1m 采集 → 2m 合成（按 2m 时间边界对齐），保留 24 小时
- **通知推送**: Telegram 实时推送开仓/平仓/风控警告/每日报告
- **回测引擎**: 历史数据回测（可选）

## 📁 模块化架构

```
futures-trading-system/
├── core/
│   ├── __init__.py          # 包初始化
│   ├── engine.py            # 主交易引擎（编排器）
│   ├── binance_client.py    # 币安 U 本位合约 API 封装
│   ├── models.py            # 数据模型（信号/持仓/账户）
│   ├── indicators.py        # 技术指标（SMA/EMA/RSI/MACD/布林带等）
│   ├── strategy_engine.py   # 策略引擎（3 策略）
│   ├── risk_engine.py       # 风控引擎（止损/止盈/仓位计算）
│   ├── order_manager.py     # 订单管理（开仓/平仓/止损止盈设置）
│   ├── notify.py            # Telegram 通知模块
│   └── kline_manager.py     # K 线管理器（1m 采集 → 2m 合成）
├── config/
│   ├── strategies.json      # 策略配置（权重/阈值）
│   ├── risk.json            # 风控参数
│   └── symbols.json         # 动态品种池（.gitignore 排除）
├── scripts/
│   └── update_symbols_pool.py  # 品种池自动筛选脚本
├── secrets/                 # 🔒 API 密钥（.gitignore 排除）
├── data/                    # 运行时数据（.gitignore 排除）
├── state/                   # 状态文件（.gitignore 排除）
├── logs/                    # 日志（.gitignore 排除）
└── .env                     # 环境配置（.gitignore 排除）
```

## ⚡ 系统参数

| 参数 | 值 |
|------|-----|
| K线周期 | 2分钟 |
| 杠杆倍数 | 20x（全策略统一） |
| 下单保证金 | ATR 海龟仓位管理（高波动自动减仓，上限 4%） |
| 风控巡检 | 15秒独立线程 |
| 信号置信度 | ≥0.7（最低阈值） |
| 品种池上限 | 35 个（按成交额排序） |
| 交易模式 | 实盘（prod） |

## 🧠 策略说明

| 策略 | 核心逻辑 | 新增指标 | 权重 | 适用场景 |
|------|---------|---------|------|---------|
| 趋势跟踪 | EMA金叉/死叉 + MACD + 放量 + **ADX趋势强度** | EMA(替代SMA)、ADX | 1.0 | 趋势行情 |
| 均值回归 | RSI超买超卖 + 布林带 + **KD双重确认** | KD(Stochastic) | 0.8 | 震荡行情 |
| 突破策略 | N周期高低点突破 + 量确认 + **ADX趋势 + OBV资金流** | ADX、OBV | 0.9 | 突破行情 |

### 信号聚合规则
- **严格共识**: 同方向策略数 ≥ `min_agreement_count`（默认2）才允许开仓
- **冲突检测**: 多空信号冲突时按加权分决断（confidence × strategy_weight）
- **冲突后共识检查**: 冲突解决后仍检查胜方策略数，不足则过滤
- 一致性加成: 多策略同向 → 置信度 +0.05/策略（最多+0.15）
- 置信度低于 0.7 → 自动过滤（各策略可独立设置最低值）
- 单策略连续3次错误 → 自动禁用

## 🛡️ 风控参数

| 规则 | 值 | 说明 |
|------|-----|------|
| 单笔止损 | -2% | 每笔亏损上限 |
| 单笔止盈 | +4% | 盈亏比 ≥ 1:2 |
| 最大杠杆 | 20x | 全局上限 |
| ATR 风险比例 | 1% | 海龟仓位: 每笔风险 = 账户 × 1% |
| ATR 回看周期 | 14 | ATR 计算窗口 |
| 下单保证金上限 | 账户总金额的 4% | ATR 极低时的上限 |
| 最大持仓 | 20 个 | 同时持仓币种数 |
| 日亏损限制 | -5% | 当日亏损上限 |
| 总回撤限制 | -15% | 总资金回撤上限 |
| 追踪止损 | 1% | 从最高点回撤触发 |
| 风控巡检 | 15秒 | 独立线程 |

## 🚀 快速开始

### 1. 配置环境
```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入币安 API Key 和 Telegram 通知配置
```

### 2. 启动交易引擎
```bash
python3 -m core.engine              # 测试网模式
python3 -m core.engine --env prod   # 实盘模式（谨慎！）
python3 -m core.engine --status     # 查看状态
```

### 3. 品种池管理
```bash
python3 scripts/update_symbols_pool.py   # 手动筛选
# 每日 UTC 00:00 自动执行
```

## 🔒 安全注意事项

1. **API 权限**: 仅开启合约交易，绝不开启提现
2. **IP 白名单**: 务必绑定服务器 IP
3. **密钥管理**: `secrets/api_keys.env`，.gitignore 已排除
4. **Dry-Run 优先**: 先测试网验证，再小资金实盘
5. **紧急停止**: `Ctrl+C` 停止引擎
6. **推送安全**: `.env`, `secrets/`, `data/`, `state/`, `logs/`, `config/symbols.json` 绝不提交

## 📈 更新日志

### V4.3（2026-05-15）— 止损止盈 API 修复 + 杠杆优雅降级
- 🔴 **止损止盈 API 错误** — `STOP_MARKET/TAKE_PROFIT_MARKET` 必须走 `/fapi/v1/algoOrder`（不是 `/fapi/v1/order`），参数名 `triggerprice`（小写）
- 🔴 **杠杆无效 -4028** — 部分品种（如 SKYAIUSDT）不支持 20x，新增 `_set_leverage_safe()` 自动降级 20x→10x→5x
- 🟡 **通知 dry_run** — 开仓通知补上 `dry_run=False`，实盘显示 🟢 实盘 而非 🔵 模拟
- 🟡 **closePosition 格式** — Algo Order API 要求 `closePosition="true"`（字符串）

### V4.2（2026-05-15）— 策略回滚：恢复 V3 原始突破策略
- 🔄 **回滚 ea-python 策略集成**：移除 CCI、EMV、Dual Thrust 通道、ATR 海龟仓位
- 📉 **突破策略恢复**：简单 N 周期高低点突破 + ADX 趋势过滤 + OBV 量能确认
- 💰 **仓位计算恢复**：账户总金额的 4%（不再使用 ATR 海龟仓位）
- 🧹 **删除指标**：`cci()`, `emv()`, `dual_thrust_range()`
- 📋 **配置清理**：移除 `cci_period`, `emv_period`, `dual_thrust_k1/k2`, `atr_risk_pct`

### V4.1（2026-05-15）— positionSide 双向持仓修复 + 通知推送修复
- 🐛 **positionSide 缺失**: 币安合约要求 `positionSide` 参数（双向持仓模式），开仓/平仓时补充 `LONG/SHORT` 字段
- 🐛 **通知推送 KeyError**: `order_manager.py` 中 `result` 字典缺少 `fill_price` 字段，导致 Telegram 通知发送失败
- ⚡ **止损止盈 positionSide**: `_place_sl_tp` 中 `pos_side` 从固定 `BOTH` 改为动态 `LONG/SHORT`
- 🧪 修复后重启实盘，通知推送正常工作

### V4（2026-05-14）— ea-python 精华策略集成
> 来源: [zhy0313/ea-python](https://github.com/zhy0313/ea-python) 精华提取，适配 2m 超短线

**新增指标（3 个）**
- **CCI 顺势指标**: CCI > +100 多头强势，CCI < -100 空头强势
- **EMV 简易波动**: 量价复合指标，EMV>0 上涨轻松，EMV<0 下跌轻松
- **Dual Thrust 通道**: `range = max(HH-LC, HC-LL)` 比简单高低点更精准

**ATR 海龟仓位管理**
- 替代固定 4% 保证金，实现风险标准化
- 海龟规则: `unit = (账户 × atr_risk_pct%) / ATR`
- 高波动品种自动减仓（ATR=50 → 1%），低波动正常开仓（上限 4%）
- ATR=0 或 None 时自动 fallback 到 4%

**突破策略三重确认**
- ADX（趋势强度 ≥ 25）+ CCI（>100/<-100）+ EMV（>0/<0）
- ≥ 2 项确认才放行，过滤假突破
- 置信度: 3项=0.88, 2项=0.72, 1项=0.58
- OBV 被 EMV 替代（EMV 信息量更大）

**配置变更**
- `risk.json`: 新增 `atr_risk_pct: 1`, `atr_period: 14`
- `strategies.json`: 新增 `cci_period: 14`, `emv_period: 14`, `dual_thrust_k1/k2: 0.5`
- `breakout.min_confidence`: 0.56 → 0.58

**排除策略**: 网格交易（不适用于合约）

### V3.4（2026-05-14）— 品种池扩容至35 + 排除贵金属
- **品种池上限**: 30 → 35 个品种
- **排除品种**: BNB、XAU（黄金）、XAG（白银）永不加入品种池
- **update_symbols_pool.py**: `is_stablecoin` → `is_excluded`，统一使用 `EXCLUDE_SYMBOLS` 集合管理排除规则

### V3.3（2026-05-14）— 开仓保证金改为账户总金额的4%
- **动态保证金**: 固定 50U/100U → 账户总金额的 4%（risk.json `position_margin_pct`）
- **风控引擎更新**: `calc_position_size` 和 `can_open_position` 改用动态比例
- **移除反转override**: 不再区分普通/反转信号保证金，统一使用 4% 规则
- **示例**: 账户 1000U → 每笔 40U；账户 5000U → 每笔 200U

### V3.2+（2026-05-14）— 价格精度统一 + 持续运行
- **价格显示精度统一**: 按价格区间自动调整小数位数（高价2位、低价4-6位）
- **持续运行**: 测试网已运行 50+ 周期，3 策略全部正常，无错误
- **累计交易记录**: 真实交易持续增加中

### V3.2（2026-05-14）— 指标扩展 + 共识逻辑修复
- **新增 4 种技术指标**:
  - `ADX`（趋势强度）: 过滤震荡期假突破，趋势跟踪策略使用
  - `Stochastic/KD`（随机指标）: 均值回归策略 RSI + KD 双重超买超卖确认
  - `OBV`（能量潮）: 突破策略量确认，资金流向验证
  - `EMA`（指数均线）: 替代 SMA 做趋势判断，对近期价格更敏感
- **突破策略修复**: `highest/lowest` 排除当前蜡烛，否则突破瞬间永远无法触发
- **共识逻辑修复**: `min_agreement_count` 改为检查同方向策略数（而非总信号数），冲突1v1场景不再误放行
- **风控增强**: 反转信号保证金加倍（100U），加大反转力度
- **参数优化**: `min_agreement_count: 1→2`、`breakout.min_confidence: 0.5→0.56`
- **品种池**: BNB 手动排除

### V3.1（2026-05-12）— 关键 Bug 修复
- 🐛 **引擎语法错误**: 修复 `_handle_sell_signal` 缺少右括号导致引擎无法启动
- 🛡️ **止盈损设置修复**: 测试网要求 `STOP_MARKET/TAKE_PROFIT_MARKET` 走 Algo Order API（`/fapi/v1/algoOrder`），参数修正为 `algotype=CONDITIONAL` + `triggerprice`（小写）
- 🔥 **熔断异常修复**: API 熔断时不再抛 `RuntimeError`，改为返回错误 dict，避免主循环崩溃
- 🛡️ **账户刷新安全**: `refresh_account` 增加 API 错误返回值校验，防止 `'str' object has no attribute 'get'` 崩溃
- ⚡ **重复调仓修复**: `open_position` 移除重复的 `adjust_quantity` 调用

### V3（2026-05-11）— 模块化重构
- 🔧 **全系统模块化**: 拆分为 8 个独立模块（binance_client/models/indicators/strategy_engine/risk_engine/order_manager/notify/kline_manager）
- 🗑️ **删除缠论策略**: 移除 CZSC 缠论模块及其依赖
- 📊 **保留3大策略**: 趋势跟踪 + 均值回归 + 突破
- 🎯 **2m 合成对齐修复**: 按 2m 时间边界分组合成，不再简单配对
- 🛡️ **止损止盈修复**: Algo Order API 添加 signed=True，错误判断逻辑修复
- ⚡ **线程安全**: account/kline_cache/signal_time 共享数据加锁
- 📩 **通知异步化**: 队列+独立线程发送，不再阻塞主循环
- 🔥 **熔断递增**: 冷却时间 60s→120s→...→最大15分钟
- 📊 **K线保留24h**: 缠论策略移除后仍保留更长历史
- 🏗️ **品种池上限30**: 按成交额排序截取，确保60秒内完成分析
- 📝 **README 全面更新**: 新架构说明 + 模块化目录结构

### V2.1（2026-05-11）
- 🚀 品种池上限30 + 置信度0.7 + 线程安全 + 通知异步化 + 熔断递增

### V2（2026-05-10）
- 🔧 多策略加权投票 + CZSC缠论 + 动态品种池 + Telegram通知

## 📄 许可

仅供学习研究，不构成投资建议。交易有风险，入市需谨慎。
