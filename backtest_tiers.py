#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_tiers.py - 三档组合策略回测引擎（干净口径）

策略（只用 T-1 及以前信息决策，T 日收盘成交）：
  稳健  全A  动量20+低波20 合成排名 top20，每 20 日调仓，上证 MA20 闸门
  均衡  全A  同上 top15，每 10 日调仓，上证 MA20 闸门
  激进  创业板 60 日 β（对创业板指）最高 top5，每 10 日调仓，创业板指 MA20 闸门

口径：
  - 价格：库内 hfq × adjust 系数 = 乘法前复权（≈现价），收益口径不变；
  - 成交：T-1 收盘出信号，T 日收盘成交；涨停不买、跌停不卖、停牌顺延；
  - 费用：滑点 0.1%/边 + 佣金万 2.5（最低 5 元）+ 印花税千 1（卖出）+ 过户费；
  - 整数手（100 股），买不起自动跳过；
  - 股票池：价 >1、上市 ≥250 日、20 日均成交额 ≥3000 万元（均为时点过滤）；
  - 退市/长停：连续 20 个交易日无 K 线按最后收盘价了结；
  - 调仓相位：主口径为「分批平均」（把资金分成 reb 份、错开相位同时运行），
    另报告单相位离散度，避免单一日期的运气被当成 alpha。

用法：
  python backtest_tiers.py --tier all --segment full
  python backtest_tiers.py --tier 激进 --segment val
"""
import argparse
import datetime as _dt
import json
import math
import os
import sqlite3
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("STOCK_DB", os.path.join(HERE, "stock_cache.db"))

TIERS = ("稳健", "均衡", "激进")
TIER_CFG = {
    "稳健": dict(universe="all", score="blend", top=20, reb=20,
                 gate="sh000001", ma=20),
    "均衡": dict(universe="all", score="blend", top=20, reb=10,
                 gate="sh000001", ma=20),
    "激进": dict(universe="chinext", score="beta", top=5, reb=10,
                 gate="sz399006", ma=60),
}
BENCH = {"稳健": "sh000001", "均衡": "sh000001", "激进": "sz399006"}
PREFIXES = ("sh60", "sh68", "sz00", "sz30")
MIN_PRICE = 1.0
MIN_BARS = 250
MIN_AMOUNT = 3e5
STALE_DAYS = 20
SLIP = 0.001
COMMISSION = 0.00025
MIN_COMMISSION = 5.0
STAMP = 0.001
TRANSFER = 0.00001
LOT = 100


def load_panel():
    con = sqlite3.connect(DB)
    adj = {c: (k or 1.0) for c, k in con.execute("select code,k from adjust")}
    rows = con.execute(
        "select code,date,open,high,low,close,vol from daily_bars "
        "where date>=? order by code,date", ("2020-01-01",)).fetchall()
    con.close()
    data, dates = {}, set()
    for c, d, o, h, l, cl, v in rows:
        if not c.startswith(PREFIXES):
            continue
        data.setdefault(c, []).append((d, o, h, l, cl, v or 0.0))
        dates.add(d)
    codes = sorted(data)
    cal = sorted(dates)
    didx = {d: i for i, d in enumerate(cal)}
    n, nc = len(codes), len(cal)
    O = np.full((n, nc), np.nan, np.float32)
    H = np.full((n, nc), np.nan, np.float32)
    L = np.full((n, nc), np.nan, np.float32)
    C = np.full((n, nc), np.nan, np.float32)
    V = np.zeros((n, nc), np.float32)
    for r, c in enumerate(codes):
        seq = data[c]
        k = adj.get(c, 1.0)
        cols = np.fromiter((didx[x[0]] for x in seq), np.int64, len(seq))
        O[r, cols] = [(x[1] * k) if x[1] else np.nan for x in seq]
        H[r, cols] = [(x[2] * k) if x[2] else np.nan for x in seq]
        L[r, cols] = [(x[3] * k) if x[3] else np.nan for x in seq]
        C[r, cols] = [(x[4] * k) if x[4] else np.nan for x in seq]
        V[r, cols] = [x[5] for x in seq]
    return codes, cal, O, H, L, C, V


def idx_series(cal, code):
    con = sqlite3.connect(DB)
    rows = con.execute("select date,close from daily_bars where code=? "
                       "order by date", (code,)).fetchall()
    con.close()
    m = {d: c for d, c in rows if c}
    cl = np.full(len(cal), np.nan)
    last = np.nan
    for i, d in enumerate(cal):
        v = m.get(d)
        if v:
            last = v
        cl[i] = last
    r = np.full(len(cal), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        r[1:] = np.where(cl[:-1] > 0, cl[1:] / cl[:-1] - 1.0, np.nan)
    return cl, r


def rank01(x):
    m = np.isfinite(x)
    out = np.full(len(x), np.nan)
    n = int(m.sum())
    if n > 5:
        out[m] = x[m].argsort().argsort() / max(1, n - 1)
    return out


def build_features(cal, C, V):
    NST, NDT = C.shape
    ret1 = np.full_like(C, np.nan)
    ret1[:, 1:] = C[:, 1:] / C[:, :-1] - 1.0
    ret = {}
    for k in (5, 20, 60, 120):
        a = np.full_like(C, np.nan)
        a[:, k:] = C[:, k:] / C[:, :-k] - 1.0
        ret[k] = a

    def roll_std(A, w):
        x = np.nan_to_num(A, nan=0.0)
        v = np.isfinite(A).astype(float)
        cx = np.cumsum(np.insert(x, 0, 0, 1), axis=1)
        cv = np.cumsum(np.insert(v, 0, 0, 1), axis=1)
        cxx = np.cumsum(np.insert(x * x, 0, 0, 1), axis=1)
        n = cv[:, w:] - cv[:, :-w]
        s = cx[:, w:] - cx[:, :-w]
        s2 = cxx[:, w:] - cxx[:, :-w]
        out = np.full(A.shape, np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            m = s / np.maximum(n, 1)
            var = np.where(n > 1, s2 / np.maximum(n, 1) - m * m, np.nan)
            out[:, w - 1:] = np.sqrt(np.maximum(var, 0))
        return out

    vol20 = roll_std(ret1, 20)
    amt = V * C
    cx = np.cumsum(np.insert(np.nan_to_num(amt), 0, 0, 1), axis=1)
    cv = np.cumsum(np.insert(np.isfinite(amt).astype(float), 0, 0, 1), axis=1)
    amt20 = np.full_like(amt, np.nan)
    amt20[:, 19:] = ((cx[:, 20:] - cx[:, :-20])
                     / np.maximum(cv[:, 20:] - cv[:, :-20], 1))
    barcount = np.cumsum(np.isfinite(C), axis=1)
    _, idx_ret = idx_series(cal, "sz399006")
    beta60 = np.full_like(C, np.nan)
    good = np.nan_to_num(ret1)
    for t in range(60, NDT):
        y = idx_ret[t - 59:t + 1]
        v = np.isfinite(y)
        if int(v.sum()) < 48:
            continue
        x = good[:, t - 59:t + 1]
        m = np.isfinite(ret1[:, t - 59:t + 1]) & v[None, :]
        n = m.sum(axis=1)
        ym = np.where(v, y, 0.0)
        sx = (x * m).sum(axis=1)
        sy = (ym[None, :] * m).sum(axis=1)
        sxy = (x * m * ym[None, :]).sum(axis=1)
        syy = (ym[None, :] ** 2 * m).sum(axis=1)
        nn = np.maximum(n, 1)
        cov = sxy / nn - (sx / nn) * (sy / nn)
        var = syy / nn - (sy / nn) ** 2
        ok = (n >= 48) & (var > 1e-12)
        beta60[ok, t] = cov[ok] / var[ok]
    return dict(ret=ret, vol20=vol20, amt20=amt20, barcount=barcount,
                beta60=beta60)


def make_score(feat, kind):
    NST, NDT = feat["vol20"].shape
    if kind == "beta":
        return feat["beta60"]
    if kind == "blend":
        r1 = np.zeros_like(feat["vol20"])
        r2 = np.zeros_like(feat["vol20"])
        for t in range(NDT):
            r1[:, t] = rank01(feat["ret"][20][:, t])
            r2[:, t] = rank01(feat["vol20"][:, t])
        return r1 + (1.0 - r2)
    raise SystemExit("未知评分: " + kind)


def limit_pct(code):
    return 0.20 if (code.startswith("sz30") or code.startswith("sh68")) else 0.10


def make_gate(cal, code, ma_w):
    cl, _ = idx_series(cal, code)
    ma = np.full(len(cal), np.nan)
    ma[ma_w - 1:] = np.convolve(cl, np.ones(ma_w) / ma_w, "valid")
    gate = np.zeros(len(cal), bool)
    with np.errstate(invalid="ignore"):
        gate[1:] = (cl[:-1] > ma[:-1]) & np.isfinite(ma[:-1])
    return gate


def sim_phase(codes, cal, C, feat, score, gate, i0, i1, cfg, phase=0,
              capital=1e6):
    NST = C.shape[0]
    top, reb = cfg["top"], cfg["reb"]
    if cfg["universe"] == "chinext":
        uni = np.array([c.startswith("sz30") for c in codes])
    else:
        uni = np.ones(NST, bool)
    elig = (np.isfinite(C) & (C > MIN_PRICE)
            & (feat["barcount"] >= MIN_BARS) & (feat["amt20"] >= MIN_AMOUNT)
            & uni[:, None])
    lim = np.array([limit_pct(c) for c in codes])[:, None]
    r1 = np.full_like(C, np.nan)
    r1[:, 1:] = C[:, 1:] / C[:, :-1] - 1.0
    limit_up = r1 >= lim - 0.005
    limit_dn = r1 <= -(lim - 0.005)

    cash = float(capital)
    shares = np.zeros(NST)
    entry = np.zeros(NST)
    last = np.full(NST, np.nan)
    miss = np.zeros(NST, int)
    holding = np.zeros(NST, bool)
    target = set()
    trades = []
    eq, eq_cal = [], []

    def sell_fee(amount):
        return (max(amount * COMMISSION, MIN_COMMISSION)
                + amount * STAMP + amount * TRANSFER)

    def close_pos(k, t, px):
        nonlocal cash
        amount = shares[k] * px
        net = amount - sell_fee(amount)
        cash += net
        trades.append({"code": str(codes[k]),
                       "ret": net / (shares[k] * entry[k]) - 1.0,
                       "hold": t})
        holding[k] = False
        shares[k] = 0.0
        target.discard(int(k))

    start = i0 + phase
    for j in range(max(0, start - 40), start + 1):
        f = np.isfinite(C[:, j])
        last[f] = C[f, j]
    for t in range(start, i1):
        col = C[:, t]
        fin = np.isfinite(col)
        last[fin] = col[fin]
        miss = np.where(holding & ~fin, miss + 1, 0)
        for k in np.nonzero(holding & (miss >= STALE_DAYS))[0]:
            close_pos(k, t, last[k] if last[k] > 0 else entry[k])
        on = bool(gate[t]) if gate is not None else True
        if (t - start) % reb == 0 and t > start:
            d = t - 1
            cand = np.nonzero(elig[:, d])[0]
            s = score[cand, d]
            m = np.isfinite(s)
            cand, s = cand[m], s[m]
            order = cand[np.argsort(-s, kind="stable")]
            target = set(order[:top].tolist()) if on else set()
        for k in np.nonzero(holding)[0]:
            if int(k) in target or not fin[k] or limit_dn[k, t]:
                continue
            close_pos(k, t, col[k] * (1 - SLIP))
        if (t - start) % reb == 0 and t > start and on:
            equity = cash + float(np.nansum(np.where(holding,
                                                     shares * last, 0.0)))
            for k in order:
                if int(holding.sum()) >= top:
                    break
                if holding[k] or not fin[k] or limit_up[k, t]:
                    continue
                px = col[k] * (1 + SLIP)
                budget = min(equity / top, cash)
                n_lot = int(budget / (px * LOT))
                if n_lot <= 0:
                    continue
                amount = n_lot * LOT * px
                fee = max(amount * COMMISSION, MIN_COMMISSION) \
                    + amount * TRANSFER
                if amount + fee > cash:
                    continue
                cash -= amount + fee
                shares[k] = n_lot * LOT
                entry[k] = (amount + fee) / shares[k]
                holding[k] = True
        eq.append(cash + float(np.nansum(np.where(holding, shares * last, 0.0))))
        eq_cal.append(cal[t])
    return np.array(eq), eq_cal, trades


def metrics(eq, dates, trades=None):
    eq = np.asarray(eq, float)
    out = {"total": None, "ann": None, "mdd": None, "sharpe": None,
           "trades": 0, "winrate": None, "pf": None, "days": len(eq)}
    if len(eq) < 2 or eq[0] <= 0:
        return out
    out["total"] = float(eq[-1] / eq[0] - 1.0)
    years = max((_dt.date.fromisoformat(dates[-1])
                 - _dt.date.fromisoformat(dates[0])).days / 365.25, 0.05)
    mult = eq[-1] / eq[0]
    out["ann"] = float(mult ** (1 / years) - 1.0) if mult > 0 else -1.0
    peak = np.maximum.accumulate(eq)
    out["mdd"] = float((eq / peak - 1.0).min())
    dr = np.diff(eq) / eq[:-1]
    sd = float(dr.std())
    out["sharpe"] = float(dr.mean() / sd * math.sqrt(252.0)) \
        if sd > 1e-12 else None
    if trades:
        rr = np.array([t["ret"] for t in trades])
        out["trades"] = len(rr)
        out["winrate"] = float((rr > 0).mean())
        gp = rr[rr > 0].sum()
        gl = -rr[rr <= 0].sum()
        out["pf"] = float(gp / gl) if gl > 1e-9 else None
    return out


def run_tier(codes, cal, C, feat, cfg, i0, i1, phases=None):
    score = make_score(feat, cfg["score"])
    gate = make_gate(cal, cfg["gate"], cfg["ma"]) if cfg.get("gate") else None
    n_ph = min(phases or cfg["reb"], cfg["reb"])
    curves, trades, cals = [], [], []
    for ph in range(n_ph):
        eq, ec, tr = sim_phase(codes, cal, C, feat, score, gate, i0, i1,
                               cfg, phase=ph, capital=1e6)
        curves.append(eq)
        cals.append(ec)
        trades.extend(tr)
    # 对齐到公共起点（第 reb-1 日各相位均已建仓），各自归一到 1 后等权平均
    norms, dates = [], None
    for p, (e, c) in enumerate(zip(curves, cals)):
        j0 = cfg["reb"] - 1 - p
        if j0 >= len(e) or e[j0] <= 0:
            continue
        norms.append(e[j0:] / e[j0])
        dates = c[j0:]
    if not norms:
        raise SystemExit("无有效相位曲线")
    L = min(len(e) for e in norms)
    E = np.mean([e[:L] for e in norms], axis=0) * 1e6
    dates = dates[:L]
    m = metrics(E, dates, trades)
    singles = [metrics(e[:L], dates) for e in norms]
    anns = [s["ann"] for s in singles if s["ann"] is not None]
    m["phase_ann_min"] = min(anns) if anns else None
    m["phase_ann_max"] = max(anns) if anns else None
    return m, E, dates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="all",
                    choices=list(TIERS) + ["all"])
    ap.add_argument("--segment", default="full",
                    choices=["full", "val", "val2025", "bull", "train",
                             "year", "yearly"])
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--phases", type=int, default=0,
                    help="0=用 reb 个相位")
    ap.add_argument("--tag", default="")
    ap.add_argument("--score", default=None, choices=["blend", "beta"])
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--reb", type=int, default=None)
    ap.add_argument("--gate-ma", type=int, default=None)
    ap.add_argument("--no-gate", action="store_true")
    args = ap.parse_args()

    print("加载面板 ...")
    t0 = time.time()
    codes, cal, O, H, L, C, V = load_panel()
    print(f"  {len(codes)} 只 × {len(cal)} 日 "
          f"({cal[0]}~{cal[-1]}) {time.time() - t0:.0f}s")
    print("构建特征 ...")
    feat = build_features(cal, C, V)

    segs = {
        "full": ("2022-09-01", cal[-1]),
        "train": (cal[0], "2024-12-31"),
        "val": ("2025-08-29", cal[-1]),
        "val2025": ("2025-01-02", "2025-08-28"),
        "bull": ("2025-03-18", "2026-09-04"),
    }
    tiers = list(TIERS) if args.tier == "all" else [args.tier]
    cfgs = {}
    for tier in tiers:
        cfg = dict(TIER_CFG[tier])
        if args.score:
            cfg["score"] = args.score
        if args.top:
            cfg["top"] = args.top
        if args.reb:
            cfg["reb"] = args.reb
        if args.gate_ma:
            cfg["ma"] = args.gate_ma
        if args.no_gate:
            cfg["gate"] = None
        cfgs[tier] = cfg

    def eval_one(tier, a, b):
        i0 = int(np.searchsorted(cal, a))
        i1 = int(np.searchsorted(cal, b, side="right"))
        if i1 - i0 < 40:
            return None
        cfg = cfgs[tier]
        m, E, ec = run_tier(codes, cal, C, feat, cfg, i0, i1,
                            phases=args.phases or None)
        bcode = BENCH[tier]
        bcl, _ = idx_series(cal, bcode)
        bmap = {d: v for d, v in zip(cal, bcl)}
        bcl_seg = np.array([bmap.get(d, np.nan) for d in ec], float)
        bm = metrics(bcl_seg, ec)
        m["benchmark"] = bcode
        m["bench"] = bm
        m["range"] = [ec[0], ec[-1]]
        m["excess_ann"] = (m["ann"] - bm["ann"]) \
            if (m["ann"] is not None and bm["ann"] is not None) else None
        m["excess_total"] = (m["total"] - bm["total"]) \
            if (m["total"] is not None and bm["total"] is not None) else None
        m["phase_ann_spread"] = (m["phase_ann_max"] - m["phase_ann_min"]) \
            if m["phase_ann_min"] is not None else None
        return m

    def show(tier, m):
        if not m:
            return
        bcode = m["benchmark"]
        bm = m["bench"]
        print(f"\n[{tier}] {m['range'][0]} ~ {m['range'][1]}  {cfgs[tier]}")
        print(f"  策略  总{m['total']*100:+.1f}%  年化{m['ann']*100:+.1f}%  "
              f"回撤{m['mdd']*100:+.1f}%  Sharpe {m['sharpe'] or 0:+.2f}  "
              f"交易{m['trades']}  胜率{(m['winrate'] or 0)*100:.0f}%  "
              f"PF {m['pf'] or 0:.2f}")
        print(f"  基准 {bcode}  总{bm['total']*100:+.1f}%  "
              f"年化{bm['ann']*100:+.1f}%  回撤{bm['mdd']*100:+.1f}%  "
              f"超额 {m['excess_total']*100:+.1f}pp")
        print(f"  相位年化区间 {m['phase_ann_min']*100:+.1f}% ~ "
              f"{m['phase_ann_max']*100:+.1f}%")

    if args.segment == "yearly":
        out = {t: [] for t in tiers}
        for y in (2022, 2023, 2024, 2025, 2026):
            a = f"{y}-01-01" if y > 2022 else "2022-09-01"
            b = f"{y}-12-31" if y < 2026 else cal[-1]
            print(f"\n===== {y} =====")
            for tier in tiers:
                m = eval_one(tier, a, b)
                if m:
                    m["year"] = y
                    out[tier].append(m)
                    show(tier, m)
        path = os.path.join(HERE, "research", "tiers_yearly.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.strftime("%Y-%m-%d %H:%M"),
                       "note": "逐年（2022 从 09-01 起；相位平均）",
                       "results": out}, f, ensure_ascii=False, indent=1)
        print(f"\n写入 {path}，耗时 {time.time() - t0:.0f}s")
        return

    if args.segment == "year":
        if not args.year:
            raise SystemExit("--segment year 需要 --year")
        segs["year"] = (f"{args.year}-01-01", f"{args.year}-12-31")
    a, b = segs[args.segment]
    i0 = int(np.searchsorted(cal, a))
    i1 = int(np.searchsorted(cal, b, side="right"))
    if i1 - i0 < 40:
        raise SystemExit(f"区间过短: {cal[i0]}~{cal[i1-1]}")
    seg_name = args.segment if args.segment != "year" else str(args.year)
    print(f"区间 {cal[i0]} ~ {cal[i1-1]}（{i1 - i0} 日）")

    out = {}
    for tier in tiers:
        m = eval_one(tier, a, b)
        out[tier] = m
        show(tier, m)

    suffix = f"_{args.tag}" if args.tag else ""
    path = os.path.join(HERE, "research",
                        f"tiers_{seg_name}{suffix}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%d %H:%M"),
                   "segment": seg_name, "range": [cal[i0], cal[i1 - 1]],
                   "phases_note": "phase-averaged (tranche) primary",
                   "results": out}, f, ensure_ascii=False, indent=1)
    print(f"\n写入 {path}，耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
