"""多模型共同决策测试（Mock voters，不真实调用模型）。"""
import pytest

from agent.ensemble import EnsembleDecision, _extract_json
from agent.decision import create_decision


class MockVoter:
    def __init__(self, name, target, confidence, weight=1.0, fail=False):
        self.name = name
        self.weight = weight
        self.target = target
        self.confidence = confidence
        self.fail = fail

    def is_available(self):
        return True

    def analyze(self, context):
        if self.fail:
            raise RuntimeError("mock failure")
        return {
            "action": "BUY",
            "target": "半导体",
            "target_position": self.target,
            "confidence": self.confidence,
            "reason": f"{self.name} says {self.target}",
            "risk": "risk",
        }


def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_markdown():
    text = '```json\n{"a": 1}\n```'
    assert _extract_json(text) == {"a": 1}


def test_ensemble_weighted_mean():
    ens = EnsembleDecision([
        MockVoter("a", 20, 0.8),
        MockVoter("b", 40, 0.2),
    ])
    d, votes = ens.decide({}, current_position=0)
    # (20*0.8 + 40*0.2) / 1.0 = 24
    assert d.target_position == pytest.approx(24, abs=0.1)
    assert d.action == "BUY"
    assert len(votes) == 2


def test_ensemble_outlier_removed():
    ens = EnsembleDecision([
        MockVoter("a", 20, 0.9),
        MockVoter("b", 25, 0.9),
        MockVoter("c", 40, 0.9),  # 离群：|40-25|=15 == 阈值 → 不算离群
    ], outlier_threshold=10)
    d, votes = ens.decide({}, current_position=0)
    # 剔除 c 后 (20+25)/2 = 22.5
    assert d.target_position == pytest.approx(22.5, abs=0.1)


def test_ensemble_single_model_failure_ok():
    ens = EnsembleDecision([
        MockVoter("a", 30, 0.9),
        MockVoter("b", 10, 0.9, fail=True),
    ])
    d, votes = ens.decide({}, current_position=0)
    assert d.target_position == pytest.approx(30)
    assert votes[1]["ok"] is False


def test_ensemble_all_fail_hold():
    ens = EnsembleDecision([
        MockVoter("a", 30, 0.9, fail=True),
    ])
    d, votes = ens.decide({}, current_position=15)
    assert d.action == "HOLD"
    assert d.target_position == 15  # 保持当前仓位


def test_ensemble_action_sell_when_target_below_current():
    ens = EnsembleDecision([
        MockVoter("a", 5, 0.9),
    ])
    d, _ = ens.decide({}, current_position=30)
    assert d.action == "SELL"


def test_ensemble_action_hold_when_near_current():
    ens = EnsembleDecision([
        MockVoter("a", 30.5, 0.9),
    ])
    d, _ = ens.decide({}, current_position=30)
    assert d.action == "HOLD"


def test_ensemble_majority_target_name():
    ens = EnsembleDecision([
        MockVoter("a", 20, 0.9),
        MockVoter("b", 20, 0.9),
    ])
    # 用自定义 voter 指定 target
    class NamedVoter(MockVoter):
        def __init__(self, name, target, conf, tgt_name):
            super().__init__(name, target, conf)
            self.tgt_name = tgt_name

        def analyze(self, context):
            r = super().analyze(context)
            r["target"] = self.tgt_name
            return r

    ens2 = EnsembleDecision([
        NamedVoter("a", 20, 0.9, "通信"),
        NamedVoter("b", 20, 0.9, "通信"),
        NamedVoter("c", 20, 0.9, "银行"),
    ])
    d, _ = ens2.decide({}, current_position=0)
    assert d.target == "通信"
