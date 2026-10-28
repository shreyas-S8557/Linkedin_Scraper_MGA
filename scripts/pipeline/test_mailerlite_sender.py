"""Unit tests for mailerlite_sender.py.

No network is ever touched: MailerLiteSender is always constructed with a
fake `http_client_factory`, so nothing in this file can send a real email
or call the real MailerLite API. Mirrors the structure/spirit of
test_email_sending_day8.py's GmailSender test coverage.

Run with (from `scripts/`):
    python -m unittest pipeline.test_mailerlite_sender -v
"""

from __future__ import annotations

import json
import unittest

import httpx

from .mailerlite_sender import (
    MailerLiteCredentialsError,
    MailerLiteSender,
    SendResult,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, headers: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = b"1" if payload is not None else b""
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://connect.mailerlite.com/api/x")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=self  # type: ignore[arg-type]
            )


class _FakeHTTPXClient:
    """Scripted fake for httpx.Client: `responses` is a dict keyed by
    (METHOD, path) -> _FakeResponse (or a list of them, consumed in order).
    Records every call made for assertions.
    """

    def __init__(self, responses: dict, calls: list, **kwargs):
        self._responses = responses
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def request(self, method, path, *, json=None):
        self._calls.append((method, path, json))
        key = (method, path.split("?")[0])
        entry = self._responses.get(key)
        if entry is None:
            # Path with an id in it, e.g. /campaigns/123/actions/schedule --
            # allow a wildcard registered as (METHOD, "*")
            entry = self._responses.get((method, "*"))
        if isinstance(entry, list):
            return entry.pop(0)
        return entry


def make_sender(responses: dict, calls: list, **overrides) -> MailerLiteSender:
    factory = lambda **kw: _FakeHTTPXClient(responses, calls, **kw)  # noqa: E731
    defaults = dict(
        api_token="tok_123",
        sender_email="verified@example.com",
        sender_name="Test Sender",
        http_client_factory=factory,
    )
    defaults.update(overrides)
    return MailerLiteSender(**defaults)


def happy_path_responses():
    return {
        ("POST", "/groups"): _FakeResponse(200, {"data": {"id": "g1"}}),
        ("POST", "/subscribers"): _FakeResponse(200, {"data": {"id": "s1", "email": "x@example.com"}}),
        ("POST", "/campaigns"): _FakeResponse(200, {"data": {"id": "c1"}}),
        ("POST", "/campaigns/c1/actions/schedule"): _FakeResponse(200, {"data": {"id": "c1", "status": "sending"}}),
    }


class MailerLiteCredentialsTests(unittest.TestCase):
    def test_missing_all_credentials(self):
        sender = MailerLiteSender(api_token="", sender_email="", sender_name="")
        with self.assertRaises(MailerLiteCredentialsError) as ctx:
            sender.validate_credentials()
        msg = str(ctx.exception)
        self.assertIn("MAILERLITE_API_TOKEN", msg)
        self.assertIn("MAILERLITE_SENDER_EMAIL", msg)
        self.assertIn("MAILERLITE_SENDER_NAME", msg)

    def test_valid_credentials_do_not_raise(self):
        sender = MailerLiteSender(api_token="t", sender_email="e@x.com", sender_name="N")
        sender.validate_credentials()  # should not raise

    def test_send_with_missing_credentials_returns_failure_not_raise(self):
        sender = MailerLiteSender(api_token="", sender_email="", sender_name="")
        result = sender.send("to@example.com", "Subject", "Body")
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertIn("MAILERLITE_API_TOKEN", result.error)


class MailerLiteSendTests(unittest.TestCase):
    def test_successful_send_creates_isolated_group_and_campaign(self):
        calls: list = []
        sender = make_sender(happy_path_responses(), calls)
        result = sender.send("prospect@example.com", "Hello there", "Body text\nwith a line break")

        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "c1")
        self.assertEqual(result.error, "")

        methods_paths = [(m, p) for m, p, _ in calls]
        self.assertEqual(
            methods_paths,
            [
                ("POST", "/groups"),
                ("POST", "/subscribers"),
                ("POST", "/campaigns"),
                ("POST", "/campaigns/c1/actions/schedule"),
            ],
        )

        # Subscriber upsert must target ONLY the new isolated group, and
        # only the intended recipient -- never any other address.
        _, _, subscriber_payload = calls[1]
        self.assertEqual(subscriber_payload["email"], "prospect@example.com")
        self.assertEqual(subscriber_payload["groups"], ["g1"])

        # Campaign must target only that same isolated group.
        _, _, campaign_payload = calls[2]
        self.assertEqual(campaign_payload["groups"], ["g1"])
        self.assertEqual(len(campaign_payload["emails"]), 1)
        self.assertEqual(campaign_payload["emails"][0]["subject"], "Hello there")
        self.assertEqual(campaign_payload["emails"][0]["from"], "verified@example.com")
        self.assertIn("Body text", campaign_payload["emails"][0]["content"])

        # Schedule call requests instant delivery.
        _, _, schedule_payload = calls[3]
        self.assertEqual(schedule_payload, {"delivery": "instant"})

    def test_empty_recipient_never_calls_api(self):
        calls: list = []
        sender = make_sender(happy_path_responses(), calls)
        result = sender.send("", "Subject", "Body")
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertEqual(calls, [])

    def test_empty_subject_or_body_never_calls_api(self):
        calls: list = []
        sender = make_sender(happy_path_responses(), calls)
        result = sender.send("to@example.com", "", "")
        self.assertFalse(result.success)
        self.assertEqual(calls, [])

    def test_authentication_failure_is_not_retryable(self):
        responses = {("POST", "/groups"): _FakeResponse(401, {"message": "Unauthenticated."})}
        calls: list = []
        sender = make_sender(responses, calls)
        result = sender.send("to@example.com", "Subj", "Body")
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertIn("authentication failed", result.error.lower())

    def test_unverified_sender_returns_clear_error(self):
        responses = {
            ("POST", "/groups"): _FakeResponse(200, {"data": {"id": "g1"}}),
            ("POST", "/subscribers"): _FakeResponse(200, {"data": {"id": "s1"}}),
            ("POST", "/campaigns"): _FakeResponse(
                422,
                {
                    "message": "The given data was invalid.",
                    "errors": {"emails.0.from": ["verified@example.com has not been verified."]},
                },
            ),
        }
        calls: list = []
        sender = make_sender(responses, calls)
        result = sender.send("to@example.com", "Subj", "Body")
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertEqual(result.error, "MailerLite sender email is not verified.")

    def test_generic_validation_error_is_not_retryable(self):
        responses = {
            ("POST", "/groups"): _FakeResponse(200, {"data": {"id": "g1"}}),
            ("POST", "/subscribers"): _FakeResponse(200, {"data": {"id": "s1"}}),
            ("POST", "/campaigns"): _FakeResponse(
                422, {"message": "The given data was invalid.", "errors": {"name": ["too long"]}}
            ),
        }
        calls: list = []
        sender = make_sender(responses, calls)
        result = sender.send("to@example.com", "Subj", "Body")
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertIn("validation error", result.error.lower())

    def test_rate_limit_is_retryable(self):
        responses = {
            ("POST", "/groups"): _FakeResponse(429, {"message": "Too Many Attempts"}, headers={"Retry-After": "30"})
        }
        calls: list = []
        sender = make_sender(responses, calls)
        result = sender.send("to@example.com", "Subj", "Body")
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)
        self.assertIn("429", result.error)

    def test_server_error_is_retryable(self):
        responses = {("POST", "/groups"): _FakeResponse(503, {"message": "Service Unavailable"})}
        calls: list = []
        sender = make_sender(responses, calls)
        result = sender.send("to@example.com", "Subj", "Body")
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)

    def test_network_error_is_retryable(self):
        class _RaisingClient:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def request(self, *a, **kw):
                raise httpx.ConnectError("boom", request=httpx.Request("POST", "https://x/api/groups"))

        sender = MailerLiteSender(
            api_token="t",
            sender_email="e@x.com",
            sender_name="N",
            http_client_factory=lambda **kw: _RaisingClient(),
        )
        result = sender.send("to@example.com", "Subj", "Body")
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)
        self.assertIn("Network error", result.error)

    def test_dry_run_never_calls_send(self):
        # DryRunEmailSender lives in the backend service layer, not here --
        # this test just documents that MailerLiteSender.send() has no
        # dry-run branch of its own: dry-run must be handled by never
        # constructing/calling a real MailerLiteSender at all (see
        # backend/app/services/sending_service.DryRunEmailSender).
        pass


class MailerLiteCheckConfigurationTests(unittest.TestCase):
    def test_reports_not_configured_without_raising(self):
        sender = MailerLiteSender(api_token="", sender_email="", sender_name="")
        result = sender.check_configuration()
        self.assertFalse(result["configured"])
        self.assertFalse(result["authenticated"])
        self.assertNotIn("api_token", result)

    def test_reports_authenticated_on_success_without_leaking_token(self):
        responses = {("GET", "/subscribers"): _FakeResponse(200, {"data": []})}
        calls: list = []
        sender = make_sender(responses, calls)
        result = sender.check_configuration()
        self.assertTrue(result["configured"])
        self.assertTrue(result["authenticated"])
        serialized = json.dumps(result)
        self.assertNotIn("tok_123", serialized)

    def test_reports_auth_failure(self):
        responses = {("GET", "/subscribers"): _FakeResponse(401, {"message": "Unauthenticated."})}
        calls: list = []
        sender = make_sender(responses, calls)
        result = sender.check_configuration()
        self.assertTrue(result["configured"])
        self.assertFalse(result["authenticated"])
        self.assertIn("unauthorized", result["error"].lower())

    def test_never_leaks_token_in_send_test_email_error(self):
        responses = {("POST", "/groups"): _FakeResponse(401, {"message": "bad token"})}
        calls: list = []
        sender = make_sender(responses, calls)
        result = sender.send_test_email("someone@example.com")
        self.assertNotIn("tok_123", result.error)


if __name__ == "__main__":
    unittest.main()
