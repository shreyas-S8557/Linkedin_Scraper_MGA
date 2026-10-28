"""Item 12/13 of the MailerLite migration brief: a backend-only way to
verify MailerLite credentials/configuration, and a safe, explicit,
one-address-only test send -- both independent of the campaign Send stage
so a person can validate the provider before ever running a real campaign.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

from app.services import email_provider_service

router = APIRouter(prefix="/api/email/provider", tags=["email-provider"])


class TestSendRequest(BaseModel):
    to: EmailStr


@router.post("/test")
def test_connection() -> dict:
    return email_provider_service.test_connection()


@router.post("/test-send")
def test_send(payload: TestSendRequest) -> dict:
    try:
        return email_provider_service.send_test_email(str(payload.to))
    except email_provider_service.TestSendNotAllowed as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
