"""LinkedIn-posts discovery source: an alternative to the profile-only
(`site:linkedin.com/in`) search in ddgs_search.py, for finding people via
what they *post about* instead of what their current job title says.

WHY THIS EXISTS: the primary discovery source (ddgs_search.py) only ever
searches LinkedIn profile pages and requires the profile's own headline
(profile_title) to match one of the campaign's configured `titles` (see
quality._titles_ok). That's precise, but it structurally can't find someone
who does relevant work without yet holding a title that says so -- e.g. a
recent graduate doing health-coaching work under an employer's brand, with
a LinkedIn headline that just says "Team Member at ...". This source
instead searches `site:linkedin.com/posts` for people actively posting
about a topic (see query_generator.build_post_queries), which is a
title-independent signal -- exactly the gap TargetConfig.search_posts /
career_stage="early_career" are meant to cover.

IMPORTANT LIMITATION, stated plainly rather than glossed over: a LinkedIn
*post* URL (`linkedin.com/posts/{slug}_...-activity-{id}`) is not the same
as that person's *profile* URL, and search engines don't reliably expose
the author's real profile link from a post snippet alone. The `{slug}`
segment right after `/posts/` is very often (not always -- LinkedIn also
uses this URL shape for reposts, company-page posts, and some numeric-ID
slugs) the author's own profile slug, so this module derives a *guessed*
`https://www.linkedin.com/in/{slug}` and runs it through the exact same
`normalize_linkedin`/`matches_target_criteria` gates every other source
uses. Nothing here claims that guess is verified -- callers/reviewers
should treat a `source == "linkedin_posts"` lead as lower-confidence on
identity than one from `ddgs_search` until a human (or the existing email-
validation stage) confirms it.

Opt-in only (see TargetConfig.search_posts) -- never run automatically,
and never a substitute for the primary profile-search source.
"""

from __future__ import annotations

import re
import sys
import time
from typing import Callable

from ..config import DISCOVERY_DELAY_SEC, DISCOVERY_NUM_RESULTS, get_active_target
from ..quality import (
    extract_email,
    extract_location,
    extract_phone,
    is_company_or_org_page,
    is_valid_person_name,
    matches_target_criteria,
)
from ..models import InvestorRow
from ..query_generator import build_post_queries
from ..target_config import TargetConfig
from .ddgs_search import ddgs_search

SOURCE_NAME = "linkedin_posts"

# The segment right after "/posts/" up to the first "_" or "/" -- see
# module docstring's limitation note. Requires at least 3 chars so a bare
# numeric activity-id fragment or single-letter junk can't be mistaken for
# a slug.
_POST_SLUG_RE = re.compile(r"linkedin\.com/posts/([a-z0-9][a-z0-9\-]{2,80})[_/]", re.I)

# Post URL shapes that are never a person's own profile slug -- a company
# page's or school's posts, or LinkedIn's own newsletter/article surface.
_NON_PERSON_SLUGS = {"company", "school", "showcase", "newsletters"}


def _guess_profile_url(post_url: str) -> str:
    """Best-effort, UNVERIFIED derivation of a /in/ profile URL from a
    /posts/ URL. Returns "" if no plausible slug is found."""
    m = _POST_SLUG_RE.search(post_url or "")
    if not m:
        return ""
    slug = m.group(1).strip("-").lower()
    if not slug or slug in _NON_PERSON_SLUGS:
        return ""
    return f"https://www.linkedin.com/in/{slug}"


def _extract_post_author_name(title: str) -> str:
    """Post search-hit titles are typically "{Name} on LinkedIn: {post
    text}" (sometimes "{Name} - {snippet}"). Pull just the name segment,
    same shape/validity check as ddgs_search.parse_search_hit uses for
    profile hits.
    """
    name = re.split(r"\s+on LinkedIn\b", title, maxsplit=1, flags=re.I)[0]
    name = name.split("|")[0].split(" - ")[0].strip()
    name = re.sub(r"^#+\s*", "", name)
    return name


def parse_post_hit(
    hit: dict[str, str],
    target: TargetConfig | None = None,
    *,
    require_target_match: bool = True,
) -> InvestorRow | None:
    """Parse one LinkedIn-posts search hit into an InvestorRow.

    Deliberately does NOT populate profile_title from the post content --
    a post's own text is not that person's job-title headline, and
    matches_target_criteria() knows to skip the titles-dimension check
    entirely for source == "linkedin_posts" rows (see quality.py) rather
    than have this module invent a fake title. industries/keywords
    evidence is still required exactly as normal, via the post text
    becoming this row's `summary`.
    """
    url = hit.get("href", "") or ""
    if "linkedin.com/posts/" not in url.lower():
        return None

    profile_url = _guess_profile_url(url)
    if not profile_url:
        return None

    title = (hit.get("title") or "").strip()
    text = (hit.get("body") or "").strip()

    if is_company_or_org_page(title, text):
        return None

    name = _extract_post_author_name(title)
    if not is_valid_person_name(name):
        return None

    loc = extract_location(text, title)
    if not loc:
        return None

    candidate: InvestorRow = {
        "name": name,
        "location": loc,
        "linkedin_url": profile_url,
        # See module docstring -- never populated from post content.
        "profile_title": "",
        "summary": text[:2000].strip(),
        "industries": "",
        "email": extract_email(text),
        "phone": extract_phone(text),
        "source": SOURCE_NAME,
    }
    active_target = target if target is not None else get_active_target()
    if require_target_match and not matches_target_criteria(candidate, active_target):
        return None
    return candidate


def run_posts_phase(
    investors: dict[str, InvestorRow],
    *,
    target: TargetConfig,
    add_investor: Callable[[dict[str, InvestorRow], InvestorRow], bool],
    on_checkpoint: Callable[[], None] | None = None,
    on_candidate: Callable[[InvestorRow], None] | None = None,
    on_query_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Same shape/contract as ddgs_search.run_ddgs_phase (checkpoint/
    candidate/query-progress callbacks), so it plugs into
    orchestrator.run_discovery the same way. `add_investor` is passed in
    (rather than imported directly) to avoid a quality<->sources import
    cycle and to keep this module's only quality.py dependency the
    stateless helper functions it already imports above.
    """
    added = 0
    since_checkpoint = 0
    queries = build_post_queries(target)
    empty_streak = 0

    print(
        f"Running LinkedIn posts discovery on {len(queries)} queries "
        f"(topic-first, opt-in source)...",
        file=sys.stderr,
    )

    for i, query in enumerate(queries, 1):
        if len(investors) >= target.target_count:
            break
        if on_query_progress is not None:
            try:
                on_query_progress(i, len(queries))
            except Exception as exc:  # noqa: BLE001
                print(f"  on_query_progress warn: {exc}", file=sys.stderr)

        results = ddgs_search(f"site:linkedin.com/posts {query}", DISCOVERY_NUM_RESULTS, linkedin_only=False)
        accepted = 0
        for hit in results:
            parsed = parse_post_hit(hit, target=target)
            if parsed and add_investor(investors, parsed):
                added += 1
                accepted += 1
                since_checkpoint += 1
                if on_candidate is not None:
                    try:
                        on_candidate(parsed)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  on_candidate warn: {exc}", file=sys.stderr)
            if len(investors) >= target.target_count:
                break

        empty_streak = 0 if (accepted or results) else empty_streak + 1
        if i % 10 == 0 or accepted:
            print(
                f"  posts {i}/{len(queries)} +{accepted} this query "
                f"(added={added} total={len(investors)})",
                file=sys.stderr,
            )
        if empty_streak >= 15:
            print(
                f"  posts: {empty_streak} empty queries in a row -- stopping posts search early",
                file=sys.stderr,
            )
            break
        if since_checkpoint >= 25 and on_checkpoint:
            on_checkpoint()
            since_checkpoint = 0

        time.sleep(DISCOVERY_DELAY_SEC)

    print(f"  +{added} from LinkedIn posts (total {len(investors)})", file=sys.stderr)
    return added
