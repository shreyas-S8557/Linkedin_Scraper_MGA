"""MailerLite REST API sending adapter -- replaces GmailSender/SMTP in the
Send stage (see gmail_sender.py's module docstring for the SMTP adapter this
replaces; that module is left in place, unused, as an optional legacy
provider -- see backend/app/services/sending_service.py's EMAIL_PROVIDER
switch).

WHY THIS LOOKS THE WAY IT DOES -- MailerLite has no SMTP relay and no
"send one transactional email to one address" endpoint. Its REST API
(https://connect.mailerlite.com/api) is a *marketing* API built around
subscribers, groups, and campaigns: a campaign is sent to a group (or
segment), not to an arbitrary ad-hoc address with its own one-off
subject/body. There is no bulk/newsletter equivalence assumed here -- this
module never sends to more than the one intended recipient.

To send ONE individualized email to ONE prospect, without ever touching any
other subscriber in the account, `send()` does exactly this, in order:

  1. POST /api/groups              -- create a brand-new, empty group that
                                       exists for this single send only
                                       (name embeds a uuid so it can never
                                       collide with a real marketing group).
  2. POST /api/subscribers          -- upsert the recipient and add them to
                                       that new group in the same call
                                       (MailerLite's subscriber upsert is
                                       non-destructive: it only adds groups,
                                       never removes the recipient from
                                       groups they're already in).
  3. POST /api/campaigns            -- create a `regular` campaign whose
                                       *only* target is `groups: [group_id]`
                                       from step 1, with this lead's
                                       generated subject/body as the
                                       campaign's one email variant.
  4. POST /api/campaigns/{id}/schedule  -- {"delivery": "instant"}, which is
                                       MailerLite's "send now" action.

Because the group created in step 1 contains exactly one subscriber and
nothing else is ever added to it, the campaign in step 3 can only ever
reach that one person -- never the account's real subscriber list, and
never another prospect's send running concurrently (each send gets its own
group). This is the "smallest clean abstraction that works with the
existing workflow" called for by the migration brief, given what
MailerLite's actual API supports; see MAILERLITE_INTEGRATION.md for the
full writeup of why a raw "send email" call doesn't exist on this API.

KNOWN CONSEQUENCES of using a campaign/group mechanism for one-to-one send,
all documented here so they're not a surprise later:
  - Every send costs 3-4 API calls against MailerLite's 120 req/min global
    rate limit (see RATE_LIMIT_PER_MINUTE below), not 1 -- this caps
    throughput well below what raw SMTP or a real transactional API would
    allow. Plan campaign send volume accordingly.
  - `emails.*.from` must already be a MailerLite-verified sender address --
    MailerLite rejects the campaign (422) otherwise. See
    MailerLiteSenderNotVerified.
  - If the campaign `content` field doesn't include its own unsubscribe
    link/account footer, MailerLite appends its own default footer
    automatically (see docs) -- so every message sent this way carries a
    MailerLite-branded footer that a raw SMTP send never had. This module
    does not attempt to suppress that; it's a MailerLite platform
    requirement, not a bug here.
  - The ephemeral one-subscriber groups created in step 1 are not deleted
    automatically (deleting a group before MailerLite finishes queuing the
    campaign's send risks silently dropping the send -- see
    `cleanup_group` below for the opt-in, best-effort alternative). They
    will accumulate in the MailerLite account over time; this is a known,
    accepted cost of using MailerLite for one-to-one sends and is called
    out again in the final integration report.
  - "sent" here means "MailerLite accepted the campaign and scheduled it
    for instant delivery," not "the recipient's inbox has it yet" --
    exactly the same caveat that already applied to the Gmail/SMTP
    adapter's SendResult (an accepted SMTP send is not a delivery
    receipt either).
"""

from __future__ import annotations

import html
import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import httpx

BASE_URL = "https://connect.mailerlite.com/api"
RATE_LIMIT_PER_MINUTE = 120  # MailerLite's documented global rate limit
DEFAULT_TIMEOUT_SECONDS = 20.0


class MailerLiteCredentialsError(RuntimeError):
    """Raised when MAILERLITE_API_TOKEN / MAILERLITE_SENDER_EMAIL /
    MAILERLITE_SENDER_NAME aren't available -- mirrors
    GmailCredentialsError's role for the Gmail adapter."""


class MailerLiteSenderNotVerified(RuntimeError):
    """Raised when MailerLite rejects the configured sender address because
    it hasn't been verified on the MailerLite account. Deliberately its own
    exception (rather than a generic API error) so callers can surface the
    exact message the migration brief asks for: "MailerLite sender email is
    not verified." Never silently falls back to a different sender."""


@dataclass
class SendResult:
    """Outcome of one send attempt. Same shape/contract as
    gmail_sender.SendResult (success/message_id/error) so
    email_sending.send_queued_email's calling code needs no change, plus
    one additive field:

    `retryable` -- whether send_queued_email's retry loop should bother
    trying again. Defaults to True (unknown-cause failures are assumed
    possibly transient, matching the old Gmail adapter's blanket-retry
    behavior). Set False for failures a retry can never fix (bad
    credentials, an unverified sender, a MailerLite-rejected recipient) so
    a permanent failure doesn't burn through max_retries pointlessly before
    landing on SEND_FAILED anyway.
    """

    success: bool
    message_id: str = ""
    error: str = ""
    retryable: bool = True


def _env(name: str) -> str:
    return os.environ.get(name, "") or ""


def _body_to_html(body: str) -> str:
    """job.body is plain text (see email_generation.py). MailerLite requires
    `emails.*.content` to be valid HTML, so this does the minimal, safe
    conversion: escape it, then turn newlines into <br> -- no markdown/rich
    formatting is invented that wasn't in the original generated text."""
    escaped = html.escape(body)
    return "<html><body><p>" + escaped.replace("\n", "<br>\n") + "</p></body></html>"


class MailerLiteSender:
    """Thin, mockable adapter around the MailerLite REST API.

    Credentials are only ever read from environment variables (or passed
    explicitly, which is how tests inject fake ones) -- never hard-coded,
    same convention as GmailSender. `http_client_factory` is injectable so
    tests can swap in a fake HTTP client and never touch the network.
    """

    def __init__(
        self,
        api_token: str | None = None,
        sender_email: str | None = None,
        sender_name: str | None = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        http_client_factory: Callable[..., httpx.Client] = httpx.Client,
        cleanup_group: bool = False,
    ) -> None:
        self.api_token = api_token if api_token is not None else _env("MAILERLITE_API_TOKEN")
        self.sender_email = (
            sender_email if sender_email is not None else _env("MAILERLITE_SENDER_EMAIL")
        )
        self.sender_name = (
            sender_name if sender_name is not None else _env("MAILERLITE_SENDER_NAME")
        )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._http_client_factory = http_client_factory
        # Best-effort cleanup of the one-off group after a successful send.
        # Off by default -- see the module docstring's "KNOWN CONSEQUENCES"
        # note on why immediate deletion risks dropping an in-flight send.
        self.cleanup_group = cleanup_group

    # -- credential / config validation --------------------------------------

    def validate_credentials(self) -> None:
        """Raise MailerLiteCredentialsError if required config isn't set.
        Pure config check -- no network call."""
        missing = []
        if not self.api_token:
            missing.append("MAILERLITE_API_TOKEN")
        if not self.sender_email:
            missing.append("MAILERLITE_SENDER_EMAIL")
        if not self.sender_name:
            missing.append("MAILERLITE_SENDER_NAME")
        if missing:
            raise MailerLiteCredentialsError(
                "Missing MailerLite configuration: "
                + ", ".join(missing)
                + ". Set these as environment variables (e.g. in a .env file). "
                "MAILERLITE_API_TOKEN is a Bearer API token generated from "
                "MailerLite -> Integrations -> Developer API, not your "
                "account email/password."
            )

    # -- low-level HTTP -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.Client:
        return self._http_client_factory(
            base_url=self.base_url, headers=self._headers(), timeout=self.timeout
        )

    def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> Any:
        """One HTTP call against the MailerLite API. Never raises the
        underlying httpx exception to callers of `send()` -- see `send()`'s
        try/except, which is the only place this is called from during a
        send. Used directly (with its own error handling) by
        check_configuration() for the lightweight, read-only connectivity
        test.
        """
        with self._client() as client:
            resp = client.request(method, path, json=json)
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # -- connectivity / configuration check (no writes) ------------------------

    def check_configuration(self) -> dict[str, Any]:
        """Read-only verification for the settings/"test connection" API
        (item 12 of the migration brief): confirms the token authenticates
        and the API is reachable, WITHOUT creating any subscriber, group, or
        campaign, and WITHOUT ever returning the token itself.

        MailerLite's API has no dedicated "is this sender domain verified"
        GET endpoint, so `sender_verified` here reflects only that a
        MAILERLITE_SENDER_EMAIL is configured (format-checked), not that
        MailerLite has actually verified it -- an unverified sender is only
        discoverable by MailerLite rejecting an actual campaign/send with a
        422 (see MailerLiteSenderNotVerified). This is surfaced explicitly
        in the returned dict rather than glossed over.
        """
        result: dict[str, Any] = {
            "provider": "mailerlite",
            "configured": False,
            "authenticated": False,
            "sender": self.sender_email,
            "sender_verified_checked": False,
            "error": "",
        }
        try:
            self.validate_credentials()
        except MailerLiteCredentialsError as exc:
            result["error"] = str(exc)
            return result
        result["configured"] = True
        try:
            # Cheapest authenticated GET available: fetch a single
            # subscriber page. Read-only, no side effects.
            self._request("GET", "/subscribers", json=None)
            result["authenticated"] = True
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                result["error"] = "MailerLite rejected the API token (unauthorized)."
            else:
                result["error"] = f"MailerLite API error (HTTP {status})."
        except httpx.RequestError as exc:
            result["error"] = f"Could not reach the MailerLite API: {exc}"
        return result

    # -- sending ---------------------------------------------------------------

    def send(
        self,
        to_address: str,
        subject: str,
        body_html: str,
        *,
        body_text: str | None = None,  # noqa: ARG002 -- kept for interface parity with GmailSender
        from_name: str = "",
        attachments: list[str] | None = None,  # noqa: ARG002 -- MailerLite campaigns don't support ad-hoc attachments
    ) -> SendResult:
        """Send one individualized email to `to_address` via an isolated,
        single-subscriber MailerLite group + campaign (see module
        docstring). Never raises for send-time failures -- always returns a
        SendResult, matching GmailSender.send()'s contract exactly.
        """
        if not to_address or not to_address.strip():
            return SendResult(success=False, error="Empty recipient address", retryable=False)
        if not subject.strip() or not body_html.strip():
            return SendResult(success=False, error="Empty subject or body", retryable=False)

        try:
            self.validate_credentials()
        except MailerLiteCredentialsError as exc:
            return SendResult(success=False, error=str(exc), retryable=False)

        group_id: str | None = None
        try:
            group_name = f"prospect-automation-outbox-{uuid.uuid4().hex}"
            group = self._request("POST", "/groups", json={"name": group_name})
            group_id = (group or {}).get("data", {}).get("id")
            if not group_id:
                return SendResult(
                    success=False, error="MailerLite did not return a group id", retryable=True
                )

            self._request(
                "POST",
                "/subscribers",
                json={"email": to_address, "groups": [group_id]},
            )

            sender_name = from_name or self.sender_name
            content_html = _body_to_html(body_html)
            campaign_payload = {
                "name": f"prospect-automation-{uuid.uuid4().hex[:12]}",
                "type": "regular",
                "emails": [
                    {
                        "subject": subject,
                        "from_name": sender_name,
                        "from": self.sender_email,
                        "content": content_html,
                    }
                ],
                "groups": [group_id],
            }
            campaign = self._request("POST", "/campaigns", json=campaign_payload)
            campaign_id = (campaign or {}).get("data", {}).get("id")
            if not campaign_id:
                return SendResult(
                    success=False, error="MailerLite did not return a campaign id", retryable=True
                )

            self._request(
                "POST", f"/campaigns/{campaign_id}/actions/schedule", json={"delivery": "instant"}
            )

            if self.cleanup_group:
                try:
                    self._request("DELETE", f"/groups/{group_id}", json=None)
                except Exception:  # noqa: BLE001 -- cleanup is best-effort, never fails the send
                    pass

            return SendResult(success=True, message_id=str(campaign_id))

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = _error_detail(exc)
            if status in (401, 403):
                return SendResult(
                    success=False,
                    error=f"MailerLite authentication failed (HTTP {status}): {detail}",
                    retryable=False,
                )
            if status == 422:
                if self.sender_email and self.sender_email.lower() in detail.lower():
                    return SendResult(
                        success=False,
                        error="MailerLite sender email is not verified.",
                        retryable=False,
                    )
                return SendResult(
                    success=False,
                    error=f"MailerLite rejected the request (validation error): {detail}",
                    retryable=False,
                )
            if status == 429:
                retry_after = exc.response.headers.get("Retry-After", "")
                return SendResult(
                    success=False,
                    error=f"MailerLite rate limit exceeded (429). Retry-After={retry_after}",
                    retryable=True,
                )
            if status >= 500:
                return SendResult(
                    success=False,
                    error=f"MailerLite server error (HTTP {status}): {detail}",
                    retryable=True,
                )
            return SendResult(
                success=False, error=f"MailerLite API error (HTTP {status}): {detail}", retryable=True
            )
        except httpx.RequestError as exc:
            return SendResult(success=False, error=f"Network error contacting MailerLite: {exc}", retryable=True)

    def send_test_email(self, to_address: str | None = None) -> SendResult:
        """A real test send to verify MailerLite authentication + sender
        config before running a campaign, mirroring
        GmailSender.send_test_email(). Defaults to sending to the
        configured sender address itself, exactly like the Gmail adapter
        did. Only ever sends to the single `to_address` given -- never a
        prospect pulled from the database (see the migration brief's
        "SEND ONE SAFE TEST EMAIL" requirement)."""
        target = to_address or self.sender_email
        return self.send(
            target,
            subject="Prospect Automation - MailerLite test email",
            body_html=(
                "This is a test email from the prospect-automation pipeline's "
                "MailerLiteSender adapter.\n\n"
                "If you received this, MailerLite sending is configured "
                "correctly and the pipeline is ready to send a real campaign."
            ),
        )


def _error_detail(exc: httpx.HTTPStatusError) -> str:
    try:
        data = exc.response.json()
    except Exception:  # noqa: BLE001
        return exc.response.text[:300]
    if isinstance(data, dict):
        message = data.get("message", "")
        errors = data.get("errors")
        if errors:
            return f"{message} {errors}".strip()
        return message or str(data)[:300]
    return str(data)[:300]
