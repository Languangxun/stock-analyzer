#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_picks.py - 每日荐股信号买卖回测（防前视）

逻辑与 GUI「每日荐股」同构（daily_pick_score），按时间轴滚动：
  1. 评估日 T：只用「截止 T 收盘」的数据给每只股票打分（daily_pick_score
     内部指标全部因果；行业上下文也只累计到 T）；
  2. T 日收盘后入围：非 ST/ETF/北交、close≥2、MA20/60 空头闸门不触发、
     score ≥ buy_th，按 score 取 TopN；
  3. 成交：T+1 开盘买入（涨停开盘跳过）；卖出信号（score < sell_th、
     MA 空头闸门、或持有到期）→ T+1 开盘卖出（跌停开盘顺延）；
  4. 成本：滑点 0.1%/边；期末按最后盯市价强平。

用法：
  python backtest_picks.py --days 250 --step 5 --top 10
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
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import stock_gui as sg


def load_universe(min_bars=120):
    """股票池：只剔除 ETF/北交所/历史不足，不按"当前名称/末日价格"预筛。

    注意：ST 历史状态与行业历史分类在库中不可得，这里不再用当前名称过滤
    （用当前名称剔除 ST 会把"当年非 ST"的股票也提前剔除，属未来信息），
    行业上下文使用当前分类的近似口径。价格≥2 的仙股过滤改在评估日当天判断。"""
    with sg.db_conn() as conn:
        names = {r[0]: (r[1] or "") for r in
                 conn.execute("SELECT code, name FROM stocks").fetchall()}
        ind_of = {r[0]: (r[1] or "") for r in
                  conn.execute("SELECT code, industry FROM stocks")}
        rows = conn.execute(
            "SELECT code,date,open,high,low,close,vol FROM ("
            "  SELECT code,date,open,high,low,close,vol,"
            "         ROW_NUMBER() OVER (PARTITION BY code "
            "                            ORDER BY date DESC) rn"
            "  FROM daily_bars) WHERE rn<=600 ORDER BY code, date"
        ).fetchall()
    by = {}
    for c, d, o, h, l, cl, v in rows:
        by.setdefault(c, []).append(
            {"date": d, "open": o, "high": h, "low": l,
             "close": cl, "vol": v or 0.0})
    out = []
    for c, r in by.items():
        if len(r) < min_bars or sg._is_etf(c) or c.startswith("bj"):
            continue
        out.append((c, r, names.get(c, c), ind_of.get(c, "")))
    return out


def industry_ctx_by_date(ind_of):
    """每个交易日各行业近5日等权收益 + 中位数/前20%领先集合（只用截止当日）。"""
    _, ind = sg._v4_mkt_ind_ctx(ind_of)
    dates = sorted(ind.keys())
    out = {}
    for i, d in enumerate(dates):
        r5 = {}
        for dd in dates[max(0, i - 4):i + 1]:
            for k, v in ind.get(dd, {}).items():
                r5[k] = r5.get(k, 0.0) + v
        if len(r5) < 3:
            continue
        vals = sorted(r5.values())
        med = vals[len(vals) // 2]
        lead = set(sorted(r5, key=lambda k: -r5[k])[:max(1, len(r5) // 5)])
        out[d] = (r5, med, lead)
    return out


def _worker(args):
    code, rows, name, evals, ctx_by_date = args
    didx = {r["date"]: k for k, r in enumerate(rows)}
    recs = []
    for d in evals:
        k = didx.get(d)
        if k is None or k < 60:
            continue
        # 仙股过滤用评估日当天收盘价（时点可见），不用回测末日价格
        if not rows[k]["close"] or rows[k]["close"] < 2:
            continue
        sub = rows[max(0, k - 399):k + 1]
        try:
            r = sg.daily_pick_score(sub, ind_ctx=ctx_by_date.get(d))
        except Exception:
            continue
        if not r:
            continue
        score, reasons, band, gates = r
        nxt = rows[k + 1] if k + 1 < len(rows) else None
        recs.append({
            "date": d, "score": score, "ma": gates.get("ma_trend", 0),
            "close": rows[k]["close"], "reason": " ".join(reasons)[:60],
            "open_next": nxt["open"] if nxt else None,
            "next_date": nxt["date"] if nxt else None,
        })
    return {"code": code, "name": name, "recs": recs}


def simulate(by_date, evals, *, top=10, max_pos=10, buy_th=2.0,
             sell_th=0.0, max_hold=20, slip=0.001, entry_ok=None):
    """组合模拟：T 日决策、T+1 开盘成交。entry_ok: {date: bool} 入场闸门。"""
    buy_m, sell_m = 1 + slip, 1 - slip
    cash, pos = 1e6, {}
    eq_curve, eq_dates, trades = [], [], []
    for d in evals:
        recs = by_date.get(d, [])
        rec_map = {x["code"]: x for x in recs}
        for code, pp in pos.items():
            x = rec_map.get(code)
            if x and x.get("close"):
                pp["mark"] = x["close"]
        eq_curve.append(cash + sum(pp["shares"] * pp["mark"]
                                   for pp in pos.values()))
        eq_dates.append(d)
        for code in list(pos):
            pp = pos[code]
            x = rec_map.get(code)
            pp["held"] += 1
            if x is None:
                continue
            if x["score"] < sell_th or x["ma"] <= -2 \
                    or pp["held"] >= max_hold:
                p = x.get("open_next")
                if not p:
                    continue
                lim = sg._v4_limit_pct(code)
                if x["close"] and p / x["close"] - 1 <= -(lim - 0.005):
                    continue
                pos.pop(code)
                net = p * sell_m
                cash += pp["shares"] * net
                trades.append({"ret": net / pp["buy_net"] - 1.0,
                               "pnl": pp["shares"] * (net - pp["buy_net"]),
                               "hold": pp["held"]})
        slots = max_pos - len(pos)
        if slots > 0 and (entry_ok is None or entry_ok.get(d, True)):
            cands = [x for x in recs
                     if x["code"] not in pos and x["score"] >= buy_th
                     and x["ma"] > -2 and x.get("open_next")]
            cands.sort(key=lambda x: -x["score"])
            for x in cands[:top]:
                if slots <= 0:
                    break
                code = x["code"]
                p = x["open_next"]
                lim = sg._v4_limit_pct(code)
                if x["close"] and p / x["close"] - 1 >= lim - 0.005:
                    continue
                net = p * buy_m
                eq0 = cash + sum(pp["shares"] * pp["mark"]
                                 for pp in pos.values())
                shares = int(eq0 / max_pos / net / 100.0) * 100
                if shares <= 0 or shares * net > cash:
                    continue
                cash -= shares * net
                pos[code] = {"shares": shares, "buy_net": net, "entry": p,
                             "held": 0, "mark": p, "date": x["next_date"]}
                slots -= 1
    for code in list(pos):
        pp = pos.pop(code)
        net = pp["mark"] * sell_m
        cash += pp["shares"] * net
        trades.append({"ret": net / pp["buy_net"] - 1.0,
                       "pnl": pp["shares"] * (net - pp["buy_net"]),
                       "hold": pp["held"]})
    eq_curve.append(cash)
    return sg._v4_metrics(eq_curve, eq_dates, trades), eq_curve, eq_dates, trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--max-pos", type=int, default=10)
    ap.add_argument("--sell-th", type=float, default=0.0)
    ap.add_argument("--max-hold", type=int, default=20,
                    help="最长持有（评估周期数，每期 step 个交易日）")
    ap.add_argument("--buy-th", type=float, default=None)
    ap.add_argument("--workers", type=int,
                    default=max(1, min(8, os.cpu_count() or 4)))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--scores-out", default="",
                    help="把逐日打分缓存为 pickle（供后续秒级调参）")
    ap.add_argument("--scores-in", default="",
                    help="复用已缓存的逐日打分，跳过滚动打分")
    args = ap.parse_args()
    buy_th = args.buy_th if args.buy_th is not None \
        else sg.CFG.risk_params()["buy_th"]
    t0 = time.time()

    print("加载股票池 ...")
    uni = load_universe()
    if args.limit:
        uni = uni[:args.limit]
    print(f"  {len(uni)} 只（剔除 ETF/北交/历史不足；仙股按评估日当天价格过滤）")
    if not uni:
        raise SystemExit("股票池为空")
    all_dates = sorted({r["date"] for _, rows, _, _ in uni for r in rows})
    evals = all_dates[-args.days:][::args.step]
    print(f"评估日 {evals[0]} ~ {evals[-1]}，共 {len(evals)} 个（步长 {args.step}）")

    if args.scores_in:
        import pickle
        print(f"复用打分缓存 {args.scores_in} ...")
        with open(args.scores_in, "rb") as f:
            by_date = pickle.load(f)
        print(f"  评估日 {len(by_date)} 个")
    else:
        ind_of = {c: ind for c, _, _, ind in uni}
        print("构建行业上下文（只用截止评估日数据）...")
        ictx = industry_ctx_by_date(ind_of)
        tasks = []
        for code, rows, name, ind in uni:
            cbd = {}
            for d in evals:
                ic = ictx.get(d)
                if ic is None:
                    continue
                r5, med, lead = ic
                cbd[d] = {"r5": r5.get(ind), "med": med, "lead": ind in lead}
            tasks.append((code, rows, name, evals, cbd))

        print(f"开始滚动打分（{len(tasks)} 只 × {len(evals)} 日）...")
        by_date = {}
        done = 0
        with ProcessPoolExecutor(max_workers=args.workers) as exe:
            futs = [exe.submit(_worker, t) for t in tasks]
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except Exception:
                    r = None
                done += 1
                if done % 1000 == 0:
                    print(f"  {done}/{len(tasks)}")
                if not r:
                    continue
                if not r["recs"]:
                    continue
                for rec in r["recs"]:
                    by_date.setdefault(rec["date"], []).append(
                        {"code": r["code"], "name": r["name"], **rec})
        if args.scores_out:
            import pickle
            with open(args.scores_out, "wb") as f:
                pickle.dump(by_date, f, protocol=4)
            print(f"  打分缓存写入 {args.scores_out}")

    # ---- 组合模拟 ----
    m, eq_curve, eq_dates, trades = simulate(
        by_date, evals, top=args.top, max_pos=args.max_pos,
        buy_th=buy_th, sell_th=args.sell_th, max_hold=args.max_hold)
    m.update({"buy_th": buy_th, "sell_th": args.sell_th,
              "max_hold": args.max_hold, "top": args.top,
              "max_pos": args.max_pos, "n_stocks": len(uni),
              "n_eval_days": len(evals)})
    print("\n=== 荐股信号买卖回测（T 日打分 → T+1 开盘成交）===")
    print(f"窗口 {evals[0]} ~ {evals[-1]} | Top{args.top} 持仓≤{args.max_pos} "
          f"buy_th={buy_th} sell_th={args.sell_th} max_hold={args.max_hold}")
    print(f"年化 {m['ann']*100:+.1f}% | 回撤 {m['mdd']*100:+.1f}% | "
          f"Calmar {(m['calmar'] or 0):+.2f} | "
          f"Sharpe {(m['sharpe'] or 0):+.2f} | "
          f"胜率 {(m['winrate'] or 0)*100:.1f}% | PF {(m['pf'] or 0):.2f} | "
          f"交易 {m['trades']} | 均持仓 {(m['avg_hold'] or 0):.1f}")

    suffix = f"_{args.tag}" if args.tag else ""
    path = os.path.join(ROOT,
                        "research", f"picks_backtest{suffix}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%d %H:%M"),
                   "config": {"days": args.days, "step": args.step,
                              "top": args.top, "max_pos": args.max_pos,
                              "buy_th": buy_th, "sell_th": args.sell_th,
                              "max_hold": args.max_hold},
                   "metrics": m}, f, ensure_ascii=False, indent=1)
    print(f"写入 {path}，耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
