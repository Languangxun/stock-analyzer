#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tier_picks_from_ablation.py - 从消融 per-stock JSON 生成三档选型 + 刷新 GUI 策略缓存

用途（v6.1.5 热修⑥，配合"近端门槛改用训练段末尾"的泄漏修复）：
  1. 读 `research/strategy_ablation_per_stock.json`（backtest_strategy_ablation.py 产物，
     含每只股票的全部候选 all_candidates）；
  2. 用与 GUI `run_ablation` 完全相同的选型逻辑（`sg._pick_one_from_pool`）逐只选出
     保守/稳健/激进三档策略；
  3. 写瘦身文件 `research/perstock_tier_picks.json`（只含三档 algo/params/label，
     供 stock_backtest_export.py --mode tiers 使用，避免每次加载 100MB+ 全候选）；
  4. 按训练集 Calmar（年化波动 >45% 时避开保守档，与 GUI 推荐一致）选出「推荐档」，
     用 `sg.save_strategy` 写回 GUI 策略缓存（meta 表，5 日 TTL），
     使修复后的重新消融立即在 GUI 生效。

用法：
  python backtests/tier_picks_from_ablation.py            # 生成 + 刷新缓存
  python backtests/tier_picks_from_ablation.py --no-cache # 只生成瘦身文件
  python backtests/tier_picks_from_ablation.py --limit 100
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import stock_gui as sg  # noqa: E402
import bt_common          # noqa: E402  回测统一规范

RESEARCH = os.path.join(ROOT, "research")


def _calmar(m):
    m = m or {}
    return (m.get("ann") or 0) / max(abs(m.get("mdd") or 0.05), 0.05)


def _vol_of(conn, code, lookback=250):
    rs = conn.execute(
        "SELECT close FROM daily_bars WHERE code=? ORDER BY date DESC LIMIT ?",
        (code, lookback)).fetchall()
    rows = [{"close": r[0]} for r in rs][::-1]
    return sg._annualized_vol(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(
        ROOT, "research", "strategy_ablation_per_stock.json"))
    ap.add_argument("--out", default="",
                    help="三档选型输出文件（默认：写进源消融运行目录，"
                         "或新建 research/tier_picks_v<版本>_<时间戳>/）")
    ap.add_argument("--no-cache", action="store_true",
                    help="不写 GUI 策略缓存")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    # 统一规范：产物写进源消融运行目录（ablation_v*/tier_picks.json）；
    # 源不是运行目录时新建 tier_picks 运行目录；根目录固定名挂最新副本
    src_dir = os.path.dirname(os.path.abspath(args.src))
    src_base = os.path.basename(src_dir)
    run_dir = None
    if args.out:
        out_file = args.out
    else:
        # 源若是根目录「最新副本」（硬链接），回溯到对应消融运行目录，
        # 让 tier_picks.json 与它所属的那次消融留在一起
        ab_run = None
        try:
            if os.path.isfile(args.src):
                ino = os.stat(args.src).st_ino
                cand = bt_common.latest_run_dir(RESEARCH, "ablation")
                if cand and os.stat(os.path.join(
                        cand, "per_stock.json")).st_ino == ino:
                    ab_run = cand
        except OSError:
            ab_run = None
        if src_base.startswith("ablation_v"):
            out_file = os.path.join(src_dir, "tier_picks.json")
        elif ab_run:
            src_dir, run_dir = ab_run, None
            out_file = os.path.join(ab_run, "tier_picks.json")
        else:
            run_dir = bt_common.new_run_dir(RESEARCH, "tier_picks")
            out_file = os.path.join(run_dir, "tier_picks.json")
    latest = os.path.join(RESEARCH, "perstock_tier_picks.json")

    print(f"加载 {args.src} …", flush=True)
    t0 = time.time()
    with open(args.src, encoding="utf-8") as f:
        per = json.load(f)
    print(f"  {len(per)} 只，耗时 {time.time() - t0:.0f}s", flush=True)

    out, n_rec = {}, {"保守": 0, "稳健": 0, "激进": 0}
    n_cache = 0
    conn = sg._cx() if not args.no_cache else None
    items = per[:args.limit] if args.limit else per
    for i, s in enumerate(items, 1):
        code = s.get("code")
        if not code:
            continue
        pool = sg._ablation_pool(s.get("all_candidates") or [], 8)
        if not pool:
            continue
        picks, full = {}, {}
        for tier in ("保守", "稳健", "激进"):
            picked, _note = sg._pick_one_from_pool(pool, tier)
            full[tier] = picked
            picks[tier] = {"algo": picked.get("algo", "composite"),
                           "params": picked.get("params")
                           or sg.CFG.RISK_PARAMS["稳健"],
                           "label": picked.get("label", ""),
                           "mode": picked.get("mode", tier)}
        out[code] = picks
        # 推荐档：训练集 Calmar（高波动避开保守），与 GUI run_ablation 一致
        scored = [(t, _calmar(full[t].get("train")))
                  for t in ("保守", "稳健", "激进")
                  if (full[t].get("train") or {}).get("trades", 0) >= 3]
        rec = max(scored, key=lambda x: x[1])[0] if scored else "稳健"
        try:
            vol = _vol_of(conn, code) if conn is not None else None
        except Exception:
            vol = None
        if rec == "保守" and vol is not None and vol > 0.45:
            alt = [(t, v) for t, v in scored if t in ("稳健", "激进")]
            if alt:
                rec = max(alt, key=lambda x: x[1])[0]
        n_rec[rec] = n_rec.get(rec, 0) + 1
        if conn is not None:
            c = picks[rec]
            sg.save_strategy(code, {"algo": c["algo"], "mode": c["mode"],
                                    "params": c["params"],
                                    "label": c["label"], "ts": time.time()})
            n_cache += 1
        if i % 1000 == 0:
            print(f"  {i}/{len(items)} …", flush=True)

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    sz = os.path.getsize(out_file) / 1e6
    print(f"写出 {out_file}（{len(out)} 只，{sz:.1f} MB）")
    bt_common.link_latest(out_file, latest)
    print(f"根目录最新副本: {latest}")
    info = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(out), "source": os.path.basename(args.src),
            "out": os.path.basename(out_file),
            "recommend": n_rec, "cache_refreshed": n_cache}
    if run_dir:
        bt_common.write_run_meta(run_dir, argv=sys.argv,
                                 elapsed=time.time() - t0, **info)
    else:
        meta_p = os.path.join(src_dir, "run_meta.json")
        try:
            meta = json.load(open(meta_p, encoding="utf-8"))
        except Exception:
            meta = {}
        meta["tier_picks"] = info
        with open(meta_p, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"推荐档分布: {n_rec}")
    if conn is not None:
        print(f"GUI 策略缓存已刷新 {n_cache} 只（5 日 TTL）")
    print(f"总耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
