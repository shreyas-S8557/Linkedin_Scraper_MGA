from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class EmailCandidateOut(BaseModel):
    candidate_id: str
    rank: int
    email: str
    sources: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    domain: str = ""
    domain_guessed: bool = False
    mx_status: str = "UNKNOWN"
    smtp_status: str = "NOT_CHECKED"
    score: float = 0.0
    confidence: str = "none"
    validation_status: str = "GENERATED"
    is_best: bool = False
    # Sep 2026: identity/domain/source-quality assessment (see
    # scripts/pipeline/identity_match.py) -- deliberately separate from
    # `confidence` (the technical MX/SMTP-driven `validation_status`
    # rollup) and from `score` (the combined overall). These are what let
    # the UI show "this mailbox exists" and "we have good evidence it
    # belongs to this prospect" as two different things, per the reported
    # davis@gmail.com-style bug: a technically-deliverable but identity-
    # unproven guess.
    identity_match_score: float = 0.0
    domain_match_score: float = 0.0
    source_quality_score: float = 0.0
    localpart_pattern: str = ""
    rejection_reason: str = ""


class ProspectOut(BaseModel):
    lead_id: str
    campaign_id: str
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    job_title: str = ""
    company_name: str = ""
    company_domain: str = ""
    company_size: str = ""
    industry: str = ""
    location: str = ""
    linkedin_url: str = ""
    profile_summary: str = ""

    age: str = ""
    age_source: str = ""
    age_confidence: str = ""

    email: str = ""
    email_status: str = "unknown"
    email_source: str = ""
    email_confidence: str = ""

    pipeline_status: str = "DISCOVERED"
    qualification_status: str = ""

    # Sep 2026 "stop over-filtering" fix (see
    # pipeline/quality.evaluate_qualification): a 0-100 weighted
    # qualification score, plus a machine-readable filter_reason_code (one
    # of pipeline/quality.FILTER_*) and human-readable filter_reason --
    # both "" for a QUALIFIED/not-yet-qualified lead, always populated for
    # a FILTERED_OUT one. Lets the UI show *why* a prospect was filtered
    # instead of a bare "FILTERED_OUT" status.
    qualification_score: str = ""
    filter_reason_code: str = ""
    filter_reason: str = ""

    # Evidence-based BUND / champion / role-relevance scoring (Aug 2026
    # requirements) -- parsed from Lead.qualification_evidence. None means
    # not yet scored (e.g. a lead that hasn't reached qualification yet).
    # Every non-"unknown" value inside carries its own evidence string; see
    # pipeline/qualification_scoring.py for the full contract.
    qualification_evidence: Optional[dict] = None

    created_at: str = ""
    updated_at: str = ""


class ProspectDetailOut(ProspectOut):
    qualification_reasons: list[str] = Field(default_factory=list)
    email_candidates: list[EmailCandidateOut] = Field(default_factory=list)
    generated_email: Optional[dict] = None


class ProspectUpdate(BaseModel):
    job_title: Optional[str] = None
    company_name: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None


class ProspectListOut(BaseModel):
    items: list[ProspectOut]
    page: int
    page_size: int
    total: int
