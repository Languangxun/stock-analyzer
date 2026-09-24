from dataclasses import dataclass


MAX_SINGLE_CHANGE = 20
MAX_POSITION = 100


@dataclass
class RiskResult:
    allowed: bool
    reason: str


def check_decision(decision, current_position):
    # 检查动作
    if decision.action not in ["BUY", "SELL", "HOLD"]:
        return RiskResult(False, "非法操作")


    # 检查单次调整
    if abs(decision.position_change) > MAX_SINGLE_CHANGE:
        return RiskResult(
            False,
            "单次仓位调整超过20%"
        )


    # 检查目标仓位
    new_position = current_position + decision.position_change

    if new_position > MAX_POSITION:
        return RiskResult(
            False,
            "超过最大仓位"
        )

    if new_position < 0:
        return RiskResult(
            False,
            "仓位不能小于0"
        )


    return RiskResult(
        True,
        "风险检查通过"
    )


if __name__ == "__main__":
    from agent.decision import create_decision

    d = create_decision(
        "BUY",
        "半导体",
        10,
        0.7,
        "测试",
        "测试风险"
    )

    result = check_decision(d, 30)

    print(result)
