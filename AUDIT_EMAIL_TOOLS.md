# Audit: validate_email / email-verifier / Email-Finder-main / emailFinder-main

Performed against `prospect-automation`'s existing generator stack
(`Email_Finder` primary, `Mailfoguess` supplementary, `email-finder-main`
fallback — see those directories' own READMEs) and its existing
MX/SMTP-only verification layer in `scripts/pipeline/email_discovery.py`.

| Project | License | Useful components | Reusable? | Integration difficulty | Risk |
|---|---|---|---|---|---|
| `validate_email-master` | **LGPLv3** (copyleft) | RFC-2822 syntax regex; SMTP RCPT-based existence check (`smtplib`, no catch-all probe) | Idea only — **not vendored** | N/A | LGPL in a project with no clear library/link boundary is exactly the "accidental copyleft contamination" the task warned about. `normalize_email()`/the Node SMTP checker already cover the same ground; reimplemented independently instead. |
| `email-verifier-main` (AfterShip) | **MIT** | (1) `metadata_free.go` — 4,479 free-mail-provider domains. (2) `metadata_role.go` — 888 role-account local-parts (`info`, `admin`, `sales`, ...). (3) `metadata_disposable.go` — 123,324 disposable-domain list. (4) `smtp.go`'s **catch-all detection** technique (probe a random local-part at the same domain; if that also returns 250, a real RCPT 250 proves nothing about the specific mailbox). (5) `suggestion.go` — Levenshtein typo-domain suggestions (`gmial.com` → `gmail.com`). | **Yes — data vendored verbatim, logic reimplemented in Python** | Low (flat data files) | None (MIT, permissive) — LICENSE + provenance kept at `email-verifier/`. |
| `Email-Finder-main` (Aboodseada) | MIT | Search-engine-based discovery: builds `"first.last@domain"`-style and `"name" "domain" email` queries, regex-extracts real email addresses that appear **verbatim in live search-result text**, filters against a domain blacklist and (optionally) the target company domain. | **Concept adopted, code not executed** | High to actually run (needs a SearXNG instance / live web search — outside this sandbox's egress allowlist and outside the scope of a candidate *generator*, since this pipeline's generators are offline/deterministic) | Low license risk (MIT); the real integration cost is operational (a live search backend), not legal. Adopted as a **source-quality concept** instead: `identity_match.py`'s source-tier model gives a "found verbatim in a reliable source" candidate the top tier this repo's technique would produce, so the architecture is ready for it the day a live search generator is wired in. |
| `emailFinder-main` (Sougata Das) | MIT | React + Supabase **crowdsourced** email directory — users manually submit/upvote/downvote an email against a LinkedIn profile. No discovery or validation algorithm of its own (client-only app). | **Not integrated** | N/A | No backend logic to take. The one transferable idea — human corroboration as a trust signal — is conceptually a source-quality tier (`"human_verified"`), included in `identity_match.py`'s `SOURCE_TIER_*` constants for a future manual-review feature, but no UI was built for it (out of scope here). |

## What was already present vs. newly integrated

Already present (untouched): `Email_Finder`, `Mailfoguess`, `email-finder-main`
candidate generators; `NodeMXChecker`/`NodeSMTPChecker`; `SMTP_CATCH_ALL`
already existed as a status *value* but nothing upstream ever computed or
scored it (see `identity_match.py` module docstring for detail).

Newly integrated: `scripts/pipeline/identity_match.py` (new module) +
`scripts/pipeline/vendor_data/{free_domains,role_accounts,disposable_domains}.txt`
(vendored from `email-verifier`, MIT) + wiring into
`email_discovery.py`'s scoring/ranking/selection and `models.py`/CSV/frontend
surfacing. See `IDENTITY_MATCHING.md` at the repo root for the full design
and before/after benchmark.
