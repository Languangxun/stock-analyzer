import os
import json
import re
import time

from dotenv import load_dotenv
from openai import OpenAI

from models.base import BaseModel
from models.prompt import SYSTEM_PROMPT
from models.config import load_model_config


load_dotenv()

REQUEST_TIMEOUT = 60
API_MAX_RETRIES = 2
PARSE_MAX_RETRIES = 2


def extract_json(text):
    """从模型输出中稳健地提取 JSON（容忍 markdown 围栏和多余文字）。"""
    if text is None:
        raise ValueError("空响应")
    text = text.strip()
    # 去掉 ```json ... ``` 围栏
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 兜底：截取第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError(f"无法解析 JSON: {text[:120]}")


class DeepSeekModel(BaseModel):

    def __init__(self, system_prompt=None):

        config = load_model_config()

        decision_config = config["models"]["decision"]

        self.model = decision_config["model"]

        self.base_url = (
            decision_config.get("base_url")
            or os.getenv("DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com"
        )

        self.temperature = 0.2

        self.system_prompt = system_prompt or SYSTEM_PROMPT

        api_key = os.getenv(
            "DEEPSEEK_API_KEY"
        )

        if not api_key:

            raise ValueError(
                "DEEPSEEK_API_KEY 未配置"
            )

        self.client = OpenAI(

            api_key=api_key,

            base_url=self.base_url,

            timeout=REQUEST_TIMEOUT,

            max_retries=API_MAX_RETRIES,

        )



    def _chat(self, context):

        response = self.client.chat.completions.create(

            model=self.model,

            temperature=self.temperature,

            messages=[

                {
                    "role": "system",
                    "content": self.system_prompt
                },

                {
                    "role": "user",
                    "content": json.dumps(
                        context,
                        ensure_ascii=False
                    )
                }

            ]

        )

        content = response.choices[0].message.content

        return extract_json(content)



    def analyze(self, context):

        """带重试的分析：网络异常或 JSON 解析失败时重试。"""

        last_err = None

        for attempt in range(PARSE_MAX_RETRIES + 1):

            try:

                return self._chat(context)

            except Exception as e:

                last_err = e

                if attempt < PARSE_MAX_RETRIES:

                    time.sleep(2 * (attempt + 1))

        raise RuntimeError(

            f"DeepSeek 决策失败（已重试 {PARSE_MAX_RETRIES} 次）: {last_err}"

        )
