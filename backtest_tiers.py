#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_tiers.py - v6.0 三档组合策略回测（研究脚本）

引擎权威实现内嵌在 stock_gui.py（`tier_*` 函数），本脚本只做命令行/产物封装，
保证 GUI、CLI（stock_predict.py）与研究脚本共用同一套实现，不会出现口径分叉。

策略（详见 README 与 stock_gui.py tier_* 注释）：
  稳健  全A 20日动量+20日低波 合成Top20 / 20日调仓 / 上证MA20
  均衡  同选股 Top20 / 10日调仓 / 上证MA20
  激进  创业板 60日β Top5 / 10日调仓 / 创业板指MA60

用法：
  python backtest_tiers.py --tier all --segment full
  python backtest_tiers.py --tier all --segment val
  python backtest_tiers.py --tier all --segment yearly
  python backtest_tiers.py --tier 激进 --segment full --gate-ma 20   # 参数敏感性
"""
import argparse
import json
import os
import time

import stock_gui as sg

TIERS = tuple(sg.TIER_CFG)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="all", choices=list(TIERS) + ["all"])
    ap.add_argument("--segment", default="full",
                    choices=["full", "train", "val", "val2025", "bull",
                             "year", "yearly"])
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--phases", type=int, default=0, help="0=按各档 reb")
    ap.add_argument("--tag", default="")
    ap.add_argument("--score", default=None, choices=["blend", "beta"])
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--reb", type=int, default=None)
    ap.add_argument("--gate-ma", type=int, default=None)
    ap.add_argument("--no-gate", action="store_true")
    args = ap.parse_args()

    overrides = {}
    if args.score:
        overrides["score"] = args.score
    if args.top:
        overrides["top"] = args.top
    if args.reb:
        overrides["reb"] = args.reb
    if args.gate_ma:
        overrides["ma"] = args.gate_ma
    if args.no_gate:
        overrides["gate"] = None
    tiers = list(TIERS) if args.tier == "all" else [args.tier]
    t0 = time.time()
    here = os.path.dirname(os.path.abspath(__file__))

    def show(tier, m):
        b = m.get("bench") or {}
        print(f"[{tier}] {m['range'][0]} ~ {m['range'][1]}  "
              f"年化 {m['ann']*100:+.1f}%  回撤 {m['mdd']*100:+.1f}%  "
              f"Sharpe {(m['sharpe'] or 0):+.2f}  交易 {m['trades']}")
        if b.get("ann") is not None:
            print(f"  基准 {m['benchmark']}  年化 {b['ann']*100:+.1f}%  "
                  f"回撤 {b['mdd']*100:+.1f}%  "
                  f"超额 {(m['excess_total'] or 0)*100:+.1f}pp  "
                  f"（相位区间 {m['phase_ann_min']*100:+.1f}% ~ "
                  f"{m['phase_ann_max']*100:+.1f}%）")

    if args.segment == "yearly":
        out = {t: [] for t in tiers}
        for y in (2022, 2023, 2024, 2025, 2026):
            print(f"\n===== {y} =====")
            for tier in tiers:
                m = sg.tier_eval(segment=str(y), tiers=[tier],
                                 phases=args.phases or None,
                                 overrides=overrides or None).get(tier)
                if m:
                    m["year"] = y
                    out[tier].append(m)
                    show(tier, m)
        path = os.path.join(here, "research", "tiers_yearly.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.strftime("%Y-%m-%d %H:%M"),
                       "note": "逐年（2022 从 09-01 起；相位平均）",
                       "results": out}, f, ensure_ascii=False, indent=1)
        print(f"\n写入 {path}，耗时 {time.time() - t0:.0f}s")
        return

    if args.segment == "year":
        if not args.year:
            raise SystemExit("--segment year 需要 --year")
        seg = str(args.year)
    else:
        seg = args.segment
    res = sg.tier_eval(segment=seg, tiers=tiers,
                       phases=args.phases or None,
                       overrides=overrides or None, progress=print)
    print(f"\n=== v6.0 三档回测 {seg} （相位平均，含全部费用）===")
    for tier in tiers:
        m = res.get(tier)
        if not m:
            continue
        b = m.get("bench") or {}
        m["excess_ann"] = (m["ann"] - b["ann"]) \
            if b.get("ann") is not None else None
        show(tier, m)
    suffix = f"_{args.tag}" if args.tag else ""
    path = os.path.join(here, "research", f"tiers_{seg}{suffix}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%d %H:%M"),
                   "segment": seg,
                   "phases_note": "phase-averaged (tranche) primary",
                   "overrides": overrides,
                   "results": res}, f, ensure_ascii=False, indent=1)
    print(f"\n写入 {path}，耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
