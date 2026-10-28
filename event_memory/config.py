"""仅从项目内配置读取密钥，禁止把配置值写入错误或日志。"""

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .errors import MemoryError

ROOT = Path(__file__).resolve().parent.parent


def env_values(path: Path | None = None) -> dict[str, str]:
    values = {}
    file = path or ROOT / ".env"
    if file.exists():
        for row in file.read_text(encoding="utf-8-sig").splitlines():
            if row.strip() and not row.lstrip().startswith("#") and "=" in row:
                name, value = row.split("=", 1)
                values[name.strip()] = value.strip().strip('"').strip("'")
    return {**values, **os.environ}


@dataclass(frozen=True)
class ModelConfig:
    key: str = field(repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    max_tokens: int = 6000
    timeout: int = 180
    input_cny_per_million: Decimal = Decimal("2")
    cached_input_cny_per_million: Decimal = Decimal("0.04")
    output_cny_per_million: Decimal = Decimal("8")

    @classmethod
    def load(cls) -> "ModelConfig":
        raise MemoryError("provider_retired", "DeepSeek 新调用已停用；该配置仅用于历史账务和回读。")


@dataclass(frozen=True)
class ChatGPTConfig:
    model: str
    principal_hash: str
    provider: str = "chatgpt_plan"
    timeout: int = 180
    max_calls_per_job: int = 128

    @classmethod
    def load(cls) -> "ChatGPTConfig":
        from .chatgpt_auth import DIR, ProbeError, atomic_json, authorized_profile, principal_hash
        import json
        try:
            if env_values().get("MODEL_PROVIDER", "chatgpt_plan") != "chatgpt_plan":
                raise MemoryError("provider_retired", "当前仅启用已授权 ChatGPT 套餐路径，不回退 DS 或 API Key。")
            profile = authorized_profile()
            proof_path = DIR / "verified-inference.json"
            proof = json.loads((proof_path if proof_path.exists() else DIR / "report.json").read_text(encoding="utf-8"))
            model = env_values().get("OPENAI_MODEL", proof.get("model", ""))
            identity = principal_hash(profile)
            if proof.get("status") != "subscription_inference_verified" or proof.get("principal_hash") != identity:
                raise MemoryError("model_not_verified", "当前账号还没有本应用的完整推理证明，请先运行套餐 probe。")
            if model != proof.get("model"):
                raise MemoryError("model_not_verified", "所选模型未通过当前账号的最小调用测试。")
            if not proof_path.exists():
                atomic_json(proof_path, proof)
            return cls(model=model, principal_hash=identity)
        except ProbeError as exc:
            raise MemoryError(exc.code, str(exc)) from None
        except (OSError, ValueError, KeyError):
            raise MemoryError("login_required", "请先运行 python -m tests.chatgpt_plan_probe login 和 probe。") from None


def configured_budget() -> Decimal | None:
    if not env_values().get("MODEL_BUDGET_CNY"):
        return None
    try:
        amount = Decimal(env_values().get("MODEL_BUDGET_CNY", ""))
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
        return amount
    except InvalidOperation as exc:
        raise MemoryError("missing_budget", "MODEL_BUDGET_CNY 须为非负有限数；该配置仅用于历史现金账务，GPT套餐可不配置。") from exc


def default_ledger_path() -> Path:
    current = ROOT / ".runtime" / "model_calls.sqlite"
    legacy = ROOT / ".runtime" / "budget.sqlite"
    return current if current.exists() or not legacy.exists() else legacy
