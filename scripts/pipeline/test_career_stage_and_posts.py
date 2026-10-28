"""Tests for:
  1. career_stage="early_career" targeting (TargetConfig.effective_exclude_titles,
     query_generator's early-career query tier, quality.py's senior-title
     auto-exclusion).
  2. The LinkedIn-posts discovery source (sources/linkedin_posts_search.py) --
     profile-URL derivation from a post URL, name/location extraction, and
     the title-match carve-out for source == "linkedin_posts" rows.
  3. The broadened evidence-descriptor-noun vocabulary (non-tech verticals).

Run with (from `scripts/`):
    python -m unittest pipeline.test_career_stage_and_posts -v
"""

from __future__ import annotations

import unittest

from .quality import matches_target_criteria, qualify_row
from .query_generator import build_post_queries, build_queries
from .sources.linkedin_posts_search import (
    _extract_post_author_name,
    _guess_profile_url,
    parse_post_hit,
)
from .target_config import (
    EARLY_CAREER_QUALIFIERS,
    SENIOR_TITLE_MARKERS,
    TargetConfig,
)


def make_row(**overrides) -> dict:
    defaults = dict(
        name="Jane Doe",
        location="San Francisco, California, United States",
        linkedin_url="https://www.linkedin.com/in/jane-doe",
        profile_title="Health Coach at Acme Wellness",
        summary="Working with clients on a health coaching practice and wellness programs.",
        industries="Health and Fitness",
        company_name="Acme Wellness",
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# 1. career_stage targeting
# ---------------------------------------------------------------------------


class CareerStageFieldTests(unittest.TestCase):
    def test_default_is_any(self):
        self.assertEqual(TargetConfig().career_stage, "any")

    def test_invalid_value_rejected(self):
        with self.assertRaises(ValueError):
            TargetConfig(career_stage="senior")

    def test_case_and_whitespace_normalized(self):
        t = TargetConfig(career_stage=" Early_Career ")
        self.assertEqual(t.career_stage, "early_career")

    def test_round_trips_through_dict(self):
        t = TargetConfig(career_stage="early_career", search_posts=True)
        reloaded = TargetConfig.from_dict(t.to_dict())
        self.assertEqual(reloaded, t)


class EffectiveExcludeTitlesTests(unittest.TestCase):
    def test_any_stage_unchanged(self):
        t = TargetConfig(exclude_titles=["Recruiter"])
        self.assertEqual(t.effective_exclude_titles, ["Recruiter"])

    def test_early_career_merges_senior_markers(self):
        t = TargetConfig(exclude_titles=["Recruiter"], career_stage="early_career")
        merged = t.effective_exclude_titles
        self.assertIn("Recruiter", merged)
        for marker in SENIOR_TITLE_MARKERS:
            self.assertIn(marker, merged)

    def test_early_career_deduplicates_case_insensitively(self):
        t = TargetConfig(exclude_titles=["Founder"], career_stage="early_career")
        merged = [m.lower() for m in t.effective_exclude_titles]
        self.assertEqual(merged.count("founder"), 1)


class EarlyCareerQualificationTests(unittest.TestCase):
    def test_senior_title_auto_rejected_even_without_explicit_exclude_titles(self):
        t = TargetConfig(titles=["Health Coach"], career_stage="early_career")
        junior = make_row(profile_title="Health Coach at Acme Wellness")
        senior = make_row(profile_title="Director of Health Coaching at Acme Wellness")
        self.assertTrue(matches_target_criteria(junior, t))
        self.assertFalse(matches_target_criteria(senior, t))

    def test_any_stage_does_not_auto_reject_senior_titles(self):
        t = TargetConfig(titles=["Health Coach"], career_stage="any")
        senior = make_row(profile_title="Director of Health Coaching at Acme Wellness")
        # "Director" isn't in this campaign's own exclude_titles, and
        # career_stage="any" never adds the built-in senior markers -- the
        # title substring match on "Health Coach" still succeeds.
        self.assertTrue(matches_target_criteria(senior, t))

    def test_qualify_row_reports_the_senior_marker_reason(self):
        t = TargetConfig(titles=["Health Coach"], career_stage="early_career")
        row = make_row(profile_title="VP of Health Coaching at Acme Wellness")
        ok, reason = qualify_row(row, t)
        self.assertFalse(ok)
        # Sep 2026: a built-in SENIOR_TITLE_MARKERS hit now gets the more
        # specific SENIORITY reason code/wording (see quality.FILTER_SENIORITY)
        # rather than being indistinguishable from a campaign's own
        # explicitly-configured exclude_titles -- see the "Add rejection
        # reasons" / filter_reason_code requirement.
        self.assertIn("senior", reason.lower())
        self.assertEqual(row["filter_reason_code"], "SENIORITY")


class EarlyCareerQueryGenerationTests(unittest.TestCase):
    def test_any_stage_produces_no_qualifier_queries(self):
        t = TargetConfig(titles=["Health Coach"], career_stage="any")
        queries = build_queries(t)
        self.assertFalse(any("recent graduate" in q for q in queries))

    def test_early_career_adds_qualifier_queries(self):
        t = TargetConfig(titles=["Health Coach"], locations=["United States"], career_stage="early_career")
        queries = build_queries(t)
        for qualifier in EARLY_CAREER_QUALIFIERS:
            self.assertTrue(
                any(qualifier in q for q in queries), f"missing qualifier: {qualifier}"
            )


# ---------------------------------------------------------------------------
# 2. LinkedIn-posts discovery source
# ---------------------------------------------------------------------------


class GuessProfileUrlTests(unittest.TestCase):
    def test_extracts_slug_before_underscore(self):
        url = "https://www.linkedin.com/posts/jane-doe-92a_healthcoaching-activity-712345-AbCd"
        self.assertEqual(_guess_profile_url(url), "https://www.linkedin.com/in/jane-doe-92a")

    def test_rejects_company_page_slug(self):
        url = "https://www.linkedin.com/posts/company_some-update-activity-712345"
        self.assertEqual(_guess_profile_url(url), "")

    def test_returns_empty_for_non_post_url(self):
        self.assertEqual(_guess_profile_url("https://www.linkedin.com/in/jane-doe"), "")

    def test_returns_empty_for_blank(self):
        self.assertEqual(_guess_profile_url(""), "")


class ExtractPostAuthorNameTests(unittest.TestCase):
    def test_splits_on_on_linkedin(self):
        title = "Jane Doe on LinkedIn: Excited to share my coaching journey"
        self.assertEqual(_extract_post_author_name(title), "Jane Doe")

    def test_splits_on_dash(self):
        title = "Jane Doe - Health Coach - Excited to share"
        self.assertEqual(_extract_post_author_name(title), "Jane Doe")


class ParsePostHitTests(unittest.TestCase):
    def _hit(self, **overrides):
        base = dict(
            href="https://www.linkedin.com/posts/jane-doe-92a_healthcoaching-earlycareer-activity-712345-AbCd",
            title="Jane Doe on LinkedIn: Excited to share my first year running a coaching practice!",
            body=(
                "Excited to share my first year running a coaching practice in "
                "San Francisco, California, United States. Grateful for the "
                "opportunity to work with my first coaching clients."
            ),
        )
        base.update(overrides)
        return base

    def test_parses_a_clean_post_hit(self):
        target = TargetConfig(keywords=["coaching"], career_stage="early_career")
        row = parse_post_hit(self._hit(), target=target)
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "Jane Doe")
        self.assertEqual(row["linkedin_url"], "https://www.linkedin.com/in/jane-doe-92a")
        self.assertEqual(row["source"], "linkedin_posts")
        # Deliberately never populated from post content -- see module
        # docstring and quality.matches_target_criteria's carve-out.
        self.assertEqual(row["profile_title"], "")

    def test_rejects_non_post_url(self):
        target = TargetConfig(keywords=["coaching"])
        hit = self._hit(href="https://www.linkedin.com/in/jane-doe-92a")
        self.assertIsNone(parse_post_hit(hit, target=target))

    def test_rejects_when_no_location_evidence(self):
        target = TargetConfig(keywords=["coaching"])
        hit = self._hit(body="Excited to share my coaching practice with my clients.")
        self.assertIsNone(parse_post_hit(hit, target=target))

    def test_rejects_when_target_criteria_not_met(self):
        target = TargetConfig(keywords=["blockchain"])
        self.assertIsNone(parse_post_hit(self._hit(), target=target))

    def test_require_target_match_false_bypasses_criteria(self):
        target = TargetConfig(keywords=["blockchain"])
        row = parse_post_hit(self._hit(), target=target, require_target_match=False)
        self.assertIsNotNone(row)


class PostsSourceTitleCarveOutTests(unittest.TestCase):
    """The core reason this source is useful: a post-derived row with no
    profile_title must not be rejected purely for failing the titles
    dimension -- only for genuinely missing industries/keywords evidence."""

    def test_title_dimension_skipped_for_linkedin_posts_source(self):
        target = TargetConfig(titles=["Health Coach"], keywords=["coaching"])
        row = {
            "name": "Jane Doe",
            "location": "San Francisco, California, United States",
            "linkedin_url": "https://www.linkedin.com/in/jane-doe-92a",
            "profile_title": "",
            "summary": "Running a coaching practice with my first coaching clients.",
            "industries": "",
            "source": "linkedin_posts",
        }
        self.assertTrue(matches_target_criteria(row, target))

    def test_title_dimension_still_enforced_for_non_posts_sources(self):
        target = TargetConfig(titles=["Health Coach"])
        row = {
            "name": "Jane Doe",
            "location": "San Francisco, California, United States",
            "linkedin_url": "https://www.linkedin.com/in/jane-doe-92a",
            "profile_title": "",
            "summary": "",
            "industries": "",
            "source": "ddgs_search",
        }
        self.assertFalse(matches_target_criteria(row, target))


class BuildPostQueriesTests(unittest.TestCase):
    def test_bounded_and_topic_first(self):
        target = TargetConfig(industries=["Health and Fitness"], titles=["Health Coach"])
        queries = build_post_queries(target)
        self.assertGreater(len(queries), 0)
        # Topic-first: industries/keywords qualifier used as subject, not
        # the bare title, when both are configured.
        self.assertTrue(any("Health and Fitness" in q for q in queries))

    def test_early_career_adds_stage_terms(self):
        target = TargetConfig(keywords=["coaching"], career_stage="early_career")
        queries = build_post_queries(target)
        for qualifier in EARLY_CAREER_QUALIFIERS:
            self.assertTrue(any(qualifier in q for q in queries))

    def test_any_stage_has_no_stage_terms(self):
        target = TargetConfig(keywords=["coaching"], career_stage="any")
        queries = build_post_queries(target)
        for qualifier in EARLY_CAREER_QUALIFIERS:
            self.assertFalse(any(qualifier in q for q in queries))


# ---------------------------------------------------------------------------
# 3. Broadened evidence-descriptor-noun vocabulary
# ---------------------------------------------------------------------------


class NonTechDescriptorNounTests(unittest.TestCase):
    def test_service_vertical_keyword_matches_near_practice(self):
        target = TargetConfig(keywords=["coaching"])
        row = make_row(
            summary="Running a coaching practice and working with coaching clients."
        )
        self.assertTrue(matches_target_criteria(row, target))

    def test_tech_vertical_still_works_unchanged(self):
        target = TargetConfig(keywords=["AI"])
        row = make_row(summary="Building an AI-powered automation platform for founders.")
        self.assertTrue(matches_target_criteria(row, target))


if __name__ == "__main__":
    unittest.main()
