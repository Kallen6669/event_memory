"""测试目录入口，复用本应用自己的 OAuth 实现，不读取 Codex 凭据。"""
from event_memory.chatgpt_auth import (
    DIRECT_SCOPE, ProbeError, dpapi, load_profile, login, main, probe,
    request_json, save_profile, validate_identity,
)

if __name__ == "__main__":
    raise SystemExit(main())
