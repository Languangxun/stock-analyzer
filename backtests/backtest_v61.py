#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_v61.py - v6.1.5 标准回测（四口径 × 三档 × 荐股）

口径：
  all      全A（含沪深主板/创业板/科创板，剔除北交所/ETF）
  main     沪深主板
  etf      仅 ETF/LOF
  all_etf  全A个股 + ETF
产品：
  稳健 / 均衡 / 激进（组合收益，相位平均）+ 荐股（逐笔收益口径）
输出（每次回测新建时间戳文件夹，报告/明细/图表全在里面）：
  research/backtest_v{版本}_{YYYYMMDD_HHMMSS}_{区间}[_tag]/
    report.json / report.md      原始指标 + 可嵌入 README 的表格
    run_meta.json                版本/时间/区间/口径/数据规模/耗时
    tables/*.csv                 组合指标/逐笔指标/相位年化/逐笔收益/基准
    charts/*.svg                 相位箱线图/逐笔箱线图/总收益柱状图等
  同时在 research/ 根保留 v61_report[_tag].json/md 副本（对比/发布链兼容）

用法：
  python backtest_v61.py                     # 四口径全期(all/main/etf/all_etf)
  python backtest_v61.py --universe main
  python backtest_v61.py --segment val
  python backtest_v61.py --charts-only --tag v6.1.5
  python backtest_v61.py --compare all
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
import csv
import json
import os
import sys
import time

import stock_gui as sg

HERE = ROOT
BT_DIR = os.path.dirname(os.path.abspath(__file__))
if BT_DIR not in sys.path:
    sys.path.insert(0, BT_DIR)
TIERS = tuple(sg.TIER_CFG)
UNI_NAME = dict(sg.UNIVERSE_NAME)          # all/main/etf/all_etf
UNIS = ("all", "main", "etf", "all_etf")   # 报告依次输出四个口径
VERSION = getattr(sg, "APP_VERSION", "6.1.5")


def _db_stats():
    """报告元数据：库内规模（版本对比时标注数据口径）。"""
    try:
        with sg.db_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
            c = conn.execute("SELECT COUNT(DISTINCT code) FROM daily_bars"
                             ).fetchone()[0]
            mx = conn.execute("SELECT MAX(date) FROM daily_bars").fetchone()[0]
        return {"bars": n, "codes": c, "max_date": mx}
    except Exception:
        return {}


def _run_universe(universe, segment, progress):
    progress(f"=== {UNI_NAME[universe]} · 组合回测 ===")
    tiers = sg.tier_eval(segment=segment, universe=universe, progress=progress)
    progress(f"=== {UNI_NAME[universe]} · 荐股逐笔回测 ===")
    picks = sg.tier_picks_stats(segment=segment, universe=universe,
                                progress=progress)
    return {"universe": universe, "tiers": tiers, "picks": picks}


def _tier_table(rep, tier):
    tv = rep["tiers"].get(tier)
    if not tv:
        return None
    b = tv.get("bench") or {}
    benches = {}
    for c, v in (tv.get("benches") or {}).items():
        if v and v.get("ann") is not None:
            benches[c] = {
                "ann": v.get("ann"),
                "excess": (tv["total"] - (v.get("total") or 0))
                if tv.get("total") is not None else None,
            }
    return {
        "range": tv["range"], "total": tv["total"], "ann": tv["ann"],
        "mdd": tv["mdd"], "sharpe": tv["sharpe"], "trades": tv["trades"],
        "winrate": tv["winrate"], "benchmark": tv["benchmark"],
        "bench_ann": b.get("ann"), "bench_mdd": b.get("mdd"),
        "benches": benches,
        "excess_total": tv.get("excess_total"),
        "phase_ann_min": tv.get("phase_ann_min"),
        "phase_ann_max": tv.get("phase_ann_max"),
        "phase_anns": tv.get("phase_anns"),      # 相位年化分布（箱线图/对比）
    }


def build_md(report):
    lab = report.get("label")
    lines = [f"### v{report.get('version', VERSION)} 标准回测（{report['segment']}，"
             f"数据截至 {report['data_end']}"
             + (f"，{lab}" if lab else "") + "）", ""]
    lines.append(f"- 生成时间：{report.get('ts', '?')}；"
                 f"版本：v{report.get('version', VERSION)}")
    lines.append("")
    for uni in UNIS:
        rep = report["results"].get(uni)
        if not rep:
            continue
        lines.append(f"#### {UNI_NAME[uni]} · 组合（相位平均，含全部费用）")
        lines.append("")
        lines.append("| 档位 | 区间 | 总收益 | 年化 | 最大回撤 | Sharpe | "
                     "交易 | 主基准 | 基准年化 | 超额(总) | 相位年化区间 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for tier in TIERS:
            t = _tier_table(rep, tier)
            if not t:
                continue
            lines.append(
                f"| {tier} | {t['range'][0]}~{t['range'][1]} | "
                f"{(t['total'] or 0)*100:+.1f}% | {(t['ann'] or 0)*100:+.1f}% | "
                f"{(t['mdd'] or 0)*100:+.1f}% | {(t['sharpe'] or 0):+.2f} | "
                f"{t['trades']} | {t['benchmark']} | "
                f"{(t['bench_ann'] or 0)*100:+.1f}% | "
                f"{(t['excess_total'] or 0)*100:+.1f}pp | "
                f"{(t['phase_ann_min'] or 0)*100:+.1f}% ~ "
                f"{(t['phase_ann_max'] or 0)*100:+.1f}% |")
        lines.append("")
        # 多基准对照（激进档主基准为科创50，另附创业板指/上证）
        lines.append(f"#### {UNI_NAME[uni]} · 三基准对照（年化 / 超额pp）")
        lines.append("")
        lines.append("| 档位 | 策略年化 | 科创50 | 超额 | 创业板指 | 超额 | "
                     "上证指数 | 超额 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for tier in TIERS:
            t = _tier_table(rep, tier)
            if not t:
                continue
            bs = t.get("benches") or {}
            def cell(code):
                v = bs.get(code)
                if not v or v.get("ann") is None:
                    return "-", "-"
                return (f"{(v['ann'] or 0)*100:+.1f}%",
                        f"{(v['excess'] or 0)*100:+.1f}pp")
            a1, e1 = cell("sh000688")
            a2, e2 = cell("sz399006")
            a3, e3 = cell("sh000001")
            lines.append(
                f"| {tier} | {(t['ann'] or 0)*100:+.1f}% | {a1} | {e1} | "
                f"{a2} | {e2} | {a3} | {e3} |")
        lines.append("")
        lines.append(f"#### {UNI_NAME[uni]} · 荐股（逐笔口径）")
        lines.append("")
        lines.append("| 档位 | 推荐笔数 | 平均收益 | 中位 | 胜率 | 盈亏比 | "
                     "PF | 均持有(日) | 最好 | 最差 | >50%右尾 | 退出原因 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for tier in TIERS:
            s = rep["picks"].get(tier)
            if not s or not s.get("n"):
                lines.append(f"| {tier} | 0 | - | - | - | - | - | - | - | - | - | - |")
                continue
            r = s.get("by_reason") or {}
            lines.append(
                f"| {tier} | {s['n']} | {s['avg_ret']*100:+.2f}% | "
                f"{s['med_ret']*100:+.2f}% | {s['winrate']*100:.1f}% | "
                f"{(s['payoff'] or 0):.2f} | {(s['pf'] or 0):.2f} | "
                f"{s['avg_hold']:.1f} | {s['best']*100:+.1f}% | "
                f"{s['worst']*100:+.1f}% | {s['tail50']*100:.1f}% | "
                f"调仓{r.get('target', 0)}/闸门{r.get('gate', 0)}/"
                f"退市{r.get('delist', 0)} |")
        lines.append("")
    lines.append("- 口径：T-1 信号 → T 日收盘成交；含滑点/佣金/印花税/整手/"
                 "涨跌停/退市了结。")
    lines.append("- **四口径（v6.1.2）**：`all`=全A个股（不含 ETF，历史口径不变）/ "
                 "`main`=沪深主板 / `etf`=仅 ETF/LOF / `all_etf`=全A个股+ETF。"
                 "各口径**在自己的基数池内做横截面排名**，互不污染。")
    lines.append("- ETF 池：东财 ETF/LOF 代码表（1491 只，剔除货币/现金类），"
                 "回填历史后 1202 只有 K 线、1145 只 ≥250 根；"
                 "ETF 三档用 blend/blend_mom + 上证 MA20 闸门（ETF 无创业板语义）。")
    lines.append("- 基准：稳健/均衡 = 上证指数；**激进档统一对标科创50**"
                 "（不分是否具备科创板权限），另附创业板指/上证对照，"
                 "避免单一强基准使超额恒负。")
    lines.append("- 主板激进档（v6.1.1）：改用 blend_mom（动量0.7/低波0.3）"
                 "高换手配置（reb10/top20/上证MA20）——原 β 口径经诊断证伪。")
    lines.append("- 注：全部为历史统计研究，不构成投资建议。")
    return "\n".join(lines)


def _load_report(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _r6(v):
    """浮点保留6位；None/缺失原样返回（CSV 写成空）。"""
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return None


def _write_tables(report, tdir):
    """把报告摊平成 CSV（箱线图/表格的数据源），返回写出文件列表。"""
    os.makedirs(tdir, exist_ok=True)
    made = []
    unis = list(report.get("results") or {})

    # 1) 组合指标（各口径 × 三档）
    p = os.path.join(tdir, "tier_metrics.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["universe", "tier", "start", "end", "total", "ann",
                    "mdd", "sharpe", "trades", "winrate", "benchmark",
                    "bench_ann", "bench_mdd", "excess_total",
                    "phase_ann_min", "phase_ann_max"])
        for u in unis:
            rep = report["results"][u]
            for t in TIERS:
                tv = (rep.get("tiers") or {}).get(t)
                if not tv:
                    continue
                b = tv.get("bench") or {}
                w.writerow([u, t, (tv.get("range") or ["", ""])[0],
                            (tv.get("range") or ["", ""])[1],
                            _r6(tv.get("total")), _r6(tv.get("ann")),
                            _r6(tv.get("mdd")), _r6(tv.get("sharpe")),
                            tv.get("trades"), _r6(tv.get("winrate")),
                            tv.get("benchmark"), _r6(b.get("ann")),
                            _r6(b.get("mdd")), _r6(tv.get("excess_total")),
                            _r6(tv.get("phase_ann_min")),
                            _r6(tv.get("phase_ann_max"))])
    made.append(p)

    # 2) 逐笔荐股指标
    p = os.path.join(tdir, "picks_metrics.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["universe", "tier", "n", "avg_ret", "med_ret",
                    "winrate", "avg_win", "avg_loss", "payoff", "pf",
                    "avg_hold", "med_hold", "best", "worst", "tail20",
                    "tail50", "exit_target", "exit_gate", "exit_delist"])
        for u in unis:
            picks = (report["results"][u].get("picks") or {})
            for t in TIERS:
                s = picks.get(t)
                if not s or not s.get("n"):
                    w.writerow([u, t, 0])
                    continue
                r = s.get("by_reason") or {}
                w.writerow([u, t, s.get("n"), _r6(s.get("avg_ret")),
                            _r6(s.get("med_ret")), _r6(s.get("winrate")),
                            _r6(s.get("avg_win")), _r6(s.get("avg_loss")),
                            _r6(s.get("payoff")), _r6(s.get("pf")),
                            _r6(s.get("avg_hold")), _r6(s.get("med_hold")),
                            _r6(s.get("best")), _r6(s.get("worst")),
                            _r6(s.get("tail20")), _r6(s.get("tail50")),
                            r.get("target", 0), r.get("gate", 0),
                            r.get("delist", 0)])
    made.append(p)

    # 3) 相位年化分布（相位箱线图数据）
    p = os.path.join(tdir, "phase_anns.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["universe", "tier", "phase", "ann"])
        for u in unis:
            tiers = (report["results"][u].get("tiers") or {})
            for t in TIERS:
                for i, a in enumerate((tiers.get(t) or {}).get("phase_anns")
                                      or []):
                    w.writerow([u, t, i, _r6(a)])
    made.append(p)

    # 4) 逐笔收益分布（逐笔箱线图数据，抽样≤1500/档）
    p = os.path.join(tdir, "picks_returns.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["universe", "tier", "ret"])
        for u in unis:
            picks = (report["results"][u].get("picks") or {})
            for t in TIERS:
                for x in ((picks.get(t) or {}).get("rets") or []):
                    w.writerow([u, t, _r6(x)])
    made.append(p)

    # 5) 三基准对照
    p = os.path.join(tdir, "benchmarks.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["universe", "tier", "bench_code", "bench_total",
                    "bench_ann", "bench_mdd", "excess_total"])
        for u in unis:
            tiers = (report["results"][u].get("tiers") or {})
            for t in TIERS:
                tv = tiers.get(t) or {}
                for code, v in (tv.get("benches") or {}).items():
                    if not v:
                        continue
                    w.writerow([u, t, code, _r6(v.get("total")),
                                _r6(v.get("ann")), _r6(v.get("mdd")),
                                None if tv.get("total") is None else
                                _r6(tv.get("total") - (v.get("total") or 0))])
    made.append(p)

    # 6) 相位平均净值曲线（组合 + 主基准）
    p = os.path.join(tdir, "equity_curves.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["universe", "tier", "date", "net_value",
                    "bench_net_value"])
        for u in unis:
            tiers = (report["results"][u].get("tiers") or {})
            for t in TIERS:
                tv = tiers.get(t) or {}
                bmap = dict(zip(tv.get("bench_curve_dates") or [],
                                tv.get("bench_curve") or []))
                for d, v in zip(tv.get("curve_dates") or [],
                                tv.get("curve") or []):
                    w.writerow([u, t, d, _r6(v), _r6(bmap.get(d))])
    made.append(p)
    return made


def _write_meta(report, run_dir, argv, elapsed):
    """运行元数据：版本/时间/区间/口径/数据规模/耗时。"""
    meta = {
        "version": VERSION,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "segment": report.get("segment"),
        "label": report.get("label"),
        "universes": list(report.get("results") or {}),
        "data_end": report.get("data_end"),
        "db_stats": report.get("db_stats"),
        "elapsed_s": round(elapsed, 1),
        "argv": list(argv),
        "python": sys.version.split()[0],
    }
    path = os.path.join(run_dir, "run_meta.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="all4",
                    choices=["all4", "both", "all", "main", "etf", "all_etf"])
    ap.add_argument("--segment", default="full",
                    choices=["full", "train", "val", "val2025", "bull"])
    ap.add_argument("--out", default=os.path.join(HERE, "research"),
                    help="research 根目录（时间戳回测文件夹建在它下面）")
    ap.add_argument("--run-dir", default="",
                    help="指定回测产物目录（默认=--out/backtest_v{版本}_"
                         "{时间戳}_{区间}[_tag]）")
    ap.add_argument("--tag", default="")
    ap.add_argument("--label", default="",
                    help="版本标签（图表/对比用；缺省=tag，再缺省=当天日期）")
    ap.add_argument("--no-charts", action="store_true",
                    help="只写 JSON/MD，不生成图表")
    ap.add_argument("--charts-only", action="store_true",
                    help="不跑回测：按 --tag/--label 读取已有报告并出图")
    ap.add_argument("--compare", nargs="?", const="all", default=None,
                    help="版本对比出图：all=扫描 research/v61_report*.json；"
                         "或逗号分隔的 tag 列表")
    args = ap.parse_args()
    label = args.label or args.tag or time.strftime("%Y%m%d")
    chart_dir = os.path.join(
        args.out, "charts",
        f"v61_{args.segment}" + (f"_{args.tag}" if args.tag else ""))

    # ---- 只对比出图（不跑回测）----
    if args.compare is not None:
        from v61_charts import discover_reports, draw_compare
        if args.compare in ("all", ""):
            reps = discover_reports(args.out, args.segment)
        else:
            reps = []
            for tag in args.compare.split(","):
                tag = tag.strip()
                fn = f"v61_report_{tag}.json" if tag else "v61_report.json"
                p = os.path.join(args.out, fn)
                if os.path.exists(p):
                    r = _load_report(p)
                    r["label"] = tag or "default"
                    reps.append(r)
                else:
                    print(f"跳过（不存在）: {p}")
        made = draw_compare(reps, chart_dir, args.segment)
        print(f"版本对比：{len(reps)} 个版本 → 图表 {len(made)} 张"
              f" @ {chart_dir}")
        for p in made:
            print("  " + p)
        return

    # ---- 只出图（不跑回测）----
    if args.charts_only:
        suffix = f"_{args.tag}" if args.tag else ""
        p = os.path.join(args.out, f"v61_report{suffix}.json")
        if not os.path.exists(p):
            raise SystemExit(f"缺少 {p}：先跑一次回测或指定 --tag")
        report = _load_report(p)
        report.setdefault("label", label)
        from v61_charts import draw_report
        made = draw_report(report, chart_dir)
        print(f"出图 {len(made)} 张 @ {chart_dir}")
        for p in made:
            print("  " + p)
        return

    # ---- 正常回测 ----
    if args.universe in ("all4", "both"):
        unis = list(UNIS)
    else:
        unis = [args.universe]
    suffix = f"_{args.tag}" if args.tag else ""
    run_dir = args.run_dir or os.path.join(
        args.out, f"backtest_v{VERSION}_{time.strftime('%Y%m%d_%H%M%S')}_"
                  f"{args.segment}{suffix}")
    os.makedirs(run_dir, exist_ok=True)
    print(f"回测产物目录: {run_dir}")
    t0 = time.time()
    codes, cal, C, V = sg.tier_load_panel()
    report = {"version": VERSION,
              "ts": time.strftime("%Y-%m-%d %H:%M"),
              "label": label, "segment": args.segment,
              "data_end": cal[-1], "db_stats": _db_stats(),
              "results": {}}
    for uni in unis:
        report["results"][uni] = _run_universe(uni, args.segment, print)
    jpath = os.path.join(run_dir, "report.json")
    mpath = os.path.join(run_dir, "report.md")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    md = build_md(report)
    with open(mpath, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    tables = []
    try:
        tables = _write_tables(report, os.path.join(run_dir, "tables"))
    except Exception as e:
        print(f"明细表写出失败（不影响报告）: {e}")
    charts = []
    if not args.no_charts:
        try:
            from v61_charts import draw_report
            charts = draw_report(report, os.path.join(run_dir, "charts"))
        except Exception as e:
            print(f"图表生成失败（不影响报告）: {e}")
    # 兼容既有对比/发布链：research 根保留一份最新报告副本
    try:
        leg_j = os.path.join(args.out, f"v61_report{suffix}.json")
        leg_m = os.path.join(args.out, f"v61_report{suffix}.md")
        if os.path.abspath(leg_j) != os.path.abspath(jpath):
            with open(leg_j, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
            with open(leg_m, "w", encoding="utf-8") as f:
                f.write(md + "\n")
    except Exception as e:
        print(f"根目录报告副本写出失败（忽略）: {e}")
    meta_p = _write_meta(report, run_dir, sys.argv, time.time() - t0)
    # 自动刷新本地网页仪表盘（失败不影响回测产物）
    dash = ""
    try:
        from v61_dashboard import build_dashboard
        dash = build_dashboard(args.out)
    except Exception as e:
        print(f"网页仪表盘生成失败（忽略）: {e}")
    print("\n" + md)
    print(f"回测目录  {run_dir}")
    print(f"  报告    report.json / report.md")
    print(f"  明细表  {len(tables)} 个 @ {os.path.join(run_dir, 'tables')}")
    print(f"  图表    {len(charts)} 张 @ {os.path.join(run_dir, 'charts')}")
    print(f"  元数据  {meta_p}")
    if dash:
        print(f"  网页    {dash}")
    print(f"耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
