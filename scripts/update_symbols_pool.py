#!/usr/bin/env python3
"""
动态交易品种池更新脚本 V2
======================
功能：
  1. 分别从 实盘(prod) 和 模拟盘(testnet) 获取 24h ticker 数据
  2. 各自计算3个集合，然后进行双重验证筛选
  3. 筛选规则：
     - 实盘有 + 模拟盘有 → 通过
     - 实盘有 + 模拟盘没有 → 排除
     - 实盘没有 + 模拟盘有 → 排除
     - 实盘成交量不达标（即使模拟盘达标）→ 排除
  4. 涨幅/跌幅排名以实盘数据为准
  5. 模拟盘涨幅/跌幅取前35（扩大范围以覆盖差异）
  6. 更新 config/symbols.json

集合定义（实盘）：
  A. 前24h成交额 > 80,000,000 USDT
  B. 24h涨幅前10
  C. 24h跌幅前10

集合定义（模拟盘）：
  A. 前24h成交额 > 80,000,000 USDT
  B. 24h涨幅前30
  C. 24h跌幅前30

最终规则：实盘和模拟盘都存在的品种，取并集

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

# 同时输出到控制台和文件
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
SECRETS_FILE = PROJECT_ROOT / "secrets" / "api_keys.env"

# 稳定币黑名单
STABLECOINS = {
    "USDC", "USDP", "TUSD", "DAI", "FDUSD", "BUSD", "USDD",
    "EURS", "EURT", "USDK", "USDT",
    "VAI", "USDN", "RSV", "GUSD", "HUSD", "SUSD",
    "UST", "USTC", "FRAX", "LUSD", "MIM", "FEI",
}

# 手动排除品种（永不加入品种池）
EXCLUDE_SYMBOLS = {
    "BNB",     # 用户手动排除
    "XAU",     # 贵金属：黄金
    "XAG",     # 贵金属：白银
}

# API URLs
API_URLS = {
    "testnet": "https://testnet.binancefuture.com",
    "prod": "https://fapi.binance.com",
}

# 阈值
VOLUME_THRESHOLD = 80_000_000  # 8000万 USDT
PROD_TOP_N = 10    # 实盘涨幅/跌幅前N
TESTNET_TOP_N = 30 # 模拟盘涨幅/跌幅前N
MAX_SYMBOLS = 35   # 品种池上限（确保60秒内可完成全量分析）


def is_excluded(base: str) -> bool:
    """判断一个基础币种是否为稳定币或在排除列表中"""
    return base.upper() in STABLECOINS or base.upper() in EXCLUDE_SYMBOLS


def load_api_keys() -> dict:
    """从 secrets/api_keys.env 加载 API 密钥"""
    keys = {}
    if SECRETS_FILE.exists():
        with open(SECRETS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip()
                    if key and val:
                        keys[key] = val
        logger.info("🔑 已从 secrets/api_keys.env 加载 API 密钥")
    else:
        logger.warning("⚠️ secrets/api_keys.env 不存在，仅使用公开接口")
    return keys


def fetch_24h_tickers(base_url: str, api_keys: dict = None, env: str = "prod") -> list:
    """从 Binance 获取 24h ticker 数据"""
    url = f"{base_url}/fapi/v1/ticker/24hr"
    logger.info(f"📡 [{env}] 正在获取 24h ticker: {url}")

    headers = {}
    if api_keys and env == "prod":
        api_key = api_keys.get("BINANCE_PROD_API_KEY", "")
        if api_key:
            headers["X-MBX-APIKEY"] = api_key
            logger.info(f"  🔑 使用实盘 API Key（{api_key[:8]}...）")

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"✅ [{env}] 获取成功，共 {len(data)} 个交易对")
        return data
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [{env}] 获取 ticker 数据失败: {e}")
        return []


def filter_symbols(tickers: list, env_label: str = "") -> list:
    """筛选 USDT 永续合约，排除稳定币和异常数据"""
    filtered = []
    excluded = 0

    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            excluded += 1
            continue

        base = symbol[:-4]
        if is_excluded(base):
            excluded += 1
            continue

        quote_volume = float(t.get("quoteVolume", 0))
        if quote_volume <= 0:
            excluded += 1
            continue

        # 排除杠杆代币
        skip = False
        for suffix in ["UP", "DOWN", "BULL", "BEAR"]:
            if base.endswith(suffix) and len(base) > len(suffix):
                excluded += 1
                skip = True
                break
        if skip:
            continue

        filtered.append(t)

    label = f"[{env_label}] " if env_label else ""
    logger.info(f"{label}📊 筛选: {len(filtered)} 个有效，排除 {excluded} 个")
    return filtered


def compute_sets(tickers: list, top_n: int, env_label: str = "") -> tuple:
    """计算3个候选集合"""

    # ── 集合A：成交额 > 8000万 USDT ──
    set_a = {}
    for t in tickers:
        quote_volume = float(t.get("quoteVolume", 0))
        if quote_volume > VOLUME_THRESHOLD:
            set_a[t["symbol"]] = {
                "symbol": t["symbol"],
                "quoteVolume": quote_volume,
                "priceChangePercent": float(t.get("priceChangePercent", 0)),
            }
    label = f"[{env_label}] " if env_label else ""
    logger.info(f"{label}📈 集合A（成交额>8000万U）: {len(set_a)} 个")

    # ── 集合B：涨幅前N ──
    sorted_by_gain = sorted(
        tickers,
        key=lambda x: float(x.get("priceChangePercent", 0)),
        reverse=True,
    )
    set_b = {}
    for t in sorted_by_gain[:top_n]:
        set_b[t["symbol"]] = {
            "symbol": t["symbol"],
            "quoteVolume": float(t.get("quoteVolume", 0)),
            "priceChangePercent": float(t.get("priceChangePercent", 0)),
        }
    logger.info(f"{label}🚀 集合B（涨幅前{top_n}）: {len(set_b)} 个")

    # ── 集合C：跌幅前N ──
    sorted_by_loss = sorted(
        tickers,
        key=lambda x: float(x.get("priceChangePercent", 0)),
    )
    set_c = {}
    for t in sorted_by_loss[:top_n]:
        set_c[t["symbol"]] = {
            "symbol": t["symbol"],
            "quoteVolume": float(t.get("quoteVolume", 0)),
            "priceChangePercent": float(t.get("priceChangePercent", 0)),
        }
    logger.info(f"{label}📉 集合C（跌幅前{top_n}）: {len(set_c)} 个")

    return set_a, set_b, set_c


def dual_filter(prod_sets, testnet_sets) -> dict:
    """
    双重验证筛选：
    - 实盘和模拟盘都必须存在该品种
    - 实盘成交量必须达标
    - 涨幅/跌幅以实盘数据为准
    """
    prod_a, prod_b, prod_c = prod_sets
    test_a, test_b, test_c = testnet_sets

    # 模拟盘所有品种的集合
    testnet_all = set(test_a.keys()) | set(test_b.keys()) | set(test_c.keys())

    # 实盘所有品种的集合
    prod_all = set(prod_a.keys()) | set(prod_b.keys()) | set(prod_c.keys())

    logger.info(f"🔍 双重验证筛选:")
    logger.info(f"  实盘候选: {len(prod_all)} 个")
    logger.info(f"  模拟盘候选: {len(testnet_all)} 个")

    # 交集：实盘和模拟盘都有
    common = prod_all & testnet_all
    logger.info(f"  ✅ 交集（两边都有）: {len(common)} 个")

    # 排除：实盘有但模拟盘没有
    prod_only = prod_all - testnet_all
    logger.info(f"  ❌ 实盘独有（排除）: {len(prod_only)} 个")
    if prod_only:
        for s in sorted(prod_only):
            logger.info(f"    - {s} (模拟盘未上线)")

    # 排除：模拟盘有但实盘没有
    testnet_only = testnet_all - prod_all
    logger.info(f"  ❌ 模拟盘独有（排除）: {len(testnet_only)} 个")

    # 最终品种池（仅保留成交额达标 OR 在涨跌榜的品种）
    final = {}
    for sym in common:
        # 使用实盘数据
        prod_data = None
        sources = []

        if sym in prod_a:
            prod_data = prod_a[sym]
            sources.append("volume")
        if sym in prod_b:
            if prod_data is None:
                prod_data = prod_b[sym]
            sources.append("top_gainers")
        if sym in prod_c:
            if prod_data is None:
                prod_data = prod_c[sym]
            sources.append("top_losers")

        if prod_data:
            final[sym] = {
                "symbol": sym,
                "quoteVolume": prod_data["quoteVolume"],
                "priceChangePercent": prod_data["priceChangePercent"],
                "sources": sources,
                "testnet_verified": True,
            }

    logger.info(f"  🎯 最终品种池: {len(final)} 个")
    return final


def build_watchlist(merged: dict, existing_config: dict) -> list:
    """构建最终的 watchlist"""
    existing = {s["symbol"]: s for s in existing_config.get("watchlist", [])}
    watchlist = []

    sorted_items = sorted(merged.values(), key=lambda x: x["quoteVolume"], reverse=True)

    # 品种池上限：按成交额截取前 MAX_SYMBOLS 个
    if len(sorted_items) > MAX_SYMBOLS:
        trimmed = sorted_items[MAX_SYMBOLS:]
        logger.info(f"⚡ 品种池上限 {MAX_SYMBOLS}，截取前 {MAX_SYMBOLS} 个（按成交额排序）")
        logger.info(f"  被截断: {[s['symbol'] for s in trimmed]}")
        sorted_items = sorted_items[:MAX_SYMBOLS]

    for i, item in enumerate(sorted_items):
        symbol = item["symbol"]
        base = symbol[:-4] if symbol.endswith("USDT") else symbol

        if symbol in existing:
            entry = existing[symbol].copy()
            entry["enabled"] = True
            entry["min_volume_24h"] = round(item["quoteVolume"], 0)
            entry["note"] = f"动态池保留 | 来源: {','.join(item['sources'])} | 实盘+模拟盘双重验证"
        else:
            entry = {
                "symbol": symbol,
                "name": base,
                "enabled": True,
                "priority": i + 1,
                "min_volume_24h": round(item["quoteVolume"], 0),
                "max_leverage": 20,
                "note": f"动态池添加 | 来源: {','.join(item['sources'])} | 实盘+模拟盘双重验证",
            }
        watchlist.append(entry)

    return watchlist


def update_symbols_file(watchlist: list, dry_run: bool = False):
    """更新 symbols.json"""
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
        "update_method": "dual_verify_pool_prod_testnet",
        "note": "动态交易品种池V2：实盘+模拟盘双重验证，每日自动更新。实盘成交额>8000万U ∪ 涨幅前10 ∪ 跌幅前10，与模拟盘（成交额>8000万U ∪ 涨幅前30 ∪ 跌幅前30）取交集",
    }

    if dry_run:
        logger.info("🔍 [DRY-RUN] 以下配置将被写入:")
        print(json.dumps(new_config, indent=2, ensure_ascii=False))
        return

    with open(SYMBOLS_FILE, "w", encoding="utf-8") as f:
        json.dump(new_config, f, indent=2, ensure_ascii=False)

    logger.info(f"✅ 已更新: {SYMBOLS_FILE}")


def print_report(final: dict, prod_sets, testnet_sets):
    """输出筛选报告"""
    prod_a, prod_b, prod_c = prod_sets
    test_a, test_b, test_c = testnet_sets

    print("\n" + "=" * 70)
    print("📊 动态交易品种池筛选报告 V2（实盘+模拟盘双重验证）")
    print(f"⏰ 更新时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)

    print("\n── 实盘数据 ──")
    print(f"  📈 集合A（成交额>1000万U）: {len(prod_a)} 个")
    print(f"  🚀 集合B（涨幅前{PROD_TOP_N}）: {len(prod_b)} 个")
    print(f"  📉 集合C（跌幅前{PROD_TOP_N}）: {len(prod_c)} 个")

    print("\n── 模拟盘数据 ──")
    print(f"  📈 集合A（成交额>1000万U）: {len(test_a)} 个")
    print(f"  🚀 集合B（涨幅前{TESTNET_TOP_N}）: {len(test_b)} 个")
    print(f"  📉 集合C（跌幅前{TESTNET_TOP_N}）: {len(test_c)} 个")

    prod_all = set(prod_a.keys()) | set(prod_b.keys()) | set(prod_c.keys())
    test_all = set(test_a.keys()) | set(test_b.keys()) | set(test_c.keys())
    print(f"\n── 双重验证 ──")
    print(f"  实盘候选: {len(prod_all)} 个")
    print(f"  模拟盘候选: {len(test_all)} 个")
    print(f"  ✅ 交集: {len(prod_all & test_all)} 个")
    print(f"  ❌ 实盘独有（排除）: {len(prod_all - test_all)} 个")
    print(f"  ❌ 模拟盘独有（排除）: {len(test_all - prod_all)} 个")
    print(f"  🎯 最终品种池: {len(final)} 个")

    print("\n── 品种池详情 ──")
    sorted_items = sorted(final.values(), key=lambda x: x["quoteVolume"], reverse=True)
    for i, item in enumerate(sorted_items, 1):
        sources = " + ".join(item["sources"])
        vol_str = f"{item['quoteVolume']:,.0f}"
        pct_str = f"{item['priceChangePercent']:+.2f}%"
        print(f"  {i:3d}. {item['symbol']:<15}  成交额: {vol_str:>15}  涨跌: {pct_str:>8}  来源: {sources}")

    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="动态交易品种池更新 V2（实盘+模拟盘双重验证）")
    parser.add_argument("--dry-run", action="store_true", help="只输出结果，不写入文件")
    parser.add_argument("--no-prod", action="store_true", help="跳过实盘，仅模拟盘（调试用）")
    args = parser.parse_args()

    logger.info("🏁 开始更新动态交易品种池 V2（双重验证）")

    # 加载 API 密钥
    api_keys = load_api_keys()

    # ── 第1步：获取实盘数据 ──
    if not args.no_prod:
        prod_tickers_raw = fetch_24h_tickers(API_URLS["prod"], api_keys, env="prod")
        prod_tickers = filter_symbols(prod_tickers_raw, env_label="实盘")
        prod_sets = compute_sets(prod_tickers, PROD_TOP_N, env_label="实盘")
    else:
        logger.info("⏭️ 跳过实盘（--no-prod）")
        prod_sets = ({}, {}, {})

    # ── 第2步：获取模拟盘数据 ──
    testnet_tickers_raw = fetch_24h_tickers(API_URLS["testnet"], api_keys, env="testnet")
    testnet_tickers = filter_symbols(testnet_tickers_raw, env_label="模拟盘")
    testnet_sets = compute_sets(testnet_tickers, TESTNET_TOP_N, env_label="模拟盘")

    # ── 第3步：双重验证筛选 ──
    final = dual_filter(prod_sets, testnet_sets)

    # ── 第4步：构建 watchlist ──
    existing_config = {}
    if SYMBOLS_FILE.exists():
        with open(SYMBOLS_FILE, "r", encoding="utf-8") as f:
            existing_config = json.load(f)
        logger.info(f"📂 已有配置: {len(existing_config.get('watchlist', []))} 个币种")

    watchlist = build_watchlist(final, existing_config)

    # ── 第5步：输出报告 ──
    print_report(final, prod_sets, testnet_sets)

    # ── 第6步：写入文件 ──
    update_symbols_file(watchlist, dry_run=args.dry_run)

    logger.info("🏁 品种池更新完成")


if __name__ == "__main__":
    main()
