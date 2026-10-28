from __future__ import annotations

from typing import Any

from pipeline.mailerlite_sender import MailerLiteSender


class TestSendNotAllowed(PermissionError):
    """Raised when a test-send is attempted without an explicit,
    caller-supplied recipient address (see the migration brief's
    "SEND ONE SAFE TEST EMAIL" requirement -- this must never fall back to
    picking a prospect from the database)."""


def test_connection() -> dict[str, Any]:
    """Read-only MailerLite configuration/connectivity check. Never touches
    subscribers/groups/campaigns and never returns the API token -- see
    MailerLiteSender.check_configuration()."""
    sender = MailerLiteSender()
    result = sender.check_configuration()
    return result


def send_test_email(to_address: str) -> dict[str, Any]:
    """Send exactly one MailerLite test email, to the explicitly-given
    `to_address` only. Never selects a prospect from the database."""
    if not to_address or not to_address.strip():
        raise TestSendNotAllowed("A 'to' address is required for a test send.")
    sender = MailerLiteSender()
    result = sender.send_test_email(to_address.strip())
    return {
        "provider": "mailerlite",
        "to": to_address.strip(),
        "success": result.success,
        "message_id": result.message_id,
        "error": result.error,
    }
