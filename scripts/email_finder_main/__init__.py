"""Thin, importable adapter around email-finder-main's candidate patterns.

This is what `scripts/pipeline/email_discovery.py`'s
`EmailFinderMainGenerator` soft-imports as `email_finder_main` and calls via
`find(first_name=..., last_name=..., company_name=..., company_domain=...)`
(see that module's `_SoftImportGenerator`/`EmailFinderMainGenerator`
docstrings). It's the FALLBACK generator — only invoked when the primary
and supplementary generators together find nothing.

Upstream email-finder-main (Go, vendored for provenance at
`../../email-finder-main/vendor_src/`) builds 5 local-part patterns and
pairs them with a domain hardcoded as `{company}.in`. See
`../../email-finder-main/README.md` for exactly what changed and why: this
adapter reimplements the pattern half in pure Python (no Go toolchain
needed for a fallback-only generator) and resolves a real domain instead of
assuming `.in` — using `company_domain` when the caller already has it,
falling back to a slugified guess across a few common TLDs otherwise.

No SMTP probing here (upstream's `checkEmail.go` did its own raw `RCPT TO`
dial) — every generator in this pipeline only generates; MX/SMTP validation
is centralized in `NodeMXChecker`/`NodeSMTPChecker`.
"""

from __future__ import annotations

import re

# Same short list ScrapegraphPatternGenerator (the primary generator) tries
# — kept independent/duplicated here rather than imported, since this
# fallback generator is deliberately isolated (see module docstring above
# and EmailFinderMainGenerator's docstring in email_discovery.py).
_GUESS_TLDS: tuple[str, ...] = (".com", ".ai", ".io", ".co")

_STOPWORDS = {"the", "a", "an", "and", "of", "group", "partners"}
_LEGAL_SUFFIXES = {
    "llc", "llp", "pllc", "pc", "pa", "inc", "incorporated", "corp",
    "corporation", "co", "cpas", "cpa", "associates", "assoc",
}


def _clean_domain(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().lower()
    value = value.removeprefix("https://").removeprefix("http://")
    value = value.removeprefix("www.")
    return value.split("/")[0].strip()


def _slugify_company(company: str) -> str:
    company = re.sub(r"[’']", "", (company or "").lower())
    company = re.sub(r"[^a-z0-9\s&]", " ", company)
    company = company.replace("&", " and ")
    words = [w for w in company.split() if w not in _STOPWORDS]
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return "".join(words)


def _candidate_domains(company_name: str | None, company_domain: str | None) -> list[str]:
    domain = _clean_domain(company_domain)
    if domain:
        return [domain]
    slug = _slugify_company(company_name or "")
    if not slug:
        return []
    return [f"{slug}{tld}" for tld in _GUESS_TLDS]


def find(
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    company_name: str | None = None,
    company_domain: str | None = None,
) -> list[dict]:
    """Guess candidate emails using email-finder-main's 5 local-part
    patterns (first+last, f+last, l+first, f+l, first.last), against the
    lead's real company domain when known, or a small set of guessed TLDs
    off the company name otherwise.

    :return: list of {"email"} dicts. Never raises -- returns [] if there
        isn't enough information (no first+last name, or no domain and no
        company name to guess one from) to generate anything.
    """
    first = re.sub(r"[^a-z]", "", (first_name or "").strip().lower())
    last = re.sub(r"[^a-z]", "", (last_name or "").strip().lower())
    if not first or not last:
        return []

    domains = _candidate_domains(company_name, company_domain)
    if not domains:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for domain in domains:
        localparts = [
            f"{first}{last}",
            f"{first[0]}{last}",
            f"{last[0]}{first}",
            f"{first[0]}{last[0]}",
            f"{first}.{last}",
        ]
        for local in localparts:
            email = f"{local}@{domain}"
            if email in seen:
                continue
            seen.add(email)
            out.append({"email": email})
    return out
