# Identity matching: design + benchmark

Fixes the reported bug: `davis@gmail.com` for "Stacey Davis" (and
`abel@gmail.com`, `anna@gmail.com`, `ch@gmail.com`, etc.) were reaching
`email_status = validated` / `email_confidence = low` purely because the
old scoring model (`email_discovery.score_candidate`) only ever measured
**deliverability** (MX/SMTP) — nothing asked "does this local-part
actually look like this lead's name?"

## Architecture

```
EmailCandidate (email, sources, domain, mx_status, smtp_status, ...)
        │
        ▼
apply_identity_assessment(candidates, lead)   [NEW -- email_discovery.py]
        │  calls identity_match.assess() per candidate
        ▼
identity_match.py  [NEW MODULE]
  ├─ classify_localpart_pattern()  -- full_first_last / initial_last /
  │                                   first_only / last_only / initials_only /
  │                                   *_partial / unrelated
  ├─ identity_match_score()        -- pattern score, SEVERELY penalized when
  │                                   the domain is a free-mail provider AND
  │                                   the pattern is weak (bare first/last/
  │                                   initials/partial/unrelated)
  ├─ domain_match_score()          -- 1.0 exact company-domain match, 0.1 free
  │                                   provider, 0.0 disposable, 0.05 unrelated
  │                                   domain, 0.3 "no company_domain known"
  ├─ source_quality_score()        -- how many independent generators agreed
  └─ rejection_reason()            -- hard-rejects: disposable_domain,
                                       role_account, firstname_only_free_email,
                                       surname_only_free_email,
                                       initials_only_free_email,
                                       weak_identity_match, unrelated_domain,
                                       invalid_mx, smtp_failed, syntax_invalid
        │
        ▼
score_candidate()  [REWRITTEN]  = identity_match.overall_confidence_score(
    identity=0.40, domain=0.30, deliverability=0.20, source=0.10  -- weighted
)
        │
        ▼
is_usable()  [EXTENDED]  -- False if rejection_reason is set, in addition to
                             the existing MX_DEAD/SMTP_NOT_EXISTS checks
        │
        ▼
confidence_label()  -- "high"/"medium"/"low"/"none" ("none" whenever rejected)
```

`EMAIL_STATUS_NO_RELIABLE_EMAIL` ("no_reliable_email") is a new Lead
`email_status`, distinct from `"not_found"`: candidates WERE generated, but
every single one failed the identity-match gate — the exact "I'd rather
have `no_reliable_email` than invent a questionable Gmail address" outcome
requested.

## Vendored data (see `AUDIT_EMAIL_TOOLS.md` for the full 4-repo audit)

`scripts/pipeline/vendor_data/{free_domains,role_accounts,disposable_domains}.txt`
— extracted verbatim from `email-verifier` (AfterShip, MIT License; see
`email-verifier/LICENSE` + `email-verifier/vendor_src/`). 4,479 free-mail
domains, 888 role-account local-parts, 123,324 disposable domains. Data
only — the classification logic (`identity_match.py`) is an independent
Python implementation. `validate_email-master` (LGPLv3) was deliberately
**not** vendored to avoid copyleft contamination; its RFC-2822/SMTP-RCPT
technique was reimplemented independently (already present in this
pipeline's own `normalize_email()`/`NodeSMTPChecker`).

## Benchmark: 50-prospect dataset (`data/saas_ai_founders.csv`)

Real run, real (sandboxed) `NodeMXChecker` (SMTP disabled — `enable_smtp`
defaults off; the numbers below are pre-existing pipeline behavior, not new
today). Every one of this dataset's prospects is a startup founder/angel
investor, not a matching-name lookalike for the bug report's named
individuals (Stacey Davis, Kevin Abel, ...) — those exact people aren't in
this repo's dataset, so they're covered by `test_identity_match.py`'s
`TestReportedBadAddressesAreRejected` instead (all 7 named examples
verified rejected with the correct reason).

### Funnel

| Metric | Count |
|---|---|
| Total prospects | 50 |
| Prospects with ≥1 candidate generated | 50 |
| Prospects with a selected ("best") email | 50 |
| — high confidence | 0 |
| — medium confidence | 50 |
| — low confidence | 0 |
| `no_reliable_email` (candidates generated, all rejected) | 0 |
| `not_found` (no candidates at all) | 0 |

All 50 land at "medium" rather than "high" because this dataset's leads
only ever carry `company_name` (never an independently-known
`company_domain`) — `domain_match_score()` deliberately caps at 0.3 (not
1.0) when the domain being scored was itself only guessed from the company
name, not confirmed against a separately-known domain. That's intentional
conservatism, not a bug — see the code comment on
`identity_match.domain_match_score`.

### Candidate-level before/after (same run, same candidates — computed both ways)

| | Old logic (deliverability only) | New logic (+ identity match) |
|---|---|---|
| Total candidates generated | 4,100 | 4,100 |
| "Usable" | 3,830 (93%) | 1,455 (35%) |
| Excluded | 270 (7%, dead MX only) | 2,645 (65%) |

### Rejection-reason breakdown (2,645 excluded candidates)

| Reason | Count |
|---|---|
| `weak_identity_match` (bare initials / partial-name / unrelated local-part) | 1,870 |
| `invalid_mx` (real, live DNS lookup — dead domain) | 270 |
| `firstname_only_free_email` | 245 |
| `surname_only_free_email` | 245 |
| `initials_only_free_email` | 10 |
| `role_account` | 5 |

### Example decisions (first 20 prospects, selected email)

| Prospect | Selected email | Domain | Identity | Domain match | Overall | Decision |
|---|---|---|---|---|---|---|
| Adib Ahnaf | adib.ahnaf@rit.com | rit.com | 0.95 | 0.30 | 0.62 | selected (medium) |
| Alan Crane | alancrane@gmail.com | gmail.com | 0.95 | 0.10 | 0.56 | selected (medium) — full-name-on-gmail, "potentially reasonable" per spec, NOT rejected |
| Amy Bedard | amy.bedard@crunch.com | crunch.com | 0.95 | 0.30 | 0.62 | selected (medium) |
| Fred Rodriguez, MBA | fredr@gmail.com | gmail.com | 0.70 | 0.10 | 0.46 | selected (medium, lower — weaker pattern) |

Note the key distinction the bug report asked for: `alancrane@gmail.com`
(full first+last name) is treated completely differently from a bare
`davis@gmail.com` (surname only) — the former is a plausible personal
address (medium confidence, never rejected), the latter is hard-rejected
outright. This is exactly Part 8's own worked example
(`stacey.davis@gmail.com`: "potentially reasonable" vs. `davis@gmail.com`:
"weak").

### Rejected-candidate examples (real output from this run)

| Prospect | Rejected candidate | Pattern | Reason |
|---|---|---|---|
| Adib Ahnaf | adib@gmail.com | first_only | firstname_only_free_email |
| Adib Ahnaf | ahnaf@gmail.com | last_only | surname_only_free_email |
| Adib Ahnaf | adib@outlook.com | first_only | firstname_only_free_email |
| Adib Ahnaf | adibh@gmail.com | first_partial | weak_identity_match |
| Adib Ahnaf | ahnafa@gmail.com | last_partial | weak_identity_match |
| Adib Ahnaf | adib.ahnaf@rit.co | full_first_last | invalid_mx (live DNS: rit.co has no MX) |

## What's still open

- No live web-search-based "found in the wild" candidate source is wired
  in (see `AUDIT_EMAIL_TOOLS.md`'s note on `Email-Finder-main`'s SearXNG
  technique) — `SOURCE_TIER_SCRAPED`/`SOURCE_TIER_HUMAN_VERIFIED` are
  reserved in `identity_match.py` for when one is.
- No dedicated prospect-detail UI was built to show the full
  identity/domain/source breakdown per candidate (the backend
  `GET /api/prospects/{lead_id}` endpoint already returns it via the
  extended `EmailCandidateOut` schema); only a compact confidence badge
  was added to the main prospects table + CSV export columns.
- SMTP catch-all detection (email-verifier's random-local-part probe
  technique, audited in `AUDIT_EMAIL_TOOLS.md`) is not implemented in the
  Node SMTP checker itself — `SMTP_CATCH_ALL` is already a recognized
  status and already scored as weak-not-confirming evidence, but nothing
  populates it via an actual catch-all probe yet.
