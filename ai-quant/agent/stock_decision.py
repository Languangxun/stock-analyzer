"""股票组合决策层：deepseek-v4.1-flash（deepseek-flash）+ qwen3-embedding 记忆。

- 候选池：stock_predict CLI 多维评分选股（data.stock.cli_bridge.candidates）
- 记忆：qwen3-embedding 向量检索相似历史（memory.otc_memory）
- 输出：orders 列表（BUY/SELL + 目标仓位），交由 trading.stock_executor 执行
"""
from models.deepseek import DeepSeekModel
from models.stock_prompt import build_system_prompt


class StockDecisionMaker:
    def __init__(self, system_prompt=None):
        self.client = DeepSeekModel(
            system_prompt=system_prompt or build_system_prompt())
        self.model = self.client.model

    def decide(self, context):
        """返回 (result_dict, error)。result 含 market_view / orders。"""
        try:
            result = self.client.analyze(context)
        except Exception as e:
            return None, str(e)
        if not isinstance(result, dict):
            return None, f"模型输出非 JSON 对象: {str(result)[:120]}"
        orders = result.get("orders")
        if orders is None:
            result["orders"] = []
        elif not isinstance(orders, list):
            return None, "orders 字段不是列表"
        return result, None
