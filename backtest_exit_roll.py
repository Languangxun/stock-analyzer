#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_exit_roll.py - 退出变体的滚动窗口 / 分年度稳健性复核

⚠️ 注意：本脚本的滚动窗是「每窗空仓重启」的切片口径，会与连续持仓口径
产生明显偏差（实证出现全期 -3.5% vs 滚动中位 +19.4%）。稳健性结论请以
backtest_roll_cont.py（连续权益曲线滚动）为准，本脚本仅作补充参考。

复用 research/v4_preds.pkl，只重跑决策层。三种检验：
  1. 分年度：全年收益 + 年内最大回撤（仅参考，末年/首年为不完整年）；
  2. 滚动窗口：窗口长 --win 交易日、步长 --step，统计各变体
     年化中位/最差、Calmar 中位、正收益窗口占比；
  3. Walk-forward 选型：窗口 k 内选最优变体（按 --select 指标），
     看它在窗口 k+1 的样本外表现，与固定默认/事后最优对比。

用法：python backtest_exit_roll.py [--tier 激进] [--win 250] [--step 60]
"""
import argparse
import json
import os
import pickle
import time

import numpy as np

import stock_gui as sg

# 候选：默认 + regime 止损-only 头部（激进档），Q25+Q90 保留为对照
VARIANTS = [
    ("Full(默认)", {}),
    ("Q25 mkt<-0.4%", {"weak_q": 25, "weak_mkt": -0.004}),
    ("Q25 mkt<-1.0%", {"weak_q": 25, "weak_mkt": -0.010}),
    ("Q25 mkt<-2.0%", {"weak_q": 25, "weak_mkt": -0.020}),
    ("Q25 mkt<0", {"weak_q": 25, "weak_mkt": 0.0}),
    ("Q50 mkt<-1.0%", {"weak_q": 50, "weak_mkt": -0.010}),
    ("Q25+Q90(对照)", {"stop_q": 25, "target_q": 90}),
]


def load_mats():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "research", "v4_preds.pkl"), "rb") as f:
        preds = pickle.load(f)["preds"]
    with sg.db_conn() as conn:
        ind_of = {c: (i or "") for c, i in
                  conn.execute("SELECT code, industry FROM stocks")}
    mkt, ind = sg._v4_mkt_ind_ctx(ind_of)
    mats = sg._v4_stack(preds)
    sg._v4_attach_rotation(mats[1], mats[0], mats[2], ind_of, mkt, ind)
    import datetime as _dt
    last_d = max(r["dates"][-1] for r in preds)
    _ld = _dt.date.fromisoformat(last_d)
    rows_bt = [k for k, r in enumerate(preds)
               if (_ld - _dt.date.fromisoformat(r["dates"][-1])).days <= 45]
    mats_bt = sg._v4_stack_subset([preds[k] for k in rows_bt])
    sg._v4_attach_rotation(mats_bt[1], mats_bt[0], mats_bt[2], ind_of, mkt, ind)
    return mats_bt


def slice_mats(mats, i0, i1):
    cal, M, codes = mats
    return cal[i0:i1], {k: v[:, i0:i1] for k, v in M.items()}, codes


def run_variants(mats, tier_name):
    tier = sg._V4_TIERS[tier_name]
    out = {}
    for name, ov in VARIANTS:
        rules = {"mode": "full", "tier_name": tier_name}
        rules.update(ov)
        out[name] = sg._v4_portfolio_sim(mats, tier, rules)
    return out


def per_year(m):
    eq, ds = m.get("equity"), m.get("dates")
    if not eq or not ds:
        return {}
    groups = {}
    for i, d in enumerate(ds):
        groups.setdefault(d[:4], []).append(i)
    out = {}
    for y, idx in sorted(groups.items()):
        i0, i1 = idx[0], idx[-1]
        r = (eq[i1] / eq[i0] - 1.0) if eq[i0] > 0 else None
        peak, mdd = eq[i0], 0.0
        for j in range(i0, i1 + 1):
            peak = max(peak, eq[j])
            if peak > 0:
                mdd = min(mdd, eq[j] / peak - 1.0)
        out[y] = {"ret": r, "mdd": mdd, "days": len(idx)}
    return out


def med(vals):
    vals = [v for v in vals if v is not None]
    return float(np.median(vals)) if vals else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="激进", choices=("保守", "平衡", "激进"))
    ap.add_argument("--win", type=int, default=250)
    ap.add_argument("--step", type=int, default=60)
    args = ap.parse_args()
    t0 = time.time()

    print("加载预测缓存 ...")
    mats = load_mats()
    cal = mats[0]
    nc = len(cal)
    print(f"日历 {cal[0]} ~ {cal[-1]}，{nc} 个交易日；候选 {len(VARIANTS)} 个")

    # ---- 1) 全窗 + 分年度 ----
    full = run_variants(mats, args.tier)
    years = sorted({d[:4] for d in cal})
    print(f"\n=== 分年度（{args.tier}档；不完整年仅供参考）===")
    hdr = "变体".ljust(24) + "".join(f"{y:>18}" for y in years)
    print(hdr)
    print("".ljust(24) + "".join(f"{'收益/回撤':>18}" for _ in years))
    full_y = {}
    for name, _ in VARIANTS:
        py = per_year(full[name])
        full_y[name] = py
        cells = []
        for y in years:
            e = py.get(y)
            cells.append(f"{e['ret']*100:+.1f}%/{e['mdd']*100:+.1f}%"
                         if e and e["ret"] is not None else "-")
        print(name.ljust(24) + "".join(f"{c:>18}" for c in cells))

    # ---- 2) 滚动窗口 ----
    K = (nc - args.win) // args.step + 1
    print(f"\n=== 滚动窗口 {args.win}日 / 步长 {args.step}（{K} 个窗口）===")
    win_res = []
    for k in range(K):
        i0 = k * args.step
        i1 = i0 + args.win
        m_w = run_variants(slice_mats(mats, i0, i1), args.tier)
        win_res.append({"i0": i0, "start": cal[i0], "end": cal[i1 - 1],
                        "res": m_w})
    print(f"{'变体':<24}{'年化中位':>9}{'最差年化':>9}{'Calmar中位':>11}"
          f"{'正收益窗':>9}{'回撤中位':>9}{'最差回撤':>9}")
    summary = {}
    for name, _ in VARIANTS:
        anns = [w["res"][name]["ann"] for w in win_res]
        mdds = [w["res"][name]["mdd"] for w in win_res]
        cals = [w["res"][name]["calmar"] for w in win_res]
        pos = sum(1 for a in anns if a > 0) / len(anns)
        summary[name] = {"ann_med": med(anns), "ann_min": min(anns),
                         "cal_med": med(cals),
                         "pos_ratio": pos, "mdd_med": med(mdds),
                         "mdd_min": min(mdds)}
        s = summary[name]
        print(f"{name:<24}{s['ann_med']*100:>+8.1f}%{s['ann_min']*100:>+8.1f}%"
              f"{s['cal_med']:>+11.2f}{pos*100:>8.0f}%{s['mdd_med']*100:>+8.1f}%"
              f"{s['mdd_min']*100:>+8.1f}%")

    # ---- 3) Walk-forward 选型 ----
    print(f"\n=== Walk-forward 选型（窗口k内选最优 → 看窗口k+1）===")
    wf_out = {}
    for metric in ("ann", "calmar"):
        oos_anns, picks = [], []
        for k in range(K - 1):
            cur, nxt = win_res[k], win_res[k + 1]
            best = max(VARIANTS, key=lambda kv: cur["res"][kv[0]][metric]
                       if cur["res"][kv[0]][metric] is not None else -9e9)[0]
            picks.append((best, cur["start"], nxt["start"],
                          nxt["res"][best]["ann"]))
            oos_anns.append(nxt["res"][best]["ann"])
        dflt = [win_res[k + 1]["res"]["Full(默认)"]["ann"]
                for k in range(K - 1)]
        best_fixed = max(
            VARIANTS,
            key=lambda kv: summary[kv[0]]["ann_med"])[0]
        fixed_oos = [win_res[k + 1]["res"][best_fixed]["ann"]
                     for k in range(K - 1)]
        print(f"[按 {metric} 选] OOS年化中位={med(oos_anns)*100:+.1f}% "
              f"均值={np.mean(oos_anns)*100:+.1f}% | "
              f"默认Full OOS中位={med(dflt)*100:+.1f}% | "
              f"事后最优固定({best_fixed}) OOS中位={med(fixed_oos)*100:+.1f}%")
        for b, s0, s1, a in picks:
            print(f"   {s0} 选 {b:<22} → {s1} 年化 {a*100:+.1f}%")
        wf_out[metric] = {"oos_ann_med": med(oos_anns),
                          "oos_ann_mean": float(np.mean(oos_anns)),
                          "default_med": med(dflt),
                          "picks": picks}

    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "research", f"v4_exit_roll_{args.tier}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"tier": args.tier, "win": args.win, "step": args.step,
                   "ts": time.strftime("%Y-%m-%d %H:%M"),
                   "years": full_y, "windows": summary,
                   "wf": wf_out}, f, ensure_ascii=False, indent=1)
    print(f"\n写入 {path}，耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
