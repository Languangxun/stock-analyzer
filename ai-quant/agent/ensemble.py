"""多模型共同决策层（Ensemble）。

- 云端 DeepSeek：主裁判（权重高）
- 本地 Ollama（qwen3:4b 等）：副裁判（权重可配）
- 每模型独立分析同一上下文 → 输出 {action, target, target_position, confidence, reason, risk}
- 聚合：离群剔除（|x - median| > outlier_threshold）后按置信度加权

聚合后的 action 由最终 target_position 与当前仓位的关系推导，
保证 BUY/SELL/HOLD 与目标仓位一致。
"""
import json
import statistics

import requests

from agent.decision import create_decision
from agent.decision_parser import parse_decision


def _industry_names():
    """从 FUND_MAP 读取可选行业，避免硬编码。"""
    try:
        from data.fund.fund_mapping import FUND_MAP
        names = list(FUND_MAP.keys())
        return names if names else ["半导体"]
    except Exception:
        return ["半导体"]


class BaseVoter:
    """单一决策模型。"""

    def __init__(self, name, weight=1.0):
        self.name = name
        self.weight = weight

    def analyze(self, context) -> dict:
        raise NotImplementedError

    def is_available(self) -> bool:
        return True


class DeepSeekVoter(BaseVoter):
    """云端 DeepSeek。"""

    def __init__(self, model=None, weight=1.0):
        if model is None:
            try:
                from models.config import load_model_config
                model = load_model_config()["models"]["decision"]["model"]
            except Exception:
                model = "deepseek-chat"
        super().__init__(f"deepseek:{model}", weight)
        from models.deepseek import DeepSeekModel
        self.client = DeepSeekModel()
        if model:
            self.client.model = model

    FALLBACK_MODELS = ("deepseek-chat", "deepseek-reasoner")

    def analyze(self, context) -> dict:
        """主模型失败（402余额/限流/超时）时逐级降级到备用模型，
        避免整个决策链因单次API故障而永远输出 HOLD。"""
        try:
            return self.client.analyze(context)
        except Exception as e:
            last_err = e
            for fb in self.FALLBACK_MODELS:
                if fb == self.client.model:
                    continue
                try:
                    print(f"  [{self.name}] 失败({e})，降级到 {fb}")
                    self.client.model = fb
                    self.name = f"deepseek:{fb}"
                    return self.client.analyze(context)
                except Exception as e2:
                    last_err = e2
            raise last_err


class OllamaVoter(BaseVoter):
    """本地 Ollama（局域网/本机）。

    CPU 推理慢，故只投喂精简摘要（云端模型才看全量上下文）。
    """

    def __init__(self, model="qwen3.5:4b", base_url="http://localhost:11434",
                 weight=0.6, timeout=600):
        super().__init__(f"ollama:{model}", weight)
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def is_available(self):
        try:
            resp = requests.get(
                f"{self.base_url}/api/tags", timeout=5
            )
            resp.raise_for_status()
            return True
        except requests.RequestException:
            return False

    def _compress(self, context) -> str:
        """把结构化上下文压成紧凑文字（CPU 模型 prefill 慢，输入要短）。"""
        lines = [f"日期: {context.get('date', '')}"]
        for m in context.get("market", []):
            lines.append(
                f"{m['symbol']}: 涨跌{m['change_1d']}% 趋势{m['trend']} "
                f"MA5={m['ma5']} MA20={m['ma20']} "
                f"上日净值{m['prev_nav']}"
            )
        for m in context.get("market", []):
            if m.get("anomaly"):
                lines.append(f"⚠️ {m['symbol']} 净值异常: {m['anomaly'][:60]}")
        lessons = context.get("lessons") or []
        if lessons:
            lines.append("历史经验教训:")
            for l in lessons[-3:]:
                lines.append(f"- {l.get('date', '')}: {l.get('lesson', '')}")
        acc = context.get("account", {})
        lines.append(
            f"账户: 现金{acc.get('cash')} 总资产{acc.get('total_asset')} "
            f"持仓{acc.get('positions') or '无'}"
        )
        return "\n".join(lines)

    def analyze(self, context) -> dict:
        _inds = "、".join(_industry_names())
        system = (
            f"你是基金决策模型。可选行业：{_inds}。\n"
            "每次要横向比较全部可选行业，选择相对最强的行业，"
            "避免长期集中于单一行业；当前持仓转弱时应调仓到更强行业。\n"
            "规则：单基金≤40%，单次调整≤20%，清仓(SELL+target_position=0)不受20%限制，"
            "最低申购100元。\n"
            "只输出一行 JSON，不要解释、不要复述规则、不要代码块：\n"
            '{"action":"BUY","target":"通信","target_position":20,"confidence":0.7,"reason":"理由","risk":"风险"}'
        )
        user = self._compress(context)
        resp = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "stream": False,
                "format": "json",  # 强制 JSON 输出
                "think": False,  # 关思考链，否则 qwen3 会超长推理
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "options": {
                    "num_predict": 256,   # 限制输出长度
                    "temperature": 0.2,
                },
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        return _extract_json(content)


def _extract_json(text):
    """从模型输出中提取 JSON 对象（容忍 markdown 包裹）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"模型输出无 JSON: {text[:200]}")
    return json.loads(text[start:end + 1])


class EnsembleDecision:
    """多模型投票聚合。"""

    def __init__(self, voters, outlier_threshold=15.0,
                 min_agreeing=1):
        self.voters = voters
        self.outlier_threshold = outlier_threshold
        self.min_agreeing = min_agreeing
        self.last_votes = []

    def decide(self, context, position_of=None):
        """返回 (Decision, votes)。单模型失败不影响整体。

        position_of: callable(symbol)->当前仓位%，用于按目标行业判断 BUY/SELL。
        若为 None，退化为按聚合目标绝对仓位 0 判断。
        """
        votes = []
        for voter in self.voters:
            try:
                if not voter.is_available():
                    votes.append({
                        "voter": voter.name,
                        "ok": False,
                        "error": "模型不可用",
                    })
                    continue
                raw = voter.analyze(context)
                d = parse_decision(raw)
                votes.append({
                    "voter": voter.name,
                    "ok": True,
                    "weight": voter.weight,
                    "decision": d,
                })
            except Exception as e:  # 单个模型失败不拖垮整体
                votes.append({
                    "voter": voter.name,
                    "ok": False,
                    "error": str(e)[:200],
                })
        self.last_votes = votes

        good = [v for v in votes if v["ok"]]
        if not good:
            return create_decision(
                "HOLD", "半导体", 0.0, 0.0,
                "所有模型均失败，保持观望", "模型不可用",
                source="ensemble",
                current_position=0.0,
            ), votes

        positions = [v["decision"].target_position for v in good]

        # 离群剔除：偏离中位数超过阈值的模型剔除
        if len(positions) >= 3:
            median = statistics.median(positions)
            kept = [
                v for v in good
                if abs(v["decision"].target_position - median)
                <= self.outlier_threshold
            ]
            if kept:
                good = kept
                positions = [v["decision"].target_position for v in good]

        # 置信度加权聚合
        total_w = sum(v["weight"] * v["decision"].confidence for v in good)
        if total_w > 0:
            target = sum(
                v["weight"] * v["decision"].confidence
                * v["decision"].target_position
                for v in good
            ) / total_w
            confidence = sum(
                v["weight"] * v["decision"].confidence
                for v in good
            ) / sum(v["weight"] for v in good)
        else:
            target = statistics.mean(positions)
            confidence = 0.0

        target = round(target, 2)

        # 目标标的：多数票（出现最多的 target）
        targets = [v["decision"].target for v in good if v["decision"].target]
        target_name = (
            max(set(targets), key=targets.count)
            if targets else "半导体"
        )

        # 按目标行业当前仓位判断 BUY/SELL（修复：不再用账户最大持仓判断）
        if callable(position_of):
            try:
                current_position = float(position_of(target_name) or 0.0)
            except Exception:
                current_position = 0.0
        else:
            current_position = 0.0

        if abs(target - current_position) < 1.0:
            action = "HOLD"
        elif target > current_position:
            action = "BUY"
        else:
            action = "SELL"

        reasons = " | ".join(
            f"[{v['voter']}] {v['decision'].reason}"
            for v in good if v["decision"].reason
        )[:500]
        risks = " | ".join(
            f"[{v['voter']}] {v['decision'].risk}"
            for v in good if v["decision"].risk
        )[:300]

        decision = create_decision(
            action, target_name, target, confidence,
            reasons or "多模型综合", risks or "未知",
            source="ensemble",
            current_position=current_position,
        )
        return decision, votes
