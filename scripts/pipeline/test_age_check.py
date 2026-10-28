"""Age check (Aug 2026): the new, separate "age_check" gate that runs
before "qualify" and uses two evidence-only proxies -- years of stated
experience and years since a stated undergraduate graduation year -- to
keep only candidates who read as early/mid career (< age_check_max_years,
default 12, on either signal). See quality.age_check_ok and the
"age_check" phase in orchestrator.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS))

from pipeline.quality import (  # noqa: E402
    age_check_ok,
    extract_experience_years_proxy,
    extract_years_since_graduation_proxy,
)
from pipeline.target_config import TargetConfig  # noqa: E402


def _target(**kw) -> TargetConfig:
    return TargetConfig(name="age_check_test", titles=["Founder"], target_count=10, **kw)


# --- extraction ---------------------------------------------------------

def test_extracts_years_of_experience():
    row = {"profile_title": "Founder", "summary": "8 years of experience in fintech."}
    years, source, confidence = extract_experience_years_proxy(row)
    assert years == "8"
    assert confidence == "high"
    assert "experience" in source.lower()


def test_extracts_years_of_experience_with_plus_and_professional():
    row = {"profile_title": "Founder", "summary": "10+ years of professional experience."}
    years, _, confidence = extract_experience_years_proxy(row)
    assert years == "10"
    assert confidence == "high"


def test_no_experience_phrase_returns_none():
    row = {"profile_title": "Founder", "summary": "Building an AI SaaS company."}
    years, source, confidence = extract_experience_years_proxy(row)
    assert years == "" and source == "" and confidence == "none"


def test_extracts_years_since_graduation():
    row = {"profile_title": "Founder", "summary": "Class of 2020, now building a startup."}
    years_since, source, confidence = extract_years_since_graduation_proxy(row)
    assert years_since.isdigit()
    assert confidence == "medium"
    assert "graduation" in source.lower()


def test_no_graduation_phrase_returns_none():
    row = {"profile_title": "Founder", "summary": "Building an AI SaaS company."}
    years_since, source, confidence = extract_years_since_graduation_proxy(row)
    assert years_since == "" and source == "" and confidence == "none"


# --- age_check_ok gate ---------------------------------------------------

def test_unknown_signals_pass_by_default():
    target = _target()
    row = {"profile_title": "Founder", "summary": "Building an AI startup."}
    ok, reason = age_check_ok(row, target)
    assert ok, reason


def test_low_experience_passes():
    target = _target()
    row = {"experience_years": "5", "experience_confidence": "high"}
    ok, reason = age_check_ok(row, target)
    assert ok, reason


def test_high_experience_fails():
    target = _target()
    row = {"experience_years": "20", "experience_confidence": "high"}
    ok, reason = age_check_ok(row, target)
    assert not ok
    assert "experience" in reason.lower()


def test_recent_graduate_passes():
    target = _target()
    row = {"years_since_graduation": "3", "graduation_confidence": "medium"}
    ok, reason = age_check_ok(row, target)
    assert ok, reason


def test_old_graduation_fails():
    target = _target()
    row = {"years_since_graduation": "25", "graduation_confidence": "medium"}
    ok, reason = age_check_ok(row, target)
    assert not ok
    assert "graduated" in reason.lower()


def test_either_signal_passing_is_enough():
    """High experience but a recent grad year still passes -- either
    criterion is sufficient evidence of the target age group."""
    target = _target()
    row = {
        "experience_years": "20",
        "experience_confidence": "high",
        "years_since_graduation": "3",
        "graduation_confidence": "medium",
    }
    ok, reason = age_check_ok(row, target)
    assert ok, reason


def test_both_signals_failing_fails_with_both_named():
    target = _target()
    row = {
        "experience_years": "20",
        "experience_confidence": "high",
        "years_since_graduation": "25",
        "graduation_confidence": "medium",
    }
    ok, reason = age_check_ok(row, target)
    assert not ok
    assert "experience" in reason.lower()
    assert "graduated" in reason.lower()


def test_custom_threshold_is_respected():
    target = _target(age_check_max_years=5)
    row = {"experience_years": "8", "experience_confidence": "high"}
    ok, _ = age_check_ok(row, target)
    assert not ok  # 8 >= 5

    row2 = {"experience_years": "4", "experience_confidence": "high"}
    ok2, reason2 = age_check_ok(row2, target)
    assert ok2, reason2


def test_disabled_check_always_passes():
    target = _target(age_check_enabled=False)
    row = {"experience_years": "40", "experience_confidence": "high"}
    ok, reason = age_check_ok(row, target)
    assert ok, reason


def test_boundary_equal_to_threshold_fails():
    """< threshold passes, == threshold does not (strictly "less than N")."""
    target = _target()
    row = {"experience_years": "12", "experience_confidence": "high"}
    ok, _ = age_check_ok(row, target)
    assert not ok


def test_target_config_rejects_non_positive_max_years():
    import pytest

    with pytest.raises(ValueError):
        TargetConfig(name="bad", age_check_max_years=0)
