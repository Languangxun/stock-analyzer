#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_v5_cross_section.py - V5 横截面组合（低换手排序）优化实验

思路：不再做"每股各自选策略"（已被证明样本外归零），改为用 v4 Walk-Forward
预测做**横截面排序**：每日早盘按 T-1 分数选 TopN 等权建仓、持有固定天数后轮换，
可叠加指数趋势闸门。防过拟合协议：日历前 60% 选参、后 40% 留出只报告。

用法：
  python backtest_v5_cross_section.py --grid
  python backtest_v5_cross_section.py --score ml_dyn --top 10 --hold 5 \
      --mkt ma20 --entry open --exit open --report
"""
import argparse
import itertools
import json
import os
import sys
import time
from datetime import date as _date

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_gui as sg  # noqa: E402

SCORES = ("ml_dyn", "p_up", "q50", "ens", "rev5", "rev20")
TOPS = (10, 20)
HOLDS = (5, 10, 20)
MKTS = ("none", "ma20", "ma60")


def build_mats(preds):
    cal = sorted({d for r in preds for d in r["dates"]})
    idx = {d: k for k, d in enumerate(cal)}
    ns, nc = len(preds), len(cal)

    def mk():
        return np.full((ns, nc), np.nan, np.float32)

    M = {k: mk() for k in ("open", "high", "low", "close", "p_up",
                           "ml_dyn", "adaptive", "q50")}
    M["h_choice"] = np.full((ns, nc), -1, np.int16)
    codes = []
    for i, r in enumerate(preds):
        codes.append(r["code"])
        cols = np.array([idx[d] for d in r["dates"]], np.int64)
        M["open"][i, cols] = r["open"]
        M["high"][i, cols] = r["high"]
        M["low"][i, cols] = r["low"]
        M["close"][i, cols] = r["close"]
        M["p_up"][i, cols] = r["p_up"]
        M["ml_dyn"][i, cols] = r["ml_dyn"]
        M["adaptive"][i, cols] = r["adaptive"]
        M["q50"][i, cols] = r["q"]["50"]
        M["h_choice"][i, cols] = r["h_choice"]
    M["has_bar"] = np.isfinite(M["close"])
    # 纯价格因子（因果）：5/20 日反转
    cl = M["close"]
    for w, name in ((5, "rev5"), (20, "rev20")):
        r = np.full((ns, nc), np.nan, np.float32)
        with np.errstate(invalid="ignore", divide="ignore"):
            r[:, w:] = cl[:, w:] / cl[:, :-w] - 1.0
        M[name] = -r
    # 三模型横截面集成：ml_dyn/q50/p_up 当日百分位均值（只对有限值）
    M["ens"] = np.full((ns, nc), np.nan, np.float32)
    for t in range(nc):
        acc = np.zeros(ns, np.float64)
        cnt = np.zeros(ns, np.int16)
        for k in ("ml_dyn", "q50", "p_up"):
            v = M[k][:, t].astype(np.float64)
            m = np.isfinite(v)
            n = int(m.sum())
            if n < 10:
                continue
            rk = np.full(ns, np.nan)
            rk[m] = v[m].argsort().argsort() / max(n - 1, 1)
            fin = np.isfinite(rk)
            acc[fin] += rk[fin]
            cnt[fin] += 1
        ok = cnt > 0
        M["ens"][ok, t] = (acc[ok] / cnt[ok]).astype(np.float32)
    ret1 = np.full((ns, nc), np.nan, np.float32)
    with np.errstate(invalid="ignore", divide="ignore"):
        ret1[:, 1:] = M["close"][:, 1:] / M["close"][:, :-1] - 1.0
    M["ret1"] = ret1
    lim = np.array([sg._v4_limit_pct(c) for c in codes], np.float64)[:, None]
    with np.errstate(invalid="ignore"):
        M["limit_up"] = ret1 >= (lim - 0.005)
        M["limit_dn"] = ret1 <= -(lim - 0.005)
    return cal, M, codes


def market_mask(cal, kind, ma_w):
    if kind == "none":
        return None
    rows = sg.get_daily("sh000001")
    cl = {r["date"]: r["close"] for r in rows if r.get("close")}
    vals, last = [], None
    for d in cal:
        v = cl.get(d)
        if v:
            last = v
        vals.append(last)
    n = len(vals)
    mask = np.zeros(n, bool)
    for t in range(n):
        # 早盘决策：用 T-1 及以前的指数收盘判断 T 日是否可开仓
        if t < ma_w + 1:
            continue
        prev = vals[t - 1]
        seg = [v for v in vals[:t] if v][-ma_w:]
        if len(seg) < ma_w or not prev:
            continue
        mask[t] = prev > sum(seg) / len(seg)
    return mask


def sim(cal, M, codes, *, score_key, top_n, hold, mkt_mask=None,
        entry="open", exit_px="open", start=0, end=None, stale=20,
        cash0=1e6):
    end = len(cal) if end is None else end
    buy_mult, sell_mult = 1.001, 0.999
    exec_in_open = entry == "open"
    exec_out_open = exit_px == "open"
    cash = cash0
    pos = {}
    last_px = np.zeros(len(codes), np.float64)
    eq_curve, trades = [], []
    for t in range(start, end):
        col = M["close"][:, t]
        upd = np.isfinite(col)
        last_px[upd] = col[upd].astype(np.float64)
        # ---- 到期/退市 卖出 ----
        for ks in sorted(pos):
            p = pos[ks]
            if not M["has_bar"][ks, t]:
                p["miss"] = p.get("miss", 0) + 1
                if p["miss"] >= stale:
                    px = last_px[ks] if last_px[ks] > 0 else p["entry"]
                    net = px * sell_mult
                    cash += p["shares"] * net
                    trades.append({"ret": net / p["buy_net"] - 1.0,
                                   "pnl": p["shares"] * (net - p["buy_net"]),
                                   "hold": t - p["t_in"]})
                    del pos[ks]
                continue
            if t - p["t_in"] >= hold and not M["limit_dn"][ks, t]:
                px = (float(M["open"][ks, t]) if exec_out_open
                      else float(col[ks]))
                if not (px and px > 0):
                    px = float(col[ks])
                net = px * sell_mult
                cash += p["shares"] * net
                trades.append({"ret": net / p["buy_net"] - 1.0,
                               "pnl": p["shares"] * (net - p["buy_net"]),
                               "hold": t - p["t_in"]})
                del pos[ks]
        # ---- 早盘按 T-1 分数轮换建仓（t>=1 防止列索引回卷到未来）----
        if t >= 1 and len(pos) < top_n:
            sc = np.asarray(M[score_key][:, t - 1], np.float64).copy()
            ok = (np.isfinite(sc) & M["has_bar"][:, t]
                  & ~M["limit_up"][:, t])
            if mkt_mask is not None and not mkt_mask[t]:
                ok[:] = False
            sc[~ok] = -np.inf
            cand = np.argsort(-sc, kind="stable")
            for ks in cand:
                if len(pos) >= top_n:
                    break
                if ks in pos or not np.isfinite(sc[ks]):
                    continue
                px = (float(M["open"][ks, t]) if exec_in_open
                      else float(col[ks]))
                if not (px and px > 0):
                    continue
                eq0 = cash + sum(pp["shares"] * last_px[k2]
                                 for k2, pp in pos.items())
                buy_net = px * buy_mult
                shares = int(eq0 / top_n / buy_net / 100.0) * 100
                if shares <= 0 or shares * buy_net > cash:
                    continue
                cash -= shares * buy_net
                pos[ks] = {"t_in": t, "shares": shares, "buy_net": buy_net,
                           "entry": px, "miss": 0}
        eq_curve.append(cash + sum(pp["shares"] * last_px[k2]
                                   for k2, pp in pos.items()))
    return eq_curve, trades


def run_config(cal, M, codes, start, end, masks, **kw):
    mask = masks.get(kw.get("mkt", "none"))
    eq, tr = sim(cal, M, codes, start=start, end=end,
                 mkt_mask=mask, **{k: v for k, v in kw.items() if k != "mkt"})
    return sg._v4_metrics(eq, cal[start:end], tr)


def bench_metrics(cal, start, end):
    rows = sg.get_daily("sh000001")
    cl = {r["date"]: r["close"] for r in rows if r.get("close")}
    seg = [cl.get(d) for d in cal[start:end]]
    vals = [v for v in seg if v]
    if len(vals) < 2:
        return None
    eq = [v / vals[0] for v in vals]
    return sg._v4_metrics(eq, [d for d, v in zip(cal[start:end], seg) if v],
                          [])


def fmt(m):
    if not m:
        return "n/a"
    return (f"ann={m['ann']*100:+.1f}% mdd={m['mdd']*100:+.1f}% "
            f"cal={m['calmar'] if m['calmar'] is None else round(m['calmar'],2)} "
            f"sharpe={m['sharpe'] if m['sharpe'] is None else round(m['sharpe'],2)} "
            f"win={(m['winrate'] or 0)*100:.0f}% tr={m['trades']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="research/v4_preds.pkl")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--score", default="ml_dyn", choices=SCORES)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--mkt", default="none", choices=MKTS)
    ap.add_argument("--entry", default="open", choices=("open", "close"))
    ap.add_argument("--exit", dest="exit_px", default="open",
                    choices=("open", "close"))
    ap.add_argument("--design-frac", type=float, default=0.6)
    ap.add_argument("--cal-days", type=int, default=500,
                    help="只取 union 日历最后 N 个交易日（默认500，"
                         "避免退市股远古片段稀释横截面）")
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    t0 = time.time()
    import pickle
    with open(args.cache, "rb") as f:
        preds = pickle.load(f)["preds"]
    print(f"preds {len(preds)} 只 ({time.time()-t0:.0f}s)")
    cal, M, codes = build_mats(preds)
    if args.cal_days and len(cal) > args.cal_days:
        j0 = len(cal) - args.cal_days
        cal = cal[j0:]
        M = {k: v[:, j0:] for k, v in M.items()}
    split = int(len(cal) * args.design_frac)
    masks = {k: market_mask(cal, k, int(k[2:])) for k in MKTS
             if k != "none"}
    masks["none"] = None
    print(f"日历 {cal[0]}~{cal[-1]} ({len(cal)}日)  "
          f"设计段 [0,{split}) 留出段 [{split},{len(cal)})")

    combos = list(itertools.product(SCORES, TOPS, HOLDS, MKTS))
    if not args.grid:
        combos = [(args.score, args.top, args.hold, args.mkt)]
    rows = []
    for score, top, hold, mkt in combos:
        kw = dict(score_key=score, top_n=top, hold=hold, mkt=mkt,
                  entry=args.entry, exit_px=args.exit_px)
        md = run_config(cal, M, codes, 0, split, masks, **kw)
        mh = run_config(cal, M, codes, split, len(cal), masks, **kw)
        rows.append({"score": score, "top": top, "hold": hold, "mkt": mkt,
                     "design": md, "holdout": mh})
        if not args.grid:
            print("design ", fmt(md))
            print("holdout", fmt(mh))

    out = {"ts": time.strftime("%Y-%m-%d %H:%M"),
           "calendar": [cal[0], cal[-1]], "split": cal[split],
           "entry": args.entry, "exit": args.exit_px,
           "design_frac": args.design_frac, "rows": rows}
    if args.grid:
        pool = [r for r in rows
                if (r["design"].get("trades") or 0) >= args.min_trades]
        if not pool:
            pool = rows
        best = max(pool, key=lambda r: (r["design"]["calmar"] or -9))
        print("\n=== 设计段最优（按 Calmar，交易≥"
              f"{args.min_trades}）: {best['score']} top={best['top']} "
              f"hold={best['hold']} mkt={best['mkt']} ===")
        print("design ", fmt(best["design"]))
        print("holdout", fmt(best["holdout"]))
        ho = sorted(r["holdout"]["ann"] for r in rows)
        print(f"全部 {len(rows)} 组留出段年化：最差 {ho[0]*100:+.1f}% / "
              f"中位 {ho[len(ho)//2]*100:+.1f}% / 最好 {ho[-1]*100:+.1f}%")
        print("留出段 Top5（按留出年化，仅诊断选择偏差）：")
        for r in sorted(rows, key=lambda x: -x["holdout"]["ann"])[:5]:
            print(f"  {r['score']} top={r['top']} hold={r['hold']} "
                  f"mkt={r['mkt']}: design {fmt(r['design'])} | "
                  f"holdout {fmt(r['holdout'])}")
        out["best_design"] = best
        out["holdout_ann_dist"] = [r["holdout"]["ann"] for r in rows]
    bd = bench_metrics(cal, 0, split)
    bh = bench_metrics(cal, split, len(cal))
    out["benchmark"] = {"design": bd, "holdout": bh}
    print("\n基准上证: design", fmt(bd))
    print("           holdout", fmt(bh))

    suffix = f"_{args.tag}" if args.tag else ""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "research", f"v5_cross_section{suffix}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"写入 {path}，耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
