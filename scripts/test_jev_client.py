"""Offline client contract and failure-transition tests."""
import http.client
import json
import os
import time
import unittest
from unittest.mock import patch

from jev_client import (DEFAULT_MODEL, JevAuthError, JevBudgetExceededError, JevClient,
                        JevRateLimitError, JevUnprocessableError, JevError, sanitize_text,
                        JevRedirectError, JevServerError, JevValidationError,
                        batch_questions, estimate_payload_tokens, validate_response)


def answer(ids, score=0.8):
    return {"model": DEFAULT_MODEL, "answers": {qid: {"type": "noul", "noul": score} for qid in ids},
            "usage": {"input_tokens": 12, "output_tokens": 3}}


def question(text="short"):
    return {"type": "noul", "instructions": text, "criteria": {"true": "yes", "false": "no"}}


class TestResponse(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_response(answer(["q"]), DEFAULT_MODEL)["answers"]["q"]["noul"], 0.8)

    def test_invalid_model(self):
        data = answer(["q"])
        data["model"] = "wrong"
        with self.assertRaises(JevValidationError): validate_response(data, DEFAULT_MODEL)

    def test_invalid_probabilities(self):
        for value in (True, False, float("nan"), float("inf"), -0.1, 1.1, "0.5"):
            with self.subTest(value=value):
                data = answer(["q"])
                data["answers"]["q"]["noul"] = value
                with self.assertRaises(JevValidationError): validate_response(data, DEFAULT_MODEL)

    def test_invalid_usage_and_shape(self):
        for usage in ({"input_tokens": True, "output_tokens": 0}, {"input_tokens": -1, "output_tokens": 0}, None):
            data = answer(["q"])
            data["usage"] = usage
            with self.assertRaises(JevValidationError): validate_response(data, DEFAULT_MODEL)
        with self.assertRaises(JevValidationError): validate_response([], DEFAULT_MODEL)

    def test_missing_answer_allowed_at_parser(self):
        self.assertEqual(validate_response(answer([]), DEFAULT_MODEL)["answers"], {})

    def test_single_payload_ceiling(self):
        with self.assertRaises(ValueError): batch_questions("s", {"large": question("x" * 100000)})


class TestExecution(unittest.TestCase):
    def test_model_and_endpoint_pinned(self):
        with self.assertRaises(ValueError): JevClient(model="other")
        with self.assertRaises(ValueError): JevClient(endpoint="https://example.com")

    def test_missing_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(JevAuthError): JevClient().execute_payload({"model": DEFAULT_MODEL}, time.monotonic() + 1)

    def test_injected_success_and_usage(self):
        client = JevClient(transport=lambda payload, headers: answer(payload["questions"]))
        result = client.evaluate_questions("s", {"q": question()})
        self.assertTrue(result["completed"])
        self.assertEqual(result["requests_made"], 1)
        self.assertEqual(result["usage"], {"input_tokens": 12, "output_tokens": 3})

    def test_missing_and_unknown_answers_partial(self):
        client = JevClient(transport=lambda payload, headers: answer(["other"]))
        result = client.evaluate_questions("s", {"q": question()})
        self.assertFalse(result["completed"])
        self.assertEqual(result["answers"], {})
        self.assertEqual(result["unknown_reasons"]["q"], "missing_answer")

    def test_oversized_question_does_not_block_neighbor(self):
        calls = []
        def transport(payload, headers):
            calls.append(tuple(payload["questions"]))
            return answer(payload["questions"])
        result = JevClient(transport=transport).evaluate_questions("s", {"huge": question("\\u754c" * 10000), "good": question()})
        self.assertEqual(calls, [("good",)])
        self.assertEqual(result["unknown_reasons"]["huge"], "question_ceiling")
        self.assertIn("good", result["answers"])
        self.assertFalse(result["completed"])

    def test_partial_success_then_failure(self):
        calls = 0
        def transport(payload, headers):
            nonlocal calls
            calls += 1
            if calls == 1: return answer(payload["questions"])
            raise JevServerError("private model text")
        with patch("jev_client.batch_questions", side_effect=lambda s, q: [{k: v} for k, v in q.items()]):
            result = JevClient(transport=transport).evaluate_questions("s", {"a": question(), "b": question()})
        self.assertIn("a", result["answers"])
        self.assertEqual(result["unknown_reasons"]["b"], "api_error:JevServerError")
        self.assertNotIn("private model text", json.dumps(result))

    def test_retry_attempts_charge_input(self):
        payload = {"model": DEFAULT_MODEL, "state": "s", "questions": {"q": question()}}
        estimate = estimate_payload_tokens("s", payload["questions"])
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            with patch.object(JevClient, "_post_http", return_value=(503, b"", {})):
                client = JevClient(sleep_fn=lambda _: None)
                with self.assertRaises(JevServerError): client.execute_payload(payload, time.monotonic() + 2, estimate * 2)
        self.assertEqual(client.requests_attempted, 2)
        self.assertEqual(client.estimated_input_tokens, estimate * 2)

    def test_retry_after_beyond_deadline_never_sleeps(self):
        sleeps = []
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            with patch.object(JevClient, "_post_http", return_value=(429, b"", {"retry-after": "10"})):
                with self.assertRaises(Exception): JevClient(sleep_fn=sleeps.append).execute_payload({"model": DEFAULT_MODEL}, time.monotonic() + 0.5)
        self.assertEqual(sleeps, [])

    def test_duplicate_json_key(self):
        body = b'{"model":"jev-1.13.0","answers":{},"answers":{},"usage":{"input_tokens":1,"output_tokens":1}}'
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            with patch.object(JevClient, "_post_http", return_value=(200, body, {})):
                with self.assertRaises(JevValidationError): JevClient().execute_payload({"model": DEFAULT_MODEL}, time.monotonic() + 2)

    def test_nonretryable_401_and_422(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            for status in (401, 422):
                with self.subTest(status=status), patch.object(JevClient, "_post_http", return_value=(status, b"secret", {})):
                    client = JevClient()
                    with self.assertRaises(Exception): client.execute_payload({"model": DEFAULT_MODEL}, time.monotonic() + 2)
                    self.assertEqual(client.requests_attempted, 1)

    def test_real_http_boundary_normalizes_read_errors(self):
        class BrokenResponse:
            status = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, n): raise http.client.IncompleteRead(b"partial")
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            client = JevClient()
            with patch.object(client._opener, "open", return_value=BrokenResponse()):
                result = client.evaluate_questions("s", {"q": question()})
        self.assertFalse(result["completed"])
        self.assertEqual(result["unknown_reasons"]["q"], "api_error:JevServerError")

    def test_real_http_boundary_normalizes_connection_errors(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            client = JevClient()
            with patch.object(client._opener, "open", side_effect=http.client.RemoteDisconnected("private response")):
                with self.assertRaises(JevServerError) as ctx: client.execute_payload({"model": DEFAULT_MODEL}, time.monotonic() + 2)
        self.assertNotIn("private response", str(ctx.exception))

    def test_trickling_body_stops_at_operation_deadline(self):
        class Trickle:
            status = 200
            headers = {}
            reads = 0
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read1(self, n):
                self.reads += 1
                time.sleep(0.006)
                return b"x"
        response = Trickle()
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            client = JevClient(deadline_seconds=0.02)
            with patch.object(client._opener, "open", return_value=response):
                result = client.evaluate_questions("s", {"q": question()})
        self.assertFalse(result["completed"])
        self.assertLess(response.reads, 20)
        self.assertEqual(result["unknown_reasons"]["q"], "operation_budget_reached")

    def test_redirect_rejected(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            client = JevClient()
            from urllib.error import URLError
            with patch.object(client._opener, "open", side_effect=URLError(JevRedirectError("redirect"))):
                with self.assertRaises(JevRedirectError): client.execute_payload({"model": DEFAULT_MODEL}, time.monotonic() + 2)

    def test_late_response_accounts_usage_without_score(self):
        def late(payload, headers):
            time.sleep(0.02)
            return answer(["q"])
        result = JevClient(transport=late, deadline_seconds=0.01).evaluate_questions("s", {"q": question()})
        self.assertFalse(result["completed"])
        self.assertEqual(result["usage"], {"input_tokens": 12, "output_tokens": 3})
        self.assertEqual(result["answers"], {})


if __name__ == "__main__": unittest.main()


class TestRecoveredContracts(unittest.TestCase):
    def test_key_redaction(self):
        key = "private-typesafe-key-0123456789"
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": key}):
            self.assertNotIn(key, str(JevError("Bearer " + key)))
            self.assertNotIn(key, sanitize_text("leak " + key))

    def test_successful_retry_honors_retry_after(self):
        body = json.dumps(answer(["q"])).encode()
        responses = [(429, b"", {"retry-after": "1"}), (200, body, {})]
        sleeps = []
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}), \
             patch.object(JevClient, "_post_http", side_effect=lambda *args: responses.pop(0)):
            client = JevClient(sleep_fn=sleeps.append)
            result = client.execute_payload({"model": DEFAULT_MODEL, "state": "s", "questions": {"q": question()}}, time.monotonic() + 5)
        self.assertEqual(result["answers"]["q"]["noul"], 0.8)
        self.assertEqual(client.requests_attempted, 2)
        self.assertEqual(sleeps, [1.0])

    def test_real_batch_split_keeps_all_questions(self):
        seen = []
        def transport(payload, headers):
            seen.extend(payload["questions"])
            return answer(payload["questions"])
        questions = {f"q{i}": question("x" * 8000) for i in range(9)}
        client = JevClient(transport=transport, max_requests=8)
        result = client.evaluate_questions("s", questions, max_input_tokens=120000)
        self.assertEqual(set(seen), set(questions))
        self.assertEqual(set(result["answers"]), set(questions))
        self.assertGreater(result["requests_made"], 1)
        self.assertTrue(result["completed"])

    def test_request_count_cap_independent_of_input_budget(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}), \
             patch.object(JevClient, "_post_http", return_value=(503, b"", {})):
            client = JevClient(max_requests=2, sleep_fn=lambda _: None)
            with self.assertRaises(JevServerError):
                client.execute_payload({"model": DEFAULT_MODEL}, time.monotonic() + 5, max_input_tokens=1000000)
        self.assertEqual(client.requests_attempted, 2)

    def test_specific_http_errors(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            for status, error_type in ((401, JevAuthError), (422, JevUnprocessableError)):
                with self.subTest(status=status), patch.object(JevClient, "_post_http", return_value=(status, b"", {})):
                    with self.assertRaises(error_type):
                        JevClient().execute_payload({"model": DEFAULT_MODEL}, time.monotonic() + 5)
            with patch.object(JevClient, "_post_http", return_value=(429, b"", {"retry-after": "10"})):
                with self.assertRaises(JevRateLimitError):
                    JevClient().execute_payload({"model": DEFAULT_MODEL}, time.monotonic() + 0.2)

    def test_bad_second_http_batch_keeps_first_answer(self):
        good = json.dumps(answer(["a"])).encode()
        responses = [(200, good, {}), (200, b"[" * 5000 + b"]" * 5000, {})]
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}), \
             patch("jev_client.batch_questions", side_effect=lambda state, questions: [{k: v} for k, v in questions.items()]), \
             patch.object(JevClient, "_post_http", side_effect=lambda *args: responses.pop(0)):
            result = JevClient().evaluate_questions("s", {"a": question(), "b": question()})
        self.assertIn("a", result["answers"])
        self.assertEqual(result["unknown_reasons"]["b"], "api_error:JevValidationError")
        self.assertEqual(result["usage"], {"input_tokens": 12, "output_tokens": 3})

    def test_bad_content_length_and_deep_json_preserve_prior_batch(self):
        class Response:
            status = 200
            def __init__(self, headers, body): self.headers, self.body = headers, body
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read1(self, n):
                if not self.body: return b""
                part, self.body = self.body[:n], self.body[n:]
                return part
        for headers, body in (({"Content-Length": "²"}, b"{}"),
                              ({"Content-Length": "9" * 10000}, b"{}"),
                              ({}, b"[" * 100000 + b"]" * 100000)):
            with self.subTest(header=str(headers)[:20]):
                def transport(payload, request_headers):
                    return answer(payload["questions"])
                client = JevClient(transport=transport)
                first = client.evaluate_questions("s", {"a": question()})
                self.assertTrue(first["completed"])
                client.transport = None
                with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}), \
                     patch.object(client._opener, "open", return_value=Response(headers, body)):
                    second = client.evaluate_questions("s", {"b": question()})
                self.assertFalse(second["completed"])
                self.assertIn("api_error:JevValidationError", second["stop_reason"])
                self.assertEqual(client.actual_input_tokens, 12)
