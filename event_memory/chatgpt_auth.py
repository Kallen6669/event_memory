"""本应用独立的 ChatGPT 套餐授权、加密凭据及最小探测。

依据官方 Sign in with ChatGPT 文档。运行：
python -m tests.chatgpt_plan_probe status
python -m tests.chatgpt_plan_probe login
python -m tests.chatgpt_plan_probe probe
"""

import argparse
import base64
import ctypes
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from event_memory.config import ROOT, env_values

DIRECT_SCOPE = "chatgpt.tokens.use.direct"
ISSUER = "https://auth.openai.com"
AUTH_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
API = "https://api.openai.com/v1"
DIR = ROOT / ".runtime" / "openai-probe"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
_refresh_lock = threading.RLock()


@contextmanager
def session_lock():
    """串行化同会话的旋转刷新令牌，避免多个进程互相使令牌失效。"""
    DIR.mkdir(parents=True, exist_ok=True)
    with _refresh_lock:
        with (DIR / "session.lock").open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                except OSError:
                    raise ProbeError("session_busy", "另一个进程正在更新本应用授权，请稍后重试。") from None
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ProbeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "拒绝认证请求重定向", headers, fp)


def request_json(url, form=None, token=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=None if form is None else urllib.parse.urlencode(form).encode(), headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # 不打印可能含认证上下文的响应正文。
        raise ProbeError("http_" + str(exc.code), f"官方端点返回 HTTP {exc.code}；未自动重试或切换计费。") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise ProbeError("network_or_response_error", "官方端点连接或响应解析失败，未跳过 TLS 校验。") from None


def dpapi(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise ProbeError("secure_storage_unavailable", "当前测试凭据使用 Windows 用户级 DPAPI，未回退为明文保存。")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ProbeError("secure_storage_error", "Windows 用户级凭据加解密失败。")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def save_profile(profile):
    with session_lock():
        _save_profile_unlocked(profile)


def _save_profile_unlocked(profile):
    DIR.mkdir(parents=True, exist_ok=True)
    target = DIR / "credentials.dpapi"
    temp = target.with_suffix(".tmp")
    temp.write_bytes(dpapi(json.dumps(profile).encode()))
    os.replace(temp, target)


def load_profile():
    path = DIR / "credentials.dpapi"
    if not path.exists():
        raise ProbeError("login_required", "本测试应用尚未得到独立授权，请先运行 login。")
    return json.loads(dpapi(path.read_bytes(), decrypt=True))


def principal_hash(profile):
    identity = [profile.get("issuer"), profile.get("client_id"), profile.get("subject")]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def authorized_profile():
    with session_lock():
        profile = load_profile()
        if DIRECT_SCOPE not in profile.get("scopes", []):
            raise ProbeError("plan_permission_missing", "本应用没有套餐用量权限，请重新审阅官方授权。")
        if time.time() < profile["saved_at"] + int(profile.get("expires_in", 0)) - 60:
            return profile
        if not profile.get("refresh_token"):
            raise ProbeError("login_expired", "本应用授权已过期且无续期权限，请重新登录。")
        # 不发送 scope，保留原 grant；不借用其他客户端的 token 或 client_id。
        pending_path = DIR / "pending-refresh.dpapi"
        generation = hashlib.sha256(profile["refresh_token"].encode()).hexdigest()
        pending = json.loads(dpapi(pending_path.read_bytes(), decrypt=True)) if pending_path.exists() else {}
        if pending.get("generation") != generation or pending.get("principal_hash") != principal_hash(profile):
            tokens = request_json(TOKEN_URL, {"grant_type": "refresh_token", "client_id": profile["client_id"],
                                  "refresh_token": profile["refresh_token"], "resource": API})
            pending = {"tokens": tokens, "generation": generation, "principal_hash": principal_hash(profile),
                       "received_at": time.time()}
            pending_temp = pending_path.with_suffix(".tmp")
            pending_temp.write_bytes(dpapi(json.dumps(pending).encode()))
            os.replace(pending_temp, pending_path)
        tokens = pending["tokens"]
        if not isinstance(tokens.get("access_token"), str) or int(tokens.get("expires_in", 0)) <= 0:
            raise ProbeError("invalid_refresh", "官方续期没有返回完整有效的令牌；旧记录保留，未切换计费。")
        if tokens.get("id_token"):
            identity = validate_identity(tokens["id_token"], profile["client_id"], profile["nonce"], require_nonce=False)
            if identity["sub"] != profile["subject"]:
                raise ProbeError("identity_changed", "续期身份与原注册账号不一致。")
        credential_fields = {k: tokens[k] for k in ("access_token", "refresh_token", "id_token", "expires_in", "token_type") if k in tokens}
        updated = {**profile, **credential_fields, "scopes": tokens.get("scope", " ".join(profile["scopes"])).split(),
                   "saved_at": pending["received_at"]}
        _save_profile_unlocked(updated)
        pending_path.unlink(missing_ok=True)
        if DIRECT_SCOPE not in updated["scopes"]:
            raise ProbeError("plan_permission_missing", "续期后的授权不含套餐权限，已停止推理。")
        return updated


def validate_identity(token, client_id, nonce, require_nonce=True):
    try:
        import jwt
        key = jwt.PyJWKClient(JWKS_URL, timeout=30).get_signing_key_from_jwt(token).key
        required = ["exp", "iat", "sub", "iss", "aud"] + (["nonce"] if require_nonce else [])
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=ISSUER,
                            audience=client_id, leeway=5,
                            options={"require": required})
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise ValueError
        if "nonce" in claims and not secrets.compare_digest(claims["nonce"], nonce):
            raise ValueError
        return claims
    except ImportError:
        raise ProbeError("missing_verifier", "测试需要 PyJWT 与 cryptography 校验官方身份签名，未使用不验证签名的降级方式。") from None
    except Exception as exc:
        raise ProbeError("invalid_identity", "官方身份签名、发行方、客户端、有效期或 nonce 校验失败（" + type(exc).__name__ + "）。") from None


def login(timeout=600):
    DIR.mkdir(parents=True, exist_ok=True)
    host_file = DIR / "host.json"
    host = json.loads(host_file.read_text()) if host_file.exists() else {"ext_agent_host_id": "urn:uuid:" + str(uuid.uuid4())}
    atomic_json(host_file, host)
    registration_file = DIR / "registration.json"
    registered = json.loads(registration_file.read_text()) if registration_file.exists() else {}
    client_id = registered.get("client_id", "dynamic_agent_client")
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    done = threading.Event()
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # 不能把带授权码的回调 URL 写日志。

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            valid_host = self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
            valid_state = len(query.get("state", [])) == 1 and secrets.compare_digest(query["state"][0], state)
            if url.path != "/auth/callback" or not valid_host or not valid_state or done.is_set():
                self.send_error(400, "Invalid callback")
                return
            result.update({k: v[0] for k, v in query.items() if len(v) == 1})
            body = "授权回调已收到。测试程序将验证身份和套餐权限；本页不显示凭据，可以关闭。".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
    params = {"client_id": client_id, "ext_agent_host_id": host["ext_agent_host_id"],
              "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPES,
              "resource": API, "state": state, "nonce": nonce,
              "code_challenge_method": "S256", "code_challenge": challenge}
    if client_id == "dynamic_agent_client":
        params["agent_name_hint"] = "Event Memory API Probe"
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    (DIR / "authorization-url.txt").write_text(url, encoding="utf-8")
    atomic_json(DIR / "login-status.json", {"status": "awaiting_user_authorization", "callback_port": server.server_port,
                                           "application": "Event Memory API Probe", "timeout_seconds": timeout})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("测试应用已准备。请在 OpenAI 官方页面选择你的 Pro 账号，审阅并决定是否授予套餐用量权限。", flush=True)
    print("授权地址保存在 .runtime/openai-probe/authorization-url.txt；不会读取或借用 Codex 登录令牌。", flush=True)
    try:
        if not done.wait(timeout):
            raise ProbeError("authorization_timeout", "等待用户授权已超时；未请求模型，DS 配置未改变。")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    if result.get("error"):
        raise ProbeError("authorization_declined", "官方授权未完成或用户未同意，未请求模型。")
    issued = result.get("client_id", client_id)
    if not issued or issued == "dynamic_agent_client" or client_id != "dynamic_agent_client" and issued != client_id:
        raise ProbeError("invalid_client", "官方回调未提供有效的新客户端身份，或返回了不匹配的身份。")
    if not result.get("code"):
        raise ProbeError("missing_code", "官方回调缺少授权码。")
    atomic_json(registration_file, {"client_id": issued})
    tokens = request_json(TOKEN_URL, {"grant_type": "authorization_code", "client_id": issued,
                          "code": result["code"], "code_verifier": verifier,
                          "redirect_uri": redirect_uri, "resource": API})
    identity = validate_identity(tokens.get("id_token", ""), issued, nonce)
    existing_path = DIR / "credentials.dpapi"
    if existing_path.exists():
        previous = load_profile()
        if previous.get("client_id") == issued and previous.get("subject") != identity["sub"]:
            raise ProbeError("identity_changed", "重新登录身份与此客户端原账号不一致，未替换已保存账号。")
    scopes = tokens.get("scope", "").split()
    profile = {**tokens, "client_id": issued, "ext_agent_host_id": host["ext_agent_host_id"],
               "subject": identity["sub"], "issuer": identity["iss"], "scopes": scopes,
               "saved_at": time.time(), "nonce": nonce}
    save_profile(profile)
    status = {"status": "authorized" if DIRECT_SCOPE in scopes else "identity_only",
              "plan_permission_granted": DIRECT_SCOPE in scopes, "credentials_storage": "Windows DPAPI",
              "ds_configuration_changed": False}
    atomic_json(DIR / "login-status.json", status)
    return status


def probe():
    profile = authorized_profile()
    if DIRECT_SCOPE not in profile.get("scopes", []):
        raise ProbeError("plan_permission_missing", "只获得身份登录权限，没有套餐用量权限，不能推理。")
    if time.time() >= profile["saved_at"] + int(profile.get("expires_in", 0)) - 30:
        raise ProbeError("login_expired", "测试授权已过期，请重新登录；未借用其他应用凭据。")
    catalog = request_json(API + "/models", token=profile["access_token"])
    models = [m for m in catalog.get("models", []) if m.get("visibility") == "list"]
    if not models:
        raise ProbeError("no_models", "所选账号未返回可用模型目录。")
    visible = {m["slug"] for m in models}
    preferred = next((m for m in ("gpt-6.1-sol", "gpt-6-luna") if m in visible), models[0]["slug"])
    payload = {"model": preferred, "input": [{"role": "user", "content": "请只回复：连接成功"}],
               "store": False, "stream": True}
    req = urllib.request.Request(API + "/responses", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + profile["access_token"], "Content-Type": "application/json"})
    completed, parts, response_id = False, [], None
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=90) as response:
            for line in response:
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if not data or data == b"[DONE]":
                    continue
                event = json.loads(data)
                if event.get("type") == "response.output_text.delta":
                    parts.append(event.get("delta", ""))
                elif event.get("type") in {"response.failed", "response.incomplete", "error"}:
                    raise ProbeError("inference_failed", "套餐推理返回失败或不完整；未判定可替换。")
                elif event.get("type") == "response.completed":
                    completed, response_id = True, event.get("response", {}).get("id")
    except urllib.error.HTTPError as exc:
        raise ProbeError("inference_http_" + str(exc.code), f"套餐推理返回 HTTP {exc.code}；目录可见不等于推理可用，DS 未改变。") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise ProbeError("inference_unknown", "推理连接或流解析异常，不自动重试，不据此切换。") from None
    text = "".join(parts).strip()
    if not completed or "连接成功" not in text:
        raise ProbeError("inference_not_verified", "未收到完整推理结束及预期测试文字，不能判定可替换。")
    return {"status": "subscription_inference_verified", "model": preferred,
            "response_completed": completed, "response_id": response_id,
            "billing_path": "chatgpt_plan", "used_api_key": False,
            "principal_hash": principal_hash(profile),
            "ds_configuration_changed": False, "message": "所选账户的本应用套餐推理已通过最小测试；仍需完成项目适配后再切换。"}


def main():
    parser = argparse.ArgumentParser(description="隔离检查 OpenAI 套餐授权；不会直接删除 DS")
    parser.add_argument("command", choices=["status", "login", "probe"], default="status", nargs="?")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    DIR.mkdir(parents=True, exist_ok=True)
    try:
        if args.command == "status":
            values = env_values()
            stored = (DIR / "credentials.dpapi").exists()
            profile = load_profile() if stored else {}
            proof_path = DIR / "verified-inference.json"
            proof = json.loads(proof_path.read_text(encoding="utf-8")) if proof_path.exists() else {}
            verified = bool(stored and proof.get("status") == "subscription_inference_verified" and
                            proof.get("principal_hash") == principal_hash(profile) and DIRECT_SCOPE in profile.get("scopes", []))
            result = {"status": "authorized_previous_probe_verified" if verified else "authorization_required",
                      "openai_api_key_configured": bool(values.get("OPENAI_API_KEY")),
                      "own_app_authorization_stored": stored,
                      "subscription_inference_verified": verified, "live_request_performed": False,
                      "production_provider": values.get("MODEL_PROVIDER", "chatgpt_plan"),
                      "deepseek_configuration_present": any(k.startswith("DEEPSEEK_") and v for k,v in values.items()),
                      "ds_configuration_changed": False,
                      "official_plan_usage_path_documented": True,
                      "message": "这是本地授权与上次完整探测记录的只读状态，不代表重新验证了实时额度；状态检查不修改生产配置。"}
        elif args.command == "login":
            result = login(args.timeout)
        else:
            result = probe()
            atomic_json(DIR / "verified-inference.json", result)
        atomic_json(DIR / "report.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ProbeError as exc:
        result = {"status": "not_verified", "error": exc.code, "message": str(exc), "ds_configuration_changed": False}
        atomic_json(DIR / "report.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
