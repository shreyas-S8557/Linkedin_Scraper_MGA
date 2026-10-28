"""Generic discovery-query generator, driven entirely by TargetConfig.

Replaces the old config.build_discovery_queries(), which had CPA titles and
CPA firm-type phrasing baked directly into the query strings. Every query
produced here follows the same shape the old CPA queries did (title/industry
+ location + "LinkedIn"), just parameterized instead of hard-coded, so the
existing discovery sources (ddgs, exa) keep working against it unchanged —
they only ever consume a flat list[str] of query strings.
"""

from __future__ import annotations

import random

from .config import US_CITIES, US_STATES
from .target_config import EARLY_CAREER_QUALIFIERS, TargetConfig

# Cap on city x title expansion so a broad config (many titles, many cities)
# doesn't explode into tens of thousands of queries. Nationwide + per-state
# queries always run in full; per-city is the enrichment tier that gets capped.
MAX_ENRICHMENT_CITIES = 80


def build_queries(target: TargetConfig) -> list[str]:
    """Build the full discovery query set for a TargetConfig.

    Mirrors the structure of the old CPA-only builder:
      1. broad nationwide queries (qualified subject + "LinkedIn")
      2. per-state queries
      3. per-city queries (qualified subject, capped tier)
      4. explicit industry x title cross (kept for backward compatibility)
      5. city x title queries (capped, enrichment tier)
      6. keyword-augmented queries
      7. full keyword x industry x title combinations

    "Subject" = titles if given, else industries, else keywords, else a bare
    location search — so a config with only keywords (no titles) still
    produces a sensible query set instead of an empty list.

    Every tier below uses the *qualified* subject (see _qualified_subjects),
    not the bare subject. This matters a lot in practice: for a config like
    titles=["Founder","CEO","CTO"], industries=["SaaS"],
    keywords=["AI","automation"], a bare-subject query like "Founder United
    States LinkedIn" matches literally any founder in any industry — a
    physical therapy practice owner, a boutique retailer, an HR partner are
    all "a Founder/Partner ... in the United States". Since the broad
    nationwide/state/city tiers vastly outnumber a single dedicated
    "industry x title" tier (thousands of city queries vs. a handful of
    industry-tagged ones), leaving them unqualified means the vast majority
    of discovery queries carry no SaaS/AI signal at all, and the qualified
    yield ends up tiny no matter how good the downstream qualification logic
    is. Folding the industry/keyword phrase into the subject itself, for
    every tier, fixes that at the source.
    """
    subjects = _subjects(target)
    qualified_subjects = _qualified_subjects(target, subjects)
    locations = target.locations or ["United States"]
    is_us = any(_is_us_location(loc) for loc in locations)

    queries: list[str] = []

    # 1. Broad nationwide / per-location queries
    for loc in locations:
        for subject in qualified_subjects:
            queries.append(f"{subject} {loc} LinkedIn")

    # 1b. Location-less broadening tier -- always included, not just for
    # non-US targets. Search engines treat every extra required term as a
    # narrower AND, and a country/region name is just one more term the
    # page has to literally contain alongside the subject/qualifiers. The
    # real location requirement is enforced downstream anyway (see
    # quality._location_ok, run against the returned snippet's own
    # evidence, not the query text) -- so dropping the location word from
    # the search string only ever *broadens* what gets retrieved for
    # consideration, never what gets accepted.
    #
    # This matters most for non-US targets: tiers 2/3/5 below (state/city
    # expansion) are US-only, so a non-US campaign (e.g. locations=
    # ["India"]) previously got only the handful of queries in this tier
    # plus 4/6/7/8 -- often under 40 total, nowhere near enough query
    # volume to reliably fill a double-digit target_count. This tier
    # meaningfully increases a non-US campaign's query count at zero extra
    # complexity (no per-country city list to maintain), and gives every
    # campaign a few maximally-broad fallback queries regardless of
    # location.
    for subject in qualified_subjects:
        queries.append(f"{subject} LinkedIn")

    # 2 & 3. US state/city expansion — only meaningful when targeting the US
    # broadly (matches old behaviour, which was nationwide-only).
    if is_us:
        for state in US_STATES:
            for subject in qualified_subjects:
                queries.append(f"{subject} {state} United States LinkedIn")
        for city in US_CITIES:
            for subject in qualified_subjects[:3]:  # keep this tier cheap
                queries.append(f"{subject} {city} United States LinkedIn")

    # 4. Per-title queries (nationwide), if titles were given separately from
    # what became the "subject" (e.g. subjects already includes titles, but
    # we still want the "title + industry" cross product below).
    if target.titles and target.industries:
        for title in target.titles:
            for industry in target.industries:
                for loc in locations:
                    queries.append(f"{industry} {title} {loc} LinkedIn")
                if not is_us:
                    # Same location-less broadening as 1b, for this tier too.
                    queries.append(f"{industry} {title} LinkedIn")

    # 5. City x title enrichment (capped), mirrors the old CPA_TITLES x
    # US_CITIES[:80] tier — now also qualified, same reasoning as tiers 1-3.
    if is_us and target.titles:
        for city in US_CITIES[:MAX_ENRICHMENT_CITIES]:
            for subject in qualified_subjects:
                queries.append(f"{subject} {city} United States LinkedIn")

    # 6. Keyword-augmented queries — combine keywords with titles/industries
    # rather than searching keywords alone (keywords alone are usually too
    # generic to return LinkedIn people-profile results). Skips appending a
    # keyword that's already a substring of the subject itself
    # (case-insensitively) to avoid a redundant, needlessly narrower query
    # like "Health Coach coach India LinkedIn" -- same rationale as
    # _qualified_subjects above. When there are no titles/industries at
    # all, the subject IS the bare keyword (`base_subjects or [kw]`
    # below), so this reduces to a single "{keyword} {loc} LinkedIn"
    # instead of a self-duplicated "{keyword} {keyword} {loc} LinkedIn".
    if target.keywords:
        base_subjects = target.titles or target.industries or []
        for kw in target.keywords:
            for subject in base_subjects or [kw]:
                for loc in locations:
                    phrase = subject if kw.lower() in subject.lower() else f"{subject} {kw}"
                    queries.append(f"{phrase} {loc} LinkedIn")

    # 7. Full keyword x industry x title combinations — the tightest, most
    # targeted tier, matching queries like "AI SaaS Founder LinkedIn" or
    # "generative AI SaaS Founder LinkedIn". Every piece here comes straight
    # from the configured TargetConfig; nothing is hard-coded, so a config
    # of Fintech + blockchain produces "blockchain Fintech Founder LinkedIn"
    # instead, with no code change.
    if target.keywords and target.industries and target.titles:
        for kw in target.keywords:
            for industry in target.industries:
                for title in target.titles:
                    queries.append(f"{kw} {industry} {title} LinkedIn")
                    queries.append(f"{kw} startup {title} LinkedIn")

    # 8. Career-stage qualifiers (opt-in via target.career_stage ==
    # "early_career") -- folds phrases like "recent graduate"/"early
    # career" into the nationwide + per-state tiers (never the huge
    # per-city tiers, to keep this bounded) so discovery actively searches
    # for career-stage signals instead of relying solely on senior/formal
    # title wording. Combined with effective_exclude_titles (see
    # quality.py), which auto-rejects senior titles even when a hit does
    # come back title-first.
    if target.career_stage == "early_career":
        for loc in locations:
            for subject in qualified_subjects:
                for qualifier in EARLY_CAREER_QUALIFIERS:
                    queries.append(f"{qualifier} {subject} {loc} LinkedIn")

    return list(dict.fromkeys(q.strip() for q in queries if q.strip()))


def shuffled_queries_for(target: TargetConfig, seed: int | None = None) -> list[str]:
    queries = build_queries(target)
    rng = random.Random(seed if seed is not None else 42)
    rng.shuffle(queries)
    return queries


def _subjects(target: TargetConfig) -> list[str]:
    """What to search for, in priority order: titles > industries > keywords.

    Uses the synonym-expanded views (see TargetConfig.expanded_titles/
    expanded_industries/expanded_keywords) so a configured title_synonyms
    map (e.g. "Founder" -> ["Founder & CEO", "Co-Founder", "Founding
    Partner"]) actually broadens the query set, not just qualification.
    Equal to the plain titles/industries/keywords lists when no synonyms
    are configured, so this changes nothing for an existing config.
    """
    if target.titles:
        return target.expanded_titles
    if target.industries:
        return target.expanded_industries
    if target.keywords:
        return target.expanded_keywords
    return ["professional"]


def _qualifiers(target: TargetConfig) -> list[str]:
    """Short industry/keyword phrase(s) to fold into every subject so the
    broad nationwide/state/city tiers stay on-topic instead of matching a
    founder/CEO/partner in any industry whatsoever. Entirely config-driven:

    - both industries and keywords configured -> the cross product PLUS
      each dimension standalone, e.g. industries=["SaaS"],
      keywords=["AI","automation"] -> ["AI SaaS", "automation SaaS", "SaaS",
      "AI", "automation"]. Earlier versions returned only the cross
      product, which for a config with several keywords/industries chains
      every one of them into a single query string (e.g. "wellness Health
      and Fitness Nutritionist India LinkedIn" -- 6 words, all required as
      an implicit AND by the search engine). A real search index rarely
      has a page matching that many simultaneous terms at once, especially
      outside a handful of major countries -- a live discovery run (India,
      health-coaching campaign) showed EVERY cross-product query returning
      zero results, including the "broad nationwide" tier, because that
      tier was never actually broad -- it always used the fully-qualified
      subject. Including the standalone, shorter variants alongside the
      cross product keeps the on-topic precision for campaigns that want
      it while also giving every discovery run some queries with
      realistic odds of a real hit.
    - only one of the two configured -> that dimension alone (unchanged)
    - neither configured -> [] (no qualifier available, unchanged)
    """
    if target.industries and target.keywords:
        crossed = [f"{kw} {ind}" for kw in target.expanded_keywords for ind in target.expanded_industries]
        standalone = list(target.expanded_industries) + list(target.expanded_keywords)
        return list(dict.fromkeys(crossed + standalone))
    if target.industries:
        return list(target.expanded_industries)
    if target.keywords:
        return list(target.expanded_keywords)
    return []


def _qualified_subjects(target: TargetConfig, subjects: list[str]) -> list[str]:
    """Fold _qualifiers() into every subject, e.g. "Founder" -> "AI SaaS
    Founder". Only applies when `subjects` actually came from `titles` (the
    common case for a people-search campaign) — when there are no titles,
    `_subjects()` already fell back to industries/keywords as the subject
    itself, and re-combining them with the same terms would just produce
    redundant phrases like "SaaS SaaS". Falls back to the bare subjects
    unqualified when no industries/keywords are configured at all, so a
    config that's titles-only still works exactly as before.

    Skips a qualifier word that already appears in the subject itself
    (case-insensitively) instead of concatenating it anyway -- e.g.
    titles=["Health Coach"], keywords=["coach"] used to produce the
    redundant, needlessly narrower "coach Health Coach"; this now leaves
    that pairing out and keeps "Health Coach" itself in the query set via
    the other qualifiers instead.
    """
    if not target.titles:
        return subjects
    qualifiers = _qualifiers(target)
    if not qualifiers:
        return subjects
    combined = []
    for q in qualifiers:
        for s in subjects:
            if q.lower() in s.lower():
                continue
            combined.append(f"{q} {s}".strip())
    return list(dict.fromkeys(combined)) or list(subjects)


def _is_us_location(location: str) -> bool:
    loc = (location or "").strip().lower()
    return loc in {"united states", "usa", "u.s.", "us"} or loc in {s.lower() for s in US_STATES}


# ---------------------------------------------------------------------------
# LinkedIn-posts query tier (see sources/linkedin_posts_search.py). Distinct
# from build_queries() above: post search targets *content people write*
# about a topic, not a profile headline/title, so these queries are
# deliberately topic-first (industries/keywords), never title-first --
# combined with a small set of authentic "sharing something" phrasings
# real posts actually use, so results skew toward posts, not generic
# articles that happen to mention the topic.
# ---------------------------------------------------------------------------

POST_ENGAGEMENT_PHRASES = (
    "excited to share",
    "thrilled to announce",
    "proud to share",
    "grateful for the opportunity to",
    "my journey as a",
    "reflecting on my time as a",
)


def build_post_queries(target: TargetConfig) -> list[str]:
    """Query set for the LinkedIn-posts discovery source. Deliberately
    small/bounded (unlike build_queries()'s nationwide/state/city tiers):
    post search is a lower-precision, opt-in source (see
    TargetConfig.search_posts), so this keeps query volume conservative
    rather than multiplying by every city/state.

    Topic-first by design: uses _qualifiers(target) (industry/keyword
    phrase, e.g. "AI SaaS") as the subject whenever available, only
    falling back to _subjects(target) (which would include titles) when
    no industry/keyword is configured at all -- searching post content
    for a job title phrase is rarely how people actually write about
    their own work, so titles are a last resort here, not the default.

    When target.career_stage == "early_career", EARLY_CAREER_QUALIFIERS
    ("recent graduate", "early career", ...) are folded in alongside the
    engagement phrases, e.g. "early career excited to share AI SaaS" --
    doubling down on career-stage signal for this source specifically.
    """
    subject_pool = _qualifiers(target) or _subjects(target)
    stage_terms = ("",) if target.career_stage != "early_career" else ("",) + EARLY_CAREER_QUALIFIERS

    queries: list[str] = []
    for subject in subject_pool:
        for phrase in POST_ENGAGEMENT_PHRASES:
            for stage in stage_terms:
                q = " ".join(part for part in (stage, phrase, subject) if part).strip()
                queries.append(q)
    return list(dict.fromkeys(q.strip() for q in queries if q.strip()))

