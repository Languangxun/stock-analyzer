import json
import os

from models.embedding import OllamaEmbedding


class EmbeddingBuilder:


    def __init__(self):

        self.model = OllamaEmbedding()

        self.trade_file = "memory/trades.json"

        self.output_file = "memory/embeddings.json"



    def load_trades(self):

        with open(
            self.trade_file,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)



    def build_text(self, trade):

        return (

            f"股票:{trade['symbol']} "

            f"操作:{trade['side']} "

            f"价格:{trade['price']} "

            f"信心:{trade['confidence']} "

            f"原因:{trade['reason']} "

            f"风险:{trade['risk']}"

        )



    def run(self):

        trades = self.load_trades()


        result = []


        for trade in trades:


            text = self.build_text(
                trade
            )


            vector = self.model.embed(
                text
            )


            result.append({

                "trade": trade,

                "text": text,

                "vector": vector

            })


            print(
                "完成:",
                trade["symbol"]
            )



        with open(
            self.output_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(

                result,

                f,

                ensure_ascii=False

            )



        print(
            "保存完成"
        )



if __name__ == "__main__":


    EmbeddingBuilder().run()
