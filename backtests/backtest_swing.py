#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_swing.py - 波段优化回测 + 各类型IC报告

诊断交易少的原因：backtest_trade.py STEP=6(每6根K线才评估一次) + TAIL=180 +
阈值过滤，单股仅~30次评估。本脚本：
  - STEP=3(trad) / 1(theme,etf)，评估密度提升2~6倍
  - 波段(震荡市)过滤：MA20斜率平坦 且 |close/MA20-1|<=3%（京东方式箱体）
  - 回调入场门槛：RSI14 < 45
  - 输出：各分组 x 各层级(L1/L2行业/L2+ETF) Rank IC、交易统计、京东方个股明细
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

import json
import math
import time

from backtest_levels import load_all, match_pool, match_l1, fuse, rank_ic, W
import stock_gui as sg
from stock_gui import _is_etf
from backtest_trade import precompute, is_theme

TAIL = 180
N_TRAD, N_THEME, N_ETF = 40, 20, 20
L2_N, ETF_N = 30, 20
STEP_TRAD, STEP_OTHER = 3, 1
THRESHOLDS = (0.0, 0.002, 0.005)
BOE = "sz000725"


def ma(vals, n, t):
    if t + 1 >= n:
        seg = vals[t + 1 - n:t + 1]
        if len(seg) == n and all(v is not None for v in seg):
            return sum(seg) / n
    return None


def swing_state(fp, t):
    """返回 (osc, pullback, rsi)"""
    closes = fp["closes"]
    m20 = ma(closes, 20, t)
    if m20 is None or t < 10:
        return False, False, fp["rsi"][t]
    m20_prev = ma(closes, 20, t - 10)
    c = closes[t]
    flat = m20_prev is not None and abs(m20 / m20_prev - 1) < 0.015
    in_band = abs(c / m20 - 1) <= 0.03
    osc = flat and in_band
    rsi = fp["rsi"][t]
    pull = rsi is not None and rsi < 45
    return osc, pull, rsi


def main():
    t0 = time.time()
    by, meta = load_all()
    stocks = [(c, r) for c, r in by.items() if len(r) >= 400 and not _is_etf(c)
              and c in meta and meta[c][2]]
    stocks.sort(key=lambda cr: -len(cr[1]))
    trad = [c for c, _ in stocks if not is_theme(meta[c][2])][:N_TRAD]
    theme = [c for c, _ in stocks if is_theme(meta[c][2])][:N_THEME]
    etfs = [c for c, r in by.items() if _is_etf(c) and len(r) >= 250][:N_ETF]
    print(f"trad={len(trad)} theme={len(theme)} etf={len(etfs)}")

    feats = {}
    def F(c):
        if c not in feats:
            feats[c] = precompute(by[c], is_etf=_is_etf(c))
        return feats[c]
    for c in trad + theme + etfs:
        F(c)
    etf_feats = [F(c) for c in etfs]
    print(f"预计算 {len(feats)} 只 {time.time()-t0:.0f}s，开始评估..")

    # 样本: dict(date, actual, p_l1, p_l2, p_l2e, osc, pull, rsi)
    R = {"trad": [], "theme": [], "etf": []}
    n_eval = 0
    for code in trad:
        m = meta[code]
        fp = F(code)
        peers = [c for c in trad if c != code and meta[c][2] == m[2]][:L2_N]
        for c in peers:
            F(c)
        n = len(fp["closes"])
        for t in range(max(2 * W + 2, n - TAIL), n - 1, STEP_TRAD):
            d = fp["dates"][t]
            cur_win = fp["zwin"][t]
            if cur_win is None:
                continue
            ctx = {"struct": fp["struct"][t], "vola": fp["vola"][t],
                   "rsi": fp["rsi"][t], "volchg": fp["volchg"][t],
                   "weekly": fp["weekly"][t]}
            vr_now = fp["vr"][t]
            s1 = match_l1(fp, t, cur_win, ctx, vr_now)
            if len(s1) < 3:
                continue
            s2i = []
            for pc in peers:
                s2i.extend(match_pool(F(pc), d, cur_win, ctx, vr_now))
            s2i.sort(key=lambda s: s["similarity_score"]); s2i = s2i[:len(s1)]
            s2e = list(s2i)
            for ef in etf_feats:
                s2e.extend(match_pool(ef, d, cur_win, ctx, vr_now))
            s2e.sort(key=lambda s: s["similarity_score"]); s2e = s2e[:len(s1)]
            osc, pull, rsi = swing_state(fp, t)
            actual = fp["closes"][t + 1] / fp["closes"][t] - 1
            R["trad"].append({
                "code": code, "date": d, "actual": actual,
                "p_l1": fuse([("L1", s1)]),
                "p_l2": fuse([("L2", s2i)]) if s2i else None,
                "p_l2e": fuse([("L2", s2e)]) if s2e else None,
                "osc": osc, "pull": pull, "rsi": rsi})
            n_eval += 1
    for grp, codes, step in (("theme", theme, STEP_OTHER),
                             ("etf", etfs, STEP_OTHER)):
        for code in codes:
            fp = F(code)
            n = len(fp["closes"])
            for t in range(max(2 * W + 2, n - TAIL), n - 1, step):
                cur_win = fp["zwin"][t]
                if cur_win is None:
                    continue
                ctx = {"struct": fp["struct"][t], "vola": fp["vola"][t],
                       "rsi": fp["rsi"][t], "volchg": fp["volchg"][t],
                       "weekly": fp["weekly"][t]}
                s1 = match_l1(fp, t, cur_win, ctx, fp["vr"][t])
                if len(s1) < 3:
                    continue
                osc, pull, rsi = swing_state(fp, t)
                actual = fp["closes"][t + 1] / fp["closes"][t] - 1
                R[grp].append({
                    "code": code, "date": fp["dates"][t], "actual": actual,
                    "p_l1": fuse([("L1", s1)]), "p_l2": None, "p_l2e": None,
                    "osc": osc, "pull": pull, "rsi": rsi})
                n_eval += 1
    print(f"评估点 {n_eval}，耗时 {time.time()-t0:.0f}s\n")

    # 京东方A 单独评估（光学光电子归入theme，这里独立跑 L1 波段）
    boe = []
    if BOE in by:
        fp = F(BOE)
        n = len(fp["closes"])
        for t in range(max(2 * W + 2, n - 250), n - 1, 1):
            cur_win = fp["zwin"][t]
            if cur_win is None:
                continue
            ctx = {"struct": fp["struct"][t], "vola": fp["vola"][t],
                   "rsi": fp["rsi"][t], "volchg": fp["volchg"][t],
                   "weekly": fp["weekly"][t]}
            s1 = match_l1(fp, t, cur_win, ctx, fp["vr"][t])
            if len(s1) < 3:
                continue
            osc, pull, rsi = swing_state(fp, t)
            boe.append({
                "date": fp["dates"][t],
                "actual": fp["closes"][t + 1] / fp["closes"][t] - 1,
                "p_l1": fuse([("L1", s1)]), "osc": osc, "pull": pull,
                "rsi": rsi})
        R["boe"] = boe

    # ---------- IC 报告 ----------
    def ic_line(name, arr, key):
        pairs = []
        po, pn = [], []
        for s in arr:
            if s[key] is None:
                continue
            pr = (s[key], s["actual"])
            pairs.append(pr)
            (po if s["osc"] else pn).append(pr)
        if len(pairs) < 30:
            return f"{name:<24} 样本不足({len(pairs)})"
        ic = rank_ic([p for p, _ in pairs], [a for _, a in pairs])
        so = (f" 震荡IC={rank_ic([p for p,_ in po],[a for _,a in po]):+.3f}"
              f"(n={len(po)})") if len(po) >= 30 else ""
        sn = (f" 非震荡IC={rank_ic([p for p,_ in pn],[a for _,a in pn]):+.3f}"
              f"(n={len(pn)})") if len(pn) >= 30 else ""
        return (f"{name:<24} IC={ic:+.3f} (n={len(pairs)}){so}{sn}")

    print("=" * 78)
    print("一、各类型 Rank IC（预测P50 vs 次日收益，按层拆分）")
    print("=" * 78)
    for grp, levels in (("trad", ("p_l1", "p_l2", "p_l2e")),
                        ("theme", ("p_l1",)), ("etf", ("p_l1",))):
        names = {"p_l1": "L1(自身历史)", "p_l2": "L2(同行业)",
                 "p_l2e": "L2(行业+ETF)"}
        for k in levels:
            print("  " + ic_line(f"{grp} / {names[k]}", R[grp], k))
        print()

    # ---------- 交易统计 ----------
    def trade_stat(name, arr, filt, th):
        trades = [s for s in arr
                  if s["p_l1"] is not None and s["p_l1"] > th and filt(s)]
        if len(trades) < 10:
            return f"{name:<34} 交易不足({len(trades)})"
        win = sum(1 for s in trades if s["actual"] > 0) / len(trades)
        avg = sum(s["actual"] for s in trades) / len(trades)
        byd = {}
        for s in trades:
            byd.setdefault(s["date"], []).append(s["actual"])
        days = sorted(byd)
        comp = 1.0
        for d in days:
            comp *= 1 + sum(byd[d]) / len(byd[d])
        ann = comp ** (250 / max(1, len(days))) - 1
        return (f"{name:<34} 交易{len(trades):>5} 胜率{win*100:5.1f}% "
                f"均次{avg*100:+5.2f}% 年化{ann*100:+7.1f}%")

    filt_all = lambda s: True
    filt_osc = lambda s: s["osc"]
    filt_op = lambda s: s["osc"] and s["pull"]
    print("=" * 78)
    print("二、交易统计（波段优化对比）")
    print("=" * 78)
    for grp in ("trad", "theme", "etf"):
        for fname, filt in (("全样本(基线,th>0)", filt_all),
                            ("震荡市过滤(th>0)", filt_osc),
                            ("震荡+回调RSI<45(th>0)", filt_op)):
            print("  " + trade_stat(f"{grp} {fname}", R[grp], filt, 0.0))
        best = None
        for th in THRESHOLDS:
            st = trade_stat(f"th>{th}", R[grp], filt_op, th)
            print("  " + st)
        print()

    # ---------- 京东方个股 ----------
    print("=" * 78)
    print("三、京东方A(sz000725) 波段明细（近250日逐日, 震荡+回调入场, th>0）")
    print("=" * 78)
    boe = R.get("boe", [])
    if boe:
        print(f"  评估点 {len(boe)}，其中震荡市 {sum(1 for s in boe if s['osc'])}，"
              f"震荡+回调 {sum(1 for s in boe if s['osc'] and s['pull'])}")
        for th in (0.0, 0.002):
            trades = [s for s in boe if s["p_l1"] is not None
                      and s["p_l1"] > th and s["osc"] and s["pull"]]
            win = sum(1 for s in trades if s["actual"] > 0) / max(1, len(trades))
            print(f"  th>{th}: 交易{len(trades)} 胜率{win*100:.1f}% "
                  f"均次{sum(s['actual'] for s in trades)/max(1,len(trades))*100:+.2f}%")
        for s in boe:
            if s["p_l1"] is not None and s["p_l1"] > 0 and s["osc"] and s["pull"]:
                print(f"    {s['date']}  p50={s['p_l1']*100:+5.2f}%  "
                      f"次日{s['actual']*100:+5.2f}%  RSI={s['rsi'] and round(s['rsi'])}")
    else:
        print("  (无京东方数据)")

    with open(_os_boot.path.join(_RESULTS_DIR, "swing_results.json"), "w", encoding="utf-8") as f:
        json.dump(R, f, ensure_ascii=False)
    print(f"\n完成 {time.time()-t0:.0f}s -> swing_results.json")


if __name__ == "__main__":
    main()
