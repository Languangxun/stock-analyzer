"""回放/每日模拟入口。

用法：
  python -m scripts.replay --start 2024-01-01 --decide   # 回放（含 AI 决策）
  python -m scripts.replay --start 2024-01-01            # 只跑确认/记录（无 AI）
"""
import argparse
import json
import os
import sys
from datetime import date, timedelta

from backtest.data_loader import HistoryDataLoader
from backtest.engine import SimEngine, load_checkpoint
from backtest.report import BacktestReport
from agent.ensemble import EnsembleDecision, DeepSeekVoter, OllamaVoter


def build_ensemble(use_ollama=False, deepseek=True):
    voters = []
    if deepseek:
        voters.append(DeepSeekVoter())
    if use_ollama:
        voters.append(OllamaVoter(model="qwen3.5:4b"))
    if not voters:
        raise ValueError("至少需要一个投票模型（--ollama 或 DeepSeek）")
    return EnsembleDecision(voters)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--capital", type=float, default=100000)
    parser.add_argument("--decide", action="store_true",
                        help="启用 AI 决策（默认只走确认/记录链路）")
    parser.add_argument("--ollama", action="store_true",
                        help="决策时启用本地 Ollama 投票")
    parser.add_argument("--no-deepseek", action="store_true",
                        help="不调用 DeepSeek API（训练省钱模式，需配合 --ollama）")
    parser.add_argument("--min-holding-days", type=int, default=7)
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--checkpoint", default=None,
                        help="每日保存断点检查点到该路径")
    parser.add_argument("--resume", default=None,
                        help="从检查点文件恢复续跑")
    args = parser.parse_args()

    loader = HistoryDataLoader()
    ensemble = (
        build_ensemble(
            use_ollama=args.ollama,
            deepseek=not args.no_deepseek,
        )
        if args.decide else None
    )

    engine = SimEngine(
        capital=args.capital,
        ensemble=ensemble,
        min_holding_days=args.min_holding_days,
        loader=loader,
    )

    print(f"回放 {args.start} -> {args.end or 'latest'}, "
          f"capital={args.capital}, decide={args.decide}")
    resume_start = args.start
    if args.resume and os.path.exists(args.resume):
        data = load_checkpoint(args.resume)
        engine.account = data["account"]
        engine.records = data["records"]
        resume_start = (
            date.fromisoformat(data["last_date"]) + timedelta(days=1)
        ).isoformat()
        print(f"恢复检查点：已处理至 {data['last_date']}，"
              f"从 {resume_start} 继续")
    engine.replay(
        start=resume_start,
        end=args.end,
        decide=args.decide,
        progress=not args.no_progress,
        checkpoint_path=args.checkpoint,
    )
    if args.checkpoint and os.path.exists(args.checkpoint):
        os.remove(args.checkpoint)

    report = BacktestReport(loader=loader).generate(
        engine.records, account=engine.account
    )
    print("\n===== 报告 =====")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    path = engine.save_result()
    print(f"\n结果已保存: {path}")


if __name__ == "__main__":
    main()
