from data.market.tencent_provider import TencentProvider


class MarketLoader:

    def __init__(self):

        self.provider = TencentProvider()


    def get_market(self):

        watchlist = [

            {
                "symbol": "半导体",
                "code": "512480",
                "market": "sh"
            },

            {
                "symbol": "通信",
                "code": "515880",
                "market": "sh"
            },

            {
                "symbol": "人工智能",
                "code": "159819",
                "market": "sz"
            },

            {
                "symbol": "银行",
                "code": "512800",
                "market": "sh"
            },

            {
                "symbol": "消费电子",
                "code": "159732",
                "market": "sz"
            }

        ]


        result = []


        for item in watchlist:

            try:

                data = self.provider.get(

                    item["symbol"],

                    item["code"],

                    item["market"]

                )

                result.append(data)


            except Exception as e:

                print(
                    f"{item['symbol']} 获取失败:",
                    e
                )


        return result



if __name__ == "__main__":


    loader = MarketLoader()


    for item in loader.get_market():

        print(item)
