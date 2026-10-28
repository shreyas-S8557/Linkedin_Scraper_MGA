"""TargetConfig: the single configuration object that drives discovery.

This replaces the hard-coded CPA_TITLES / build_discovery_queries() constants
that used to live in config.py. Every campaign — CPA partners, SaaS founders,
angel investors, or anything else — is now just a different TargetConfig
instance, not a different code path.

Nothing in here is CPA-specific. The old CPA behaviour is preserved as a
*preset* (CPA_PARTNER_PRESET, at the bottom) built out of this same generic
schema, purely so the existing CLI keeps working unchanged if no config is
supplied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Career-stage targeting: an alternative to requiring a specific senior
# `titles` match, for campaigns that want "young/early-career professionals
# in this field" rather than "people whose current title says Founder/
# Director/etc." Two pieces work together (see TargetConfig.career_stage
# below, and query_generator.build_post_queries):
#   - EARLY_CAREER_QUALIFIERS: phrases folded into search queries so
#     discovery actively looks for career-stage signals ("recent graduate",
#     "early career", ...), not just the bare subject/title.
#   - SENIOR_TITLE_MARKERS: substrings that get auto-added to a campaign's
#     exclude_titles (see TargetConfig.effective_exclude_titles) so an
#     obviously senior/executive title is rejected even if the campaign
#     never typed it into exclude_titles itself.
# Neither list is exhaustive by design -- these are recall/precision aids,
# not a hard taxonomy. A campaign can still add its own exclude_titles on
# top of the built-in ones.
# ---------------------------------------------------------------------------

CAREER_STAGES = ("any", "early_career")

EARLY_CAREER_QUALIFIERS = (
    "recent graduate",
    "early career",
    "entry level",
    "young professional",
)

SENIOR_TITLE_MARKERS = (
    "chief", "ceo", "cfo", "coo", "cto", "cmo", "president", "vice president",
    "svp", "evp", "vp ", "director", "head of", "founder", "co-founder",
    "owner", "partner", "principal", "managing director", "general manager",
    "executive", "senior manager", "senior director",
)


@dataclass
class TargetConfig:
    """User-facing campaign criteria. Maps directly to the UI config form.

    Every field is optional except `target_count`, so a caller can supply as
    little or as much as they know. Empty lists / None mean "no constraint on
    this dimension" — NOT "match nothing".
    """

    locations: list[str] = field(default_factory=lambda: ["United States"])
    titles: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    # How `industries` and `keywords` combine when BOTH are configured:
    #   "all" (default, backward-compatible): a candidate must independently
    #     satisfy both dimensions -- the industry terms AND the keyword
    #     terms each need their own qualifying evidence. This is the
    #     strictest reading and is appropriate for campaigns where the two
    #     dimensions are genuinely orthogonal requirements.
    #   "any": a candidate needs evidence for industries OR keywords, not
    #     necessarily both. Appropriate when the two dimensions describe
    #     overlapping facets of the same thing a campaign is looking for
    #     (e.g. industries=["SaaS"], keywords=["AI","automation"] -- a
    #     founder who is clearly AI/automation-focused but whose thin DDGS
    #     snippet never literally says "SaaS", or vice versa, is still a
    #     good match). "all" mode on overlapping dimensions like this tends
    #     to reject far more genuinely-qualified candidates than it should,
    #     since real-world search snippets rarely state every angle of a
    #     person's business in one short blurb.
    # Only affects `industries`+`keywords`; every other dimension (titles,
    # locations, exclude_*, company_size, age) is unaffected and always
    # still required when configured.
    topic_match_mode: str = "all"

    # --- optional synonym expansion -------------------------------------
    # Purely additive recall aids: each maps one of the *configured* base
    # terms above (titles/industries/keywords) to a list of alternative
    # phrasings that should be treated as equivalent evidence for that same
    # dimension -- e.g. title_synonyms={"Founder": ["Founder & CEO",
    # "Co-Founder", "Founding Partner"]}. A synonym key that doesn't match
    # any configured base term is ignored (never silently expands a
    # dimension the campaign didn't actually ask for). Used by both
    # query_generator.py (broader query phrasing) and quality.py's
    # matches_target_criteria (broader evidence matching) via the
    # expanded_titles/expanded_industries/expanded_keywords properties
    # below -- nothing here is hard-coded to any one vertical; a Fintech or
    # CPA campaign supplies its own synonym maps the same way.
    title_synonyms: dict[str, list[str]] = field(default_factory=dict)
    industry_synonyms: dict[str, list[str]] = field(default_factory=dict)
    keyword_synonyms: dict[str, list[str]] = field(default_factory=dict)

    company_size_min: int | None = None
    company_size_max: int | None = None

    # Age is a *proxy* filter applied downstream during qualification (Day 3+),
    # never invented — see quality.py age_source rules. Stored here only as
    # the user's requested bounds.
    age_min: int | None = None
    age_max: int | None = None

    # --- "age check" (Aug 2026): a lighter-weight, always-on-by-default
    # early/mid-career gate, distinct from the age_min/age_max numeric range
    # filter above (which only fires when a campaign explicitly configures
    # it). Uses two independent, evidence-only proxies -- see
    # quality.age_check_ok -- either one is enough to confirm the candidate
    # reads as "around this age group":
    #   1. fewer than `age_check_max_years` years of stated professional
    #      experience.
    #   2. fewer than `age_check_max_years` years since a stated
    #      undergraduate graduation year.
    # A row with neither signal present is "unknown", not "too senior", and
    # is not rejected on this check alone -- same missing-data contract as
    # every other proxy filter in this file. Runs in its own "age_check"
    # phase, before "qualify" (see orchestrator.py); rows that fail it are
    # dropped before qualification ever sees them.
    age_check_enabled: bool = True
    age_check_max_years: int = 12

    target_count: int = 1000

    # What `target_count` counts. "raw" (default) is the original,
    # backward-compatible meaning: stop once `target_count` raw/discovered
    # candidates have been collected, regardless of how many go on to
    # qualify. "qualified" means the discovery loop should keep going
    # (subject to the budget fields below) until approximately
    # `target_count` candidates have actually QUALIFIED -- so
    # target_count=50 in "qualified" mode aims to produce ~50 usable
    # prospects, not 50 raw hits that mostly get filtered out.
    target_count_mode: str = "raw"

    # Budget controls for "qualified" mode (ignored in "raw" mode, where
    # target_count itself is already the hard stopping point). None means
    # "no explicit cap on this dimension" -- callers should still apply a
    # sane default rather than looping forever. These exist so
    # target_count_mode="qualified" with an unreachable target_count (e.g.
    # 5000 qualified prospects for a tiny niche) can't turn into unbounded
    # search-engine traffic.
    max_queries: int | None = None
    max_raw_candidates: int | None = None
    max_minutes: float | None = None

    # Free-form negative signal words, e.g. to exclude "recruiter", "student".
    exclude_keywords: list[str] = field(default_factory=list)

    # Same "negative signal" idea as exclude_keywords, but scoped to the
    # other two searchable dimensions instead of the free-form one:
    #   - exclude_titles: reject a profile whose (curated) job title
    #     matches one of these, even if it also matched a wanted title
    #     (e.g. titles=["Founder"], exclude_titles=["Co-Founder"] keeps
    #     solo founders but drops co-founders). Checked the same way
    #     _titles_ok() checks `titles` -- substring match against the
    #     decontaminated profile_title only.
    #   - exclude_locations: reject a profile whose location matches one
    #     of these, using the same matching rules as `locations`/
    #     _location_ok() (US heuristics for "United States" and common
    #     spellings, case-insensitive substring for anything else) -- e.g.
    #     locations=["United States"], exclude_locations=["California"]
    #     keeps the rest of the US but drops CA.
    # Like exclude_keywords, both are OR'd internally (any one matching
    # term is enough to exclude) and empty means "no exclusion".
    exclude_titles: list[str] = field(default_factory=list)
    exclude_locations: list[str] = field(default_factory=list)

    # --- career-stage targeting -----------------------------------------
    # "any" (default): unchanged behaviour -- titles/exclude_titles are
    # exactly what's configured, nothing implicit added.
    # "early_career": for campaigns that want young/early-career
    # professionals in a field rather than people who already hold a
    # senior/formal title in it (e.g. a Health Coach campaign that wants
    # recent grads and associates, not just Directors of Wellness). Two
    # effects, both additive on top of whatever the campaign already
    # configured:
    #   1. query_generator.build_post_queries() folds EARLY_CAREER_QUALIFIERS
    #      ("recent graduate", "early career", ...) into its query phrasing.
    #   2. effective_exclude_titles (below) merges in SENIOR_TITLE_MARKERS,
    #      so an obviously senior title is auto-rejected even if the
    #      campaign never typed it into exclude_titles.
    # Never changes the titles dimension itself -- a campaign's own titles
    # (e.g. "Health Coach") still has to match (or, for LinkedIn-posts-
    # sourced candidates, is treated as satisfied by topical evidence
    # instead -- see quality.matches_target_criteria).
    career_stage: str = "any"

    # Opt-in: also run the LinkedIn-posts discovery source (see
    # pipeline.sources.linkedin_posts_search), which searches post content
    # for topical engagement instead of profile headlines/titles -- a
    # second, complementary way to surface people who post about a field
    # without yet holding a formal title in it. Off by default: it's a
    # slower, lower-precision source (post URLs only yield a *guessed*
    # /in/ profile slug, not a confirmed one -- see that module's
    # docstring), so campaigns opt in deliberately rather than having it
    # silently added to every discovery run.
    search_posts: bool = False

    # Optional human label, used for output file naming / campaign naming.
    name: str = "campaign"

    # --- optional Campaign metadata/templates -------------------------
    # None of these are required. They exist so a campaign's outreach
    # copy can be driven entirely by its TargetConfig JSON (the config
    # mechanism that already exists for discovery/qualification) instead
    # of hard-coding any vertical-specific subject/body text into generic
    # pipeline code. When omitted, campaign.ensure_campaign() falls back
    # to its own generic default templates. `campaign_name` defaults to
    # `name` above when unset.
    campaign_name: str | None = None
    campaign_description: str = ""
    email_subject_template: str | None = None
    email_body_template: str | None = None
    email_sender_name: str = ""

    def __post_init__(self) -> None:
        self.locations = _clean_list(self.locations)
        self.titles = _clean_list(self.titles)
        self.industries = _clean_list(self.industries)
        self.keywords = _clean_list(self.keywords)
        self.exclude_keywords = _clean_list(self.exclude_keywords)
        self.exclude_titles = _clean_list(self.exclude_titles)
        self.exclude_locations = _clean_list(self.exclude_locations)
        self.title_synonyms = _clean_synonym_map(self.title_synonyms)
        self.industry_synonyms = _clean_synonym_map(self.industry_synonyms)
        self.keyword_synonyms = _clean_synonym_map(self.keyword_synonyms)
        self.career_stage = (self.career_stage or "any").strip().lower()
        if self.career_stage not in CAREER_STAGES:
            raise ValueError(
                f"career_stage must be one of {CAREER_STAGES!r}, got {self.career_stage!r}"
            )
        self.topic_match_mode = (self.topic_match_mode or "all").strip().lower()
        if self.topic_match_mode not in ("all", "any"):
            raise ValueError(
                f"topic_match_mode must be 'all' or 'any', got {self.topic_match_mode!r}"
            )
        if self.target_count <= 0:
            raise ValueError("target_count must be a positive integer")
        if self.target_count_mode not in TARGET_COUNT_MODES:
            raise ValueError(
                f"target_count_mode must be one of {TARGET_COUNT_MODES!r}, "
                f"got {self.target_count_mode!r}"
            )
        if self.age_min is not None and self.age_max is not None:
            if self.age_min > self.age_max:
                raise ValueError("age_min cannot be greater than age_max")
        if self.age_check_max_years <= 0:
            raise ValueError("age_check_max_years must be a positive integer")
        if self.company_size_min is not None and self.company_size_max is not None:
            if self.company_size_min > self.company_size_max:
                raise ValueError("company_size_min cannot be greater than company_size_max")

    # --- (de)serialization -------------------------------------------------

    # --- synonym-expanded views ---------------------------------------
    # What query_generator.py and quality.py's matches_target_criteria
    # actually match/search against -- the configured base terms plus
    # whatever synonyms were supplied for them. Equal to the base list
    # itself when no synonyms are configured, so nothing changes for an
    # existing config that doesn't use this feature.

    @property
    def expanded_titles(self) -> list[str]:
        return _expand_with_synonyms(self.titles, self.title_synonyms)

    @property
    def expanded_industries(self) -> list[str]:
        return _expand_with_synonyms(self.industries, self.industry_synonyms)

    @property
    def expanded_keywords(self) -> list[str]:
        return _expand_with_synonyms(self.keywords, self.keyword_synonyms)

    @property
    def effective_exclude_titles(self) -> list[str]:
        """exclude_titles as actually enforced: the campaign's own
        exclude_titles, plus (only when career_stage == "early_career")
        the built-in SENIOR_TITLE_MARKERS -- so an early-career campaign
        auto-rejects obviously senior/executive titles without the
        campaign having to type them all in manually. De-duplicated
        case-insensitively, campaign-configured terms first. Identical to
        plain `exclude_titles` for career_stage == "any" (the default), so
        this changes nothing for an existing config that doesn't use the
        feature.
        """
        if self.career_stage != "early_career":
            return list(self.exclude_titles)
        seen = {t.lower() for t in self.exclude_titles}
        merged = list(self.exclude_titles)
        for marker in SENIOR_TITLE_MARKERS:
            if marker not in seen:
                seen.add(marker)
                merged.append(marker)
        return merged

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TargetConfig":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown TargetConfig field(s): {', '.join(sorted(unknown))}")
        return cls(**data)

    @classmethod
    def from_json_file(cls, path: str | Path) -> "TargetConfig":
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    def to_json_file(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    # --- output naming -------------------------------------------------

    def output_stem(self) -> str:
        """Filesystem-safe stem for this campaign's output CSV, e.g. 'saas_founders'."""
        import re

        slug = re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_")
        return slug or "campaign"


TARGET_COUNT_MODES = ("raw", "qualified")


def _clean_synonym_map(mapping: dict[str, list[str]] | None) -> dict[str, list[str]]:
    """Trim/drop-empty for a {base_term: [synonym, ...]} map, mirroring
    _clean_list()'s rules for the value lists."""
    if not mapping:
        return {}
    out: dict[str, list[str]] = {}
    for key, values in mapping.items():
        key = (key or "").strip()
        if not key:
            continue
        cleaned = _clean_list(values)
        if cleaned:
            out[key] = cleaned
    return out


def _expand_with_synonyms(base: list[str], synonyms: dict[str, list[str]]) -> list[str]:
    """base terms + every configured synonym for each, de-duplicated
    case-insensitively while preserving order. A synonym key that doesn't
    case-insensitively match one of `base`'s own terms is ignored, so a
    synonym map can't silently pull in evidence for a dimension the
    campaign didn't actually configure."""
    base_lower = {b.lower() for b in base}
    seen = set(base_lower)
    out = list(base)
    for key, values in synonyms.items():
        if key.strip().lower() not in base_lower:
            continue
        for v in values:
            low = v.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append(v)
    return out


def _clean_list(values: list[str] | None) -> list[str]:
    """Trim, drop empties, de-dupe case-insensitively while preserving order."""
    if not values:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# Legacy preset: reproduces the exact criteria that used to be hard-coded in
# config.py (CPA_TITLES + the CPA-only query strings). Used only as the
# default when the orchestrator is run with no --config / no CLI overrides,
# so the existing CPA collection script keeps behaving identically.
# ---------------------------------------------------------------------------
CPA_PARTNER_PRESET = TargetConfig(
    name="us_cpa_partners",
    locations=["United States"],
    titles=[
        "managing partner", "name partner", "partner", "founding partner",
        "senior partner", "tax partner", "audit partner",
    ],
    industries=["CPA firm", "accounting firm", "public accounting"],
    keywords=[],
    target_count=5000,
)
