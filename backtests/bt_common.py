#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bt_common.py - 回测程序统一规范（版本化运行目录 + run_meta + 根目录最新副本）

规范（与 backtests/backtest_v61.py 既有约定一致，v6.1.5 统一到所有回测/研究程序）：

  research/<kind>_v<APP_VERSION>_<YYYYMMDD_HHMMSS>[_<extra>][_<tag>]/
      run_meta.json        版本/时间/参数/数据规模/耗时/命令行（必写）
      <products>           该程序全部产物（JSON/CSV/xlsx/SVG…）
  同时 research/ 根目录保留「最新副本」（默认硬链接，跨盘回退复制），
  供既有下游脚本按固定文件名读取（如 strategy_ablation_per_stock.json）。

用法：
  from bt_common import new_run_dir, write_run_meta, link_latest
  run = new_run_dir("research", "ablation", extra="full")   # -> 目录路径
  write_run_meta(run, argv=sys.argv, elapsed=123.4, objects=7235)
  link_latest(os.path.join(run, "per_stock.json"),
              os.path.join("research", "strategy_ablation_per_stock.json"))
"""
import json
import os
import shutil
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_version():
    try:
        import stock_gui as sg
        return getattr(sg, "APP_VERSION", "0")
    except Exception:
        return "0"


def new_run_dir(base_dir, kind, tag="", extra="", stamp=None):
    """创建 research/<kind>_v<版本>_<时间戳>[_<extra>][_<tag>]/ 并返回路径。"""
    ts = stamp or time.strftime("%Y%m%d_%H%M%S")
    name = f"{kind}_v{app_version()}_{ts}"
    if extra:
        name += f"_{extra}"
    if tag:
        name += f"_{tag}"
    path = os.path.join(base_dir, name)
    os.makedirs(path, exist_ok=True)
    return path


def write_run_meta(run_dir, argv=None, elapsed=None, **fields):
    """写 run_meta.json（统一字段 + 自定义字段）。"""
    meta = {
        "version": app_version(),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "argv": list(argv) if argv else [],
        "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
    }
    meta.update(fields)
    path = os.path.join(run_dir, "run_meta.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return path


def link_latest(src, dst):
    """把最新产物挂到固定路径：优先硬链接（同盘零拷贝），失败回退复制。"""
    try:
        if os.path.exists(dst):
            os.remove(dst)
    except OSError:
        pass
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)
    return dst


def link_run_dir(src_dir, dst_dir):
    """把运行目录里的产物挂到另一个固定目录（硬链接，缺失回退复制）。"""
    os.makedirs(dst_dir, exist_ok=True)
    for name in os.listdir(src_dir):
        s = os.path.join(src_dir, name)
        d = os.path.join(dst_dir, name)
        if os.path.isdir(s):
            link_run_dir(s, d)
        else:
            link_latest(s, d)
    return dst_dir


def latest_run_dir(base_dir, kind):
    """返回 research 下最新一个 <kind>_v* 运行目录（无则 None）。"""
    import glob
    cands = sorted(glob.glob(os.path.join(base_dir, f"{kind}_v*")))
    return cands[-1] if cands else None
