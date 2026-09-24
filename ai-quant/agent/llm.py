import json

from models.deepseek import DeepSeekModel


class LLMClient:

    def __init__(self, model="deepseek"):

        self.model = model


        if self.model == "deepseek":
            self.client = DeepSeekModel()


    def analyze(self, context):

        if self.model == "deepseek":

            return self.client.analyze(
                context
            )


        elif self.model == "mock":

            return {
                "action": "HOLD",
                "target": "半导体",
                "position_change": 0,
                "confidence": 0.5,
                "reason": "模拟模型暂不操作",
                "risk": "需要更多数据"
            }


        else:

            raise ValueError(
                f"Unknown model: {self.model}"
            )



if __name__ == "__main__":

    llm = LLMClient()

    result = llm.analyze(
        {
            "market": "半导体上涨2%",
            "news": "行业政策支持"
        }
    )


    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        )
    )
