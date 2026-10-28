"""ChatGPT 套餐的公开 Responses 适配；无 API Key、无付费回退。"""

import json
import http.client
import time
import urllib.error
import urllib.request

from .chatgpt_auth import API, ProbeError, authorized_profile, principal_hash
from .config import ChatGPTConfig
from .errors import MemoryError
from .model import Ledger, NoRedirect
from .store import dumps


class TrackedHTTPSConnection(http.client.HTTPSConnection):
    """只记录发送阶段，不记录凭据或正文；代理CONNECT不算模型请求发送。"""
    def __init__(self, *args, trace, **kwargs):
        self.trace = trace
        super().__init__(*args, **kwargs)

    def connect(self):
        self.trace.update(phase="connect", in_connect=True)
        try:
            super().connect()
        finally:
            self.trace["in_connect"] = False
        self.trace["phase"] = "connected"

    def send(self, data):
        if self.trace.get("in_connect"):
            return super().send(data)
        if self.sock is None and self.auto_open:
            self.connect()
        self.trace.update(phase="request_write", request_started=True)
        return super().send(data)

    def getresponse(self):
        self.trace["phase"] = "response_headers"
        return super().getresponse()


class TrackedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    def https_open(self, req):
        def connection(*args, **kwargs):
            return TrackedHTTPSConnection(*args, trace=self.trace, **kwargs)
        return self.do_open(connection, req, context=self._context)


class ChatGPTModel:
    def __init__(self, config: ChatGPTConfig, ledger: Ledger):
        self.config, self.ledger = config, ledger

    def replay(self, call_id: str, job_id: str):
        row = self.ledger.get(call_id)
        if not row or row["state"] != "received":
            return None
        if row["job_id"] != job_id or row["model"] != self.config.model or row["billing_path"] != "chatgpt_plan" or row["principal_hash"] != self.config.principal_hash:
            raise MemoryError("request_conflict", "原结果不属于本作业、模型或当前授权账号。")
        if row["response"] is None:
            raise MemoryError("cleared", "原模型结果已经清除，不允许通过重发恢复。")
        return self.decode(json.loads(row["response"]))

    @staticmethod
    def payload(model: str, messages: list[dict]) -> dict:
        instructions = []
        inputs = []
        for message in messages:
            if message["role"] == "system":
                instructions.append(message["content"])
            elif message["role"] in {"user", "assistant", "developer"}:
                inputs.append({"role": message["role"], "content": message["content"]})
            else:
                raise MemoryError("unsupported_message", "当前套餐路径不支持这类消息。")
        # 套餐预览不接受 max_output_tokens、temperature、metadata 等字段。
        return {"model": model, "instructions": "\n\n".join(instructions),
                "input": inputs, "store": False, "stream": True}

    def generate(self, call_id: str, job_id: str, messages: list[dict]) -> dict:
        saved = self.replay(call_id, job_id)
        if saved is not None:
            return saved
        try:
            profile = authorized_profile()
        except ProbeError as exc:
            raise MemoryError(exc.code, str(exc)) from None
        if principal_hash(profile) != self.config.principal_hash:
            raise MemoryError("account_changed", "授权账号已切换，不向另一个账号继续发送本作业材料。")
        payload = self.payload(self.config.model, messages)
        cached = self.ledger.reserve_subscription(call_id, job_id, payload, self.config.principal_hash, self.config.max_calls_per_job)
        if cached:
            if cached["response"] is None:
                raise MemoryError("cleared", "模型结果已清除。")
            return self.decode(json.loads(cached["response"]))
        request = urllib.request.Request(API + "/responses", data=dumps(payload).encode("utf-8"), method="POST",
                                         headers={"Authorization": "Bearer " + profile["access_token"], "Content-Type": "application/json"})
        started = time.monotonic()
        stream_type = None
        opened = False
        trace = {"phase": "unobserved", "request_started": False}
        try:
            with urllib.request.build_opener(NoRedirect(), TrackedHTTPSHandler(trace)).open(request, timeout=self.config.timeout) as stream:
                opened = True
                headers = getattr(stream, "headers", None)
                stream_type = headers.get("Content-Type") if headers else None
                response = self.consume(stream)
            # 金额为零仅表示此条不走人民币 API 计费；套餐 token 与状态仍独立保存。
            self.ledger.received(call_id, response, 0, time.monotonic()-started)
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in {408, 409}:
                self.ledger.rejected(call_id)
                messages = {401: "本应用授权已失效，请重新登录。", 403: "账号、地区、工作区或应用权限未获准。",
                            429: "ChatGPT 套餐或本应用用量限制已达到。"}
                raise MemoryError("subscription_rejected", messages.get(exc.code, "套餐请求参数被官方拒绝。") + f"（HTTP {exc.code}）未回退 DS 或 API Key。") from None
            self.ledger.unknown(call_id)
            raise MemoryError("unknown_call", f"套餐调用返回 HTTP {exc.code}，结果待核对，不自动重发。") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError, TypeError, KeyError) as exc:
            cause = getattr(exc, "reason", exc)
            diagnostic = {"category": type(exc).__name__, "content_type": stream_type,
                                         "cause_type": type(cause).__name__,
                                         "errno": getattr(cause, "errno", None),
                                         "phase": "response_stream" if opened else "connection_or_headers",
                                         "transport_phase": trace["phase"],
                                         "request_started": trace["request_started"], "automatic_retry": False}
            if trace["phase"] == "connect" and not trace["request_started"]:
                self.ledger.not_sent(call_id, diagnostic)
                raise MemoryError("request_not_sent", "HTTPS连接建立失败，已确认模型请求尚未发送；可以按原身份恢复。") from None
            self.ledger.unknown(call_id, diagnostic)
            raise MemoryError("unknown_call", "套餐调用连接或流解析中断，已保留原请求身份；未知结果不自动重发。") from None
        return self.decode(response)

    @staticmethod
    def consume(stream) -> dict:
        chunks, data_lines = [], []
        total = 0
        for raw in stream:
            total += len(raw)
            if total > 4_000_000:
                raise ValueError("模型输出超过本地保护范围")
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                if data == "[DONE]":
                    continue
                event = json.loads(data)
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    chunks.append(event.get("delta", ""))
                elif kind in {"response.completed", "response.failed", "response.incomplete"}:
                    response = event.get("response", {})
                    response.setdefault("status", kind.split(".")[1])
                    if "output" not in response:
                        response["output"] = []
                    if not response.get("output") and chunks:
                        response["output"] = [{"type": "message", "content": [{"type": "output_text", "text": "".join(chunks)}]}]
                    return response
                elif kind == "error":
                    return {"status": "failed", "output": [], "error": {"code": "stream_error"}}
        raise ValueError("流没有完整终态，不能假装成功")

    @staticmethod
    def decode(response: dict) -> dict:
        if response.get("status") != "completed":
            raise MemoryError("model_incomplete", "模型返回失败或不完整终态，原结果已保存，未发布为成功。")
        try:
            content = "".join(part["text"] for item in response["output"] if item.get("type") == "message"
                              for part in item.get("content", []) if part.get("type") == "output_text")
        except (KeyError, TypeError, AttributeError):
            raise MemoryError("invalid_provider_response", "模型响应结构不完整。") from None
        if not content:
            raise MemoryError("invalid_provider_response", "模型未返回可用文本，不作为空列表成功。")
        return {"content": content, "provider_model": response.get("model"),
                "provider_request_id": response.get("id"), "usage": response.get("usage", {}),
                "billing_path": "chatgpt_plan"}


def create_model(ledger: Ledger) -> ChatGPTModel:
    return ChatGPTModel(ChatGPTConfig.load(), ledger)
