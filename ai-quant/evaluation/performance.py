import json
from pathlib import Path


TRADE_FILE = Path("memory/trades.json")


def load_trades():

    if not TRADE_FILE.exists():
        return []

    with open(
        TRADE_FILE,
        "r",
        encoding="utf-8"
    ) as f:
        return json.load(f)



def statistics():

    trades = load_trades()

    total = len(trades)

    buy_count = 0
    sell_count = 0


    for trade in trades:

        if trade["side"] == "BUY":
            buy_count += 1

        elif trade["side"] == "SELL":
            sell_count += 1


    return {
        "total_trades": total,
        "buy_count": buy_count,
        "sell_count": sell_count
    }



def report():

    result = statistics()

    print("=== Performance Report ===")

    for key, value in result.items():
        print(
            f"{key}: {value}"
        )



if __name__ == "__main__":

    report()
