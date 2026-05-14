#!/usr/bin/env python3
"""
动态交易品种池更新脚本 V3
======================
功能：
  1. 从币安实盘获取 24h ticker 数据
  2. 涨幅前15 + 跌幅前15 + 24h成交额>3000万USDT
  3. 去重、剔除稳定币/贵金属/杠杆代币
  4. 结果存入 config/symbols.json

用法：
  python3 scripts/update_symbols_pool.py [--dry-run]
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── 日志设置 ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "symbols_pool_update.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("symbols_pool")

# ── 常量 ──
CONFIG_DIR = PROJECT_ROOT / "config"
SYMBOLS_FILE = CONFIG_DIR / "symbols.json"

PROD_API = "https://fapi.binance.com"
VOLUME_THRESHOLD = 30_000_000  # 3000万 USDT
TOP_N = 15                      # 涨幅/跌幅前15
MAX_SYMBOLS = 35                # 品种池上限

# 稳定币黑名单
STABLECOINS = {
    "USDC", "USDP", "TUSD", "DAI", "FDUSD", "BUSD", "USDD",
    "EURS", "EURT", "USDK", "USDT",
    "VAI", "USDN", "RSV", "GUSD", "HUSD", "SUSD",
    "UST", "USTC", "FRAX", "LUSD", "MIM", "FEI",
}

# 手动排除品种
EXCLUDE_SYMBOLS = {"BNB", "XAU", "XAG"}


def is_excluded(base: str) -> bool:
    """判断是否为稳定币或排除品种"""
    return base.upper() in STABLECOINS or base.upper() in EXCLUDE_SYMBOLS


def fetch_24h_tickers() -> list:
    """从币安实盘获取 24h ticker"""
    url = f"{PROD_API}/fapi/v1/ticker/24hr"
    logger.info(f"📡 获取实盘 24h ticker: {url}")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"✅ 获取成功，共 {len(data)} 个交易对")
        return data
    except Exception as e:
        logger.error(f"❌ 获取失败: {e}")
        return []


def build_pool(tickers: list) -> list:
    """构建品种池：涨幅前15 + 跌幅前15 + 成交额>3000万"""
    # 筛选：USDT永续、非稳定币、非排除、有成交量
    valid = []
    excluded_count = 0
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            excluded_count += 1
            continue
        base = symbol[:-4]
        if is_excluded(base):
            excluded_count += 1
            continue
        quote_volume = float(t.get("quoteVolume", 0))
        if quote_volume <= 0:
            excluded_count += 1
            continue
        # 排除杠杆代币
        if any(base.endswith(s) and len(base) > len(s) for s in ["UP", "DOWN", "BULL", "BEAR"]):
            excluded_count += 1
            continue
        valid.append({
            "symbol": symbol,
            "base": base,
            "quoteVolume": quote_volume,
            "priceChangePercent": float(t.get("priceChangePercent", 0)),
        })
    logger.info(f"📊 有效品种: {len(valid)} 个，排除 {excluded_count} 个")

    # 三个集合
    # A: 成交额 > 3000万
    set_vol = {s["symbol"] for s in valid if s["quoteVolume"] > VOLUME_THRESHOLD}
    logger.info(f"📈 成交额>{VOLUME_THRESHOLD/1e6:.0f}万U: {len(set_vol)} 个")

    # B: 涨幅前15
    sorted_gain = sorted(valid, key=lambda x: x["priceChangePercent"], reverse=True)
    set_gain = {s["symbol"] for s in sorted_gain[:TOP_N]}
    logger.info(f"🚀 涨幅前{TOP_N}: {set_gain}")

    # C: 跌幅前15
    sorted_loss = sorted(valid, key=lambda x: x["priceChangePercent"])
    set_loss = {s["symbol"] for s in sorted_loss[:TOP_N]}
    logger.info(f"📉 跌幅前{TOP_N}: {set_loss}")

    # 合并去重
    merged = set_vol | set_gain | set_loss
    logger.info(f"🎯 合并去重后: {len(merged)} 个")

    # 构建完整数据
    vol_map = {s["symbol"]: s for s in valid}
    result = []
    for sym in sorted(merged):
        info = vol_map[sym]
        result.append(info)

    # 按成交额排序，取前 MAX_SYMBOLS
    result.sort(key=lambda x: x["quoteVolume"], reverse=True)
    if len(result) > MAX_SYMBOLS:
        trimmed = result[MAX_SYMBOLS:]
        logger.info(f"⚡ 截取前 {MAX_SYMBOLS} 个（按成交额排序）")
        logger.info(f"  被截断: {[s['symbol'] for s in trimmed]}")
        result = result[:MAX_SYMBOLS]

    return result


def main():
    parser = argparse.ArgumentParser(description="动态交易品种池更新 V3（实盘数据）")
    parser.add_argument("--dry-run", action="store_true", help="只输出结果，不写入文件")
    args = parser.parse_args()

    logger.info("🏁 开始更新动态交易品种池 V3（实盘数据）")

    tickers = fetch_24h_tickers()
    if not tickers:
        logger.error("❌ 无法获取 ticker 数据，退出")
        sys.exit(1)

    pool = build_pool(tickers)

    # 构建 watchlist
    watchlist = []
    for i, item in enumerate(pool):
        watchlist.append({
            "symbol": item["symbol"],
            "name": item["base"],
            "enabled": True,
            "priority": i + 1,
            "min_volume_24h": round(item["quoteVolume"], 0),
            "max_leverage": 20,
            "note": f"V3动态池 | 来源: volume/top_gainers/top_losers | 实盘数据",
        })

    # 打印报告
    print("\n" + "=" * 70)
    print("📊 动态交易品种池筛选报告 V3（实盘数据）")
    print(f"⏰ 更新时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)
    for i, item in enumerate(pool, 1):
        vol_str = f"{item['quoteVolume']:,.0f}"
        pct_str = f"{item['priceChangePercent']:+.2f}%"
        print(f"  {i:3d}. {item['symbol']:<15}  成交额: {vol_str:>15}  涨跌: {pct_str:>8}")
    print(f"\n总计: {len(pool)} 个品种")
    print("=" * 70 + "\n")

    if args.dry_run:
        logger.info("🔍 [DRY-RUN] 未写入文件")
        return

    # 备份 + 写入
    backup_path = SYMBOLS_FILE.with_suffix(
        f".json.bak.{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    )
    if SYMBOLS_FILE.exists():
        import shutil
        shutil.copy2(SYMBOLS_FILE, backup_path)
        logger.info(f"💾 已备份: {backup_path}")

    new_config = {
        "watchlist": watchlist,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_symbols": len(watchlist),
        "update_method": "prod_only_v3",
        "note": "V3: 实盘数据 | 涨幅前15+跌幅前15+成交额>3000万U | 去重取前35",
    }
    with open(SYMBOLS_FILE, "w", encoding="utf-8") as f:
        json.dump(new_config, f, indent=2, ensure_ascii=False)
    logger.info(f"✅ 已更新: {SYMBOLS_FILE}")


if __name__ == "__main__":
    main()
