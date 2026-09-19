# -*- coding: utf-8 -*-
"""T+5 累计收益预测：方向/IC/区间校准（n≈4500）"""
# --- 目录引导：backtests/ 下运行也能导入 stock_gui/factor_lab，产物写项目根 ---
import os as _os_boot
import sys as _sys_boot

ROOT = _os_boot.path.dirname(_os_boot.path.dirname(
    _os_boot.path.abspath(__file__)))
if ROOT not in _sys_boot.path:
    _sys_boot.path.insert(0, ROOT)
# --- 目录引导结束 ---
import math
from backtest_levels import load_all, precompute, match_l1, wpct, STEP, TAIL, TOPK, W
import stock_gui as sg
from stock_gui import _is_etf

by, meta = load_all()
cands = [(c, r) for c, r in by.items() if len(r) >= 400 and not _is_etf(c)
         and c in meta]
cands.sort(key=lambda cr: -len(cr[1]))
targets = cands[:300]
feats = [precompute(r) for _, r in targets]

preds, acts = [], []
rows = []
for fp in feats:
    closes = fp["closes"]
    nn = len(closes)
    for t in range(max(2 * W + 2, nn - TAIL), nn - 6, STEP):
        cur_win = fp["zwin"][t]
        if cur_win is None:
            continue
        cur_ctx = {"struct": fp["struct"][t], "vola": fp["vola"][t],
                   "rsi": fp["rsi"][t], "volchg": fp["volchg"][t],
                   "weekly": fp["weekly"][t]}
        s1 = match_l1(fp, t, cur_win, cur_ctx, fp["vr"][t])
        if len(s1) < 3:
            continue
        # 样本 j ≤ t-W → j+5 ≤ t-5 < t，标签完全实现，无前视
        pairs5 = []
        for s in s1:
            j = s["_j"]
            if j + 5 < len(closes):
                pairs5.append((closes[j + 5] / closes[j] - 1, s["weight"]))
        if len(pairs5) < 3:
            continue
        actual = closes[t + 5] / closes[t] - 1
        p50 = wpct(pairs5, 50)
        preds.append(p50); acts.append(actual)
        rows.append((wpct(pairs5, 10), wpct(pairs5, 25), p50,
                     wpct(pairs5, 75), wpct(pairs5, 90), actual))

n = len(preds)
hit = sum(1 for p, a in zip(preds, acts) if (p > 0) == (a > 0)) / n
z = (hit - 0.5) / math.sqrt(0.25 / n)
ic = None
def rank(x):
    idx = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    for rr, i in enumerate(idx):
        r[i] = rr
    return r
rp, ra = rank(preds), rank(acts)
mp, ma = sum(rp)/n, sum(ra)/n
cov = sum((a-mp)*(b-ma) for a, b in zip(rp, ra))
sp = math.sqrt(sum((a-mp)**2 for a in rp)*sum((b-ma)**2 for b in ra)) or 1
ic = cov/sp
print(f"T+5: n={n} 方向命中={hit*100:.1f}% z={z:+.2f} IC={ic:+.4f} "
      f"t(IC)={ic*math.sqrt(n):+.2f} MAE={sum(abs(p-a) for p,a in zip(preds,acts))/n*100:.2f}%")
print(f"5日实际波动基线: std={ (sum((a-sum(acts)/n)**2 for a in acts)/n)**0.5*100:.2f}%")
for k in (1.0, 1.2, 1.3, 1.4):
    c25 = sum(1 for p10, p25, p50, p75, p90, a in rows
              if p50 + (p25-p50)*k <= a <= p50 + (p75-p50)*k)
    c10 = sum(1 for p10, p25, p50, p75, p90, a in rows
              if p50 + (p10-p50)*k <= a <= p50 + (p90-p50)*k)
    print(f"k={k:.1f}  P25-P75覆盖 {c25/n*100:.1f}% (目标50)  P10-P90覆盖 {c10/n*100:.1f}% (目标80)")
