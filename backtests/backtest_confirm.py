#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_confirm.py - 大样本确认回测（n≈1000+，带显著性检验）

复检四个生产决策：
  C1 prod    : L1+L2 等权+衰减（现行）
  C2 exp     : L1+L2 指数加权+衰减（旧版）
  C3 L1only  : 仅L1 等权+衰减
  C4 nodecay : L1+L2 等权无衰减
样本复用：每个评估点只匹配一次 L1/L2，四个配置共享。
检验：方向命中 vs 50% 的 z 值；IC 的 t = IC*sqrt(n)。
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

from backtest_levels import (load_all, precompute, match_pool, match_l1,
                             wpct, rank_ic, STEP, TAIL, TOPK, W)
import stock_gui as sg
from stock_gui import _is_etf, CFG

TARGET_N, POOL_N = 40, 250


def hit_stats(preds, acts):
    n = len(preds)
    hit = sum(1 for p, a in zip(preds, acts) if (p > 0) == (a > 0)) / n
    z = (hit - 0.5) / math.sqrt(0.25 / n)
    ic = rank_ic(preds, acts)
    t_ic = ic * math.sqrt(n)
    mae = sum(abs(p - a) for p, a in zip(preds, acts)) / n
    return {"n": n, "hit": hit, "z": z, "ic": ic, "t_ic": t_ic, "mae": mae}


def fuse_w(levels, mode):
    levels = [(k, s) for k, s in levels if s]
    if not levels:
        return None
    LW = sg._dynamic_lv_weights(levels)
    tot = sum(LW.get(k, 0.0) for k, _ in levels) or 1.0
    pairs = []
    for k, smp in levels:
        lw = LW.get(k, 0.0) / tot
        if mode == "exp":
            tw = sum(s["weight_exp"] for s in smp) or 1.0
            pairs.extend((s["n1_cl"], lw * s["weight_exp"] / tw) for s in smp)
        else:
            tw = sum(s["weight"] for s in smp) or 1.0
            pairs.extend((s["n1_cl"], lw * s["weight"] / tw) for s in smp)
    return wpct(pairs, 50)


def main():
    t0 = time.time()
    by, meta = load_all()
    cands = [(c, r) for c, r in by.items() if len(r) >= 400 and not _is_etf(c)
             and c in meta]
    cands.sort(key=lambda cr: -len(cr[1]))
    pool = cands[:POOL_N]
    targets = cands[:TARGET_N]
    print(f"目标 {TARGET_N} 只 | 池 {POOL_N} 只 | 步长{STEP} 回看{TAIL}")

    feats = {}
    def F(c):
        if c not in feats:
            feats[c] = precompute(by[c])
        return feats[c]
    for c, _ in pool:
        F(c)
    print(f"预计算 {len(feats)} 只 {time.time()-t0:.0f}s，开始评估...")

    C = {"C1_prod(等权+衰减+L2)": [], "C2_exp(指数加权+L2)": [],
         "C3_L1only": [], "C4_nodecay(等权无衰减+L2)": []}
    n_eval = 0
    for code, rows in targets:
        m = meta[code]
        _, _, ind, cap, tier = m
        fp = F(code)
        lg = math.log(max(cap or 1e8, 1e8))
        peers = [c for c, _ in pool if c != code and meta[c][2] == ind]
        peers.sort(key=lambda c: abs(math.log(
            max(meta[c][3] or 1e8, 1e8)) - lg))
        peers = peers[:40]
        for c in peers:
            F(c)
        n = len(fp["closes"])
        for t in range(max(2 * W + 2, n - TAIL), n - 1, STEP):
            d = fp["dates"][t]
            cur_win = fp["zwin"][t]
            if cur_win is None:
                continue
            cur_ctx = {"struct": fp["struct"][t], "vola": fp["vola"][t],
                       "rsi": fp["rsi"][t], "volchg": fp["volchg"][t],
                       "weekly": fp["weekly"][t]}
            vr_now = fp["vr"][t]
            s1 = match_l1(fp, t, cur_win, cur_ctx, vr_now)
            s2 = []
            for pc in peers:
                s2.extend(match_pool(F(pc), d, cur_win, cur_ctx, vr_now))
            s2.sort(key=lambda s: s["similarity_score"]); s2 = s2[:TOPK]
            # 双权重：等权(带衰减) 与 指数(带衰减)
            best = min((s["similarity_score"] for s in s1 + s2), default=0)
            for s in s1 + s2:
                s["weight_exp"] = math.exp(
                    -min(max(0.0, s["similarity_score"] - best), 6.0) / 0.9)
            actual = fp["closes"][t + 1] / fp["closes"][t] - 1
            n_eval += 1
            C["C1_prod(等权+衰减+L2)"].append(
                (fuse_w([("L1", s1), ("L2", s2)], "eq"), actual))
            C["C2_exp(指数加权+L2)"].append(
                (fuse_w([("L1", s1), ("L2", s2)], "exp"), actual))
            C["C3_L1only"].append((fuse_w([("L1", s1)], "eq"), actual))
            # 无衰减：把权重重置为1再融合
            save = [(s, s["weight"]) for s in s1 + s2]
            for s in s1 + s2:
                s["weight"] = 1.0
            C["C4_nodecay(等权无衰减+L2)"].append(
                (fuse_w([("L1", s1), ("L2", s2)], "eq"), actual))
            for s, w0 in save:
                s["weight"] = w0

    print(f"评估点 {n_eval}\n")
    print(f"{'配置':<28}{'n':>5}{'方向命中':>9}{'z(vs50%)':>9}"
          f"{'IC':>9}{'t(IC)':>8}{'MAE':>8}")
    import json
    out = {}
    for name, arr in C.items():
        pr = [(p, a) for p, a in arr if p is not None]
        if len(pr) < 50:
            print(f"{name:<28} 样本不足({len(pr)})")
            continue
        st = hit_stats([p for p, _ in pr], [a for _, a in pr])
        out[name] = st
        sig = "**" if abs(st["z"]) > 1.96 else ("*" if abs(st["z"]) > 1.64 else "  ")
        print(f"{name:<28}{st['n']:>5}{st['hit']*100:>8.1f}%{st['z']:>+8.2f}{sig}"
              f"{st['ic']:>+9.4f}{st['t_ic']:>+8.2f}{st['mae']*100:>7.3f}%")
    print("\n(z>1.96≈95%置信, >1.64≈90%置信；t(IC)>2≈显著)")
    with open(_os_boot.path.join(_RESULTS_DIR, "confirm_results.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"完成 {time.time()-t0:.0f}s → confirm_results.json")


if __name__ == "__main__":
    main()
