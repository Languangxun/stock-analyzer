import socket
import requests
from datetime import datetime

from data.market.market_data import MarketData


# 强制IPv4，避免部分环境IPv6解析异常
_old_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(*args, **kwargs):

    responses = _old_getaddrinfo(
        *args,
        **kwargs
    )

    return [
        r for r in responses
        if r[0] == socket.AF_INET
    ]



socket.getaddrinfo = _ipv4_getaddrinfo



class EastMoneyProvider:


    def __init__(self):

        self.url = (
            "https://push2.eastmoney.com/api/qt/stock/get"
        )

        self.session = requests.Session()

        self.session.headers.update({

            "User-Agent":
            "Mozilla/5.0",

            "Referer":
            "https://quote.eastmoney.com/"

        })



    def _secid(self, code, market):

        if market == "sh":

            return f"1.{code}"

        elif market == "sz":

            return f"0.{code}"

        else:

            raise ValueError(
                f"未知市场: {market}"
            )



    def get(self, symbol, code, market):

        secid = self._secid(
            code,
            market
        )


        params = {

            "secid": secid,

            "fields":
            "f43,f47,f58,f170"

        }


        try:

            response = self.session.get(

                self.url,

                params=params,

                timeout=5

            )


            response.raise_for_status()


        except requests.RequestException as e:

            raise RuntimeError(
                f"东财行情请求失败: {e}"
            )



        result = response.json()


        if not result.get("data"):

            raise ValueError(
                f"东财返回异常: {result}"
            )


        quote = result["data"]



        price = (

            float(
                quote["f43"]
            )
            /
            1000

        )


        change_percent = (

            float(
                quote["f170"]
            )
            /
            100

        )


        volume = int(
            quote["f47"]
        )


        return MarketData(

            symbol=symbol,

            name=quote["f58"],

            price=price,

            change_percent=change_percent,

            volume=volume,

            timestamp=datetime.now()

        )



if __name__ == "__main__":


    provider = EastMoneyProvider()


    result = provider.get(

        symbol="半导体",

        code="512480",

        market="sh"

    )


    print(result)
