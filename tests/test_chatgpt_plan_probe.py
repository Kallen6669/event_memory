"""套餐测试边界：不借用登录、不把目录可见误判为推理可用、拒绝无权限请求。"""

import io
import json
import os
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.chatgpt_plan_probe import DIRECT_SCOPE, ProbeError, dpapi, probe, validate_identity


class SSEReply(io.BytesIO):
    def __init__(self, events):
        super().__init__(b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events))


class ChatGPTPlanProbeTests(unittest.TestCase):
    def profile(self, scopes=None):
        return {"scopes": [DIRECT_SCOPE] if scopes is None else scopes,
                "saved_at": time.time(), "expires_in": 3600, "access_token": "synthetic-token"}

    def test_identity_login_alone_cannot_send_inference(self):
        with patch("event_memory.chatgpt_auth.load_profile",return_value=self.profile([])), \
             patch("event_memory.chatgpt_auth.request_json") as network:
            with self.assertRaises(ProbeError) as error:
                probe()
        self.assertEqual(error.exception.code,"plan_permission_missing")
        network.assert_not_called()

    def test_model_catalog_without_completed_inference_does_not_prove_access(self):
        catalog={"models":[{"visibility":"list","slug":"gpt-6.1-sol"}]}
        events=[{"type":"response.output_text.delta","delta":"连接成功"}]
        with patch("event_memory.chatgpt_auth.load_profile",return_value=self.profile()), \
             patch("event_memory.chatgpt_auth.request_json",return_value=catalog), \
             patch("urllib.request.OpenerDirector.open",return_value=SSEReply(events)):
            with self.assertRaises(ProbeError) as error:
                probe()
        self.assertEqual(error.exception.code,"inference_not_verified")

    def test_completed_subscription_response_can_prove_only_the_selected_model(self):
        catalog={"models":[{"visibility":"list","slug":"gpt-6.1-sol"}]}
        events=[{"type":"response.output_text.delta","delta":"连接成功"},
                {"type":"response.completed","response":{"id":"synthetic-request"}}]
        with patch("event_memory.chatgpt_auth.load_profile",return_value=self.profile()), \
             patch("event_memory.chatgpt_auth.request_json",return_value=catalog), \
             patch("urllib.request.OpenerDirector.open",return_value=SSEReply(events)) as network:
            result=probe()
        self.assertEqual(result["status"],"subscription_inference_verified")
        self.assertFalse(result["ds_configuration_changed"])
        request=network.call_args.args[0]
        self.assertEqual(request.full_url,"https://api.openai.com/v1/responses")
        payload=json.loads(request.data)
        self.assertFalse(payload["store"])
        self.assertTrue(payload["stream"])
        self.assertNotIn("max_output_tokens",payload)
        self.assertNotIn("temperature",payload)

    @unittest.skipUnless(os.name=="nt","Windows 用户级加密验证")
    def test_dpapi_keeps_credentials_out_of_plaintext_files(self):
        value=b"synthetic-sensitive-credentials"
        encrypted=dpapi(value)
        self.assertNotIn(value,encrypted)
        self.assertEqual(dpapi(encrypted,decrypt=True),value)

    def test_forged_or_wrong_identity_is_rejected(self):
        try:
            import jwt
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            self.skipTest("可选 OAuth 校验依赖未安装")
        private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        claims={"sub":"synthetic-account","iss":"https://auth.openai.com","aud":"synthetic-client",
                "iat":int(time.time()),"exp":int(time.time())+300,"nonce":"expected-nonce"}
        token=jwt.encode(claims,private,algorithm="RS256",headers={"kid":"synthetic-key"})
        with patch("jwt.PyJWKClient.get_signing_key_from_jwt",return_value=SimpleNamespace(key=private.public_key())):
            verified=validate_identity(token,"synthetic-client","expected-nonce")
            self.assertEqual(verified["sub"],"synthetic-account")
            for client,nonce in (("wrong-client","expected-nonce"),("synthetic-client","wrong-nonce")):
                with self.assertRaises(ProbeError):
                    validate_identity(token,client,nonce)
        other=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        with patch("jwt.PyJWKClient.get_signing_key_from_jwt",return_value=SimpleNamespace(key=other.public_key())):
            with self.assertRaises(ProbeError):
                validate_identity(token,"synthetic-client","expected-nonce")

    def test_refresh_id_token_may_omit_nonce_but_initial_signin_may_not(self):
        try:
            import jwt
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            self.skipTest("可选身份签名库未安装")
        private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        claims={"sub":"same-user","iss":"https://auth.openai.com","aud":"client",
                "iat":int(time.time()),"exp":int(time.time())+300}
        token=jwt.encode(claims,private,algorithm="RS256")
        with patch("jwt.PyJWKClient.get_signing_key_from_jwt",return_value=SimpleNamespace(key=private.public_key())):
            with self.assertRaises(ProbeError):validate_identity(token,"client","nonce")
            self.assertEqual(validate_identity(token,"client","nonce",require_nonce=False)["sub"],"same-user")

    def test_rotating_refresh_is_saved_atomically_and_cannot_override_identity(self):
        from event_memory.chatgpt_auth import authorized_profile
        profile={"issuer":"https://auth.openai.com","client_id":"client","subject":"user",
                 "nonce":"nonce","scopes":[DIRECT_SCOPE],"saved_at":0,"expires_in":1,
                 "access_token":"old-access","refresh_token":"old-refresh"}
        tokens={"access_token":"new-access","refresh_token":"new-refresh","expires_in":3600,
                "id_token":"signed-fixture","subject":"wrong-untrusted-field","client_id":"wrong-field"}
        with tempfile.TemporaryDirectory() as folder, \
             patch("event_memory.chatgpt_auth.DIR",Path(folder)), \
             patch("event_memory.chatgpt_auth.load_profile",return_value=profile), \
             patch("event_memory.chatgpt_auth.request_json",return_value=tokens) as network, \
             patch("event_memory.chatgpt_auth.validate_identity",return_value={"sub":"user"}), \
             patch("event_memory.chatgpt_auth.dpapi",side_effect=lambda b,decrypt=False:b), \
             patch("event_memory.chatgpt_auth._save_profile_unlocked") as saved:
            updated=authorized_profile()
            self.assertEqual(updated["subject"],"user")
            self.assertEqual(updated["client_id"],"client")
            self.assertEqual(updated["refresh_token"],"new-refresh")
            self.assertEqual(saved.call_count,1)
            self.assertEqual(network.call_args.args[1]["grant_type"],"refresh_token")
            self.assertNotIn("scope",network.call_args.args[1])
