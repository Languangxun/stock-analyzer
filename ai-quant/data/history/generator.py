import random
import json
from datetime import datetime, timedelta


class MarketGenerator:


    def __init__(
        self,
        start_price=1.0,
        days=300
    ):

        self.price = start_price

        self.days = days



    def generate(self):

        data = []

        date = datetime(
            2026,
            1,
            1
        )


        regime = "side"


        for i in range(self.days):


            if i % 60 == 0:

                regime = random.choice(
                    [
                        "bull",
                        "bear",
                        "side",
                        "crash",
                        "recover"
                    ]
                )



            if regime == "bull":

                change = random.uniform(
                    0,
                    2
                )


            elif regime == "bear":

                change = random.uniform(
                    -2,
                    0
                )


            elif regime == "crash":

                change = random.uniform(
                    -8,
                    -3
                )


            elif regime == "recover":

                change = random.uniform(
                    0,
                    4
                )


            else:

                change = random.uniform(
                    -1.5,
                    1.5
                )



            self.price *= (
                1 + change / 100
            )


            data.append(

                {

                    "date":
                    (
                        date
                        +
                        timedelta(days=i)
                    ).strftime(
                        "%Y-%m-%d"
                    ),


                    "symbol":
                    "半导体ETF",


                    "price":
                    round(
                        self.price,
                        4
                    ),


                    "change":
                    round(
                        change,
                        2
                    )

                }

            )


        return data




if __name__ == "__main__":


    generator = MarketGenerator()


    data = generator.generate()


    with open(
        "data/history/generated.json",
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


    print(
        "生成完成:",
        len(data)
    )
