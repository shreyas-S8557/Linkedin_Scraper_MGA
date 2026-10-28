"""Regression tests for the Sep 2026 "stop over-filtering valid prospects"
fix (see quality.py's evaluate_qualification() module comment for the full
root-cause writeup).

These reproduce the exact reported bug using the exact campaign shape
("Young Professional Financial Growth": early-career titles, a broad
industries list, explicit excluded titles, age 22-35) and the exact named
prospects from that run's FILTERED_OUT list, plus the two names explicitly
called out as prospects that must STAY filtered (David Ferrante / Justin
Horne, "Financial Consultant").

Run with (from `scripts/`):
    python -m unittest pipeline.test_qualification_over_filtering_fix -v
"""

from __future__ import annotations

import unittest

from .quality import build_qualification_audit_text, matches_target_criteria, qualify_row
from .target_config import TargetConfig

TARGET_TITLES = [
    "Software Engineer", "Software Developer", "Product Manager",
    "Product Designer", "UX Designer", "Data Analyst", "Data Scientist",
    "Business Analyst", "Consultant", "Marketing Manager",
    "Marketing Specialist", "Sales Manager", "Account Executive",
    "Project Manager", "Program Manager", "Operations Manager",
    "Operations Analyst", "Engineer", "Architect", "Researcher",
    "Recruiter", "HR Manager", "Communications Manager",
    "Customer Success Manager", "Accountant",
]

EXCLUDE_TITLES = [
    "Financial Advisor", "Financial Planner", "Wealth Manager",
    "Investment Advisor", "Portfolio Manager", "Financial Coach",
    "Life Coach", "Career Coach", "Business Coach", "Insurance Agent",
    "Mortgage Broker", "Real Estate Agent", "Realtor", "Stock Trader",
    "Financial Consultant",
]

TARGET_INDUSTRIES = [
    "Technology", "Software", "Consulting", "Financial Services",
    "Marketing", "Advertising", "Professional Services", "Healthcare",
    "E-commerce", "Manufacturing",
]


def young_professional_financial_growth_target() -> TargetConfig:
    return TargetConfig(
        name="young_professional_financial_growth",
        locations=["United States"],
        titles=TARGET_TITLES,
        industries=TARGET_INDUSTRIES,
        exclude_titles=EXCLUDE_TITLES,
        career_stage="early_career",
        age_min=22,
        age_max=35,
        target_count=250,
    )


def _bare_linkedin_row(name: str, title: str) -> dict:
    """A realistic bare DDGS/LinkedIn search-hit row: only a name and a
    one-line title headline, no summary/industries/age -- exactly the
    thin snippet shape that triggered the over-filtering bug (there is
    nothing here for a proximity-based industry/keyword check to find).
    """
    slug = name.replace(" ", "").replace(".", "").lower()
    return {
        "name": name,
        "profile_title": f"{name} - {title} | LinkedIn",
        "summary": "",
        "linkedin_url": f"https://www.linkedin.com/in/{slug}",
        "location": "United States",
    }


# Exactly the FILTERED_OUT examples named in the bug report -- all carry a
# legitimate configured target title and nothing else.
FALSELY_FILTERED_PROSPECTS = [
    ("Otmane Aatik", "Software Developer"),
    ("Maria Navarro", "Program Manager"),
    ("Chang Wang", "Industrial Engineer"),
    ("Naveen Kumar Siranji", "Software Engineer"),
    ("Tyler Smith", "Business Analyst"),
    ("Harshith Maringanti", "Business Data Analyst"),
    ("Nicholas Bond", "Business Operations Analyst"),
    ("Marc P", "Software Engineer"),
    ("Jared Anderson", "Manufacturing Engineer"),
    ("Michael King", "UX Designer"),
    ("Alex M.", "Marketing Manager"),
    ("Ben Barnes", "Business Analyst"),
    ("Gabriel Jordan", "Software Engineer"),
]

# Named in the bug report as correctly-filtered examples that must STAY
# filtered after the fix (explicitly excluded title).
CORRECTLY_FILTERED_PROSPECTS = [
    ("David Ferrante", "Financial Consultant"),
    ("Justin Horne", "Financial Consultant"),
]


class FalsePositiveFilteringFixTests(unittest.TestCase):
    """Every legitimate target-title candidate from the bug report must now
    qualify, purely because their title matches -- even though nothing in
    their bare discovery snippet states an industry."""

    def setUp(self):
        self.target = young_professional_financial_growth_target()

    def test_all_legitimate_target_title_prospects_now_qualify(self):
        failures = []
        for name, title in FALSELY_FILTERED_PROSPECTS:
            row = _bare_linkedin_row(name, title)
            ok, reason = qualify_row(row, self.target)
            if not ok:
                failures.append(f"{name} ({title}): unexpectedly filtered -- {reason}")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_matches_target_criteria_agrees(self):
        for name, title in FALSELY_FILTERED_PROSPECTS:
            with self.subTest(name=name):
                row = _bare_linkedin_row(name, title)
                self.assertTrue(matches_target_criteria(row, self.target))

    def test_unknown_industry_evidence_is_recorded_as_unknown_not_false(self):
        row = _bare_linkedin_row("Maria Navarro", "Program Manager")
        ok, _ = qualify_row(row, self.target)
        self.assertTrue(ok)
        # industry_evidence is genuinely empty (nothing in the thin snippet
        # supports it) -- the row qualifies anyway because the title
        # dimension already established relevance. This is the
        # "unknown != false" contract the fix exists to enforce.
        self.assertEqual(row["industry_evidence"], "")
        self.assertEqual(row["filter_reason_code"], "")
        self.assertEqual(row["filter_reason"], "")


class HardExclusionsStillWorkTests(unittest.TestCase):
    """The fix must not weaken any genuine hard exclusion."""

    def setUp(self):
        self.target = young_professional_financial_growth_target()

    def test_explicitly_excluded_titles_stay_filtered(self):
        for name, title in CORRECTLY_FILTERED_PROSPECTS:
            with self.subTest(name=name):
                row = _bare_linkedin_row(name, title)
                ok, reason = qualify_row(row, self.target)
                self.assertFalse(ok)
                self.assertIn("Financial Consultant", reason)
                self.assertEqual(row["filter_reason_code"], "EXCLUDED_TITLE")

    def test_senior_titles_still_auto_rejected_for_early_career_campaign(self):
        row = _bare_linkedin_row("Jane Executive", "VP of Engineering")
        ok, reason = qualify_row(row, self.target)
        self.assertFalse(ok)
        self.assertEqual(row["filter_reason_code"], "SENIORITY")

    def test_c_level_still_auto_rejected(self):
        row = _bare_linkedin_row("Bob Chief", "CEO")
        ok, reason = qualify_row(row, self.target)
        self.assertFalse(ok)
        self.assertEqual(row["filter_reason_code"], "SENIORITY")

    def test_confirmed_out_of_range_age_still_rejected(self):
        row = _bare_linkedin_row("Older Prospect", "Software Engineer")
        row["age"] = "41"
        row["age_confidence"] = "high"
        row["age_source"] = "explicit age stated in profile text"
        ok, reason = qualify_row(row, self.target)
        self.assertFalse(ok)
        self.assertEqual(row["filter_reason_code"], "AGE")
        self.assertIn("41", reason)

    def test_title_not_in_configured_list_still_rejected(self):
        row = _bare_linkedin_row("Random Person", "Chief Yoga Instructor")
        ok, reason = qualify_row(row, self.target)
        self.assertFalse(ok)
        self.assertIn(row["filter_reason_code"], ("TITLE_MISMATCH", "SENIORITY", "EXCLUDED_TITLE"))


class UnknownDataNeverAutoRejectsTests(unittest.TestCase):
    """Explicit coverage of every "missing X must not cause rejection"
    requirement from the bug report."""

    def setUp(self):
        self.target = young_professional_financial_growth_target()

    def test_missing_age_does_not_reject(self):
        row = _bare_linkedin_row("No Age Info", "Data Analyst")
        self.assertNotIn("age", row)
        ok, _ = qualify_row(row, self.target)
        self.assertTrue(ok)

    def test_missing_graduation_date_does_not_reject(self):
        row = _bare_linkedin_row("No Grad Info", "Data Scientist")
        self.assertNotIn("years_since_graduation", row)
        ok, _ = qualify_row(row, self.target)
        self.assertTrue(ok)

    def test_missing_years_of_experience_does_not_reject(self):
        row = _bare_linkedin_row("No Exp Info", "Consultant")
        self.assertNotIn("experience_years", row)
        ok, _ = qualify_row(row, self.target)
        self.assertTrue(ok)

    def test_unclear_industry_does_not_reject_when_title_matches(self):
        row = _bare_linkedin_row("Unclear Industry", "Operations Analyst")
        ok, _ = qualify_row(row, self.target)
        self.assertTrue(ok)

    def test_undetermined_seniority_is_not_treated_as_senior(self):
        row = _bare_linkedin_row("Ambiguous Seniority", "Engineer")
        ok, _ = qualify_row(row, self.target)
        self.assertTrue(ok)
        self.assertNotEqual(row["filter_reason_code"], "SENIORITY")


class AuditModeTests(unittest.TestCase):
    """The qualification-audit representation required by the bug report."""

    def setUp(self):
        self.target = young_professional_financial_growth_target()

    def test_audit_text_for_a_qualified_candidate(self):
        row = _bare_linkedin_row("Maria Navarro", "Program Manager")
        text = build_qualification_audit_text(row, self.target)
        self.assertIn("Target title match: YES", text)
        self.assertIn("Industry match: UNKNOWN", text)
        self.assertIn("Decision: KEEP", text)

    def test_audit_text_for_a_filtered_candidate_names_the_reason(self):
        row = _bare_linkedin_row("David Ferrante", "Financial Consultant")
        text = build_qualification_audit_text(row, self.target)
        self.assertIn("Decision: FILTERED_OUT", text)
        self.assertIn("Reason: EXCLUDED_TITLE", text)


class ScoreRegressionTests(unittest.TestCase):
    """The overall pass rate on this exact candidate set must materially
    improve -- no hard-coded target percentage (per the bug report's
    success criteria), just a sanity check that recall genuinely improved
    rather than everyone now passing indiscriminately."""

    def test_recall_improves_and_hard_exclusions_still_work(self):
        target = young_professional_financial_growth_target()
        qualified = 0
        for name, title in FALSELY_FILTERED_PROSPECTS:
            ok, _ = qualify_row(_bare_linkedin_row(name, title), target)
            qualified += int(ok)
        # All 13 legitimate target-title prospects should now qualify.
        self.assertEqual(qualified, len(FALSELY_FILTERED_PROSPECTS))

        still_filtered = 0
        for name, title in CORRECTLY_FILTERED_PROSPECTS:
            ok, _ = qualify_row(_bare_linkedin_row(name, title), target)
            still_filtered += int(not ok)
        # Both explicitly-excluded prospects should still be filtered.
        self.assertEqual(still_filtered, len(CORRECTLY_FILTERED_PROSPECTS))


if __name__ == "__main__":
    unittest.main()
