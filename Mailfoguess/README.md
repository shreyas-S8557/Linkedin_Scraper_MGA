# Mailfoguess (vendored)

Upstream: https://github.com/WildSiphon/Mailfoguess (GPLv3 — see `LICENSE`).

This directory vendors the upstream source **for provenance only**
(`vendor_src/`, copied verbatim from the `Mailfoguess-main` checkpoint
supplied alongside this repo). It is not imported directly and is not on
`sys.path` — it exists so the origin of the local-part generation logic is
auditable in one place, the same way `Email_Finder/` keeps the vendored
pattern-guesser it was adapted from.

The pipeline's actual, importable integration lives at
`scripts/mailfoguess/` (see `scripts/pipeline/email_discovery.py`'s
`MailfoguessGenerator`, which does a soft `import mailfoguess` and calls
`mailfoguess.find_candidates(first_name=..., last_name=...,
company_domain=...)`). That package re-implements
`vendor_src/modules/user.py` and `vendor_src/modules/localpart_generator.py`
verbatim (the two dependency-free modules that do the actual username
guessing) and adds a thin adapter on top — it deliberately does **not**
pull in `vendor_src/modules/email_generator.py`, `guesser.py`, or the
upstream `holehe`/`trio`/`httpx` dependencies, because:

- This pipeline already has its own MX/SMTP validation layer
  (`NodeMXChecker` / `NodeSMTPChecker` in `email_discovery.py`), so
  Mailfoguess's own `--verify` step (which shells out to holehe against
  gmail/yahoo/laposte/protonmail account-recovery flows) would be a
  redundant and much heavier dependency for the same job.
- Keeping the guesser dependency-free matches this repo's existing
  convention (`Email_Finder/email_finder.py` is also pure stdlib) and keeps
  `MailfoguessGenerator.available` truthful without requiring anyone to
  `pip install` a large extra stack just to get local-part guesses.

## What it guesses

Mailfoguess's local-part permutation engine (first/last/middle name +
initials + separators, at generation levels `min`/`low`/`high`/`max`) is
domain-agnostic upstream — the original tool only ever paired it with a
fixed list of free-mail providers (gmail, yahoo, outlook, ...), since it's
built for OSINT lookups of a person's personal inbox rather than a company
inbox.

`scripts/mailfoguess/__init__.py` guesses **both**:

1. candidates at the lead's actual **company domain** (the useful case for
   this pipeline — a B2B prospect's work email), and
2. candidates at a short list of common **free-mail providers** (gmail.com,
   outlook.com, hotmail.com, yahoo.com, icloud.com), since some prospects
   (solo founders, angels) list a personal address as their contact email.

Company-domain guesses are scored higher than free-mail guesses by default
(`MailfoguessGenerator` in `email_discovery.py`) — a free-mail candidate is
only preferred over a company-domain one if it actually gets SMTP-confirmed.
