from evaluation.performance import statistics
from evaluation.trade_matcher import match_trades


def generate_report():

    stats = statistics()

    results = match_trades()

    win = 0
    lose = 0
    total_return = 0


    for item in results:

        r = item["return"]

        total_return += r

        if r > 0:
            win += 1

        elif r < 0:
            lose += 1


    completed = len(results)

    win_rate = (
        round(win / completed * 100, 2)
        if completed
        else 0
    )

    avg_return = (
        round(total_return / completed, 2)
        if completed
        else 0
    )


    print("=== AI Quant Report ===")
    print()

    print("交易总数:", stats["total_trades"])
    print("买入次数:", stats["buy_count"])
    print("卖出次数:", stats["sell_count"])

    print()

    print("完成交易:", completed)
    print("盈利:", win)
    print("亏损:", lose)
    print("胜率:", win_rate, "%")
    print("平均收益:", avg_return, "%")


if __name__ == "__main__":
    generate_report()
