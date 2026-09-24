from abc import ABC, abstractmethod


class MarketProvider(ABC):

    @abstractmethod
    def get_market_data(self):
        pass
