from __future__ import annotations

from pydantic import BaseModel


class CampaignStatsOut(BaseModel):
    discovered: int = 0
    qualified: int = 0
    email_found: int = 0
    validated: int = 0
    generated: int = 0
    approved: int = 0
    queued: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0


class FunnelStageOut(BaseModel):
    stage: str
    count: int
    drop_off_from_previous: int = 0


class CampaignFunnelOut(BaseModel):
    campaign_id: str
    stages: list[FunnelStageOut]
    rejection_reasons: dict[str, int] = {}
    # Sep 2026 "stop over-filtering" fix: a breakdown of *why* FILTERED_OUT
    # leads were filtered (one of pipeline.quality.FILTER_* codes -> count),
    # so the UI can show the actual reason instead of a bare FILTERED_OUT
    # count. Empty when there are no filtered leads yet.
    filtered_out_reasons: dict[str, int] = {}


class DashboardStatsOut(BaseModel):
    total_campaigns: int = 0
    active_campaigns: int = 0
    total_prospects: int = 0
    qualified_prospects: int = 0
    emails_found: int = 0
    emails_validated: int = 0
    emails_approved: int = 0
    emails_sent: int = 0
