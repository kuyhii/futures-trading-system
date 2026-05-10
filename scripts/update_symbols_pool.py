#!/usr/bin/env python3
"""
动态交易品种池更新脚本
======================
功能：
  1. 从 Binance 获取 24h ticker 数据
  2. 筛选 USDT 永续合约
  3. 排除稳定币
  4. 计算3个集合并取并集
  5. 更新 config/symbols.json

集合定义：
  A. 前24h成交额 > 10,000,000 USDT
  B. 24h涨幅前20
  C. 24h跌幅前20

用法：
  python3 scripts/update_symbols_pool.py [--testnet|--prod] [--dry-run]
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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("symbols_pool")

# ── 常量 ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
SYMBOLS_FILE = CONFIG_DIR / "symbols.json"

# 稳定币黑名单（基础币种 + 常见衍生）
STABLECOINS = {
    "USDC", "USDP", "TUSD", "DAI", "FDUSD", "BUSD", "USDD",
    "EURS", "EURT", "USDK", "USDT",  # USDT itself excluded as base
    "VAI", "USDN", "RSV", "GUSD", "HUSD", "SUSD",
    "UST", "USTC", "FRAX", "LUSD", "MIM", "FEI",
    # 稳定币衍生对
    "USD",  # 任何以USD结尾但不是USDT的
}

# API URLs
API_URLS = {
    "testnet": "https://testnet.binancefuture.com",
    "prod": "https://fapi.binance.com",
}

# 阈值
VOLUME_THRESHOLD = 10_000_000  # 1000万 USDT
TOP_N = 20  # 涨跌幅前N


def is_stablecoin(base: str) -> bool:
    """判断一个基础币种是否为稳定币"""
    upper = base.upper()
    if upper in STABLECOINS:
        return True
    # 排除含"USD"但不是"USDT"的（如USDC、USDP、BUSD等）
    # USDT 本身作为报价货币保留，不作为基础币种
    return False


def fetch_24h_tickers(base_url: str) -> list:
    """从 Binance 获取 24h ticker 数据"""
    url = f"{base_url}/fapi/v1/ticker/24hr"
    logger.info(f"📡 正在获取 24h ticker 数据: {url}")

    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"✅ 获取成功，共 {len(data)} 个交易对")
        return data
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ 获取 ticker 数据失败: {e}")
        sys.exit(1)


def filter_symbols(tickers: list) -> list:
    """筛选 USDT 永续合约，排除稳定币和异常数据"""
    filtered = []
    excluded_count = 0

    for t in tickers:
        symbol = t.get("symbol", "")

        # 只保留 USDT 永续合约
        if not symbol.endswith("USDT"):
            excluded_count += 1
            continue

        base = symbol[:-4]  # 去掉 "USDT"

        # 排除稳定币
        if is_stablecoin(base):
            excluded_count += 1
            continue

        # 排除异常数据（成交额为0或负数）
        quote_volume = float(t.get("quoteVolume", 0))
        if quote_volume <= 0:
            excluded_count += 1
            continue

        # 排除杠杆代币（UP/DOWN/BULL/BEAR）
        for suffix in ["UP", "DOWN", "BULL", "BEAR"]:
            if base.endswith(suffix) and len(base) > len(suffix):
                excluded_count += 1
                break
        else:
            filtered.append(t)

    logger.info(f"📊 筛选结果: {len(filtered)} 个有效交易对，排除 {excluded_count} 个")
    return filtered


def compute_sets(tickers: list) -> tuple:
    """计算3个候选集合"""

    # ── 集合A：成交额 > 1000万 USDT ──
    set_a = []
    for t in tickers:
        quote_volume = float(t.get("quoteVolume", 0))
        if quote_volume > VOLUME_THRESHOLD:
            set_a.append({
                "symbol": t["symbol"],
                "quoteVolume": quote_volume,
                "priceChangePercent": float(t.get("priceChangePercent", 0)),
            })
    set_a.sort(key=lambda x: x["quoteVolume"], reverse=True)
    logger.info(f"📈 集合A（成交额>1000万U）: {len(set_a)} 个币种")

    # ── 集合B：涨幅前20 ──
    sorted_by_gain = sorted(
        tickers,
        key=lambda x: float(x.get("priceChangePercent", 0)),
        reverse=True,
    )
    set_b = []
    for t in sorted_by_gain[:TOP_N]:
        set_b.append({
            "symbol": t["symbol"],
            "quoteVolume": float(t.get("quoteVolume", 0)),
            "priceChangePercent": float(t.get("priceChangePercent", 0)),
        })
    logger.info(f"🚀 集合B（涨幅前{TOP_N}）: {[s['symbol'] for s in set_b]}")

    # ── 集合C：跌幅前20 ──
    sorted_by_loss = sorted(
        tickers,
        key=lambda x: float(x.get("priceChangePercent", 0)),
    )
    set_c = []
    for t in sorted_by_loss[:TOP_N]:
        set_c.append({
            "symbol": t["symbol"],
            "quoteVolume": float(t.get("quoteVolume", 0)),
            "priceChangePercent": float(t.get("priceChangePercent", 0)),
        })
    logger.info(f"📉 集合C（跌幅前{TOP_N}）: {[s['symbol'] for s in set_c]}")

    return set_a, set_b, set_c


def merge_sets(set_a, set_b, set_c) -> dict:
    """合并3个集合，取并集，按成交额排序"""
    merged = {}

    for item in set_a:
        merged[item["symbol"]] = {
            "symbol": item["symbol"],
            "quoteVolume": item["quoteVolume"],
            "priceChangePercent": item["priceChangePercent"],
            "sources": ["volume"],
        }

    for item in set_b:
        if item["symbol"] in merged:
            merged[item["symbol"]]["sources"].append("top_gainers")
        else:
            merged[item["symbol"]] = {
                "symbol": item["symbol"],
                "quoteVolume": item["quoteVolume"],
                "priceChangePercent": item["priceChangePercent"],
                "sources": ["top_gainers"],
            }

    for item in set_c:
        if item["symbol"] in merged:
            merged[item["symbol"]]["sources"].append("top_losers")
        else:
            merged[item["symbol"]] = {
                "symbol": item["symbol"],
                "quoteVolume": item["quoteVolume"],
                "priceChangePercent": item["priceChangePercent"],
                "sources": ["top_losers"],
            }

    # 按成交额排序
    result = sorted(merged.values(), key=lambda x: x["quoteVolume"], reverse=True)
    return result


def get_symbol_name(symbol: str) -> str:
    """从 symbol 提取基础名称（BTCUSDT → BTC）"""
    if symbol.endswith("USDT"):
        return symbol[:-4]
    return symbol


def build_watchlist(merged: list, existing_config: dict) -> list:
    """构建最终的 watchlist，保留原有配置信息"""
    existing = {s["symbol"]: s for s in existing_config.get("watchlist", [])}
    watchlist = []

    for i, item in enumerate(merged):
        symbol = item["symbol"]
        base = get_symbol_name(symbol)

        if symbol in existing:
            # 保留已有配置，启用它
            entry = existing[symbol].copy()
            entry["enabled"] = True
            # 更新统计数据
            entry["min_volume_24h"] = round(item["quoteVolume"], 0)
        else:
            # 新增币种
            entry = {
                "symbol": symbol,
                "name": base,
                "enabled": True,
                "priority": i + 1,
                "min_volume_24h": round(item["quoteVolume"], 0),
                "max_leverage": 5,
                "note": f"动态池添加 | 来源: {','.join(item['sources'])}",
            }
        watchlist.append(entry)

    return watchlist


def update_symbols_file(watchlist: list, dry_run: bool = False):
    """更新 symbols.json"""
    backup_path = SYMBOLS_FILE.with_suffix(f".json.bak.{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")

    # 备份旧文件
    if SYMBOLS_FILE.exists():
        import shutil
        shutil.copy2(SYMBOLS_FILE, backup_path)
        logger.info(f"💾 已备份: {backup_path}")

    new_config = {
        "watchlist": watchlist,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_symbols": len(watchlist),
        "update_method": "dynamic_pool_3set_union",
        "note": "动态交易品种池：每日自动更新，基于成交额>1000万U ∪ 涨幅前20 ∪ 跌幅前20",
    }

    if dry_run:
        logger.info("🔍 [DRY-RUN] 以下配置将被写入:")
        print(json.dumps(new_config, indent=2, ensure_ascii=False))
        return

    with open(SYMBOLS_FILE, "w", encoding="utf-8") as f:
        json.dump(new_config, f, indent=2, ensure_ascii=False)

    logger.info(f"✅ 已更新: {SYMBOLS_FILE}")


def print_report(merged: list, set_a: list, set_b: list, set_c: list):
    """输出筛选报告"""
    print("\n" + "=" * 70)
    print("📊 动态交易品种池筛选报告")
    print(f"⏰ 更新时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)

    print(f"\n📈 集合A（成交额>1000万USDT）: {len(set_a)} 个")
    print(f"🚀 集合B（涨幅前{TOP_N}）: {len(set_b)} 个")
    print(f"📉 集合C（跌幅前{TOP_N}）: {len(set_c)} 个")
    print(f"🎯 最终品种池（并集）: {len(merged)} 个")

    print("\n── 品种池详情 ──")
    for i, item in enumerate(merged, 1):
        sources = " + ".join(item["sources"])
        vol_str = f"{item['quoteVolume']:,.0f}"
        pct_str = f"{item['priceChangePercent']:+.2f}%"
        print(f"  {i:3d}. {item['symbol']:<15}  成交额: {vol_str:>15}  涨跌: {pct_str:>8}  来源: {sources}")

    # 来源统计
    source_counts = {"volume": 0, "top_gainers": 0, "top_losers": 0}
    for item in merged:
        for s in item["sources"]:
            source_counts[s] += 1

    print("\n── 来源统计 ──")
    print(f"  仅成交额达标: {sum(1 for x in merged if x['sources'] == ['volume'])} 个")
    print(f"  仅涨幅达标:   {sum(1 for x in merged if x['sources'] == ['top_gainers'])} 个")
    print(f"  仅跌幅达标:   {sum(1 for x in merged if x['sources'] == ['top_losers'])} 个")
    print(f"  多源重叠:     {sum(1 for x in merged if len(x['sources']) > 1)} 个")
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="动态交易品种池更新")
    parser.add_argument("--testnet", action="store_true", default=True, help="使用测试网（默认）")
    parser.add_argument("--prod", action="store_true", help="使用实盘")
    parser.add_argument("--dry-run", action="store_true", help="只输出结果，不写入文件")
    args = parser.parse_args()

    env = "prod" if args.prod else "testnet"
    base_url = API_URLS[env]

    logger.info(f"🏁 开始更新动态交易品种池 [环境: {env}]")

    # Step 1: 获取数据
    tickers_raw = fetch_24h_tickers(base_url)

    # Step 2: 筛选
    tickers = filter_symbols(tickers_raw)

    # Step 3: 计算集合
    set_a, set_b, set_c = compute_sets(tickers)

    # Step 4: 合并
    merged = merge_sets(set_a, set_b, set_c)

    # Step 5: 构建 watchlist
    existing_config = {}
    if SYMBOLS_FILE.exists():
        with open(SYMBOLS_FILE, "r", encoding="utf-8") as f:
            existing_config = json.load(f)
        logger.info(f"📂 已有配置: {len(existing_config.get('watchlist', []))} 个币种")

    watchlist = build_watchlist(merged, existing_config)

    # Step 6: 输出报告
    print_report(merged, set_a, set_b, set_c)

    # Step 7: 写入文件
    update_symbols_file(watchlist, dry_run=args.dry_run)

    logger.info("🏁 品种池更新完成")


if __name__ == "__main__":
    main()
