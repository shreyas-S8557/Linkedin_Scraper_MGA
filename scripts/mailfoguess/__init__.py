"""Thin, importable adapter around Mailfoguess's local-part guesser.

This is what `scripts/pipeline/email_discovery.py`'s `MailfoguessGenerator`
soft-imports as `mailfoguess` and calls via
`find_candidates(first_name=..., last_name=..., company_domain=...)` (see
that module's `_SoftImportGenerator`/`MailfoguessGenerator` docstrings).

The heavy lifting — permuting first/middle/last name + initials across
separators at a configurable generation level — is Mailfoguess's own
`LocalPartGenerator`/`User`, vendored verbatim (aside from one import path)
into `_localpart_generator.py` / `_user.py`. See `../../Mailfoguess/README.md`
for what was and wasn't carried over from upstream, and why.

Upstream Mailfoguess only ever paired those local-parts with a fixed list
of free-mail providers (gmail, yahoo, outlook, ...) — it's built for OSINT
lookups of a person's personal inbox. This adapter guesses **both**:

  * the lead's real company domain (`company_domain`) — the useful case for
    a B2B prospecting pipeline, and
  * a short list of common free-mail providers, as a fallback for prospects
    (solo founders, angels, etc.) who list a personal address.

Each returned item is tagged `domain_type` ("company" or "personal") so the
caller can weight them differently; `MailfoguessGenerator` in
`email_discovery.py` scores company-domain guesses higher by default, since
that's the actually-useful signal for this pipeline, and only lets a
personal-domain guess win once it's independently SMTP-confirmed.
"""

from __future__ import annotations

from ._localpart_generator import LocalPartGenerator
from ._user import User

# Small, deliberately short list — see Mailfoguess/README.md for why we
# don't pull in the upstream tool's full ~90-domain "all" provider list.
# Longer lists mostly add noise (near-certain MX validity for every one of
# them, since they're all major providers) without adding real signal.
DEFAULT_FREE_MAIL_PROVIDERS: tuple[str, ...] = (
    "gmail.com",
    "outlook.com",
    "hotmail.com",
    "yahoo.com",
    "icloud.com",
)

DEFAULT_LEVEL = "low"


def _clean_domain(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().lower()
    value = value.removeprefix("https://").removeprefix("http://")
    value = value.removeprefix("www.")
    return value.split("/")[0].strip()


def find_candidates(
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    middle_name: str | None = None,
    company_domain: str | None = None,
    level: str = DEFAULT_LEVEL,
    free_providers: tuple[str, ...] | None = DEFAULT_FREE_MAIL_PROVIDERS,
    max_localparts: int = 60,
) -> list[dict]:
    """Guess candidate emails for a person at both their company domain and
    a short list of common free-mail providers.

    :param first_name: person's first name
    :param last_name: person's last name
    :param middle_name: optional middle name
    :param company_domain: the lead's company domain (e.g. "acme.com"),
        already resolved by the caller — this adapter does not itself guess
        a domain from a company name (`ScrapegraphPatternGenerator` already
        owns that job as the primary generator).
    :param level: Mailfoguess generation level -- "min"/"low"/"high"/"max".
        Higher levels produce many more local-part permutations (initials,
        vowel-stripped last names, reordering); "low" is a reasonable
        default supplementary-generator setting -- enough extra coverage
        beyond the primary pattern generator without exploding candidate
        volume.
    :param free_providers: which free-mail domains to also try. Pass an
        empty tuple/None to guess only against `company_domain`.
    :param max_localparts: cap on how many generated local-parts are turned
        into candidates, to keep supplementary output bounded even at
        higher generation levels.

    :return: list of {"email", "domain", "domain_type"} dicts. Never
        raises -- returns [] if there isn't enough information (no name at
        all) to generate anything.
    """
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    middle_name = (middle_name or "").strip()
    if not first_name and not last_name:
        return []

    user = User(
        firstname=first_name,
        middlename=middle_name,
        lastname=last_name,
        username=None,
        number=None,
    )
    generator = LocalPartGenerator(level=level or DEFAULT_LEVEL, separators="")
    localparts = generator.generate(user)[:max_localparts]
    if not localparts:
        return []

    domains: list[tuple[str, str]] = []
    company_domain = _clean_domain(company_domain)
    if company_domain:
        domains.append((company_domain, "company"))
    for provider in free_providers or ():
        domains.append((provider, "personal"))

    out: list[dict] = []
    seen: set[str] = set()
    for domain, domain_type in domains:
        for local in localparts:
            if not local:
                continue
            email = f"{local}@{domain}"
            if email in seen:
                continue
            seen.add(email)
            out.append({"email": email, "domain": domain, "domain_type": domain_type})
    return out
