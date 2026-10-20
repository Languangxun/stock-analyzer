#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compare_tier_sm701.py - 三档生产策略 vs sm701 评分（全库口径）

口径：
  · 同一份全库日K面板、同一区间（默认 sm701 历史预测起点 2024-03-12 至今）；
  · 当前三档 = 稳健/均衡/激进原配置（评分/闸门/调仓/IC/熔断原样）；
  · sm701 三档 = 仅把 score 换成 sm701 概率，其余风险控制完全保留；
  · 另跑 test（sm701 原生 Top10/每日/无闸门）作模型原始口径参照。

用法：
  python backtests/compare_tier_sm701.py                 # 全A + 主板
  python backtests/compare_tier_sm701.py --universe all
  python backtests/compare_tier_sm701.py --segment val
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import stock_gui as sg  # noqa: E402

TIERS = ["稳健", "均衡", "激进"]


def _fmt(v, pct=False, nd=2):
    if v is None:
        return "-"
    if pct:
        return f"{v * 100:+.{nd}f}%"
    return f"{v:.{nd}f}"


def _row(name, m):
    return {
        "name": name,
        "total": m.get("total"),
        "ann": m.get("ann"),
        "mdd": m.get("mdd"),
        "sharpe": m.get("sharpe"),
        "winrate": m.get("winrate"),
        "pf": m.get("pf"),
        "trades": m.get("trades"),
        "excess_total": m.get("excess_total"),
        "phase_ann_min": m.get("phase_ann_min"),
        "phase_ann_max": m.get("phase_ann_max"),
        "range": m.get("range"),
    }


def _print_table(universe, seg, rows):
    print(f"\n=== {sg.UNIVERSE_NAME.get(universe, universe)} · {seg} ===")
    head = (f"{'策略':<18}{'总收益':>9}{'年化':>9}{'回撤':>9}"
            f"{'Sharpe':>8}{'胜率':>7}{'PF':>7}{'交易':>7}")
    print(head)
    print("-" * len(head))
    for r in rows:
        print(f"{r['name']:<18}{_fmt(r['total'], True):>9}"
              f"{_fmt(r['ann'], True):>9}{_fmt(r['mdd'], True):>9}"
              f"{_fmt(r['sharpe']):>8}{_fmt(r['winrate'], True, 1):>7}"
              f"{_fmt(r['pf']):>7}{r['trades'] or 0:>7}")


def _run(universe, segment, progress):
    base = sg.tier_eval(segment=segment, universe=universe,
                        tiers=TIERS, progress=progress)
    sm = sg.tier_eval(segment=segment, universe=universe, tiers=TIERS,
                      overrides={"score": "sm701",
                                 "score_start_from_data": True},
                      progress=progress)
    raw = sg.tier_eval(segment=segment, universe=universe, tiers=["test"],
                       progress=progress)
    rows = []
    for t in TIERS:
        if t in base:
            rows.append(_row(f"{t}·当前", base[t]))
        if t in sm:
            rows.append(_row(f"{t}·sm701", sm[t]))
    if "test" in raw:
        rows.append(_row("test·sm701原生", raw["test"]))
    return {"baseline": base, "sm701": sm, "raw_test": raw, "rows": rows}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--segment", default="sm701",
                    help="区间：sm701(默认)/full/val/bull/2024...")
    ap.add_argument("--universe", default="all",
                    help="口径：all/main/etf/all_etf；逗号分隔可跑多个")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    universes = [u.strip() for u in args.universe.split(",") if u.strip()]
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(ROOT, "research")
    os.makedirs(out_dir, exist_ok=True)
    payload = {"ts": ts, "segment": args.segment, "results": {}}

    def progress(s):
        print(s, flush=True)

    for u in universes:
        res = _run(u, args.segment, progress)
        payload["results"][u] = {
            "rows": res["rows"],
            "baseline": {k: _row(k, v) for k, v in res["baseline"].items()},
            "sm701": {k: _row(k, v) for k, v in res["sm701"].items()},
            "raw_test": {k: _row(k, v) for k, v in res["raw_test"].items()},
        }
        _print_table(u, args.segment, res["rows"])

    out = args.out or os.path.join(
        out_dir, f"compare_tier_sm701_{ts}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    latest = os.path.join(out_dir, "compare_tier_sm701.json")
    with open(latest, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n结果已写入 {out}\n最新副本 {latest}")


if __name__ == "__main__":
    main()
