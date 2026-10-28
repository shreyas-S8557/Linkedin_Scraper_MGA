# email-finder-main (vendored)

Upstream: https://github.com/hiteshwadhwani/email-finder (`module
github.com/hiteshwadhwani/email-finder.git`, per `vendor_src/go.mod`). No
LICENSE file was included in the upstream checkpoint supplied alongside
this repo; `vendor_src/` is kept here verbatim, unmodified, for provenance
only, the same way `Mailfoguess/vendor_src/` and `Email_Finder/` (this
repo's other vendored guessers) are kept.

This is a small Go CLI (`pkg/input/input.go` + `pkg/checkEmail/checkEmail.go`)
that:

1. asks for first name, last name, company name,
2. builds 5 candidate local-parts (`first+last`, `f+last`, `l+first`,
   `f+l`, `first.last`) against a domain built as `{company}.in`
   (hardcoded — upstream is written for `.in`-domain companies), and
3. "validates" each by dialing the domain's MX host directly on port 25
   and issuing a raw `RCPT TO` (no `verify_smtp.js`/holehe involved — its
   own from-scratch SMTP probe).

## What's actually wired into the pipeline

`scripts/pipeline/email_discovery.py`'s `EmailFinderMainGenerator` is the
**FALLBACK** generator (see that module's docstring): it's only invoked by
`generate_candidates_for_lead` when the primary (`ScrapegraphPatternGenerator`)
and supplementary (`MailfoguessGenerator`) generators together produce zero
candidates for a lead. It soft-imports `email_finder_main` and calls
`find(first_name=, last_name=, company_name=, company_domain=)`.

`scripts/email_finder_main/__init__.py` re-implements the pattern-generation
half of the algorithm above in pure Python (so it's live without a Go
toolchain — this pipeline is Python end to end, and shelling out to a
compiled binary as a fallback-only generator would be a heavy, fragile
dependency for a handful of extra candidates). Two deliberate changes from
upstream:

* **Domain resolution isn't hardcoded to `.in`.** It uses the lead's real
  `company_domain` when known; only falls back to guessing a domain from
  `company_name` (with the same handful of common TLDs the primary
  generator already tries) when no domain is known at all — upstream's
  `{company}.in` assumption would silently produce wrong-TLD candidates for
  the vast majority of this pipeline's (US-centric SaaS/CPA/angel) leads.
* **No SMTP probing inside the generator.** `checkEmail.go`'s raw
  `RCPT TO` dial duplicates work this pipeline already does centrally,
  uniformly, and more robustly via `NodeMXChecker`/`NodeSMTPChecker` in
  `email_discovery.py` (batched, timeout-guarded, degrades to
  `MX_UNKNOWN`/`SMTP_NOT_CHECKED` instead of raising) — every generator
  (primary, supplementary, fallback) is expected to only *generate*
  candidates and let that shared layer validate them, exactly as
  `Mailfoguess/README.md` explains for the Mailfoguess integration.
