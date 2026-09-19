# -*- coding: utf-8 -*-
"""补充：混合 L2（精确行业40 + 类型池补位）对照"""
# --- 目录引导：backtests/ 下运行也能导入 stock_gui/factor_lab，产物写项目根 ---
import os as _os_boot
import sys as _sys_boot

ROOT = _os_boot.path.dirname(_os_boot.path.dirname(
    _os_boot.path.abspath(__file__)))
if ROOT not in _sys_boot.path:
    _sys_boot.path.insert(0, ROOT)
# --- 目录引导结束 ---
import math
import time
from backtest_l2type import load_all, precompute, match_pool, match_l1, \
    fuse, rank_ic, STEP, TAIL, TOPK, W, type_of
import stock_gui as sg
from stock_gui import _is_etf


def main():
    t0 = time.time()
    by, meta = load_all()
    cands = [(c, r) for c, r in by.items() if len(r) >= 400 and not _is_etf(c)
             and c in meta]
    cands.sort(key=lambda cr: -len(cr[1]))
    pool200 = cands[:200]
    seen = {}
    targets = []
    for c, r in cands:
        ty = type_of(meta[c][2])
        if seen.get(ty, 0) < 3:
            targets.append(c)
            seen[ty] = seen.get(ty, 0) + 1
        if len(targets) >= 18:
            break
    feats = {}
    def F(c):
        if c not in feats:
            feats[c] = precompute(by[c])
        return feats[c]
    for c, _ in pool200:
        F(c)
    for c in targets:
        F(c)
    combos = {"L1+L2精确行业(40)": [], "L1+L2混合(精确+类型补位)": []}
    n_eval = 0
    for code in targets:
        m = meta[code]
        _, _, ind, cap, tier = m
        fp = F(code)
        lg = math.log(max(cap or 1e8, 1e8))
        peers_ex = [c for c, _ in pool200 if c != code and meta[c][2] == ind]
        peers_ex.sort(key=lambda c: abs(math.log(
            max(meta[c][3] or 1e8, 1e8)) - lg))
        peers_ex = peers_ex[:40]
        my_type = type_of(ind)
        peers_ty = [c for c, _ in pool200 if c != code
                    and type_of(meta[c][2]) == my_type and c not in peers_ex]
        peers_ty.sort(key=lambda c: abs(math.log(
            max(meta[c][3] or 1e8, 1e8)) - lg))
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
            s_ex = []
            for pc in peers_ex:
                s_ex.extend(match_pool(F(pc), d, cur_win, cur_ctx, vr_now))
            s_ex.sort(key=lambda s: s["similarity_score"]); s_ex = s_ex[:TOPK]
            s_hy = list(s_ex)
            for pc in peers_ty[:60]:
                s_hy.extend(match_pool(F(pc), d, cur_win, cur_ctx, vr_now))
            s_hy.sort(key=lambda s: s["similarity_score"]); s_hy = s_hy[:TOPK]
            actual = fp["closes"][t + 1] / fp["closes"][t] - 1
            n_eval += 1
            combos["L1+L2精确行业(40)"].append(
                (fuse([("L1", s1), ("L2", s_ex)]), actual))
            combos["L1+L2混合(精确+类型补位)"].append(
                (fuse([("L1", s1), ("L2", s_hy)]), actual))
    print(f"评估点 {n_eval}\n")
    print(f"{'配置':<26}{'n':>5}{'方向命中':>9}{'IC':>9}{'MAE':>9}")
    import json
    out = {}
    for name, arr in combos.items():
        pr = [(p, a) for p, a in arr if p is not None]
        preds = [p for p, _ in pr]; acts = [a for _, a in pr]
        hit = sum(1 for p, a in zip(preds, acts)
                  if (p > 0) == (a > 0)) / len(preds)
        ic = rank_ic(preds, acts)
        mae = sum(abs(p - a) for p, a in zip(preds, acts)) / len(preds)
        out[name] = {"n": len(preds), "dir_hit": hit, "ic": ic, "mae": mae}
        print(f"{name:<26}{len(preds):>5}{hit*100:>8.1f}%{ic:>+9.4f}{mae*100:>8.3f}%")
    with open("l2hybrid_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"完成 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
