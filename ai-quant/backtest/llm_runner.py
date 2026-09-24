from agent.llm import LLMClient
from agent.decision import create_decision
from memory.retriever import MemoryRetriever


class LLMRunner:
    """
    AI决策层

    backtest=True:
        回测模式
        跳过embedding检索，提高速度

    backtest=False:
        实盘模式
        使用历史记忆RAG
    """


    def __init__(self, backtest=False):

        self.llm = LLMClient(
            model="deepseek"
        )

        self.backtest = backtest


        if not backtest:

            self.memory = MemoryRetriever()



    def decide(self, features):


        if self.backtest:

            history = []


        else:

            history = self.memory.search(
                features,
                limit=3
            )



        context = {

            "market": "ETF",

            "features": features,

            "similar_history": history

        }



        result = self.llm.analyze(
            context
        )



        decision = create_decision(

            action=result.get(
                "action",
                "HOLD"
            ),


            target=result.get(
                "target",
                "ETF"
            ),


            position_change=float(
                result.get(
                    "position_change",
                    0
                )
            ),


            confidence=float(
                result.get(
                    "confidence",
                    0
                )
            ),


            reason=result.get(
                "reason",
                ""
            ),


            risk=result.get(
                "risk",
                ""
            )

        )


        return decision




if __name__ == "__main__":


    runner = LLMRunner(
        backtest=True
    )


    features = {

        "price":1.088,

        "change_1d":2.85,

        "ma5":1.08,

        "ma20":1.08,

        "trend":"side",

        "volatility":0.04

    }


    result = runner.decide(
        features
    )


    print(result)
