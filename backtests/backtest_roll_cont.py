#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_roll_cont.py - 连续持仓口径的滚动稳健性检验（不重置仓位）

与 backtest_exit_roll.py 的切片法不同：本脚本对每个变体只跑一次
全期连续组合回测，然后在权益曲线上取滚动窗口 W 日、步长 step，
统计滚动年化 / 回撤 / Sharpe 的分布。这样不会因「每窗空仓重启」
放大或掩盖路径依赖。

用法：python backtest_roll_cont.py [--tier 激进] [--win 250] [--step 5]
"""
# --- 目录引导：backtests/ 下运行也能导入 stock_gui/factor_lab，产物写项目根 ---
import os as _os_boot
import sys as _sys_boot

ROOT = _os_boot.path.dirname(_os_boot.path.dirname(
    _os_boot.path.abspath(__file__)))
if ROOT not in _sys_boot.path:
    _sys_boot.path.insert(0, ROOT)
# --- 目录引导结束 ---
import argparse
import datetime as _dt
import json
import math
import os
import time

import numpy as np

import stock_gui as sg
from backtest_exit_roll import load_mats

# 控制参数个数：只测一条阈值曲线 + 一个退出阈值变体
VARIANTS = [
    ("Full(默认)", {}),
    ("Q25 mkt<-0.2%", {"weak_q": 25, "weak_mkt": -0.002}),
    ("Q25 mkt<-0.4%", {"weak_q": 25, "weak_mkt": -0.004}),
    ("Q25 mkt<-0.6%", {"weak_q": 25, "weak_mkt": -0.006}),
    ("Q25 mkt<-1.0%", {"weak_q": 25, "weak_mkt": -0.010}),
    ("Q25 mkt<-2.0%", {"weak_q": 25, "weak_mkt": -0.020}),
    ("Q25 mkt<-0.6%+exitP.55", {"weak_q": 25, "weak_mkt": -0.006,
                                "weak_exit_p": 0.55}),
]

# 跨档借用候选：报告 ablation 各档头部（含激进档被否/丢弃的结构）
OPT_VARIANTS = [
    ("Base(生产默认)", {}),
    ("Adaptive+Horizon", {"use_logistic": False, "use_lgbm": False}),
    ("无Q10棘轮", {"use_q10_stop": False}),
    ("LightGBM+Quantile", {"use_logistic": False, "use_adaptive": False}),
    ("Q25+Q90+NoPup+Cd10", {"stop_q": 25, "target_q": 90,
                            "use_logistic": False, "cooldown": 10}),
    ("Q25+Q90+MinHold5", {"stop_q": 25, "target_q": 90, "min_hold": 5}),
    ("T10only", {"h_only": 10, "cooldown": 5, "min_hold": 5}),
    ("Full-LightGBM", {"use_lgbm": False}),
    ("Rot-Top50", {"rot_top": 0.50}),
    ("Rot-Strong", {"rot_strong": True}),
]


def rolling_metrics(eq, dates, W=250, step=5):
    eq = np.asarray(eq, float)
    n = len(eq)
    anns, mdds, shps, rets = [], [], [], []
    for i in range(W - 1, n, step):
        seg = eq[i - W + 1:i + 1]
        if seg[0] <= 0:
            continue
        r = seg[-1] / seg[0] - 1.0
        try:
            d0 = _dt.date.fromisoformat(dates[i - W + 1])
            d1 = _dt.date.fromisoformat(dates[i])
            yrs = max((d1 - d0).days / 365.25, 1e-6)
        except Exception:
            yrs = W / 252.0
        ann = (1 + r) ** (1 / yrs) - 1 if r > -1 else -1.0
        peak, mdd = seg[0], 0.0
        for v in seg:
            peak = max(peak, v)
            if peak > 0:
                mdd = min(mdd, v / peak - 1.0)
        daily = np.diff(seg) / seg[:-1]
        sd = float(daily.std())
        shp = float(daily.mean()) / sd * math.sqrt(252.0) if sd > 1e-12 \
            else None
        anns.append(ann)
        mdds.append(mdd)
        shps.append(shp)
        rets.append(r)
    return {"anns": anns, "mdds": mdds, "shps": shps, "rets": rets}


def pct(vals, q):
    vals = [v for v in vals if v is not None]
    return float(np.percentile(vals, q)) if vals else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="激进", choices=("保守", "平衡", "激进"))
    ap.add_argument("--win", type=int, default=250)
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--set", default="stop",
                    choices=("stop", "opt", "all"))
    args = ap.parse_args()
    variants = []
    if args.set in ("stop", "all"):
        variants += VARIANTS
    if args.set in ("opt", "all"):
        variants += OPT_VARIANTS
    t0 = time.time()

    print("加载预测缓存 ...")
    mats = load_mats()
    cal = mats[0]
    tier = sg._V4_TIERS[args.tier]
    print(f"日历 {cal[0]} ~ {cal[-1]}，{len(cal)} 日；"
          f"滚动窗 {args.win} 日 / 步长 {args.step}")

    out = {}
    print(f"\n=== 连续口径滚动 ({args.win}日/{args.step}) · {args.tier}档 ===")
    print(f"{'变体':<24}{'全期年化':>9}{'全期回撤':>9}{'滚动年化P25':>12}"
          f"{'P50':>9}{'P75':>9}{'最差':>9}{'滚动回撤P50':>12}{'最差':>9}"
          f"{'SharpeP50':>10}{'正收益%':>9}")
    for name, ov in variants:
        rules = {"mode": "full", "tier_name": args.tier}
        rules.update(sg._V4_TIER_EXTRA.get(args.tier, {}))   # 生产档位默认
        rules.update(ov)
        m = sg._v4_portfolio_sim(mats, tier, rules)
        rm = rolling_metrics(m["equity"], m["dates"], args.win, args.step)
        pos = sum(1 for r in rm["rets"] if r > 0) / len(rm["rets"])
        out[name] = {"rules": ov, "ann": m["ann"], "mdd": m["mdd"],
                     "ann_p25": pct(rm["anns"], 25),
                     "ann_p50": pct(rm["anns"], 50),
                     "ann_p75": pct(rm["anns"], 75),
                     "ann_min": min(rm["anns"]),
                     "mdd_p50": pct(rm["mdds"], 50),
                     "mdd_min": min(rm["mdds"]),
                     "shp_p50": pct(rm["shps"], 50),
                     "pos_ratio": pos, "n_win": len(rm["rets"])}
        o = out[name]
        print(f"{name:<24}{m['ann']*100:>+8.1f}%{m['mdd']*100:>+8.1f}%"
              f"{o['ann_p25']*100:>+11.1f}%{o['ann_p50']*100:>+8.1f}%"
              f"{o['ann_p75']*100:>+8.1f}%{o['ann_min']*100:>+8.1f}%"
              f"{o['mdd_p50']*100:>+11.1f}%{o['mdd_min']*100:>+8.1f}%"
              f"{(o['shp_p50'] or 0):>+10.2f}{pos*100:>8.0f}%")

    here = ROOT
    path = os.path.join(here, "research",
                        f"v4_exit_roll_cont_{args.tier}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"tier": args.tier, "win": args.win, "step": args.step,
                   "ts": time.strftime("%Y-%m-%d %H:%M"), "results": out},
                  f, ensure_ascii=False, indent=1)
    print(f"\n写入 {path}，耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
