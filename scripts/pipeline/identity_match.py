"""Sep 2026: identity-match / domain-match / source-quality scoring --
separate from (and computed alongside) the existing MX/SMTP deliverability
checks in email_discovery.py.

## The bug this fixes

Before this module existed, `email_discovery.score_candidate()` combined
only two things: a flat per-generator `base_score`, and MX/SMTP
deliverability evidence. Nothing ever asked "does this local-part actually
look like THIS lead's name?" -- so a Mailfoguess free-mail guess like
`davis@gmail.com` for lead "Stacey Davis" scored on the same axis as
`stacey.davis@stacey-davis-co.com`: gmail.com always has valid MX, and if
gmail happens to host *some* real account under that common surname (very
plausible on a huge free provider, and exactly what SMTP_EXISTS proves --
that *a* mailbox exists, not *whose*), the deliverability-only score had no
way to tell "this mailbox exists" apart from "this mailbox belongs to the
prospect".

## The fix

Three genuinely separate scores, per the task's identity-matching
requirements, computed independently of deliverability:

  * `identity_match_score`  -- does the local-part look like THIS lead's
    name (see classify_localpart_pattern)? A bare surname or first name on
    a free-mail provider is a SEVERE penalty here, exactly like
    `davis@gmail.com` for "Stacey Davis" -- see _WEAK_PATTERNS_ON_FREE_MAIL.
  * `domain_match_score`    -- does the domain actually belong to the
    lead's company, or is it an unrelated/free/disposable domain?
  * `source_quality_score`  -- how was this candidate found (a single
    pattern generator vs. multiple independent generators agreeing;
    reserved tiers for a future scraped/human-verified source -- see
    SOURCE_TIER_* below and AUDIT_EMAIL_TOOLS.md for why those tiers exist
    but aren't populated by anything yet).

email_discovery.py combines these with the existing deliverability score
into one `overall_confidence` (see overall_confidence() below) and can now
reject a candidate outright (see rejection_reason()) instead of quietly
letting a technically-deliverable-but-identity-unproven guess become the
selected email.

## Vendored data

`vendor_data/free_domains.txt`, `vendor_data/role_accounts.txt`, and
`vendor_data/disposable_domains.txt` are extracted verbatim from
email-verifier (github.com/AfterShip/email-verifier, MIT License -- see
`../../email-verifier/LICENSE` and `../../email-verifier/vendor_src/` for
the original Go source and full provenance) -- data only, no code copied;
the classification logic in this module is an independent Python
implementation. See AUDIT_EMAIL_TOOLS.md at the repo root for the full
audit of every repository considered for this integration, including why
`validate_email-master` (LGPLv3) was deliberately NOT vendored.
"""

from __future__ import annotations

import re
from pathlib import Path

_VENDOR_DATA_DIR = Path(__file__).resolve().parent / "vendor_data"


def _load_set(filename: str) -> frozenset[str]:
    """Never raises -- a missing/unreadable vendor data file degrades to an
    empty set (every is_*() check below simply returns False), the same
    soft-dependency contract the rest of this pipeline uses for optional
    vendored components (see email_discovery._SoftImportGenerator)."""
    path = _VENDOR_DATA_DIR / filename
    try:
        return frozenset(
            line.strip().lower() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
    except OSError:
        return frozenset()


FREE_PROVIDER_DOMAINS = _load_set("free_domains.txt")
ROLE_ACCOUNT_LOCALPARTS = _load_set("role_accounts.txt")
DISPOSABLE_DOMAINS = _load_set("disposable_domains.txt")


def is_free_provider(domain: str) -> bool:
    return (domain or "").strip().lower() in FREE_PROVIDER_DOMAINS


def is_disposable_domain(domain: str) -> bool:
    return (domain or "").strip().lower() in DISPOSABLE_DOMAINS


def is_role_account(local_part: str) -> bool:
    return (local_part or "").strip().lower() in ROLE_ACCOUNT_LOCALPARTS


def split_email(email: str) -> tuple[str, str]:
    """(local_part, domain), both lowercased. ("", "") for anything without
    exactly one "@"."""
    email = (email or "").strip().lower()
    if email.count("@") != 1:
        return "", ""
    local, domain = email.split("@", 1)
    return local, domain


# ---------------------------------------------------------------------------
# A. Local-part <-> name pattern classification
# ---------------------------------------------------------------------------

PATTERN_FULL_NAME = "full_first_last"  # johnsmith, john.smith, john_smith, smithjohn
PATTERN_INITIAL_LAST = "initial_last"  # jsmith
PATTERN_FIRST_INITIAL_LAST = "first_last_initial"  # johns
PATTERN_INITIALS_ONLY = "initials_only"  # js
PATTERN_FIRST_ONLY = "first_only"  # john
PATTERN_LAST_ONLY = "last_only"  # smith
PATTERN_FIRST_PARTIAL = "first_partial"  # johnny / jon (prefix overlap, not exact)
PATTERN_LAST_PARTIAL = "last_partial"
PATTERN_UNRELATED = "unrelated"  # no discernible relationship to the lead's name


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z]", "", (value or "").lower())


def classify_localpart_pattern(local_part: str, first_name: str, last_name: str) -> str:
    """Which of the PATTERN_* shapes `local_part` matches against this
    lead's (first_name, last_name) -- the core of identity matching: a
    local-part that's just a bare surname or first name is a completely
    different confidence tier from `firstname.lastname`, even though both
    are "syntactically valid" and both might pass SMTP.

    Deliberately conservative: falls through to PATTERN_UNRELATED rather
    than guessing whenever the local-part doesn't clearly derive from the
    name -- an unrelated/unrecognized local-part must never be silently
    treated as identity evidence.
    """
    lp = _norm_name(local_part)
    first = _norm_name(first_name)
    last = _norm_name(last_name)
    if not lp:
        return PATTERN_UNRELATED

    fi, li = first[:1], last[:1]

    if first and last:
        if lp == first + last or lp == last + first:
            return PATTERN_FULL_NAME
        if fi and lp == fi + last:
            return PATTERN_INITIAL_LAST
        if li and lp == first + li:
            return PATTERN_FIRST_INITIAL_LAST
        if fi and li and lp == fi + li:
            return PATTERN_INITIALS_ONLY

    if first and lp == first:
        return PATTERN_FIRST_ONLY
    if last and lp == last:
        return PATTERN_LAST_ONLY

    # Prefix-overlap partial match (e.g. "steph" for "stephanie") -- still
    # name-derived, but meaningfully weaker than an exact match since it's
    # ambiguous (could just as easily be someone else's short local-part
    # that happens to share a prefix).
    if first and len(lp) >= 3 and (first.startswith(lp) or lp.startswith(first[:4])):
        return PATTERN_FIRST_PARTIAL
    if last and len(lp) >= 3 and (last.startswith(lp) or lp.startswith(last[:4])):
        return PATTERN_LAST_PARTIAL

    return PATTERN_UNRELATED


# Patterns that, ON A FREE-MAIL PROVIDER specifically, get a severe penalty
# per the task spec ("A first-name-only Gmail address should also receive a
# severe penalty", "A surname-only Gmail address should receive a severe
# identity-match penalty", "unexplained initials ... severe penalty"). The
# same pattern on a confirmed COMPANY domain is not penalized this way --
# "jsmith@acme.com" for a lead confirmed to work at Acme is a completely
# normal, common corporate convention, not a weak guess.
_SEVERE_PENALTY_ON_FREE_MAIL = frozenset(
    {PATTERN_FIRST_ONLY, PATTERN_LAST_ONLY, PATTERN_INITIALS_ONLY, PATTERN_FIRST_PARTIAL, PATTERN_LAST_PARTIAL, PATTERN_UNRELATED}
)

_PATTERN_BASE_SCORE = {
    PATTERN_FULL_NAME: 0.95,
    PATTERN_INITIAL_LAST: 0.75,
    PATTERN_FIRST_INITIAL_LAST: 0.7,
    PATTERN_INITIALS_ONLY: 0.3,
    PATTERN_FIRST_ONLY: 0.35,
    PATTERN_LAST_ONLY: 0.35,
    PATTERN_FIRST_PARTIAL: 0.25,
    PATTERN_LAST_PARTIAL: 0.25,
    PATTERN_UNRELATED: 0.05,
}

_FREE_MAIL_PENALTY_MULTIPLIER = 0.25  # "severe penalty" per task spec


def identity_match_score(local_part: str, first_name: str, last_name: str, domain: str) -> tuple[float, str]:
    """(score 0-1, pattern) for how strongly `local_part` looks like it
    belongs to this specific lead. See classify_localpart_pattern() for the
    pattern taxonomy and the module docstring for why free-mail providers
    get an extra penalty on top of the base pattern score."""
    pattern = classify_localpart_pattern(local_part, first_name, last_name)
    score = _PATTERN_BASE_SCORE.get(pattern, 0.05)
    if is_free_provider(domain) and pattern in _SEVERE_PENALTY_ON_FREE_MAIL:
        score *= _FREE_MAIL_PENALTY_MULTIPLIER
    return round(score, 4), pattern


# ---------------------------------------------------------------------------
# B. Domain / company match
# ---------------------------------------------------------------------------


def _clean_domain(value: str) -> str:
    value = (value or "").strip().lower()
    value = value.removeprefix("https://").removeprefix("http://").removeprefix("www.")
    return value.split("/")[0].strip()


def domain_match_score(domain: str, company_domain: str) -> float:
    """Does `domain` actually correspond to the lead's own company? The
    single highest-signal check in this whole module: a company-domain
    email with the RIGHT domain is real evidence; a free-mail guess has
    NO domain relationship to the company at all, regardless of how
    plausible the local-part looks."""
    domain = _clean_domain(domain)
    company_domain = _clean_domain(company_domain)
    if not domain:
        return 0.0
    if is_disposable_domain(domain):
        return 0.0
    if company_domain and domain == company_domain:
        return 1.0
    if is_free_provider(domain):
        return 0.1  # not wrong, just unrelated to the company -- see identity_match_score for the real signal here
    if company_domain:
        return 0.05  # some OTHER domain that isn't the lead's known company domain
    return 0.3  # no company_domain known at all to compare against -- can't confirm OR contradict


# ---------------------------------------------------------------------------
# C. Source quality
#
# Every current generator (Email_Finder / Mailfoguess / email-finder-main)
# is an offline PATTERN generator -- none of them observe a candidate
# "in the wild" (a company staff page, a verified directory, ...). Tiers 3+
# are defined here so the scoring model and CandidateRow schema are ready
# for that the day a live-source generator is wired in (see
# AUDIT_EMAIL_TOOLS.md's note on Email-Finder-main's search-based
# discovery and emailFinder-main's human-corroboration concept), but as of
# this integration only tiers 1-2 are ever actually produced.
# ---------------------------------------------------------------------------

SOURCE_TIER_GENERATED = 1  # exactly one pattern generator produced this candidate
SOURCE_TIER_MULTI_GENERATOR = 2  # 2+ independent generators agree on the same address
SOURCE_TIER_SCRAPED = 3  # (reserved) found verbatim in a reliable public/company source
SOURCE_TIER_HUMAN_VERIFIED = 4  # (reserved) a human reviewer confirmed it

_SOURCE_TIER_SCORE = {
    SOURCE_TIER_GENERATED: 0.3,
    SOURCE_TIER_MULTI_GENERATOR: 0.55,
    SOURCE_TIER_SCRAPED: 0.85,
    SOURCE_TIER_HUMAN_VERIFIED: 1.0,
}


def source_quality_score(sources: tuple[str, ...]) -> tuple[float, int]:
    """(score, tier) from how many independent generators agreed on this
    candidate. Cross-source agreement is real (if weak) corroboration --
    two independently-guessing pattern generators landing on the exact
    same address is less likely by chance than either guessing alone."""
    tier = SOURCE_TIER_MULTI_GENERATOR if len(sources) > 1 else SOURCE_TIER_GENERATED
    return _SOURCE_TIER_SCORE[tier], tier


# ---------------------------------------------------------------------------
# D. Overall confidence + rejection
# ---------------------------------------------------------------------------

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_REJECTED = "rejected"

# Weights: identity + domain match (the two "does this belong to THIS
# person/company" signals) dominate; deliverability and source quality
# matter but never let a well-guessed-but-unproven address outscore a real
# company-domain, name-matched one just because it happened to pass SMTP.
_W_IDENTITY = 0.40
_W_DOMAIN = 0.30
_W_DELIVERABILITY = 0.20
_W_SOURCE = 0.10


def overall_confidence_score(
    *, identity: float, domain: float, deliverability: float, source: float
) -> float:
    return round(
        max(0.0, min(1.0, _W_IDENTITY * identity + _W_DOMAIN * domain + _W_DELIVERABILITY * deliverability + _W_SOURCE * source)),
        4,
    )


def confidence_level(overall: float, *, rejected: bool) -> str:
    if rejected:
        return CONFIDENCE_REJECTED
    if overall >= 0.70:
        return CONFIDENCE_HIGH
    if overall >= 0.45:
        return CONFIDENCE_MEDIUM
    if overall > 0:
        return CONFIDENCE_LOW
    return CONFIDENCE_REJECTED


# Hard rejection reasons -- these are checked independently of the
# weighted overall_confidence_score: a candidate matching one of these is
# rejected outright (never selected as the best email) regardless of how
# the weighted average happens to land, because each one is a distinct,
# checkable reason no reasonable person would trust the address.
def rejection_reason(
    *, local_part: str, domain: str, company_domain: str, pattern: str, mx_dead: bool, smtp_not_exists: bool
) -> str:
    """"" if the candidate isn't outright rejected; otherwise one of the
    task's specified rejection-reason codes."""
    if mx_dead:
        return "invalid_mx"
    if smtp_not_exists:
        return "smtp_failed"
    if not local_part or not domain:
        return "syntax_invalid"
    if is_disposable_domain(domain):
        return "disposable_domain"
    if is_role_account(local_part):
        return "role_account"
    company_domain = _clean_domain(company_domain)
    free = is_free_provider(domain)
    if free:
        if pattern == PATTERN_FIRST_ONLY:
            return "firstname_only_free_email"
        if pattern == PATTERN_LAST_ONLY:
            return "surname_only_free_email"
        if pattern == PATTERN_INITIALS_ONLY:
            return "initials_only_free_email"
        if pattern in (PATTERN_FIRST_PARTIAL, PATTERN_LAST_PARTIAL, PATTERN_UNRELATED):
            return "weak_identity_match"
    elif company_domain and domain != company_domain:
        return "unrelated_domain"
    elif pattern == PATTERN_UNRELATED:
        return "weak_identity_match"
    return ""


class IdentityAssessment:
    """Roll-up of every score this module computes for one (email, lead)
    pair -- what email_discovery.py actually asks for. Kept as a plain
    object (not a dataclass) with __slots__-free simplicity since it's
    small and only ever constructed by assess()."""

    __slots__ = (
        "local_part", "domain", "pattern", "identity_score", "domain_score",
        "source_score", "source_tier", "rejection", "is_free_provider", "is_role",
    )

    def __init__(self, local_part, domain, pattern, identity_score, domain_score, source_score, source_tier, rejection):
        self.local_part = local_part
        self.domain = domain
        self.pattern = pattern
        self.identity_score = identity_score
        self.domain_score = domain_score
        self.source_score = source_score
        self.source_tier = source_tier
        self.rejection = rejection
        self.is_free_provider = is_free_provider(domain)
        self.is_role = is_role_account(local_part)


def assess(
    *,
    email: str,
    first_name: str,
    last_name: str,
    company_domain: str,
    sources: tuple[str, ...],
    mx_dead: bool = False,
    smtp_not_exists: bool = False,
) -> IdentityAssessment:
    """The one entry point email_discovery.py needs: every identity/domain/
    source score for one candidate email against one lead's known name and
    company domain, plus any hard rejection_reason."""
    local_part, domain = split_email(email)
    identity, pattern = identity_match_score(local_part, first_name, last_name, domain)
    domain_score = domain_match_score(domain, company_domain)
    source_score, source_tier = source_quality_score(sources)
    rejection = rejection_reason(
        local_part=local_part,
        domain=domain,
        company_domain=company_domain,
        pattern=pattern,
        mx_dead=mx_dead,
        smtp_not_exists=smtp_not_exists,
    )
    return IdentityAssessment(local_part, domain, pattern, identity, domain_score, source_score, source_tier, rejection)
