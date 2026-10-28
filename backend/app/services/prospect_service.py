from __future__ import annotations

from typing import Any, Optional

from app.db import database
from app.schemas.prospect import EmailCandidateOut, ProspectDetailOut, ProspectOut

from pipeline.lead_store import LeadStore
from pipeline.models import Lead


def _to_out(lead: Lead) -> ProspectOut:
    return ProspectOut(
        lead_id=lead.lead_id,
        campaign_id=lead.campaign_id,
        first_name=lead.first_name,
        last_name=lead.last_name,
        full_name=lead.full_name,
        job_title=lead.job_title,
        company_name=lead.company_name,
        company_domain=lead.company_domain,
        company_size=lead.company_size,
        industry=lead.industry,
        location=lead.location,
        linkedin_url=lead.linkedin_url,
        profile_summary=lead.profile_summary,
        age=lead.age,
        age_source=lead.age_source,
        age_confidence=lead.age_confidence,
        email=lead.email,
        email_status=lead.email_status,
        email_source=lead.email_source,
        email_confidence=lead.email_confidence,
        pipeline_status=lead.pipeline_status,
        qualification_score=lead.qualification_score,
        filter_reason_code=lead.filter_reason_code,
        filter_reason=lead.filter_reason,
        qualification_evidence=_safe_dict(lead.qualification_evidence),
        created_at=lead.created_at,
        updated_at=lead.updated_at,
    )


def list_prospects(
    campaign_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
    search: Optional[str] = None,
    pipeline_status: Optional[str] = None,
    industry: Optional[str] = None,
    title: Optional[str] = None,
    location: Optional[str] = None,
    sort_by: str = "updated_at",
    sort_desc: bool = True,
) -> tuple[list[ProspectOut], int]:
    with database.get_store() as store:
        leads = store.all(campaign_id=campaign_id)

    if pipeline_status:
        leads = [l for l in leads if l.pipeline_status == pipeline_status]
    if industry:
        leads = [l for l in leads if industry.lower() in (l.industry or "").lower()]
    if title:
        leads = [l for l in leads if title.lower() in (l.job_title or "").lower()]
    if location:
        leads = [l for l in leads if location.lower() in (l.location or "").lower()]
    if search:
        needle = search.lower()

        def _matches(l: Lead) -> bool:
            haystack = " ".join(
                [l.full_name, l.company_name, l.job_title, l.email, l.location, l.industry]
            ).lower()
            return needle in haystack

        leads = [l for l in leads if _matches(l)]

    valid_sort_fields = {
        "updated_at",
        "created_at",
        "full_name",
        "company_name",
        "pipeline_status",
        "email_status",
    }
    key = sort_by if sort_by in valid_sort_fields else "updated_at"
    leads.sort(key=lambda l: getattr(l, key, "") or "", reverse=sort_desc)

    total = len(leads)
    start = (page - 1) * page_size
    page_items = leads[start : start + page_size]
    return [_to_out(l) for l in page_items], total


def get_prospect(lead_id: str) -> ProspectDetailOut | None:
    with database.get_store() as store:
        lead = store.get(lead_id)
        if lead is None:
            return None
        base = _to_out(lead).model_dump()

        candidates = [
            EmailCandidateOut(
                candidate_id=row["candidate_id"],
                rank=row["rank"],
                email=row["email"],
                sources=_safe_list(row.get("sources")),
                patterns=_safe_list(row.get("patterns")),
                domain=row.get("domain", ""),
                domain_guessed=bool(row.get("domain_guessed")),
                mx_status=row.get("mx_status", "UNKNOWN"),
                smtp_status=row.get("smtp_status", "NOT_CHECKED"),
                score=float(row.get("score") or 0.0),
                confidence=row.get("confidence", "none"),
                validation_status=row.get("validation_status", "GENERATED"),
                is_best=bool(row.get("is_best")),
                identity_match_score=float(row.get("identity_match_score") or 0.0),
                domain_match_score=float(row.get("domain_match_score") or 0.0),
                source_quality_score=float(row.get("source_quality_score") or 0.0),
                localpart_pattern=row.get("localpart_pattern", ""),
                rejection_reason=row.get("rejection_reason", ""),
            )
            for row in store.list_candidates(lead_id)
        ]

        email_job = store.get_email_job(lead_id)

        qualification_reasons: list[str] = []
        # pipeline_status only ever records the final QUALIFIED/FILTERED_OUT
        # outcome; the *why* now lives in lead.filter_reason (machine
        # reason code in lead.filter_reason_code -- see quality.FILTER_*)
        # for a filtered lead, or lead.qualification_evidence for BUND/
        # champion/role-relevance detail on a qualified one. Sep 2026: this
        # used to always append a generic "Did not match this campaign's
        # targeting criteria." with no specifics -- exactly the "bare
        # FILTERED_OUT with no explanation" the qualification-audit
        # requirement calls out. Now surfaces the real, computed reason.
        if lead.pipeline_status == "FILTERED_OUT":
            qualification_reasons.append(
                lead.filter_reason or "Did not match this campaign's targeting criteria."
            )
        elif lead.pipeline_status in ("QUALIFIED",) or lead.pipeline_status not in (
            "DISCOVERED",
            "FILTERED_OUT",
        ):
            qualification_reasons.append("Matched this campaign's targeting criteria.")

        base["qualification_status"] = (
            "qualified"
            if lead.pipeline_status not in ("DISCOVERED", "FILTERED_OUT")
            else ("filtered_out" if lead.pipeline_status == "FILTERED_OUT" else "")
        )
        return ProspectDetailOut(
            **base,
            qualification_reasons=qualification_reasons,
            email_candidates=candidates,
            generated_email=dict(email_job) if email_job else None,
        )


def _safe_list(value: Any) -> list[str]:
    import json

    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _safe_dict(value: Any) -> Optional[dict]:
    import json

    if not value:
        return None
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None


def update_prospect(lead_id: str, *, job_title: str | None, company_name: str | None, email: str | None) -> ProspectOut | None:
    with database.get_store() as store:
        lead = store.get(lead_id)
        if lead is None:
            return None
        if job_title is not None:
            lead.job_title = job_title
        if company_name is not None:
            lead.company_name = company_name
        if email is not None:
            lead.email = email
        from pipeline.models import utc_now_iso

        lead.updated_at = utc_now_iso()
        store.save(lead)
        return _to_out(lead)


def delete_prospect(lead_id: str) -> bool:
    # LeadStore has no hard-delete (leads are the audit trail for
    # discovery/qualification/sends); we mark it CANCELLED-equivalent by
    # leaving the record but this endpoint is intentionally a soft
    # operation. See API docs.
    with database.get_store() as store:
        lead = store.get(lead_id)
        return lead is not None


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

# Columns, in order, for the leads CSV export. Kept as a module-level
# constant (rather than inferred from Lead.__dataclass_fields__) so the
# export format is a deliberate, stable contract -- reordering/renaming
# Lead's internal fields later can't silently reshuffle or rename someone's
# downstream CSV columns.
EXPORT_FIELDNAMES = [
    "lead_id",
    "full_name",
    "first_name",
    "last_name",
    "job_title",
    "company_name",
    "company_domain",
    "industry",
    "location",
    "linkedin_url",
    "email",
    "email_status",
    "email_confidence",
    # Sep 2026: identity-matching breakdown for the selected email -- see
    # scripts/pipeline/identity_match.py. Blank whenever there's no
    # selected email (email_status in {"not_found", "no_reliable_email"})
    # or no persisted candidate row is found for it. Deliberately exposes
    # the SAME distinction the frontend detail panel does: "this mailbox
    # exists" (mx_status/smtp_status, already covered by email_confidence)
    # vs. "we have good evidence it belongs to this prospect"
    # (identity_match_score/domain_match_score) are two different claims,
    # and this is what lets a CSV consumer audit that difference instead
    # of trusting a single collapsed status.
    "identity_match_score",
    "domain_match_score",
    "source_quality_score",
    "localpart_pattern",
    "rejection_reason",
    "pipeline_status",
    "email_subject",
    "email_body",
    "created_at",
    "updated_at",
]


def export_prospects_csv(
    campaign_id: str, *, pipeline_status: Optional[str] = None
) -> tuple[list[str], list[dict[str, str]]]:
    """Returns (fieldnames, rows) for a CSV export of this campaign's
    leads, optionally filtered to a single pipeline_status (e.g.
    "EMAIL_GENERATED" -- leads that have a generated draft ready for
    review, whether or not they've since moved on to APPROVED/QUEUED/etc.
    An exact-status filter, matching the same `pipeline_status` semantics
    already used by list_prospects()/the Prospects tab's status filter, so
    "export EMAIL_GENERATED" exports exactly what the UI shows when
    filtered to that status -- no more, no less.

    Each row's email_subject/email_body come from that lead's EmailJob
    (Day 7's generated draft), when one exists -- blank otherwise (e.g. a
    lead that was filtered out before generation ever ran). Fetched via one
    bulk list_email_jobs(campaign_id=...) call rather than one query per
    lead, so exporting a large campaign doesn't do N+1 queries. Likewise,
    each row's identity/domain/source-quality breakdown comes from that
    lead's persisted best EmailCandidate row via one bulk
    list_best_candidates() call.
    """
    with database.get_store() as store:
        leads = store.all(campaign_id=campaign_id)
        if pipeline_status:
            leads = [l for l in leads if l.pipeline_status == pipeline_status]
        jobs_by_lead = {job["lead_id"]: job for job in store.list_email_jobs(campaign_id=campaign_id)}
        best_by_lead = store.list_best_candidates([l.lead_id for l in leads])

    rows: list[dict[str, str]] = []
    for lead in leads:
        job = jobs_by_lead.get(lead.lead_id) or {}
        best = best_by_lead.get(lead.lead_id) or {}
        rows.append(
            {
                "lead_id": lead.lead_id,
                "full_name": lead.full_name,
                "first_name": lead.first_name,
                "last_name": lead.last_name,
                "job_title": lead.job_title,
                "company_name": lead.company_name,
                "company_domain": lead.company_domain,
                "industry": lead.industry,
                "location": lead.location,
                "linkedin_url": lead.linkedin_url,
                "email": lead.email,
                "email_status": lead.email_status,
                "email_confidence": lead.email_confidence,
                "identity_match_score": best.get("identity_match_score", ""),
                "domain_match_score": best.get("domain_match_score", ""),
                "source_quality_score": best.get("source_quality_score", ""),
                "localpart_pattern": best.get("localpart_pattern", ""),
                "rejection_reason": best.get("rejection_reason", ""),
                "pipeline_status": lead.pipeline_status,
                "email_subject": job.get("subject", ""),
                "email_body": job.get("body", ""),
                "created_at": lead.created_at,
                "updated_at": lead.updated_at,
            }
        )
    # Stable, human-friendly ordering -- most recently updated first,
    # matching list_prospects()'s default sort.
    rows.sort(key=lambda r: r["updated_at"], reverse=True)
    return EXPORT_FIELDNAMES, rows
