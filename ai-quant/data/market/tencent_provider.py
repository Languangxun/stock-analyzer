import requests
from datetime import datetime

from data.market.market_data import MarketData


class TencentProvider:


    def __init__(self):

        self.url = "https://qt.gtimg.cn/q="

        self.session = requests.Session()

        self.session.headers.update({

            "User-Agent":
            "Mozilla/5.0"

        })


    def get(self, symbol, code, market):

        if market == "sh":
            query = "sh" + code

        elif market == "sz":
            query = "sz" + code

        else:
            raise ValueError(
                "未知市场"
            )


        response = self.session.get(

            self.url + query,

            timeout=5

        )


        response.encoding = "gbk"


        text = response.text


        data = text.split('"')[1]


        fields = data.split("~")


        return MarketData(

            symbol=symbol,

            name=fields[1],

            price=float(fields[3]),

            change_percent=float(fields[32]),

            volume=int(fields[36]),

            timestamp=datetime.now()

        )



if __name__ == "__main__":

    provider = TencentProvider()


    print(

        provider.get(

            "半导体",

            "512480",

            "sh"

        )

    )
