#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_cli.py - 从 stock_gui.py 自动生成独立单文件版 stock_predict.py

stock_gui.py 是唯一算法源。本脚本做三件事：
  1. 把缓存层（原 stock_cache.py 的内容，现已内嵌在 stock_gui.py 里）抽出
  2. 抽出算法主体（QT_URL 起，到 slice_view 前止，不含任何 tkinter 代码）
  3. 拼上 CLI 专属代码（推送/命令行入口），写出 stock_predict.py

改完 stock_gui.py 的算法后运行： python build_cli.py
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_PATH = os.path.join(HERE, "stock_gui.py")
CLI_PATH = os.path.join(HERE, "stock_predict.py")

CLI_HEADER = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票形态相似度预测 · 命令行版（独立单文件，不依赖 stock_gui.py）

与 stock_gui.py 共用同一套分析算法（由 build_cli.py 自动生成）：
价格形态 + 量能状态 + 大盘 + 板块 + 同行业 + 同市值层 多级加权匹配，
多算法消融选策略（训练/验证切分防过拟合）。
内建 SQLite 缓存（stock_cache.db），同行业/同市值层样本池只回填一次。
K线源自动切换：腾讯(三域名轮换) -> 东财 -> 网易163 -> 新浪；支持代理。

用法：python stock_predict.py [--push] [--refresh-cache] [--backfill]
                             [--clean] [--research] [--v4 [--v4-limit N]]
                             [--tiers [--tier 稳健|均衡|激进]]
                             [--tiers-backtest] [股票代码]
  --push           分析完成后把报告推送到 Pi 量化系统收件箱（ai-quant）
  --refresh-cache  刷新全市场代码表/市值分层（约1分钟，7天有效）
  --backfill       全市场1000交易日日K回填（断点续传，配额内自动分晚完成）
  --clean          数据清洗（结构异常/除权残留/退市/粘性，扫描+修复）
  --research       全A研究报告：各算法 IC/胜率/年化/回撤 跨股聚合
  --v4             v4.0 全A研究：Walk-Forward自适应ML + 三档风险回测 + 消融
  --tiers          v6.0 三档组合：输出最新目标持仓/闸门状态（可配 --tier）
  --tiers-backtest v6.0 三档组合：全期回测摘要（相位平均，含全部费用）
  --picks-backtest v6.0 荐股收益回测（逐笔口径，按风险偏好；--tier 过滤）
  --picks-seg     荐股回测区间：full(默认)/val/bull/2024/2025...
"""

'''

CLI_IMPORTS = '''
import atexit
import configparser
import heapq
import json
import logging
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

try:
    import numpy as np            # 数值加速（缺失时自动退回纯Python）
except ImportError:
    np = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CACHE_OK = True      # 缓存层已内嵌，恒可用

'''

CLI_MAIN_MARKER = "# ==================== 以下为 CLI 专属 ===================="


def extract(src, start_marker, end_marker):
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


def extract_cache_block(gui_src):
    """缓存层：GUI 内嵌段，从 CACHE_OK = True 后的缓存标记到 QT_URL 前。"""
    start = gui_src.index("# ================= 内嵌缓存层")
    end = gui_src.index("QT_URL = ", start)
    return gui_src[start:end]


def extract_algo_block(gui_src):
    """算法主体：QT_URL 起，slice_view 前止。"""
    return extract(gui_src, "QT_URL = ", "def slice_view")


def extract_cli_tail(old_cli_src):
    """CLI 专属：build_payload 起到文件尾（清洗掉旧的外部模块引用）。"""
    tail = old_cli_src[old_cli_src.index("def build_payload"):]
    tail = re.sub(r"\bsca\.", "", tail)
    tail = re.sub(r"\b(sca|sc) is not None", "True", tail)
    return tail


def main():
    with open(GUI_PATH, encoding="utf-8") as f:
        gui_src = f.read()
    if not os.path.exists(CLI_PATH):
        raise SystemExit("缺少 %s：CLI 专属尾部只能从既有生成物提取" % CLI_PATH)
    with open(CLI_PATH, encoding="utf-8") as f:
        old_cli = f.read()

    cache_start = gui_src.index("# ================= 内嵌缓存层")
    cache_end = gui_src.index("QT_URL = ", cache_start)
    algo_start = gui_src.index("QT_URL = ")
    if cache_end > algo_start:
        raise SystemExit("缓存块与算法块区间重叠，拒绝生成")

    cache_block = extract_cache_block(gui_src)
    algo_block = extract_algo_block(gui_src)

    # 算法块里不应有 tkinter 残留
    if "tkinter" in algo_block:
        raise SystemExit("算法块混入了 tkinter 代码")

    cli_tail = extract_cli_tail(old_cli)

    out = (CLI_HEADER + CLI_IMPORTS + "\n" + cache_block + "\n\n"
           + algo_block.rstrip() + "\n\n"
           + CLI_MAIN_MARKER + "\n\n" + cli_tail)
    # 写入前先做语法校验，避免生成物被写坏
    try:
        compile(out, CLI_PATH, "exec")
    except SyntaxError as e:
        raise SystemExit("生成结果语法错误，未写入: %s" % e)
    tmp = CLI_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    os.replace(tmp, CLI_PATH)
    print("已生成 %s (%.1f KB, 算法+缓存内嵌, 独立运行)"
          % (CLI_PATH, len(out.encode("utf-8")) / 1024))


if __name__ == "__main__":
    main()
