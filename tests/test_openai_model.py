"""现役 GPT 套餐：流终态、账号绑定、缓存、未知调用和非现金记账。"""

import io
import json
import unittest
import urllib.error
import ssl
import http.client
from decimal import Decimal
from unittest.mock import patch

from event_memory.chatgpt_auth import principal_hash
from event_memory.config import ChatGPTConfig
from event_memory.errors import MemoryError
from event_memory.model import Ledger
from event_memory.openai_model import ChatGPTModel, TrackedHTTPSConnection


def stream_reply(status="completed"):
    text = '{"events":[],"unclassified":[]}'
    response = {"id":"synthetic-response","status":status,"model":"gpt-6.1-sol",
                "usage":{"input_tokens":100,"output_tokens":50},
                "output":[{"type":"message","content":[{"type":"output_text","text":text}]}]}
    events = [{"type":"response.output_text.delta","delta":text},
              {"type":"response."+status,"response":response}]
    return io.BytesIO(b"".join(b"data: "+json.dumps(e).encode()+b"\n\n" for e in events))


class OpenAIModelTests(unittest.TestCase):
    def setUp(self):
        self.ledger=Ledger(":memory:",Decimal("3"))
        self.profile={"issuer":"https://auth.openai.com","client_id":"synthetic-client",
                      "subject":"synthetic-account","access_token":"synthetic-private-token"}
        self.config=ChatGPTConfig(model="gpt-6.1-sol",principal_hash=principal_hash(self.profile))
        self.model=ChatGPTModel(self.config,self.ledger)

    def tearDown(self):self.ledger.close()

    def test_complete_stream_and_replay_do_not_reissue_network_or_cash_charge(self):
        messages=[{"role":"system","content":"只输出 JSON"},{"role":"user","content":"合成材料"}]
        with patch("event_memory.openai_model.authorized_profile",return_value=self.profile), \
             patch("urllib.request.OpenerDirector.open",return_value=stream_reply()) as network:
            first=self.model.generate("call","job",messages)
            second=self.model.generate("call","job",messages)
        self.assertEqual(first,second)
        self.assertEqual(network.call_count,1)
        body=json.loads(network.call_args.args[0].data)
        self.assertEqual(network.call_args.args[0].full_url,"https://api.openai.com/v1/responses")
        self.assertEqual(set(body),{"model","instructions","input","store","stream"})
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        self.assertEqual(self.ledger.summary()["estimated_spent_cny"],0)
        self.assertEqual(self.ledger.summary()["subscription"]["input_tokens"],100)
        self.assertEqual(self.ledger.summary()["subscription"]["output_tokens"],50)
        self.assertNotIn(self.profile["access_token"],self.ledger.get("call")["request_payload"])

    def test_unknown_result_is_not_retried_or_switched_to_other_billing(self):
        with patch("event_memory.openai_model.authorized_profile",return_value=self.profile), \
             patch("urllib.request.OpenerDirector.open",side_effect=TimeoutError) as network:
            for _ in range(2):
                with self.assertRaises(MemoryError) as error:self.model.generate("call","job",[])
                self.assertEqual(error.exception.code,"unknown_call")
        self.assertEqual(network.call_count,1)
        self.assertEqual(self.ledger.summary()["subscription"]["unknown_calls"],1)

    def test_received_response_rejects_changed_messages_before_any_network(self):
        messages=[{"role":"user","content":"原请求"}]
        with patch("event_memory.openai_model.authorized_profile",return_value=self.profile), \
             patch("urllib.request.OpenerDirector.open",return_value=stream_reply()) as network:
            self.model.generate("call","job",messages)
            with self.assertRaises(MemoryError) as error:
                self.model.generate("call","job",[{"role":"user","content":"不同请求"}])
            self.assertEqual(error.exception.code,"request_conflict")
            self.assertEqual(self.model.saved_messages("call","job"), messages)
        self.assertEqual(network.call_count,1)

    def test_changed_candidates_do_not_hide_or_resend_an_unknown_request(self):
        with patch("event_memory.openai_model.authorized_profile",return_value=self.profile), \
             patch("urllib.request.OpenerDirector.open",side_effect=TimeoutError) as network:
            with self.assertRaises(MemoryError):self.model.generate("call","job",[])
            with self.assertRaises(MemoryError) as error:
                self.model.generate("call","job",[{"role":"user","content":"排序修复后的候选"}])
        self.assertEqual(error.exception.code,"unknown_call")
        self.assertEqual(self.ledger.get("call")["state"],"unknown")
        self.assertEqual(network.call_count,1)

    def test_incomplete_response_is_saved_but_never_success(self):
        with patch("event_memory.openai_model.authorized_profile",return_value=self.profile), \
             patch("urllib.request.OpenerDirector.open",return_value=stream_reply("incomplete")):
            with self.assertRaises(MemoryError) as error:self.model.generate("call","job",[])
        self.assertEqual(error.exception.code,"model_incomplete")
        self.assertEqual(self.ledger.get("call")["state"],"received")

    def test_missing_terminal_stream_remains_unknown(self):
        with patch("event_memory.openai_model.authorized_profile",return_value=self.profile), \
             patch("urllib.request.OpenerDirector.open",return_value=io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n')):
            with self.assertRaises(MemoryError):self.model.generate("call","job",[])
        self.assertEqual(self.ledger.get("call")["state"],"unknown")
        diagnostic=json.loads(self.ledger.get("call")["diagnostic"])
        self.assertEqual(diagnostic["phase"],"response_stream")

    def test_connection_diagnostics_do_not_expose_error_text_or_allow_retry(self):
        failure=urllib.error.URLError(ConnectionRefusedError(10061,"不应记录的敏感原始错误文本"))
        with patch("event_memory.openai_model.authorized_profile",return_value=self.profile), \
             patch("urllib.request.OpenerDirector.open",side_effect=failure) as network:
            for _ in range(2):
                with self.assertRaises(MemoryError):self.model.generate("call","job",[])
        diagnostic=json.loads(self.ledger.get("call")["diagnostic"])
        self.assertEqual(diagnostic["phase"],"connection_or_headers")
        self.assertEqual(diagnostic["cause_type"],"ConnectionRefusedError")
        self.assertNotIn("敏感",self.ledger.get("call")["diagnostic"])
        self.assertEqual(network.call_count,1)

    def test_tls_failure_before_request_can_resume_with_same_identity(self):
        with patch("event_memory.openai_model.authorized_profile",return_value=self.profile), \
             patch("http.client.HTTPSConnection.connect",side_effect=ssl.SSLEOFError("合成握手失败")):
            with self.assertRaises(MemoryError) as error:self.model.generate("call","job",[])
        self.assertEqual(error.exception.code,"request_not_sent")
        row=self.ledger.get("call")
        self.assertEqual(row["state"],"rejected")
        self.assertFalse(json.loads(row["diagnostic"])["request_started"])
        with patch("event_memory.openai_model.authorized_profile",return_value=self.profile), \
             patch("urllib.request.OpenerDirector.open",return_value=stream_reply()) as network:
            self.model.generate("call","job",[])
        self.assertEqual(network.call_count,1)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM call_retries').fetchone()[0],1)

    def test_write_failure_is_never_classified_as_before_request(self):
        trace={"phase":"unobserved","request_started":False}
        connection=TrackedHTTPSConnection("example.invalid",trace=trace)
        connection.sock=object()
        with patch("http.client.HTTPConnection.send",side_effect=ssl.SSLEOFError("发送时中断")):
            with self.assertRaises(ssl.SSLEOFError):connection.send(b"synthetic")
        self.assertTrue(trace["request_started"])
        self.assertEqual(trace["phase"],"request_write")

    def test_truncated_http_body_is_saved_as_unknown_not_left_pending(self):
        class BrokenStream(io.BytesIO):
            def __iter__(self):
                raise http.client.IncompleteRead(b"",10)
        with patch("event_memory.openai_model.authorized_profile",return_value=self.profile), \
             patch("urllib.request.OpenerDirector.open",return_value=BrokenStream()):
            with self.assertRaises(MemoryError) as error:self.model.generate("call","job",[])
        self.assertEqual(error.exception.code,"unknown_call")
        self.assertEqual(self.ledger.get("call")["state"],"unknown")

    def test_account_change_stops_before_any_inference(self):
        other={**self.profile,"subject":"another-account"}
        with patch("event_memory.openai_model.authorized_profile",return_value=other), \
             patch("urllib.request.OpenerDirector.open") as network:
            with self.assertRaises(MemoryError) as error:self.model.generate("call","job",[])
        self.assertEqual(error.exception.code,"account_changed")
        network.assert_not_called()

    def test_provider_rejection_does_not_fall_back(self):
        with patch("event_memory.openai_model.authorized_profile",return_value=self.profile), \
             patch("urllib.request.OpenerDirector.open",side_effect=urllib.error.HTTPError("https://api.openai.com/v1/responses",403,"forbidden",{},None)) as network:
            with self.assertRaises(MemoryError) as error:self.model.generate("call","job",[])
        self.assertEqual(error.exception.code,"subscription_rejected")
        self.assertEqual(network.call_count,1)
        self.assertEqual(self.ledger.get("call")["billing_path"],"chatgpt_plan")

    def test_job_limit_is_not_unlimited_subscription_and_old_cash_is_unchanged(self):
        payload={"model":"gpt-6.1-sol"}
        identity=self.config.principal_hash
        self.ledger.reserve_subscription("one","job",payload,identity,1)
        with self.assertRaises(MemoryError) as error:self.ledger.reserve_subscription("two","job",payload,identity,1)
        self.assertEqual(error.exception.code,"call_limit")
        self.assertEqual(self.ledger.summary()["cap_cny"],3)

    def test_cleared_subscription_response_cannot_be_reintroduced(self):
        self.ledger.reserve_subscription("call","job",{"model":"gpt-6.1-sol"},self.config.principal_hash,10)
        self.ledger.unknown("call")
        self.ledger.purge_jobs(["job"])
        self.ledger.reconcile("call",{"response":{"status":"completed","output":[]}})
        self.assertIsNone(self.ledger.get("call")["response"])
        self.assertIsNone(self.ledger.get("call")["request_payload"])
