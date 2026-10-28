from __future__ import annotations

import csv
import io
import time

from tests.helpers import seed_qualified_leads


def _wait(client, job_id, timeout=10):
    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        time.sleep(0.02)
    return body


def _campaign_with_generated_emails(client, count=3):
    """Same discover->qualify->find-emails->validate->generate pipeline as
    test_sending.py's _approved_campaign helper, but deliberately stops
    before approve-all -- these leads must land on EMAIL_GENERATED, not
    move on to APPROVED, so the export filter has something to exercise.
    """
    campaign = client.post(
        "/api/campaigns",
        json={"campaign_name": "Export Test", "target_leads": 10},
    ).json()
    seed_qualified_leads(campaign["campaign_id"], count=count)
    for stage in ("find-emails", "validate-emails", "generate-emails"):
        job = client.post(f"/api/campaigns/{campaign['campaign_id']}/{stage}").json()
        _wait(client, job["job_id"])
    return campaign


def _parse_csv(response) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(response.text)))


def test_export_returns_csv_with_expected_headers(client):
    campaign = _campaign_with_generated_emails(client)
    r = client.get(
        f"/api/campaigns/{campaign['campaign_id']}/prospects/export",
        params={"pipeline_status": "EMAIL_GENERATED"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "email_generated" in r.headers["content-disposition"].lower()

    rows = _parse_csv(r)
    assert set(rows[0].keys()) >= {
        "lead_id", "full_name", "email", "pipeline_status", "email_subject", "email_body",
    }


def test_export_email_generated_filter_includes_personalized_email(client):
    # The multi-stage find-emails -> validate-emails -> generate-emails
    # pipeline can legitimately drop a lead at any stage depending on its
    # (synthetic, in this test) data -- e.g. test_sending.py's own
    # _approved_campaign helper only ever asserts "sent >= 1", never an
    # exact count, for the same reason. This asserts at least one lead
    # made it all the way to EMAIL_GENERATED, and that every row which did
    # carries its own real personalized subject/body.
    campaign = _campaign_with_generated_emails(client, count=3)
    r = client.get(
        f"/api/campaigns/{campaign['campaign_id']}/prospects/export",
        params={"pipeline_status": "EMAIL_GENERATED"},
    )
    rows = _parse_csv(r)
    assert len(rows) >= 1
    for row in rows:
        assert row["pipeline_status"] == "EMAIL_GENERATED"
        # The personalized subject/body generated for this lead must ride
        # along as real CSV columns -- not just a lead_id someone would
        # have to look up separately.
        assert row["email_subject"].strip() != ""
        assert row["email_body"].strip() != ""


def test_export_filter_excludes_leads_in_other_statuses(client):
    campaign = _campaign_with_generated_emails(client, count=3)
    before = _parse_csv(
        client.get(
            f"/api/campaigns/{campaign['campaign_id']}/prospects/export",
            params={"pipeline_status": "EMAIL_GENERATED"},
        )
    )
    assert len(before) >= 1
    approved_lead_id = before[0]["lead_id"]

    # Approve exactly one -- it moves EMAIL_GENERATED -> APPROVED and must
    # then be excluded from an EMAIL_GENERATED-filtered export.
    approve_resp = client.post(f"/api/emails/{approved_lead_id}/approve")
    assert approve_resp.status_code == 200

    after = _parse_csv(
        client.get(
            f"/api/campaigns/{campaign['campaign_id']}/prospects/export",
            params={"pipeline_status": "EMAIL_GENERATED"},
        )
    )
    assert len(after) == len(before) - 1
    assert approved_lead_id not in {row["lead_id"] for row in after}


def test_export_without_status_filter_returns_every_lead(client):
    campaign = _campaign_with_generated_emails(client, count=2)
    r = client.get(f"/api/campaigns/{campaign['campaign_id']}/prospects/export")
    rows = _parse_csv(r)
    assert len(rows) == 2
    assert "email_generated" not in r.headers["content-disposition"].lower()


def test_export_404_for_unknown_campaign(client):
    r = client.get(
        "/api/campaigns/does-not-exist/prospects/export",
        params={"pipeline_status": "EMAIL_GENERATED"},
    )
    assert r.status_code == 404


def test_export_empty_result_still_returns_valid_csv_with_headers_only(client):
    campaign = client.post(
        "/api/campaigns", json={"campaign_name": "Empty Export Test", "target_leads": 5}
    ).json()
    r = client.get(
        f"/api/campaigns/{campaign['campaign_id']}/prospects/export",
        params={"pipeline_status": "EMAIL_GENERATED"},
    )
    assert r.status_code == 200
    rows = _parse_csv(r)
    assert rows == []
