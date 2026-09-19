#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_strategy_ablation.py - 全 A 股多算法策略自动消融选股（v6.1）

对每只股票：
  1. 取近 1000 交易日；
  2. 生成 8 种基础算法信号（MACD/KDJ/RSI/布林带/MA趋势/L1形态/
     筹码峰/板块轮动）+ 多维评分 × 3 档风险；消融全程本地计算，AI 不参与；
  3. 训练集（前 75%）选型，验证集（后 25%）只报告；
  4. 多指标结合选优（v6.1）：训练集 Calmar/盈亏比/胜率/年化 横截面 rank 加权，
     稳健偏 Calmar+PF，均衡/激进偏年化+Calmar；激进=均衡选型+组合层弱市覆盖；
  5. 输出每只股票的结果 + 全市场聚合统计。

v2026-09-19（板块轮动序列经 initializer 注入子进程，避免重复构建）
"""
# --- 目录引导：backtests/ 下运行也能导入 stock_gui/factor_lab，产物写项目根 ---
import os as _os_boot
import sys as _sys_boot

ROOT = _os_boot.path.dirname(_os_boot.path.dirname(
    _os_boot.path.abspath(__file__)))
if ROOT not in _sys_boot.path:
    _sys_boot.path.insert(0, ROOT)
# --- 目录引导结束 ---
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime

import numpy as np

# 从 GUI/CLI 唯一算法源导入回测口径
import stock_gui as sg
from stock_gui import (
    db_conn, _is_etf,
    _sig_macd, _sig_kdj, _sig_rsi, _sig_boll, _sig_ma_trend, _sig_l1_pattern,
    _sig_chip_peak, _sig_sector_rot,
    _composite_signals, _composite_precompute,
    _bt_events, _precompute_atr, _bull_bear_score, _regime_map,
    _ablation_pf, pick_ablation_multi,
    ALGO_LABEL, CFG, get_daily
)

OUTPUT_DIR = os.path.join(ROOT, "research")
PER_STOCK_FILE = os.path.join(OUTPUT_DIR, "strategy_ablation_per_stock.json")
SUMMARY_FILE = os.path.join(OUTPUT_DIR, "strategy_ablation_summary.json")

# 预加载指数 regime，全市场共享
_IDX_REGIME_CACHE = None


def _init_sector(cal, series, med5):
    """子进程初始化：把主进程构建的板块动量序列注入 stock_gui 缓存，
    避免每个 worker 重复加载全A面板。"""
    sg._SECTOR_MOM_CACHE["data"] = (cal, series, med5)
    sg._SECTOR_MOM_CACHE["ts"] = time.time()


def _load_index_regime():
    """加载上证指数并计算牛熊 regime（收盘 >= MA120 为牛）。"""
    global _IDX_REGIME_CACHE
    if _IDX_REGIME_CACHE is not None:
        return _IDX_REGIME_CACHE
    try:
        idx_rows = get_daily("sh000001")
    except Exception as e:
        print(f"上证指数加载失败: {e}")
        idx_rows = []
    # _regime_map 返回 {date: bool}
    _IDX_REGIME_CACHE = _regime_map(idx_rows, len(idx_rows)) if idx_rows else {}
    return _IDX_REGIME_CACHE


def load_stocks(min_bars=200, with_etf=True):
    """加载缓存中**所有对象**（个股 + ETF）逐个消融，只按最低 bar 数过滤。

    v6.1.2：不再排除 ETF；min_bars 默认 200（低于此值无法做训练/验证切分）。
    返回 [(code, rows, industry)]，industry 对 ETF 为 "ETF"（stocks 表内标注）。"""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT code, date, open, high, low, close, vol "
            "FROM daily_bars ORDER BY code, date"
        ).fetchall()
        ind_of = {c: (i or "") for c, i in
                  conn.execute("SELECT code, industry FROM stocks")}
    by = {}
    for c, d, o, h, l, cl, v in rows:
        by.setdefault(c, []).append(
            {"date": d, "open": o, "high": h, "low": l, "close": cl, "vol": v or 0.0}
        )
    out = []
    for c, r in by.items():
        if len(r) < min_bars:
            continue
        if not with_etf and _is_etf(c):
            continue
        out.append((c, r, ind_of.get(c, "ETF" if _is_etf(c) else "")))
    return out


def _trim_rows(rows, max_bars=1000):
    """截断到最近 max_bars 根，并过滤无效 close。"""
    rows = [r for r in rows if r.get("close") and r["close"] > 0]
    if len(rows) > max_bars:
        rows = rows[-max_bars:]
    return rows


def _pick_candidates(cands, key):
    """v6.1 多指标结合选优（训练集 Calmar/PF/胜率/年化 rank 加权）：
    稳健偏 Calmar+PF；均衡/激进偏年化+Calmar；激进=均衡选型 + 组合层
    弱市覆盖（本脚本不生成，见 backtest_strategy_portfolio.py）。"""
    return pick_ablation_multi(cands, "稳健" if key == "稳健" else "激进")


def run_ablation_for_stock(args):
    """对单只股票跑完整消融。多进程 worker。"""
    code, rows, industry = args
    regime = _load_index_regime()      # 进程内缓存，避免随任务反复 pickle
    rows = _trim_rows(rows)
    n = len(rows)
    if n < 200:
        return None
    val_n = max(200, n // 4)
    split = n - val_n

    # 预计算 ATR(14) 与多维评分指标，所有候选/档位复用（关键提速）
    atrs = _precompute_atr(rows, 0, n)

    # 各基础算法信号发生器（v6.1 增：筹码峰 / 板块轮动；AI 不参与）
    gens = {
        "macd": lambda: _sig_macd(rows),
        "kdj": lambda: _sig_kdj(rows),
        "rsi": lambda: _sig_rsi(rows),
        "boll": lambda: _sig_boll(rows),
        "ma_trend": lambda: _sig_ma_trend(rows),
        "l1_pattern": lambda: _sig_l1_pattern(rows),
        "chip_peak": lambda: _sig_chip_peak(rows),
        "sector_rot": lambda: _sig_sector_rot(rows, industry=industry),
    }

    cands = []
    for algo, gen in gens.items():
        try:
            sigs = gen()
        except Exception:
            continue
        if not sigs:
            continue
        for mode, rp in CFG.RISK_PARAMS.items():
            tr_tr, va_tr = [], []
            tr = _bt_events(rows, sigs, rp, 0, split, atrs=atrs,
                            trade_out=tr_tr)
            va = _bt_events(rows, sigs, rp, split, n, atrs=atrs,
                            trade_out=va_tr)
            if not tr:
                continue
            bull, bear = _bull_bear_score(rows, tr["curve"], tr["i0"], regime)
            train = {k: v for k, v in tr.items() if k != "curve"}
            val = ({k: v for k, v in va.items() if k != "curve"} if va else None)
            if tr_tr:
                train["pf"] = _ablation_pf(tr_tr)
            if va_tr and val is not None:
                val["pf"] = _ablation_pf(va_tr)
            cands.append({
                "algo": algo,
                "mode": mode,
                "params": dict(rp),
                "label": f"{ALGO_LABEL.get(algo, algo)}·{mode}",
                "train": train,
                "val": val,
                "bull": bull,
                "bear": bear,
            })

    # 多维评分 × 3 档风险（指标只算一次）
    comp_pre = _composite_precompute(rows)
    for mode, rp in CFG.RISK_PARAMS.items():
        try:
            sigs = _composite_signals(rows, rp, idx_chg_by_date=None,
                                      pre=comp_pre)
        except Exception:
            continue
        if not sigs:
            continue
        tr_tr, va_tr = [], []
        tr = _bt_events(rows, sigs, rp, 0, split, atrs=atrs, trade_out=tr_tr)
        va = _bt_events(rows, sigs, rp, split, n, atrs=atrs, trade_out=va_tr)
        if not tr:
            continue
        bull, bear = _bull_bear_score(rows, tr["curve"], tr["i0"], regime)
        train = {k: v for k, v in tr.items() if k != "curve"}
        val = ({k: v for k, v in va.items() if k != "curve"} if va else None)
        if tr_tr:
            train["pf"] = _ablation_pf(tr_tr)
        if va_tr and val is not None:
            val["pf"] = _ablation_pf(va_tr)
        cands.append({
            "algo": "composite",
            "mode": mode,
            "params": dict(rp),
            "label": f"多维评分·{mode}",
            "train": train,
            "val": val,
            "bull": bull,
            "bear": bear,
        })

    if not cands:
        return None

    _bal = _pick_candidates(cands, "均衡")
    out = {
        "code": code,
        "bars": n,
        "is_etf": bool(_is_etf(code)),
        "train_n": split,
        "val_n": n - split,
        "mode_candidates": {
            "稳健": _pick_candidates(cands, "稳健"),
            "均衡": _bal,
            "激进": _bal,      # 均衡选型 + 组合层弱市覆盖 = 激进（冠军）
        },
        "all_candidates": cands,
    }
    return out


def _median_or_none(vals):
    vals = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return float(np.median(vals)) if vals else None


def _mean_or_none(vals):
    vals = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return float(np.mean(vals)) if vals else None


def _pct_or_none(count, total):
    return round(count / total * 100, 2) if total else None


def build_summary(per_stock, coverage=None):
    """由每个对象（个股/ETF）结果构建聚合统计。"""
    total = len(per_stock)
    if total == 0:
        return {}

    modes = ["稳健", "均衡"]        # 激进=均衡+组合层弱市覆盖，不单独统计
    n_etf = sum(1 for s in per_stock if s.get("is_etf"))
    summary = {
        "total_stocks": total,
        "total_objects": total,
        "etf_count": n_etf,
        "stock_count": total - n_etf,
        "timestamp": datetime.now().isoformat(),
        "modes": {},
    }
    if coverage:
        summary["coverage"] = coverage

    for mode in modes:
        selections = [s["mode_candidates"][mode] for s in per_stock
                      if s and s["mode_candidates"].get(mode)]
        n_sel = len(selections)
        if n_sel == 0:
            summary["modes"][mode] = {"count": 0}
            continue

        # 算法分布
        algo_counts = {}
        for c in selections:
            algo_counts[c["algo"]] = algo_counts.get(c["algo"], 0) + 1

        train_ann = [c["train"]["ann"] for c in selections]
        train_mdd = [c["train"]["mdd"] for c in selections]
        train_win = [c["train"]["winrate"] for c in selections]
        train_trades = [c["train"]["trades"] for c in selections]
        val_ann = [c["val"]["ann"] for c in selections if c.get("val")]
        val_mdd = [c["val"]["mdd"] for c in selections if c.get("val")]
        val_win = [c["val"]["winrate"] for c in selections if c.get("val")]
        val_trades = [c["val"]["trades"] for c in selections if c.get("val")]
        bull = [c["bull"] for c in selections if c.get("bull") is not None]
        bear = [c["bear"] for c in selections if c.get("bear") is not None]

        summary["modes"][mode] = {
            "count": n_sel,
            "algo_distribution": {k: {"count": v, "pct": round(v / n_sel * 100, 2)}
                                  for k, v in algo_counts.items()},
            "train": {
                "ann_median": _median_or_none(train_ann),
                "ann_mean": _mean_or_none(train_ann),
                "mdd_median": _median_or_none(train_mdd),
                "winrate_median": _median_or_none(train_win),
                "trades_median": _median_or_none(train_trades),
            },
            "val": {
                "ann_median": _median_or_none(val_ann),
                "ann_mean": _mean_or_none(val_ann),
                "mdd_median": _median_or_none(val_mdd),
                "winrate_median": _median_or_none(val_win),
                "trades_median": _median_or_none(val_trades),
            },
            "regime": {
                "bull_ann_median": _median_or_none(bull),
                "bear_ann_median": _median_or_none(bear),
            },
        }
    return summary


def main(limit=None, max_workers=None, tag=""):
    t0 = time.time()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    suffix = f"_{tag}" if tag else ""
    per_stock_file = os.path.join(OUTPUT_DIR,
                                  f"strategy_ablation_per_stock{suffix}.json")
    summary_file = os.path.join(OUTPUT_DIR,
                                f"strategy_ablation_summary{suffix}.json")

    print("加载上证指数 regime...")
    regime = _load_index_regime()
    print(f"  regime 日期数: {len(regime)}")

    print("加载全缓存对象（个股 + ETF，逐个消融）...")
    stocks = load_stocks(min_bars=200, with_etf=True)
    # 覆盖率统计：缓存内全部对象 vs 本次纳入消融的对象
    with db_conn() as conn:
        all_objs = {c: n for c, n in conn.execute(
            "select code, count(*) from daily_bars group by code")}
    covered = {c for c, _, _ in stocks}
    miss = sorted((c, n) for c, n in all_objs.items() if c not in covered)
    if limit:
        stocks = stocks[:limit]
    n_etf = sum(1 for c, _, _ in stocks if _is_etf(c))
    print(f"  缓存对象总数 {len(all_objs)}，纳入消融 {len(stocks)}"
          f"（其中 ETF {n_etf}）；因 K线<200 未纳入 {len(miss)}")
    if miss:
        print(f"  未纳入清单（前20）: {miss[:20]}")

    print("构建板块轮动序列（行业5日动量，一次构建注入子进程）...")
    try:
        cal_mom, series_mom, med_mom = sg.sector_mom_series()
        print(f"  行业数: {len(series_mom)}，交易日: {len(cal_mom)}")
    except Exception as e:
        print(f"  板块轮动序列构建失败，sector_rot 将跳过: {e}")
        cal_mom, series_mom, med_mom = [], {}, None

    if max_workers is None:
        max_workers = max(1, min(os.cpu_count() or 4, 8))

    print(f"开始消融（workers={max_workers}，多指标结合：筹码峰/板块轮动），"
          f"每只股票自动产生三档策略...")
    per_stock = []
    done = 0
    skipped = 0
    skipped_codes = []          # (code, 原因)，确保「所有对象逐个消融」可核验

    args_list = [(code, rows, ind) for code, rows, ind in stocks]
    with ProcessPoolExecutor(
            max_workers=max_workers, initializer=_init_sector,
            initargs=(cal_mom, series_mom, med_mom)) as exe:
        futures = {exe.submit(run_ablation_for_stock, a): a[0] for a in args_list}
        for fut in as_completed(futures):
            code = futures[fut]
            done += 1
            try:
                res = fut.result()
            except Exception as e:
                print(f"[{done}/{len(stocks)}] {code} 异常: {e}")
                skipped += 1
                skipped_codes.append((code, f"异常: {e}"))
                continue
            if res is None:
                skipped += 1
                skipped_codes.append((code, "无有效候选(样本/信号不足)"))
                if done % 500 == 0:
                    print(f"[{done}/{len(stocks)}] {code} 无有效候选...")
                continue
            per_stock.append(res)
            if done % 500 == 0:
                print(f"[{done}/{len(stocks)}] {code} 完成，累计有效 {len(per_stock)}")

    n_etf_done = sum(1 for s in per_stock if s.get("is_etf"))
    print(f"\n消融完成: 总对象 {done}, 有效 {len(per_stock)}"
          f"（ETF {n_etf_done}）, 跳过 {skipped}, 耗时 {time.time()-t0:.0f}s")
    coverage = {
        "cache_objects": len(all_objs),
        "included": len(args_list),
        "ablated": len(per_stock),
        "skipped": len(skipped_codes),
        "skipped_detail": skipped_codes,
        "etf_cache": sum(1 for c in all_objs if _is_etf(c)),
        "etf_included": n_etf,
        "etf_ablated": n_etf_done,
        "excluded_by_bars": miss,
    }

    # 第一层输出：每只股票
    print(f"写入 {per_stock_file} ...")
    with open(per_stock_file, "w", encoding="utf-8") as f:
        json.dump(per_stock, f, ensure_ascii=False, indent=1)

    # 第二层输出：聚合统计
    print(f"构建并写入 {summary_file} ...")
    summary = build_summary(per_stock, coverage=coverage)
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 控制台摘要
    print("\n=== 覆盖率 ===")
    print(f"  缓存对象 {coverage['cache_objects']}（ETF {coverage['etf_cache']}）"
          f" → 纳入 {coverage['included']}（ETF {coverage['etf_included']}）"
          f" → 有效消融 {coverage['ablated']}（ETF {coverage['etf_ablated']}）"
          f"，跳过 {coverage['skipped']}"
          f"，K线不足排除 {len(coverage['excluded_by_bars'])}")
    print("\n=== 聚合摘要 ===")
    for mode, data in summary.get("modes", {}).items():
        print(f"\n【{mode}】选中 {data['count']} 只")
        print(f"  算法分布: {data.get('algo_distribution', {})}")
        t = data.get("train", {})
        v = data.get("val", {})
        print(f"  训练集: 年化中位={t.get('ann_median'):.1%} 回撤中位={t.get('mdd_median'):.1%} 胜率中位={t.get('winrate_median'):.1%} 交易中位={t.get('trades_median')}")
        print(f"  验证集: 年化中位={v.get('ann_median'):.1%} 回撤中位={v.get('mdd_median'):.1%} 胜率中位={v.get('winrate_median'):.1%} 交易中位={v.get('trades_median')}")

    print(f"\n全部完成，总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 只股票（测试用）")
    parser.add_argument("--workers", type=int, default=None, help="并行进程数")
    parser.add_argument("--tag", default="", help="产物后缀（默认覆盖正式文件）")
    args = parser.parse_args()
    main(limit=args.limit, max_workers=args.workers, tag=args.tag)
