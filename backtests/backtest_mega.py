#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_mega.py - 满功率 L1 确认（n≈9000，只跑便宜的L1匹配）

300 只深历史股 × 每股约30个评估点。三个配置共用同一次匹配：
  M1 等权+衰减（现行）
  M2 等权+无衰减
  M3 指数加权+衰减（旧）
n≈9000 → SE(命中)≈0.5pp，SE(IC)≈0.011，结论才有统计效力。
"""
# --- 目录引导：backtests/ 下运行也能导入 stock_gui/factor_lab，产物写项目根 ---
import os as _os_boot
import sys as _sys_boot

ROOT = _os_boot.path.dirname(_os_boot.path.dirname(
    _os_boot.path.abspath(__file__)))
if ROOT not in _sys_boot.path:
    _sys_boot.path.insert(0, ROOT)
# --- 目录引导结束 ---
# 历史结果统一归档到 research/legacy/results/（不污染仓库根目录）
_RESULTS_DIR = _os_boot.path.join(ROOT, "research", "legacy", "results")
_os_boot.makedirs(_RESULTS_DIR, exist_ok=True)

import math
import time

from backtest_levels import load_all, precompute, match_l1, wpct, rank_ic, \
    STEP, TAIL, TOPK, W
import stock_gui as sg
from stock_gui import _is_etf, CFG


def hit_stats(preds, acts):
    n = len(preds)
    hit = sum(1 for p, a in zip(preds, acts) if (p > 0) == (a > 0)) / n
    z = (hit - 0.5) / math.sqrt(0.25 / n)
    ic = rank_ic(preds, acts)
    return {"n": n, "hit": hit, "z": z, "ic": ic,
            "t_ic": ic * math.sqrt(n),
            "mae": sum(abs(p - a) for p, a in zip(preds, acts)) / n}


def main():
    t0 = time.time()
    by, meta = load_all()
    cands = [(c, r) for c, r in by.items() if len(r) >= 400 and not _is_etf(c)
             and c in meta]
    cands.sort(key=lambda cr: -len(cr[1]))
    targets = cands[:300]
    print(f"目标 {len(targets)} 只 × 步长{STEP} 回看{TAIL}")

    feats = [precompute(r) for _, r in targets]
    print(f"预计算 {len(feats)} 只 {time.time()-t0:.0f}s，开始评估...")

    M = {"M1_等权+衰减": [], "M2_等权+无衰减": [], "M3_指数加权+衰减": []}
    n_eval = 0
    for fi, fp in enumerate(feats):
        code = targets[fi][0]
        n = len(fp["closes"])
        for t in range(max(2 * W + 2, n - TAIL), n - 1, STEP):
            cur_win = fp["zwin"][t]
            if cur_win is None:
                continue
            cur_ctx = {"struct": fp["struct"][t], "vola": fp["vola"][t],
                       "rsi": fp["rsi"][t], "volchg": fp["volchg"][t],
                       "weekly": fp["weekly"][t]}
            vr_now = fp["vr"][t]
            s1 = match_l1(fp, t, cur_win, cur_ctx, vr_now)
            if len(s1) < 3:
                continue
            actual = fp["closes"][t + 1] / fp["closes"][t] - 1
            best = s1[0]["similarity_score"]
            for s in s1:
                s["weight_exp"] = math.exp(
                    -min(max(0.0, s["similarity_score"] - best), 6.0) / 0.9)

            def fuse(mode, decay=True):
                pairs = []
                for s in s1:
                    w = (s["weight"] if decay else 1.0) \
                        if mode == "eq" else s["weight_exp"]
                    pairs.append((s["n1_cl"], w))
                return wpct(pairs, 50)

            n_eval += 1
            M["M1_等权+衰减"].append((fuse("eq", True), actual))
            M["M2_等权+无衰减"].append((fuse("eq", False), actual))
            M["M3_指数加权+衰减"].append((fuse("exp", True), actual))

    print(f"评估点 {n_eval}\n")
    print(f"{'配置':<20}{'n':>6}{'方向命中':>9}{'z(vs50%)':>9}"
          f"{'IC':>9}{'t(IC)':>8}{'MAE':>8}")
    import json
    out = {}
    for name, arr in M.items():
        pr = [(p, a) for p, a in arr if p is not None]
        st = hit_stats([p for p, _ in pr], [a for _, a in pr])
        out[name] = st
        sig = "**" if abs(st["z"]) > 1.96 else ("*" if abs(st["z"]) > 1.64 else "  ")
        print(f"{name:<20}{st['n']:>6}{st['hit']*100:>8.1f}%{st['z']:>+8.2f}{sig}"
              f"{st['ic']:>+9.4f}{st['t_ic']:>+8.2f}{st['mae']*100:>7.3f}%")
    print("\n(z>1.96≈95%置信；t(IC)>2≈显著)")
    with open(_os_boot.path.join(_RESULTS_DIR, "mega_results.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"完成 {time.time()-t0:.0f}s → mega_results.json")


if __name__ == "__main__":
    main()
