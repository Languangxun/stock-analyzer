#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_l3risk.py - L3 重建消融：同市值层(旧) vs 同风险组(新)

风险组定义（按东财行业名关键词）：
  tech_med: 医药+科技（软件/半导体/电子/通信/医药/生物/医疗…）
  fin:      证券+银行+保险+多元金融
对照组：L1 / L1+L2 / L1+L2+L3旧(同市值层) / L1+L2+L3风险组 / L1+L3风险组
"""
# --- 目录引导：backtests/ 下运行也能导入 stock_gui/factor_lab，产物写项目根 ---
import os as _os_boot
import sys as _sys_boot

ROOT = _os_boot.path.dirname(_os_boot.path.dirname(
    _os_boot.path.abspath(__file__)))
if ROOT not in _sys_boot.path:
    _sys_boot.path.insert(0, ROOT)
# --- 目录引导结束 ---
import time

from backtest_levels import (load_all, precompute, match_pool, match_l1,
                             fuse, rank_ic, TARGET_N, STEP, TAIL, TOPK)
import stock_gui as sg
from stock_gui import _is_etf

TECH_KW = ("软件", "计算机", "半导体", "元件", "电子", "通信", "光电",
           "IT服务", "互联网", "游戏", "传媒", "数字", "消费电子", "光学")
MED_KW = ("医药", "中药", "生物", "医疗", "制药", "疫苗", "兽药")
FIN_KW = ("证券", "银行", "保险", "多元金融")


def cohort(ind):
    ind = ind or ""
    if any(k in ind for k in TECH_KW) or any(k in ind for k in MED_KW):
        return "tech_med"
    if any(k in ind for k in FIN_KW):
        return "fin"
    return None


def main():
    t0 = time.time()
    by, meta = load_all()
    cands = [(c, r) for c, r in by.items() if len(r) >= 400 and not _is_etf(c)
             and c in meta]
    cands.sort(key=lambda cr: -len(cr[1]))
    top200 = cands[:200]

    # 目标：风险组内有足够缓存的目标股（科技医药 与 金融 各取一半）
    tg_tech = [c for c, _ in top200 if cohort(meta[c][2]) == "tech_med"]
    tg_fin = [c for c, _ in top200 if cohort(meta[c][2]) == "fin"]
    tg_codes = tg_tech[:8] + tg_fin[:7]
    print(f"目标股 {len(tg_codes)}（科技医药{len(tg_tech[:8])}+金融{len(tg_fin[:7])}），"
          f"池候选 {len(top200)}")

    feats = {}
    def F(c):
        if c not in feats:
            feats[c] = precompute(by[c])
        return feats[c]
    for c, _ in top200:
        F(c)
    print(f"预计算 {len(feats)} 只 {time.time()-t0:.0f}s\n")

    combos = {"L1": [], "L1+L2": [], "L1+L2+L3旧(市值层)": [],
              "L1+L2+L3新(风险组)": [], "L1+L3新(风险组)": []}
    stat = {"L3old": [0, 0], "L3risk": [0, 0]}
    n_eval = 0
    for code in tg_codes:
        m = meta[code]
        _, _, ind, cap, tier = m
        fp = F(code)
        lg = __import__("math").log(max(cap or 1e8, 1e8))
        peers = [c for c, _ in top200 if c != code and meta[c][2] == ind]
        peers.sort(key=lambda c: abs(__import__("math").log(
            max(meta[c][3] or 1e8, 1e8)) - lg))
        peers = peers[:40]
        co = cohort(ind)
        l3risk = [c for c, _ in top200
                  if c != code and c not in peers and cohort(meta[c][2]) == co]
        l3risk = l3risk[:60]
        l3old = [c for c, _ in top200
                 if c != code and c not in peers and meta[c][4] == tier
                 and meta[c][2] != ind]
        l3old = l3old[:60]
        n = len(fp["closes"])
        for t in range(max(2 * 10 + 2, n - TAIL), n - 1, STEP):
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
            s3r = []
            for pc in l3risk:
                s3r.extend(match_pool(F(pc), d, cur_win, cur_ctx, vr_now))
            s3r.sort(key=lambda s: s["similarity_score"]); s3r = s3r[:TOPK]
            s3o = []
            for pc in l3old:
                s3o.extend(match_pool(F(pc), d, cur_win, cur_ctx, vr_now))
            s3o.sort(key=lambda s: s["similarity_score"]); s3o = s3o[:TOPK]
            actual = fp["closes"][t + 1] / fp["closes"][t] - 1
            stat["L3risk"][0] += len(s3r); stat["L3risk"][1] += bool(s3r)
            stat["L3old"][0] += len(s3o); stat["L3old"][1] += bool(s3o)
            n_eval += 1
            combos["L1"].append((fuse([("L1", s1)]), actual))
            combos["L1+L2"].append((fuse([("L1", s1), ("L2", s2)]), actual))
            combos["L1+L2+L3旧(市值层)"].append(
                (fuse([("L1", s1), ("L2", s2), ("L3", s3o)]), actual))
            combos["L1+L2+L3新(风险组)"].append(
                (fuse([("L1", s1), ("L2", s2), ("L3", s3r)]), actual))
            combos["L1+L3新(风险组)"].append(
                (fuse([("L1", s1), ("L3", s3r)]), actual))

    print(f"评估点 {n_eval} | L3风险组平均样本 "
          f"{stat['L3risk'][0]/max(1,stat['L3risk'][1]):.1f} 覆盖"
          f"{stat['L3risk'][1]}/{n_eval} | L3旧平均 "
          f"{stat['L3old'][0]/max(1,stat['L3old'][1]):.1f} 覆盖"
          f"{stat['L3old'][1]}/{n_eval}\n")
    print(f"{'配置':<22}{'n':>5}{'方向命中':>9}{'IC':>9}{'MAE':>9}")
    import json
    out = {}
    for name, arr in combos.items():
        pr = [(p, a) for p, a in arr if p is not None]
        if len(pr) < 20:
            print(f"{name:<22} 样本不足({len(pr)})")
            continue
        preds = [p for p, _ in pr]; acts = [a for _, a in pr]
        hit = sum(1 for p, a in zip(preds, acts)
                  if (p > 0) == (a > 0)) / len(preds)
        ic = rank_ic(preds, acts)
        mae = sum(abs(p - a) for p, a in zip(preds, acts)) / len(preds)
        out[name] = {"n": len(preds), "dir_hit": hit, "ic": ic, "mae": mae}
        print(f"{name:<22}{len(preds):>5}{hit*100:>8.1f}%{ic:>+9.4f}{mae*100:>8.3f}%")
    with open("l3risk_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → l3risk_results.json")


if __name__ == "__main__":
    main()
