"""生成回放绩效报告：读回放结果 + 历史数据 → markdown 报告。

用法：
    python -m scripts.metrics_report [result.json] [--trades] [--out report.md]
默认 result: backtest/results/latest.json，报告输出到 memory/daily/。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.data_loader import HistoryDataLoader
from backtest.metrics import compute_metrics, format_report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("result", nargs="?", default="backtest/results/latest.json")
    ap.add_argument("--trades", action="store_true", help="附带逐笔交易明细")
    ap.add_argument("--out", default=None, help="报告输出路径（默认 memory/daily/绩效报告-日期.md）")
    args = ap.parse_args()

    d = json.load(open(args.result))
    records = d.get("records", [])
    account = d.get("account", {})
    if not records:
        print("records 为空")
        return

    loader = HistoryDataLoader()
    m = compute_metrics(records, account=account, loader=loader)
    rep = format_report(m, verbose_trades=args.trades)

    if not args.out:
        os.makedirs("memory/daily", exist_ok=True)
        end = m["period"]["end"]
        args.out = f"memory/daily/绩效报告-{end}.md"
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(rep + "\n")
    print(rep)
    print(f"\n[metrics_report] 已写入 {args.out}")


if __name__ == "__main__":
    main()
