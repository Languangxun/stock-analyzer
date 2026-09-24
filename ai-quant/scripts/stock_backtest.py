#!/usr/bin/env python3
"""全缓存股票组合回测入口（CLI 信号 + A股规则）。

用法（在 ai-quant 目录下）：
  .venv/bin/python scripts/stock_backtest.py                     # 全缓存全量
  .venv/bin/python scripts/stock_backtest.py --limit 300         # 抽样调试
  .venv/bin/python scripts/stock_backtest.py --mode 激进 --start 2024-01-01
  .venv/bin/python scripts/stock_backtest.py --exec-px open
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.stock_engine import StockBacktest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="回测起点（默认=最早信号日）")
    ap.add_argument("--end", default=None)
    ap.add_argument("--capital", type=float, default=None)
    ap.add_argument("--mode", default=None, choices=["保守", "稳健", "激进"])
    ap.add_argument("--max-positions", type=int, default=None)
    ap.add_argument("--limit", type=int, default=0,
                    help="只测市值前 N 只（0=全缓存）")
    ap.add_argument("--boards", default=None, choices=["all", "main"],
                    help="股票池板块：all=全市场，main=沪深主板")
    ap.add_argument("--exec-px", default=None, choices=["close", "open"])
    ap.add_argument("--tag", default=None, help="结果文件名标签")
    args = ap.parse_args()

    bt = StockBacktest(
        capital=args.capital, risk_mode=args.mode,
        max_positions=args.max_positions, start=args.start, end=args.end,
        limit=args.limit, exec_px=args.exec_px, boards=args.boards,
    )
    bt.run()
    print(json.dumps(bt.stats, ensure_ascii=False, indent=2, default=str))
    path = bt.save(args.tag)
    print(f"结果: {path}")


if __name__ == "__main__":
    main()
