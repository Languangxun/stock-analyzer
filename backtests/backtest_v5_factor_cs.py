#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_v5_factor_cs.py - 稳定因子集横截面组合（factor_lab 面板）

用 factor_lab 稳定性筛选出的因子（训练段定方向）构建横截面百分位合成分，
在验证段按 TopN/持有 N 日轮换（次日开盘成交），验证因子集是否有组合级 alpha。
防过拟合：方向与因子集来自训练段；验证段分前后半段分别报告。

用法：
  python backtest_v5_factor_cs.py --top 20 --hold 5
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
import json
import os
import sys
from datetime import date as _date

import numpy as np

sys.path.insert(0, ROOT)
import stock_gui as sg  # noqa: E402

STABLE = ["量能", "L2同行业", "L3同市值", "筹码压力", "BIAS20", "BIAS60",
          "VOLA20"]


def daily_rank_cs(vals):
    """当日横截面百分位（NaN 保留 NaN）。"""
    out = np.full(len(vals), np.nan)
    m = np.isfinite(vals)
    n = int(m.sum())
    if n >= 5:
        out[m] = vals[m].argsort().argsort() / max(n - 1, 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="research/factor_lab/panel.npz")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--mkt", default="none", choices=("none", "ma20"))
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    from factor_lab.factors import F
    z = np.load(args.panel, allow_pickle=True)
    X, Y1 = z["X"], z["Y1"]
    date_i, code_i = z["date"], z["code"]
    is_train, is_val = z["is_train"], z["is_val"]
    eval_dates = list(z["eval_dates"])
    codes = list(z["codes"])
    idx = {c: k for k, c in enumerate(codes)}
    stable_j = [F[name] for name in STABLE if name in F]
    print(f"面板 {X.shape}  股票 {len(codes)}  因子 {len(stable_j)}/{len(STABLE)}"
          f"  训练 {int(is_train.sum())} 验证 {int(is_val.sum())}")

    # 训练段方向：因子的日截面 IC 均值符号
    signs = np.zeros(len(stable_j))
    tr_days = sorted(set(date_i[is_train]))
    for k, j in enumerate(stable_j):
        ics = []
        for d in tr_days:
            m = is_train & (date_i == d)
            if m.sum() < 30:
                continue
            x, y = X[m, j], Y1[m]
            if x.std() <= 0 or y.std() <= 0:
                continue
            ics.append(np.corrcoef(x, y)[0, 1])
        signs[k] = 1.0 if np.mean(ics) >= 0 else -1.0
    print("训练方向:", dict(zip(STABLE, signs)))

    # 验证段合成分（百分位均值）
    val_mask = is_val
    vd = date_i[val_mask]
    vc = code_i[val_mask]
    comp = np.zeros(int(val_mask.sum()))
    cnt = np.zeros(int(val_mask.sum()))
    for k, j in enumerate(stable_j):
        x = X[val_mask, j].astype(np.float64) * signs[k]
        ranks = np.full(len(x), np.nan)
        for d in sorted(set(vd)):
            m = vd == d
            ranks[m] = daily_rank_cs(x[m])
        fin = np.isfinite(ranks)
        comp[fin] += ranks[fin]
        cnt[fin] += 1
    comp = np.where(cnt > 0, comp / np.maximum(cnt, 1), np.nan)

    # 价格：从 DB 取面板股票的日K
    print("加载日K ...")
    ph = ",".join("?" for _ in codes)
    with sg.db_conn() as conn:
        rows = conn.execute(
            f"SELECT code,date,open,close FROM daily_bars "
            f"WHERE code IN ({ph}) ORDER BY code,date", codes).fetchall()
    px = {}
    for c, d, o, cl in rows:
        px.setdefault(c, {})[d] = (o, cl)

    val_days = sorted({int(x) for x in vd})
    n_days = len(val_days)
    halves = {"H1": val_days[:n_days // 2], "H2": val_days[n_days // 2:]}
    pos_of = {d: i for i, d in enumerate(val_days)}
    # 指数趋势闸门（早盘用 T-1 指数收盘 vs MA20）
    mkt_ok = None
    if args.mkt == "ma20":
        irows = sg.get_daily("sh000001")
        icl = {r["date"]: r["close"] for r in irows if r.get("close")}
        vals, last = [], None
        for di in val_days:
            v = icl.get(eval_dates[di])
            if v:
                last = v
            vals.append(last)
        mkt_ok = {}
        for i, di in enumerate(val_days):
            if i < 21:
                continue
            prev = vals[i - 1]
            seg = [v for v in vals[:i] if v][-20:]
            if prev and len(seg) >= 20:
                mkt_ok[di] = prev > sum(seg) / len(seg)
    by_pos = {}
    for i in range(len(vd)):
        by_pos.setdefault(pos_of[int(vd[i])], {})[
            codes[int(vc[i])]] = comp[i]

    def sim(days):
        buy_m, sell_m = 1.001, 0.999
        cash, pos = 1e6, {}
        eq_curve, trades = [], []
        last = {}
        for t, di in enumerate(days):
            d = eval_dates[di]
            for c2, pp in pos.items():
                p = px.get(c2, {}).get(d)
                if p and p[1]:
                    last[c2] = p[1]
            # 卖出到期
            for c2 in sorted(pos):
                pp = pos[c2]
                if t - pp["t_in"] >= args.hold:
                    nxt = px.get(c2, {}).get(d)
                    if not nxt or not nxt[0]:
                        continue
                    net = nxt[0] * sell_m
                    cash += pp["shares"] * net
                    trades.append({"ret": net / pp["buy_net"] - 1.0,
                                   "pnl": pp["shares"] * (net - pp["buy_net"]),
                                   "hold": t - pp["t_in"]})
                    del pos[c2]
            # 建仓（用 T-1 分数 → T 日开盘；趋势闸门关闭时只平不开）
            if t >= 1 and len(pos) < args.top \
                    and (mkt_ok is None or mkt_ok.get(di, False)):
                sc = {c2: v for c2, v in by_pos.get(t - 1, {}).items()
                      if np.isfinite(v)}
                for c2 in sorted(sc, key=lambda x: -sc[x]):
                    if len(pos) >= args.top or c2 in pos:
                        break
                    nxt = px.get(c2, {}).get(d)
                    if not nxt or not nxt[0]:
                        continue
                    eq0 = cash + sum(pp["shares"] * last.get(c3, pp["entry"])
                                     for c3, pp in pos.items())
                    buy_net = nxt[0] * buy_m
                    shares = int(eq0 / args.top / buy_net / 100.0) * 100
                    if shares <= 0 or shares * buy_net > cash:
                        continue
                    cash -= shares * buy_net
                    pos[c2] = {"t_in": t, "shares": shares,
                               "buy_net": buy_net, "entry": nxt[0]}
            eqv = cash + sum(pp["shares"] * last.get(c3, pp["entry"])
                             for c3, pp in pos.items())
            eq_curve.append(eqv)
        return sg._v4_metrics(eq_curve, [eval_dates[d] for d in days],
                              trades)

    for name, days in halves.items():
        m = sim(days)
        print(f"[验证{name}] {eval_dates[days[0]]}~{eval_dates[days[-1]]}  "
              f"ann={m['ann']*100:+.1f}% mdd={m['mdd']*100:+.1f}% "
              f"cal={m['calmar']} sharpe={m['sharpe']} "
              f"win={(m['winrate'] or 0)*100:.0f}% tr={m['trades']}")
    m = sim(val_days)
    print(f"[验证全段] {eval_dates[val_days[0]]}~{eval_dates[val_days[-1]]}  "
          f"ann={m['ann']*100:+.1f}% mdd={m['mdd']*100:+.1f}% "
          f"cal={m['calmar']} sharpe={m['sharpe']} "
          f"win={(m['winrate'] or 0)*100:.0f}% tr={m['trades']}")

    out = {"ts": __import__("time").strftime("%Y-%m-%d %H:%M"),
           "stable_factors": STABLE, "signs": signs.tolist(),
           "top": args.top, "hold": args.hold,
           "metrics_full": m,
           "range": [eval_dates[val_days[0]], eval_dates[val_days[-1]]]}
    suffix = f"_{args.tag}" if args.tag else ""
    path = os.path.join(ROOT,
                        "research", f"v5_factor_cs{suffix}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("写入", path)


if __name__ == "__main__":
    main()
