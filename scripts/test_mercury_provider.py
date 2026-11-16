#!/usr/bin/env python3
"""Tests for Mercury 3 exploration provider subprocess adapter."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from scripts.mercury_provider import (
    extract_and_parse_json,
    get_inception_api_key,
    redact_obj,
    redact_text,
    run_mercury,
    validate_schema,
    is_transient_rate_limit,
    is_account_quota_exhausted,
    StrictNoRedirectHandler,
    INCEPTION_API_HOST,
    INCEPTION_ENDPOINT,
    MAX_TURNS,
    MAX_TOOL_CALLS,
    MAX_COMPLETION_TOKENS,
    MAX_TOTAL_COMPLETION_TOKENS,
    OVERALL_TIMEOUT_SECONDS,
)


class MockStreamingResponse:
    """Mock HTTP response object yielding SSE lines."""

    def __init__(self, sse_lines: list[str]):
        self.buffer = io.BytesIO(''.join(sse_lines).encode())
        self.fp = MagicMock()
        self.fp.raw._sock.settimeout = MagicMock()

    def read1(self, size):
        return self.buffer.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def make_sse_chunk(content=None, tool_calls=None, finish_reason=None, usage=None, reasoning=None):
    if usage is None and finish_reason is not None:
        usage = {"completion_tokens": 1, "prompt_tokens": 1}
    delta = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    if reasoning is not None:
        delta["reasoning"] = reasoning

    choice = {"index": 0, "delta": delta}
    if finish_reason:
        choice["finish_reason"] = finish_reason

    payload = {"choices": [choice]}
    if usage:
        payload["usage"] = usage
    return f"data: {json.dumps(payload)}\n\n"


class TestMercuryProvider(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name).resolve()

        self.corpus = self.dir_path / "corpus"
        self.corpus.mkdir()
        (self.corpus / "module.py").write_text("def helper():\n    return 'OK'\n", encoding="utf-8")

        self.task_file = self.dir_path / "task.json"
        self.task_file.write_text(json.dumps({
            "id": "test-task",
            "objective": "Explain helper in module.py",
            "acceptance_criteria": ["Cite exact lines"],
        }), encoding="utf-8")

        self.output_file = self.dir_path / "output.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_fixed_endpoint_and_strict_no_redirect(self):
        self.assertEqual(INCEPTION_ENDPOINT, "https://api.inceptionlabs.ai/v1/chat/completions")
        handler = StrictNoRedirectHandler()
        req = MagicMock(full_url=INCEPTION_ENDPOINT)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(req, None, 301, "Moved Permanently", {}, "https://api.inceptionlabs.ai/v2/chat")
        self.assertIn("rejected", str(ctx.exception).lower())

    def test_schema_requires_citations_in_evidence(self):
        # Valid schema with citations
        valid_obj = {
            "answer": "Helper returns OK",
            "findings": [
                {
                    "claim": "helper defined",
                    "evidence": [{"path": "module.py", "start": 1, "end": 2}],
                }
            ],
            "tests": [{"path": "test_module.py", "name": "test_helper", "reason": "Verifies helper"}],
            "caveats": ["None"],
        }
        valid, err = validate_schema(valid_obj)
        self.assertTrue(valid, err)

        # Missing citations (empty findings)
        no_citations_obj = {
            "answer": "Helper returns OK",
            "findings": [],
            "tests": [],
            "caveats": [],
        }
        valid, err = validate_schema(no_citations_obj)
        self.assertFalse(valid)
        self.assertIn("requires citations", err.lower())

        # Empty evidence list in findings
        empty_ev_obj = {
            "answer": "Helper returns OK",
            "findings": [{"claim": "helper defined", "evidence": []}],
            "tests": [],
            "caveats": [],
        }
        valid, err = validate_schema(empty_ev_obj)
        self.assertFalse(valid)
        self.assertIn("requires citations", err.lower())

    def test_extract_and_parse_json(self):
        text = 'Summary:\n```json\n{"answer": "ok", "findings": [{"claim": "c", "evidence": [{"path": "m.py", "start": 1, "end": 2}]}], "tests": [], "caveats": []}\n```'
        parsed, valid, err = extract_and_parse_json(text)
        self.assertTrue(valid)
        self.assertEqual(parsed["answer"], "ok")

        # Invalid JSON
        parsed, valid, err = extract_and_parse_json("Not json at all")
        self.assertFalse(valid)
        self.assertIn("JSON decode error", err)

    def test_missing_api_key_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True), patch("scripts.mercury_provider.get_inception_api_key", return_value=None):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
            self.assertEqual(result["status"], "auth_error")
            self.assertFalse(result["final_parse_valid"])
            self.assertIn("INCEPTION_API_KEY missing", result["error"])

    def test_credential_retrieval_from_registry(self):
        if sys.platform != "win32":
            self.skipTest("Registry key test only applicable on Windows")
        mock_winreg = MagicMock()
        mock_winreg.HKEY_CURRENT_USER = 1
        mock_winreg.OpenKey.return_value.__enter__.return_value = MagicMock()
        mock_winreg.QueryValueEx.return_value = ("reg-secret-key", 1)
        with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {"winreg": mock_winreg}):
            key = get_inception_api_key()
            self.assertEqual(key, "reg-secret-key")

    def test_redaction_of_api_key_and_bearer_tokens(self):
        secret = "secret-inception-key-12345"
        text = f"Authorization: Bearer {secret} and key={secret} and Bearer abcdef1234567890"
        redacted = redact_text(text, secret)
        self.assertNotIn(secret, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_discard_hidden_reasoning(self):
        final_answer = json.dumps({
            "answer": "Investigated",
            "findings": [{"claim": "found", "evidence": [{"path": "module.py", "start": 1, "end": 2}]}],
            "tests": [],
            "caveats": [],
        })
        stream = [
            make_sse_chunk(content="Thinking step 1", reasoning="super secret private reasoning thoughts"),
            make_sse_chunk(content=final_answer, finish_reason="stop", usage={"completion_tokens": 10, "prompt_tokens": 20}),
            "data: [DONE]\n\n",
        ]
        opener = MagicMock()
        opener.open.return_value = MockStreamingResponse(stream)
        with patch("scripts.mercury_provider.get_inception_api_key", return_value="synthetic-key"), \
             patch("urllib.request.build_opener", return_value=opener):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["final_parse_valid"])
            # Ensure reasoning thoughts never entered the transcript or result payload
            dumped = json.dumps(result)
            self.assertNotIn("super secret private reasoning", dumped)

    def test_sum_usage_across_turns_and_cap_next_request(self):
        tool_stream = [
            make_sse_chunk(tool_calls=[{"index": 0, "id": "t1", "function": {"name": "list_files", "arguments": "{}"}}],
                           finish_reason="tool_calls",
                           usage={"completion_tokens": 15, "prompt_tokens": 50}),
            "data: [DONE]\n\n",
        ]
        final_answer = json.dumps({
            "answer": "Investigated",
            "findings": [{"claim": "found", "evidence": [{"path": "module.py", "start": 1, "end": 2}]}],
            "tests": [],
            "caveats": [],
        })
        final_stream = [
            make_sse_chunk(content=final_answer, finish_reason="stop", usage={"completion_tokens": 25, "prompt_tokens": 70}),
            "data: [DONE]\n\n",
        ]
        opener = MagicMock()
        opener.open.side_effect = [MockStreamingResponse(tool_stream), MockStreamingResponse(final_stream)]

        with patch("scripts.mercury_provider.get_inception_api_key", return_value="synthetic-key"), \
             patch("urllib.request.build_opener", return_value=opener), \
             patch("scripts.mercury_provider.MAX_TOTAL_COMPLETION_TOKENS", 100):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["tokens"]["completion_tokens"], 40)
            self.assertEqual(result["tokens"]["prompt_tokens"], 120)

            # Check outgoing request capping on second call
            call_2_payload = json.loads(opener.open.call_args_list[1].args[0].data)
            # Budget was 100, turn 1 used 15 completion tokens -> remaining budget 85
            self.assertEqual(call_2_payload["max_completion_tokens"], 85)

    def test_aggregate_completion_budget_exhaustion(self):
        tool_stream = [
            make_sse_chunk(tool_calls=[{"index": 0, "id": "t1", "function": {"name": "list_files", "arguments": "{}"}}],
                           finish_reason="tool_calls",
                           usage={"completion_tokens": 30, "prompt_tokens": 20}),
            "data: [DONE]\n\n",
        ]
        opener = MagicMock()
        opener.open.side_effect = [MockStreamingResponse(tool_stream), MockStreamingResponse(tool_stream)]

        with patch("scripts.mercury_provider.get_inception_api_key", return_value="synthetic-key"), \
             patch("urllib.request.build_opener", return_value=opener), \
             patch("scripts.mercury_provider.MAX_TOTAL_COMPLETION_TOKENS", 50):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
            self.assertEqual(result["status"], "budget_exhausted")
            # The second turn reports 30 against a remaining allowance of 20.
            # Its tools must not execute and its invalid accounting is rejected.
            self.assertEqual(result["tool_calls_count"], 1)
            self.assertEqual(result["tokens"]["completion_tokens"], 30)
            self.assertIn("ceiling", result["errors"][0].lower())

    def test_tool_call_batch_ceiling_enforced(self):
        # Model returns 5 tool calls in one batch, but MAX_TOOL_CALLS is set to 2
        five_calls = [
            {"index": i, "id": f"tc_{i}", "function": {"name": "list_files", "arguments": "{}"}}
            for i in range(5)
        ]
        batch_stream = [
            make_sse_chunk(tool_calls=five_calls, finish_reason="tool_calls", usage={"completion_tokens": 5, "prompt_tokens": 10}),
            "data: [DONE]\n\n",
        ]
        opener = MagicMock()
        opener.open.return_value = MockStreamingResponse(batch_stream)

        with patch("scripts.mercury_provider.get_inception_api_key", return_value="synthetic-key"), \
             patch("urllib.request.build_opener", return_value=opener), \
             patch("scripts.mercury_provider.MAX_TOOL_CALLS", 2):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
            self.assertEqual(result["status"], "budget_exhausted")
            self.assertEqual(result["tool_calls_count"], 2)
            self.assertIn("Tool call ceiling", result["errors"][0])

    def test_missing_done_or_missing_usage_fails_closed(self):
        # Stream missing [DONE]
        stream_no_done = [
            make_sse_chunk(content="hello", finish_reason="stop", usage={"completion_tokens": 1, "prompt_tokens": 1}),
        ]
        opener = MagicMock()
        opener.open.return_value = MockStreamingResponse(stream_no_done)
        with patch("scripts.mercury_provider.get_inception_api_key", return_value="synthetic-key"), \
             patch("urllib.request.build_opener", return_value=opener):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
            self.assertEqual(result["status"], "error")
            self.assertIn("[DONE]", result["errors"][0])

        # Stream missing usage
        stream_no_usage = [
            'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n",
        ]
        opener.open.return_value = MockStreamingResponse(stream_no_usage)
        with patch("scripts.mercury_provider.get_inception_api_key", return_value="synthetic-key"), \
             patch("urllib.request.build_opener", return_value=opener):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
            self.assertEqual(result["status"], "error")
            self.assertIn("Missing token usage", result["errors"][0])

    def test_length_truncation_fails_closed(self):
        stream_len = [
            make_sse_chunk(content="partial text", finish_reason="length", usage={"completion_tokens": 10, "prompt_tokens": 5}),
            "data: [DONE]\n\n",
        ]
        opener = MagicMock()
        opener.open.return_value = MockStreamingResponse(stream_len)
        with patch("scripts.mercury_provider.get_inception_api_key", return_value="synthetic-key"), \
             patch("urllib.request.build_opener", return_value=opener):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
            self.assertEqual(result["status"], "length_truncated")

    def test_distinguishes_transient_429_from_account_quota(self):
        self.assertTrue(is_transient_rate_limit("Rate limit exceeded: 60 requests per minute"))
        self.assertFalse(is_transient_rate_limit("Account quota exceeded: insufficient funds"))

        self.assertTrue(is_account_quota_exhausted(429, "insufficient_quota: credit balance too low"))
        self.assertTrue(is_account_quota_exhausted(403, "billing hard limit reached"))
        self.assertFalse(is_account_quota_exhausted(429, "too many requests; 30 requests/min limit reached"))

    def test_transient_wording_wins_generic_quota_labels(self):
        from scripts.mercury_provider import classify_error
        for phrase in ('too many requests', 'tokens/min exceeded', 'tokens/sec exceeded',
                       'concurrency limit reached', 'requests per minute exceeded', 'rate_limit_exceeded'):
            with self.subTest(phrase=phrase):
                message = 'quota_exceeded: ' + phrase
                self.assertFalse(is_account_quota_exhausted(429, message))
                self.assertEqual(classify_error(429, message), 'rate_limit_error')
        self.assertEqual(classify_error(401, 'invalid_api_key: quota_exceeded'), 'auth_error')
        self.assertEqual(classify_error(404, 'model_not_found: quota_exceeded'), 'model_error')
        self.assertEqual(classify_error(403, 'billing hard limit reached'), 'quota_exhausted')
        self.assertEqual(classify_error(429, 'insufficient_quota: credit balance too low'), 'quota_exhausted')

    def test_rate_limit_429_returns_rate_limit_error(self):
        err = urllib.error.HTTPError(INCEPTION_ENDPOINT, 429, "Too Many Requests", {}, io.BytesIO(b"requests/min exceeded"))
        opener = MagicMock()
        opener.open.side_effect = err
        with patch("scripts.mercury_provider.get_inception_api_key", return_value="synthetic-key"), \
             patch("urllib.request.build_opener", return_value=opener):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
            self.assertEqual(result["status"], "rate_limit_error")

    def test_quota_exhausted_returns_quota_exhausted(self):
        err = urllib.error.HTTPError(INCEPTION_ENDPOINT, 429, "Too Many Requests", {}, io.BytesIO(b"insufficient_quota"))
        opener = MagicMock()
        opener.open.side_effect = err
        with patch("scripts.mercury_provider.get_inception_api_key", return_value="synthetic-key"), \
             patch("urllib.request.build_opener", return_value=opener):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
            self.assertEqual(result["status"], "quota_exhausted")

    def test_auth_error_returns_auth_error(self):
        err = urllib.error.HTTPError(INCEPTION_ENDPOINT, 401, "Unauthorized", {}, io.BytesIO(b"Invalid API key"))
        opener = MagicMock()
        opener.open.side_effect = err
        with patch("scripts.mercury_provider.get_inception_api_key", return_value="synthetic-key"), \
             patch("urllib.request.build_opener", return_value=opener):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
            self.assertEqual(result["status"], "auth_error")

    def run_stream(self, stream, **patches):
        from contextlib import ExitStack, redirect_stdout
        opener = MagicMock()
        opener.open.return_value = MockStreamingResponse(stream)
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(patch('scripts.mercury_provider.get_inception_api_key', return_value='synthetic-key'))
            stack.enter_context(patch('urllib.request.build_opener', return_value=opener))
            for key, value in patches.items():
                stack.enter_context(patch('scripts.mercury_provider.' + key, value))
            return run_mercury(self.corpus, self.task_file, self.output_file)

    def test_adversarial_streams_never_execute_tools(self):
        call = {'index': 0, 'id': 'c', 'function': {'name': 'list_files', 'arguments': '{}'}}
        start = make_sse_chunk(tool_calls=[call])
        valid = make_sse_chunk(finish_reason='tool_calls')
        done = 'data: [DONE]\n\n'
        cases = [
            [start, 'data: broken-json\n\n', valid, done],
            [start, 'data: []\n\n', valid, done],
            [start, 'data: {"choices": {}}\n\n', valid, done],
            [start, 'data: {"choices": [null]}\n\n', valid, done],
            [start, 'data: {"choices": [{"delta": []}]}\n\n', valid, done],
            [start, 'data: {"choices": [{"delta": {"content": 7}}]}\n\n', valid, done],
            [start, make_sse_chunk(finish_reason='stop'), done],
            [make_sse_chunk(finish_reason='tool_calls'), done],
            [start, done], [start, valid],
            [start, valid, valid, done],
            [start, valid, make_sse_chunk(content='late'), done],
            [start, valid, done, 'data: {"error": {"message": "late"}}\n\n'],
            [start, make_sse_chunk(usage={'completion_tokens': 0, 'prompt_tokens': 0}), valid, done],
            [start, valid, 'data: {"choices": [], "usage": {"completion_tokens": 1, "prompt_tokens": 1}}\n\n', done],
        ]
        for field in ('completion_tokens', 'prompt_tokens'):
            for value in (None, -1, True, 1.5, '1'):
                usage = {'completion_tokens': 1, 'prompt_tokens': 1}
                if value is None:
                    usage.pop(field)
                else:
                    usage[field] = value
                cases.append([start, make_sse_chunk(finish_reason='tool_calls', usage=usage), done])
        cases.append([start, make_sse_chunk(finish_reason='tool_calls', usage={
            'completion_tokens': 8193, 'prompt_tokens': 1}), done])
        cases.append([start, make_sse_chunk(finish_reason='tool_calls', usage={
            'completion_tokens': 1, 'prompt_tokens': 1, 'total_tokens': -1}), done])
        for idx, stream in enumerate(cases):
            with self.subTest(case=idx), patch('scripts.mercury_provider.execute_reader_tool') as tool:
                result = self.run_stream(stream)
                self.assertIn(result['status'], ('error', 'budget_exhausted'))
                self.assertFalse(result['final_parse_valid'])
                tool.assert_not_called()

    def test_fragmented_calls_and_separate_usage_succeed(self):
        first = {'index': 0, 'id': 'call', 'function': {'name': 'read_', 'arguments': '{"path":'}}
        second = {'index': 0, 'function': {'name': 'file', 'arguments': '"module.py"}'}}
        stream = [make_sse_chunk(tool_calls=[first]), make_sse_chunk(tool_calls=[second]),
                  'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
                  'data: {"choices":[],"usage":{"completion_tokens":2,"prompt_tokens":3,"total_tokens":5}}\n\n',
                  'data: [DONE]\n\n']
        result = self.run_stream(stream, MAX_TURNS=1)
        self.assertEqual(result['tool_calls_count'], 1)
        self.assertIn('1 | def helper', result['transcript'][-1]['content'])
        self.assertEqual(result['tokens']['completion_tokens'], 2)

    def test_live_empty_finish_trailer_and_null_calls(self):
        answer = json.dumps({'answer': 'ok', 'findings': [{'claim': 'helper',
            'evidence': [{'path': 'module.py', 'start': 1, 'end': 2}]}], 'tests': [], 'caveats': []})
        first = 'data: ' + json.dumps({'choices': [{'index': 0,
            'delta': {'content': answer, 'role': 'assistant', 'tool_calls': None}, 'finish_reason': 'stop'}]}) + '\n\n'
        trailer = 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        usage = 'data: {"choices":[],"usage":{"completion_tokens":2,"prompt_tokens":3,"total_tokens":5}}\n\n'
        done = 'data: [DONE]\n\n'
        self.assertTrue(self.run_stream([first, trailer, usage, done])['final_parse_valid'])
        self.assertTrue(self.run_stream([first, usage, done])['final_parse_valid'])
        for bad in ([trailer, trailer], [trailer.replace('stop', 'tool_calls')],
                    [make_sse_chunk(content='late')]):
            with self.subTest(trailers=bad):
                result = self.run_stream([first, *bad, usage, done])
                self.assertEqual(result['status'], 'error')
                self.assertFalse(result['final_parse_valid'])

    def test_stream_and_http_error_classification_match(self):
        cases = [(401, 'invalid_api_key', 'auth_error'), (403, 'insufficient_quota', 'quota_exhausted'),
                 (404, 'model_not_found', 'model_error'), (429, 'requests/min exceeded', 'rate_limit_error'),
                 (500, 'server failed', 'error')]
        for code, message, expected in cases:
            with self.subTest(message=message):
                stream = ['data: ' + json.dumps({'error': {'status': code, 'message': message}}) + '\n\n']
                self.assertEqual(self.run_stream(stream)['status'], expected)
        self.assertEqual(self.run_stream([make_sse_chunk(content='', finish_reason='stop'), 'data: [DONE]\n\n'])['status'], 'error')
        self.assertEqual(self.run_stream([make_sse_chunk(content='not JSON', finish_reason='stop'), 'data: [DONE]\n\n'])['status'], 'error')

    def test_credentials_filtered_before_request_and_in_artifacts(self):
        import hashlib
        host_secret = 'framework opaque fixture'
        fingerprint = [(len(host_secret), [hashlib.sha256(host_secret.encode()).hexdigest()])]
        source = 'api_key = "source-fixture-value"\nprint("' + host_secret + '")\n'
        (self.corpus / 'config.py').write_text(source)
        self.task_file.write_text(json.dumps({'objective': 'api_key="task-fixture-value" ' + host_secret}))
        call = {'index': 0, 'id': 'c', 'function': {'name': 'read_file', 'arguments': '{"path":"config.py"}'}}
        answer = json.dumps({'answer': 'ok', 'findings': [{'claim': 'api_key="response-fixture-value"',
            'evidence': [{'path': 'config.py', 'start': 1, 'end': 2}]}], 'tests': [], 'caveats': []})
        opener = MagicMock()
        opener.open.side_effect = [MockStreamingResponse([make_sse_chunk(tool_calls=[call], finish_reason='tool_calls'), 'data: [DONE]\n\n']),
                                   MockStreamingResponse([make_sse_chunk(content=answer, finish_reason='stop'), 'data: [DONE]\n\n'])]
        with patch.dict(os.environ, {'MERCURY_REDACTION_DIGESTS': json.dumps(fingerprint)}), \
             patch('scripts.mercury_provider.get_inception_api_key', return_value='synthetic-key'), \
             patch('urllib.request.build_opener', return_value=opener), patch('sys.stdout', new_callable=io.StringIO) as telemetry:
            result = run_mercury(self.corpus, self.task_file, self.output_file)
        requests = '\n'.join(c.args[0].data.decode() for c in opener.open.call_args_list)
        artifacts = telemetry.getvalue() + self.output_file.read_text() + json.dumps(result)
        for value in ('source-fixture-value', 'task-fixture-value', 'response-fixture-value', host_secret):
            self.assertNotIn(value, requests + artifacts)
        self.assertIn('1 | api_key', requests)
        self.assertIn('2 | print', requests)
        self.assertTrue(result['final_parse_valid'])
        self.assertEqual(json.loads(result['final_text']), result['parsed_result'])

    def test_trickle_and_tool_batch_share_absolute_deadline(self):
        import scripts.mercury_provider as provider
        clock = [0.0]
        class Trickle:
            def read1(self, size):
                clock[0] += 0.4
                return b'd'
        with patch.object(provider.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(TimeoutError):
                provider.parse_stream(Trickle(), 1.0, 100)
        calls = [{'index': n, 'id': str(n), 'function': {'name': 'list_files', 'arguments': '{}'}} for n in range(3)]
        clock[0] = 0
        deadlines = []
        def tool(*args, deadline):
            deadlines.append(deadline)
            clock[0] += 0.6
            return 'ok'
        with patch.object(provider.time, 'monotonic', side_effect=lambda: clock[0]):
            result = self.run_stream([make_sse_chunk(tool_calls=calls, finish_reason='tool_calls'), 'data: [DONE]\n\n'],
                                     OVERALL_TIMEOUT_SECONDS=1.0, execute_reader_tool=tool)
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(deadlines, [1.0, 1.0])
        self.assertEqual(result['tool_calls_count'], 2)

    def test_multiline_credentials_absent_from_requests_and_artifacts(self):
        sources = ['database_password: PRIVATE_FIXTURE_FIRST\n  PRIVATE_FIXTURE_SECOND\npublic: visible\n',
                   '- database_password:\n    PRIVATE_FIXTURE_FIRST\n    PRIVATE_FIXTURE_SECOND\n  public: visible\n',
                   'database_password:\n  proto://PRIVATE_FIXTURE_FIRST@host/db\npublic: visible\n',
                   'database_password:\n  PRIVATE_FIXTURE_FIRST\n  PRIVATE_FIXTURE_SECOND\npublic: visible\n',
                   'database_password: # pending\n\n# comment\n  PRIVATE_FIXTURE_FIRST:tail=value\n\n# comment\n  PRIVATE_FIXTURE_SECOND=value:tail\npublic: visible\n',
                   'service:\n  database_password:\n    nested: PRIVATE_FIXTURE_FIRST\n    deeper:\n      PRIVATE_FIXTURE_SECOND\n  public: visible\n',
                   'password: /* comment\n */\n \"PRIVATE_FIXTURE_FIRST\"\npublic: visible\n',
                   'password:\n  |\n    PRIVATE_FIXTURE_FIRST\n    PRIVATE_FIXTURE_SECOND\npublic: visible\n',
                   'password:\n[\n \"PRIVATE_FIXTURE_FIRST\",\n {\"nested\": \"PRIVATE_FIXTURE_SECOND\"}\n]\npublic: visible\n',
                   'password:\n{\n \"first\": \"PRIVATE_FIXTURE_FIRST\",\n \"second\": \"PRIVATE_FIXTURE_SECOND\"\n}\npublic: visible\n',
                   'password = (\n \"PRIVATE_FIXTURE_FIRST\"\n \"PRIVATE_FIXTURE_SECOND\"\n)\npublic: visible\n',
                   '\"password\":\n  \"PRIVATE_FIXTURE_FIRST\",\npublic: visible\n',
                   'password = \"PRIVATE_FIXTURE_FIRST\\\nPRIVATE_FIXTURE_SECOND\"\npublic = \"visible\"\n',
                   'api_key: |\n  PRIVATE_FIXTURE_FIRST\n  PRIVATE_FIXTURE_SECOND\npublic: visible\n',
                   'api_key = """\nPRIVATE_FIXTURE_FIRST\nPRIVATE_FIXTURE_SECOND\n"""\npublic = "visible"\n']
        for source in sources:
            with self.subTest(source=source.splitlines()[0]):
                (self.corpus / 'config.yaml').write_text(source, encoding='utf8')
                calls = [
                    {'index': number - 2, 'id': f'partial-{number}', 'function': {'name': 'read_file',
                        'arguments': json.dumps({'path': 'config.yaml', 'start': number, 'end': number})}}
                    for number in range(2, len(source.splitlines()) + 1)
                ]
                calls.append({'index': len(calls), 'id': 'search', 'function': {'name': 'search',
                    'arguments': json.dumps({'query': 'PRIVATE_FIXTURE', 'glob': 'config.yaml'})}})
                answer = json.dumps({'answer': 'redacted', 'findings': [{'claim': 'credential block',
                    'evidence': [{'path': 'config.yaml', 'start': 1, 'end': 3}]}], 'tests': [], 'caveats': []})
                opener = MagicMock()
                opener.open.side_effect = [MockStreamingResponse([
                    make_sse_chunk(tool_calls=calls, finish_reason='tool_calls'), 'data: [DONE]\n\n']),
                    MockStreamingResponse([make_sse_chunk(content=answer, finish_reason='stop'), 'data: [DONE]\n\n'])]
                with patch('scripts.mercury_provider.get_inception_api_key', return_value='synthetic-key'), \
                     patch('urllib.request.build_opener', return_value=opener), \
                     patch('sys.stdout', new_callable=io.StringIO) as telemetry:
                    result = run_mercury(self.corpus, self.task_file, self.output_file)
                requests = '\n'.join(c.args[0].data.decode() for c in opener.open.call_args_list)
                artifacts = telemetry.getvalue() + self.output_file.read_text() + json.dumps(result)
                for value in ('PRIVATE_FIXTURE_FIRST', 'PRIVATE_FIXTURE_SECOND'):
                    self.assertNotIn(value, requests + artifacts)
                self.assertTrue(result['final_parse_valid'])

    def test_actual_runner_wrapper_keeps_repository_requirements(self):
        from scripts.provider_runner import prompt_for
        for name, text in (('AGENTS.md', 'Require ALPHA_REQUIREMENT.'),
                           ('CLAUDE.md', 'Require BETA_REQUIREMENT.'),
                           ('selected.md', 'Require GAMMA_REQUIREMENT. password: \"OPAQUE_INSTRUCTION_VALUE\"')):
            (self.corpus / name).write_text(text)
        task = {'objective': 'Explain {nested} TASK CONTRACT and REPOSITORY INSTRUCTIONS delimiters',
                'acceptance_criteria': ['Cite lines'], 'owned_paths': ['DISTINCT_SCOPE/module.py'], 'validation': ['Check DISTINCT_VALIDATION {nested}'],
                'instructions_files': ['selected.md'], 'metadata': {'nested': {'value': 'brace }'}}}
        self.task_file.write_text(prompt_for(self.corpus, self.task_file, task=task, activity='exploration'))
        answer = json.dumps({'answer':'ok','findings':[{'claim':'found', 'evidence':[{'path':'module.py','start':1,'end':2}]}], 'tests':[], 'caveats':[]})
        opener = MagicMock()
        opener.open.return_value = MockStreamingResponse([make_sse_chunk(content=answer, finish_reason='stop'), 'data: [DONE]\n\n'])
        with patch('scripts.mercury_provider.get_inception_api_key', return_value='synthetic-key'), \
             patch('urllib.request.build_opener', return_value=opener), patch('sys.stdout', new_callable=io.StringIO):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
        request = json.loads(opener.open.call_args.args[0].data)
        outbound = json.dumps(request)
        for requirement in ('ALPHA_REQUIREMENT', 'BETA_REQUIREMENT', 'GAMMA_REQUIREMENT', 'DISTINCT_SCOPE/module.py', 'DISTINCT_VALIDATION', task['objective']):
            self.assertIn(requirement, outbound)
        self.assertNotIn('OPAQUE_INSTRUCTION_VALUE', outbound + self.output_file.read_text())
        self.assertTrue(result['final_parse_valid'], result)

    def test_malformed_wrapped_contract_fails_before_transport(self):
        self.task_file.write_text('prefix\nTASK CONTRACT\n' + json.dumps({'objective':'inspect'}) +
                                  '\nREPOSITORY INSTRUCTIONS_BROKEN\nrequirements')
        with patch('scripts.mercury_provider.get_inception_api_key', return_value='synthetic-key'), \
             patch('urllib.request.build_opener') as transport, patch('sys.stdout', new_callable=io.StringIO):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
        self.assertEqual(result['status'], 'setup_error')
        transport.assert_not_called()

    def test_real_urllib_transport_offline_and_trickled_headers(self):
        import scripts.mercury_provider as provider
        from contextlib import redirect_stdout
        answer = json.dumps({'answer':'ok','findings':[{'claim':'c','evidence':[{'path':'module.py','start':1,'end':2}]}], 'tests':[], 'caveats':[]})
        body = (make_sse_chunk(content=answer, finish_reason='stop')+'data: [DONE]\n\n').encode()
        wire = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n' + format(len(body),'x').encode() + b'\r\n' + body + b'\r\n0\r\n\r\n'
        class OfflineSocket:
            def makefile(self, *args, **kwargs):
                return io.BytesIO(wire)
            def sendall(self, data):
                pass
            def close(self):
                pass
            def settimeout(self, seconds):
                pass
        def connect(connection):
            connection.sock = OfflineSocket()
        with patch.object(provider.DeadlineHTTPSConnection, 'connect', connect), \
             patch.object(provider, 'get_inception_api_key', return_value='offline-fixture-key'), redirect_stdout(io.StringIO()):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
        self.assertEqual(result['status'], 'completed', result)
        clock = [0.0]
        class Trickle(io.BytesIO):
            def readinto(self, buffer):
                clock[0] += .2
                data = self.read(1)
                buffer[:len(data)] = data
                return len(data)
        class TrickleSocket(OfflineSocket):
            def makefile(self, *args, **kwargs):
                return Trickle(wire)
        def trickle_connect(connection):
            connection.sock = TrickleSocket()
        with patch.object(provider.DeadlineHTTPSConnection, 'connect', trickle_connect), \
             patch.object(provider.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(provider, 'OVERALL_TIMEOUT_SECONDS', 1.0), \
             patch.object(provider, 'get_inception_api_key', return_value='offline-fixture-key'), redirect_stdout(io.StringIO()):
            result = run_mercury(self.corpus, self.task_file, self.output_file)
        self.assertEqual(result['status'], 'timeout', result)
        self.assertLess(clock[0], 2.0)

    def test_task_and_outgoing_request_size_limits(self):
        self.task_file.write_text(json.dumps({'objective': 'x'*1000}))
        self.assertEqual(self.run_stream([], MAX_TASK_BYTES=100)['status'], 'setup_error')
        self.assertEqual(self.run_stream([], MAX_REQUEST_BYTES=100)['status'], 'budget_exhausted')
        for invalid in ([], {'objective':False}, {'acceptance_criteria':'not a list'}):
            self.task_file.write_text(json.dumps(invalid))
            self.assertEqual(self.run_stream([])['status'], 'setup_error')

    def test_response_event_arguments_and_error_body_bounds(self):
        import scripts.mercury_provider as provider
        done = 'data: [DONE]\n\n'
        with patch('scripts.mercury_provider.execute_reader_tool') as tool:
            self.assertEqual(self.run_stream(['data: ' + 'x' * 150 + '\n'], MAX_EVENT_BYTES=100)['status'], 'budget_exhausted')
            self.assertEqual(self.run_stream([': ignored\n' * 50], MAX_RESPONSE_BYTES=100)['status'], 'budget_exhausted')
            self.assertEqual(self.run_stream([make_sse_chunk(content='a')] * 4, MAX_EVENTS=3)['status'], 'budget_exhausted')
            call = {'index': 0, 'id': 'c', 'function': {'name': 'search', 'arguments': '{"query":"' + 'x'*100 + '"}'}}
            self.assertEqual(self.run_stream([make_sse_chunk(tool_calls=[call], finish_reason='tool_calls'), done], MAX_TOOL_ARGUMENT_BYTES=50)['status'], 'budget_exhausted')
            tool.assert_not_called()
        body = io.BytesIO(b'x' * 100000)
        self.assertEqual(len(provider.read_error_body(body, __import__('time').monotonic()+10)), provider.MAX_ERROR_BYTES)
        self.assertEqual(body.tell(), provider.MAX_ERROR_BYTES)

    def test_deadline_applies_to_raw_header_reads(self):
        from scripts.mercury_provider import DeadlineRaw
        clock = [0.0]
        class Raw:
            def readinto(self, buffer):
                clock[0] += .6
                buffer[0] = 65
                return 1
            def close(self):
                pass
        with patch('scripts.mercury_provider.time.monotonic', side_effect=lambda: clock[0]):
            raw = DeadlineRaw(Raw(), MagicMock(), 1.0)
            self.assertEqual(raw.readinto(bytearray(1)), 1)
            with self.assertRaises(TimeoutError):
                raw.readinto(bytearray(1))


if __name__ == "__main__":
    unittest.main()
