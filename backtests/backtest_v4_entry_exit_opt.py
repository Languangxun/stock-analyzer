#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_v4_entry_exit_opt.py - v4 三档「进入/退出」决策层优化（防过拟合协议）

背景：v4 预测为每股 Walk-Forward 模型（不重训），收益瓶颈在决策层。
本脚本固定 research/v4_preds.pkl，只搜索进入/退出参数：

协议（防过拟合）：
  1. 日历按时间切三段 S1/S2/S3；
  2. 选参：只看 T12=S1+S2 的连续持仓口径（样本内）；
  3. 验证：S1/S2/S3 各跑一次「切片重置」独立回测（不继承段前仓位），
     稳健候选必须 S3 样本外不劣于默认、且 ≥2/3 段不劣于默认；
  4. 自动把「样本内最优进入变体 × 最优退出变体」组合再评估（坐标上升一轮）；
  5. 全部候选明细落盘 research/v4_entry_exit_opt.json，便于人工复核参数平台。

用法：
  python backtest_v4_entry_exit_opt.py                 # 三档
  python backtest_v4_entry_exit_opt.py --tiers 激进 保守
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
import statistics
import time

import stock_gui as sg
from backtest_exit_roll import load_mats, slice_mats

TIERS = ("保守", "平衡", "激进")
OBJECTIVE = {"保守": "calmar", "平衡": "calmar", "激进": "ann"}
CLEAN_SEGS = ("S1", "S2", "S3")


def build_candidates(name):
    """返回 [(label, kind, tier_over, rule_over)]；kind: entry/exit/other。"""
    d = sg._V4_TIERS[name]
    c = []

    def add(label, kind, t=None, r=None):
        c.append((label, kind, t or {}, r or {}))

    add("默认", "other")
    # ---- 进入阈值 ----
    for dp in (-0.04, -0.02, 0.02, 0.04):
        add("p_th%+.2f" % dp, "entry", {"p_th": d["p_th"] + dp})
    for dr in (-0.004, -0.002, 0.002, 0.004):
        add("r_th%+.3f" % dr, "entry",
            {"r_th": max(0.0, round(d["r_th"] + dr, 4))})
    for da in (-0.2, -0.1, 0.1, 0.2):
        add("a_th%+.1f" % da, "entry", {"a_th": round(d["a_th"] + da, 2)})
    for ep in (0.40, 0.45, 0.50, 0.55):
        add("exit_p=%.2f" % ep, "entry", {"exit_p": ep})
    # ---- 退出结构 ----
    add("stop_q=25", "exit", r={"stop_q": 25})
    add("stop_q=50", "exit", r={"stop_q": 50})
    add("target_q=50", "exit", r={"target_q": 50})
    add("target_q=90", "exit", r={"target_q": 90})
    add("无Q10止损", "exit", r={"use_q10_stop": False})
    for v in (3, 5, 10):
        add("min_hold=%d" % v, "exit", r={"min_hold": v})
    for v in (3, 5, 10):
        add("cooldown=%d" % v, "exit", r={"cooldown": v})
    add("无p_up退出", "exit", r={"use_logistic": False})
    # ---- 弱市条件化止损 ----
    for wq in (10, 25, 50):
        for wm in (-0.002, -0.004, -0.006, -0.010):
            add("weak Q%d mkt<%.1f%%" % (wq, wm * 100), "exit",
                r={"weak_q": wq, "weak_mkt": wm})
    for wq, wm in ((25, -0.004), (25, -0.006), (50, -0.010)):
        add("weak Q%d mkt<%.1f%%+exitP.55" % (wq, wm * 100), "exit",
            r={"weak_q": wq, "weak_mkt": wm, "weak_exit_p": 0.55})
    add("weak Q25 disp>=0.7", "exit", r={"weak_q": 25, "weak_disp": 0.7})
    # ---- 进入闸门/组件 ----
    add("无再入场升档", "other", r={"reentry_tier": False})
    add("无q50门槛", "other", r={"q50_entry": False})
    add("轮动Top50", "other", r={"rot_top": 0.50})
    add("行业跑赢大盘", "other", r={"rot_strong": True})
    add("disp_max=0.5", "other", r={"disp_max": 0.5})
    add("disp_min=0.7", "other", r={"disp_min": 0.7})
    add("h_only=10", "other", r={"h_only": 10})
    return c


def run_one(mats, tier_name, tier_over, rule_over):
    tier = dict(sg._V4_TIERS[tier_name])
    tier.update(tier_over)
    rules = {"mode": "full", "tier_name": tier_name}
    rules.update(sg._V4_TIER_EXTRA.get(tier_name, {}))
    rules.update(rule_over)
    return sg._v4_portfolio_sim(mats, tier, rules)


def seg_stats(eq, dates, i0, i1):
    """权益曲线 [i0,i1) 段：收益/年化/段内回撤/Calmar。"""
    base = eq[i0 - 1] if i0 > 0 else eq[0]
    r = eq[i1 - 1] / base - 1.0 if base > 0 else -1.0
    n = i1 - i0
    ann = (1 + r) ** (250.0 / n) - 1 if r > -1 else -1.0
    peak, mdd = base, 0.0
    for j in range(i0, i1):
        peak = max(peak, eq[j])
        if peak > 0:
            mdd = min(mdd, eq[j] / peak - 1.0)
    cal = ann / abs(mdd) if mdd < -1e-9 else None
    return {"ret": r, "ann": ann, "mdd": mdd, "calmar": cal}


def eval_slice(m):
    """切片单段（重置口径）指标。"""
    eq = m["equity"]
    return {"ret": eq[-1] / eq[0] - 1.0 if eq and eq[0] else -1.0,
            "ann": m.get("ann"), "mdd": m.get("mdd"),
            "calmar": m.get("calmar"), "sharpe": m.get("sharpe"),
            "winrate": m.get("winrate"), "pf": m.get("pf"),
            "trades": m.get("trades")}


def eval_candidate(m, i1, i2, nc):
    out = {}
    eq, ds = m["equity"], m["dates"]
    for tag, a, b in (("S1", 0, i1), ("S2", i1, i2), ("S3", i2, nc),
                      ("T12", 0, i2), ("all", 0, nc)):
        out[tag] = seg_stats(eq, ds, a, b) if b > a else None
    for k in ("ann", "mdd", "calmar", "sharpe", "winrate", "pf", "trades"):
        out[k] = m.get(k)
    return out


def wf_folds(nc, train=200, test=60, step=60):
    """滚动折：(train0,train1,test0,test1)，测试段互不重叠。"""
    folds, s = [], 0
    while s + train + test <= nc:
        folds.append((s, s + train, s + train, s + train + test))
        s += step
    if s + train < nc:
        folds.append((s, s + train, s + train, nc))
    return folds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", nargs="+", default=list(TIERS), choices=TIERS)
    ap.add_argument("--top", type=int, default=10, help="每档打印/复核的候选数")
    ap.add_argument("--tag", default="")
    ap.add_argument("--no-wf", action="store_true", help="跳过滚动WF验证")
    ap.add_argument("--wf-train", type=int, default=200)
    ap.add_argument("--wf-test", type=int, default=60)
    ap.add_argument("--wf-step", type=int, default=60)
    args = ap.parse_args()
    t0 = time.time()
    print("加载 v4 预测缓存 ...")
    mats = load_mats()
    cal = mats[0]
    nc = len(cal)
    i1, i2 = nc // 3, 2 * nc // 3
    seg_idx = {"S1": (0, i1), "S2": (i1, i2), "S3": (i2, nc)}
    print(f"日历 {cal[0]} ~ {cal[-1]}，{nc} 日；"
          f"S1={cal[0]}~{cal[i1-1]}  S2={cal[i1]}~{cal[i2-1]}  "
          f"S3={cal[i2]}~{cal[-1]}")
    print("协议：T12 连续口径选参；S1/S2/S3 切片重置验证；S3 为样本外")

    seg_mats = {s: slice_mats(mats, a, b) for s, (a, b) in seg_idx.items()}

    def clean(tier_name, t_over, r_over):
        return {s: eval_slice(run_one(seg_mats[s], tier_name, t_over, r_over))
                for s in CLEAN_SEGS}

    report = {"ts": time.strftime("%Y-%m-%d %H:%M"),
              "calendar": [cal[0], cal[-1]], "n_days": nc,
              "split": {"S1": list(seg_idx["S1"]),
                        "S2": list(seg_idx["S2"]),
                        "S3": list(seg_idx["S3"])}, "tiers": {}}
    for tier_name in args.tiers:
        t1 = time.time()
        base_m = run_one(mats, tier_name, {}, {})
        base = eval_candidate(base_m, i1, i2, nc)
        base["clean"] = clean(tier_name, {}, {})
        obj = OBJECTIVE[tier_name]

        def score(e):
            v = e["T12"].get(obj)
            return -1e9 if v is None else v

        cands = {}
        for label, kind, t_over, r_over in build_candidates(tier_name):
            try:
                m = run_one(mats, tier_name, t_over, r_over)
                c = {"kind": kind, "tier_over": t_over, "rule_over": r_over,
                     "stats": eval_candidate(m, i1, i2, nc)}
                c["stats"]["clean"] = clean(tier_name, t_over, r_over)
                cands[label] = c
            except Exception as exc:
                print(f"  [{tier_name}] {label} 异常: {exc}")

        # ---- 组合头部进入 × 退出（坐标上升一轮）----
        def _kind_pool(kind):
            pool = [(l, c) for l, c in cands.items() if c["kind"] == kind
                    and score(c["stats"]) > score(base)]
            pool.sort(key=lambda kv: -score(kv[1]["stats"]))
            return pool[:3]

        for le, ce in _kind_pool("entry"):
            for lx, cx in _kind_pool("exit"):
                lab = f"{le} + {lx}"
                to = dict(ce["tier_over"])
                ro = dict(cx["rule_over"])
                try:
                    m = run_one(mats, tier_name, to, ro)
                    c = {"kind": "combo", "tier_over": to, "rule_over": ro,
                         "stats": eval_candidate(m, i1, i2, nc)}
                    c["stats"]["clean"] = clean(tier_name, to, ro)
                    cands[lab] = c
                except Exception as exc:
                    print(f"  [{tier_name}] {lab} 异常: {exc}")

        # ---- 稳健过滤 ----
        def robust(c):
            st, bc = c["stats"], base
            ok_sel = score(st) > score(bc)
            o3, b3 = st["clean"]["S3"], bc["clean"]["S3"]
            ok_oos = (o3["ann"] is not None and b3["ann"] is not None
                      and o3["ann"] >= b3["ann"] - 1e-9)
            wins = sum(1 for s in CLEAN_SEGS
                       if st["clean"][s]["ret"] >= bc["clean"][s]["ret"] - 1e-9)
            return ok_sel and ok_oos and wins >= 2, wins

        ranked = sorted(cands.items(), key=lambda kv: -score(kv[1]["stats"]))
        robust_list = [(l, c, robust(c)) for l, c in ranked if robust(c)[0]]

        print(f"\n=== [{tier_name}] 默认: T12 {obj}={score(base):+.2f} "
              f"| 全期年化 {base['ann']*100:+.1f}% 回撤 {base['mdd']*100:+.1f}% "
              f"| 切片S3年化 {base['clean']['S3']['ann']*100:+.1f}% ===")
        b = base["clean"]
        print(f"  默认切片: S1 {b['S1']['ret']*100:+.1f}% / "
              f"S2 {b['S2']['ret']*100:+.1f}% / "
              f"S3 {b['S3']['ret']*100:+.1f}%(年化 "
              f"{b['S3']['ann']*100:+.1f}%)")
        print(f"{'候选':<32}{'T12收益':>9}{'T12'+obj[:3]:>8}"
              f"{'S1切':>8}{'S2切':>8}{'S3切':>8}{'S3切年化':>9}"
              f"{'全期年化':>9}{'全期回撤':>9}{'段胜':>5}{'稳健':>5}")
        for label, c, (ok, wins) in robust_list[:args.top]:
            st = c["stats"]
            cl = st["clean"]
            print(f"{label:<32}{st['T12']['ret']*100:>+8.1f}%"
                  f"{score(st):>+8.2f}{cl['S1']['ret']*100:>+7.1f}%"
                  f"{cl['S2']['ret']*100:>+7.1f}%{cl['S3']['ret']*100:>+7.1f}%"
                  f"{cl['S3']['ann']*100:>+8.1f}%{st['ann']*100:>+8.1f}%"
                  f"{st['mdd']*100:>+8.1f}%{wins:>5d}{'Y':>5}")
        print("  -- 样本内头部（未过滤稳健），看 S3 切片是否翻车 --")
        for label, c in ranked[:args.top]:
            st = c["stats"]
            cl = st["clean"]
            print(f"{label:<32}{st['T12']['ret']*100:>+8.1f}%"
                  f"{score(st):>+8.2f}{cl['S1']['ret']*100:>+7.1f}%"
                  f"{cl['S2']['ret']*100:>+7.1f}%{cl['S3']['ret']*100:>+7.1f}%"
                  f"{cl['S3']['ann']*100:>+8.1f}%{st['ann']*100:>+8.1f}%"
                  f"{st['mdd']*100:>+8.1f}%")

        winner = None
        if robust_list and robust_list[0][0] != "默认":
            wl, wc, _ = robust_list[0]
            winner = {"label": wl, "tier_over": wc["tier_over"],
                      "rule_over": wc["rule_over"],
                      "clean": wc["stats"]["clean"],
                      "T12": wc["stats"]["T12"]}
            print(f"  >> 推荐: {wl}  (S3 切片年化 "
                  f"{wc['stats']['clean']['S3']['ann']*100:+.1f}% vs 默认 "
                  f"{base['clean']['S3']['ann']*100:+.1f}%)")
        else:
            print("  >> 无稳健胜出候选，维持默认")

        # ---- 滚动 Walk-Forward：验证「选参过程」本身的样本外表现 ----
        wf_out = None
        if not args.no_wf:
            folds = wf_folds(nc, args.wf_train, args.wf_test, args.wf_step)
            pools = build_candidates(tier_name)
            wf_rows = []
            for (a, b, c, d) in folds:
                trm = slice_mats(mats, a, b)
                tem = slice_mats(mats, c, d)
                best = None
                for label, kind, to, ro in pools:
                    e = eval_slice(run_one(trm, tier_name, to, ro))
                    sc = e.get(obj)
                    if sc is None:
                        continue
                    if best is None or sc > best[0]:
                        best = (sc, label, to, ro)
                if best is None:
                    continue
                dm = eval_slice(run_one(tem, tier_name, {}, {}))
                wm = eval_slice(run_one(tem, tier_name, best[2], best[3]))
                wf_rows.append({"train": [cal[a], cal[b - 1]],
                                "test": [cal[c], cal[d - 1]],
                                "pick": best[1], "test_ann": wm["ann"],
                                "test_ret": wm["ret"],
                                "default_ann": dm["ann"],
                                "default_ret": dm["ret"]})
            if wf_rows:
                wins = sum(1 for r in wf_rows if r["test_ann"] is not None
                           and r["default_ann"] is not None
                           and r["test_ann"] >= r["default_ann"])
                win_ret = sum(1 for r in wf_rows
                              if r["test_ret"] >= r["default_ret"])
                med_w = statistics.median(r["test_ann"] for r in wf_rows)
                med_d = statistics.median(r["default_ann"] for r in wf_rows)
                # 固定规则跨测试段一致性（默认 + 头部稳健候选）
                fixed = {}
                for label in ["默认"] + [l for l, _, _ in robust_list[:3]]:
                    if label == "默认":
                        to, ro = {}, {}
                    else:
                        cc = cands.get(label)
                        if not cc:
                            continue
                        to, ro = cc["tier_over"], cc["rule_over"]
                    anns = [eval_slice(run_one(
                        slice_mats(mats, c, d), tier_name, to, ro))["ann"]
                        for (a, b, c, d) in folds]
                    fixed[label] = anns
                wf_out = {"folds": wf_rows, "ann_wins": wins,
                          "ret_wins": win_ret, "n": len(wf_rows),
                          "median_ann": med_w, "median_default_ann": med_d,
                          "fixed": fixed}
                print(f"  -- 滚动WF({len(wf_rows)}折 训{args.wf_train}/测"
                      f"{args.wf_test}日): 选参OOS年化中位 {med_w*100:+.1f}% "
                      f"vs 默认 {med_d*100:+.1f}% | 胜 {wins}/{len(wf_rows)}"
                      f"(年化) {win_ret}/{len(wf_rows)}(收益)")
                for r in wf_rows:
                    print(f"     {r['test'][0]}~{r['test'][1]} 选"
                          f"[{r['pick']}] OOS {r['test_ann']*100:+.1f}% vs "
                          f"默认 {r['default_ann']*100:+.1f}%")
                for label, anns in fixed.items():
                    print(f"     [固定] {label:<28} 各测试段年化中位 "
                          f"{statistics.median(anns)*100:+.1f}%")

        report["tiers"][tier_name] = {
            "base": base,
            "winner": winner,
            "wf": wf_out,
            "candidates": {l: {"kind": c["kind"],
                               "tier_over": c["tier_over"],
                               "rule_over": c["rule_over"],
                               "stats": c["stats"]}
                           for l, c in cands.items()},
        }
        print(f"  [{tier_name}] 用时 {time.time()-t1:.0f}s")

    suffix = f"_{args.tag}" if args.tag else ""
    path = os.path.join(ROOT,
                        "research", f"v4_entry_exit_opt{suffix}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n写入 {path}，总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
