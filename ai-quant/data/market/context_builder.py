import json


class MarketContextBuilder:


    def build(self, market_data):

        market = []


        for item in market_data:

            market.append({

                "symbol": item.symbol,

                "name": item.name,

                "price": item.price,

                "change_percent": item.change_percent,

                "volume": item.volume,

                "time": item.timestamp.isoformat()

            })


        return {

            "market": market,

            "source": "tencent",

            "type": "realtime"

        }


    def to_json(self, context):

        return json.dumps(

            context,

            ensure_ascii=False,

            indent=2

        )



if __name__ == "__main__":

    from data.market.market_loader import MarketLoader


    loader = MarketLoader()

    data = loader.get_market()


    builder = MarketContextBuilder()


    context = builder.build(data)


    print(

        builder.to_json(context)

    )
