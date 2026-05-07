#!/bin/bash
# trade.sh - 交易执行脚本（Phase 4）
# 用途: 封装 binance-cli 下单操作
# 使用方式:
#   bash scripts/trade.sh open BTCUSDT long 0.001       # 开多
#   bash scripts/trade.sh open BTCUSDT short 0.001      # 开空
#   bash scripts/trade.sh close BTCUSDT                  # 平仓
#   bash scripts/trade.sh stop BTCUSDT 41000 long        # 设置止损
#   bash scripts/trade.sh status                         # 查询账户状态
#   bash scripts/trade.sh positions                      # 查询持仓

set -e

WORKSPACE="/root/projects/futures-trading-system"

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

CONFIG_RISK="$WORKSPACE/config/risk.json"
STATE_DIR="$WORKSPACE/state"
LOG="$WORKSPACE/logs/trades.log"
TIMESTAMP=$(date -u '+%Y-%m-%d %H:%M:%S')

log() {
    echo "[$TIMESTAMP] [trade] $1" | tee -a "$LOG"
}

ACTION="${1:-help}"
SYMBOL="${2:-}"
SIDE="${3:-}"
QTY="${4:-}"
PRICE="${5:-}"

usage() {
    echo "用法: $0 <action> [symbol] [side] [quantity] [price]"
    echo ""
    echo "Actions:"
    echo "  open   <symbol> <long|short> <qty>           - 市价开仓"
    echo "  limit  <symbol> <long|short> <qty> <price>   - 限价开仓"
    echo "  close  <symbol>                              - 市价平仓"
    echo "  stop   <symbol> <price> <long|short>         - 设置止损"
    echo "  tp     <symbol> <price> <long|short>         - 设置止盈"
    echo "  status                                       - 查询账户状态"
    echo "  positions                                    - 查询当前持仓"
    echo "  orders                                       - 查询当前挂单"
    echo "  history [symbol]                             - 查询历史成交"
    echo "  leverage <symbol> <leverage>                 - 设置杠杆"
    echo "  cancel-all <symbol>                          - 取消所有挂单"
    echo ""
    echo "示例:"
    echo "  $0 open BTCUSDT long 0.001"
    echo "  $0 limit BTCUSDT short 0.01 85000"
    echo "  $0 close BTCUSDT"
    echo "  $0 status"
}

# 记录交易到历史
record_trade() {
    local action="$1" symbol="$2" side="$3" qty="$4" price="$5" result="$6"
    local history_file="$STATE_DIR/trade_history.json"

    # 如果文件不存在，创建初始结构
    if [ ! -f "$history_file" ]; then
        echo '{"trades":[]}' > "$history_file"
    fi

    # 追加交易记录（简易实现）
    local trade_entry="{\"time\":\"$TIMESTAMP\",\"action\":\"$action\",\"symbol\":\"$symbol\",\"side\":\"$side\",\"quantity\":\"$qty\",\"price\":\"$price\",\"result\":\"$result\"}"

    # 使用 python 追加到 JSON（如果有 python 的话）
    if command -v python3 &>/dev/null; then
        python3 -c "
import json
with open('$history_file', 'r') as f:
    data = json.load(f)
data['trades'].append($trade_entry)
with open('$history_file', 'w') as f:
    json.dump(data, f, indent=2)
" 2>/dev/null || echo "$trade_entry" >> "${history_file}.log"
    else
        echo "$trade_entry" >> "${history_file}.log"
    fi

    log "📝 交易已记录: $action $symbol $side $qty @$price → $result"
}

cmd_open() {
    local sym="$1" side="$2" qty="$3"
    local binance_side=""
    [ "$side" = "long" ] && binance_side="BUY" || binance_side="SELL"

    log "📤 开${side}仓: $sym $qty"
    local result=$(binance-cli futures-usds new-order \
        --symbol "$sym" \
        --side "$binance_side" \
        --type MARKET \
        --quantity "$qty" 2>&1)

    if echo "$result" | grep -qi "error"; then
        log "❌ 开仓失败: $result"
        record_trade "open" "$sym" "$side" "$qty" "MARKET" "FAILED: $result"
    else
        local fill_price=$(echo "$result" | grep -oP '"avgPrice"\s*:\s*"\K[^"]+' || echo "$result" | grep -oP '"price"\s*:\s*"\K[^"]+' || "N/A")
        log "✅ 开仓成功，成交价: $fill_price"
        record_trade "open" "$sym" "$side" "$qty" "MARKET" "OK @$fill_price"
    fi
    echo "$result"
}

cmd_limit() {
    local sym="$1" side="$2" qty="$3" price="$4"
    local binance_side=""
    [ "$side" = "long" ] && binance_side="BUY" || binance_side="SELL"

    log "📤 限价开${side}仓: $sym $qty @ $price"
    local result=$(binance-cli futures-usds new-order \
        --symbol "$sym" \
        --side "$binance_side" \
        --type LIMIT \
        --price "$price" \
        --quantity "$qty" \
        --timeInForce GTC 2>&1)

    if echo "$result" | grep -qi "error"; then
        log "❌ 限价开仓失败: $result"
        record_trade "limit" "$sym" "$side" "$qty" "$price" "FAILED"
    else
        log "✅ 限价挂单成功"
        record_trade "limit" "$sym" "$side" "$qty" "$price" "OK"
    fi
    echo "$result"
}

cmd_close() {
    local sym="$1"
    log "📤 平仓: $sym"

    # 先获取持仓方向和数量
    local pos_data=$(binance-cli futures-usds position-information-v3 --symbol "$sym" 2>&1)
    local pos_amt=$(echo "$pos_data" | grep -oP '"positionAmt"\s*:\s*"\K[^"]+' || echo "0")

    if [ "$pos_amt" = "0" ] || [ "$pos_amt" = "0.000" ]; then
        log "ℹ️  $sym 无持仓，无需平仓"
        return
    fi

    # 确定平仓方向
    local close_side=""
    if [ "$(echo "$pos_amt > 0" | bc -l 2>/dev/null || echo 0)" = "1" ]; then
        close_side="SELL"  # 平多
    else
        close_side="BUY"   # 平空
        pos_amt=$(echo "$pos_amt" | tr -d '-')
    fi

    local result=$(binance-cli futures-usds new-order \
        --symbol "$sym" \
        --side "$close_side" \
        --type MARKET \
        --quantity "$pos_amt" \
        --reduceOnly true 2>&1)

    if echo "$result" | grep -qi "error"; then
        log "❌ 平仓失败: $result"
        record_trade "close" "$sym" "auto" "$pos_amt" "MARKET" "FAILED"
    else
        log "✅ 平仓成功"
        record_trade "close" "$sym" "auto" "$pos_amt" "MARKET" "OK"
    fi
    echo "$result"
}

cmd_stop() {
    local sym="$1" price="$2" side="$3"
    local close_side=""
    [ "$side" = "long" ] && close_side="SELL" || close_side="BUY"

    log "📤 设置止损: $sym $price ($side)"
    local result=$(binance-cli futures-usds new-order \
        --symbol "$sym" \
        --side "$close_side" \
        --type STOP_MARKET \
        --stopPrice "$price" \
        --closePosition true 2>&1)

    if echo "$result" | grep -qi "error"; then
        log "❌ 止损设置失败: $result"
    else
        log "✅ 止损已设置: $price"
    fi
    echo "$result"
}

cmd_tp() {
    local sym="$1" price="$2" side="$3"
    local close_side=""
    [ "$side" = "long" ] && close_side="SELL" || close_side="BUY"

    log "📤 设置止盈: $sym $price ($side)"
    local result=$(binance-cli futures-usds new-order \
        --symbol "$sym" \
        --side "$close_side" \
        --type TAKE_PROFIT_MARKET \
        --stopPrice "$price" \
        --closePosition true 2>&1)

    if echo "$result" | grep -qi "error"; then
        log "❌ 止盈设置失败: $result"
    else
        log "✅ 止盈已设置: $price"
    fi
    echo "$result"
}

cmd_status() {
    log "📊 查询账户状态..."
    echo "=== 账户余额 ==="
    binance-cli futures-usds futures-account-balance-v3 2>&1
    echo ""
    echo "=== 账户信息 ==="
    binance-cli futures-usds account-information-v3 2>&1
}

cmd_positions() {
    log "📊 查询当前持仓..."
    binance-cli futures-usds position-information-v3 2>&1
}

cmd_orders() {
    log "📊 查询当前挂单..."
    binance-cli futures-usds current-all-open-orders 2>&1
}

cmd_history() {
    local sym="$1"
    log "📊 查询历史成交${sym:+: $sym}"
    if [ -n "$sym" ]; then
        binance-cli futures-usds account-trade-list --symbol "$sym" --limit 20 2>&1
    else
        binance-cli futures-usds account-trade-list --limit 20 2>&1
    fi
}

cmd_leverage() {
    local sym="$1" lev="$2"
    log "📤 设置杠杆: $sym ${lev}x"
    binance-cli futures-usds change-initial-leverage --symbol "$sym" --leverage "$lev" 2>&1
}

cmd_cancel_all() {
    local sym="$1"
    log "📤 取消 $sym 所有挂单"
    binance-cli futures-usds cancel-all-open-orders --symbol "$sym" 2>&1
}

case "$ACTION" in
    open)       [ -z "$SYMBOL" ] && usage && exit 1; cmd_open "$SYMBOL" "$SIDE" "$QTY" ;;
    limit)      [ -z "$SYMBOL" ] || [ -z "$PRICE" ] && usage && exit 1; cmd_limit "$SYMBOL" "$SIDE" "$QTY" "$PRICE" ;;
    close)      [ -z "$SYMBOL" ] && usage && exit 1; cmd_close "$SYMBOL" ;;
    stop)       [ -z "$SYMBOL" ] || [ -z "$PRICE" ] && usage && exit 1; cmd_stop "$SYMBOL" "$PRICE" "$SIDE" ;;
    tp)         [ -z "$SYMBOL" ] || [ -z "$PRICE" ] && usage && exit 1; cmd_tp "$SYMBOL" "$PRICE" "$SIDE" ;;
    status)     cmd_status ;;
    positions)  cmd_positions ;;
    orders)     cmd_orders ;;
    history)    cmd_history "$SYMBOL" ;;
    leverage)   [ -z "$SYMBOL" ] || [ -z "$SIDE" ] && usage && exit 1; cmd_leverage "$SYMBOL" "$SIDE" ;;
    cancel-all) [ -z "$SYMBOL" ] && usage && exit 1; cmd_cancel_all "$SYMBOL" ;;
    help|*)     usage ;;
esac
