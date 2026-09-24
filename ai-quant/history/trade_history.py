import json
import os


FILE = "memory/trades.json"


def save_trade(
    trade,
    confidence=0,
    risk=""
):
    os.makedirs(
        "memory",
        exist_ok=True
    )

    data = []

    if os.path.exists(FILE):
        with open(
            FILE,
            "r",
            encoding="utf-8"
        ) as f:
            try:
                data = json.load(f)
            except (json.JSONDecodeError, TypeError):
                data = []

    record = {
        "time": trade.timestamp.isoformat(),
        "symbol": trade.symbol,
        "side": trade.side,
        "quantity": trade.quantity,
        "price": trade.price,
        "confidence": confidence,
        "reason": trade.reason,
        "risk": risk
    }

    data.append(record)

    with open(
        FILE,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


if __name__ == "__main__":
    print("trade_history OK")
