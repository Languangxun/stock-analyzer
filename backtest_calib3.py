# -*- coding: utf-8 -*-
"""清洗后复测：覆盖率 + k标定"""
from backtest_levels import load_all, precompute, match_l1, wpct, STEP, TAIL, TOPK, W
import stock_gui as sg
from stock_gui import _is_etf

by, meta = load_all()
cands = [(c, r) for c, r in by.items() if len(r) >= 400 and not _is_etf(c)
         and c in meta]
cands.sort(key=lambda cr: -len(cr[1]))
targets = cands[:300]
feats = [precompute(r) for _, r in targets]
print(f"目标 {len(targets)} 只")

rows = []
for fp in feats:
    nn = len(fp["closes"])
    for t in range(max(2 * W + 2, nn - TAIL), nn - 1, STEP):
        cur_win = fp["zwin"][t]
        if cur_win is None:
            continue
        cur_ctx = {"struct": fp["struct"][t], "vola": fp["vola"][t],
                   "rsi": fp["rsi"][t], "volchg": fp["volchg"][t],
                   "weekly": fp["weekly"][t]}
        s1 = match_l1(fp, t, cur_win, cur_ctx, fp["vr"][t])
        if len(s1) < 3:
            continue
        pairs = [(s["n1_cl"], s["weight"]) for s in s1]
        rows.append((wpct(pairs, 10), wpct(pairs, 25), wpct(pairs, 50),
                     wpct(pairs, 75), wpct(pairs, 90),
                     fp["closes"][t + 1] / fp["closes"][t] - 1))
n = len(rows)
print(f"n={n}")
for k in (1.0, 1.2, 1.3, 1.4, 1.5):
    c25 = sum(1 for p10, p25, p50, p75, p90, a in rows
              if p50 + (p25 - p50) * k <= a <= p50 + (p75 - p50) * k)
    c10 = sum(1 for p10, p25, p50, p75, p90, a in rows
              if p50 + (p10 - p50) * k <= a <= p50 + (p90 - p50) * k)
    print(f"k={k:.1f}  P25-P75覆盖 {c25/n*100:.1f}% (目标50)  "
          f"P10-P90覆盖 {c10/n*100:.1f}% (目标80)")
