#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sweep_bata.py - bata（高赔率·低频·允许打板）档选型扫描

协议：
  选型只在训练段 2022-09 ~ 2024-12（`train`）做；
  `val`（2025-08-29 ~ 最新）与 `bull`（2025-03-18 ~ 2026-09-04）仅用于报告复核。
  目标：高赔率（payoff/tail50）、低频（低交易笔数），同时要求年化/回撤可接受。

候选维度：
  score ∈ {beta_star, beta, blend_mom, mom}
  top   ∈ {5, 10, 20}
  reb   ∈ {10, 20, 30}
  闸门  ∈ {sh000688 MA60, sz399006 MA60, sh000001 MA20, 无}
  全部候选 allow_limit_up=True（bata 档解除打板限制）。

用法：
  python backtests/sweep_bata.py            # 四口径全扫（约 20~40 分钟）
  python backtests/sweep_bata.py --universe all --quick
输出：
  research/bata_sweep_<时间戳>/sweep.json / sweep.md
"""
import os as _os_boot
import sys as _sys_boot

ROOT = _os_boot.path.dirname(_os_boot.path.dirname(
    _os_boot.path.abspath(__file__)))
if ROOT not in _sys_boot.path:
    _sys_boot.path.insert(0, ROOT)

import argparse
import datetime as dt
import json
import os
import time

import numpy as np

import stock_gui as sg

UNIS = ("all", "main", "etf", "all_etf")
SEGS = {"train": ("2022-09-01", "2024-12-31"),
        "val": ("2025-08-29", None),
        "bull": ("2025-03-18", "2026-09-04")}
SCORES = ("beta_star", "beta", "blend_mom", "mom")
TOPS = (5, 10, 20)
REBS = (10, 20, 30)
GATES = (("sh000688", 60), ("sz399006", 60), ("sh000001", 20), (None, None))


def seg_idx(cal, seg):
    a, b = SEGS[seg]
    i0 = int(np.searchsorted(cal, a))
    i1 = int(np.searchsorted(cal, b or cal[-1], side="right"))
    return i0, i1


def eval_cfg(codes, cal, C, feat, cfg, seg, bench="sh000688"):
    """单配置评估：相位平均组合 + 逐笔统计（一次模拟同时产出两者）。"""
    i0, i1 = seg_idx(cal, seg)
    base = sg.tier_rank_base(codes, cfg.get("universe", "all"))
    score = sg.tier_make_score(feat, cfg["score"], cfg.get("mom_w"), base=base)
    gate = sg.tier_make_gate(cal, cfg["gate"], cfg["ma"]) \
        if cfg.get("gate") else None
    n_ph = min(cfg["reb"], 30)
    norms, dates, trades = [], None, []
    for p in range(n_ph):
        eq, ec, tr = sg.tier_sim_phase(codes, cal, C, feat, score, gate,
                                       i0, i1, cfg, phase=p, capital=1e6)
        j0 = cfg["reb"] - 1 - p
        if j0 >= len(eq) or eq[j0] <= 0:
            continue
        norms.append(eq[j0:] / eq[j0])
        dates = ec[j0:]
        trades.extend(tr)
    if not norms:
        return None
    L = min(len(e) for e in norms)
    E = np.mean([e[:L] for e in norms], axis=0) * 1e6
    dates = dates[:L]
    m = sg._tier_metrics(E, dates)
    rr = np.array([t["ret"] for t in trades], float) if trades else np.array([])
    holds = np.array([t["hold"] for t in trades], float) if trades else \
        np.array([])
    wins, losses = rr[rr > 0], rr[rr <= 0]
    bcl, _ = sg.tier_idx_series(cal, bench)
    bmap = {d: v for d, v in zip(cal, bcl)}
    bseg = np.array([bmap.get(d, np.nan) for d in dates], float)
    bm = sg._tier_metrics(bseg, dates)
    m.update({
        "n": len(rr),
        "avg_ret": float(rr.mean()) if len(rr) else None,
        "winrate_p": float((rr > 0).mean()) if len(rr) else None,
        "payoff": float(wins.mean() / abs(losses.mean()))
        if len(wins) and len(losses) else None,
        "pf_p": float(wins.sum() / -losses.sum())
        if len(losses) and losses.sum() < 0 else None,
        "avg_hold": float(holds.mean()) if len(holds) else None,
        "tail20": float((rr > 0.20).mean()) if len(rr) else None,
        "tail50": float((rr > 0.50).mean()) if len(rr) else None,
        "best": float(rr.max()) if len(rr) else None,
        "worst": float(rr.min()) if len(rr) else None,
        "bench_total": bm.get("total"),
        "excess": (m.get("total") - bm.get("total"))
        if m.get("total") is not None and bm.get("total") is not None else None,
    })
    return m


def fmt_row(tag, cfg, m):
    if not m:
        return f"{tag:<28} 无数据"
    gate = f"{cfg['gate']}MA{cfg['ma']}" if cfg.get("gate") else "无闸门"
    return (f"{tag:<28} {cfg['score']:<9} top{cfg['top']:<3} reb{cfg['reb']:<3} "
            f"{gate:<12} 年化{(m['ann'] or 0)*100:+6.1f}% "
            f"回撤{(m['mdd'] or 0)*100:+6.1f}% 笔数{m['n']:>5} "
            f"均{(m['avg_ret'] or 0)*100:+5.2f}% 胜率{(m['winrate_p'] or 0)*100:4.1f}% "
            f"赔率{(m['payoff'] or 0):4.2f} PF{(m['pf_p'] or 0):4.2f} "
            f"持有{(m['avg_hold'] or 0):4.1f} >50%{(m['tail50'] or 0)*100:4.1f}% "
            f"超额{(m['excess'] or 0)*100:+6.1f}pp")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="all4",
                    choices=("all4", "all", "main", "etf", "all_etf"))
    ap.add_argument("--segment", default="train",
                    choices=("train", "val", "bull"))
    ap.add_argument("--quick", action="store_true",
                    help="缩表：top5/10 + reb20/30 + 仅科创50闸门")
    ap.add_argument("--phase", action="store_true",
                    help="只跑「闸门对照」阶段（对每口径最优 base 配置）")
    ap.add_argument("--neighbors", action="store_true",
                    help="对每口径最优族做邻域扫描（top/reb/mom_w + 打板对照）")
    ap.add_argument("--base-json", default="",
                    help="--phase 用的首轮结果 JSON（默认读最新 sweep.json）")
    args = ap.parse_args()

    t0 = time.time()
    codes, cal, C, V = sg.tier_load_panel()
    print(f"面板 {len(codes)} 只 × {len(cal)} 日")
    if sg._TIER_CACHE.get("feat") is None:
        sg._TIER_CACHE["feat"] = sg.tier_build_features(cal, C, V)
    feat = sg._TIER_CACHE["feat"]

    out_dir = os.path.join(ROOT, "research",
                           f"bata_sweep_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(out_dir, exist_ok=True)
    unis = list(UNIS) if args.universe == "all4" else [args.universe]

    if args.phase or args.neighbors:
        # 第二轮：对首轮每口径最优 base 配置做闸门对照（训练段）
        src = args.base_json
        if not src:
            cands = sorted(
                [os.path.join(ROOT, "research", d) for d in
                 os.listdir(os.path.join(ROOT, "research"))
                 if d.startswith("bata_sweep_")
                 and os.path.isfile(os.path.join(ROOT, "research", d,
                                                 "sweep.json"))],
                reverse=True)
            src = os.path.join(cands[0], "sweep.json")
        with open(src, encoding="utf-8") as f:
            first = json.load(f)
        if args.neighbors:
            results = {}
            for u in unis:
                base = first["results"][u]["train_best"]["cfg"]
                print("=" * 100)
                print(f"【{sg.UNIVERSE_NAME[u]}】邻域扫描 base: {base}")
                rows = []
                for top in (3, 5, 8, 10, 20):
                    for reb in (10, 20, 30):
                        for mw in (0.5, 0.6, 0.7, 0.8, 0.9):
                            cfg = dict(base)
                            cfg.update(top=top, reb=reb, mom_w=mw)
                            m = eval_cfg(codes, cal, C, feat, cfg, "train")
                            tag = f"{u} top{top} reb{reb} mw{mw}"
                            rows.append({"cfg": cfg, "m": m, "tag": tag})
                            print(fmt_row(tag, cfg, m), flush=True)
                # 打板对照（同配置 allow_limit_up True/False）
                cfg = dict(base)
                m_on = eval_cfg(codes, cal, C, feat, cfg, "train")
                cfg = dict(base, allow_limit_up=False)
                m_off = eval_cfg(codes, cal, C, feat, cfg, "train")
                print(f"[打板对照] 解除: 年化{(m_on['ann'] or 0)*100:+.1f}% "
                      f"赔率{(m_on['payoff'] or 0):.2f} 笔数{m_on['n']} | "
                      f"限制: 年化{(m_off['ann'] or 0)*100:+.1f}% "
                      f"赔率{(m_off['payoff'] or 0):.2f} 笔数{m_off['n']}")
                results[u] = {"base": base, "rows": rows,
                              "limit_up_on": m_on, "limit_up_off": m_off}
            with open(os.path.join(out_dir, "neighbors.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"results": results}, f, ensure_ascii=False,
                          indent=1, default=float)
            print(f"\n耗时 {time.time() - t0:.0f}s → {out_dir}")
            return
        results = {}
        for u in unis:
            best = first["results"][u]["train_best"]
            print("=" * 100)
            print(f"【{sg.UNIVERSE_NAME[u]}】首轮最优 base: {best}")
            rows = []
            for gcode, gma in GATES:
                cfg = dict(best["cfg"])
                if gcode:
                    cfg.update(gate=gcode, ma=gma)
                else:
                    cfg.pop("gate", None)
                    cfg.pop("ma", None)
                m = eval_cfg(codes, cal, C, feat, cfg, "train")
                tag = f"{sg.UNIVERSE_NAME[u]}/gate={gcode or '无'}"
                rows.append({"cfg": cfg, "train": m})
                print(fmt_row(tag, cfg, m))
            results[u] = {"base": best["cfg"], "gates": rows}
        with open(os.path.join(out_dir, "phase2.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"results": results}, f, ensure_ascii=False, indent=1)
        print(f"\n耗时 {time.time() - t0:.0f}s → {out_dir}")
        return

    tops = (5, 10) if args.quick else TOPS
    rebs = (20, 30) if args.quick else REBS
    gates = GATES[:1] if args.quick else GATES

    report = {"ts": time.strftime("%Y-%m-%d %H:%M"),
              "segment": args.segment, "results": {}}
    for u in unis:
        print("=" * 100)
        print(f"【{sg.UNIVERSE_NAME[u]}】{args.segment} 扫描中 ...")
        rows = []
        for score in SCORES:
            for top in tops:
                for reb in rebs:
                    for gcode, gma in gates:
                        cfg = dict(universe=u, score=score, top=top, reb=reb,
                                   allow_limit_up=True)
                        if gcode:
                            cfg.update(gate=gcode, ma=gma)
                        else:
                            cfg.update(gate=None, ma=None)
                        m = eval_cfg(codes, cal, C, feat, cfg, args.segment)
                        tag = (f"{score} top{top} reb{reb} "
                               f"{gcode or '无'}")
                        rows.append({"cfg": cfg, "m": m, "tag": tag})
                        print(fmt_row(tag, cfg, m), flush=True)
        # 训练段选型：赔率/PF/年化为主，年化必须为正、回撤不过分
        ok = [r for r in rows
              if r["m"] and r["m"]["ann"] is not None and r["m"]["ann"] > 0
              and (r["m"]["excess"] or 0) > -0.05]

        def rank_key(r):
            m = r["m"]
            return (m["payoff"] or 0, m["pf_p"] or 0, m["ann"] or 0,
                    -(m["mdd"] or 0))
        ok.sort(key=rank_key, reverse=True)
        best = ok[0] if ok else (max(
            [r for r in rows if r["m"]], key=lambda r: r["m"]["ann"] or -9)
            if any(r["m"] for r in rows) else None)
        report["results"][u] = {"rows": rows,
                                "train_best": best}
        if best:
            print(f"→ 训练段选型（按赔率/PF/年化）: {best['tag']}")
    p = os.path.join(out_dir, "sweep.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, default=float)
    print(f"\n结果 {p}；耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
