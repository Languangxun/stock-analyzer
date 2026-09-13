#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_portfolio_overlay_opt.py - 每股消融组合·进入/退出覆盖层寻优

对象：research/strategy_ablation_per_stock.json 的每股选型（选型只用各自 train
段），在组合层搜索「弱市条件化退出/进场」覆盖层：
  弱市 ATR 止损缩放 scale × 弱市阈值 th × 弱市停开仓 skip。

防过拟合协议（三套选参/验证口径，任一通过才算稳健）：
  A. train→val：train 段（各股前 75%，放宽对齐）选参，val 段（样本外）验证；
  B. h1→h2：val 前半段选参、后半段验证；
  C. h2→h1：反向对照（检验 regime 依赖，不作采纳依据）。
另做参数平台检查：选中点的邻域不能是孤峰。

用法：python backtest_portfolio_overlay_opt.py [--tiers 保守 稳健 激进]
"""
import argparse
import datetime as _dt
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

import stock_gui as sg
from backtest_strategy_ablation import load_stocks, PER_STOCK_FILE
from backtest_strategy_portfolio import (
    TIERS, SLOT_CFG, _worker_slot, build_matrices, market_weak_mask,
    slot_sim)

OBJECTIVE = {"保守": "calmar", "稳健": "calmar", "激进": "ann"}
MIN_TRADES = 20
FIELDS = ("ann", "mdd", "calmar", "sharpe", "winrate", "pf", "trades")


def load_selections():
    here = os.path.dirname(os.path.abspath(__file__))
    per = os.path.join(here, PER_STOCK_FILE)
    if not os.path.exists(per):
        raise SystemExit(f"缺少 {per}")
    with open(per, encoding="utf-8") as f:
        data = json.load(f)
    sel = {}
    for d in data:
        if d:
            sel[d["code"]] = dict(d.get("mode_candidates", {}))
    return sel


def build_segment(stocks, selections, seg, workers, align_days):
    """跑指定段（train/val）的每股信号 → 时间对齐 → 矩阵。"""
    argl = [(c, r, selections.get(c), seg) for c, r in stocks]
    results = [None] * len(argl)
    with ProcessPoolExecutor(max_workers=workers) as exe:
        futs = {exe.submit(_worker_slot, a): i for i, a in enumerate(argl)}
        done = 0
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception:
                results[i] = {"code": argl[i][0], "dates": None,
                              "buy": {}, "sell": {}, "rp": {}}
            done += 1
            if done % 1500 == 0:
                print(f"    {done}/{len(argl)}")
    ends = [r["dates"][-1] for r in results if r.get("dates")]
    if not ends:
        raise SystemExit("无有效股票")
    last_d = max(ends)
    ld = _dt.date.fromisoformat(last_d)
    keep_idx = [i for i, r in enumerate(results)
                if r.get("dates")
                and (ld - _dt.date.fromisoformat(
                    r["dates"][-1])).days <= align_days]
    cal = sorted({d for i in keep_idx for d in results[i]["dates"]})
    M, buy, sell, rps = build_matrices(results, keep_idx, cal)
    codes = [results[i]["code"] for i in keep_idx]
    return cal, codes, M, buy, sell, rps


def slice_seg(seg, i0, i1):
    cal, codes, M, buy, sell, rps = seg
    Ms = {k: v[:, i0:i1] for k, v in M.items()}
    bs = {t: buy[t][:, i0:i1] for t in buy}
    ss = {t: sell[t][:, i0:i1] for t in sell}
    return cal[i0:i1], codes, Ms, bs, ss, rps


def grid_combos():
    """(name, scale, th, skip)；基线 scale=1 无弱市掩码。"""
    out = [("基线(无弱市覆盖)", 1.0, None, False)]
    for th in (-0.004, -0.006, -0.010):
        out.append((f"纯进场闸门 th<{th:.1%}", 1.0, th, True))
        for sc in (0.4, 0.5, 0.6, 0.7, 0.8):
            out.append((f"ATR×{sc:.1f} th<{th:.1%}", sc, th, False))
            out.append((f"ATR×{sc:.1f} th<{th:.1%} 停开仓", sc, th, True))
    return out


def rp_for(rps, n, tier):
    return {k: (rps.get((k, tier)) or sg.CFG.RISK_PARAMS["稳健"])
            for k in range(n)}


def val_of(m, key):
    v = m.get(key)
    return v if v is not None else -9e9


def eval_slice_rows(combos, seg, tier, masks):
    """在给定段的各覆盖层上跑 slot_sim，返回 {name: metrics}。"""
    cal, codes, M, buy, sell, rps = seg
    rp = rp_for(rps, len(codes), tier)
    out = {}
    for name, sc, th, skip in combos:
        kw = {"weak_mask": (masks[th] if th is not None else None),
              "weak_atr_scale": sc, "weak_skip_entry": skip}
        m = slot_sim(cal, codes, M, buy[tier], sell[tier], rp,
                     SLOT_CFG[tier], **kw)
        out[name] = {k: m.get(k) for k in FIELDS}
    return out


def brief(m):
    return (f"ann={m['ann']*100:+.1f}% mdd={m['mdd']*100:+.1f}% "
            f"cal={(m['calmar'] or 0):+.2f} tr={m['trades']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", nargs="+", default=list(TIERS), choices=TIERS)
    ap.add_argument("--workers", type=int,
                    default=max(1, min(8, os.cpu_count() or 4)))
    ap.add_argument("--align-train", type=int, default=800)
    ap.add_argument("--align-val", type=int, default=45)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    t0 = time.time()
    print("加载每股选型与日K ...")
    selections = load_selections()
    stocks = load_stocks(min_bars=400)
    print(f"  股票 {len(stocks)} 只")

    seg_data = {}
    for seg, al in (("train", args.align_train), ("val", args.align_val)):
        print(f"构建 {seg} 段矩阵（align {al} 天）...")
        seg_data[seg] = build_segment(stocks, selections, seg, args.workers, al)
        cal = seg_data[seg][0]
        print(f"  {seg}: {cal[0]} ~ {cal[-1]}，{len(cal)} 日，"
              f"{len(seg_data[seg][1])} 只")
    print(f"  矩阵构建耗时 {time.time()-t0:.0f}s")

    combos = grid_combos()
    names = [c[0] for c in combos]
    ths = (-0.004, -0.006, -0.010)
    cal_va = seg_data["val"][0]
    h = len(cal_va) // 2
    # 段切片：train 全段 / val 全段 / val 前半 / val 后半
    segs = {
        "train": seg_data["train"],
        "val": seg_data["val"],
        "h1": slice_seg(seg_data["val"], 0, h),
        "h2": slice_seg(seg_data["val"], h, len(cal_va)),
    }
    masks = {seg: {th: market_weak_mask(s[0], th) for th in ths}
             for seg, s in segs.items()}
    for seg, s in segs.items():
        mk = masks[seg][-0.006]
        print(f"  {seg}: {s[0][0]} ~ {s[0][-1]}，{len(s[0])} 日，"
              f"弱市日(th<-0.6%) {int(mk.sum())}/{len(mk)}")

    report = {"ts": time.strftime("%Y-%m-%d %H:%M"), "tiers": {}}
    for tier in args.tiers:
        obj = OBJECTIVE[tier]
        rows = {n: {} for n in names}
        for seg_name in ("train", "val", "h1", "h2"):
            res = eval_slice_rows(combos, segs[seg_name], tier,
                                  masks[seg_name])
            for n in names:
                rows[n][seg_name] = res[n]
        base = rows["基线(无弱市覆盖)"]
        print(f"\n=== {tier} ===  基线: "
              f"train {brief(base['train'])} | val {brief(base['val'])} "
              f"| h1 {brief(base['h1'])} | h2 {brief(base['h2'])}")

        # 按段选参 + 平台检查（邻域：scale±0.1 同 th/skip，或 skip 翻转）
        def pick_on(seg_pick):
            pool = [n for n in names
                    if (rows[n][seg_pick]["trades"] or 0) >= MIN_TRADES] \
                or list(names)
            b = max(pool, key=lambda n: val_of(rows[n][seg_pick], obj))
            bo = val_of(rows[b][seg_pick], obj)
            tol = max(0.05, 0.3 * abs(bo))
            neigh = []
            bb = next(c for c in combos if c[0] == b)
            for n in pool:
                if n == b:
                    continue
                r = next(c for c in combos if c[0] == n)
                same_line = (r[2] == bb[2] and r[3] == bb[3]
                             and abs(r[1] - bb[1]) <= 0.1001)
                flip = (r[2] == bb[2] and r[1] == bb[1] and r[3] != bb[3])
                if same_line or flip:
                    neigh.append(n)
            plat = sum(1 for n in neigh
                       if val_of(rows[n][seg_pick], obj)
                       >= bo - tol) >= 2
            return b, plat

        best_t, plat_t = pick_on("train")
        best_h, plat_h = pick_on("h1")
        best_r, plat_r = pick_on("h2")
        for tag, bsel, plat, pick_seg, eval_seg in (
                ("train→val", best_t, plat_t, "train", "val"),
                ("h1→h2", best_h, plat_h, "h1", "h2"),
                ("h2→h1(反向)", best_r, plat_r, "h2", "h1")):
            m_sel = rows[bsel]
            print(f"  选参[{tag}] {bsel} 平台{'过' if plat else '孤峰'}"
                  f" | {pick_seg} {brief(m_sel[pick_seg])}")
            print(f"      {eval_seg} OOS {brief(m_sel[eval_seg])} vs "
                  f"基线 {brief(base[eval_seg])}")
        ref = "ATR×0.5 th<-0.6%"
        if ref in rows:
            print(f"  [固定对照] {ref}: val {brief(rows[ref]['val'])} | "
                  f"h2 {brief(rows[ref]['h2'])}")
        report["tiers"][tier] = {
            "objective": obj, "baseline": base, "rows": rows,
            "protocols": {
                "train->val": {"pick": best_t, "plateau": plat_t},
                "h1->h2": {"pick": best_h, "plateau": plat_h},
                "h2->h1": {"pick": best_r, "plateau": plat_r}}}

    suffix = f"_{args.tag}" if args.tag else ""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "research", f"portfolio_overlay_opt{suffix}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n写入 {path}，总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
