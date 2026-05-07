#!/bin/bash
# test_connectivity.sh - 测试 binance-cli 连通性
# Phase 0: 验证工具可用性，不依赖 API Key

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

LOG="$WORKSPACE/logs/system.log"

log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $1" | tee -a "$LOG"
}

log "========== 连通性测试开始 =========="

# 1. 检查 binance-cli 是否安装
log ">>> 1. 检查 binance-cli 安装..."
if command -v binance-cli &>/dev/null; then
    BINANCE_CLI=$(command -v binance-cli)
    log "✅ binance-cli 已安装: $BINANCE_CLI"
    binance-cli --version 2>&1 | head -3 | while read line; do log "   $line"; done
else
    log "❌ binance-cli 未安装"
    exit 1
fi

# 2. 检查 futures-usds 子命令
log ">>> 2. 检查 futures-usds 子命令..."
if binance-cli futures-usds --help &>/dev/null; then
    log "✅ futures-usds 子命令可用"
else
    log "❌ futures-usds 子命令不可用"
    exit 1
fi

# 3. 测试无需认证的公开接口
log ">>> 3. 测试公开 API 接口..."

# 3a. 服务器时间
log "   3a. 获取服务器时间..."
TIME_RESULT=$(binance-cli futures-usds get-server-time 2>&1)
if echo "$TIME_RESULT" | grep -q "serverTime"; then
    SERVER_TIME=$(echo "$TIME_RESULT" | grep -o '"serverTime":[0-9]*' | cut -d: -f2)
    log "   ✅ 服务器时间: $SERVER_TIME ($(date -d @$(echo "$SERVER_TIME" | cut -c1-10) -u '+%Y-%m-%d %H:%M:%S UTC' 2>/dev/null || echo '解析失败'))"
else
    log "   ⚠️  服务器时间获取失败: $TIME_RESULT"
fi

# 3b. 交易规则
log "   3b. 获取交易规则..."
EXCHANGE_INFO=$(binance-cli futures-usds exchange-information 2>&1)
if echo "$EXCHANGE_INFO" | grep -q "symbols"; then
    SYMBOL_COUNT=$(echo "$EXCHANGE_INFO" | grep -o '"symbol"' | wc -l)
    log "   ✅ 交易规则获取成功，共 $SYMBOL_COUNT 个交易对"
else
    log "   ⚠️  交易规则获取失败"
fi

# 3c. 获取 BTCUSDT 标记价格
log "   3c. 获取 BTCUSDT 标记价格..."
MARK_PRICE=$(binance-cli futures-usds mark-price --symbol BTCUSDT 2>&1)
if echo "$MARK_PRICE" | grep -q "markPrice"; then
    PRICE=$(echo "$MARK_PRICE" | grep -oP '"markPrice"\s*:\s*"[^"]*"' | grep -oP '[\d.]+')
    log "   ✅ BTCUSDT 标记价格: $PRICE"
else
    log "   ⚠️  BTCUSDT 标记价格获取失败: $MARK_PRICE"
fi

# 4. 检查 API Key 配置
log ">>> 4. 检查 API Key 配置..."
if [ -n "$BINANCE_API_KEY" ] && [ -n "$BINANCE_SECRET_KEY" ]; then
    log "✅ API Key 已配置 (BINANCE_API_ENV=${BINANCE_API_ENV:-未设置})"
    if [ "${BINANCE_API_ENV:-}" = "testnet" ] || [ "${BINANCE_API_ENV:-}" = "demo" ]; then
        log "   当前环境: ${BINANCE_API_ENV}（测试环境）"
    else
        log "   ⚠️  当前环境: ${BINANCE_API_ENV:-prod}（请确认是否使用实盘）"
    fi
else
    log "⚠️  API Key 未配置（公开接口测试正常，交易功能暂不可用）"
    log "   请设置: BINANCE_API_KEY, BINANCE_SECRET_KEY, BINANCE_API_ENV"
fi

# 5. 检查工作空间目录
log ">>> 5. 检查工作空间目录..."
for dir in config data/klines data/signals data/positions logs scripts state; do
    if [ -d "$WORKSPACE/$dir" ]; then
        log "   ✅ $dir/"
    else
        log "   ❌ $dir/ 不存在"
    fi
done

# 6. 检查配置文件
log ">>> 6. 检查配置文件..."
for conf in strategies.json risk.json symbols.json; do
    if [ -f "$WORKSPACE/config/$conf" ]; then
        log "   ✅ config/$conf"
    else
        log "   ❌ config/$conf 不存在"
    fi
done

log "========== 连通性测试完成 =========="
