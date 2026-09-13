#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_exit_ablation.py - v4 退出结构专项消融（默认以「激进」档为主）

复用 research/v4_preds.pkl（Walk-Forward 预测与退出规则无关），
只重跑决策层组合回测，分钟级完成。

用法：
  python backtest_exit_ablation.py                 # 激进档
  python backtest_exit_ablation.py --tier 平衡
  python backtest_exit_ablation.py --tag combo     # 结果写 exit_ablation_<tag>.json
"""
import argparse
import json
import os
import pickle
import time

import stock_gui as sg


def load_mats():
    """加载预测缓存并重建堆叠矩阵（对齐子样本 + 轮动上下文）。"""
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "research", "v4_preds.pkl")
    with open(cache, "rb") as f:
        blob = pickle.load(f)
    preds = blob["preds"]
    with sg.db_conn() as conn:
        ind_of = {c: (i or "") for c, i in
                  conn.execute("SELECT code, industry FROM stocks")}
    mkt, ind = sg._v4_mkt_ind_ctx(ind_of)
    mats = sg._v4_stack(preds)
    sg._v4_attach_rotation(mats[1], mats[0], mats[2], ind_of, mkt, ind)
    import datetime as _dt
    last_d = max(r["dates"][-1] for r in preds)
    _ld = _dt.date.fromisoformat(last_d)
    rows_bt = [k for k, r in enumerate(preds)
               if (_ld - _dt.date.fromisoformat(r["dates"][-1])).days <= 45]
    mats_bt = sg._v4_stack_subset([preds[k] for k in rows_bt])
    sg._v4_attach_rotation(mats_bt[1], mats_bt[0], mats_bt[2], ind_of, mkt, ind)
    return mats, mats_bt, len(rows_bt), len(preds)


# 退出专项变体：单因子 + 参数扫描（规则见 stock_gui._v4_portfolio_sim）
EXIT_VARIANTS = [
    ("Full(默认)", {}),
    ("exit_mode=hybrid", {"exit_mode": "hybrid"}),
    ("hybrid+无移动止盈", {"exit_mode": "hybrid", "use_trailing": False}),
    ("无Q10棘轮", {"use_q10_stop": False}),
    ("Q25棘轮", {"stop_q": 25}),
    ("Q25棘轮+hybrid", {"stop_q": 25, "exit_mode": "hybrid"}),
    ("目标Q50", {"target_q": 50}),
    ("目标Q90", {"target_q": 90}),
    ("无分布退出(ATR)", {"use_dist_exit": False}),
    ("无p_up退出", {"use_logistic": False}),
    ("exit_p=0.40", {"exit_p": 0.40}),
    ("exit_p=0.50", {"exit_p": 0.50}),
    ("exit_p=0.55", {"exit_p": 0.55}),
    ("min_hold=3", {"min_hold": 3}),
    ("min_hold=5", {"min_hold": 5}),
    ("min_hold=10", {"min_hold": 10}),
    ("cooldown=3", {"cooldown": 3}),
    ("cooldown=5", {"cooldown": 5}),
    ("cooldown=10", {"cooldown": 10}),
    ("hybrid:1.05/0.95", {"exit_mode": "hybrid", "trail_trigger": 1.05,
                          "trail_ratio": 0.95}),
    ("hybrid:1.10/0.92", {"exit_mode": "hybrid", "trail_trigger": 1.10,
                          "trail_ratio": 0.92}),
    ("hybrid:1.15/0.90", {"exit_mode": "hybrid", "trail_trigger": 1.15,
                          "trail_ratio": 0.90}),
    ("hybrid:1.05/0.90", {"exit_mode": "hybrid", "trail_trigger": 1.05,
                          "trail_ratio": 0.90}),
    ("ATR止损x1.0", {"use_dist_exit": False, "atr_mult": 1.0}),
    ("ATR止损x2.0", {"use_dist_exit": False, "atr_mult": 2.0}),
    ("无升档再入场", {"reentry_tier": False}),
]

# 第二批：对第一批中“降回撤”或“增收益”的旋钮做组合
COMBO_VARIANTS = [
    ("Q50棘轮", {"stop_q": 50}),
    ("Q25+cooldown10", {"stop_q": 25, "cooldown": 10}),
    ("Q25+无p_up退出", {"stop_q": 25, "use_logistic": False}),
    ("Q25+目标Q90", {"stop_q": 25, "target_q": 90}),
    ("Q25+min_hold5", {"stop_q": 25, "min_hold": 5}),
    ("Q25+无再入场", {"stop_q": 25, "reentry_tier": False}),
    ("cooldown10+无p_up退出", {"cooldown": 10, "use_logistic": False}),
    ("cooldown10+min_hold5", {"cooldown": 10, "min_hold": 5}),
    ("无p_up退出+无再入场", {"use_logistic": False, "reentry_tier": False}),
    ("exit_p=0.55+Q25", {"exit_p": 0.55, "stop_q": 25}),
    ("exit_p=0.55+cooldown10", {"exit_p": 0.55, "cooldown": 10}),
    ("Q25+无p_up退出+cooldown10", {"stop_q": 25, "use_logistic": False,
                                    "cooldown": 10}),
    ("Q25+cooldown10+无再入场", {"stop_q": 25, "cooldown": 10,
                                 "reentry_tier": False}),
    ("min_hold5+无p_up退出+cooldown10", {"min_hold": 5, "use_logistic": False,
                                          "cooldown": 10}),
]

# 第三批：围绕 Q25 棘轮 + 高目标位 细化
COMBO2_VARIANTS = [
    ("Q25+Q90", {"stop_q": 25, "target_q": 90}),
    ("Q25+Q90+无p_up退出", {"stop_q": 25, "target_q": 90,
                            "use_logistic": False}),
    ("Q25+Q90+无再入场", {"stop_q": 25, "target_q": 90,
                          "reentry_tier": False}),
    ("Q25+Q90+cooldown10", {"stop_q": 25, "target_q": 90, "cooldown": 10}),
    ("Q25+Q90+min_hold5", {"stop_q": 25, "target_q": 90, "min_hold": 5}),
    ("Q25+Q90+min_hold10", {"stop_q": 25, "target_q": 90, "min_hold": 10}),
    ("Q25+Q90+exit_p0.55", {"stop_q": 25, "target_q": 90, "exit_p": 0.55}),
    ("Q25+Q90+exit_p0.40", {"stop_q": 25, "target_q": 90, "exit_p": 0.40}),
    ("Q25+Q90+无p_up+cooldown10", {"stop_q": 25, "target_q": 90,
                                    "use_logistic": False, "cooldown": 10}),
    ("Q25+Q90+无p_up+min_hold5", {"stop_q": 25, "target_q": 90,
                                   "use_logistic": False, "min_hold": 5}),
    ("Q25+Q90+无p_up+无再入场", {"stop_q": 25, "target_q": 90,
                                  "use_logistic": False, "reentry_tier": False}),
    ("Q10+Q90", {"stop_q": 10, "target_q": 90}),
    ("Q25+Q75+无p_up", {"stop_q": 25, "target_q": 75,
                        "use_logistic": False}),
    ("hybrid+Q25+Q90", {"exit_mode": "hybrid", "stop_q": 25, "target_q": 90}),
]

# 第四批：regime 条件化止损（只收紧止损/退出，不改仓位）
# weak_mkt 阈值单位＝近5日日均收益（-0.4% ≈ 5日累计 -2%）
REGIME_VARIANTS = [
    ("Q25 mkt<-0.2%", {"weak_q": 25, "weak_mkt": -0.002}),
    ("Q25 mkt<-0.4%", {"weak_q": 25, "weak_mkt": -0.004}),
    ("Q25 mkt<-0.6%", {"weak_q": 25, "weak_mkt": -0.006}),
    ("Q25 mkt<-1.0%", {"weak_q": 25, "weak_mkt": -0.010}),
    ("Q25 mkt<-2.0%", {"weak_q": 25, "weak_mkt": -0.020}),
    ("Q25 mkt<0", {"weak_q": 25, "weak_mkt": 0.0}),
    ("Q50 mkt<-0.4%", {"weak_q": 50, "weak_mkt": -0.004}),
    ("Q50 mkt<-0.6%", {"weak_q": 50, "weak_mkt": -0.006}),
    ("Q50 mkt<-1.0%", {"weak_q": 50, "weak_mkt": -0.010}),
    ("Q25 mkt<-0.6%+exitP.55", {"weak_q": 25, "weak_mkt": -0.006,
                                "weak_exit_p": 0.55}),
    ("Q25 mkt<-1.0%+exitP.55", {"weak_q": 25, "weak_mkt": -0.010,
                                "weak_exit_p": 0.55}),
    ("Q25 disp>=0.7", {"weak_q": 25, "weak_disp": 0.7}),
]

FIELDS = ("ann", "mdd", "calmar", "sharpe", "winrate", "pf", "trades",
          "avg_hold", "days", "forced_closes")


def _half_ann(m):
    """把权益曲线对半切，返回前/后半段年化（稳定性参考）。"""
    eq, ds = m.get("equity"), m.get("dates")
    if not eq or not ds or len(eq) < 40:
        return None, None
    h = len(eq) // 2
    a1 = (eq[h] / eq[0]) ** (250.0 / h) - 1 if eq[0] > 0 else None
    n2 = len(eq) - 1 - h
    a2 = (eq[-1] / eq[h]) ** (250.0 / n2) - 1 if eq[h] > 0 and n2 > 0 else None
    return a1, a2


def run_variants(mats_bt, tier_name, variants):
    tier = sg._V4_TIERS[tier_name]
    out = {}
    for name, ov in variants:
        rules = {"mode": "full", "tier_name": tier_name}
        rules.update(sg._V4_TIER_EXTRA.get(tier_name, {}))   # 生产档位默认
        rules.update(ov)
        t0 = time.time()
        m = sg._v4_portfolio_sim(mats_bt, tier, rules)
        out[name] = {k: m.get(k) for k in FIELDS}
        out[name]["ann_1h"], out[name]["ann_2h"] = _half_ann(m)
        out[name]["rules"] = ov
        out[name]["secs"] = round(time.time() - t0, 1)
        h1 = f"{out[name]['ann_1h']*100:+.0f}%" if out[name]['ann_1h'] is not None else "-"
        h2 = f"{out[name]['ann_2h']*100:+.0f}%" if out[name]['ann_2h'] is not None else "-"
        print(f"  {name:24s} ann={m['ann']*100:+7.2f}% mdd={m['mdd']*100:+7.2f}% "
              f"cal={m['calmar']:+.2f} shp={m['sharpe']:+.2f} "
              f"win={m['winrate']*100:.1f}% tr={m['trades']:4d} "
              f"1h/2h={h1}/{h2}")
    return out


def print_table(tier_name, res):
    rows = sorted(res.items(), key=lambda kv: -kv[1]["ann"])
    print(f"\n=== Exit Ablation · v4 {tier_name}档（按年化排序）===")
    print(f"{'变体':<24}{'年化':>9}{'回撤':>9}{'Calmar':>8}{'Sharpe':>8}"
          f"{'胜率':>7}{'PF':>6}{'交易':>6}{'持仓':>6}{'前半':>7}{'后半':>7}")
    for name, m in rows:
        h1 = f"{m['ann_1h']*100:+.0f}%" if m.get('ann_1h') is not None else "-"
        h2 = f"{m['ann_2h']*100:+.0f}%" if m.get('ann_2h') is not None else "-"
        print(f"{name:<24}{m['ann']*100:>+8.2f}%{m['mdd']*100:>+8.2f}%"
              f"{m['calmar']:>+8.2f}{m['sharpe']:>+8.2f}"
              f"{m['winrate']*100:>6.1f}%{m['pf']:>6.2f}"
              f"{m['trades']:>6d}{m['avg_hold']:>6.1f}{h1:>7}{h2:>7}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="激进", choices=("保守", "平衡", "激进"))
    ap.add_argument("--tag", default="")
    ap.add_argument("--set", default="all",
                    choices=("single", "combo", "combo2", "regime", "all"))
    args = ap.parse_args()
    variants = []
    if args.set in ("single", "all"):
        variants += EXIT_VARIANTS
    if args.set in ("combo", "all"):
        variants += COMBO_VARIANTS
    if args.set in ("combo2", "all"):
        variants += COMBO2_VARIANTS
    if args.set in ("regime", "all"):
        variants += REGIME_VARIANTS
    t0 = time.time()
    print("加载 v4 预测缓存并重建矩阵 ...")
    _mats, mats_bt, n_bt, n_all = load_mats()
    print(f"对齐 {n_bt}/{n_all} 只，开始 {len(variants)} 个退出变体 ...")
    res = run_variants(mats_bt, args.tier, variants)
    print_table(args.tier, res)

    suffix = f"_{args.tag}" if args.tag else ""
    path = os.path.join("research", f"v4_exit_ablation{suffix}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"tier": args.tier, "ts": time.strftime("%Y-%m-%d %H:%M"),
                   "n_bt": n_bt, "n_all": n_all, "results": res},
                  f, ensure_ascii=False, indent=1)
    print(f"\n写入 {path}，耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
