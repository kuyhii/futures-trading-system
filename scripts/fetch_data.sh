#!/bin/bash
# fetch_data.sh - 数据采集脚本（Phase 1 框架）
# 用途: 从 binance-cli 拉取行情数据并缓存到 data/ 目录
# 使用方式:
#   bash scripts/fetch_data.sh kline BTCUSDT 15m
#   bash scripts/fetch_data.sh price BTCUSDT
#   bash scripts/fetch_data.sh funding BTCUSDT
#   bash scripts/fetch_data.sh oi BTCUSDT
#   bash scripts/fetch_data.sh lsr BTCUSDT
#   bash scripts/fetch_data.sh all

set -e

WORKSPACE="/root/.openclaw/workspace-taizi/futures-trading-system"

# ── 加载环境配置（API Key + 测试网/实盘选择） ──
ENV_FILE="$WORKSPACE/.env"
if [ -f "$ENV_FILE" ]; then
    # 用 set -a 确保所有从 .env 读取的变量都自动 export
    set -a
    # 只读取 KEY=VALUE 行，忽略注释和空行
    while IFS= read -r line; do
        case "$line" in
            ''|\#*) continue ;;
            *=*) eval "export $line" ;;
        esac
    done < "$ENV_FILE"
    set +a
fi

BINANCE_API_ENV="${BINANCE_API_ENV:-testnet}"

# 实盘环境切换：如果 BINANCE_API_ENV=prod 且有独立的 prod key，则覆盖
if [ "$BINANCE_API_ENV" = "prod" ]; then
    [ -n "${BINANCE_PROD_API_KEY:-}" ] && export BINANCE_API_KEY="$BINANCE_PROD_API_KEY"
    [ -n "${BINANCE_PROD_SECRET_KEY:-}" ] && export BINANCE_SECRET_KEY="$BINANCE_PROD_SECRET_KEY"
fi

if [ -z "${BINANCE_API_KEY:-}" ]; then
    echo "⚠️  [fetch_data] API Key 未配置，仅支持公开接口（K线/价格/资金费率等）"
fi

DATA_DIR="$WORKSPACE/data"
LOG="$WORKSPACE/logs/system.log"
TIMESTAMP=$(date -u '+%Y%m%d_%H%M%S')

log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] [fetch_data] $1" | tee -a "$LOG"
}

# 默认参数
ACTION="${1:-help}"
SYMBOL="${2:-BTCUSDT}"
TIMEFRAME="${3:-15m}"

usage() {
    echo "用法: $0 <action> [symbol] [timeframe]"
    echo ""
    echo "Actions:"
    echo "  kline   [symbol] [timeframe]  - 拉取 K 线数据"
    echo "  price   [symbol]              - 拉取标记价格"
    echo "  funding [symbol]              - 拉取资金费率"
    echo "  oi      [symbol]              - 拉取持仓量"
    echo "  lsr     [symbol]              - 拉取大户多空比"
    echo "  all     [symbol]              - 拉取所有数据"
    echo "  help                          - 显示此帮助"
    echo ""
    echo "示例:"
    echo "  $0 kline BTCUSDT 15m"
    echo "  $0 price BTCUSDT"
    echo "  $0 all BTCUSDT"
}

fetch_kline() {
    local sym="$1" tf="$2"
    local outfile="$DATA_DIR/klines/${sym}_${tf}_${TIMESTAMP}.json"
    log "拉取 K 线数据: $sym $tf"
    local result=$(binance-cli futures-usds kline-candlestick-data \
        --symbol "$sym" \
        --interval "$tf" \
        --limit 100 2>&1)
    echo "$result" > "$outfile"
    local count=$(echo "$result" | grep -c '\[' || true)
    log "✅ K 线数据已保存: $outfile ($count 条)"
    echo "$outfile"
}

fetch_price() {
    local sym="$1"
    local outfile="$DATA_DIR/klines/mark_price_${sym}_${TIMESTAMP}.json"
    log "拉取标记价格: $sym"
    binance-cli futures-usds mark-price --symbol "$sym" 2>&1 | tee "$outfile"
    log "✅ 标记价格已保存: $outfile"
}

fetch_funding() {
    local sym="$1"
    local outfile="$DATA_DIR/klines/funding_rate_${sym}_${TIMESTAMP}.json"
    log "拉取资金费率: $sym"
    binance-cli futures-usds get-funding-rate-history \
        --symbol "$sym" \
        --limit 10 2>&1 | tee "$outfile"
    log "✅ 资金费率已保存: $outfile"
}

fetch_oi() {
    local sym="$1"
    local outfile="$DATA_DIR/klines/open_interest_${sym}_${TIMESTAMP}.json"
    log "拉取持仓量: $sym"
    binance-cli futures-usds open-interest-statistics \
        --symbol "$sym" \
        --period "5m" \
        --limit 30 2>&1 | tee "$outfile"
    log "✅ 持仓量已保存: $outfile"
}

fetch_lsr() {
    local sym="$1"
    local outfile="$DATA_DIR/klines/lsr_${sym}_${TIMESTAMP}.json"
    log "拉取大户多空比: $sym"
    binance-cli futures-usds top-trader-long-short-ratio-accounts \
        --symbol "$sym" \
        --period "5m" \
        --limit 30 2>&1 | tee "$outfile"
    log "✅ 大户多空比已保存: $outfile"
}

fetch_all() {
    local sym="$1"
    log "========== 全量数据采集: $sym =========="
    fetch_price "$sym"
    fetch_kline "$sym" "15m"
    fetch_kline "$sym" "1h"
    fetch_funding "$sym"
    fetch_oi "$sym"
    fetch_lsr "$sym"
    log "========== 全量数据采集完成: $sym =========="
}

case "$ACTION" in
    kline)   fetch_kline "$SYMBOL" "$TIMEFRAME" ;;
    price)   fetch_price "$SYMBOL" ;;
    funding) fetch_funding "$SYMBOL" ;;
    oi)      fetch_oi "$SYMBOL" ;;
    lsr)     fetch_lsr "$SYMBOL" ;;
    all)     fetch_all "$SYMBOL" ;;
    help|*)  usage ;;
esac
