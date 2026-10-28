"""Sep 2026: identity-match / domain-match / source-quality tests.

Covers the reported bug directly: `davis@gmail.com` for "Stacey Davis"
must be REJECTED, not merely mislabeled "low" confidence -- and a real
company-domain, full-name-matched address must outrank it even when the
weak guess happens to pass MX/SMTP.

Run with (from the `scripts/` directory):
    python -m unittest pipeline.test_identity_match -v
"""

from __future__ import annotations

import unittest

from pipeline import identity_match as im
from pipeline.email_discovery import (
    EmailCandidate,
    apply_identity_assessment,
    confidence_label,
    is_usable,
    rank_candidates,
    score_candidate,
    MX_VALID,
    SMTP_EXISTS,
)
from pipeline.models import Lead, PipelineStatus


def _lead(**kw) -> Lead:
    defaults = dict(
        first_name="Stacey", last_name="Davis", full_name="Stacey Davis",
        pipeline_status=PipelineStatus.QUALIFIED.value,
    )
    defaults.update(kw)
    return Lead(**defaults)


# ---------------------------------------------------------------------------
# A. Local-part <-> name pattern classification
# ---------------------------------------------------------------------------


class TestLocalpartPatternClassification(unittest.TestCase):
    def test_full_first_last(self):
        self.assertEqual(im.classify_localpart_pattern("staceydavis", "Stacey", "Davis"), im.PATTERN_FULL_NAME)
        self.assertEqual(im.classify_localpart_pattern("stacey.davis", "Stacey", "Davis"), im.PATTERN_FULL_NAME)
        self.assertEqual(im.classify_localpart_pattern("davis_stacey", "Stacey", "Davis"), im.PATTERN_FULL_NAME)

    def test_initial_last(self):
        self.assertEqual(im.classify_localpart_pattern("sdavis", "Stacey", "Davis"), im.PATTERN_INITIAL_LAST)

    def test_first_initial_last(self):
        self.assertEqual(im.classify_localpart_pattern("staceyd", "Stacey", "Davis"), im.PATTERN_FIRST_INITIAL_LAST)

    def test_initials_only(self):
        self.assertEqual(im.classify_localpart_pattern("sd", "Stacey", "Davis"), im.PATTERN_INITIALS_ONLY)

    def test_surname_only(self):
        self.assertEqual(im.classify_localpart_pattern("davis", "Stacey", "Davis"), im.PATTERN_LAST_ONLY)

    def test_firstname_only(self):
        self.assertEqual(im.classify_localpart_pattern("stacey", "Stacey", "Davis"), im.PATTERN_FIRST_ONLY)

    def test_unrelated(self):
        self.assertEqual(im.classify_localpart_pattern("ch", "Stacey", "Davis"), im.PATTERN_UNRELATED)
        self.assertEqual(im.classify_localpart_pattern("info", "Stacey", "Davis"), im.PATTERN_UNRELATED)

    def test_no_name_to_compare_against_is_unrelated_not_a_guess(self):
        self.assertEqual(im.classify_localpart_pattern("stacey", "", ""), im.PATTERN_UNRELATED)

    def test_empty_localpart_is_unrelated(self):
        self.assertEqual(im.classify_localpart_pattern("", "Stacey", "Davis"), im.PATTERN_UNRELATED)


# ---------------------------------------------------------------------------
# B. Free-provider / role-account / disposable detection (vendored data)
# ---------------------------------------------------------------------------


class TestVendoredMetadata(unittest.TestCase):
    def test_gmail_is_a_free_provider(self):
        self.assertTrue(im.is_free_provider("gmail.com"))
        self.assertTrue(im.is_free_provider("Gmail.COM"))  # case-insensitive

    def test_company_domain_is_not_a_free_provider(self):
        self.assertFalse(im.is_free_provider("georgia.com"))

    def test_common_role_accounts_detected(self):
        for local in ("info", "sales", "support", "admin", "contact", "hello"):
            self.assertTrue(im.is_role_account(local), f"{local!r} should be a role account")

    def test_real_name_localpart_is_not_a_role_account(self):
        self.assertFalse(im.is_role_account("stacey.davis"))

    def test_metadata_loaded_nonempty(self):
        # Sanity check the vendored data files actually loaded -- an empty
        # set here would silently make every is_free_provider/is_role_account
        # check return False, masking real bugs.
        self.assertGreater(len(im.FREE_PROVIDER_DOMAINS), 1000)
        self.assertGreater(len(im.ROLE_ACCOUNT_LOCALPARTS), 100)
        self.assertGreater(len(im.DISPOSABLE_DOMAINS), 1000)


# ---------------------------------------------------------------------------
# C. Domain match
# ---------------------------------------------------------------------------


class TestDomainMatch(unittest.TestCase):
    def test_exact_company_domain_match_is_strong(self):
        self.assertEqual(im.domain_match_score("georgia.com", "georgia.com"), 1.0)

    def test_free_provider_is_weak_regardless_of_local_part(self):
        self.assertLess(im.domain_match_score("gmail.com", "georgia.com"), 0.3)

    def test_unrelated_domain_is_very_weak(self):
        self.assertLess(im.domain_match_score("randomsite.net", "georgia.com"), 0.2)

    def test_disposable_domain_scores_zero(self):
        disposable = next(iter(im.DISPOSABLE_DOMAINS))
        self.assertEqual(im.domain_match_score(disposable, ""), 0.0)


# ---------------------------------------------------------------------------
# D. The exact reported bug: surname/firstname-only free-mail guesses
# ---------------------------------------------------------------------------


class TestReportedBadAddressesAreRejected(unittest.TestCase):
    """Every one of these is verbatim from the bug report."""

    CASES = [
        ("Stacey", "Davis", "davis@gmail.com", "surname_only_free_email"),
        ("Kevin", "Abel", "abel@gmail.com", "surname_only_free_email"),
        ("Sara", "Bernstein", "bernstein@gmail.com", "surname_only_free_email"),
        ("Anna", "Thompson", "anna@gmail.com", "firstname_only_free_email"),
        ("Linda", "Slauson", "linda@gmail.com", "firstname_only_free_email"),
        ("Heather", "Gervais", "gervais@gmail.com", "surname_only_free_email"),
        ("Alexis", "Mayhew", "alexis@gmail.com", "firstname_only_free_email"),
    ]

    def test_every_reported_bad_address_is_rejected(self):
        for first, last, email, expected_reason in self.CASES:
            with self.subTest(email=email):
                a = im.assess(email=email, first_name=first, last_name=last, company_domain="", sources=("mailfoguess",))
                self.assertEqual(a.rejection, expected_reason)

    def test_rejected_candidates_are_not_usable_even_if_mx_and_smtp_pass(self):
        candidates = [
            EmailCandidate(email="davis@gmail.com", sources=("mailfoguess",), domain="gmail.com", mx_status=MX_VALID, smtp_status=SMTP_EXISTS)
        ]
        apply_identity_assessment(candidates, _lead())
        self.assertFalse(is_usable(candidates[0]))
        self.assertEqual(confidence_label(candidates[0]), "none")
        self.assertTrue(candidates[0].rejection_reason)

    def test_initials_only_free_email_rejected(self):
        a = im.assess(email="ch@gmail.com", first_name="Olivia", last_name="Malm", company_domain="", sources=("mailfoguess",))
        self.assertTrue(a.rejection)  # unrelated to either name -> weak_identity_match

    def test_disposable_domain_rejected_regardless_of_name_match(self):
        disposable = next(iter(im.DISPOSABLE_DOMAINS))
        a = im.assess(
            email=f"stacey.davis@{disposable}", first_name="Stacey", last_name="Davis",
            company_domain="", sources=("mailfoguess",),
        )
        self.assertEqual(a.rejection, "disposable_domain")

    def test_role_account_rejected_regardless_of_domain(self):
        a = im.assess(email="info@georgia.com", first_name="Stacey", last_name="Davis", company_domain="georgia.com", sources=("scrapegraph_pattern",))
        self.assertEqual(a.rejection, "role_account")


# ---------------------------------------------------------------------------
# E. Good addresses score high and are never rejected
# ---------------------------------------------------------------------------


class TestGoodAddressesScoreHigh(unittest.TestCase):
    GOOD_CASES = [
        ("Andrea", "Mathis", "andrea.mathis@georgia.com", "georgia.com"),
        ("Patrick", "Anderson", "patrick.anderson@leaderfitness.com", "leaderfitness.com"),
        ("Patricia", "Douglas", "patricia.douglas@bluecrossblueshieldflorida.com", "bluecrossblueshieldflorida.com"),
    ]

    def test_full_name_company_domain_matches_are_never_rejected_and_score_high(self):
        for first, last, email, domain in self.GOOD_CASES:
            with self.subTest(email=email):
                a = im.assess(email=email, first_name=first, last_name=last, company_domain=domain, sources=("scrapegraph_pattern",))
                self.assertEqual(a.rejection, "")
                overall = im.overall_confidence_score(identity=a.identity_score, domain=a.domain_score, deliverability=0.6, source=a.source_score)
                self.assertEqual(im.confidence_level(overall, rejected=False), im.CONFIDENCE_HIGH)


# ---------------------------------------------------------------------------
# F. Ranking: strong company-domain match beats a weak-but-SMTP-confirmed
#    free-mail guess, per the task's explicit worked example.
# ---------------------------------------------------------------------------


class TestCandidateRankingPrefersIdentityOverDeliverability(unittest.TestCase):
    def test_company_domain_full_name_match_beats_confirmed_but_weak_free_mail_guess(self):
        lead = _lead(company_domain="georgia.com")
        strong = EmailCandidate(
            email="stacey.davis@georgia.com", sources=("scrapegraph_pattern",), domain="georgia.com",
            mx_status=MX_VALID,  # not SMTP-confirmed
        )
        weak_but_confirmed = EmailCandidate(
            email="davis@gmail.com", sources=("mailfoguess",), domain="gmail.com",
            mx_status=MX_VALID, smtp_status=SMTP_EXISTS,  # DOES pass SMTP
        )
        candidates = [weak_but_confirmed, strong]
        apply_identity_assessment(candidates, lead)
        ranked = rank_candidates(candidates)
        self.assertEqual(ranked[0].email, "stacey.davis@georgia.com")
        self.assertFalse(is_usable(weak_but_confirmed))  # hard-rejected despite SMTP_EXISTS
        usable = [c for c in ranked if is_usable(c)]
        self.assertEqual(len(usable), 1)
        self.assertEqual(usable[0].email, "stacey.davis@georgia.com")


if __name__ == "__main__":
    unittest.main()
