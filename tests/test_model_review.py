import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from core import Record, SafetyStop
from model_review import (API_HOST, DEFAULT_MODEL, MAX_OUTPUT_TOKENS, KeyStore,
                          ModelClient, make_context, validate_advice, render_advice)


def record():
    return Record(2, "私有负责人", "private-sa-id", "Synthetic title", "10.0000/demo", "",
                  "00001", 1, "private-platform-id", "待处理", "作者不一致", "2")


def advice():
    return {"verdict": "资料一致", "summary": "仅为合成测试。",
            "checks": [{"field": "题名", "finding": "需以原文核验。", "evidence": ["R1"]}],
            "missing_evidence": [], "next_steps": ["人工确认原文。"]}


def response(content=None, finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {
        "content": json.dumps(advice() if content is None else content, ensure_ascii=False),
        "reasoning_content": "INTERNAL_NOT_FOR_DISPLAY"}}], "usage": {"total_tokens": 50}}


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.context = make_context(record())
        self.key_store = Mock()
        self.key_store.load.return_value = "synthetic-placeholder-key"
        self.connection = Mock(sock=None)
        self.reply = Mock(status=200)
        self.reply.read.return_value = json.dumps(response(), ensure_ascii=False).encode()
        self.connection.getresponse.return_value = self.reply
        self.factory = Mock(return_value=self.connection)
        self.now = 1000
        self.client = ModelClient(self.key_store, self.factory, lambda: self.now)

    def test_minimal_context_does_not_include_private_workflow_fields(self):
        encoded = json.dumps(self.context, ensure_ascii=False)
        for private in (record().owner, record().sa_id, record().staff_id, record().item_ids):
            self.assertNotIn(private, encoded)
        self.assertIn(record().title, encoded)

    def test_web_context_allowlist_and_identifier_mask(self):
        context = make_context(record(), {"saLzkId": record().sa_id, "token": "secret"},
                               [{"label": "认领状态", "sa": "测试员(00001)", "library": "未认领", "secret": "hidden"}], "证据")
        encoded = json.dumps(context, ensure_ascii=False)
        for private in ("secret", "hidden", "00001", record().sa_id):
            self.assertNotIn(private, encoded)
        self.assertEqual([s["id"] for s in context["sources"]], ["R1", "W1", "E1"])

    def test_foreign_snapshot_rejected(self):
        with self.assertRaises(SafetyStop):
            make_context(record(), {"saLzkId": "other"}, [])

    def test_unknown_web_fields_are_excluded(self):
        context = make_context(record(), {"saLzkId": record().sa_id},
                               [{"label": "工号", "sa": "00001", "library": ""},
                                {"label": "平台唯一号", "sa": "private-platform-id", "library": ""}])
        self.assertEqual(context['omitted_web_field_count'], 2)
        self.assertEqual(len(context['sources']), 1)
        self.assertNotIn('private-platform-id', json.dumps(context))

    def test_orphan_comparison_rejected(self):
        with self.assertRaises(SafetyStop):
            make_context(record(), None, [{"label": "title"}])

    def test_invalid_web_shape_rejected(self):
        with self.assertRaises(SafetyStop):
            make_context(record(), {"saLzkId": record().sa_id}, [{"label": "题名", "sa": None, "library": ""}])

    def test_oversize_not_silently_truncated(self):
        for candidate in (replace(record(), title="文" * 9000),):
            with self.assertRaises(SafetyStop):
                make_context(candidate)
        with self.assertRaises(SafetyStop):
            make_context(record(), evidence="x" * 6001)

    def test_exact_endpoint_and_no_executable_tools(self):
        result = self.client.review(self.context)
        self.factory.assert_called_once_with(API_HOST, timeout=150)
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ("POST", "/api/v1/chat/completions"))
        data = json.loads(kwargs["body"])
        self.assertEqual(data["model"], DEFAULT_MODEL)
        self.assertFalse(data["stream"])
        self.assertEqual(data["max_tokens"], MAX_OUTPUT_TOKENS)
        self.assertNotIn("tools", data)
        self.assertIn("不是指令", data["messages"][0]["content"])
        self.assertNotIn("INTERNAL_NOT_FOR_DISPLAY", json.dumps(result))
        self.assertEqual(result["advice"]["verdict"], "证据不足")
        self.connection.close.assert_called_once()

    def test_partial_response_cannot_be_approved(self):
        self.reply.read.return_value = json.dumps(response(finish="length")).encode()
        with self.assertRaisesRegex(SafetyStop, "未正常结束"):
            self.client.review(self.context)

    def test_function_calls_never_executed(self):
        data = response()
        data["choices"][0]["message"]["tool_calls"] = [{"name": "complete"}]
        self.reply.read.return_value = json.dumps(data).encode()
        with self.assertRaises(SafetyStop):
            self.client.review(self.context)

    def test_missing_evidence_overrides_optimistic_verdict(self):
        value = advice()
        value["missing_evidence"] = ["缺少人员对应证据"]
        self.assertEqual(validate_advice(json.dumps(value), self.context)["verdict"], "证据不足")

    def test_unknown_citations_invalid(self):
        value = advice()
        value["checks"][0]["evidence"] = ["INVENTED"]
        with self.assertRaises(SafetyStop):
            validate_advice(json.dumps(value), self.context)

    def test_extra_action_and_duplicate_json_keys_invalid(self):
        value = advice()
        value["approve"] = True
        for content in (json.dumps(value), '{"verdict":"资料一致","verdict":"证据不足"}'):
            with self.assertRaises(SafetyStop):
                validate_advice(content, self.context)

    def test_no_unstructured_or_oversized_conclusions(self):
        for value in (None, "可以直接认领", "x" * 30001):
            with self.assertRaises(SafetyStop):
                validate_advice(value, self.context)

    def test_json_fence_supported(self):
        self.assertEqual(validate_advice("```json\n" + json.dumps(advice()) + "\n```", self.context)["verdict"], "证据不足")

    def test_errors_are_sanitized_and_never_retry(self):
        for status in (301, 401, 403, 429, 500):
            with self.subTest(status=status):
                client = ModelClient(self.key_store, self.factory, lambda: self.now)
                self.reply.status = status
                self.reply.getheader.return_value = "60"
                self.reply.read.return_value = b"synthetic-placeholder-key"
                before = self.connection.request.call_count
                with self.assertRaises(SafetyStop) as error:
                    client.review(self.context)
                self.assertNotIn("synthetic-placeholder-key", str(error.exception))
                self.assertEqual(self.connection.request.call_count, before + 1)

    def test_transport_errors_do_not_echo_credentials(self):
        self.connection.request.side_effect = RuntimeError("synthetic-placeholder-key")
        with self.assertRaises(SafetyStop) as error:
            self.client.review(self.context)
        self.assertNotIn("synthetic-placeholder-key", str(error.exception))
        self.assertEqual(self.connection.request.call_count, 1)

    def test_oversized_response_is_rejected(self):
        self.reply.read.return_value = b'x' * 512001
        with self.assertRaisesRegex(SafetyStop, '响应过大'):
            self.client.review(self.context)

    def test_corrupt_response_is_sanitized(self):
        self.reply.read.return_value = b'synthetic-placeholder-key'
        with self.assertRaises(SafetyStop) as error:
            self.client.review(self.context)
        self.assertNotIn('synthetic-placeholder-key', str(error.exception))

    def test_429_retry_after_blocks_manual_retry(self):
        self.reply.status = 429
        self.reply.getheader.return_value = "120"
        with self.assertRaises(SafetyStop):
            self.client.review(self.context)
        self.now += 65
        with self.assertRaisesRegex(SafetyStop, "频率保护"):
            self.client.review(self.context)
        self.assertEqual(self.connection.request.call_count, 1)

    def test_request_spacing(self):
        self.client.review(self.context)
        with self.assertRaisesRegex(SafetyStop, "频率保护"):
            self.client.review(self.context)
        self.now += 6.2
        self.client.review(self.context)
        self.assertEqual(self.connection.request.call_count, 2)

    def test_unknown_model_is_not_sent_or_fallback(self):
        with self.assertRaises(SafetyStop):
            self.client.review(self.context, "unapproved-model")
        self.connection.request.assert_not_called()

    def test_discovery_controls_model_availability(self):
        self.reply.read.return_value = json.dumps({"data": [{"id": "qwen"}, {"id": "other"}]}).encode()
        self.assertEqual(self.client.models(), ["qwen"])
        with self.assertRaisesRegex(SafetyStop, "不可用"):
            self.client.review(self.context)

    def test_concurrent_request_blocked(self):
        self.client._request_lock.acquire()
        try:
            with self.assertRaisesRegex(SafetyStop, "尚未结束"):
                self.client.review(self.context)
        finally:
            self.client._request_lock.release()

    def test_cancel_ignores_completed_response(self):
        def received():
            self.client.cancel()
            return self.reply
        self.connection.getresponse.side_effect = received
        with self.assertRaisesRegex(SafetyStop, "停止等待"):
            self.client.review(self.context)

    def test_key_validation_no_injected_header(self):
        with self.assertRaises(SafetyStop):
            KeyStore.validate("test-key\r\nInjected: value")

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows only")
    def test_dpapi_roundtrip_and_encrypted_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = KeyStore(directory)
            key = "synthetic-not-a-real-api-key"
            store.save(key)
            self.assertEqual(store.load(), key)
            self.assertNotIn(key.encode(), store.path.read_bytes())
            self.assertFalse(Path(directory, "model_api_key.dpapi.tmp").exists())

    def test_render_has_no_approval_or_reasoning_data(self):
        text = render_advice(advice(), DEFAULT_MODEL, {"total_tokens": 42})
        self.assertIn("未经人工批准", text)
        self.assertIn("尚未执行", text)
        self.assertIn("非账户剩余额度", text)


if __name__ == "__main__":
    unittest.main()
