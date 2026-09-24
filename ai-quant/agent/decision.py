"""AI 决策模型：核心字段 target_position（目标仓位 %）。

position_change 保留为兼容属性（= target - current，需要 current 才可计算）。
"""
from dataclasses import dataclass
from datetime import datetime

VALID_ACTIONS = ("BUY", "SELL", "HOLD")


@dataclass
class Decision:
    action: str            # BUY / SELL / HOLD
    target: str            # 行业名：半导体/通信/人工智能/银行/消费电子
    target_position: float  # 目标仓位（占总资产 %）
    confidence: float
    reason: str
    risk: str
    source: str = "unknown"
    timestamp: str = ""
    current_position: float = None  # 决策时的已确认仓位（可选，用于兼容计算）

    @property
    def position_change(self):
        """兼容旧字段：target_position - current_position。"""
        if self.current_position is None:
            return None
        return round(self.target_position - self.current_position, 4)


def create_decision(action, target, target_position, confidence,
                    reason, risk, source="unknown", current_position=None):
    return Decision(
        action=action,
        target=target,
        target_position=float(target_position),
        confidence=float(confidence),
        reason=reason,
        risk=risk,
        source=source,
        timestamp=str(datetime.now()),
        current_position=current_position,
    )
