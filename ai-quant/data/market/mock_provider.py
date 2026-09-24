import random
from datetime import datetime

from data.market.market_data import MarketData


WATCH_LIST = [
    "银行",
    "半导体",
    "通信",
    "人工智能",
    "消费电子",
]


def get_mock_market():
    data = []

    for name in WATCH_LIST:
        item = MarketData(
            symbol=name,
            name=name,
            price=round(random.uniform(0.8, 3.0), 3),
            change_percent=round(random.uniform(-3, 3), 2),
            volume=random.randint(100000, 5000000),
            timestamp=datetime.now(),
        )

        data.append(item)

    return data


if __name__ == "__main__":
    for item in get_mock_market():
        print(item)
