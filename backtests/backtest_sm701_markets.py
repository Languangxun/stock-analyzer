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
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import stock_gui as sg  # noqa: E402


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


def backtest_one(full):
    try:
        rows = sg.get_daily(full)[-1000:]
        if len(rows) < 300:
            return None
        sigs = sg._sig_sm701(rows, full)
        rp = dict(sg.CFG.RISK_PARAMS["test"])
        bt = sg._bt_events(rows, sigs, rp) if sigs else None
        bh = (rows[-1]["close"] / rows[0]["close"] - 1) if rows else 0
        if not bt:
            return {"code": full, "trades": 0, "bh": bh}
        return {"code": full, "trades": bt.get("trades", 0),
                "winrate": bt.get("winrate"), "ann": bt.get("ann"),
                "mdd": bt.get("mdd"), "total": bt.get("total"), "bh": bh}
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    out = {"version": sg.APP_VERSION, "generated": sg.time.strftime(
        "%Y-%m-%d %H:%M"), "markets": {}}
    md = ["# 多市场 sm701 信号回测（v{}，{}）".format(
        sg.APP_VERSION, out["generated"]), "",
          "口径：sm701 概率滞回信号（≥0.55买 / <0.50卖），test 档风控，",
          "近 1000 根日K，样本外历史概率 + 实时复算；中位数统计。", ""]
    for market in ("hk", "us", "futures"):
        if not sg.sm701_has_model(market):
            md.append(f"## {sg._MARKET_LABELS[market]}：无生产模型，跳过")
            out["markets"][market] = {"skipped": True}
            continue
        codes = market_top_codes(market, args.top)
        print(f"[{market}] top{len(codes)} 回测中...", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(backtest_one, codes))
        summ = summarize([r for r in results if r and "error" not in r])
        out["markets"][market] = {"summary": summ, "detail": results}
        md.append(f"## {sg._MARKET_LABELS[market]}（top {len(codes)}）")
        md.append(f"- 有效 {summ['n_traded']}/{summ['n']}；"
                  f"交易数中位 {summ['trades_median']}；"
                  f"胜率中位 {summ['winrate_median']}")
        md.append(f"- 年化中位 {summ['ann_median']}；回撤中位 "
                  f"{summ['mdd_median']}；区间收益中位 {summ['total_median']}"
                  f"；买入持有中位 {summ['bh_median']}")
        md.append("")
        print(f"[{market}] {summ}", flush=True)
    os.makedirs(os.path.join(ROOT, "research"), exist_ok=True)
    with open(os.path.join(ROOT, "research", "sm701_markets.json"),
              "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(os.path.join(ROOT, "research", "sm701_markets.md"),
              "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print("已写出 research/sm701_markets.json/.md")


if __name__ == "__main__":
    main()
