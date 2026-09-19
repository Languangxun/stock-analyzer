#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_aggr_lottery.py - 激进「核心+彩票」双袖套引擎（2026-09-13，开发中）

用途：研究/落地「以小博大」激进策略，主板/创业板可分别运行。
  核心袖套：按市值（当前 mktcap 推股本 × 当日价）top-N 等权，定期调仓，
            近似跟踪指数重仓结构（创业板核心在 val 窗口跑赢创业板指）。
  彩票袖套：低价股（价格横截面低分位）中选「放量 + 急跌」的飞刀，直接接，
            固定持有 N 日，小比例、多仓分散，吃右尾。

规则（防前视，日频权益）：
  - 信号取 T-1（VR/DROP/低价分位），T 日收盘成交（含滑点/成本）；
  - 旧版「同日信号+同日收盘成交」为前视 bug，已于 2026-09-15 修复，
    修复后 val 双引擎不再显著跑赢创业板指（见 README v6.0）；
  - 停牌顺延（用最后可得收盘盯市）；ST 与上市不足 min_bars 的票剔除；
  - 无杠杆；空仓允许（现金）。

用法：
  python backtest_aggr_lottery.py --universe main --segment val --benchmark sh000001
  python backtest_aggr_lottery.py --universe chinext --grid --benchmark sz399006
"""
# --- 目录引导：backtests/ 下运行也能导入 stock_gui/factor_lab，产物写项目根 ---
import os as _os_boot
import sys as _sys_boot

ROOT = _os_boot.path.dirname(_os_boot.path.dirname(
    _os_boot.path.abspath(__file__)))
if ROOT not in _sys_boot.path:
    _sys_boot.path.insert(0, ROOT)
# --- 目录引导结束 ---
import argparse
import datetime as _dt
import json
import math
import os
import statistics
import time

import numpy as np

import stock_gui as sg

FIELDS = ("total", "ann", "mdd", "calmar", "sharpe", "trades",
          "avg_hold", "winrate", "pf", "tail20", "tail50", "max_trade")


def load_universe(prefixes, min_bars=250):
    """加载股票池并按显示缩放系数把 hfq 价转成乘法前复权（≈现价）口径。

    库内价格为后复权(hfq)，含累计分红因子，跨股票比价会失真；这里给每只
    乘上 sg._get_adjust 的缩放系数（收益率不变，价格水平≈现价）。缺失
    系数的股票保持 hfq 原值并计数（低价分位/价格上限可能失真）。
    本脚本只用 close（比价/收益/股本反推），故仅缩放 close。
    """
    with sg.db_conn() as conn:
        cap = {c: (m or 0.0) for c, m in
               conn.execute("SELECT code, mktcap FROM stocks")}
        names = {c: (n or "") for c, n in
                 conn.execute("SELECT code, name FROM stocks")}
        rows = conn.execute(
            "SELECT code,date,close,vol FROM daily_bars ORDER BY code,date")
        data = {}
        for c, d, cl, v in rows:
            if cl:
                data.setdefault(c, []).append((d, cl, v or 0.0))
    codes = sorted(c for c, seq in data.items()
                   if c.startswith(prefixes) and len(seq) >= min_bars
                   and "ST" not in names.get(c, "").upper())
    cal = sorted({d for c in codes for d, _, _ in data[c]})
    didx = {d: i for i, d in enumerate(cal)}
    n, nc = len(codes), len(cal)
    C = np.full((n, nc), np.nan)
    V = np.zeros((n, nc))
    shares = np.zeros(n)
    n_missing_adj = 0
    for r, c in enumerate(codes):
        seq = data[c]
        adj = sg._get_adjust(c)              # hfq → 乘法前复权（≈现价）
        if not (adj and adj > 0):
            n_missing_adj += 1
            adj = 1.0
        cols = np.array([didx[d] for d, _, _ in seq], np.int64)
        C[r, cols] = [x[1] * adj for x in seq]
        V[r, cols] = [x[2] for x in seq]
        last = seq[-1][1] * adj              # 缩放后收盘价，与当前市值同口径
        if cap.get(c, 0.0) > 0 and last > 0:
            shares[r] = cap[c] / last
    return cal, codes, C, V, shares, n_missing_adj


def _roll_mean(A, w):
    """行向滚动均值（NaN 视为 0，仅统计有效个数；前 w-1 列 NaN）。"""
    n, nc = A.shape
    x = np.nan_to_num(A, nan=0.0)
    v = np.isfinite(A).astype(float)
    cx = np.cumsum(np.insert(x, 0, 0.0, axis=1), axis=1)
    cv = np.cumsum(np.insert(v, 0, 0.0, axis=1), axis=1)
    sm = cx[:, w:] - cx[:, :-w]
    cnt = cv[:, w:] - cv[:, :-w]
    out = np.full((n, nc), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        out[:, w - 1:] = np.where(cnt > 0, sm / cnt, np.nan)
    return out


def precompute(C, V):
    """因果特征：VR(5/20)、DROP(5日)、有效价格。"""
    vm5 = _roll_mean(V, 5)
    vm20 = _roll_mean(V, 20)
    with np.errstate(invalid="ignore", divide="ignore"):
        VR = np.where(vm20 > 0, vm5 / vm20, np.nan)
    Cn = C.copy()
    DROP = np.full_like(C, np.nan)
    DROP[:, 5:] = Cn[:, 5:] / Cn[:, :-5] - 1.0
    return VR, DROP


def _metrics(eq, dates, trades):
    eq = np.asarray(eq, float)
    out = {k: None for k in FIELDS}
    if len(eq) < 2 or eq[0] <= 0:
        return out
    out["total"] = float(eq[-1] / eq[0] - 1.0)
    years = max((_dt.date.fromisoformat(dates[-1])
                 - _dt.date.fromisoformat(dates[0])).days / 365.25, 0.25)
    mult = eq[-1] / eq[0] if eq[0] > 0 else 0.0
    out["ann"] = float(mult ** (1.0 / years) - 1.0) if mult > 0 else -1.0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    out["mdd"] = float(mdd)
    out["calmar"] = (out["ann"] / abs(mdd)) if mdd < -1e-9 else None
    daily = np.diff(eq) / eq[:-1]
    sd = float(daily.std())
    out["sharpe"] = float(daily.mean()) / sd * math.sqrt(252.0) \
        if sd > 1e-12 else None
    if trades:
        rets = [t for t, _ in trades]
        wins = [x for x in rets if x > 0]
        losses = [-x for x in rets if x <= 0]
        out["trades"] = len(rets)
        out["winrate"] = len(wins) / len(rets)
        gp, gl = sum(wins), sum(losses)
        out["pf"] = gp / gl if gl > 1e-9 else None
        out["avg_hold"] = statistics.mean(h for _, h in trades)
        out["tail20"] = sum(1 for x in rets if x > 0.20) / len(rets)
        out["tail50"] = sum(1 for x in rets if x > 0.50) / len(rets)
        out["max_trade"] = max(rets)
    return out


def benchmark_stats(code, cal, i0, i1):
    rows = sg.get_daily(code)
    d = {r["date"]: (r.get("close") or 0.0) for r in rows}
    vals = [d.get(cal[i]) for i in range(i0, i1)]
    vals = [v for v in vals if v]
    if len(vals) < 30:
        return None
    total = vals[-1] / vals[0] - 1.0
    years = max((_dt.date.fromisoformat(cal[i1 - 1])
                 - _dt.date.fromisoformat(cal[i0])).days / 365.25, 0.25)
    ann = (1 + total) ** (1 / years) - 1
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"total": total, "ann": ann, "mdd": mdd}


def run(cal, C, V, VR, DROP, cap_shares, i0, i1, *, capital=1e6,
        core_top=20, core_frac=0.90, core_reb=20, core_max_price=None,
        etf_close=None, etf_ma=None,
        lot_frac=0.10, lot_k=10,
        lot_step=5, lot_hold=10, price_q=0.30, vr_th=1.5, drop_th=-0.08,
        slip=0.001, commission=0.00025, min_commission=5.0, stamp=0.001,
        transfer=0.00001, hand=100):
    """双袖套日频回测（小资金口径：整数手 + 最低佣金 + 印花税/过户费）。

    买入按可用现金与整手约束，买不起（不足 1 手）自动跳过。"""
    ns, nc = C.shape
    cash = float(capital)
    core = {}
    lots = []
    trades = []
    eq_curve, eq_dates = [], []
    last_px = np.zeros(ns)

    def _buy_fee(amount):
        return max(amount * commission, min_commission) + amount * transfer

    def _sell_fee(amount):
        return (max(amount * commission, min_commission)
                + amount * stamp + amount * transfer)

    etf_last = [float("nan")]

    def _pos_value():
        v = cash
        for k, p in core.items():
            px = etf_last[0] if k == -1 else last_px[k]
            if np.isfinite(px):
                v += p["shares"] * px
        v += sum(p["shares"] * last_px[p["k"]] for p in lots)
        return v

    for t, d in enumerate(range(i0, i1)):
        col = C[:, d]
        upd = np.isfinite(col)
        last_px[upd] = col[upd]
        if etf_close is not None and np.isfinite(etf_close[d]):
            etf_last[0] = etf_close[d]
        # --- 彩票到期卖出 ---
        keep = []
        for p in lots:
            if p["exit_t"] <= d:
                px = col[p["k"]]
                if not np.isfinite(px):
                    keep.append(p)              # 停牌顺延
                    continue
                amount = p["shares"] * px
                proceeds = amount - _sell_fee(amount)
                cash += proceeds
                trades.append(((proceeds - p["cost"]) / p["cost"],
                               d - p["t_in"]))
            else:
                keep.append(p)
        lots = keep
        # --- ETF 核心（小资金可负担；可选 MA 趋势闸门）---
        if etf_close is not None:
            px = etf_close[d]
            if np.isfinite(px) and px > 0:
                on = True
                if etf_ma:
                    j = d - 1
                    seg = etf_close[max(0, d - int(etf_ma)):d]
                    seg = seg[np.isfinite(seg)]
                    on = (len(seg) >= int(etf_ma)
                          and np.isfinite(etf_close[j])
                          and etf_close[j] > seg.mean())
                if not on and core:
                    for k, p in list(core.items()):
                        amount = p["shares"] * px
                        cash += amount - _sell_fee(amount)
                        del core[k]
                elif on and not core:
                    per = _pos_value() * core_frac
                    lots_n = int(per // (px * (1 + slip) * hand))
                    if lots_n > 0:
                        amount = lots_n * hand * px * (1 + slip)
                        fee = _buy_fee(amount)
                        if amount + fee <= cash:
                            cash -= amount + fee
                            core[-1] = {"shares": lots_n * hand,
                                        "cost": amount + fee,
                                        "buy": px * (1 + slip)}
        # --- 股票核心调仓（未启用 ETF 核心时）---
        elif core_reb and core_top and t % core_reb == 0:
            for k, p in core.items():
                px = col[k]
                if np.isfinite(px):
                    amount = p["shares"] * px
                    cash += amount - _sell_fee(amount)
                else:
                    cash += p["cost"]           # 停牌：近似按成本退出
            core = {}
            cap_now = C[:, d - 1 if d > 0 else d] * cap_shares
            ok = np.isfinite(cap_now) & (cap_now > 0)
            if core_max_price:
                ok &= (col > 0) & (col <= float(core_max_price))
            cand = np.nonzero(ok)[0]
            if len(cand):
                cand = cand[np.argsort(-cap_now[cand])]
                per = _pos_value() * core_frac / max(1, core_top)
                filled = 0
                for k in cand:
                    if filled >= core_top:
                        break
                    px = col[k]
                    lots_n = int(per // (px * (1 + slip) * hand))
                    if lots_n <= 0:
                        continue            # 买不起：按市值顺序找下一只
                    amount = lots_n * hand * px * (1 + slip)
                    fee = _buy_fee(amount)
                    if amount + fee > cash:
                        lots_n = int(cash * 0.999
                                     // (px * (1 + slip) * hand))
                        if lots_n <= 0:
                            continue
                        amount = lots_n * hand * px * (1 + slip)
                        fee = _buy_fee(amount)
                        if amount + fee > cash:
                            continue
                    cash -= amount + fee
                    core[k] = {"shares": lots_n * hand,
                               "cost": amount + fee,
                               "buy": px * (1 + slip)}
                    filled += 1
        # --- 彩票入场（信号取 T-1，T 日收盘成交）---
        if lot_frac > 0 and lot_k and t % lot_step == 0 and d > 0:
            per = _pos_value() * lot_frac / max(1, lot_k)
            px = col
            prev = C[:, d - 1]
            ok = np.isfinite(px) & (px > 0) & np.isfinite(prev) & (prev > 0)
            m = ok & (VR[:, d - 1] >= vr_th) & (DROP[:, d - 1] <= drop_th)
            cand = np.nonzero(m)[0]
            if len(cand) > 1:
                prices = np.sort(prev[ok])
                cut = prices[int(len(prices) * price_q)]
                cand = np.array([k for k in cand if prev[k] <= cut
                                 and k not in core
                                 and not any(p["k"] == k for p in lots)])
            if len(cand):
                cand = cand[np.argsort(DROP[cand, d - 1])]
                for k in cand:
                    if len(lots) >= lot_k:
                        break
                    px_k = col[k]
                    lots_n = int(per // (px_k * (1 + slip) * hand))
                    if lots_n <= 0:
                        continue                     # 买不起：跳过
                    amount = lots_n * hand * px_k * (1 + slip)
                    fee = _buy_fee(amount)
                    if amount + fee > cash:
                        continue
                    cash -= amount + fee
                    lots.append({"k": int(k), "shares": lots_n * hand,
                                 "cost": amount + fee,
                                 "buy": px_k * (1 + slip),
                                 "exit_t": min(d + lot_hold, nc - 1),
                                 "t_in": d})
        eq_curve.append(_pos_value())
        eq_dates.append(cal[d])
    return _metrics(eq_curve, eq_dates, trades), eq_curve, eq_dates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="main",
                    choices=("main", "chinext", "all"))
    ap.add_argument("--segment", default="val",
                    choices=("val", "train", "all"))
    ap.add_argument("--days", type=int, default=360,
                    help="val 段长度（日历交易日，默认最后 360 日）")
    ap.add_argument("--benchmark", default=None)
    ap.add_argument("--core-top", type=int, default=20)
    ap.add_argument("--core-frac", type=float, default=0.90)
    ap.add_argument("--core-reb", type=int, default=20)
    ap.add_argument("--core-max-price", type=float, default=None,
                    help="核心只买价格≤该值（元）的票，给小资金控一手成本")
    ap.add_argument("--core-etf", default=None,
                    help="用 ETF 作核心（小资金），如 sz159915；配合 --core-etf-ma")
    ap.add_argument("--core-etf-ma", type=int, default=0,
                    help="ETF 核心趋势闸门均线（0=买入持有）")
    ap.add_argument("--lot-frac", type=float, default=0.10)
    ap.add_argument("--lot-k", type=int, default=10)
    ap.add_argument("--lot-step", type=int, default=5)
    ap.add_argument("--lot-hold", type=int, default=10)
    ap.add_argument("--price-q", type=float, default=0.30)
    ap.add_argument("--vr", type=float, default=1.5)
    ap.add_argument("--drop", type=float, default=-0.08)
    ap.add_argument("--cost", type=float, default=0.001,
                    help="滑点（单边，默认 0.1%%；小资金建议 0.1~0.2%%）")
    ap.add_argument("--capital", type=float, default=1e6,
                    help="初始本金（元）；小资金如 10000/50000")
    ap.add_argument("--commission", type=float, default=0.00025)
    ap.add_argument("--min-commission", type=float, default=5.0)
    ap.add_argument("--stamp", type=float, default=0.001)
    ap.add_argument("--transfer", type=float, default=0.00001)
    ap.add_argument("--hand", type=int, default=100, help="一手股数")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--min-active", type=int, default=500,
                    help="裁剪当日有K线股票数<N 的稀疏早期日历")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    PREFIX = {"main": ("sh60", "sz00"), "chinext": ("sz30",),
              "all": ("sh60", "sz00", "sz30")}[args.universe]
    BENCH = args.benchmark or {"main": "sh000001",
                               "chinext": "sz399006",
                               "all": "sh000001"}[args.universe]
    t0 = time.time()
    print(f"加载 {args.universe} 股票池 ...")
    cal, codes, C, V, shares, n_missing_adj = load_universe(PREFIX)
    print(f"  adjust 缩放：{len(codes)-n_missing_adj}/{len(codes)} 只有系数；"
          f"{n_missing_adj} 只缺失（用 hfq 原值，低价筛选/价格上限可能失真）")
    VR, DROP = precompute(C, V)
    active = np.isfinite(C).sum(axis=0)
    good = np.nonzero(active >= args.min_active)[0]
    if len(good):
        a, b = int(good[0]), int(good[-1]) + 1
        if a > 0 or b < len(cal):
            cal, C, V = cal[a:b], C[:, a:b], V[:, a:b]
            VR, DROP = VR[:, a:b], DROP[:, a:b]
    print(f"  {len(codes)} 只 × {len(cal)} 日（{cal[0]}~{cal[-1]}）")
    etf_close = None
    if args.core_etf:
        try:
            rows = sg.get_daily(args.core_etf)
            dmap = {r["date"]: (r.get("close") or np.nan) for r in rows}
            etf_close = np.array([dmap.get(x, np.nan) for x in cal], float)
            valid = int(np.isfinite(etf_close).sum())
            print(f"  ETF核心 {args.core_etf}: {valid}/{len(cal)} 日有效")
        except Exception as e:
            print(f"  ETF核心 {args.core_etf} 加载失败: {e}")
    if args.segment == "all":
        i0, i1 = 0, len(cal)
    else:
        split = max(0, len(cal) - args.days)
        i0, i1 = (0, split) if args.segment == "train" else (split, len(cal))
    dates = [cal[i] for i in range(i0, i1)]
    print(f"  段 {args.segment}: {dates[0]} ~ {dates[-1]}（{len(dates)} 日）")

    grid = [dict()]
    holdout_split = False
    if args.grid:
        grid = [dict(lot_frac=lf, lot_k=lk, lot_hold=hd,
                     vr_th=vr, drop_th=dp)
                for lf in (0.10, 0.20)
                for lk in (10, 20)
                for hd in (5, 10)
                for vr in (1.5, 2.0)
                for dp in (-0.06, -0.08, -0.10)]
        # 参数网格留出：前 2/3 段选参、后 1/3 段仅报告（不参与选参），
        # 避免 48 组参数在同一段内选优带来的选择偏差。
        holdout_split = (i1 - i0) >= 90
    if holdout_split:
        cut = i0 + (i1 - i0) * 2 // 3
        sel_i0, sel_i1 = i0, cut
        ho_i0, ho_i1 = cut, i1
        print(f"  网格留出：selection {cal[sel_i0]} ~ {cal[sel_i1-1]}"
              f"（前 2/3）选参；holdout {cal[ho_i0]} ~ {cal[ho_i1-1]}"
              f"（后 1/3，未参与选参）")
    else:
        sel_i0, sel_i1 = i0, i1
        ho_i0, ho_i1 = i0, i1
        if args.grid:
            print("  ⚠ 段太短，网格未做留出切分（selection=holdout）")
    base_kw = dict(capital=args.capital,
                   core_top=args.core_top, core_frac=args.core_frac,
                   core_reb=args.core_reb,
                   core_max_price=args.core_max_price,
                   etf_close=etf_close, etf_ma=args.core_etf_ma,
                   lot_step=args.lot_step, price_q=args.price_q,
                   slip=args.cost, commission=args.commission,
                   min_commission=args.min_commission,
                   stamp=args.stamp, transfer=args.transfer,
                   hand=args.hand)
    out = []
    for kw in grid:
        kw_run = dict(base_kw,
                      lot_frac=kw.get("lot_frac", args.lot_frac),
                      lot_k=kw.get("lot_k", args.lot_k),
                      lot_hold=kw.get("lot_hold", args.lot_hold),
                      vr_th=kw.get("vr_th", args.vr),
                      drop_th=kw.get("drop_th", args.drop))
        m, _, _ = run(cal, C, V, VR, DROP, shares, sel_i0, sel_i1, **kw_run)
        rec = {"kw": kw, **{k: m[k] for k in FIELDS}}
        if holdout_split:
            mh, _, _ = run(cal, C, V, VR, DROP, shares, ho_i0, ho_i1,
                           **kw_run)
            rec["holdout"] = {k: mh[k] for k in FIELDS}
        out.append(rec)
        print(f"  [selection] 本金{args.capital:.0f} "
              f"frac={rec['kw'].get('lot_frac', args.lot_frac)} "
              f"k={rec['kw'].get('lot_k', args.lot_k)} "
              f"hold={rec['kw'].get('lot_hold', args.lot_hold)} "
              f"vr={rec['kw'].get('vr_th', args.vr)} "
              f"drop={rec['kw'].get('drop_th', args.drop)} | "
              f"总{m['total']*100:+.1f}% 年化{m['ann']*100:+.1f}% "
              f"回撤{m['mdd']*100:+.1f}% Calmar{(m['calmar'] or 0):+.2f} "
              f"交易{m['trades']} 胜率{(m['winrate'] or 0)*100:.0f}% "
              f">20%{(m['tail20'] or 0)*100:.1f}% >50%{(m['tail50'] or 0)*100:.1f}%")
        if holdout_split:
            print(f"      holdout（未参与选参）: 总{mh['total']*100:+.1f}% "
                  f"年化{mh['ann']*100:+.1f}% 回撤{mh['mdd']*100:+.1f}% "
                  f"Calmar{(mh['calmar'] or 0):+.2f} 交易{mh['trades']} "
                  f"胜率{(mh['winrate'] or 0)*100:.0f}% "
                  f">20%{(mh['tail20'] or 0)*100:.1f}% "
                  f">50%{(mh['tail50'] or 0)*100:.1f}%")
    bench = benchmark_stats(BENCH, cal, i0, i1)
    if bench:
        print(f"  基准 {BENCH}: 总{bench['total']*100:+.1f}% "
              f"年化{bench['ann']*100:+.1f}% 回撤{bench['mdd']*100:+.1f}%")
    path = os.path.join(ROOT,
                        "research", f"aggr_lottery_{args.universe}"
                        f"_{args.segment}"
                        f"{('_' + args.tag) if args.tag else ''}.json")
    json.dump({"ts": time.strftime("%Y-%m-%d %H:%M"),
               "universe": args.universe, "segment": args.segment,
               "capital": args.capital,
               "range": [dates[0], dates[-1]], "benchmark": bench,
               "holdout_split": holdout_split,
               "selection_range": [cal[sel_i0], cal[sel_i1-1]],
               "holdout_range": ([cal[ho_i0], cal[ho_i1-1]]
                                 if holdout_split else None),
               "results": out}, open(path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"写入 {path}，耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
