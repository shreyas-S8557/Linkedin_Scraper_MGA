"""Sep 2026: evidence-consistency, grammatical-fragment, and scoring-
ceiling regression tests -- covers the specific reported bugs:
  1. hook_type=company_strong_evidence co-occurring with evidence_used=False
  2. "Saw that you're improve healthcare delivery..." (verb-phrase fragment)
  3. "...strategic growth initiatives, the expansion of services, and
     ongoing" (dangling-word / mid-word truncation)
  4. a generic-fallback email scoring 100 while an evidence-based one
     scores lower for unrelated reasons

Run with (from the `scripts/` directory):
    python -m unittest pipeline.test_evidence_consistency -v
"""

from __future__ import annotations

import unittest

from pipeline.campaign import create_campaign, SHARP_SUBJECT_TEMPLATE, SHARP_BODY_TEMPLATE
from pipeline.email_generation import (
    EMAIL_1_INTRO,
    assess_email_quality,
    compose_outreach_personalization,
    compute_email_quality_score,
    detect_ai_slop,
    extract_strong_evidence,
    personalization_category,
    render_email,
)
from pipeline.models import Lead, PipelineStatus


def _campaign():
    return create_campaign("t", SHARP_SUBJECT_TEMPLATE, SHARP_BODY_TEMPLATE)


def _lead(**kw) -> Lead:
    defaults = dict(
        first_name="Pat", last_name="Doe", full_name="Pat Doe",
        pipeline_status=PipelineStatus.QUALIFIED.value,
    )
    defaults.update(kw)
    return Lead(**defaults)


class TestEvidenceUsedConsistency(unittest.TestCase):
    """Requirement 7: evidence_used=False must never coexist with a
    strong-evidence hook_type or high confidence, and vice versa."""

    def test_strong_evidence_in_hook_sets_evidence_used_true(self):
        lead = _lead(profile_summary="Pat is the founder of a venture-backed AI SaaS startup focused on healthcare automation.")
        result = compose_outreach_personalization(lead, _campaign())
        self.assertEqual(result.hook_type, "company_strong_evidence")
        self.assertTrue(result.evidence_used)
        self.assertGreaterEqual(result.personalization_confidence, 0.7)
        self.assertTrue(result.evidence_sources)

    def test_role_strong_evidence_also_sets_evidence_used_true(self):
        lead = _lead(job_title="VP of Growth", profile_summary="Pat is the founder of a venture-backed AI SaaS startup focused on healthcare automation.")
        result = compose_outreach_personalization(lead, _campaign())
        self.assertEqual(result.hook_type, "role_strong_evidence")
        self.assertTrue(result.evidence_used)

    def test_no_hook_type_ever_claims_evidence_without_evidence_used(self):
        # Sweep every real-world evidence shape and assert the invariant
        # holds across all of them, not just one hand-picked case.
        cases = [
            _lead(),  # nothing at all
            _lead(job_title="Health Coach"),  # role only
            _lead(company_name="Acme Wellness Co"),  # company only
            _lead(job_title="Health Coach", company_name="Acme Wellness Co"),
            _lead(industry="Health, Wellness"),
            _lead(profile_summary="Founder of a venture-backed wellness platform."),
        ]
        for lead in cases:
            with self.subTest(lead=lead.job_title or lead.company_name or "bare"):
                result = compose_outreach_personalization(lead, _campaign())
                if result.hook_type in ("role_strong_evidence", "company_strong_evidence"):
                    self.assertTrue(result.evidence_used)
                    self.assertGreaterEqual(result.personalization_confidence, 0.7)
                if not result.evidence_used:
                    self.assertNotIn(result.hook_type, ("role_strong_evidence", "company_strong_evidence"))


class TestGrammaticalFragmentFixes(unittest.TestCase):
    """The two exact reported malformed-sentence bugs."""

    def test_helps_plus_bare_verb_is_not_captured_as_evidence(self):
        # "helps improve healthcare delivery" -- "improve" is a bare-
        # infinitive verb, not a noun phrase; must never become
        # "Saw that you're improve healthcare delivery".
        lead = _lead(profile_summary="Elizabeth is a healthcare leader who helps improve healthcare delivery and patient outcomes.")
        self.assertEqual(extract_strong_evidence(lead), [])

    def test_no_verb_phrase_ever_produces_an_ungrammatical_sentence(self):
        verb_starters = ["improve", "increase", "reduce", "help", "support", "deliver", "drive", "grow"]
        for verb in verb_starters:
            with self.subTest(verb=verb):
                lead = _lead(profile_summary=f"Pat helps {verb} outcomes for clients across the region.")
                for item in extract_strong_evidence(lead):
                    self.assertNotEqual(item.text.split()[0].lower(), verb)

    def test_word_count_truncation_never_cuts_mid_word(self):
        # Real reported bug: a 70-CHARACTER regex cap cut "operational"
        # into "op" before the word-based truncation ever ran.
        lead = _lead(
            profile_summary=(
                "Bryan is focused on strategic growth initiatives, the expansion "
                "of services, and ongoing operational improvements across the network."
            )
        )
        evidence = extract_strong_evidence(lead)
        self.assertTrue(evidence)
        for item in evidence:
            for word in item.text.split():
                self.assertGreater(len(word.strip(",.")), 1, f"suspiciously short word fragment: {word!r} in {item.text!r}")

    def test_truncation_never_leaves_a_dangling_trailing_word(self):
        lead = _lead(
            profile_summary="Bryan is focused on strategic growth initiatives, the expansion of services, and ongoing operational improvements across the network."
        )
        for item in extract_strong_evidence(lead):
            last_word = item.text.split()[-1].strip(",.").lower()
            self.assertNotIn(last_word, {"and", "or", "the", "a", "an", "of", "ongoing", "new", "recent"})

    def test_every_evidence_item_has_a_complete_sentence(self):
        cases = [
            "Founder of a venture-backed AI SaaS startup focused on healthcare automation.",
            "We're hiring engineers to help scale our platform.",
            "The company just launched a new patient-engagement platform.",
            "Providing AI-driven legal research tools for small firms.",
        ]
        for summary in cases:
            with self.subTest(summary=summary):
                lead = _lead(profile_summary=summary)
                for item in extract_strong_evidence(lead):
                    self.assertTrue(item.sentence.strip().endswith("."))
                    self.assertTrue(item.sentence[0].isupper())
                    # A real sentence has more words than the bare fragment
                    # alone -- it's wrapped in a subject/verb, not just the
                    # fragment with a period slapped on.
                    self.assertGreater(len(item.sentence.split()), len(item.text.split()))


class TestScoringCeilings(unittest.TestCase):
    """Requirement: generic_fallback <= 55, role_context <= 70,
    company_context <= 80, evidence_based can reach 90+. A generic email
    must never outscore an evidence-based one."""

    def test_generic_fallback_never_exceeds_its_ceiling(self):
        lead = _lead()
        outreach = compose_outreach_personalization(lead, _campaign())
        self.assertEqual(personalization_category(outreach.hook_type), "generic_fallback")
        rendered = render_email(_campaign(), lead)
        score = compute_email_quality_score(rendered, outreach, [])
        self.assertLessEqual(score, 55)

    def test_role_context_never_exceeds_its_ceiling(self):
        lead = _lead(job_title="Health Coach")
        outreach = compose_outreach_personalization(lead, _campaign())
        self.assertEqual(personalization_category(outreach.hook_type), "role_context")
        rendered = render_email(_campaign(), lead)
        score = compute_email_quality_score(rendered, outreach, [])
        self.assertLessEqual(score, 70)

    def test_company_context_never_exceeds_its_ceiling(self):
        lead = _lead(job_title="Health Coach", company_name="Acme Wellness Co")
        outreach = compose_outreach_personalization(lead, _campaign())
        self.assertEqual(personalization_category(outreach.hook_type), "company_context")
        rendered = render_email(_campaign(), lead)
        score = compute_email_quality_score(rendered, outreach, [])
        self.assertLessEqual(score, 80)

    def test_evidence_based_can_exceed_generic_fallback_ceiling(self):
        lead = _lead(profile_summary="Founder of a venture-backed AI SaaS startup focused on healthcare automation.")
        outreach = compose_outreach_personalization(lead, _campaign())
        self.assertEqual(personalization_category(outreach.hook_type), "evidence_based")
        rendered = render_email(_campaign(), lead)
        score = compute_email_quality_score(rendered, outreach, [])
        self.assertGreater(score, 55)

    def test_generic_email_never_outscores_evidence_based_email(self):
        generic_lead = _lead()
        evidence_lead = _lead(
            first_name="Alex", last_name="Kim", full_name="Alex Kim",
            profile_summary="Founder of a venture-backed AI SaaS startup focused on healthcare automation.",
        )
        campaign = _campaign()
        generic_outreach = compose_outreach_personalization(generic_lead, campaign)
        evidence_outreach = compose_outreach_personalization(evidence_lead, campaign)
        generic_score = compute_email_quality_score(render_email(campaign, generic_lead), generic_outreach, [])
        evidence_score = compute_email_quality_score(render_email(campaign, evidence_lead), evidence_outreach, [])
        self.assertGreater(evidence_score, generic_score)

    def test_evidence_claim_without_evidence_used_does_not_get_evidence_ceiling(self):
        # Defense-in-depth: even if a caller somehow constructs a
        # hook_type="company_strong_evidence" result with evidence_used
        # False (a metadata bug elsewhere), scoring must not reward it
        # with the evidence_based ceiling.
        from pipeline.email_generation import OutreachPersonalizationResult

        fake = OutreachPersonalizationResult(
            hook_line="Saw that you're doing something.", evidence_line="", cta_line="Worth a look?",
            problem_subject="Quick question", hook_type="company_strong_evidence", evidence_used=False,
            evidence_sources=(), personalization_confidence=0.8, cta_type="soft", stage=EMAIL_1_INTRO,
        )
        rendered = render_email(_campaign(), _lead())
        score = compute_email_quality_score(rendered, fake, [])
        self.assertLessEqual(score, 80)  # never the evidence_based (100) ceiling


class TestAiSlopBannedPhrases(unittest.TestCase):
    def test_newly_reported_filler_phrases_are_banned(self):
        for phrase in ("came across your profile", "your profile stood out", "writing on the off chance", "let me know either way"):
            self.assertIn(phrase, detect_ai_slop(f"Hi there, {phrase.capitalize()}."))

    def test_generic_fallback_pool_never_contains_banned_phrases(self):
        # Sweep several distinct lead_ids so every pool entry gets hit at
        # least once (pool selection is a stable hash of lead_id).
        campaign = _campaign()
        for i in range(20):
            lead = _lead(lead_id=f"lead-{i}")
            outreach = compose_outreach_personalization(lead, campaign)
            self.assertEqual(detect_ai_slop(outreach.hook_line), [])
            self.assertEqual(detect_ai_slop(outreach.cta_line), [])


    def test_evidence_used_email_never_flagged_for_missing_company_name(self):
        # Real reported false positive: an evidence-based email correctly
        # doesn't restate the company's NAME mid-sentence ("I noticed you
        # just launched a new hybrid strength-training membership
        # program...") but was rejected for "not reflecting" company_name
        # even though the sentence IS grounded (built entirely from this
        # lead's own profile_summary).
        lead = _lead(
            first_name="Morgan", last_name="Reyes", full_name="Morgan Reyes",
            job_title="Owner", company_name="Reyes Fitness Studio", industry="Fitness",
            profile_summary="Morgan's studio just launched a new hybrid strength-training membership program for busy professionals.",
        )
        campaign = _campaign()
        rendered = render_email(campaign, lead)
        outreach = compose_outreach_personalization(lead, campaign)
        self.assertTrue(outreach.evidence_used)
        issues = assess_email_quality(lead, rendered, outreach)
        self.assertEqual(issues, [])
        score = compute_email_quality_score(rendered, outreach, issues)
        self.assertGreaterEqual(score, 90)


if __name__ == "__main__":
    unittest.main()
