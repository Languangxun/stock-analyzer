#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_picks_v6.py - v6.1 荐股收益回测（按风险偏好）

引擎：stock_gui.py 内嵌 tier_*（与 GUI/CLI 同源）。
口径：T-1 打分 → T 日收盘买入；跌出 TopN / 闸门关闭 / 退市 → 平仓；
      含滑点/佣金/印花税/整手；相位平均；同一相位内每笔等权。
输出：每档逐笔统计（推荐笔数/平均收益/胜率/盈亏比/持有期/右尾）+ JSON。

用法：
  python backtest_picks_v6.py --segment full
  python backtest_picks_v6.py --segment full --universe main
  python backtest_picks_v6.py --segment val --tier 激进
  python backtest_picks_v6.py --yearly
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

TIERS = tuple(sg.TIER_CFG)
SEGS = ("full", "val", "bull", "2023", "2024", "2025", "2026")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--segment", default="full", choices=SEGS)
    ap.add_argument("--tier", default="all", choices=list(TIERS) + ["all"])
    ap.add_argument("--yearly", action="store_true")
    ap.add_argument("--tag", default="")
    ap.add_argument("--universe", default="all", choices=["all", "main"])
    args = ap.parse_args()
    tiers = list(TIERS) if args.tier == "all" else [args.tier]
    t0 = time.time()
    here = ROOT
    usuffix = "" if args.universe == "all" else "_main"
    if args.yearly:
        out = {}
        for y in ("2022", "2023", "2024", "2025", "2026"):
            out[y] = sg.tier_picks_stats(segment=y, tiers=tiers, progress=print,
                                         universe=args.universe)
        path = os.path.join(here, "research",
                            f"picks_v6_yearly{usuffix}.json")
    else:
        out = {args.segment: sg.tier_picks_stats(segment=args.segment,
                                                 tiers=tiers, progress=print,
                                                 universe=args.universe)}
        suffix = f"_{args.tag}" if args.tag else ""
        path = os.path.join(here, "research",
                            f"picks_v6_{args.segment}{usuffix}{suffix}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%d %H:%M"),
                   "universe": args.universe,
                   "note": "v6.1 荐股收益回测（逐笔、相位平均、含费用）",
                   "results": out}, f, ensure_ascii=False, indent=1)
    print(sg.tier_picks_report_text(segment=args.segment, tiers=tiers,
                                    universe=args.universe)
          if not args.yearly else "逐年结果见 JSON")
    print(f"写入 {path}，耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
