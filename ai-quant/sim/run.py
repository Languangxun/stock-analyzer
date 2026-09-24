"""模拟盘统一入口（双模式）。

  python -m sim.run                 # 默认：股票模式（stock_run）
  python -m sim.run --mode fund     # 旧版：场外 ETF 联接 C 类基金（fund_run）

股票模式：CLI 选股 + deepseek-v4.1-flash + qwen3-embedding 记忆 + A股规则。
基金模式：etf-c 旧版，保留可切换（cron 无需改动）。
"""
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = "stock"
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--mode" and i + 1 < len(argv):
            mode = argv[i + 1]
            i += 2
            continue
        if a.startswith("--mode="):
            mode = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    if mode == "fund":
        from sim import fund_run
        return fund_run.main(rest)
    from sim import stock_run
    return stock_run.main(rest)


if __name__ == "__main__":
    main()
