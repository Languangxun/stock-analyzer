from dataclasses import dataclass
from datetime import datetime


@dataclass
class MarketData:
    symbol: str
    name: str
    price: float
    change_percent: float
    volume: float
    timestamp: datetime


def create_market_data(
    symbol: str,
    name: str,
    price: float,
    change_percent: float,
    volume: float
):
    return MarketData(
        symbol=symbol,
        name=name,
        price=price,
        change_percent=change_percent,
        volume=volume,
        timestamp=datetime.now(),
    )
