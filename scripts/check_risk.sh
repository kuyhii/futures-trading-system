#!/bin/bash
# check_risk.sh - 风控检查脚本（Phase 3）
# 用途: 检查当前持仓风险，判断是否触发止损/止盈/回撤限制
# 使用方式:
#   bash scripts/check_risk.sh          # 检查所有持仓
#   bash scripts/check_risk.sh BTCUSDT  # 检查指定币种

set -e

WORKSPACE="/root/.openclaw/workspace-taizi/futures-trading-system"

# ── 加载环境配置 ──
ENV_FILE="$WORKSPACE/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    while IFS= read -r line; do
        case "$line" in ''|\#*) continue ;; *=*) eval "export $line" ;; esac
    done < "$ENV_FILE"
    set +a
fi
BINANCE_API_ENV="${BINANCE_API_ENV:-testnet}"

# 实盘环境切换
if [ "$BINANCE_API_ENV" = "prod" ]; then
    [ -n "${BINANCE_PROD_API_KEY:-}" ] && export BINANCE_API_KEY="$BINANCE_PROD_API_KEY"
    [ -n "${BINANCE_PROD_SECRET_KEY:-}" ] && export BINANCE_SECRET_KEY="$BINANCE_PROD_SECRET_KEY"
fi

if [ -z "${BINANCE_API_KEY:-}" ]; then
    echo "⚠️  [risk] API Key 未配置，风控检查受限"
fi

CONFIG_RISK="$WORKSPACE/config/risk.json"
STATE_DIR="$WORKSPACE/state"
LOG="$WORKSPACE/logs/risk.log"
TIMESTAMP=$(date -u '+%Y-%m-%d %H:%M:%S')

log() {
    echo "[$TIMESTAMP] [risk] $1" | tee -a "$LOG"
}

# 读取风控配置
MAX_LEVERAGE=$(grep -o '"max_leverage":[0-9]*' "$CONFIG_RISK" | cut -d: -f2)
STOP_LOSS_PCT=$(grep -o '"stop_loss_pct":[0-9]*' "$CONFIG_RISK" | cut -d: -f2)
TAKE_PROFIT_PCT=$(grep -o '"take_profit_pct":[0-9]*' "$CONFIG_RISK" | cut -d: -f2)
MAX_POSITIONS=$(grep -o '"max_positions":[0-9]*' "$CONFIG_RISK" | cut -d: -f2)
DAILY_LOSS_PCT=$(grep -o '"daily_loss_limit_pct":[0-9]*' "$CONFIG_RISK" | cut -d: -f2)
MAX_DRAWDOWN_PCT=$(grep -o '"max_drawdown_pct":[0-9]*' "$CONFIG_RISK" | cut -d: -f2)
TRAILING_STOP=$(grep -o '"trailing_stop":true\|false' "$CONFIG_RISK" | cut -d: -f2)
TRAILING_DIST=$(grep -o '"trailing_distance_pct":[0-9]*' "$CONFIG_RISK" | cut -d: -f2)

log "========== 风控巡检 =========="
log "配置: 杠杆≤${MAX_LEVERAGE}x | 止损-${STOP_LOSS_PCT}% | 止盈+${TAKE_PROFIT_PCT}% | 最大持仓${MAX_POSITIONS}个 | 日损-${DAILY_LOSS_PCT}% | 回撤-${MAX_DRAWDOWN_PCT}%"

SYMBOL="${1:-ALL}"

check_position_risk() {
    local sym="$1"
    log "--- 检查 $sym ---"

    # 获取当前标记价格
    local price_data=$(binance-cli futures-usds mark-price --symbol "$sym" 2>&1)
    local current_price=$(echo "$price_data" | grep -oP '"markPrice"\s*:\s*"\K[^"]+')

    if [ -z "$current_price" ]; then
        log "⚠️  无法获取 $sym 价格，跳过"
        return
    fi

    log "  当前价格: $current_price"

    # 获取持仓信息（需要 API Key）
    local pos_data=$(binance-cli futures-usds position-information-v3 --symbol "$sym" 2>&1)

    # 检查是否返回错误（无 API Key 或未持仓）
    if echo "$pos_data" | grep -qi "error\|required\|invalid"; then
        log "  ⚠️  无法获取持仓信息（需要 API Key 或未持仓）"
        return
    fi

    # 解析持仓数据
    local position_amt=$(echo "$pos_data" | grep -oP '"positionAmt"\s*:\s*"\K[^"]+' || echo "0")
    local entry_price=$(echo "$pos_data" | grep -oP '"entryPrice"\s*:\s*"\K[^"]+' || echo "0")
    local unrealized_pnl=$(echo "$pos_data" | grep -oP '"unRealizedProfit"\s*:\s*"\K[^"]+' || echo "0")
    local leverage=$(echo "$pos_data" | grep -oP '"leverage"\s*:\s*\K[0-9]+' || echo "0")

    # 未持仓
    if [ "$position_amt" = "0" ] || [ "$position_amt" = "0.000" ]; then
        log "  ℹ️  无持仓"
        return
    fi

    log "  持仓量: $position_amt | 开仓价: $entry_price | 未实现盈亏: $unrealized_pnl | 杠杆: ${leverage}x"

    # 杠杆检查
    if [ "$leverage" -gt "$MAX_LEVERAGE" ] 2>/dev/null; then
        log "  🚨 杠杆超标: ${leverage}x > ${MAX_LEVERAGE}x"
    fi

    # 计算盈亏百分比
    if [ "$entry_price" != "0" ] && [ -n "$entry_price" ]; then
        local direction=1
        if [ "$(echo "$position_amt < 0" | bc -l 2>/dev/null || echo 0)" = "1" ]; then
            direction=-1
        fi

        local pnl_pct=$(echo "scale=4; ($current_price - $entry_price) / $entry_price * 100 * $direction" | bc -l 2>/dev/null || echo "0")
        log "  浮动盈亏: ${pnl_pct}%"

        # 止损检查
        local stop_threshold=$(echo "scale=4; $pnl_pct < -$STOP_LOSS_PCT" | bc -l 2>/dev/null || echo 0)
        if [ "$stop_threshold" = "1" ]; then
            log "  🚨🚨🚨 触发止损! 亏损 ${pnl_pct}% 超过限制 ${STOP_LOSS_PCT}%"
        fi

        # 止盈检查
        local tp_threshold=$(echo "scale=4; $pnl_pct > $TAKE_PROFIT_PCT" | bc -l 2>/dev/null || echo 0)
        if [ "$tp_threshold" = "1" ]; then
            log "  ✅ 触发止盈! 盈利 ${pnl_pct}% 超过目标 ${TAKE_PROFIT_PCT}%"
        fi
    fi
}

# 检查日亏损（读取 state/daily_pnl.json）
check_daily_loss() {
    local daily_file="$STATE_DIR/daily_pnl.json"
    if [ -f "$daily_file" ]; then
        local daily_pnl=$(grep -oP '"total_pnl"\s*:\s*\K-?[0-9.]+' "$daily_file" || echo "0")
        log "当日累计盈亏: $daily_pnl"
    else
        log "当日盈亏记录不存在"
    fi
}

# 主流程
if [ "$SYMBOL" = "ALL" ]; then
    # 从配置读取监控币种
    while IFS= read -r sym; do
        sym=$(echo "$sym" | tr -d '," ')
        [ -z "$sym" ] || [ "$sym" = "[" ] || [ "$sym" = "]" ] && continue
        echo "$sym" | grep -q "USDT" && check_position_risk "$sym"
    done < "$WORKSPACE/config/symbols.json"
else
    check_position_risk "$SYMBOL"
fi

check_daily_loss

log "========== 风控巡检完成 =========="
