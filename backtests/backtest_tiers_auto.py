#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auto 三档切换回测（v7.3.1）：大盘 BOLL 带内位置三等分 → 稳健/均衡/激进。

口径（与 stock_gui.auto_boll_zone 单股 auto 风险偏好同一套三等分规则）：
  - 每交易日收盘后用**上证** BOLL(CFG.AUTO_RISK_WINDOW=20, NBDEV=2) 位置
    pos=(收盘−下轨)/带宽（恒只用 T 日及以前的收盘序列，无前视），三等分：
      pos<1/3 → 保守（下 1/3，弱市贴下轨）→ 组合映射「稳健档」（reb20 长持有）
      pos<2/3 → 稳健（中 1/3）          → 组合映射「均衡档」（reb10 中期）
      pos≥2/3 → 激进（上 1/3，追势）    → 组合映射「激进档」（板块轮动 Top5）
  - 组合引擎完全复用 irm tier_sim_phase 同款交易约束：滑点 0.1%/边、
    佣金万 2.5（**最低 5 元**）、印花税卖出 1‰、过户费沪市十万 1、
    100 股整手、涨停不买（激进档允许打板）、跌停不卖、停牌顺延；
    档位的闸门（稳健/均衡上证 MA20、激进板块轮动闸门）与 mdd_guard=0.08
    熔断沿用各自档位配置；档位切换日与调仓日立即换仓（按该档重出 TopN）。
  - 固定三档基线 = 同引擎、只是把风险档恒定为某档（跨档数字与 tier_eval
    相位平均口径不可直接互比——本脚本不相位平均，这是单相位口径）。
  - 默认 IC 确认层关闭（--ic 可开，候选须过 tier_ic_confirm）。

用法：
  python backtests/backtest_tiers_auto.py                 # full 段，全A
  python backtests/backtest_tiers_auto.py --seg val --universe main
  python backtests/backtest_tiers_auto.py --seg full --ic
产物：research/tiers_auto.json/.md（根目录最新副本规范见 bt_common.py）。
"""
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import stock_gui as sg  # noqa: E402

TIERS = ("稳健", "均衡", "激进")
ZONE_LABEL = ("下1/3(保守)", "中1/3(稳健)", "上1/3(激进)")


def index_boll_pos(window=20, nbdev=2.0):
    """上证收盘 BOLL 带内位置 {date: pos∈[0,1]}（滚动窗口只用 T 日及以前）。"""
    with sg.db_conn() as conn:
        rows = conn.execute(
            "SELECT date, close FROM daily_bars WHERE code='sh000001' "
            "ORDER BY date").fetchall()
    dates = [r[0] for r in rows]
    cl = np.array([float(r[1]) for r in rows])
    ma = np.full(len(cl), np.nan)
    sd = np.full(len(cl), np.nan)
    ma[window - 1:] = np.convolve(cl, np.ones(window) / window, "valid")
    # 滚动标准差（ddof=0）：滑动窗口方差 cumsum
    c1 = np.cumsum(cl)
    c2 = np.cumsum(cl ** 2)
    n = np.arange(len(cl)) + 1
    cs1 = np.empty(len(cl)); cs1[:window] = c1[:window]
    cs2 = np.empty(len(cl)); cs2[:window] = c2[:window]
    cs1[window:] = c1[window:] - c1[:-window]
    cs2[window:] = c2[window:] - c2[:-window]
    var = np.maximum(cs2 / window - (cs1 / window) ** 2, 0.0)
    sd[window - 1:] = np.sqrt(var[window - 1:])
    lower = ma - nbdev * sd
    span = 2 * nbdev * sd
    pos = np.full(len(cl), 0.5)
    ok = np.isfinite(span) & (span > 1e-9)
    pos[ok] = np.clip((cl[ok] - lower[ok]) / span[ok], 0.0, 1.0)
    out = {}
    for d, p in zip(dates, pos):
        out[d] = float(p) if np.isfinite(p) else None
    return out


def zone_of(pos, mode="fixed_0"):
    """pos → 风险区 0/1/2（fixed_* 供固定档基线：恒定在某个风险档）。"""
    if isinstance(mode, str) and mode.startswith("fixed_"):
        return int(mode[6:])
    if pos is None:
        return 1
    return 0 if pos < 1.0 / 3 else (1 if pos < 2.0 / 3 else 2)


def sim_composite(mode, i0, i1, ctx):
    """auto/固定档组合模拟（档位动态切换版 tier_sim_phase，费率/约束不变）。

    mode: "auto"（每日按 BOLL 位置解析）或 "fixed_0/1/2"（固定风险档基线）。"""
    codes, cal, C, V = ctx["codes"], ctx["cal"], ctx["C"], ctx["V"]
    feat = ctx["feat"]
    tier_cfg = ctx["tier_cfg"]
    scores = ctx["scores"]
    gates = ctx["gates"]
    ic_ok = ctx["ic_ok"]
    NST = C.shape[0]
    uni = {t: sg.tier_universe_mask(codes, tier_cfg[t].get("universe", "all"))
           for t in TIERS}
    lim = np.array([sg._tier_limit_pct(c) for c in codes])[:, None]
    r1 = np.full_like(C, np.nan)
    r1[:, 1:] = C[:, 1:] / C[:, :-1] - 1.0
    limit_up = r1 >= lim - 0.005
    limit_dn = r1 <= -(lim - 0.005)
    eligible = {}
    for t in TIERS:
        # 与 tier_sim_phase 同口径的可交易池（价格/根数/成交额/池掩码）
        eligible[t] = (np.isfinite(C) & (C > sg._TIER_MIN_PRICE)
                       & (feat["barcount"] >= sg._TIER_MIN_BARS)
                       & (feat["amt20"] >= sg._TIER_MIN_AMOUNT)
                       & uni[t][:, None])

    capital = 1e6
    cash = float(capital)
    shares = np.zeros(NST)
    entry = np.zeros(NST)
    t_in = np.zeros(NST, int)
    last = np.full(NST, np.nan)
    miss = np.zeros(NST, int)
    holding = np.zeros(NST, bool)
    target = set()
    peak = float(capital)
    eq, eq_cal, zone_seq, switch_cnt = [], [], [], 0
    prev_zone = None
    fee_total = 0.0
    n_buy = n_sell = 0

    def sell_fee(amount):
        return (max(amount * sg._TIER_COMMISSION, sg._TIER_MIN_COMMISSION)
                + amount * sg._TIER_STAMP + amount * sg._TIER_TRANSFER)

    def equity():
        return cash + float(np.nansum(np.where(
            holding, shares * np.where(np.isnan(last), entry, last), 0.0)))

    def close_pos(k, t, px, reason):
        nonlocal cash, n_sell, fee_total
        amount = shares[k] * px
        fee = sell_fee(amount)
        fee_total += fee
        cash += amount - fee
        holding[k] = False
        shares[k] = 0.0
        target.discard(int(k))
        n_sell += 1

    for t in range(i0, i1):
        col = C[:, t]
        fin = np.isfinite(col)
        last[fin] = col[fin]
        miss = np.where(holding & ~fin, miss + 1, 0)
        for k in np.nonzero(holding & (miss >= sg._TIER_STALE_DAYS))[0]:
            close_pos(k, t, last[k] if last[k] > 0 else entry[k], "delist")

        zone = zone_of(index_pos.get(cal[t]) if mode == "auto" else None,
                       mode)
        tier = TIERS[zone]
        cfg = tier_cfg[tier]
        gate = gates[tier]
        on = bool(gate[t]) if gate is not None else True
        if prev_zone is not None and zone != prev_zone:
            switch_cnt += 1          # 档位切换日立即换仓
        reb_now = ((t - i0) % max(1, cfg["reb"]) == 0) \
            or (prev_zone is not None and zone != prev_zone)
        prev_zone = zone

        if reb_now:
            d = t - 1
            cand = np.nonzero(eligible[tier][:, d])[0]
            s = scores[tier][cand, d]
            m = np.isfinite(s)
            cand, s = cand[m], s[m]
            if ic_ok is not None:
                keep = ic_ok[cand, d]
                cand, s = cand[keep], s[keep]
            order = cand[np.argsort(-s, kind="stable")]
            top, buf = cfg["top"], int(cfg.get("hold_buffer", 0) or 0)
            if on and buf > 0:
                rank_of = {int(k): r for r, k in enumerate(order)}
                INF = 1 << 30
                keep = {int(k) for k in np.nonzero(holding)[0]
                        if rank_of.get(int(k), INF) < top + buf}
                room = max(0, top - len(keep))
                add = [int(k) for k in order if int(k) not in keep][:room]
                target = keep | set(add)
            else:
                target = set(order[:top].tolist()) if on else set()

        for k in np.nonzero(holding)[0]:
            if int(k) in target or not fin[k] or limit_dn[k, t]:
                continue
            close_pos(k, t, col[k] * (1 - sg._TIER_SLIP),
                      "switch" if (prev_zone and reb_now and not on) else "target")

        if reb_now and on:
            eqv = equity()
            for k in order:
                if int(holding.sum()) >= cfg["top"]:
                    break
                if holding[k] or not fin[k] or \
                        (limit_up[k, t] and not cfg.get("allow_limit_up")):
                    continue
                px = col[k] * (1 + sg._TIER_SLIP)
                budget = min(eqv / cfg["top"], cash)
                n_lot = int(budget / (px * sg._TIER_LOT))
                if n_lot <= 0:
                    continue
                amount = n_lot * sg._TIER_LOT * px
                fee = max(amount * sg._TIER_COMMISSION, sg._TIER_MIN_COMMISSION) \
                    + amount * sg._TIER_TRANSFER
                if amount + fee > cash:
                    continue
                cash -= amount + fee
                fee_total += fee
                shares[k] = n_lot * sg._TIER_LOT
                entry[k] = (amount + fee) / shares[k]
                t_in[k] = t
                holding[k] = True
                target.add(int(k))
                n_buy += 1

        eq_v = cash + float(np.nansum(np.where(holding, shares * last, 0.0)))
        eq.append(eq_v)
        eq_cal.append(cal[t])
        zone_seq.append(zone)
        if eq_v > peak:
            peak = eq_v
        # mdd_guard：跟随**当前档**配置（稳健档才有），碰撞即全清 + 峰值重置
        guard = cfg.get("mdd_guard")
        if guard and peak > 0 and (peak - eq_v) / peak > guard and holding.any():
            for k in np.nonzero(holding)[0]:
                if not fin[k] or limit_dn[k, t]:
                    continue
                close_pos(k, t, col[k] * (1 - sg._TIER_SLIP), "mdd_guard")
            target = set()
            peak = eq_v

    import datetime
    if len(eq) < 2:
        return None
    d0 = datetime.date.fromisoformat(eq_cal[0])
    d1 = datetime.date.fromisoformat(eq_cal[-1])
    years = max((d1 - d0).days / 365.25, 0.25)
    res_total = float(eq[-1]) / capital
    return {
        "total": res_total - 1.0,
        "ann": res_total ** (1 / years) - 1.0 if res_total > 0 else -1.0,
        "mdd": _curve_mdd(eq),
        "years": years, "sells": n_sell, "buys": n_buy,
        "fee_total": fee_total,
        "switches": switch_cnt,
        "fraction_zones": [round(sum(1 for z in zone_seq if z == i) / len(zone_seq), 4)
                           for i in range(3)],
        "curve_dates": eq_cal[:: max(1, len(eq_cal) // 300)],
        "curve": [round(float(v) / capital, 6)
                  for v in eq[:: max(1, len(eq_cal) // 300)]],
    }


def _curve_mdd(eq):
    peak = 0.0
    mdd = 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seg", default="full",
                    help="回测段：full(默认)/val/bull/val2025/train")
    ap.add_argument("--universe", default="all",
                    choices=["all", "main", "etf", "all_etf"])
    ap.add_argument("--ic", action="store_true", help="启用 IC 确认层")
    ap.add_argument("--zones", default="auto,fixed_0,fixed_1,fixed_2")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--nbdev", type=float, default=2.0)
    args = ap.parse_args()

    print("加载面板 ...", flush=True)
    codes, cal, C, V = sg.tier_load_panel()
    print(f"面板 {len(codes)} 只 × {len(cal)} 根，构建特征 ...", flush=True)
    feat = sg.tier_build_features(cal, C, V)
    tier_cfg = {t: sg.tier_cfg(t, universe=args.universe) for t in TIERS}
    base = sg.tier_rank_base(codes, args.universe)
    print("构建分数/闸门 ...", flush=True)
    scores = {t: sg.tier_make_score(feat, tier_cfg[t]["score"],
                                    tier_cfg[t].get("mom_w"), base=base,
                                    sec_w=tier_cfg[t].get("sec_w"))
              for t in TIERS}
    gates = {t: sg.tier_build_gate(cal, tier_cfg[t], feat) for t in TIERS}
    ic_ok = None
    if args.ic:
        icp = sg.tier_ic_confirm(C)
        ic_ok = icp["ok"]

    segs = sg.tier_segments(cal)
    seg = segs[args.seg]
    d0, d1 = seg
    date_pos = {d: i for i, d in enumerate(cal)}
    i0 = date_pos.get(d0)
    i1 = (date_pos.get(d1, len(cal) - 1)) + 1
    if i0 is None or i1 is None or i1 - i0 < 60:
        raise SystemExit(f"段 {args.seg} 有效区间不足: {seg}")

    ctx = {"codes": codes, "cal": cal, "C": C, "V": V, "feat": feat,
           "tier_cfg": tier_cfg, "scores": scores, "gates": gates,
           "ic_ok": ic_ok}
    global index_pos
    index_pos = index_boll_pos(args.window, args.nbdev)

    out = {"version": sg.APP_VERSION, "generated": sg.time.strftime(
        "%Y-%m-%d %H:%M"), "seg": args.seg, "universe": args.universe,
        "window": args.window, "nbdev": args.nbdev, "ic": bool(args.ic),
        "modes": {}}
    md = ["# auto 三档切换回测（BOLL 三等分，v{}，{}）".format(
        sg.APP_VERSION, out["generated"]), "",
          f"段={args.seg}({d0}~{d1}，{i1 - i0}根) · 池={args.universe} · "
          f"BOLL({args.window},{args.nbdev:g}) 三等分：下1/3→稳健(保守)、"
          f"中1/3→均衡、上1/3→激进；档位切换日立即换仓。",
          "费用：滑点0.1%/边、佣金万2.5（**最低5元**）、印花卖出1‰、"
          "过户费沪十万1；整手/涨跌停/停牌/闸门/缓冲带/mdd_guard=8%（稳健档）"
          f" full 口径；IC确认层={'开' if args.ic else '关'}。",
          "单相位口径（不相位平均），跨 mode 基线可比。", "",
          "| 风险档切换 | 总收益 | 年化 | 最大回撤 | 买入笔 | 卖出笔 | 手续费(元) | 档位切换次数 | 区间占用 稳/均/激 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for mode in [m.strip() for m in args.zones.split(",") if m.strip()]:
        res = sim_composite(mode, i0, i1, ctx)
        if not res:
            continue
        label = _labels.get(mode, mode)
        out["modes"][mode] = res
        fq = res["fraction_zones"]
        md.append(
            f"| {label} | {res['total'] * 100:+.1f}% | "
            f"{res['ann'] * 100:+.1f}% | {res['mdd'] * 100:.1f}% | "
            f"{res['buys']} | {res['sells']} | {res['fee_total']:,.0f} | "
            f"{res['switches']} | {fq[0]:.0%}/{fq[1]:.0%}/{fq[2]:.0%} |")
        print(label, {k: float(res[k]) for k in ("total", "ann", "mdd")},
              flush=True)

    os.makedirs(os.path.join(ROOT, "research"), exist_ok=True)
    with open(os.path.join(ROOT, "research", "tiers_auto.json"),
              "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1,
                  default=lambda o: float(o))
    with open(os.path.join(ROOT, "research", "tiers_auto.md"),
              "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print("已写出 research/tiers_auto.json/.md")


index_pos = {}
_labels = {
    "auto": "**auto**（BOLL三等分动态切换）",
    "fixed_0": "恒 保守(稳健档·reb20)",
    "fixed_1": "恒 稳健(均衡档·reb10)",
    "fixed_2": "恒 激进(板块轮动·Top5)",
}

if __name__ == "__main__":
    main()
