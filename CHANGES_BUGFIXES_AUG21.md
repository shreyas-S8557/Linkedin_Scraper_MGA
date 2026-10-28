# Bug fixes (Aug 21 2026 follow-up)

Two real bugs found from your logs/screenshots, both fixed and verified.

## 1. "'int' object has no attribute 'industries'" — discovery job failing

**Root cause:** `scripts/pipeline/orchestrator.py`'s `run_pipeline()` builds
the real campaign config into a variable called `target_config`, then
immediately creates a *second*, differently-typed local variable also
called `target = target_config.target_count` (a plain int, used everywhere
else in that function purely as a stop-count). Every phase function in the
file (`run_ddgs_phase`, `run_webclaw_phase`, etc.) expects that int — except
`run_posts_phase` (the opt-in "search LinkedIn posts" source you had
enabled), which is the one phase that needs the *full* `TargetConfig`
object internally (it reads `target.target_count`, `target.career_stage`,
and calls `build_post_queries(target)`, which reads `target.industries`).

The call site was passing the int (`target=target`) into `run_posts_phase`,
which passed it straight into `build_post_queries()` → `_qualifiers()` →
`target.industries` → crash, the instant the posts phase started.

**Fix:** that one call now passes `target=target_config` (the real object)
instead of `target=target` (the int). Verified this reproduces the exact
error on the old code and is fixed on the new code (see test below) — no
other phase call needed to change, since they genuinely do want the int.

## 2. Contaminated/glued multi-person profiles slipping through, causing GENERATION_FAILED

Your screenshots showed prospects like *"Donna Fantel - Quality
Improvement Coach at Rhode Island...Donna Lopez - Bodi Health & Wellness
Coach"* and *"Catherine "Katie" Cardina...Ashley Carpenter...Autumn
Phillips..."* — clearly two or three different people's search-result
snippets glued into one row. There's already a contamination-detection
system in `quality.py` meant to catch and reject exactly this, but its
core signal (`_NAME_TITLE_PREFIX_RE`) only recognized a new person starting
at the very beginning of the text or right after a `|` character. Real
Yahoo/DDG results frequently glue a second hit on using `...` (a truncation
ellipsis) instead, which that regex never matched — so this specific,
common shape of contamination sailed straight through undetected, ending
up as "qualified" leads with garbage/mixed evidence. That garbage evidence
is exactly why `assess_email_quality()`'s check #5 ("does the draft reflect
this lead's actual evidence?") kept failing and dumping leads into
GENERATION_FAILED — the lead's stored evidence was itself contaminated
nonsense.

**Fix**, in `scripts/pipeline/quality.py`:
- Widened `_NAME_TITLE_PREFIX_RE`'s boundary group to also accept `...`/`…`,
  not just start-of-string/`|`.
- Added a second, complementary signal (`_ELLIPSIS_NAME_RE`) that catches
  the common case where a second person's name is followed by credentials
  before the dash (e.g. `"...Ashley Carpenter, RDN, CDN, IFNCP - Buffalo
  Nutrition"`), which the first signal still can't reach even with the
  boundary fix.
- Updated the decontamination boundary-finder to use the same new signal,
  so a contaminated hit gets truncated back to just the first (legitimate)
  person's data and salvaged where possible, instead of always being
  discarded outright.

Verified against the exact five contaminated examples visible in your
screenshots — all five are now correctly flagged and either salvaged
(truncated to the real first person) or rejected, and three clean
single-person control examples are confirmed to still pass through
untouched (no new false positives).

## 3. Raw search-result junk (`| LinkedIn`, `...`, dangling `-`) leaking into generated email text

**What you reported:** emails like *"In Choose Health With Heather | LinkedIn
roles..."* and *"In Health Coach and Owner at Georgia ... - roles..."* —
literal search-result-page chrome and truncation marks showing up in live
email copy.

**Root cause:** `scripts/pipeline/email_generation.py`'s `clean_job_title()`
only ever stripped a leading `"{Name} - "` prefix and a trailing `"at/of
{company}"` clause. The underlying `job_title` field itself still carries
whatever raw junk was in the original search-result title after that
(`"| LinkedIn"` site chrome, a truncation `"..."`, a dangling leftover
`"-"`), and none of that was ever being removed before the cleaned title
got spliced into the email body.

**Fix:**
- Strip trailing `"| LinkedIn"`, trailing `...`/`…` truncation marks, and
  any dangling `-` left over once those are gone — applied repeatedly,
  since a raw title can stack more than one of these.
- Do this stripping *before* the existing "at/of company" suffix check,
  so junk sitting after the company clause doesn't block that check from
  firing.
- If the cleaned title ends up being nothing but the company's own name
  (a common shape for a self-titled small business, e.g. `"{Name} -
  Choose Health With Heather | LinkedIn"` with no real title in between),
  drop it entirely — using it as a "role" would draft nonsense like *"In
  Choose Health With Heather roles..."*.
- If suffix-stripping the company clause leaves a dangling, incomplete
  fragment behind (e.g. `"...University"` after stripping `"of
  Pennsylvania Health System"` off `"...University of Pennsylvania
  Health System"`), drop the whole title rather than splice a broken
  fragment into an email — the system already has a safe, honest
  fallback for "no usable role evidence" (see Tyler Dickey's own email in
  your CSV, `hook_type: role_only` with the role actually present, vs.
  what Heather/Chris's rows now correctly fall back to).

Verified directly against your uploaded CSV (`compose_personalization()`
run on all 5 real leads): Tyler and Andrea's roles clean up correctly,
Molly's ellipsis is gone, and Heather/Chris — which had nothing usable
left after removing the junk — now correctly produce an empty role
instead of a broken fragment, so the email generator will fall back to a
different hook rather than repeat the bug.

## Testing

- Full pipeline suite: 532/533 passing (the one failure is the same
  pre-existing, unrelated test-isolation issue from before — fails
  identically on unmodified code).
- Backend suite: 71/71 passing.
- Both bugs reproduced against the *old* code and confirmed fixed against
  the *new* code with standalone repro scripts (no live network needed for
  either — the orchestrator bug is a pure Python AttributeError, and the
  contamination bug is pure regex/string logic).

## Files changed

- `scripts/pipeline/orchestrator.py`
- `scripts/pipeline/quality.py`
- `scripts/pipeline/email_generation.py`
