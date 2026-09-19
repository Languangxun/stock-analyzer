#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_v61.py - v6.1 标准回测（两口径 × 四类产品）

口径：
  full  全A（含沪深主板/创业板/科创板，剔除北交所/ETF）
  main  沪深主板（sh60/sz00）
产品：
  稳健 / 均衡 / 激进（组合收益，相位平均）+ 荐股（逐笔收益口径）
输出：
  research/v61_report.json  （原始指标，含基准/超额/退出原因分布）
  research/v61_report.md    （可直接嵌入 README 的表格）

用法：
  python backtest_v61.py                     # 两口径全期
  python backtest_v61.py --universe main
  python backtest_v61.py --segment val
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

HERE = ROOT
TIERS = tuple(sg.TIER_CFG)
UNI_NAME = {"all": "全A", "main": "沪深主板"}


def _run_universe(universe, segment, progress):
    progress(f"=== {UNI_NAME[universe]} · 组合回测 ===")
    tiers = sg.tier_eval(segment=segment, universe=universe, progress=progress)
    progress(f"=== {UNI_NAME[universe]} · 荐股逐笔回测 ===")
    picks = sg.tier_picks_stats(segment=segment, universe=universe,
                                progress=progress)
    return {"universe": universe, "tiers": tiers, "picks": picks}


def _tier_table(rep, tier):
    tv = rep["tiers"].get(tier)
    if not tv:
        return None
    b = tv.get("bench") or {}
    return {
        "range": tv["range"], "total": tv["total"], "ann": tv["ann"],
        "mdd": tv["mdd"], "sharpe": tv["sharpe"], "trades": tv["trades"],
        "winrate": tv["winrate"], "benchmark": tv["benchmark"],
        "bench_ann": b.get("ann"), "bench_mdd": b.get("mdd"),
        "excess_total": tv.get("excess_total"),
        "phase_ann_min": tv.get("phase_ann_min"),
        "phase_ann_max": tv.get("phase_ann_max"),
    }


def build_md(report):
    lines = [f"### v6.1 标准回测（{report['segment']}，"
             f"数据截至 {report['data_end']}）", ""]
    for uni in ("all", "main"):
        rep = report["results"].get(uni)
        if not rep:
            continue
        lines.append(f"#### {UNI_NAME[uni]} · 组合（相位平均，含全部费用）")
        lines.append("")
        lines.append("| 档位 | 区间 | 总收益 | 年化 | 最大回撤 | Sharpe | "
                     "交易 | 基准 | 基准年化 | 超额(总) | 相位年化区间 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for tier in TIERS:
            t = _tier_table(rep, tier)
            if not t:
                continue
            lines.append(
                f"| {tier} | {t['range'][0]}~{t['range'][1]} | "
                f"{(t['total'] or 0)*100:+.1f}% | {(t['ann'] or 0)*100:+.1f}% | "
                f"{(t['mdd'] or 0)*100:+.1f}% | {(t['sharpe'] or 0):+.2f} | "
                f"{t['trades']} | {t['benchmark']} | "
                f"{(t['bench_ann'] or 0)*100:+.1f}% | "
                f"{(t['excess_total'] or 0)*100:+.1f}pp | "
                f"{(t['phase_ann_min'] or 0)*100:+.1f}% ~ "
                f"{(t['phase_ann_max'] or 0)*100:+.1f}% |")
        lines.append("")
        lines.append(f"#### {UNI_NAME[uni]} · 荐股（逐笔口径）")
        lines.append("")
        lines.append("| 档位 | 推荐笔数 | 平均收益 | 中位 | 胜率 | 盈亏比 | "
                     "PF | 均持有(日) | 最好 | 最差 | >50%右尾 | 退出原因 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for tier in TIERS:
            s = rep["picks"].get(tier)
            if not s or not s.get("n"):
                lines.append(f"| {tier} | 0 | - | - | - | - | - | - | - | - | - | - |")
                continue
            r = s.get("by_reason") or {}
            lines.append(
                f"| {tier} | {s['n']} | {s['avg_ret']*100:+.2f}% | "
                f"{s['med_ret']*100:+.2f}% | {s['winrate']*100:.1f}% | "
                f"{(s['payoff'] or 0):.2f} | {(s['pf'] or 0):.2f} | "
                f"{s['avg_hold']:.1f} | {s['best']*100:+.1f}% | "
                f"{s['worst']*100:+.1f}% | {s['tail50']*100:.1f}% | "
                f"调仓{r.get('target', 0)}/闸门{r.get('gate', 0)}/"
                f"退市{r.get('delist', 0)} |")
        lines.append("")
    lines.append("- 口径：T-1 信号 → T 日收盘成交；含滑点/佣金/印花税/整手/"
                 "涨跌停/退市了结；主板激进档 β 与闸门改用上证 MA20。")
    lines.append("- 注：全部为历史统计研究，不构成投资建议。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="both",
                    choices=["both", "all", "main"])
    ap.add_argument("--segment", default="full",
                    choices=["full", "train", "val", "val2025", "bull"])
    ap.add_argument("--out", default=os.path.join(HERE, "research"))
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    unis = ["all", "main"] if args.universe == "both" else [args.universe]
    t0 = time.time()
    codes, cal, C, V = sg.tier_load_panel()
    report = {"ts": time.strftime("%Y-%m-%d %H:%M"),
              "segment": args.segment, "data_end": cal[-1],
              "results": {}}
    for uni in unis:
        report["results"][uni] = _run_universe(uni, args.segment, print)
    suffix = f"_{args.tag}" if args.tag else ""
    jpath = os.path.join(args.out, f"v61_report{suffix}.json")
    mpath = os.path.join(args.out, f"v61_report{suffix}.md")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    md = build_md(report)
    with open(mpath, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    print("\n" + md)
    print(f"写入 {jpath}")
    print(f"写入 {mpath}")
    print(f"耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
