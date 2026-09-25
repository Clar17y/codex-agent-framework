"""Bounded direct SystemOne client. Estimates are admission heuristics, not tokenizer guarantees."""
import email.utils
import http.client
import json
import math
import os
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_DEADLINE_SECONDS = 30.0
DEFAULT_MAX_REQUESTS = 8
MAX_STATE_ALL_QUESTIONS_TOKENS = 64000
MAX_STATE_LARGEST_QUESTION_TOKENS = 32000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RETRY_AFTER_SECONDS = 15.0
# Deliberate headroom: UTF-8 JSON bytes overestimates most tokenizers, but is not a proof.
ADMISSION_FRACTION = 0.75


class JevError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(sanitize_text(message))
        self.message = sanitize_text(message)
        self.status_code = status_code


class JevAuthError(JevError): pass
class JevUnprocessableError(JevError): pass
class JevRateLimitError(JevError): pass
class JevServerError(JevError): pass
class JevRedirectError(JevError): pass
class JevValidationError(JevError): pass
class JevBudgetExceededError(JevError): pass


def sanitize_text(text):
    key = os.environ.get("TYPESAFE_API_KEY", "")
    return str(text).replace(key, "[REDACTED_API_KEY]") if key else str(text)


def _json_bytes(value):
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


def estimate_tokens(text):
    return len(str(text).encode("utf-8"))


def estimate_question_tokens(question):
    return _json_bytes(question) + 32


def estimate_payload_tokens(state, questions):
    return _json_bytes({"model": DEFAULT_MODEL, "state": state, "questions": questions}) + 64


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JevValidationError("Duplicate response JSON key")
        result[key] = value
    return result


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise JevRedirectError("HTTP redirect rejected")


def parse_retry_after(value):
    if not value:
        return None
    try:
        if value.strip().isdigit():
            return float(value.strip())
        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return None


def validate_response(data, expected_model):
    if not isinstance(data, dict):
        raise JevValidationError("Response must be an object")
    if data.get("model") != expected_model:
        raise JevValidationError("Model identity mismatch")
    answers, usage = data.get("answers"), data.get("usage")
    if not isinstance(answers, dict) or not isinstance(usage, dict):
        raise JevValidationError("Response missing answers or usage object")
    cleaned = {}
    for qid, answer in answers.items():
        if not isinstance(qid, str) or not isinstance(answer, dict) or answer.get("type") != "noul":
            raise JevValidationError("Invalid answer shape or type")
        score = answer.get("noul")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise JevValidationError("Noul must be a float/int number")
        if not math.isfinite(score):
            raise JevValidationError("Noul must be finite")
        if not 0 <= score <= 1:
            raise JevValidationError("Noul must be between 0.0 and 1.0")
        cleaned[qid] = {"type": "noul", "noul": float(score)}
    for field in ("input_tokens", "output_tokens"):
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise JevValidationError("Invalid usage count")
    return {"model": expected_model, "answers": cleaned, "usage": {k: usage[k] for k in ("input_tokens", "output_tokens")}}


def batch_questions(state, questions, max_state_all_tokens=MAX_STATE_ALL_QUESTIONS_TOKENS,
                    max_state_single_tokens=MAX_STATE_LARGEST_QUESTION_TOKENS):
    batches, current = [], {}
    all_limit = int(max_state_all_tokens * ADMISSION_FRACTION)
    single_limit = int(max_state_single_tokens * ADMISSION_FRACTION)
    if estimate_payload_tokens(state, {}) >= single_limit:
        raise ValueError("State exceeds ceiling")
    for qid, question in questions.items():
        if estimate_payload_tokens(state, {qid: question}) > single_limit:
            raise ValueError("Question exceeds ceiling")
        proposed = {**current, qid: question}
        if current and estimate_payload_tokens(state, proposed) > all_limit:
            batches.append(current)
            current = {}
        current[qid] = question
    if current:
        batches.append(current)
    return batches


class JevClient:
    def __init__(self, model=DEFAULT_MODEL, endpoint=DEFAULT_ENDPOINT,
                 timeout_seconds=DEFAULT_TIMEOUT_SECONDS, deadline_seconds=DEFAULT_DEADLINE_SECONDS,
                 max_requests=DEFAULT_MAX_REQUESTS, transport=None, sleep_fn=time.sleep):
        if model != DEFAULT_MODEL:
            raise ValueError("Only pinned jev-1.13.0 model is supported")
        if endpoint != DEFAULT_ENDPOINT:
            raise ValueError("Arbitrary remote endpoints are not permitted")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("Invalid timeout or deadline")
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
            raise ValueError("Invalid max_requests")
        self.model, self.endpoint = model, endpoint
        self.timeout_seconds, self.deadline_seconds = timeout_seconds, deadline_seconds
        self.max_requests, self.transport, self.sleep_fn = max_requests, transport, sleep_fn
        self.requests_attempted = self.estimated_input_tokens = 0
        self.actual_input_tokens = self.actual_output_tokens = 0
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def _post_http(self, payload_bytes, headers, timeout, operation_deadline=None):
        req = urllib.request.Request(self.endpoint, data=payload_bytes, headers=headers, method="POST")
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                response_headers = {k.lower(): v for k, v in resp.headers.items()}
                length = response_headers.get("content-length")
                if length is not None and (not isinstance(length, str) or not length.isascii() or not length.isdigit() or len(length) > 10):
                    raise JevValidationError("Invalid Content-Length")
                expected_length = int(length) if length is not None else None
                if expected_length is not None and expected_length > MAX_RESPONSE_BYTES:
                    raise JevValidationError("Response body exceeded limit")
                body = bytearray()
                read_one = getattr(resp, "read1", None)
                while True:
                    if expected_length is not None and len(body) >= expected_length:
                        break
                    if operation_deadline is not None and time.monotonic() >= operation_deadline:
                        raise JevBudgetExceededError("Operation deadline during response read")
                    # read1 consumes one available socket chunk; fallback read(1) avoids
                    # waiting for a large requested count on alternate response objects.
                    piece = read_one(min(65536, MAX_RESPONSE_BYTES + 1 - len(body))) if read_one else resp.read(1)
                    if not piece:
                        break
                    body.extend(piece)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise JevValidationError("Response body exceeded limit")
                return resp.status, bytes(body), response_headers
        except urllib.error.HTTPError as exc:
            return exc.code, b"", {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, JevRedirectError):
                raise exc.reason
            raise JevServerError("Network error") from None
        except (OSError, http.client.HTTPException):
            raise JevServerError("Network or HTTP response failure") from None

    def execute_payload(self, payload, operation_deadline, max_input_tokens=120000):
        key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not key and self.transport is None:
            raise JevAuthError("TYPESAFE_API_KEY missing")
        headers = {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {key}" if key else ""}
        payload_bytes = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        estimate = estimate_payload_tokens(payload.get("state", ""), payload.get("questions", {}))
        retry_count = 0
        while True:
            remaining = operation_deadline - time.monotonic()
            if remaining <= 0 or self.requests_attempted >= self.max_requests or self.estimated_input_tokens + estimate > max_input_tokens:
                raise JevBudgetExceededError("Operation deadline or request/input budget reached")
            self.requests_attempted += 1
            self.estimated_input_tokens += estimate
            if self.transport is not None:
                try:
                    data = self.transport(payload, headers)
                except JevError:
                    raise
                except Exception:
                    raise JevServerError("Transport failed") from None
                response = validate_response(data, self.model)
            else:
                status, body, response_headers = self._post_http(payload_bytes, headers, min(self.timeout_seconds, remaining), operation_deadline)
                if status == 200:
                    try:
                        data = json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicates,
                                          parse_constant=lambda _: (_ for _ in ()).throw(JevValidationError("Non-finite JSON value")))
                    except (UnicodeError, ValueError, RecursionError):
                        raise JevValidationError("Invalid response JSON") from None
                    response = validate_response(data, self.model)
                elif status == 401:
                    raise JevAuthError("HTTP 401", status)
                elif status == 422:
                    raise JevUnprocessableError("HTTP 422", status)
                elif status in (429, 529, 500, 502, 503, 504):
                    retry_count += 1
                    after = parse_retry_after(response_headers.get("retry-after"))
                    wait = after if after is not None else min(MAX_RETRY_AFTER_SECONDS, 0.5 * 2 ** min(retry_count - 1, 8))
                    if wait > MAX_RETRY_AFTER_SECONDS or wait >= operation_deadline - time.monotonic() or self.requests_attempted >= self.max_requests or self.estimated_input_tokens + estimate > max_input_tokens:
                        error = JevRateLimitError if status in (429, 529) else JevServerError
                        raise error("Retry unavailable within operation budget", status)
                    self.sleep_fn(wait)
                    continue
                else:
                    raise JevServerError("Unexpected HTTP status", status)
            self.actual_input_tokens += response["usage"]["input_tokens"]
            self.actual_output_tokens += response["usage"]["output_tokens"]
            if time.monotonic() > operation_deadline:
                raise JevBudgetExceededError("Operation deadline reached after response")
            return response

    def evaluate_questions(self, state, questions, max_input_tokens=120000):
        answers, unknown_reasons, stop_reason = {}, {}, None
        start_requests, start_est = self.requests_attempted, self.estimated_input_tokens
        start_in, start_out = self.actual_input_tokens, self.actual_output_tokens
        eligible = {}
        single_limit = int(MAX_STATE_LARGEST_QUESTION_TOKENS * ADMISSION_FRACTION)
        if estimate_payload_tokens(state, {}) >= single_limit:
            unknown_reasons = {qid: "state_ceiling" for qid in questions}
            batches = []
        else:
            for qid, question in questions.items():
                if estimate_payload_tokens(state, {qid: question}) > single_limit:
                    unknown_reasons[qid] = "question_ceiling"
                else:
                    eligible[qid] = question
            batches = batch_questions(state, eligible)
        deadline = time.monotonic() + self.deadline_seconds
        for batch in batches:
            try:
                response = self.execute_payload({"model": self.model, "state": state, "questions": batch}, deadline, max_input_tokens)
                answers.update({qid: ans for qid, ans in response["answers"].items() if qid in batch})
                for qid in batch:
                    if qid not in response["answers"]:
                        unknown_reasons[qid] = "missing_answer"
                if set(response["answers"]) - set(batch):
                    stop_reason = "unknown_answer_ids"
            except JevBudgetExceededError:
                stop_reason = "operation_budget_reached"
                break
            except JevError as exc:
                stop_reason = "api_error:" + type(exc).__name__
                break
        for qid in questions:
            if qid not in answers and qid not in unknown_reasons:
                unknown_reasons[qid] = stop_reason or "not_attempted"
        if unknown_reasons and stop_reason is None:
            stop_reason = "unknown_answers"
        completed = not unknown_reasons and stop_reason is None and len(answers) == len(questions)
        return {"model": self.model, "answers": answers, "unknown_reasons": unknown_reasons,
                "usage": {"input_tokens": self.actual_input_tokens - start_in, "output_tokens": self.actual_output_tokens - start_out},
                "estimated_input_tokens": self.estimated_input_tokens - start_est,
                "requests_made": self.requests_attempted - start_requests,
                "completed": completed, "stop_reason": stop_reason}
