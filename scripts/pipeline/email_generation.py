"""Day 7: EMAIL_VALIDATED -> EMAIL_GENERATED -> APPROVED / REJECTED.

    EMAIL_VALIDATED lead + Campaign
        -> render_email(): deterministic {{variable}} substitution from the
           Lead's own fields (item 5) -- never touches the network or an LLM
        -> persist the exact rendered subject/body as an EmailJob, keyed
           1:1 on lead_id (item 7)
        -> ONLY THEN transition the Lead to EMAIL_GENERATED (item 6) -- a
           lead is never moved to EMAIL_GENERATED with nothing persisted
           for it, and rendering failures divert to GENERATION_FAILED
           instead
        -> Review: preview / edit the persisted draft (items 8-9)
        -> approve_email() -> APPROVED, or reject_email() -> REJECTED,
           singly or in bulk (items 9-10)

This module is the *only* place PipelineStatus.EMAIL_GENERATED is ever
written, mirroring the ownership convention set by email_discovery.py
(EMAIL_CANDIDATES_FOUND) and email_validation.py (EMAIL_VALIDATED): each
pipeline stage owns exactly one forward transition. Nothing in models.py's
Lead/PipelineStatus (beyond the additive EMAIL_GENERATED -> REJECTED edge
described in models.py itself), lead_pipeline.py, email_discovery.py, or
email_validation.py is changed here. Gmail sending is explicitly out of
scope (Day 8) -- there is no code path anywhere in this module that sends
anything.
"""

from __future__ import annotations

import logging
import re
import uuid
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .campaign import Campaign, render_template
from .lead_store import LeadStore
from .models import InvalidStateTransition, Lead, PipelineStatus, utc_now_iso, validate_transition
from .qualification_scoring import seniority_band
from .quality import (
    is_non_company_org,
    is_valid_company_name,
    is_valid_person_name,
)

log = logging.getLogger("pipeline.email_generation")

# review_status values stored on EmailJob rows. Free-text, mirroring the
# email_status convention on Lead (not state-machine-validated itself --
# PipelineStatus is the state machine; review_status is just a convenient,
# queryable mirror of "which side of review is this draft on" that lets
# list_email_jobs() filter without joining against leads).
REVIEW_PENDING = "PENDING"
REVIEW_APPROVED = "APPROVED"
REVIEW_REJECTED = "REJECTED"

EMAIL_JOB_FIELDNAMES = [
    "job_id",
    "lead_id",
    "campaign_id",
    "subject",
    "body",
    "review_status",
    "edited",
    "rejection_reason",
    "generated_at",
    "reviewed_at",
    "created_at",
    "updated_at",
    "metadata_json",
]


class EmailGenerationError(Exception):
    """Raised (and turned into a GENERATION_FAILED transition) when
    rendering a campaign's templates against a lead doesn't produce usable
    content -- e.g. an empty subject or body after substitution."""


class NoGeneratedEmail(ValueError):
    """Raised by review operations (preview persistence, edit, approve,
    reject) when there is no persisted EmailJob for the given lead_id."""


# ---------------------------------------------------------------------------
# Item 5: deterministic personalization from Lead data
# ---------------------------------------------------------------------------

# Generic (never vertical-specific) dictionary terms that describe an
# *industry/technology*, not a company. A company_name that IS one of
# these, exactly, is almost certainly a mis-extraction (e.g. "AI" lifted
# from an "industries" field rather than a real employer name) -- a real
# company's brand name is essentially never identical to a bare category
# word like this. Deliberately conservative/short: only exact, whole-name
# matches are rejected, so a real company whose name merely *contains* one
# of these words (e.g. "Example Test SaaS Co") is left alone.
_GENERIC_COMPANY_TERMS = frozenset(
    {
        "ai", "ml", "iot", "vr", "ar", "nlp", "llm", "api", "erp", "crm",
        "saas", "paas", "iaas", "b2b", "b2c", "it", "hr", "pr", "seo",
        "tech", "technology", "technologies", "software", "solutions",
        "platform", "cloud", "data", "digital", "automation", "analytics",
        "startup", "company", "corporation", "inc", "llc",
    }
)

# Cue words that signal "the institution named nearby is where this person
# studies/studied", not their employer -- e.g. "junior MIS student at RIT".
# Broader than quality.py's _EDU_ENROLLMENT_CLAUSE_RE: that regex only
# fires when the institution's own name literally contains a word like
# "University"/"Institute"; this one catches the very common case where the
# institution is referred to only by an ambiguous short-form/abbreviation
# (e.g. "RIT") that reads exactly like a plausible company name on its own
# and would otherwise slip past every other company-name check.
_ENROLLMENT_CUE_RE = re.compile(
    r"\b(?:student|studying|studies|attending|enrolled|alumnus|alumna|alumni)\b",
    re.I,
)
_ENROLLMENT_WINDOW_CHARS = 60


def _is_enrollment_mention(name: str, evidence_text: str) -> bool:
    """True if `name` only appears in `evidence_text` right after an
    enrollment cue word (e.g. "student at RIT") -- i.e. it reads like the
    person's school, not their employer, even though the institution's own
    name gives no other textual hint of that (see module docstring above).
    """
    name = (name or "").strip()
    if not name or not evidence_text:
        return False
    low_text = evidence_text.lower()
    low_name = name.lower()
    name_re = re.compile(r"\b" + re.escape(low_name) + r"\b")
    for m in _ENROLLMENT_CUE_RE.finditer(low_text):
        window = low_text[m.end(): m.end() + _ENROLLMENT_WINDOW_CHARS]
        if name_re.search(window):
            return True
    return False


# Sep 2026: field-confusion guards. Real reported bugs: "You're Indiana
# University Health, which is what got my attention" and "You're
# CommonSpirit Health..." -- the raw job_title field sometimes contains an
# ORGANIZATION name (a hospital system, university, etc.) rather than an
# actual role, a common scrape artifact (the source couldn't cleanly split
# "Name - Employer - Title" and the employer half ended up in the title
# field). clean_job_title() only strips known prefix/suffix junk; it never
# validated that what's LEFT actually reads like a job title. This is that
# validation.
_ORG_NAME_MARKERS = (
    "health system", "healthcare", "hospital", "medical center",
    "university", "college", "institute", "foundation", "ministries",
    "church", "corporation", " corp", "inc.", " inc ", " llc", "l.l.c.",
    " group", "clinic", "clinics", "associates", " network", "association",
    "society", "council", "authority", "commission", "diocese",
)
_ROLE_INDICATOR_WORDS = (
    "coach", "owner", "founder", "director", "manager", "president",
    "ceo", "cfo", "coo", "cto", "cmo", "vp ", "vice president", "head of",
    "lead ", "specialist", "consultant", "trainer", "nutritionist",
    "instructor", "practitioner", "advisor", "adviser", "officer",
    "partner", "chair", "principal", "executive", "supervisor",
    "administrator", "physician", "therapist", "dietitian", "expert",
    "professional", "educator", "counselor", "strategist", "analyst",
    "engineer", "developer", "designer", "recruiter", "agent", "broker",
    "assistant", "associate", "representative", "rep ", "planner",
)


def _looks_like_org_not_role(text: str) -> bool:
    """True if `text` (a candidate job_title, already prefix/suffix
    cleaned) reads like the name of an organization rather than a person's
    role within one -- e.g. "Indiana University Health" or "CommonSpirit
    Health" have no role word in them at all, just an institution name.
    A role that happens to also mention an org by name (e.g. "Director of
    Nursing, CommonSpirit Health") is NOT rejected -- the presence of a
    real role word is what matters, not the absence of an org marker."""
    low = f" {(text or '').lower()} "
    has_org_marker = any(m in low for m in _ORG_NAME_MARKERS)
    has_role_word = any(w in low for w in _ROLE_INDICATOR_WORDS)
    return has_org_marker and not has_role_word


# Sep 2026: "You're Health Coach and Owner at Georgia…" -- company_name
# sometimes contains a bare US state name (almost always a location field
# that bled into company_name upstream, not a real employer called
# "Georgia"). A real company is essentially never named identically to a
# state, and even when the location genuinely IS just repeated into
# company_name for some other reason, presenting a state name as if it
# were an employer reads as a data error to any recipient.
_US_STATE_NAMES = frozenset(
    {
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
        "maine", "maryland", "massachusetts", "michigan", "minnesota",
        "mississippi", "missouri", "montana", "nebraska", "nevada",
        "new hampshire", "new jersey", "new mexico", "new york",
        "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
        "pennsylvania", "rhode island", "south carolina", "south dakota",
        "tennessee", "texas", "utah", "vermont", "virginia", "washington",
        "west virginia", "wisconsin", "wyoming",
    }
)


def _collides_with_location(company: str, location: str) -> bool:
    """True if `company` is actually just a state name, or is one of the
    comma/slash-separated tokens of `location` verbatim -- i.e. it reads
    like a location field, not an employer name."""
    company_l = (company or "").strip().lower()
    if not company_l:
        return False
    if company_l in _US_STATE_NAMES:
        return True
    location_l = (location or "").strip().lower()
    if not location_l:
        return False
    tokens = {t.strip() for t in re.split(r"[,/]", location_l) if t.strip()}
    return company_l in tokens


def is_confident_company(lead: Lead) -> bool:
    """True only if `lead.company_name` can be confidently presented as
    this person's employer in outreach copy.

    Deliberately conservative -- this governs whether a company gets
    *claimed* in an email, so it's better to say nothing than to guess
    wrong. Combines:
      1. quality.is_valid_company_name() / is_non_company_org() -- the same
         checks the LLM company-extraction path (llm.py) already applies,
         reused rather than duplicated.
      2. A short, generic-term stoplist (see _GENERIC_COMPANY_TERMS) that
         catches bare industry/tech words (e.g. "AI") is_valid_company_name
         alone doesn't reject, since they're technically well-formed
         "names".
      3. An enrollment-mention check (see _is_enrollment_mention) that
         catches an educational institution referred to only by an
         ambiguous short form (e.g. "RIT") that reads exactly like a
         plausible company name in isolation.
      4. A glued-own-name check: upstream extraction sometimes corrupts
         company_name by concatenating the lead's own name onto the real
         company (e.g. company_name="Hebbia George Sivulka" for a lead
         actually named George Sivulka, or "SaasRise Ryan Allis. Austin").
         A company name that embeds the person's own full name is never a
         genuine employer name, so it's rejected here too rather than
         presented as fact in an email.
    """
    name = (lead.company_name or "").strip()
    if not name:
        return False
    if not is_valid_company_name(name):
        return False
    if is_non_company_org(name):
        return False
    if name.lower() in _GENERIC_COMPANY_TERMS:
        return False
    full_name = (lead.full_name or "").strip()
    if full_name and len(full_name) >= 4 and full_name.lower() in name.lower():
        return False
    evidence_text = f"{lead.job_title} {lead.profile_summary}"
    if _is_enrollment_mention(name, evidence_text):
        return False
    if _collides_with_location(name, lead.location):
        return False
    return True


def display_person_name(name: str) -> str:
    """Normalize a name for use in a greeting/subject line.

    Never invents or "corrects" a name (e.g. an ALL-CAPS or oddly-spelled
    name is never guessed at as a different, "more normal" spelling) --
    this only adjusts *casing* for a more professional-looking greeting
    (e.g. "MCHAEL" -> "Mchael", not a guessed "Michael"). Returns "" for
    anything that doesn't look like a usable person name at all (reusing
    quality.is_valid_person_name), so callers can fall back to a safe
    generic greeting instead of blindly using unusable input.
    """
    name = (name or "").strip()
    if not name or not is_valid_person_name(name):
        return ""
    if name.isupper() or name.islower():
        return name.title()
    return name


# Common scraped-headline artifact: a profile's "title" field sometimes
# turns out to be the *entire* headline ("<Name> - <Company> - <Role
# descriptor>") rather than just the role, because the discovery source
# couldn't cleanly separate them. Stripping a leading "<Name> - " and/or
# "<Company> - " duplicate recovers just the role/descriptor part so it
# reads naturally in a sentence ("... your role as {role} at {company}"
# rather than repeating the person's own name and company back to them).
def clean_job_title(job_title: str, full_name: str, company_name: str) -> str:
    title = (job_title or "").strip()
    if not title:
        return ""
    for prefix_source in (full_name, company_name):
        prefix_source = (prefix_source or "").strip()
        if not prefix_source:
            continue
        prefix = f"{prefix_source} - "
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix):].strip()

    # Strip trailing search-result boilerplate/truncation junk BEFORE the
    # "at/of <company>$" suffix check below, so junk sitting after the
    # company clause (e.g. "...at Georgia ... -", "...Health System |
    # LinkedIn") doesn't block that check from recognizing and removing the
    # company clause underneath it. Applied repeatedly since a raw title
    # can carry more than one of these stacked at the end (e.g. an ellipsis
    # immediately followed by a dangling "-" once the ellipsis is gone).
    #   - "| LinkedIn" (site chrome carried over from the search-result
    #     title field itself, e.g. "Choose Health With Heather | LinkedIn").
    #   - a trailing "..."/"…" truncation ellipsis, with or without
    #     surrounding whitespace.
    #   - a dangling "-" left dangling once trailing junk after it is gone
    #     (e.g. "...at Georgia ... -" -> "...at Georgia ..." -> "...at
    #     Georgia" once both the ellipsis and the now-dangling "-" strip).
    _trailing_junk_re = re.compile(
        r"(\s*[|\u2013\u2014-]\s*LinkedIn\s*$)|(\s*(?:\.{3,}|\u2026)\s*$)|(\s*[|\u2013\u2014-]\s*$)", re.I
    )
    while True:
        new_title = _trailing_junk_re.sub("", title).strip()
        if new_title == title:
            break
        title = new_title

    # Also strip a trailing "at <company>"/"of <company>" clause when it
    # names the same company that will be separately appended by
    # compose_personalization's "... at {company}" phrasing (e.g. job_title
    # "Founder & CEO at Hebbia" with company_name "Hebbia") -- otherwise the
    # company name is stated twice in one sentence ("...as Founder & CEO at
    # Hebbia at Hebbia.").
    company_source = (company_name or "").strip()
    if company_source:
        suffix_re = re.compile(
            r"\s+(?:at|of)\s+" + re.escape(company_source) + r"\s*$", re.I
        )
        title = suffix_re.sub("", title).strip()

    # Stripping a trailing "at/of <company>" clause (just above) can leave
    # a dangling, grammatically-incomplete fragment behind when the raw
    # title was actually a compound phrase like "... University of
    # Pennsylvania Health System" and company_name only captured part of
    # it ("Pennsylvania Health System") -- stripping just that part leaves
    # "... University" hanging with no object, which reads as broken in a
    # sentence. If the title now ends on one of these bare connector/
    # institution words with nothing after it, the whole thing is no
    # longer a coherent role phrase -- drop it entirely rather than splice
    # a dangling fragment into a generated email.
    _DANGLING_TRAILING_WORDS = {
        "of", "at", "for", "in", "and", "the", "&",
        "university", "college", "hospital", "system", "department", "dept",
    }
    last_word = re.split(r"\s+", title.strip())[-1].lower().strip(",.") if title.strip() else ""
    if last_word in _DANGLING_TRAILING_WORDS:
        return ""

    # A "title" that, after all the above, reduces to nothing but the
    # company's own name (e.g. job_title was just "{Name} - {Company} |
    # LinkedIn" with no actual role/headline in between -- common for a
    # small self-titled business) isn't a job title at all; using it as one
    # would draft something like "In Choose Health With Heather roles..."
    # which reads as nonsense. Safer to drop it than splice the company's
    # own name back in disguised as a role.
    if company_source and title.strip().lower() == company_source.lower():
        return ""

    # A "title" that's still this long/sentence-like after stripping known
    # artifacts is more likely a whole headline or bio than a usable role
    # phrase -- safer to drop it than splice a run-on sentence into a
    # generated email.
    if not title or len(title) > 80 or title.count(" ") > 12:
        return ""
    return title


def format_industry_list(industry: str) -> str:
    """"SaaS; AI; Automation" -> "SaaS, AI and Automation" -- a natural-
    language rendering of the (semicolon/comma-separated) industry field,
    de-duplicated case-insensitively while preserving order. "" if empty."""
    parts = [p.strip() for p in re.split(r"[;,]", industry or "") if p.strip()]
    seen: set[str] = set()
    clean: list[str] = []
    for p in parts:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(p)
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return f"{', '.join(clean[:-1])}, and {clean[-1]}"


def primary_industry_term(industry: str) -> str:
    parts = [p.strip() for p in re.split(r"[;,]", industry or "") if p.strip()]
    return parts[0] if parts else ""


@dataclass
class PersonalizationResult:
    """Evidence-grounded personalization computed for one Lead. Every
    string here is built only from Lead fields that are themselves
    evidence (never invented) -- see compose_personalization()."""

    display_first_name: str
    company_used: str  # "" unless is_confident_company(lead) was True
    role_used: str  # cleaned job_title, "" if unusable/absent
    industries_used: str  # formatted industry list, "" if absent
    opening_line: str
    value_line: str
    subject_hook: str
    evidence_used: tuple[str, ...]  # e.g. ("role", "company") -- for tests/checks
    fallback: bool  # True iff no evidence at all was available


def compose_personalization(lead: Lead) -> PersonalizationResult:
    """Build every evidence-grounded, derived string used by
    DEFAULT_SUBJECT_TEMPLATE/DEFAULT_BODY_TEMPLATE (and available to any
    custom campaign template via {{opening_line}}/{{value_line}}/
    {{subject_hook}}). Pure function of `lead` -- no network, no LLM, fully
    deterministic, and never fabricates a company/product/fact beyond what
    is already present in the lead's own fields.
    """
    display_first = display_person_name(lead.first_name) or "there"
    confident_company = is_confident_company(lead)
    company = lead.company_name.strip() if confident_company else ""
    role = clean_job_title(lead.job_title, lead.full_name, lead.company_name)
    if role and _looks_like_org_not_role(role):
        role = ""
    industries = format_industry_list(lead.industry)
    primary_industry = primary_industry_term(lead.industry)

    evidence_used: list[str] = []
    if company and role:
        opening = f"I noticed your role as {role} at {company}."
        evidence_used += ["role", "company"]
    elif company:
        opening = f"I came across {company} and wanted to reach out."
        evidence_used += ["company"]
    elif role and industries:
        opening = f"I noticed your work as {role} in {industries}."
        evidence_used += ["role", "industry"]
    elif role:
        opening = f"I noticed your work as {role}."
        evidence_used += ["role"]
    elif industries:
        opening = f"I came across your profile and noticed your work in {industries}."
        evidence_used += ["industry"]
    else:
        opening = "I came across your profile and wanted to reach out."

    if company:
        subject_hook = f"Quick question about {company}"
    elif primary_industry:
        subject_hook = f"Quick question about your {primary_industry} work"
    elif display_first != "there":
        subject_hook = f"Quick question, {display_first}"
    else:
        subject_hook = "Quick question"

    if industries:
        value_line = (
            f"Would be glad to share more about what we do in "
            f"{industries} — open to a quick call?"
        )
    else:
        value_line = "Would you be open to a quick call sometime this week?"

    return PersonalizationResult(
        display_first_name=display_first,
        company_used=company,
        role_used=role,
        industries_used=industries,
        opening_line=opening,
        value_line=value_line,
        subject_hook=subject_hook,
        evidence_used=tuple(evidence_used),
        fallback=not evidence_used,
    )


# ---------------------------------------------------------------------------
# Aug 2026: sharper, evidence-vs-hedged-hypothesis-vs-generic-aware,
# role-intelligent first-touch generation (translating the useful,
# non-deceptive parts of the "Gautam call" sales methodology into
# deterministic, evidence-grounded outreach copy).
#
# Deliberately built ALONGSIDE (not by modifying) compose_personalization()
# above: every existing test locked to that function's exact wording
# continues to pass unchanged, and this reuses its already-computed,
# evidence-grounded company/role/industry fields rather than re-deriving
# them differently.
# ---------------------------------------------------------------------------

EMAIL_1_INTRO = "EMAIL_1_INTRO"
EMAIL_2_FOLLOWUP = "EMAIL_2_FOLLOWUP"
EMAIL_3_EXPANSION = "EMAIL_3_EXPANSION"
EMAIL_4_CALL_TRANSITION = "EMAIL_4_CALL_TRANSITION"
EMAIL_STAGES = (EMAIL_1_INTRO, EMAIL_2_FOLLOWUP, EMAIL_3_EXPANSION, EMAIL_4_CALL_TRANSITION)

def _indefinite_article(text: str) -> str:
    """"a" or "an" for prepending to a role noun phrase (e.g. "a Health
    Coach", "an Owner") -- simple leading-sound heuristic, good enough for
    the job-title-ish text this is used with."""
    first = (text or "").strip()[:1].lower()
    return "an" if first in "aeiou" else "a"


def _truncate_at_word(text: str, max_len: int) -> str:
    """Truncate `text` to at most `max_len` chars WITHOUT cutting a word in
    half -- e.g. slicing "chronic-condition management" at a raw character
    index can otherwise yield "...chronic-" or "...chronic-condition ma",
    reading as a typo/truncation glitch in a subject line. Truncates to
    the last whole word that fits instead."""
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(",;:- ")


def _stable_index(key: str, length: int) -> int:
    """Deterministic (not random) selection so the same lead always renders
    the same copy (see test_render_email_deterministic-style expectations),
    while different leads still get some variety instead of byte-identical
    templates."""
    if length <= 0:
        return 0
    return (abs(hash(key)) if key else 0) % length


# ---------------------------------------------------------------------------
# Evidence classification (Sep 2026 rewrite -- see PR description /
# task requirements: "never manufacture personalization").
#
# The previous version of this module (_ROLE_PAIN_ANGLES / role_pain_angle /
# select_pain_angle, removed here) picked a generic business-problem guess
# purely from the lead's SENIORITY BAND and asserted it as a hedged
# "hypothesis" on every single lead -- an "Angel Investor" with zero company
# data would get told "operational friction ... tends to show up once
# things start scaling" just because *some* pool entry had to be selected.
# That is fabricated personalization: nothing about the lead's own data
# licenses a claim about their operations, their growth, or their manual
# workload.
#
# This rewrite replaces "guess a problem from the title" with a strict
# evidence gate, per the task's three-tier evidence model:
#
#   STRONG  -- a specific, quotable fact about this lead/company: what they
#              actually build/offer, a hiring signal, a recent announcement,
#              etc. Mined from Lead.profile_summary (the only free-text
#              field this pipeline has -- see extract_strong_evidence()).
#              Only ever the lead's own words, cleaned of scrape junk --
#              never invented, never a template filled with the lead's name.
#   WEAK    -- job title alone / industry alone / location alone / a
#              confidently-established company name with nothing else.
#              Real, but not enough to assert anything about the person's
#              business -- see compose_personalization() above for how
#              these are extracted/validated.
#   NONE    -- nothing usable at all.
#
# A campaign MAY additionally configure Campaign.pain_points -- real
# problem statements the campaign owner actually wrote for their own ICP.
# Those are never treated as evidence *about this lead* (using one is not
# "STRONG" or "WEAK" evidence for THIS person) -- they only ever appear
# framed as what WE (the sender) help with, never as an assertion that the
# prospect has that problem. See _offer_line().
# ---------------------------------------------------------------------------


@dataclass
class EvidenceItem:
    """One piece of STRONG evidence: a short, cleaned quote from the lead's
    own data (`text`, <= 14 words, drawn only from Lead.profile_summary --
    see extract_strong_evidence()), AND a complete, grammatically valid
    `sentence` built around that quote.

    Sep 2026: `sentence` was added after a real reported bug: earlier code
    only ever produced `text` (a bare fragment) and every caller spliced it
    into a single rigid frame ("Saw that you're {text}."). Since `text` is
    sometimes a noun phrase ("a venture-backed AI SaaS startup") and
    sometimes a verb phrase or a truncated list ("improve healthcare
    delivery and patient outcomes", "strategic growth initiatives, the
    expansion of services, and ongoing"), forcing everything through one
    frame produced sentences like "Saw that you're improve healthcare
    delivery..." -- grammatically broken. `sentence` is built once, at
    extraction time, using a frame chosen for THIS evidence kind, so it's
    always a complete, valid sentence and callers use it as a unit rather
    than re-assembling it.
    """

    kind: str  # "role_of_org" | "focus_area" | "hiring" | "recent_activity"
    text: str
    sentence: str


# Sep 2026: candidates whose first content word is one of these are a VERB
# phrase, not a noun phrase (e.g. a "helps X" trigger capturing "improve
# healthcare delivery and patient outcomes" -- "helps" takes a bare-
# infinitive object here, not a noun). None of this module's sentence
# frames can grammatically host a bare verb phrase, so these are rejected
# outright rather than forced into a frame that would break.
_VERB_PHRASE_STARTERS = frozenset(
    {
        "improve", "improves", "increase", "increases", "reduce", "reduces",
        "build", "builds", "create", "creates", "drive", "drives", "grow",
        "grows", "help", "helps", "support", "supports", "deliver",
        "delivers", "provide", "provides", "enable", "enables", "expand",
        "expands", "boost", "boosts", "streamline", "streamlines",
        "optimize", "optimizes", "transform", "transforms", "make",
        "makes", "ensure", "ensures", "achieve", "achieves", "develop",
        "develops", "manage", "manages", "connect", "connects",
    }
)

# Sep 2026: words that grammatically REQUIRE a following noun -- if a
# candidate's word-count truncation (or a scrape-junk truncation) happens
# to cut right after one of these, the result dangles ("...and ongoing.",
# "...expansion of services, and ongoing" with nothing after "ongoing").
# Trimmed from the END of a candidate repeatedly until the last word isn't
# one of these (or the candidate becomes too short and gets rejected by
# the existing length check) -- this is the direct fix for the reported
# "strategic growth initiatives, the expansion of services, and ongoing"
# bug.
_DANGLING_TRAILING_WORDS = frozenset(
    {
        "and", "or", "the", "a", "an", "of", "in", "with", "for", "on",
        "to", "its", "their", "our", "your", "ongoing", "new", "recent",
        "growing", "continued", "increasing", "additional", "further",
    }
)


def _build_evidence_sentence(kind: str, text: str) -> str:
    """A complete, grammatically valid sentence for one EvidenceItem, per
    its `kind` -- see EvidenceItem's docstring for why this exists as a
    ready-made unit rather than a fragment callers assemble themselves."""
    if kind == "role_of_org":
        return f"I noticed you're leading {_indefinite_article(text)} {text}."
    if kind == "focus_area":
        return f"I noticed your work is focused on {text}."
    if kind == "hiring":
        return f"I noticed {text}."
    if kind == "recent_activity":
        return f"I noticed you just launched {text}."
    return f"I noticed {text}."  # unreachable with the current _STRONG_EVIDENCE_PATTERNS kinds


# Trigger phrases for each strong-evidence kind. Deliberately narrow and
# literal (not an NLP model) -- a miss just means this lead falls through to
# WEAK/NONE, which is always the SAFE failure mode per the task's core
# requirement ("if there is no meaningful evidence, DO NOT pretend there
# is"). A false *positive* (matching junk as if it were a real fact) is the
# expensive failure mode, so every pattern requires a real trigger word
# immediately followed by real content, and every captured candidate is
# still put through the sanity filters below before being trusted.
_STRONG_EVIDENCE_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (
        "role_of_org",
        re.compile(
            r"\b(?:founder(?:/co-founder)?|co-founder|ceo|coo|cto)\s+(?:and\s+\w+\s+)?of\s+(?:a |an |the )?"
            r"([a-z0-9][^.!?\n]{4,220})",
            re.I,
        ),
    ),
    (
        "focus_area",
        # Note: "helps"/"help" deliberately excluded from this trigger set
        # -- "helps X" very often takes a bare-infinitive VERB phrase as
        # its object in real scraped bios ("helps improve healthcare
        # delivery"), not a noun phrase, which is exactly the reported
        # "Saw that you're improve healthcare delivery" bug. The other
        # triggers here reliably take a noun-phrase object.
        re.compile(
            r"\b(?:providing|provides|building|builds|offers?|specializ(?:es|ing) in|focused on|"
            r"working on)\s+([a-z0-9][^.!?\n]{4,220})",
            re.I,
        ),
    ),
    ("hiring", re.compile(r"\b(we'?re hiring[^.!?\n]{0,50}|now hiring[^.!?\n]{0,50}|join our team[^.!?\n]{0,50})", re.I)),
    (
        "recent_activity",
        re.compile(r"\b(?:just launched|recently launched|announced|released)\s+([a-z0-9][^.!?\n]{4,220})", re.I),
    ),
)

_EVIDENCE_STOPWORDS = _GENERIC_COMPANY_TERMS | {"a", "an", "the", "and", "or", "of", "for", "with"}

# Scraped LinkedIn/search-result noise that must never leak into a strong-
# evidence quote (follower/connection counts, site chrome, boilerplate).
# extract_strong_evidence() truncates a candidate quote at the first match
# of any of these rather than including it -- e.g. "AI Legal Solutions
# Blacksburg, Virginia 101 followers 101 connections" -> "AI Legal
# Solutions Blacksburg, Virginia" (further trimmed by the word-count cap
# below), not "...101 followers".
_EVIDENCE_JUNK_RE = re.compile(
    r"\d+[.,]?\d*\s*(?:followers?|connections?)\b"
    r"|\bsee (?:your )?mutual connections\b"
    r"|\d+\+?\s*(?:years?|yrs?)\b"
    r"|[|\u2022]|\blinkedin\b",
    re.I,
)


def _clean_evidence_source_text(text: str) -> str:
    """Collapse whitespace in raw profile_summary text. Junk-token removal
    happens per-candidate in extract_strong_evidence (truncating at the
    junk boundary, not deleting mid-string), so this is just whitespace
    normalization."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def extract_strong_evidence(lead: Lead) -> list[EvidenceItem]:
    """Mine Lead.profile_summary for STRONG evidence (see module comment
    above). Returns at most one item -- capping to one keeps an evidence-
    backed email honest and short (stacking three "facts" into one email
    starts to look like padding, not research) and keeps the highest-
    confidence match only when a summary happens to trigger more than one
    pattern.

    Never raises, never returns anything that isn't a substring-derived
    quote from the lead's own profile_summary. Returns [] (not a guess)
    whenever nothing passes the sanity filters -- that's the expected,
    common case for thin/scraped-junk profiles, not a bug.
    """
    raw = _clean_evidence_source_text(lead.profile_summary)
    if not raw:
        return []

    company = (lead.company_name or "").strip().lower()

    for kind, pattern in _STRONG_EVIDENCE_PATTERNS:
        m = pattern.search(raw)
        if not m:
            continue
        candidate = (m.group(1) if m.lastindex else m.group(0)).strip(" .,;:-\u2026")
        # Truncate at the first scrape-junk boundary rather than discarding
        # the whole candidate -- "AI Legal Solutions Blacksburg, Virginia
        # 101 followers" still yields a clean "AI Legal Solutions
        # Blacksburg, Virginia" quote.
        junk = _EVIDENCE_JUNK_RE.search(candidate)
        if junk:
            candidate = candidate[: junk.start()].strip(" .,;:-\u2026")
        words = candidate.split()
        if len(words) > 14:
            words = words[:14]
        # Sep 2026: trim a trailing word that grammatically demands a noun
        # after it (e.g. word-count truncation cutting "...expansion of
        # services, and ongoing" right after "ongoing", with nothing after
        # -- the exact reported "strategic growth initiatives, the
        # expansion of services, and ongoing" bug). Repeated, since more
        # than one such word can be stacked at the end.
        while words and re.sub(r"[^a-z]", "", words[-1].lower()) in _DANGLING_TRAILING_WORDS:
            words = words[:-1]
        candidate = " ".join(words)

        # Sanity filters -- reject rather than guess-fix anything that
        # doesn't look like a real, self-contained quotable fact:
        if len(words) < 2:
            continue  # too thin to be a specific fact ("AI" alone, etc.)
        if not re.search(r"[a-zA-Z]{3,}", candidate):
            continue  # no real word content
        if candidate.lower() == company:
            continue  # just repeats the company name, not a new fact
        first_word = re.sub(r"[^a-z]", "", words[0].lower())
        if first_word in _VERB_PHRASE_STARTERS:
            continue  # a verb phrase, not a noun phrase -- see module comment above
        # a candidate that's ONLY generic/industry vocabulary (e.g. "SaaS
        # and Automation") isn't a *specific* fact either -- it's exactly
        # the "industry alone" weak-evidence case, already handled by
        # compose_personalization()'s industries_used field.
        content_words = [w for w in words if re.sub(r"[^a-z]", "", w.lower()) not in _EVIDENCE_STOPWORDS]
        if len(content_words) < 2:
            continue

        return [EvidenceItem(kind=kind, text=candidate, sentence=_build_evidence_sentence(kind, candidate))]

    return []


# ---------------------------------------------------------------------------
# Case-study selection (requirement E): pick the most relevant VERIFIED
# case study for this lead, or None. Never fabricates -- a campaign with no
# case_studies configured always returns None here, and the caller must
# fall back to clearly-hedged hypothesis language (see _evidence_line).
# ---------------------------------------------------------------------------


def select_case_study(campaign: Campaign | None, lead: Lead) -> dict[str, Any] | None:
    if campaign is None:
        return None
    candidates = campaign.case_studies
    if not candidates:
        return None

    lead_industry_terms = {
        t.strip().lower() for t in re.split(r"[;,]", lead.industry or "") if t.strip()
    }
    lead_text = f"{lead.job_title} {lead.profile_summary} {lead.industry}".lower()

    best: dict[str, Any] | None = None
    best_score = 0
    for cs in candidates:
        if not isinstance(cs, dict) or not (cs.get("text") or "").strip():
            continue
        score = 0
        for industry in cs.get("industries") or []:
            industry_l = str(industry).strip().lower()
            if not industry_l:
                continue
            if industry_l in lead_industry_terms or industry_l in lead_text:
                score += 2
        for keyword in cs.get("keywords") or []:
            keyword_l = str(keyword).strip().lower()
            if keyword_l and keyword_l in lead_text:
                score += 1
        if score > best_score:
            best_score = score
            best = cs
    return best


# ---------------------------------------------------------------------------
# CTA selection (requirement F): low-friction by default; a direct call ask
# is only ever offered when there's real evidence (a verified case study
# AND at least role+one-more-signal grounding this specific lead) -- never
# forced when evidence is weak, and never the same CTA on every email.
# ---------------------------------------------------------------------------

_CTA_SOFT_POOL = [
    "Worth a quick look?",
    "Let me know if that's worth a conversation.",
    "Happy to share more if it'd help.",
    "No worries if now's not a priority.",
    "Does that sound relevant to what you're doing?",
    "Open to comparing notes sometime?",
    "Let me know what you think either way.",
    "Feel free to pass if it's not a fit.",
    "Interested in hearing more?",
    "Let me know what you think.",
]
_CTA_CALL = "Worth 10 minutes to see if it's a fit?"


def select_cta(*, has_case_study: bool, evidence_strength: int, lead_id: str, stage: str) -> tuple[str, str]:
    """Returns (cta_text, cta_type). `evidence_strength` is the count of
    grounded signals used (role/industry/company) -- 0-3."""
    if stage == EMAIL_4_CALL_TRANSITION:
        return _CTA_CALL, "call"
    if has_case_study and evidence_strength >= 2 and _stable_index(lead_id, 3) == 0:
        return _CTA_CALL, "call"
    idx = _stable_index(lead_id + stage, len(_CTA_SOFT_POOL))
    return _CTA_SOFT_POOL[idx], "soft"


# ---------------------------------------------------------------------------
# Roll-up dataclass + composer
# ---------------------------------------------------------------------------


@dataclass
class OutreachPersonalizationResult:
    hook_line: str
    evidence_line: str
    cta_line: str
    problem_subject: str
    hook_type: str
    evidence_used: bool
    evidence_sources: tuple[str, ...]
    personalization_confidence: float
    cta_type: str
    stage: str
    pain_point_source: str = "generic"

    def metadata(self) -> dict[str, Any]:
        return {
            "hook_type": self.hook_type,
            "evidence_used": self.evidence_used,
            "evidence_sources": list(self.evidence_sources),
            "personalization_confidence": round(self.personalization_confidence, 3),
            "cta_type": self.cta_type,
            "stage": self.stage,
            "pain_point_source": self.pain_point_source,
        }


def _campaign_pain_point(campaign: Campaign | None, lead: Lead) -> str:
    """A real, campaign-owner-authored pain phrase if one is configured AND
    matches this lead's industry/keywords/role -- never a generic guess.
    "" if none configured or none match (the honest, common case)."""
    if campaign is None:
        return ""
    candidates = campaign.pain_points
    if not candidates:
        return ""
    lead_industry_terms = {t.strip().lower() for t in re.split(r"[;,]", lead.industry or "") if t.strip()}
    lead_text = f"{lead.job_title} {lead.profile_summary} {lead.industry}".lower()
    best_phrase, best_score = "", 0
    for pp in candidates:
        if not isinstance(pp, dict) or not (pp.get("phrase") or "").strip():
            continue
        score = 0
        for industry in pp.get("industries") or []:
            industry_l = str(industry).strip().lower()
            if industry_l and (industry_l in lead_industry_terms or industry_l in lead_text):
                score += 2
        for keyword in pp.get("keywords") or []:
            keyword_l = str(keyword).strip().lower()
            if keyword_l and keyword_l in lead_text:
                score += 1
        if score > best_score:
            best_score, best_phrase = score, str(pp["phrase"]).strip()
    return best_phrase


def compose_outreach_personalization(
    lead: Lead, campaign: Campaign | None = None, *, stage: str = EMAIL_1_INTRO
) -> OutreachPersonalizationResult:
    """Build the sharper hook/evidence/CTA structure (requirements A-K).

    Reuses compose_personalization(lead)'s already-evidence-grounded
    company/role/industry fields rather than re-deriving them -- so a
    company/role that compose_personalization() decided NOT to trust (e.g.
    RIT, or a bare "AI") never appears here either.
    """
    base = compose_personalization(lead)
    company = base.company_used
    role = base.role_used
    industries = base.industries_used
    primary_industry = primary_industry_term(lead.industry) if industries else ""

    pain_phrase = _campaign_pain_point(campaign, lead)
    pain_source = "custom" if pain_phrase else "none"
    strong = extract_strong_evidence(lead)

    evidence_signals = 0
    if role:
        evidence_signals += 1
    if primary_industry:
        evidence_signals += 1
    if company:
        evidence_signals += 1

    # --- hook_line: never asserts a problem the lead's own data doesn't
    # support. A campaign-configured pain_phrase is used only as "this is
    # what we help with", not as a claim the prospect has that problem.
    # Strong evidence (a real, quoted fact) is preferred over any pain
    # phrase whenever one was actually extracted. Weak evidence
    # (role/company/industry alone) is used only as plain, descriptive
    # context -- it never implies "you're seeing this too", since a title
    # or company name alone is no evidence that any particular problem
    # applies to them.
    industry_phrase = primary_industry or "this space"
    evidence_used = False
    evidence_sources: list[str] = []
    if campaign is not None and (campaign.offer_icp or "").strip() and (campaign.offer_outcome or "").strip():
        icp, outcome = campaign.offer_icp.strip(), campaign.offer_outcome.strip()
        offer_pool = [
            f"We work with {icp} on {outcome}.",
            f"We help {icp} with {outcome}.",
            f"Been working with {icp} on {outcome}.",
            f"We focus on helping {icp} with {outcome}.",
        ]
        offer_sentence = offer_pool[_stable_index(lead.lead_id + ":offer", len(offer_pool))]
    elif pain_phrase:
        offer_sentence = f"We work with {industry_phrase} teams on {pain_phrase}."
    else:
        offer_sentence = ""

    if stage == EMAIL_2_FOLLOWUP:
        prefix = "Following up on my last note — "
    elif stage == EMAIL_3_EXPANSION:
        prefix = "One more thing that might be useful — "
    else:
        prefix = ""

    if strong:
        # Sep 2026: strong[0].sentence is already a complete, grammatically
        # valid sentence (built once, per evidence kind, in
        # extract_strong_evidence/_build_evidence_sentence) -- used as a
        # unit, not re-spliced into another frame. Splicing strong[0].text
        # into ad hoc frames here is exactly what produced "Saw that you're
        # improve healthcare delivery..." before.
        context_clause = strong[0].sentence
        hook_type = "role_strong_evidence" if role else "company_strong_evidence"
        # Sep 2026: evidence really was used in THIS hook -- must be
        # reflected immediately, not left for the evidence_line section
        # below to (maybe) set. Previously this branch left evidence_used
        # at its default False, producing the reported
        # "hook_type=company_strong_evidence, evidence_used=no" bug.
        evidence_used = True
        evidence_sources.append(f"profile_summary:{strong[0].kind}:{strong[0].text[:80]}")
    elif company and role:
        pool = [
            f"Saw your role as {role} at {company}.",
            f"{company} is where I found you — you're the {role} there.",
            f"Reaching out since you're {role} at {company}.",
            f"Your work as {role} at {company} is why I'm writing.",
        ]
        context_clause = pool[_stable_index(lead.lead_id, len(pool))]
        hook_type = "role_industry_company" if primary_industry else "role_company"
    elif company:
        pool = [
            f"Saw {company} and figured I'd say hello.",
            f"{company} is what brought me here.",
            f"Came across {company} recently.",
            f"Writing because of {company}.",
        ]
        context_clause = pool[_stable_index(lead.lead_id, len(pool))]
        hook_type = "company_industry" if primary_industry else "company_only"
    elif role and primary_industry:
        pool = [
            f"Saw your work as {role} in {primary_industry}.",
            f"Reaching out since you work in {primary_industry} as {role}.",
            f"Your {primary_industry} work as {role} is why I'm writing.",
            f"Writing because you're {_indefinite_article(role)} {role}, in {primary_industry}.",
        ]
        context_clause = pool[_stable_index(lead.lead_id, len(pool))]
        hook_type = "role_industry"
    elif role:
        pool = [
            f"Saw your work as {role}.",
            f"Reaching out since you're {_indefinite_article(role)} {role}.",
            f"Your work as {role} is why I'm writing.",
            f"Writing because you're {_indefinite_article(role)} {role}.",
        ]
        context_clause = pool[_stable_index(lead.lead_id, len(pool))]
        hook_type = "role_only"
    elif primary_industry:
        pool = [
            f"Saw your work in {primary_industry}.",
            f"Reaching out since you're in {primary_industry}.",
            f"Your {primary_industry} work is why I'm writing.",
        ]
        context_clause = pool[_stable_index(lead.lead_id, len(pool))]
        hook_type = "industry_only"
    else:
        pool = [
            "Reaching out on the chance this is useful to you.",
            "Hope it's alright to introduce myself this way.",
            "Taking a chance this lands at a good time.",
            "Figured I'd say hello, even without much to go on.",
        ]
        context_clause = pool[_stable_index(lead.lead_id, len(pool))]
        hook_type = "generic_fallback"

    hook_line = f"{prefix}{context_clause}"
    if offer_sentence:
        hook_line = f"{hook_line} {offer_sentence}"

    # --- evidence_line (requirement B/E) ----------------------------------
    # Sep 2026: evidence_used/evidence_sources are initialized once, above
    # (before hook_line construction), and only ever ADDED to here -- never
    # reset to False. Previously this block unconditionally set
    # `evidence_used = False` in its else-branch, which stomped on a True
    # already set by the hook_line block when strong evidence was used
    # there (the exact reported "hook_type=company_strong_evidence,
    # evidence_used=no" bug: strong evidence WAS used, in the hook, and
    # this line then overwrote the flag back to False).
    case_study = select_case_study(campaign, lead)
    if case_study:
        text = str(case_study.get("text", "")).strip()
        evidence_line = f"We recently worked on something similar — {text}"
        evidence_used = True
        evidence_sources.append(f"case_study:{text[:80]}")
    else:
        evidence_line = ""

    # --- CTA (requirement F) ----------------------------------------------
    cta_line, cta_type = select_cta(
        has_case_study=bool(case_study),
        evidence_strength=evidence_signals,
        lead_id=lead.lead_id,
        stage=stage,
    )

    # --- subject (requirement A: concise, problem/outcome-oriented,
    # genuinely varied rather than always "{industry} — quick question" /
    # "Quick question, {name}") ------------------------------------------
    display_first = base.display_first_name
    if strong:
        subj_pool = [
            _truncate_at_word(strong[0].text.capitalize(), 60),
            _truncate_at_word(f"Quick note re: {strong[0].text}", 60),
            _truncate_at_word(f"{company or primary_industry or display_first} — {strong[0].text}", 60),
        ]
    elif company and role:
        subj_pool = [
            f"Quick question about {company}",
            company,
            f"Question for {display_first}",
            f"{role} at {company}",
        ]
    elif company:
        subj_pool = [f"Quick question about {company}", company, f"Question for {display_first}"]
    elif pain_phrase and primary_industry:
        subj_pool = [_truncate_at_word(f"{primary_industry}: {pain_phrase}", 60), _truncate_at_word(f"Question about {pain_phrase}", 60)]
    elif primary_industry:
        subj_pool = [
            f"{primary_industry} — quick question",
            f"Question about your {primary_industry} work",
            f"Quick question, {display_first}",
            primary_industry,
        ]
    elif role:
        subj_pool = [
            f"Quick question, {display_first}",
            "Question about your work",
            _truncate_at_word(role, 60),
            f"Quick note for {display_first}",
            "A quick question",
        ]
    else:
        subj_pool = [f"Quick question, {display_first}", "Quick question", "Question for you"]
    problem_subject = subj_pool[_stable_index(lead.lead_id + ":subject", len(subj_pool))] or base.subject_hook

    # --- personalization_confidence (Sep 2026: made honest) --------------
    # Previously: evidence_signals / 3.0, treating "we know their bare job
    # title" as worth exactly the same weight per signal as "we have a
    # quoted, specific fact about them" -- that's how a role-only email
    # reported personalization_confidence=0.333 despite using nothing but
    # a job title. Confidence now reflects the REAL tier of evidence used
    # (see this function's Tier 1/2/3 structure above), not a raw count of
    # which fields happened to be non-empty.
    if strong and hook_type in ("role_strong_evidence", "company_strong_evidence"):
        confidence = 0.9 if case_study else 0.8  # Tier 1: specific, quoted evidence
    elif case_study:
        confidence = 0.7  # a verified case study, even without lead-specific evidence
    elif company and role:
        confidence = 0.45  # Tier 2: real but generic professional context (two signals)
    elif company or role:
        confidence = 0.3  # Tier 2: one generic signal
    elif primary_industry:
        confidence = 0.12  # Tier 3: industry alone is barely personalization at all
    else:
        confidence = 0.0  # Tier 3: nothing

    return OutreachPersonalizationResult(
        hook_line=hook_line,
        evidence_line=evidence_line,
        cta_line=cta_line,
        problem_subject=problem_subject,
        hook_type=hook_type,
        evidence_used=evidence_used,
        evidence_sources=tuple(evidence_sources),
        personalization_confidence=confidence,
        cta_type=cta_type,
        stage=stage,
        pain_point_source=pain_source,
    )


def personalization_context(lead: Lead) -> dict[str, str]:
    """The six supported template variables, mapped from a Lead's fields.

    Every value is a plain string (Lead fields are never None), so a
    missing optional field (e.g. no job_title) simply maps to "" -- exactly
    what render_template substitutes for a variable it's given, with no
    special-casing needed on either side.

    Deliberately left as a raw, unsanitized 1:1 mapping (unchanged since
    before evidence-grounding was added) -- see grounded_context() for the
    sanitized/derived version render_email() actually uses.
    """
    return {
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "company_name": lead.company_name,
        "job_title": lead.job_title,
        "location": lead.location,
        "industry": lead.industry,
    }


def grounded_context(lead: Lead, campaign: Campaign | None = None, *, stage: str = "EMAIL_1_INTRO") -> dict[str, str]:
    """personalization_context(), layered with evidence-grounded
    sanitization/derivation -- this is what render_email() actually renders
    templates against.

    Differences from the raw personalization_context():
      - company_name is blanked unless is_confident_company(lead) is True,
        so *no* campaign template (default or custom) can ever splice an
        unverified company into an email, even one written before this
        safeguard existed.
      - job_title is cleaned (see clean_job_title) to drop a duplicated
        name/company prefix that scraped headlines sometimes carry.
      - first_name is casing-normalized for the greeting (see
        display_person_name) -- never re-spelled, only re-cased.
      - opening_line / value_line / subject_hook are added: full,
        evidence-grounded sentences (see compose_personalization) that
        DEFAULT_SUBJECT_TEMPLATE/DEFAULT_BODY_TEMPLATE render directly,
        and any custom campaign template may opt into as well.
      - hook_line / evidence_line / cta_line / problem_subject (Aug 2026):
        the sharper, role/industry/company-aware, evidence-or-hedged-
        hypothesis 3-part structure (see compose_outreach_personalization)
        that SHARP_SUBJECT_TEMPLATE/SHARP_BODY_TEMPLATE render. Computed
        unconditionally (not just when a campaign uses those variables) so
        any template can opt in; when `campaign` is None (e.g. some legacy
        preview call sites) these fall back to campaign-agnostic content
        (no case-study lookup, since there's no campaign to look one up
        on).
    """
    ctx = personalization_context(lead)
    result = compose_personalization(lead)
    ctx["first_name"] = result.display_first_name
    ctx["company_name"] = result.company_used
    ctx["job_title"] = result.role_used
    ctx["opening_line"] = result.opening_line
    ctx["value_line"] = result.value_line
    ctx["subject_hook"] = result.subject_hook

    outreach = compose_outreach_personalization(lead, campaign, stage=stage)
    ctx["hook_line"] = outreach.hook_line
    ctx["evidence_line"] = outreach.evidence_line
    ctx["cta_line"] = outreach.cta_line
    ctx["problem_subject"] = outreach.problem_subject
    return ctx


@dataclass
class RenderedEmail:
    """A subject/body pair produced by rendering a Campaign against a Lead.
    Not itself persisted -- see EmailJob for the persisted form."""

    subject: str
    body: str


def render_email(campaign: Campaign, lead: Lead, *, stage: str = "EMAIL_1_INTRO") -> RenderedEmail:
    """Pure rendering: Campaign templates + one Lead's fields -> subject/body.

    Renders against grounded_context() (evidence-sanitized: unconfirmed
    company names blanked, job_title cleaned, name casing normalized, plus
    the derived opening_line/value_line/subject_hook AND the sharper
    hook_line/evidence_line/cta_line/problem_subject) rather than the raw
    personalization_context() -- see grounded_context()'s docstring. No
    persistence, no state-machine involvement, no side effects -- this is
    also exactly what preview_email() calls, so a preview and the eventually
    generated/persisted draft are always produced by the identical code
    path (item 8: preview reflects reality).

    `stage` (Aug 2026, default EMAIL_1_INTRO) only affects the derived
    hook_line/evidence_line/cta_line variables -- see EMAIL_STAGES and
    compose_outreach_personalization for what changes per stage. Campaigns
    using the older opening_line/value_line/subject_hook variables are
    completely unaffected by `stage`.
    """
    context = grounded_context(lead, campaign, stage=stage)
    subject = render_template(campaign.subject_template, context)
    body = render_template(campaign.body_template, context)
    # Sep 2026: collapse blank paragraphs left behind when a derived
    # variable (typically evidence_line, when there's no case study/strong
    # evidence to add beyond what's already in hook_line) renders empty --
    # e.g. "...\n\n\n\nWorth a look?" -> "...\n\nWorth a look?". Emails
    # should vary in length (2-4 short paragraphs per the length/structure
    # requirements), not carry visible gaps where a template variable
    # happened to be blank for this particular lead.
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return RenderedEmail(subject=subject, body=body)


def preview_email(campaign: Campaign, lead: Lead) -> RenderedEmail:
    """Item 8: email preview functionality.

    Available for any Lead regardless of pipeline_status (a reviewer can
    preview what a campaign *would* render before any generation/commit
    step runs) -- unlike generate_email_for_lead, this never touches the
    store and never requires EMAIL_VALIDATED.
    """
    return render_email(campaign, lead)


# ---------------------------------------------------------------------------
# Quality safeguards -- run on every rendered draft *before* a Lead is ever
# allowed to reach EMAIL_GENERATED. These are deliberately separate from
# (and run after) grounded_context()/compose_personalization(): those build
# the best draft they can from available evidence; this is the final,
# independent check that the result is actually safe to send, so a bug or
# an unusual lead/template combination can't silently slip a bad draft
# through. Any issue found here diverts the lead to GENERATION_FAILED with
# the issue text as the reason (see generate_email_for_lead) instead of
# producing a bad email.
# ---------------------------------------------------------------------------

# Phrases that assert something an email should never claim unless it's
# grounded in the lead's own evidence -- funding, revenue, press, and
# similar specifics are exactly the kind of thing this pipeline must never
# invent (see module docstring / task requirements). This is a narrow,
# best-effort net: it can't catch every possible hallucination, but it
# reliably catches the clearest categories, and only fires when the phrase
# is NOT also present in the lead's own evidence text (i.e. it's fine to
# reference something the lead's own profile actually says).
_UNSUPPORTED_CLAIM_MARKERS = (
    "series a", "series b", "series c", "seed round", "funding round",
    "raised $", "raised a", "million in revenue", "billion in revenue",
    "acquired by", "acquisition of", "went public", "ipo", "recently launched",
    "just launched", "award-winning", "named to", "featured in forbes",
    "featured in techcrunch",
)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\}\}")


def _has_unsupported_claim(rendered: "RenderedEmail", lead: Lead) -> str:
    blob = f"{rendered.subject}\n{rendered.body}".lower()
    evidence = (
        f"{lead.job_title} {lead.profile_summary} {lead.industry} "
        f"{lead.company_name}"
    ).lower()
    for marker in _UNSUPPORTED_CLAIM_MARKERS:
        if marker in blob and marker not in evidence:
            return marker
    return ""


def assess_email_quality(
    lead: Lead, rendered: "RenderedEmail", outreach: "OutreachPersonalizationResult | None" = None
) -> list[str]:
    """Independent, final safety check on a rendered draft. Returns a list
    of human-readable issues (empty == passes). See module comment above
    for what this deliberately does and doesn't try to catch.

    `outreach` (Sep 2026, optional -- existing callers/tests that only
    have `lead`/`rendered` still work unchanged) is compose_outreach_
    personalization()'s result for this same render, when available. Used
    only to exempt the company-name-reflection check (#5 below) when
    `evidence_used` is already True: a strong-evidence sentence ("I
    noticed you just launched...") is real personalization by
    construction (it's built only from text extracted out of this lead's
    own profile_summary -- see extract_strong_evidence) even when it
    doesn't happen to restate the company's NAME verbatim.
    """
    issues: list[str] = []
    blob = f"{rendered.subject}\n{rendered.body}"

    # 1. Recipient name is sane.
    if lead.first_name and not is_valid_person_name(lead.first_name):
        issues.append(f"recipient first_name {lead.first_name!r} does not look like a valid person name")

    # 2. Company name, if it appears anywhere in the draft, is one this
    #    lead's evidence actually confirms -- defense-in-depth on top of
    #    grounded_context() already blanking an unconfirmed company_name.
    #    Skipped for names on the generic-term stoplist (e.g. "AI"): those
    #    are rejected as a *company* precisely because they're ordinary
    #    industry/technology vocabulary, so their mere appearance elsewhere
    #    in grounded, industry-based copy (e.g. "...work in SaaS, AI and
    #    Automation") is expected and not itself suspicious. Word-boundary
    #    matched so a short name can't false-positive on a substring of an
    #    unrelated word.
    raw_company = (lead.company_name or "").strip()
    if (
        raw_company
        and not is_confident_company(lead)
        and raw_company.lower() not in _GENERIC_COMPANY_TERMS
        and re.search(r"\b" + re.escape(raw_company.lower()) + r"\b", blob.lower())
    ):
        issues.append(
            f"company name {raw_company!r} appears in the draft but could not be "
            "confidently established as this person's employer"
        )

    # 3. No unresolved {{variable}} placeholders leaked into the final text.
    if _PLACEHOLDER_RE.search(blob):
        issues.append("draft still contains an unresolved {{variable}} placeholder")

    # 4. No unsupported factual claims (funding/revenue/press/etc) beyond
    #    what the lead's own evidence supports.
    claim = _has_unsupported_claim(rendered, lead)
    if claim:
        issues.append(f"draft references {claim!r}, which isn't supported by this lead's evidence")

    # 5. Meaningful personalization: if this lead actually has usable
    #    evidence (confident company or a real job title/industry), the
    #    draft must reflect at least one of them somewhere -- across
    #    subject AND body combined, so a plain subject next to a
    #    well-personalized body (a legitimate, common template style) is
    #    never penalized. A lead with NO evidence at all is exempt: the
    #    honest generic fallback is the intended, safe behavior, not a
    #    defect.
    #
    #    Sep 2026: location intentionally dropped from this list. Bare
    #    location ("you're in Texas") is exactly the kind of "generic
    #    information alone" the personalization-hierarchy rewrite treats
    #    as too weak to personalize on at all (Tier 3) -- neither
    #    compose_outreach_personalization() nor its opener pools ever
    #    reference location, so requiring it here was checking the
    #    generator against a signal it correctly never uses, and was
    #    rejecting real, honest generic-fallback drafts for leads whose
    #    only "evidence" was their city/state.
    #
    #    Also filtered through _looks_like_org_not_role: a raw job_title
    #    that reads like an organization name (the exact "Indiana
    #    University Health" bug) is never treated as a role the draft
    #    owes a mention to -- compose_outreach_personalization() already
    #    correctly suppresses it, so requiring its reflection here would
    #    re-introduce the same bug at the validation layer instead.
    #
    #    Sep 2026: also skipped entirely when `outreach.evidence_used` is
    #    True. A strong-evidence sentence ("I noticed you just launched a
    #    new hybrid strength-training membership program...") is real,
    #    grounded personalization by construction -- extract_strong_evidence
    #    only ever returns text pulled out of this lead's own
    #    profile_summary -- even when it doesn't happen to restate the
    #    company's NAME verbatim (the exact reported false-positive: an
    #    evidence-based email about what a company just launched, correctly
    #    not naming the company mid-sentence, was rejected for "not
    #    reflecting" the company name).
    if outreach is not None and outreach.evidence_used:
        pass  # item 5 skipped for this lead (see comment above); 6/7 still run below
    else:
        cleaned_title = clean_job_title(lead.job_title, lead.full_name, lead.company_name)
        if cleaned_title and _looks_like_org_not_role(cleaned_title):
            cleaned_title = ""
        evidence_tokens = [
            t for t in (
                lead.company_name.strip() if is_confident_company(lead) else "",
                cleaned_title,
                primary_industry_term(lead.industry),
            )
            if t
        ]
        if evidence_tokens:
            low_blob = blob.lower()
            if not any(tok.lower() in low_blob for tok in evidence_tokens):
                issues.append(
                    "available evidence for this lead (role/company/industry/location) "
                    "is not reflected anywhere in the drafted subject or body"
                )

    # 6. AI-slop / cold-email-cliche phrase detector (Sep 2026). Not every
    #    hit here is impossible in a legitimate email (see the list's own
    #    caveats below), but every one of these is either a phrase this
    #    generator should never itself produce, or a strong AI-cliche
    #    signal worth flagging on a draft regardless of its source.
    slop = detect_ai_slop(blob)
    if slop:
        issues.append(f"draft contains cliched/AI-sounding phrasing: {', '.join(slop)}")

    # 7. Defense-in-depth on top of compose_personalization()'s upstream
    #    _looks_like_org_not_role()/_collides_with_location() checks: if
    #    role/company text that reads like an organization or a bare
    #    location somehow made it into the final rendered text anyway
    #    (e.g. a future code path bypasses compose_personalization), catch
    #    it here rather than let a "You're Indiana University Health..."-
    #    style sentence reach the UI.
    for m in re.finditer(r"\byou'?re ([A-Z][A-Za-z&,.' -]{2,60})", rendered.body):
        candidate = m.group(1).rstrip(".,;: ")
        if _looks_like_org_not_role(candidate):
            issues.append(f"draft says \"you're {candidate}\", which reads like an organization name, not a role")
        elif candidate.strip().lower() in _US_STATE_NAMES:
            issues.append(f"draft says \"you're {candidate}\", which is a location, not an employer")

    return issues


# ---------------------------------------------------------------------------
# AI-slop / cold-email-cliche detector (Sep 2026)
# ---------------------------------------------------------------------------

# Every phrase this generator's own pools could ever produce is explicitly
# NOT on this list (they're checked for over-repetition, not banned
# outright -- see the pools themselves for the actual variety mechanism).
# This list is aimed at (a) catching a regression if a future edit
# reintroduces one of these into a pool, and (b) flagging a hand-written
# campaign template that leans on the same cliches every cold-email
# generator reaches for by default.
_AI_SLOP_PHRASES = (
    "tends to come up sooner or later", "operational friction",
    "manual work that starts competing", "that's a pattern we've seen",
    "curious if this is relevant", "would it be useful if i shared",
    "happy to send", "worth sending over", "i noticed that you",
    "i came across your", "i wanted to reach out", "given your role",
    "teams like yours", "businesses like yours", "as you scale",
    "streamline operations", "unlock growth", "drive efficiency",
    "optimize your workflow", "leverage", "seamlessly", "revolutionize",
    "i hope this finds you well", "what caught my eye",
    "that's what got my attention", "just wanted to ask",
    "i was impressed by", "i'd love to connect", "i'd love to learn more",
    "reaching out because", "came across your profile",
    "your profile stood out", "writing on the off chance",
    "let me know either way",
)


def detect_ai_slop(text: str) -> list[str]:
    """Case-insensitive substring scan against _AI_SLOP_PHRASES. Returns
    the matched phrases (deduplicated, in the order first found), or []."""
    low = (text or "").lower()
    return [p for p in _AI_SLOP_PHRASES if p in low]


# ---------------------------------------------------------------------------
# Item 7: EmailJob -- persisted draft representation
# ---------------------------------------------------------------------------


@dataclass
class EmailJob:
    """The exact subject/body that will eventually be sent for one lead,
    plus its review state. Exactly one EmailJob exists per lead_id at a
    time (see UNIQUE(lead_id) in lead_store.py) -- generating again for the
    same lead overwrites this record rather than creating a new one, so
    "the current draft for this lead" is always an unambiguous lookup.
    """

    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    lead_id: str = ""
    campaign_id: str = ""
    subject: str = ""
    body: str = ""
    review_status: str = REVIEW_PENDING
    edited: bool = False
    rejection_reason: str = ""
    generated_at: str = ""
    reviewed_at: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    # Aug 2026: debugging/detail-view metadata -- hook_type, evidence_used,
    # evidence_sources, personalization_confidence, cta_type,
    # email_quality_score, stage. JSON-encoded; "{}" if not computed.
    # Deliberately not surfaced in the main email card UI (see
    # ProspectDetailOut / the Emails tab) -- available for a details/debug
    # view only. See PersonalizationResult.metadata() below.
    metadata_json: str = "{}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["edited"] = int(self.edited)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmailJob":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known and v is not None}
        if "edited" in clean and not isinstance(clean["edited"], bool):
            clean["edited"] = bool(int(clean["edited"]))
        return cls(**clean)

    @property
    def metadata(self) -> dict[str, Any]:
        import json

        try:
            parsed = json.loads(self.metadata_json or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}


def get_email_job(store: LeadStore, lead_id: str) -> EmailJob | None:
    row = store.get_email_job(lead_id)
    return EmailJob.from_dict(row) if row else None


def list_email_jobs(
    store: LeadStore, *, campaign_id: str | None = None, review_status: str | None = None
) -> list[EmailJob]:
    return [
        EmailJob.from_dict(row)
        for row in store.list_email_jobs(campaign_id=campaign_id, review_status=review_status)
    ]


# ---------------------------------------------------------------------------
# Item 6: EMAIL_VALIDATED -> EMAIL_GENERATED, only after successful
# rendering + persistence.
# ---------------------------------------------------------------------------


_HOOK_TYPE_TO_CATEGORY = {
    "role_strong_evidence": "evidence_based",
    "company_strong_evidence": "evidence_based",
    "role_industry_company": "company_context",
    "role_company": "company_context",
    "company_industry": "company_context",
    "company_only": "company_context",
    "role_industry": "role_context",
    "role_only": "role_context",
    "industry_only": "role_context",
    "generic_fallback": "generic_fallback",
}

# Sep 2026: explicit, hard score CEILINGS per personalization category --
# not just a subtracted penalty. A generic-fallback email can be flawless
# prose and still never crack 55; an evidence-based email starts with
# headroom up to 100. This is what makes "generic email = 40, no-evidence
# email = 100" (the reported bug) structurally impossible: the ceiling is
# checked, not just an additive penalty that a lucky combination of other
# factors could route around.
_CATEGORY_SCORE_CEILING = {
    "evidence_based": 100,
    "company_context": 80,
    "role_context": 70,
    "generic_fallback": 55,
}


def personalization_category(hook_type: str) -> str:
    """Collapse the many granular hook_type values (role_industry_company,
    company_only, industry_only, ...) into the four-category taxonomy this
    module's scoring/UI reasoning uses: evidence_based / company_context /
    role_context / generic_fallback. Unknown hook_type values (should not
    happen, but never raise) fall back to "generic_fallback" -- the safest
    (most restrictive) ceiling."""
    return _HOOK_TYPE_TO_CATEGORY.get(hook_type, "generic_fallback")


def compute_email_quality_score(rendered: "RenderedEmail", outreach: "OutreachPersonalizationResult", issues: list[str]) -> int:
    """0-100 (requirement J). Starts at a category CEILING (see
    _CATEGORY_SCORE_CEILING) and only ever subtracts from there for
    concrete, checkable problems -- never a subjective "niceness" score.

    Sep 2026: rebuilt around a hard per-category ceiling rather than a
    single flat starting point (100) with penalties subtracted off.
    Previously a generic-fallback email and an evidence-based email both
    started at 100 and only diverged by a capped set of penalties, which
    is how a no-evidence email could still land at 100/100 while a
    perfectly honest generic one scored 40 for unrelated reasons (body
    length, etc) -- the two were never structurally prevented from
    inverting. Starting from the category's ceiling makes that ordering
    impossible: nothing can push a generic_fallback email's score above
    55, no matter how clean everything else about it is, and nothing
    caps an evidence_based email's score below what its own flaws (if any)
    actually cost it.

    Also enforces requirement #7: `evidence_used=False` combined with a
    strong-evidence hook_type / high confidence is a metadata-consistency
    bug in the caller, not something this function should silently trust
    -- it's now defended against directly in compose_outreach_personalization
    (see that function's hook_line/evidence_line comments), and this
    function additionally never lets such a mismatch buy a higher ceiling
    than the confidence would otherwise support.
    """
    category = personalization_category(outreach.hook_type)
    ceiling = _CATEGORY_SCORE_CEILING[category]
    # Defense in depth: a hook_type claiming evidence_based without
    # evidence_used actually being True never gets the evidence_based
    # ceiling -- treated as company_context (its next-most-generous
    # neighbor) instead, so a metadata bug can't buy a higher score.
    if category == "evidence_based" and not outreach.evidence_used:
        ceiling = _CATEGORY_SCORE_CEILING["company_context"]

    score = ceiling
    score -= 30 * len(issues)  # any failed safeguard is a serious problem

    # Within a category, confidence still modulates the score -- a
    # role_context email with confidence right at the tier floor scores
    # lower than one with confidence near the tier's top.
    conf = outreach.personalization_confidence
    if category == "evidence_based":
        score -= round((1.0 - conf) * 15)  # 0.7-1.0 -> -0 to -4.5; keeps this tier's headroom mostly intact
    elif category == "company_context":
        score -= round((0.6 - conf) * 20) if conf < 0.6 else 0
    elif category == "role_context":
        score -= round((0.35 - conf) * 25) if conf < 0.35 else 0

    body_len = len(rendered.subject) + len(rendered.body)
    if body_len > 700:
        score -= 10  # working against the "fits one mobile screen" requirement

    return max(0, min(ceiling, score))


def _render_and_check(
    campaign: Campaign, lead: Lead, *, stage: str = EMAIL_1_INTRO
) -> tuple[RenderedEmail | None, str, "OutreachPersonalizationResult"]:
    """Render one lead's email and run every quality safeguard on it.

    Returns (rendered, "", outreach) on success, or (None, reason, outreach)
    if rendering produced nothing usable or the quality safeguards
    (assess_email_quality) rejected it -- `reason` is a human-readable
    string suitable for logging or surfacing to an operator, never just a
    generic failure marker. `outreach` (Aug 2026) is always returned so
    callers can persist its metadata regardless of success/failure.
    """
    rendered = render_email(campaign, lead, stage=stage)
    outreach = compose_outreach_personalization(lead, campaign, stage=stage)
    if not rendered.subject.strip() or not rendered.body.strip():
        return None, f"rendered email for lead_id={lead.lead_id!r} has an empty subject or body", outreach
    issues = assess_email_quality(lead, rendered, outreach)
    if issues:
        return None, "; ".join(issues), outreach
    return rendered, "", outreach


def generate_email_for_lead(
    store: LeadStore, lead: Lead, campaign: Campaign, *, stage: str = EMAIL_1_INTRO
) -> Lead:
    """Render + persist a personalized email for one EMAIL_VALIDATED lead,
    then transition it to EMAIL_GENERATED.

    Ordering matters (item 6 + item 7): the render happens first, its
    result is validated (non-empty subject/body, and now also the full
    assess_email_quality() safeguard suite -- see that function) and
    persisted as an EmailJob, and only *then* is the Lead's pipeline_status
    moved to EMAIL_GENERATED. A lead's status becoming EMAIL_GENERATED is
    therefore always backed by an actual persisted draft that has passed
    every quality safeguard; nothing can observe an EMAIL_GENERATED lead
    with no EmailJob for it, and nothing can observe a low-quality/
    ungrounded EmailJob at all. If rendering produces nothing usable, OR
    the quality safeguards reject the draft, the lead is diverted to
    GENERATION_FAILED instead (no partial/empty/low-quality draft is ever
    written), mirroring how email_discovery.py and email_validation.py
    divert to their own failure states rather than faking success. The
    specific reason is logged (see log.warning below) so a human running
    the pipeline can see *why* a lead failed, not just that it did.

    `stage` (Aug 2026, default EMAIL_1_INTRO): which of EMAIL_STAGES this
    draft is for -- affects only the hook_line/evidence_line/cta_line
    derived variables (see compose_outreach_personalization). No scheduler
    exists to auto-advance a lead through stages; a caller (e.g. a future
    follow-up job) chooses the stage explicitly, same as it already chooses
    the campaign.

    Raises InvalidStateTransition immediately (before any rendering or
    writes) if `lead` is not currently EMAIL_VALIDATED -- generation is
    only ever legal from that state.
    """
    validate_transition(lead.pipeline_status, PipelineStatus.EMAIL_GENERATED)

    rendered, reason, outreach = _render_and_check(campaign, lead, stage=stage)
    if rendered is None:
        log.warning("lead_id=%s campaign_id=%s GENERATION_FAILED: %s", lead.lead_id, campaign.campaign_id, reason)
        return store.transition(lead.lead_id, PipelineStatus.GENERATION_FAILED)

    quality_score = compute_email_quality_score(rendered, outreach, [])
    metadata = outreach.metadata()
    metadata["email_quality_score"] = quality_score

    now = utc_now_iso()
    existing = store.get_email_job(lead.lead_id)
    job = EmailJob(
        job_id=existing["job_id"] if existing else uuid.uuid4().hex,
        lead_id=lead.lead_id,
        campaign_id=campaign.campaign_id,
        subject=rendered.subject,
        body=rendered.body,
        review_status=REVIEW_PENDING,
        edited=False,
        rejection_reason="",
        generated_at=now,
        reviewed_at="",
        created_at=existing["created_at"] if existing else now,
        updated_at=now,
        metadata_json=json.dumps(metadata),
    )
    store.save_email_job(job.to_dict())

    lead.campaign_id = campaign.campaign_id
    store.save(lead)

    return store.transition(lead.lead_id, PipelineStatus.EMAIL_GENERATED)


def generate_pending_emails(store: LeadStore, campaign: Campaign, *, stage: str = EMAIL_1_INTRO) -> dict[str, Any]:
    """Process every Lead currently EMAIL_VALIDATED for `campaign`'s
    campaign_id.

    Resumable by construction, mirroring email_discovery.py /
    email_validation.py's bulk runners: only EMAIL_VALIDATED leads are
    pulled, so leads already moved on to EMAIL_GENERATED or
    GENERATION_FAILED are never reprocessed or re-rendered on a second run.

    Returns {"generated": int, "failed": int, "failures": [{"lead_id",
    "reason"}, ...]} -- `failures` gives the useful, per-lead reason for
    every GENERATION_FAILED (empty-render or a failed quality safeguard),
    not just a bare count.
    """
    generated = 0
    failed = 0
    failures: list[dict[str, str]] = []
    for lead in store.list_by_status(PipelineStatus.EMAIL_VALIDATED, campaign_id=campaign.campaign_id):
        _, reason, _outreach = _render_and_check(campaign, lead, stage=stage)
        updated = generate_email_for_lead(store, lead, campaign, stage=stage)
        if updated.status == PipelineStatus.EMAIL_GENERATED:
            generated += 1
        else:
            failed += 1
            failures.append({"lead_id": lead.lead_id, "reason": reason})
    return {"generated": generated, "failed": failed, "failures": failures}


# ---------------------------------------------------------------------------
# Items 8-9: preview (see above) / edit / approve / reject
# ---------------------------------------------------------------------------


def edit_email_job(store: LeadStore, lead_id: str, *, subject: str | None = None, body: str | None = None) -> EmailJob:
    """Overwrite the persisted draft's subject and/or body.

    Only legal while the lead is still EMAIL_GENERATED (i.e. under review,
    not yet approved or rejected) -- editing after a decision has been made
    would silently change what "APPROVED"/"REJECTED" referred to.
    """
    row = store.get_email_job(lead_id)
    if row is None:
        raise NoGeneratedEmail(f"No generated email for lead_id={lead_id!r}")

    lead = store.get(lead_id)
    if lead is None or lead.status != PipelineStatus.EMAIL_GENERATED:
        current = lead.status.value if lead is not None else "unknown"
        raise ValueError(
            f"Cannot edit draft for lead_id={lead_id!r}: lead is {current}, "
            "not EMAIL_GENERATED"
        )

    job = EmailJob.from_dict(row)
    if subject is not None:
        job.subject = subject
    if body is not None:
        job.body = body
    job.edited = True
    job.updated_at = utc_now_iso()
    store.save_email_job(job.to_dict())
    return job


def approve_email(store: LeadStore, lead_id: str) -> Lead:
    """EMAIL_GENERATED -> APPROVED for one lead, and mark its EmailJob approved."""
    row = store.get_email_job(lead_id)
    if row is None:
        raise NoGeneratedEmail(f"No generated email for lead_id={lead_id!r}")

    lead = store.transition(lead_id, PipelineStatus.APPROVED)

    now = utc_now_iso()
    job = EmailJob.from_dict(row)
    job.review_status = REVIEW_APPROVED
    job.reviewed_at = now
    job.updated_at = now
    store.save_email_job(job.to_dict())
    return lead


def reject_email(store: LeadStore, lead_id: str, *, reason: str = "") -> Lead:
    """EMAIL_GENERATED -> REJECTED for one lead, and mark its EmailJob rejected."""
    row = store.get_email_job(lead_id)
    if row is None:
        raise NoGeneratedEmail(f"No generated email for lead_id={lead_id!r}")

    lead = store.transition(lead_id, PipelineStatus.REJECTED)

    now = utc_now_iso()
    job = EmailJob.from_dict(row)
    job.review_status = REVIEW_REJECTED
    job.rejection_reason = reason
    job.reviewed_at = now
    job.updated_at = now
    store.save_email_job(job.to_dict())
    return lead


# ---------------------------------------------------------------------------
# Item 10: bulk approval/rejection
# ---------------------------------------------------------------------------


def bulk_approve(store: LeadStore, lead_ids: list[str]) -> dict[str, list]:
    """Approve every lead_id in `lead_ids`. Never aborts partway through --
    each lead is attempted independently, so one bad id (already decided,
    no draft, wrong state) doesn't block the rest of the batch."""
    approved: list[str] = []
    failed: list[dict[str, str]] = []
    for lead_id in lead_ids:
        try:
            approve_email(store, lead_id)
            approved.append(lead_id)
        except (NoGeneratedEmail, InvalidStateTransition, KeyError) as exc:
            failed.append({"lead_id": lead_id, "error": str(exc)})
    return {"approved": approved, "failed": failed}


def bulk_reject(store: LeadStore, lead_ids: list[str], *, reason: str = "") -> dict[str, list]:
    """Reject every lead_id in `lead_ids`. Same per-lead isolation as bulk_approve."""
    rejected: list[str] = []
    failed: list[dict[str, str]] = []
    for lead_id in lead_ids:
        try:
            reject_email(store, lead_id, reason=reason)
            rejected.append(lead_id)
        except (NoGeneratedEmail, InvalidStateTransition, KeyError) as exc:
            failed.append({"lead_id": lead_id, "error": str(exc)})
    return {"rejected": rejected, "failed": failed}


if __name__ == "__main__":
    import argparse

    from .campaign import load_campaign
    from .config import load_env

    ap = argparse.ArgumentParser(
        description="Day 7: generate personalized emails for EMAIL_VALIDATED leads in a campaign."
    )
    ap.add_argument("--db", default=None, help="Path to the LeadStore SQLite file (default: data/pipeline_state.db)")
    ap.add_argument("--campaign-id", required=True, help="Campaign to generate emails for")
    args = ap.parse_args()

    load_env()
    with (LeadStore(args.db) if args.db else LeadStore()) as store:
        campaign = load_campaign(store, args.campaign_id)
        if campaign is None:
            raise SystemExit(f"No campaign with campaign_id={args.campaign_id!r}")
        stats = generate_pending_emails(store, campaign)
        print(f"EMAIL_GENERATED:    {stats['generated']}")
        print(f"GENERATION_FAILED:  {stats['failed']}")
        for item in stats.get("failures", []):
            print(f"  lead_id={item['lead_id']}: {item['reason']}")
