"""官方 HTTPS 模型适配与持久化共享预算；不自动重试结果未知的调用。"""

import json
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from .config import ModelConfig
from .errors import MemoryError


def micros(amount: Decimal) -> int:
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def usage_charge(usage: dict, config: ModelConfig | None = None) -> int | None:
    if not isinstance(usage, dict):
        return None
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if type(prompt) is not int or type(completion) is not int or prompt < 0 or completion < 0:
        return None
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    cached = usage.get("prompt_cache_hit_tokens", details.get("cached_tokens", 0))
    if type(cached) is not int or not 0 <= cached <= prompt:
        cached = 0
    input_price = config.input_cny_per_million if config else Decimal("2")
    cached_price = config.cached_input_cny_per_million if config else Decimal("0.04")
    output_price = config.output_cny_per_million if config else Decimal("8")
    return micros((Decimal(prompt-cached)*input_price + Decimal(cached)*cached_price +
                   Decimal(completion)*output_price) / 1_000_000)


from .ledger import Ledger


class DeepSeekModel:
    def __init__(self, config: ModelConfig, ledger: Ledger):
        self.config, self.ledger = config, ledger

    def replay(self, call_id: str, job_id: str) -> dict | None:
        """修正本地校验器后仍复用原响应；调用方必须再次核验当前来源与引用范围。"""
        row = self.ledger.get(call_id)
        if row is None or row["state"] != "received":
            return None
        if row["job_id"] != job_id or row["model"] != self.config.model:
            raise MemoryError("request_conflict", "原响应不属于当前作业或模型。")
        if row["response"] is None:
            raise MemoryError("cleared", "原模型响应已清除，不允许重建。")
        return self._decode_response(json.loads(row["response"]))

    def generate(self, call_id: str, job_id: str, messages: list[dict]) -> dict:
        raise MemoryError("provider_retired", "DeepSeek 新调用已按用户要求停用；保留历史结果与费用，不回退调用。")

    @staticmethod
    def _decode_response(response: dict) -> dict:
        try:
            choice = response["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise MemoryError("truncated_output", "模型未正常结束输出；保留原响应，禁止伪装成功。")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise TypeError
            return {"content": content, "provider_model": response.get("model"),
                    "provider_request_id": response.get("id"), "usage": response.get("usage", {})}
        except (KeyError, IndexError, TypeError, AttributeError):
            raise MemoryError("invalid_provider_response", "官方响应结构不完整，已保存原响应。") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不把认证头和转录随 HTTP 重定向交给另一个地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "拒绝重定向", headers, fp)
