"""解析 AI 输出的 JSON 为 Decision（核心字段 target_position）。"""
from agent.decision import Decision, VALID_ACTIONS


def parse_decision(data):
    action = str(data.get("action", "HOLD")).upper()
    if action not in VALID_ACTIONS:
        action = "HOLD"
    return Decision(
        action=action,
        target=str(data.get("target", "")),
        target_position=float(data.get("target_position", 0)),
        confidence=float(data.get("confidence", 0)),
        reason=str(data.get("reason", "")),
        risk=str(data.get("risk", "")),
        source=data.get("source", "llm"),
        timestamp=str(data.get("timestamp", "")),
        current_position=(
            float(data["current_position"])
            if data.get("current_position") is not None
            else None
        ),
    )
