#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_picks10.py - v6.2 10万元荐股组合回测（IC确认 · Top10）

引擎：stock_gui.py 内嵌 tier_*（与 GUI/CLI 同源，ic_filter 确认层）。
口径：10 万元**组合**总资金（非每只 10 万）——每档按 IC 确认 + 综合分取
      Top10、每只约 1/10 仓位（约 1 万元），T-1 决策 → T 收盘成交；
      滑点/佣金/印花税/过户费/整手/涨跌停/停牌/退市全约束；
      调仓频率按档位各自节奏（稳健 20 日 / 均衡 10 日 / 激进 10 日），
      相位平均消除起始日运气；确认不足时宁缺毋滥（少于 10 只→留现金）。
荐股逻辑（v6.2.0）：先过个股历史信号 IC 确认层（tier_ic_confirm：120 日窗
      「近5日收益→未来5日收益」Pearson IC>0 且 t≥2 且样本≥60，且 T-1 日
      MA20/60 多头趋势在场——IC 与指标综合考量、自然数阈值防过拟合），
      再按档位综合打分排序取 Top10。

用法：
  python backtest_picks10.py --segment full            # 三档全期
  python backtest_picks10.py --segment val --tier 稳健
  python backtest_picks10.py --yearly
  python backtest_picks10.py --segment full --capital 1000000  # 本金敏感性
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
import json
import os
import time

import stock_gui as sg

# 用户口径：三档各自节奏（高风险档为板块轮动口径，不在本回测默认范围）
TIERS3 = ("稳健", "均衡", "激进")
SEGS = ("full", "train", "val", "val2025", "bull", "year", "yearly")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--segment", default="full", choices=SEGS)
    ap.add_argument("--tier", default="all",
                    choices=list(TIERS3) + ["高风险", "all"])
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--capital", type=float, default=100000.0,
                    help="组合总本金（默认 10 万，非每只）")
    ap.add_argument("--top", type=int, default=10,
                    help="每期持仓只数（默认 10，每只约 1/top 仓）")
    ap.add_argument("--tag", default="")
    ap.add_argument("--universe", default="all",
                    choices=["all", "main", "etf", "all_etf"])
    args = ap.parse_args()
    tiers = list(TIERS3) if args.tier == "all" else [args.tier]
    t0 = time.time()
    here = ROOT
    usuffix = "" if args.universe == "all" else f"_{args.universe}"
    csuffix = "" if abs(args.capital - 100000.0) < 1e-6 \
        else f"_c{int(args.capital)}"
    tsuffix = "" if args.tier == "all" else f"_{args.tier}"

    def run(seg):
        return sg.tier_eval(segment=seg, tiers=tiers,
                            overrides={"top": args.top},
                            universe=args.universe, ic_filter=True,
                            capital=args.capital, progress=print)

    def show(tier, m):
        b = m.get("bench") or {}
        print(f"[{tier}] {m['range'][0]} ~ {m['range'][1]}  "
              f"总 {m['total']*100:+.1f}%  年化 {m['ann']*100:+.1f}%  "
              f"回撤 {m['mdd']*100:+.1f}%  Sharpe {(m['sharpe'] or 0):+.2f}  "
              f"交易 {m['trades']}")
        if b.get("ann") is not None:
            print(f"  基准 {m['benchmark']}  年化 {b['ann']*100:+.1f}%  "
                  f"超额 {(m['excess_total'] or 0)*100:+.1f}pp  "
                  f"（相位区间 {m['phase_ann_min']*100:+.1f}% ~ "
                  f"{m['phase_ann_max']*100:+.1f}%）")

    if args.segment == "yearly":
        out = {t: [] for t in tiers}
        for y in (2022, 2023, 2024, 2025, 2026):
            print(f"\n===== {y} =====")
            for tier in tiers:
                m = run(str(y)).get(tier)
                if m:
                    m["year"] = y
                    out[tier].append(m)
                    show(tier, m)
        path = os.path.join(here, "research",
                            f"picks10_yearly{usuffix}{csuffix}{tsuffix}.json")
    else:
        seg = str(args.year) if args.segment == "year" else args.segment
        out = {seg: run(seg)}
        print(f"\n=== v6.2 10万元荐股组合回测 {seg} · "
              f"{sg.UNIVERSE_NAME.get(args.universe, args.universe)} · "
              f"本金 {args.capital:,.0f} · Top{args.top} · IC确认层开 "
              f"（相位平均，含全部费用）===")
        for tier in tiers:
            m = out[seg].get(tier)
            if m:
                show(tier, m)
        suffix = f"_{args.tag}" if args.tag else ""
        path = os.path.join(here, "research",
                            f"picks10_{seg}{usuffix}{csuffix}{tsuffix}"
                            f"{suffix}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%d %H:%M"),
                   "app_version": sg.APP_VERSION,
                   "note": "10万元组合荐股回测：IC确认层+综合分Top10，"
                           "每只约1/top仓，T-1决策→T收盘成交，相位平均",
                   "capital": args.capital, "top": args.top,
                   "ic_filter": True, "tiers": tiers,
                   "universe": args.universe,
                   "overrides": {"top": args.top},
                   "results": out}, f, ensure_ascii=False, indent=1)
    print(f"\n写入 {path}，耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
