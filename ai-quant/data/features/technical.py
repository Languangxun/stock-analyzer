import statistics


class TechnicalFeature:

    def calculate_prices(self, prices):
        """基于价格序列 [float] 计算指标。"""
        if not prices:
            return {"price": 0, "change_1d": 0, "ma5": 0, "ma20": 0,
                    "trend": "side", "volatility": 0}
        current = prices[-1]

        change_1d = (
            (prices[-1] - prices[-2]) / prices[-2] * 100
            if len(prices) > 1 else 0
        )

        def ma(n):
            if len(prices) < n:
                return current
            return sum(prices[-n:]) / n

        ma5 = ma(5)
        ma20 = ma(20)

        if current > ma5 > ma20:
            trend = "up"
        elif current < ma5 < ma20:
            trend = "down"
        else:
            trend = "side"

        volatility = (
            statistics.stdev(prices) if len(prices) > 1 else 0
        )

        return {
            "price": current,
            "change_1d": change_1d,
            "ma5": ma5,
            "ma20": ma20,
            "trend": trend,
            "volatility": volatility
        }

    def calculate(self, history):

        prices = [
            item["price"]
            for item in history
        ]

        return self.calculate_prices(prices)



if __name__ == "__main__":

    data = [

        {
            "price":1.0
        },

        {
            "price":1.05
        },

        {
            "price":1.08
        }

    ]


    engine = TechnicalFeature()

    print(
        engine.calculate(data)
    )
