#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_levels.py - 层级组合消融：L1 / L1+L2 / L1+L3 / L1+L2+L3

协议（与主程序同构，无前视）：
  - 评估日 d = 目标股第 t 根K线日期，预测 t+1 收益
  - 当前窗口 = znorm(rets[t-W : t])（收盘 t 已知，不含 t+1 收益）
  - L1 样本：同股，j ≤ t-W（与当前窗口不重叠）
  - L2 样本：同行业池，label日期 ≤ d（收盘后标签已知）
  - L3 样本：同市值层池（排除L2行业），label日期 ≤ d
  - 每层取相似度 Top10，等权 + 时间衰减，跨层用主程序 _dynamic_lv_weights 融合
"""
import bisect
import math
import time

import stock_gui as sg
from stock_gui import (znorm, logret, wpct, rsi_at, vola_at, candle_feats,
                       volchg_at, weekly_ctx, vol_ratio_at, _is_etf, _dynamic_lv_weights, CFG)

W = CFG.W_WINDOW
TOPK = CFG.TOPK
MIN_GAP = max(3, W // 2)
L2_N, L3_N = 40, 60
TARGET_N, STEP, TAIL = 15, 10, 150


def load_all(min_bars=400):
    with sg.db_conn() as conn:
        bars = conn.execute(
            "SELECT code, date, open, high, low, close, vol "
            "FROM daily_bars ORDER BY code, date").fetchall()
        meta = {r[0]: r for r in conn.execute(
            "SELECT code, name, industry, mktcap, tier FROM stocks").fetchall()}
    by = {}
    for c, d, o, h, l, cl, v in bars:
        by.setdefault(c, []).append(
            {"date": d, "open": o, "high": h, "low": l, "close": cl,
             "vol": v or 0.0})
    return by, meta


def precompute(rows):
    closes = [r["close"] for r in rows]
    rets = logret(closes, is_etf=False)
    vols = [r["vol"] for r in rows]
    n = len(rows)
    return {
        "dates": [r["date"] for r in rows],
        "closes": closes,
        "zwin": [znorm(rets[i - W:i]) if i >= W else None
                 for i in range(n + 1)],
        "struct": [candle_feats(rows, i) for i in range(n)],
        "vola": [vola_at(rets, i) for i in range(n)],
        "rsi": [rsi_at(closes, i) for i in range(n)],
        "volchg": [volchg_at(vols, i) for i in range(n)],
        "weekly": [weekly_ctx(rows, i, CFG.WEEKLY_N) for i in range(n)],
        "vr": [vol_ratio_at(vols, i) for i in range(n)],
    }


def decay_w(date_str):
    try:
        age = (time.mktime(time.strptime(
            time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
            - time.mktime(time.strptime(date_str, "%Y-%m-%d"))) / 86400.0
        if age > CFG.TIME_DECAY_DAYS:
            return max(CFG.TIME_DECAY_RATE,
                       1.0 - (age - CFG.TIME_DECAY_DAYS) / 365.0 * 0.5)
    except (ValueError, TypeError):
        pass
    return 1.0


def match_pool(fpc, d, cur_win, cur_ctx, vr_now, topk=TOPK):
    """在池股 fpc 中匹配日期 ≤ d 的窗口。返回样本列表。"""
    sims = []
    neu = (CFG.STRUCT_W + CFG.VOLA_W + CFG.RSI_W + CFG.VOLCHG_W
           + CFG.WEEKLY_W) * 0.5
    dates = fpc["dates"]
    b = bisect.bisect_right(dates, d) - 1      # 最后一个 date ≤ d 的下标
    for j in range(W, b):                       # label j+1 ≤ b ≤ d 已知
        w = fpc["zwin"][j]
        if w is None:
            continue
        dsc = sum((a - b2) ** 2 for a, b2 in zip(cur_win, w)) ** 0.5
        vr_j = fpc["vr"][j]
        if vr_now is not None and vr_j is not None:
            dsc += 0.6 * min(abs(math.log(max(vr_now, 1e-6)
                                          / max(vr_j, 1e-6))), 2.5)
        else:
            dsc += 0.30
        sx, va, rj, gj, wj = (fpc["struct"][j], fpc["vola"][j],
                              fpc["rsi"][j], fpc["volchg"][j],
                              fpc["weekly"][j])
        if cur_ctx["struct"] is not None and sx is not None:
            dsc += CFG.STRUCT_W * (sum((a - b2) ** 2
                                       for a, b2 in zip(cur_ctx["struct"], sx))
                                   / len(sx)) ** 0.5
        else:
            dsc += CFG.STRUCT_W * 0.5
        if cur_ctx["vola"] and va:
            dsc += CFG.VOLA_W * min(abs(math.log(cur_ctx["vola"] / va)), 1.5)
        else:
            dsc += CFG.VOLA_W * 0.5
        if cur_ctx["rsi"] is not None and rj is not None:
            dsc += CFG.RSI_W * abs(cur_ctx["rsi"] - rj) / 100.0
        else:
            dsc += CFG.RSI_W * 0.5
        if cur_ctx["volchg"] is not None and gj is not None:
            dsc += CFG.VOLCHG_W * min(abs(cur_ctx["volchg"] - gj), 1.5)
        else:
            dsc += CFG.VOLCHG_W * 0.5
        if cur_ctx["weekly"] is not None and wj is not None:
            dsc += CFG.WEEKLY_W * min(
                (sum((a - b2) ** 2 for a, b2 in zip(cur_ctx["weekly"], wj))
                 / len(wj)) ** 0.5 * 5, 1.5)
        else:
            dsc += CFG.WEEKLY_W * 0.5
        sims.append((dsc, j))
    if len(sims) < 3:
        return []
    sims.sort(key=lambda x: x[0])
    best = sims[0][0]
    out = []
    for dsc, j in sims:
        lbl = fpc["closes"][j + 1] / fpc["closes"][j] - 1
        out.append({"similarity_score": dsc, "weight": decay_w(dates[j]),
                    "n1_cl": lbl, "n1_hi": None, "n1_lo": None, "_j": j})
        if len(out) >= topk:
            break
    return out


def match_l1(fp, t, cur_win, cur_ctx, vr_now, topk=TOPK):
    sims = []
    for j in range(W, t - W + 1):               # 与当前窗口不重叠
        w = fp["zwin"][j]
        dsc = sum((a - b2) ** 2 for a, b2 in zip(cur_win, w)) ** 0.5
        vr_j = fp["vr"][j]
        if vr_now is not None and vr_j is not None:
            dsc += 0.6 * min(abs(math.log(max(vr_now, 1e-6)
                                          / max(vr_j, 1e-6))), 2.5)
        else:
            dsc += 0.30
        sx, va, rj, gj, wj = (fp["struct"][j], fp["vola"][j],
                              fp["rsi"][j], fp["volchg"][j],
                              fp["weekly"][j])
        if cur_ctx["struct"] is not None and sx is not None:
            dsc += CFG.STRUCT_W * (sum((a - b2) ** 2
                                       for a, b2 in zip(cur_ctx["struct"], sx))
                                   / len(sx)) ** 0.5
        else:
            dsc += CFG.STRUCT_W * 0.5
        if cur_ctx["vola"] and va:
            dsc += CFG.VOLA_W * min(abs(math.log(cur_ctx["vola"] / va)), 1.5)
        else:
            dsc += CFG.VOLA_W * 0.5
        if cur_ctx["rsi"] is not None and rj is not None:
            dsc += CFG.RSI_W * abs(cur_ctx["rsi"] - rj) / 100.0
        else:
            dsc += CFG.RSI_W * 0.5
        if cur_ctx["volchg"] is not None and gj is not None:
            dsc += CFG.VOLCHG_W * min(abs(cur_ctx["volchg"] - gj), 1.5)
        else:
            dsc += CFG.VOLCHG_W * 0.5
        if cur_ctx["weekly"] is not None and wj is not None:
            dsc += CFG.WEEKLY_W * min(
                (sum((a - b2) ** 2 for a, b2 in zip(cur_ctx["weekly"], wj))
                 / len(wj)) ** 0.5 * 5, 1.5)
        else:
            dsc += CFG.WEEKLY_W * 0.5
        sims.append((dsc, j))
    sims.sort(key=lambda x: x[0])
    out, last_j = [], -10 ** 9
    for dsc, j in sims:
        if abs(j - last_j) < MIN_GAP:
            continue
        out.append({"similarity_score": dsc,
                    "weight": decay_w(fp["dates"][j]),
                    "n1_cl": fp["closes"][j + 1] / fp["closes"][j] - 1,
                    "n1_hi": None, "n1_lo": None, "_j": j})
        last_j = j
        if len(out) >= topk:
            break
    return out


def fuse(levels):
    """跨层融合（主程序同款动态权重），返回 P50 收益预测。"""
    levels = [(k, s) for k, s in levels if s]
    if not levels:
        return None
    LW = _dynamic_lv_weights(levels)
    tot = sum(LW.get(k, 0.0) for k, _ in levels) or 1.0
    pairs = []
    for k, smp in levels:
        lw = LW.get(k, 0.0) / tot
        tw = sum(s["weight"] for s in smp) or 1.0
        pairs.extend((s["n1_cl"], lw * s["weight"] / tw) for s in smp)
    return wpct(pairs, 50)


def rank_ic(preds, acts):
    def rank(x):
        idx = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        for rr, i in enumerate(idx):
            r[i] = rr
        return r
    rp, ra = rank(preds), rank(acts)
    mp, ma = sum(rp) / len(rp), sum(ra) / len(ra)
    cov = sum((a - mp) * (b - ma) for a, b in zip(rp, ra))
    sp = math.sqrt(sum((a - mp) ** 2 for a in rp)
                   * sum((b - ma) ** 2 for b in ra)) or 1.0
    return cov / sp


def main():
    t0 = time.time()
    by, meta = load_all()
    cands = [(c, r) for c, r in by.items() if len(r) >= 400 and not _is_etf(c)
             and c in meta]
    cands.sort(key=lambda cr: -len(cr[1]))
    targets = cands[:TARGET_N]
    print(f"目标股 {len(targets)} 只，池股缓存 {len(cands)} 只；"
          f"L2={L2_N} L3={L3_N} 评估步长={STEP} 回看={TAIL}")

    # 预计算全部涉及股票（targets ∪ pools 按需），先全量算 targets
    feats = {}
    for c, r in cands[:200]:
        feats[c] = precompute(r)
    print(f"预计算 {len(feats)} 只 {time.time()-t0:.0f}s，开始评估...\n")

    combos = {"L1": [], "L1+L2": [], "L1+L3": [], "L1+L2+L3": []}
    lv_stat = {"L2": [0, 0], "L3": [0, 0]}     # [样本总数, 非空次数]
    n_eval = 0
    for code, rows in targets:
        m = meta.get(code)
        if not m:
            continue
        _, _, ind, cap, tier = m
        fp = feats[code]
        # 池：L2 同行业（市值接近），L3 同市值层（排除该行业）
        peers = [c for c, r in cands[:200]
                 if c != code and meta.get(c) and meta[c][2] == ind]
        lg = math.log(max(cap or 1e8, 1e8))
        peers.sort(key=lambda c: abs(math.log(max(meta[c][3] or 1e8, 1e8)) - lg))
        peers = peers[:L2_N]
        tier_pool = [c for c, r in cands[:200]
                     if c != code and c not in peers and meta.get(c)
                     and meta[c][4] == tier and meta[c][2] != ind]
        tier_pool = tier_pool[:L3_N]
        for c in peers + tier_pool:
            if c not in feats:
                feats[c] = precompute(by[c])
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
                s2.extend(match_pool(feats[pc], d, cur_win, cur_ctx, vr_now))
            s2.sort(key=lambda s: s["similarity_score"])
            s2 = s2[:TOPK]
            s3 = []
            for pc in tier_pool:
                s3.extend(match_pool(feats[pc], d, cur_win, cur_ctx, vr_now))
            s3.sort(key=lambda s: s["similarity_score"])
            s3 = s3[:TOPK]
            actual = fp["closes"][t + 1] / fp["closes"][t] - 1
            lv_stat["L2"][0] += len(s2); lv_stat["L2"][1] += bool(s2)
            lv_stat["L3"][0] += len(s3); lv_stat["L3"][1] += bool(s3)
            n_eval += 1
            levels_all = [("L1", s1), ("L2", s2), ("L3", s3)]
            combos["L1"].append((fuse([("L1", s1)]), actual))
            combos["L1+L2"].append((fuse([("L1", s1), ("L2", s2)]), actual))
            combos["L1+L3"].append((fuse([("L1", s1), ("L3", s3)]), actual))
            combos["L1+L2+L3"].append((fuse(levels_all), actual))

    print(f"评估点 {n_eval} 个 | L2平均样本 {lv_stat['L2'][0]/max(1,lv_stat['L2'][1]):.1f}"
          f" 覆盖{lv_stat['L2'][1]}/{n_eval} | L3平均 {lv_stat['L3'][0]/max(1,lv_stat['L3'][1]):.1f}"
          f" 覆盖{lv_stat['L3'][1]}/{n_eval}\n")
    print(f"{'配置':<14}{'n':>5}{'方向命中':>9}{'IC':>9}{'MAE':>9}")
    summary = {}
    for name, arr in combos.items():
        pr = [(p, a) for p, a in arr if p is not None]
        if len(pr) < 20:
            print(f"{name:<14} 样本不足({len(pr)})")
            continue
        preds = [p for p, _ in pr]
        acts = [a for _, a in pr]
        hit = sum(1 for p, a in zip(preds, acts) if (p > 0) == (a > 0)) / len(preds)
        ic = rank_ic(preds, acts)
        mae = sum(abs(p - a) for p, a in zip(preds, acts)) / len(preds)
        summary[name] = {"n": len(preds), "dir_hit": hit, "ic": ic, "mae": mae}
        print(f"{name:<14}{len(preds):>5}{hit*100:>8.1f}%{ic:>+9.4f}{mae*100:>8.3f}%")
    import json
    with open("level_ablation_results.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → level_ablation_results.json")


if __name__ == "__main__":
    main()
