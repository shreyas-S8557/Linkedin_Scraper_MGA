"""Tests for the exclude_titles / exclude_locations dimensions (exclude_keywords
already had coverage in test_qualification_smoke.py -- these mirror that
same style, but as proper unittest.TestCase so they're picked up by
`pytest`/`unittest discover` without a manual __main__ invocation).

Run with (from `scripts/`):
    python -m unittest pipeline.test_exclude_dimensions -v
"""

from __future__ import annotations

import unittest

from .quality import matches_target_criteria, qualify_row
from .target_config import TargetConfig


def make_row(**overrides) -> dict:
    defaults = dict(
        name="Jane Doe",
        location="San Francisco, California, United States",
        linkedin_url="https://www.linkedin.com/in/jane-doe",
        profile_title="Founder at Acme AI",
        summary="Building an AI SaaS automation platform for founders.",
        industries="SaaS",
        company_name="Acme AI",
    )
    defaults.update(overrides)
    return defaults


class TargetConfigExcludeFieldsTests(unittest.TestCase):
    def test_defaults_are_empty(self):
        target = TargetConfig()
        self.assertEqual(target.exclude_titles, [])
        self.assertEqual(target.exclude_locations, [])

    def test_cleaned_like_other_list_fields(self):
        target = TargetConfig(
            exclude_titles=[" Co-Founder ", "", "co-founder", "Recruiter"],
            exclude_locations=[" California ", "", "california"],
        )
        # trimmed, empties dropped, case-insensitive de-duped, order kept
        self.assertEqual(target.exclude_titles, ["Co-Founder", "Recruiter"])
        self.assertEqual(target.exclude_locations, ["California"])

    def test_round_trips_through_dict(self):
        target = TargetConfig(exclude_titles=["Co-Founder"], exclude_locations=["California"])
        reloaded = TargetConfig.from_dict(target.to_dict())
        self.assertEqual(reloaded, target)


class ExcludeTitlesTests(unittest.TestCase):
    def test_excluded_title_rejects_an_otherwise_matching_profile(self):
        target = TargetConfig(titles=["Founder"], exclude_titles=["Co-Founder"])
        solo_founder = make_row(profile_title="Founder at Acme AI")
        co_founder = make_row(profile_title="Co-Founder at Acme AI")
        self.assertTrue(matches_target_criteria(solo_founder, target))
        self.assertFalse(matches_target_criteria(co_founder, target))

    def test_qualify_row_reports_the_matched_excluded_title(self):
        target = TargetConfig(titles=["Founder"], exclude_titles=["Co-Founder"])
        row = make_row(profile_title="Co-Founder at Acme AI")
        ok, reason = qualify_row(row, target)
        self.assertFalse(ok)
        self.assertIn("Co-Founder", reason)

    def test_no_exclude_titles_configured_never_rejects(self):
        target = TargetConfig(titles=["Founder"])
        row = make_row(profile_title="Co-Founder at Acme AI")
        self.assertTrue(matches_target_criteria(row, target))


class ExcludeLocationsTests(unittest.TestCase):
    def test_excluded_location_rejects_an_otherwise_matching_profile(self):
        target = TargetConfig(locations=["United States"], exclude_locations=["California"])
        ca_row = make_row(location="San Francisco, California, United States")
        tx_row = make_row(location="Austin, Texas, United States")
        self.assertFalse(matches_target_criteria(ca_row, target))
        self.assertTrue(matches_target_criteria(tx_row, target))

    def test_qualify_row_reports_the_matched_excluded_location(self):
        target = TargetConfig(locations=["United States"], exclude_locations=["California"])
        row = make_row(location="San Francisco, California, United States")
        ok, reason = qualify_row(row, target)
        self.assertFalse(ok)
        self.assertIn("California", reason)

    def test_no_exclude_locations_configured_never_rejects(self):
        target = TargetConfig(locations=["United States"])
        row = make_row(location="San Francisco, California, United States")
        self.assertTrue(matches_target_criteria(row, target))


class ExcludeDimensionsCombineWithExcludeKeywordsTests(unittest.TestCase):
    def test_all_three_exclude_dimensions_can_reject_independently(self):
        target = TargetConfig(
            titles=["Founder"],
            locations=["United States"],
            exclude_titles=["Co-Founder"],
            exclude_locations=["California"],
            exclude_keywords=["recruiter"],
        )
        base = make_row(profile_title="Founder at Acme AI", location="Austin, Texas, United States")
        self.assertTrue(matches_target_criteria(base, target))

        by_title = dict(base, profile_title="Co-Founder at Acme AI")
        self.assertFalse(matches_target_criteria(by_title, target))

        by_location = dict(base, location="San Francisco, California, United States")
        self.assertFalse(matches_target_criteria(by_location, target))

        by_keyword = dict(base, summary="Works as a technical recruiter for AI startups.")
        self.assertFalse(matches_target_criteria(by_keyword, target))


if __name__ == "__main__":
    unittest.main()
