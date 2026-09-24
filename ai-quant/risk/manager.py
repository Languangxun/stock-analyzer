"""统一风控：场外基金仓位规则。

规则：
- action 必须是 BUY/SELL/HOLD
- 0 <= target_position <= max_position（默认 40）
- BUY 必须提高仓位 / SELL 必须降低仓位（与 target_position 一致）
- 普通单次调整 <= max_single_change（默认 20 个百分点）
- SELL + target_position=0 为清仓，豁免单次调整限制
- HOLD 保持当前已确认仓位（pending 订单不计入已持仓）
"""
from dataclasses import dataclass

VALID_ACTIONS = ("BUY", "SELL", "HOLD")


@dataclass
class RiskResult:
    allowed: bool
    target_position: float
    reason: str

    def __bool__(self):
        return self.allowed


class RiskManager:
    def __init__(self, max_position=40.0, max_single_change=20.0):
        self.max_position = max_position
        self.max_single_change = max_single_change

    def check(self, decision, current_position=0.0) -> RiskResult:
        """校验决策。current_position 必须是已确认持仓（%），不含 pending。"""
        action = str(getattr(decision, "action", "")).upper()
        target = float(getattr(decision, "target_position", 0))

        if action not in VALID_ACTIONS:
            return RiskResult(False, current_position, "非法操作")

        if action == "HOLD":
            # HOLD：保持当前已确认仓位，不校验 target 数字
            return RiskResult(True, current_position, "保持当前仓位")

        if target < 0:
            return RiskResult(False, current_position, "目标仓位不能为负")
        if target > self.max_position:
            return RiskResult(
                False, self.max_position,
                f"超过单基金最大仓位 {self.max_position}%"
            )

        if action == "HOLD":
            return RiskResult(True, current_position, "保持当前仓位")

        delta = target - current_position

        # BUY/SELL 与目标仓位一致性
        if action == "BUY" and delta <= 0:
            return RiskResult(False, current_position, "BUY 必须提高仓位")
        if action == "SELL" and delta >= 0:
            return RiskResult(False, current_position, "SELL 必须降低仓位")

        # 单次调整限制（清仓豁免）
        is_liquidation = action == "SELL" and target == 0
        if abs(delta) > self.max_single_change and not is_liquidation:
            return RiskResult(
                False, current_position,
                f"单次仓位调整 {abs(delta):.2f}% 超过 {self.max_single_change}%"
            )

        return RiskResult(True, target, "风险检查通过")
