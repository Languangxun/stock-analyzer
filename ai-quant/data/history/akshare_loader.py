import akshare as ak
import json


class AKShareLoader:


    def __init__(
        self,
        symbol="512480"
    ):

        self.symbol = symbol



    def fetch(self):

        df = ak.fund_etf_hist_em(
            symbol=self.symbol
        )


        data = []


        for _, row in df.iterrows():

            data.append(

                {

                    "date":
                    str(
                        row["日期"]
                    ),


                    "symbol":
                    "半导体ETF",


                    "price":
                    float(
                        row["收盘"]
                    ),


                    "change":
                    float(
                        row["涨跌幅"]
                    )

                }

            )


        return data



    def save(
        self,
        path="data/history/real.json"
    ):


        data = self.fetch()


        with open(
            path,
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
            "保存完成:",
            len(data)
        )



if __name__ == "__main__":


    loader = AKShareLoader()


    loader.save()
