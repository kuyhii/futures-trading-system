#!/usr/bin/env python3
"""
orchestrator.py - 主调度器（Phase 5）
职责: 协调数据采集、策略计算、风控巡检的循环执行
通过 OpenClaw cron 触发，每次运行完成一个完整周期

使用方式:
  python3 scripts/orchestrator.py          # 完整周期
  python3 scripts/orchestrator.py data     # 仅数据采集
  python3 scripts/orchestrator.py strategy # 仅策略计算
  python3 scripts/orchestrator.py risk     # 仅风控巡检
"""

import json
import os
import sys
import subprocess
from datetime import datetime, timezone

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(WORKSPACE, "config")
SCRIPTS_DIR = os.path.join(WORKSPACE, "scripts")
STATE_DIR = os.path.join(WORKSPACE, "state")
LOG_FILE = os.path.join(WORKSPACE, "logs", "system.log")

# ── 加载 .env 环境变量 ──
def load_env():
    env_file = os.path.join(WORKSPACE, ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip()
                    if key and val and key not in os.environ:
                        os.environ[key] = val

load_env()

# 环境标识确认
BINANCE_API_ENV = os.environ.get("BINANCE_API_ENV", "testnet")
if BINANCE_API_ENV == "prod":
    BINANCE_API_KEY = os.environ.get("BINANCE_PROD_API_KEY", "")
else:
    BINANCE_API_KEY = os.environ.get("BINANCE_TESTNET_API_KEY", "")

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [orchestrator] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def run_script(script_name, args=None):
    """执行脚本并返回结果"""
    cmd = ["bash", os.path.join(SCRIPTS_DIR, script_name)]
    if args:
        cmd.extend(args)

    log(f"执行: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                log(f"  stdout: {line}")
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                log(f"  stderr: {line}")
        return result.returncode == 0, result.stdout
    except subprocess.TimeoutExpired:
        log(f"⚠️  脚本超时: {script_name}")
        return False, "timeout"
    except Exception as e:
        log(f"❌ 脚本异常: {e}")
        return False, str(e)

def run_python_script(script_name, args=None):
    """执行 Python 脚本"""
    cmd = ["python3", os.path.join(SCRIPTS_DIR, script_name)]
    if args:
        cmd.extend(args)

    log(f"执行: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                log(f"  stdout: {line}")
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                log(f"  stderr: {line}")
        return result.returncode == 0, result.stdout
    except subprocess.TimeoutExpired:
        log(f"⚠️  脚本超时: {script_name}")
        return False, "timeout"
    except Exception as e:
        log(f"❌ 脚本异常: {e}")
        return False, str(e)

def update_daily_pnl():
    """更新当日盈亏快照"""
    daily_file = os.path.join(STATE_DIR, "daily_pnl.json")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    data = {}
    if os.path.exists(daily_file):
        with open(daily_file) as f:
            data = json.load(f)

    if data.get("date") != today:
        # 新的一天，重置
        data = {
            "date": today,
            "total_pnl": 0.0,
            "trades_count": 0,
            "wins": 0,
            "losses": 0,
            "max_drawdown": 0.0,
        }

    with open(daily_file, "w") as f:
        json.dump(data, f, indent=2)

    return data

def pipeline_full():
    """完整周期: 数据采集 → 策略计算 → 风控巡检"""
    log("=" * 60)
    log("🚀 完整周期开始")
    log("=" * 60)

    results = {}

    # Step 1: 数据采集
    log("─── Step 1: 数据采集 ───")
    ok, output = run_script("fetch_data.sh", ["all"])
    results["data_fetch"] = ok

    # Step 2: 策略计算
    log("─── Step 2: 策略计算 ───")
    ok, output = run_python_script("strategy_engine.py")
    results["strategy"] = ok

    # Step 3: 风控巡检
    log("─── Step 3: 风控巡检 ───")
    ok, output = run_script("check_risk.sh")
    results["risk_check"] = ok

    # Step 4: 更新当日盈亏
    log("─── Step 4: 更新当日盈亏 ───")
    update_daily_pnl()
    results["pnl_update"] = True

    # 汇总
    all_ok = all(results.values())
    log("=" * 60)
    log(f"周期完成: {'✅ 全部成功' if all_ok else '⚠️ 部分失败'}")
    for step, ok in results.items():
        log(f"  {step}: {'✅' if ok else '❌'}")
    log("=" * 60)

    return all_ok, results

def pipeline_data_only():
    """仅数据采集"""
    log("─── 数据采集周期 ───")
    ok, output = run_script("fetch_data.sh", ["all"])
    return ok, {"data_fetch": ok}

def pipeline_strategy_only():
    """仅策略计算"""
    log("─── 策略计算周期 ───")
    ok, output = run_python_script("strategy_engine.py")
    return ok, {"strategy": ok}

def pipeline_risk_only():
    """仅风控巡检"""
    log("─── 风控巡检周期 ───")
    ok, output = run_script("check_risk.sh")
    return ok, {"risk_check": ok}

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"

    if mode == "full":
        return pipeline_full()
    elif mode == "data":
        return pipeline_data_only()
    elif mode == "strategy":
        return pipeline_strategy_only()
    elif mode == "risk":
        return pipeline_risk_only()
    else:
        log(f"未知模式: {mode}")
        log("可用模式: full, data, strategy, risk")
        return False, {}

if __name__ == "__main__":
    ok, results = main()
    sys.exit(0 if ok else 1)
