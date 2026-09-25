#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""progress.py - 后台长任务进度看板

显示三件事（纯标准库，随时可跑，不影响后台任务）：
  ① 全市场深历史拉取到 2000 根（读 pull2000_run.log + 查库统计）
  ② 拉完后的四口径回测（读 pull2000_run.log 的 BACKTEST 行）
  ③ 因子实验室全库面板（读 factorlab_full2000_run.log + 产物文件）

用法：
  python3 progress.py          # 看一次
  python3 progress.py -w 30    # 每 30 秒刷新（Ctrl-C 退出）
"""
import argparse
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "stock_cache.db")
PULL_LOG = os.path.join(ROOT, "pull2000_run.log")
LAB_LOG = os.path.join(ROOT, "factorlab_full2000_run.log")
LAB_DIR = os.path.join(ROOT, "research", "factor_lab", "full2000")

PULL_PAT = re.compile(
    r"全市场回填 (\d+)/(\d+) \((\d+)%\) 成功(\d+) 失败(\d+) ETA (\d+)分")
STAGE_PAT = re.compile(r"Stage (\d)/4\s+(.+)")


def find_procs(substr):
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "ignore")
        except OSError:
            continue
        if substr in cmd:
            out.append((int(pid), cmd.strip()))
    return out


def tail_lines(path, n=2000):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.readlines()[-n:]
    except OSError:
        return []


def fmt_ago(ts):
    d = time.time() - ts
    if d < 60:
        return f"{d:.0f}秒前"
    if d < 3600:
        return f"{d/60:.0f}分钟前"
    return f"{d/3600:.1f}小时前"


def fmt_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f}{u}" if u != "B" else f"{n}{u}"
        n /= 1024


def pull_status():
    lines = tail_lines(PULL_LOG)
    state = {"running": bool(find_procs("pull2000_then_backtest")),
             "last": None, "done": None, "backtests": [], "all_done": False}
    for ln in lines:
        m = PULL_PAT.search(ln)
        if m:
            state["last"] = tuple(int(x) for x in m.groups())
        if "PULL done:" in ln:
            state["done"] = ln.strip()
        if "BACKTEST " in ln and "exit" not in ln:
            state["backtests"].append(ln.split("]", 1)[-1].strip())
        if "ALL DONE" in ln:
            state["all_done"] = True
    return state


def db_stats():
    if not os.path.exists(DB):
        return {}
    try:
        c = sqlite3.connect(DB, timeout=10)
        total = c.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
        codes = c.execute("SELECT COUNT(DISTINCT code) FROM daily_bars"
                          ).fetchone()[0]
        ge2000 = c.execute(
            "SELECT COUNT(*) FROM (SELECT code FROM daily_bars "
            "GROUP BY code HAVING COUNT(*)>=2000)").fetchone()[0]
        mx = c.execute("SELECT MAX(date) FROM daily_bars").fetchone()[0]
        c.close()
        return {"total": total, "codes": codes, "ge2000": ge2000, "max": mx}
    except Exception as e:
        return {"err": str(e)}


def lab_status():
    lines = tail_lines(LAB_LOG)
    st = {"running": bool(find_procs("factor_ablation")),
          "stage": None, "stage_txt": "", "waiting": False, "done": False,
          "log": os.path.exists(LAB_LOG)}
    for ln in lines:
        if "waiting for pull job" in ln:
            st["waiting"] = True
        m = STAGE_PAT.search(ln)
        if m:
            st["stage"] = int(m.group(1))
            st["stage_txt"] = m.group(2).strip()
        if "factor lab exit" in ln or "ALL DONE" in ln:
            st["done"] = True
    arts = [("stocks_pass_a.pkl", "Pass A 因子缓存"),
            ("match_pass_b.pkl", "L2/L3 匹配缓存"),
            ("panel.npz", "面板"),
            ("enum_stats_equal.npz", "穷举(equal)"),
            ("enum_stats.npz", "穷举(framework)"),
            ("validate_results.json", "过拟合验证")]
    st["artifacts"] = []
    for fn, label in arts:
        p = os.path.join(LAB_DIR, fn)
        if os.path.exists(p):
            st["artifacts"].append((label, fmt_size(os.path.getsize(p)),
                                    fmt_ago(os.path.getmtime(p))))
    return st


def render():
    if sys.stdout.isatty():
        os.system("clear")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"后台任务进度  {now}")
    print("=" * 62)

    ps = pull_status()
    print("① 深历史拉取（→2000根）" + ("  [运行中]" if ps["running"] else
                                    "  [已结束]"))
    if ps["last"]:
        done, tot, pct, ok, fail, eta = ps["last"]
        bar_w = 30
        filled = int(bar_w * pct / 100)
        bar = "█" * filled + "·" * (bar_w - filled)
        rate = ok / max(done, 1) * 100
        print(f"   [{bar}] {pct}%  {done}/{tot} 只"
              f"  成功{ok} 失败{fail}（{rate:.0f}%）  ETA {eta}分")
    elif not ps["all_done"]:
        print("   暂无进度（日志未开始/刚启动）")
    if ps["done"]:
        print("   " + ps["done"])
    print("② 拉取后回测" + ("  [排队中]" if not ps["backtests"] else ""))
    for b in ps["backtests"]:
        print("   " + b)
    if ps["all_done"]:
        print("   四口径回测完成（产物 research/v61_report*_pull2000.*）")

    st = db_stats()
    if st and "err" not in st:
        print("-" * 62)
        print(f"   库内：{st['total']/1e4:.0f}万根 | {st['codes']} 只 | "
              f"≥2000根 {st['ge2000']} 只 | 最新 {st['max']}")

    print("-" * 62)
    lab = lab_status()
    tag = ("[运行中]" if lab["running"] else
           "[等待①结束]" if lab["waiting"] and lab["running"] is False and
           find_procs("run_factorlab_after_pull") else
           "[已结束]" if lab["done"] else "[未启动]")
    print(f"③ 因子实验室全库面板（EVAL_DAYS=2000, stage=all）  {tag}")
    if lab["stage"]:
        print(f"   当前阶段 {lab['stage']}/4：{lab['stage_txt']}")
    if lab["artifacts"]:
        for label, size, ago in lab["artifacts"]:
            print(f"   ✓ {label:<12} {size:>9}  {ago}")
    else:
        print("   暂无产物（等待拉取完成后自动开始）")
    print("=" * 62)
    print("日志：pull2000_run.log / factorlab_full2000_run.log"
          "（-w 30 自动刷新）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-w", "--watch", type=int, default=0,
                    help="刷新间隔秒数（缺省只看一次）")
    a = ap.parse_args()
    if a.watch <= 0:
        render()
        return
    try:
        while True:
            render()
            time.sleep(a.watch)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
