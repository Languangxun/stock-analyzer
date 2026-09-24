from agent.llm import LLMClient
from agent.decision_parser import parse_decision
from agent.risk import check_decision

from trading.account import Account
from trading.executor import Executor

from data.market.market_loader import MarketLoader
from data.market.context_builder import MarketContextBuilder



class AgentController:


    def __init__(self):

        self.llm = LLMClient()

        self.account = Account(100000)

        self.executor = Executor(
            self.account
        )

        self.loader = MarketLoader()

        self.builder = MarketContextBuilder()



    def run(self):


        # 1. 获取真实行情

        market_data = self.loader.get_market()


        # 2. 构建AI上下文

        context = self.builder.build(
            market_data
        )


        print("MARKET CONTEXT:")

        print(context)



        # 3. AI分析

        result = self.llm.analyze(
            context
        )


        print("\nLLM OUTPUT:")

        print(result)



        # 4. 转换决策

        decision = parse_decision(
            result
        )


        print("\nDECISION:")

        print(decision)



        # 5. 风控

        current_position = 0


        if decision.target in self.account.positions:

            position = self.account.positions[
                decision.target
            ]

            current_position = (

                position.quantity
                *
                position.avg_price

                /
                self.account.total_asset()

                *
                100

            )


        risk = check_decision(

            decision,

            current_position

        )


        print("\nRISK:")

        print(risk)



        # 6. 执行（目前模拟）

        if risk.allowed:

            trade = self.executor.execute(
                decision
            )

            print("\nTRADE:")

            print(trade)


        else:

            print(
                "交易被拒绝"
            )


        return decision



if __name__ == "__main__":


    controller = AgentController()


    controller.run()
