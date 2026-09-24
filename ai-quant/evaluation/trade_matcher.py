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



def match_trades():

    trades = load_trades()

    positions = {}
    results = []


    for trade in trades:

        symbol = trade["symbol"]


        if trade["side"] == "BUY":

            positions[symbol] = trade


        elif trade["side"] == "SELL":

            if symbol in positions:

                buy = positions.pop(symbol)

                profit = (
                    trade["price"]
                    -
                    buy["price"]
                ) / buy["price"] * 100


                results.append({
                    "symbol": symbol,
                    "buy_price": buy["price"],
                    "sell_price": trade["price"],
                    "return": round(profit, 2)
                })


    return results



if __name__ == "__main__":

    print(
        match_trades()
    )
