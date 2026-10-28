from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.config import ALLOW_LIVE_SEND, EMAIL_PROVIDER
from app.db import database
from app.workers.jobs import Job, JobControl, job_manager

from pipeline import campaign as campaign_pipeline
from pipeline import campaign_control
from pipeline import email_sending
from pipeline.gmail_sender import GmailCredentialsError, GmailSender
from pipeline.mailerlite_sender import (
    MailerLiteCredentialsError,
    MailerLiteSender,
    SendResult,
)
from pipeline.models import PipelineStatus


class SendModeNotAllowed(PermissionError):
    pass


class DryRunEmailSender:
    """Drop-in replacement for any real EmailSender that never touches the
    network. Used for `dry_run` and `test` send modes (and always used for
    automated tests -- "DO NOT send emails during automated tests").
    Implements the same `.send()` contract (returns a SendResult) that
    email_sending.send_queued_email expects, so the exact same
    queueing/state-machine/retry code path is exercised without ever
    calling out to MailerLite (or, previously, smtplib).
    """

    def send(self, to_email: str, subject: str, body: str, *, from_name: str = "") -> SendResult:
        return SendResult(success=True, message_id=f"dry-run-{uuid.uuid4().hex}", error="")


# EmailSender provider abstraction (migration from Gmail/SMTP to MailerLite):
#
#   EmailSender
#       +-- MailerLiteSender   (default -- see app.config.EMAIL_PROVIDER)
#       +-- GmailSender        (legacy/optional -- not selected by default;
#                                only used if EMAIL_PROVIDER=gmail is set
#                                explicitly. Not removed from the codebase
#                                so a rollback doesn't require restoring
#                                deleted code, but it is never accidentally
#                                selected.)
#
# Both classes share the same `.send(to, subject, body, *, from_name="") ->
# SendResult` contract, so nothing downstream of _resolve_sender() (i.e.
# email_sending.py) needs to know or care which provider is active.
_PROVIDERS = {
    "mailerlite": MailerLiteSender,
    "gmail": GmailSender,
}


def _resolve_sender(send_mode: str) -> GmailSender | MailerLiteSender | DryRunEmailSender:
    if send_mode == "live":
        if not ALLOW_LIVE_SEND:
            raise SendModeNotAllowed(
                "Live sending is disabled on this backend (PROSPECT_ALLOW_LIVE_SEND is not "
                "set). This is a deliberate safety default -- see DEVELOPMENT.md."
            )
        provider_cls = _PROVIDERS.get(EMAIL_PROVIDER)
        if provider_cls is None:
            raise SendModeNotAllowed(
                f"Unknown EMAIL_PROVIDER={EMAIL_PROVIDER!r}. Must be one of: "
                f"{', '.join(sorted(_PROVIDERS))}."
            )
        sender = provider_cls()
        try:
            sender.validate_credentials()
        except (MailerLiteCredentialsError, GmailCredentialsError) as exc:
            raise SendModeNotAllowed(str(exc)) from exc
        return sender
    return DryRunEmailSender()


def _run_send_job(job: Job, ctl: JobControl, campaign_id: str, send_mode: str) -> dict[str, Any]:
    ctl.set_phase("sending")
    sender = _resolve_sender(send_mode)
    with database.get_store() as store:
        template = campaign_pipeline.load_campaign(store, campaign_id)

        # Queue every APPROVED lead first.
        approved = store.list_by_status(PipelineStatus.APPROVED, campaign_id=campaign_id)
        for lead in approved:
            ctl.checkpoint()
            if campaign_control.get_run_state(store, campaign_id) != campaign_control.RUN_STATE_RUNNING:
                break
            try:
                email_sending.queue_approved_email(store, lead)
            except Exception:  # noqa: BLE001 -- one bad lead must not abort the batch
                continue

        queued = store.list_by_status(PipelineStatus.QUEUED, campaign_id=campaign_id)
        ctl.set_total(len(queued), phase="sending")
        sent = failed = 0
        for lead in queued:
            ctl.checkpoint()
            if campaign_control.get_run_state(store, campaign_id) != campaign_control.RUN_STATE_RUNNING:
                break
            updated = email_sending.send_queued_email(store, sender, lead.lead_id, campaign=template)
            ok = updated.status == PipelineStatus.SENT
            sent += int(ok)
            failed += int(not ok)
            ctl.advance(success=ok, message=updated.full_name or updated.lead_id)
    return {"sent": sent, "failed": failed, "send_mode": send_mode}


def start_sending(campaign_id: str, *, send_mode: str) -> Job:
    return job_manager.create(
        "sending", campaign_id, lambda job, ctl: _run_send_job(job, ctl, campaign_id, send_mode)
    )


def pause_campaign(campaign_id: str) -> dict[str, str]:
    with database.get_store() as store:
        control = campaign_control.pause_campaign(store, campaign_id)
    return control.to_dict()


def resume_campaign(campaign_id: str) -> dict[str, str]:
    with database.get_store() as store:
        control = campaign_control.resume_campaign(store, campaign_id)
    return control.to_dict()


def stop_campaign(campaign_id: str) -> dict[str, str]:
    with database.get_store() as store:
        control = campaign_control.stop_campaign(store, campaign_id)
    return control.to_dict()


def sending_summary(campaign_id: str) -> dict[str, int]:
    with database.get_store() as store:
        sends = store.list_email_sends(campaign_id=campaign_id)
    counts = {"queued": 0, "sending": 0, "sent": 0, "failed": 0, "cancelled": 0}
    status_map = {
        "QUEUED": "queued",
        "SENDING": "sending",
        "SENT": "sent",
        "SEND_FAILED": "failed",
        "CANCELLED": "cancelled",
    }
    for row in sends:
        key = status_map.get(row.get("send_status") or "")
        if key:
            counts[key] += 1
    return counts
