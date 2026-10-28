"""Tests for query_generator.py's _qualifiers/_qualified_subjects fix:
when both industries and keywords are configured, the query set must
include short standalone variants (not only the full keyword+industry
cross product folded into every subject), and must not concatenate a
qualifier word that's already present in the subject itself.

This was a real-world discovery failure: a live run (India, health-coaching
campaign, titles=["Nutritionist","Health Coach"], industries=["Health and
Fitness"], keywords=["Health","wellness","coach","nutrition"]) produced
queries like "wellness Health and Fitness Nutritionist India LinkedIn" (6
required terms) for EVERY query in the "broad nationwide" tier, and every
single one returned zero search results.

Run with (from `scripts/`):
    python -m unittest pipeline.test_query_generator -v
"""

from __future__ import annotations

import unittest

from .query_generator import _qualified_subjects, _qualifiers, build_queries
from .target_config import TargetConfig


class QualifiersIncludeStandaloneVariantsTests(unittest.TestCase):
    def test_both_industries_and_keywords_includes_standalone_terms(self):
        target = TargetConfig(industries=["Health and Fitness"], keywords=["wellness", "coach"])
        qualifiers = _qualifiers(target)
        # Cross-product terms still present (unchanged behaviour)...
        self.assertIn("wellness Health and Fitness", qualifiers)
        self.assertIn("coach Health and Fitness", qualifiers)
        # ...but each dimension is now also available standalone, so at
        # least some generated queries stay short.
        self.assertIn("Health and Fitness", qualifiers)
        self.assertIn("wellness", qualifiers)
        self.assertIn("coach", qualifiers)

    def test_only_industries_unchanged(self):
        target = TargetConfig(industries=["SaaS"])
        self.assertEqual(_qualifiers(target), ["SaaS"])

    def test_only_keywords_unchanged(self):
        target = TargetConfig(keywords=["AI", "automation"])
        self.assertEqual(_qualifiers(target), ["AI", "automation"])

    def test_neither_configured_is_empty(self):
        self.assertEqual(_qualifiers(TargetConfig()), [])


class QualifiedSubjectsSkipsSelfOverlapTests(unittest.TestCase):
    def test_skips_qualifier_word_already_in_subject(self):
        target = TargetConfig(titles=["Health Coach"], keywords=["coach"])
        subjects = _qualified_subjects(target, target.expanded_titles)
        self.assertNotIn("coach Health Coach", subjects)

    def test_keeps_qualifier_word_not_in_subject(self):
        target = TargetConfig(titles=["Nutritionist"], keywords=["coach"])
        subjects = _qualified_subjects(target, target.expanded_titles)
        self.assertIn("coach Nutritionist", subjects)

    def test_never_returns_empty_when_every_qualifier_overlaps(self):
        # If every configured qualifier happens to already be a substring
        # of every subject, falling back to the bare subjects (rather than
        # an empty query set) keeps this campaign searchable at all.
        target = TargetConfig(titles=["Health Coach"], keywords=["health", "coach"])
        subjects = _qualified_subjects(target, target.expanded_titles)
        self.assertTrue(subjects)


class BuildQueriesProducesShorterVariantsTests(unittest.TestCase):
    def test_short_queries_present_alongside_long_ones(self):
        target = TargetConfig(
            titles=["Nutritionist", "Health Coach"],
            industries=["Health and Fitness"],
            keywords=["Health", "wellness", "coach", "nutrition"],
            locations=["India"],
        )
        queries = build_queries(target)
        word_counts = [len(q.split()) for q in queries]
        # Before the fix, every nationwide-tier query for this exact
        # config was 6+ words (the full keyword+industry+title cross).
        # Now at least some queries are short enough to have realistic
        # odds of matching a real indexed page.
        self.assertTrue(any(n <= 4 for n in word_counts), f"no short queries found: {queries}")

    def test_no_query_repeats_the_same_word_twice(self):
        target = TargetConfig(titles=["Health Coach"], keywords=["coach"], locations=["India"])
        queries = build_queries(target)
        for q in queries:
            words = [w.lower() for w in q.split()]
            # "LinkedIn" and location words aside, no content word should
            # appear twice back-to-back-redundant in the same query.
            content_words = [w for w in words if w not in ("linkedin", "india", "united", "states")]
            self.assertEqual(
                len(content_words),
                len(set(content_words)),
                f"query has a duplicated word: {q!r}",
            )


if __name__ == "__main__":
    unittest.main()
