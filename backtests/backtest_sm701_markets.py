#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多市场 sm701 信号回测（v7.0.1）。

对 hk/us/fut 三市场（sm701 生产模型覆盖的市场）：
  - 每市场按副本库成交量取流动性 Top N 标的（默认 30）
  - 从主库读同源日K（与训练数据一致），用 sm701 概率滞回信号
    （≥0.55 BUY / <0.50 SELL）跑 _bt_events（test 档风控参数）
  - 汇总每市场：交易数/胜率/年化中位/回撤中位/买入持有中位
产物 research/sm701_markets.json + .md，供网页面板展示。

用法：python backtests/backtest_sm701_markets.py [--top 30] [--workers 8]
"""
import argparse
import csv
import gzip
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import stock_gui as sg  # noqa: E402


def market_all_codes(market, min_bars=300):
    """全库代码：主库 daily_bars 该前缀全部代码（HAVING>=min_bars 根）。"""
    prefix = {"hk": "hk", "us": "us", "futures": "fu"}[market]
    with sg.db_conn() as conn:
        rows = conn.execute(
            "SELECT code FROM (SELECT code, COUNT(*) c FROM daily_bars "
            "WHERE code LIKE ? GROUP BY code) WHERE c >= ? ORDER BY code",
            (prefix + "%", min_bars)).fetchall()
    return [r[0] for r in rows if r[0]]


def load_probs_full(pred_path):
    """一次性流式读入全部历史样本外概率 {code: {date: prob}}（全库口径）。

    单进程单遍扫描，避免每码重复解压 28MB+ gz；覆盖窗口以文件末日为界
    （末日后无分数，不实时复算——全库代价太高，口径备注于报告）。"""
    out = {}
    with gzip.open(pred_path, "rt", encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        head = next(rd, [])
        ci, di = head.index("code"), head.index("date")
        pi = next(head.index(c) for c in
                  ("prob", "prob_lgbm", "prob_fuse") if c in head)
        need = max(ci, di, pi)
        for row in rd:
            if len(row) <= need:
                continue
            try:
                out.setdefault(row[ci], {})[row[di]] = float(row[pi])
            except ValueError:
                continue
    return out


def market_top_codes(market, top):
    """市场池按近 30 根平均成交额取 Top N（SQL 直出，避免逐只拉取）。"""
    prefix = {"hk": "hk", "us": "us", "futures": "fu"}[market]
    with sg.db_conn() as conn:
        rows = conn.execute(
            "SELECT code, SUM(close*vol)/MAX(COUNT(*),1) AS amt "
            "FROM (SELECT code,date,close,vol FROM daily_bars "
            "      WHERE code LIKE ? ORDER BY date DESC LIMIT 999999) "
            "WHERE date >= date('now','-45 days') "
            "GROUP BY code "
            "HAVING COUNT(*) >= 20 "
            "ORDER BY amt DESC LIMIT ?",
            (prefix + "%", top)).fetchall()
    return [r[0] for r in rows if r[0]]


def backtest_one(full, keep_curve=False):
    try:
        rows = sg.get_daily(full)[-1000:]
        if len(rows) < 300:
            return None
        sigs = sg._sig_sm701(rows, full)
        rp = dict(sg._SM701_EXT_RISK)
        bt = sg._bt_events(rows, sigs, rp) if sigs else None
        bh = (rows[-1]["close"] / rows[0]["close"] - 1) if rows else 0
        out = {"code": full, "bh": bh}
        if not bt:
            out["trades"] = 0
            if keep_curve and rows:
                out["curve"] = [1.0] * len(rows)
                out["curve_dates"] = [r["date"] for r in rows]
            return out
        out.update({"trades": bt.get("trades", 0),
                    "winrate": bt.get("winrate"), "ann": bt.get("ann"),
                    "mdd": bt.get("mdd"), "total": bt.get("total")})
        if keep_curve:
            curve = bt.get("curve") or []
            dates = [r["date"] for r in rows]
            if curve and len(curve) == len(dates):
                out["curve"] = [round(float(v), 6) for v in curve]
                out["curve_dates"] = dates
        return out
    except Exception as e:
        return {"code": full, "error": str(e)[:80]}


def market_curve(detail):
    """市场等权平均净值曲线：按日期对齐全部含曲线标的，逐日等权平均。"""
    import statistics as st
    by_date = {}
    for r in detail:
        cu = r.get("curve")
        if not cu:
            continue
        for d, v in zip(r.get("curve_dates") or [], cu):
            by_date.setdefault(d, []).append(v)
    if not by_date:
        return None
    dates = sorted(by_date)
    vals = [st.mean(by_date[d]) for d in dates]
    # 起点 1.0 对齐
    if vals and vals[0]:
        base = vals[0]
        vals = [v / base for v in vals]
    return {"dates": dates, "curve": [round(v, 6) for v in vals]}


def backtest_one_full(full, probs=None):
    """全库口径单标的：历史落盘概率直出滞回信号（不实时复算末日之后）。"""
    try:
        rows = sg.get_daily(full)[-1000:]
        if len(rows) < 300:
            return None
        sigs = sg._sig_sm701_from_probs(rows, probs or {})
        bt = sg._bt_events(rows, sigs, sg._SM701_EXT_RISK) if sigs else None
        bh = (rows[-1]["close"] / rows[0]["close"] - 1) if rows else 0
        out = {"code": full, "bh": bh}
        if not bt:
            out["trades"] = 0
            return out
        out.update({"trades": bt.get("trades", 0),
                    "winrate": bt.get("winrate"), "ann": bt.get("ann"),
                    "mdd": bt.get("mdd"), "total": bt.get("total")})
        return out
    except Exception as e:
        return {"code": full, "error": str(e)[:80]}


def summarize(results):
    import statistics as st
    ok = [r for r in results if r.get("trades")]
    def med(key):
        vals = [r[key] for r in ok if r.get(key) is not None]
        return st.median(vals) if vals else None
    return {
        "n": len(results),
        "n_traded": len(ok),
        "trades_median": med("trades"),
        "winrate_median": med("winrate"),
        "ann_median": med("ann"),
        "mdd_median": med("mdd"),
        "total_median": med("total"),
        "bh_median": (st.median([r["bh"] for r in results if "bh" in r])
                      if results else None),
    }


def fmt_pct(v, digits=1):
    """None 安全百分比格式化（回测报告用；比率×100）。"""
    return "" if v is None else f"{v * 100:.{digits}f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30,
                    help="每市场流动性Top N；0=全库（≥300根日K的全部代码）")
    ap.add_argument("--market", default="all",
                    help="hk/us/futures 或 all（默认三种全跑）")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    markets = ([m.strip() for m in args.market.split(",") if m.strip()]
               if args.market != "all" else ["hk", "us", "futures"])
    full_db = bool(args.top == 0)
    out = {"version": sg.APP_VERSION, "generated": sg.time.strftime(
        "%Y-%m-%d %H:%M"), "markets": {}, "full_db": full_db}
    md = ["# 多市场 sm701 外接算法信号回测（v{}，{}）".format(
        sg.APP_VERSION, out["generated"]), "",
          "口径：sm701 概率滞回信号（≥0.55买 / <0.50卖），外接风控（EXT_RISK），",
          "近 1000 根日K，样本外历史概率 + 实时复算；中位数统计。"]
    if full_db:
        md.append("全库口径：--top 0，全部代码（主库 ≥300 根日K）；"
                  "概率只用落盘训练/验证段（valid_predictions*，不实时复算"
                  "文件末日之后），避免全库逐码重算。")
    md.append("")
    for market in markets:
        if not sg.sm701_has_model(market):
            md.append(f"## {sg._MARKET_LABELS[market]}：无生产模型，跳过")
            out["markets"][market] = {"skipped": True}
            continue
        paths = sg.sm701_paths(market=market)
        if full_db:
            codes = market_all_codes(market)
            print(f"[{market}] 全库 {len(codes)} 个代码（先一次性载入历史概率）",
                  flush=True)
            probs_all = load_probs_full(paths["pred"])
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                results = list(ex.map(
                    lambda c: backtest_one_full(
                        c, probs_all.get(sg._sm701_code(c))),
                    codes))
            n_hist = len(probs_all)
            del probs_all
            print(f"[{market}] 历史概率覆盖 {n_hist} 个股票池代码"
                  "（无分数不交易）", flush=True)
        else:
            codes = market_top_codes(market, args.top)
            print(f"[{market}] top{len(codes)} 回测中...", flush=True)
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                results = list(ex.map(lambda c: backtest_one(c, keep_curve=True),
                                      codes))
        summ = summarize([r for r in results if r and "error" not in r])
        entry = {"summary": summ, "codes": len(codes),
                 "detail": results}
        if not full_db:
            entry["curve"] = market_curve(results)
        out["markets"][market] = entry
        md.append(f"## {sg._MARKET_LABELS[market]}"
                  + (f"（全库 {len(codes)}）" if full_db
                     else f"（top {len(codes)}）"))
        md.append(f"- 有效 {summ['n_traded']}/{summ['n']}；"
                  f"交易数中位 {summ['trades_median']}；"
                  f"胜率中位 {fmt_pct(summ['winrate_median'])}")
        md.append(f"- 年化中位 {fmt_pct(summ['ann_median'])}；回撤中位 "
                  f"{fmt_pct(summ['mdd_median'])}；区间收益中位 "
                  f"{fmt_pct(summ['total_median'])}"
                  f"；买入持有中位 {fmt_pct(summ['bh_median'])}")
        md.append("")
        print(f"[{market}] {summ}", flush=True)
    name = "sm701_markets_full" if full_db else "sm701_markets"
    os.makedirs(os.path.join(ROOT, "research"), exist_ok=True)
    with open(os.path.join(ROOT, "research", name + ".json"),
              "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(os.path.join(ROOT, "research", name + ".md"),
              "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print(f"已写出 research/{name}.json/.md")


if __name__ == "__main__":
    main()
