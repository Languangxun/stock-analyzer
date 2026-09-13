#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_l2type.py - L2 分组方式消融：精确行业(现状) vs 行业类型(粗分类)

行业类型关键词映射（东财行业名 → 粗分类）：
  金融/医药/TMT科技/消费/周期资源/装备制造/交通运输/环保公用/地产
对照配置：
  L1 / L1+L2精确行业(40) / L1+L2类型(60) / L1+L2类型(100)
协议与 backtest_levels 完全一致（walk-forward，无前视）。
"""
import math
import time

from backtest_levels import (load_all, precompute, match_pool, match_l1,
                             fuse, rank_ic, STEP, TAIL, TOPK, W)
import stock_gui as sg
from stock_gui import _is_etf

TYPE_KW = [
    ("金融", ("证券", "银行", "保险", "多元金融")),
    ("医药", ("医药", "中药", "生物", "医疗", "制药", "疫苗", "兽药")),
    ("TMT科技", ("软件", "计算机", "半导体", "元件", "电子", "通信",
              "光电", "IT", "互联网", "游戏", "传媒", "数字", "消费电子",
              "光学")),
    ("消费", ("食品", "饮料", "酿酒", "白酒", "家电", "纺织", "服装",
            "零售", "旅游", "酒店", "餐饮", "美容", "珠宝", "农业",
            "种植", "养殖", "渔业", "乳品", "调味", "家居", "文娱")),
    ("周期资源", ("钢铁", "有色", "金属", "煤炭", "化工", "化纤", "塑料",
               "橡胶", "水泥", "建材", "玻璃", "造纸", "石油", "燃气",
               "电力", "能源", "采矿", "采掘")),
    ("装备制造", ("机械", "设备", "仪器", "仪表", "汽车", "军工", "船舶",
               "电机", "工程", "模具", "自动化", "轨交", "电网")),
    ("交通运输", ("运输", "港口", "航运", "公路", "铁路", "航空", "物流",
               "快递")),
    ("环保公用", ("环保", "水务", "园林", "公用")),
    ("地产基建", ("房地产", "地产", "园区", "基建", "装饰", "厨卫")),
]


def type_of(ind):
    ind = ind or ""
    for name, kws in TYPE_KW:
        if any(k in ind for k in kws):
            return name
    return "其他"


def main():
    t0 = time.time()
    by, meta = load_all()
    cands = [(c, r) for c, r in by.items() if len(r) >= 400 and not _is_etf(c)
             and c in meta]
    cands.sort(key=lambda cr: -len(cr[1]))
    pool200 = cands[:200]
    # 目标：18只，尽量覆盖不同行业类型
    seen_types = {}
    targets = []
    for c, r in cands:
        ty = type_of(meta[c][2])
        if seen_types.get(ty, 0) < 3:
            targets.append(c)
            seen_types[ty] = seen_types.get(ty, 0) + 1
        if len(targets) >= 18:
            break
    print(f"目标 {len(targets)} 只（类型分布：{seen_types}），池候选 {len(pool200)}")

    feats = {}
    def F(c):
        if c not in feats:
            feats[c] = precompute(by[c])
        return feats[c]
    for c, _ in pool200:
        F(c)
    for c in targets:
        F(c)
    print(f"预计算 {len(feats)} 只 {time.time()-t0:.0f}s\n")

    combos = {"L1": [], "L1+L2精确行业(40)": [],
              "L1+L2类型(60)": [], "L1+L2类型(100)": []}
    stat = {"L2ex": [0, 0], "L2t60": [0, 0], "L2t100": [0, 0]}
    n_eval = 0
    for code in targets:
        m = meta[code]
        _, _, ind, cap, tier = m
        fp = F(code)
        lg = math.log(max(cap or 1e8, 1e8))
        my_type = type_of(ind)
        # 精确行业池（市值接近优先）
        peers_ex = [c for c, _ in pool200 if c != code and meta[c][2] == ind]
        peers_ex.sort(key=lambda c: abs(math.log(
            max(meta[c][3] or 1e8, 1e8)) - lg))
        peers_ex = peers_ex[:40]
        # 类型池（含自身行业，市值接近优先）
        peers_ty = [c for c, _ in pool200 if c != code
                    and type_of(meta[c][2]) == my_type]
        peers_ty.sort(key=lambda c: abs(math.log(
            max(meta[c][3] or 1e8, 1e8)) - lg))
        n = len(fp["closes"])
        for t in range(max(2 * W + 2, n - TAIL), n - 1, STEP):
            d = fp["dates"][t]
            cur_win = fp["zwin"][t]
            if cur_win is None:
                continue
            cur_ctx = {"struct": fp["struct"][t], "vola": fp["vola"][t],
                       "rsi": fp["rsi"][t], "volchg": fp["volchg"][t],
                       "weekly": fp["weekly"][t]}
            vr_now = fp["vr"][t]
            s1 = match_l1(fp, t, cur_win, cur_ctx, vr_now)
            s_ex = []
            for pc in peers_ex:
                s_ex.extend(match_pool(F(pc), d, cur_win, cur_ctx, vr_now))
            s_ex.sort(key=lambda s: s["similarity_score"]); s_ex = s_ex[:TOPK]
            s_t60, s_t100 = [], []
            for pc in peers_ty[:100]:
                sm = match_pool(F(pc), d, cur_win, cur_ctx, vr_now)
                if pc in peers_ty[:60]:
                    s_t60.extend(sm)
                s_t100.extend(sm)
            s_t60.sort(key=lambda s: s["similarity_score"]); s_t60 = s_t60[:TOPK]
            s_t100.sort(key=lambda s: s["similarity_score"]); s_t100 = s_t100[:TOPK]
            actual = fp["closes"][t + 1] / fp["closes"][t] - 1
            stat["L2ex"][0] += len(s_ex); stat["L2ex"][1] += bool(s_ex)
            stat["L2t60"][0] += len(s_t60); stat["L2t60"][1] += bool(s_t60)
            stat["L2t100"][0] += len(s_t100); stat["L2t100"][1] += bool(s_t100)
            n_eval += 1
            combos["L1"].append((fuse([("L1", s1)]), actual))
            combos["L1+L2精确行业(40)"].append(
                (fuse([("L1", s1), ("L2", s_ex)]), actual))
            combos["L1+L2类型(60)"].append(
                (fuse([("L1", s1), ("L2", s_t60)]), actual))
            combos["L1+L2类型(100)"].append(
                (fuse([("L1", s1), ("L2", s_t100)]), actual))

    print(f"评估点 {n_eval} | 平均样本: L2精确 {stat['L2ex'][0]/max(1,stat['L2ex'][1]):.1f}"
          f"(覆盖{stat['L2ex'][1]}/{n_eval})  类型60 {stat['L2t60'][0]/max(1,stat['L2t60'][1]):.1f}"
          f"(覆盖{stat['L2t60'][1]}/{n_eval})  类型100 {stat['L2t100'][0]/max(1,stat['L2t100'][1]):.1f}"
          f"(覆盖{stat['L2t100'][1]}/{n_eval})\n")
    print(f"{'配置':<20}{'n':>5}{'方向命中':>9}{'IC':>9}{'MAE':>9}")
    import json
    out = {}
    for name, arr in combos.items():
        pr = [(p, a) for p, a in arr if p is not None]
        if len(pr) < 20:
            print(f"{name:<20} 样本不足({len(pr)})")
            continue
        preds = [p for p, _ in pr]; acts = [a for _, a in pr]
        hit = sum(1 for p, a in zip(preds, acts)
                  if (p > 0) == (a > 0)) / len(preds)
        ic = rank_ic(preds, acts)
        mae = sum(abs(p - a) for p, a in zip(preds, acts)) / len(preds)
        out[name] = {"n": len(preds), "dir_hit": hit, "ic": ic, "mae": mae}
        print(f"{name:<20}{len(preds):>5}{hit*100:>8.1f}%{ic:>+9.4f}{mae*100:>8.3f}%")
    with open("l2type_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → l2type_results.json")


if __name__ == "__main__":
    main()
