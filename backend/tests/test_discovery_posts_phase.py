"""Confirms discovery_service._run_discovery_job passes the "posts" phase
to orchestrator.run_discovery only when the campaign's target_config has
search_posts=True -- and never otherwise, since sources/linkedin_posts_search
is a slower/lower-precision source that must stay strictly opt-in (see
config.parse_phases and orchestrator's "posts" phase wiring).

orchestrator.run_discovery is monkeypatched (no real network/LLM), same
pattern as test_live_incremental_discovery.py -- this only asserts on the
`phases` argument it was called with.
"""
from __future__ import annotations

import csv
import tempfile
from pathlib import Path


def _create_campaign(client, **overrides):
    payload = {
        "campaign_name": "Posts Phase Test",
        "target_leads": 5,
        "titles": ["Health Coach"],
        "industries": ["Health and Fitness"],
        "locations": ["United States"],
    }
    payload.update(overrides)
    return client.post("/api/campaigns", json=payload).json()


def _make_fake_run_discovery(captured_phases: list):
    def _fake_run_discovery(target, *, phases=None, on_candidate=None, on_query_progress=None, **kw):
        captured_phases.append(phases)
        tmp = Path(tempfile.mkdtemp()) / "out.csv"
        with tmp.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=["name"]).writeheader()
        return tmp

    return _fake_run_discovery


def _wait(client, job_id, timeout=5):
    import time

    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        time.sleep(0.02)
    return body


def test_search_posts_off_by_default_omits_posts_phase(client, monkeypatch):
    from app.services import discovery_service

    captured: list = []
    monkeypatch.setattr(
        discovery_service.orchestrator, "run_discovery", _make_fake_run_discovery(captured)
    )

    campaign = _create_campaign(client)
    r = client.post(f"/api/campaigns/{campaign['campaign_id']}/discover")
    assert r.status_code == 200
    _wait(client, r.json()["job_id"])

    assert captured == ["ddgs,classify,company_name,age,qualify"]


def test_search_posts_true_adds_posts_phase(client, monkeypatch):
    from app.services import discovery_service

    captured: list = []
    monkeypatch.setattr(
        discovery_service.orchestrator, "run_discovery", _make_fake_run_discovery(captured)
    )

    campaign = _create_campaign(client, search_posts=True)
    r = client.post(f"/api/campaigns/{campaign['campaign_id']}/discover")
    assert r.status_code == 200
    _wait(client, r.json()["job_id"])

    assert captured == ["ddgs,posts,classify,company_name,age,qualify"]
