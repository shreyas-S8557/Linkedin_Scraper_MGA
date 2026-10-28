"""Tests for:
  1. ddgs_search()'s transient-error retry -- a timeout/connection-reset
     error on one backend should retry once before falling through to the
     next backend, but "genuinely zero results" must not be retried.
  2. DISCOVERY_BACKENDS includes a third fallback ("brave") only ever
     reached after yahoo AND duckduckgo both fail.
  3. query_generator's location-less broadening tier -- the fix for
     non-US campaigns (e.g. locations=["India"]) getting far fewer
     queries than a US campaign, since the state/city expansion tiers are
     US-only.

Run with (from `scripts/`):
    python -m unittest pipeline.test_ddgs_resilience -v
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from ddgs.exceptions import DDGSException, TimeoutException

from .query_generator import build_queries
from .sources import ddgs_search as ddgs_module
from .target_config import TargetConfig


class DiscoveryBackendsTests(unittest.TestCase):
    def test_three_backends_configured_brave_last(self):
        self.assertEqual(ddgs_module.DISCOVERY_BACKENDS, ("yahoo", "duckduckgo", "brave"))


class IsTransientErrorTests(unittest.TestCase):
    def test_timeout_exception_is_transient(self):
        self.assertTrue(ddgs_module._is_transient_ddgs_error(TimeoutException("Request timed out")))

    def test_no_results_message_is_not_transient(self):
        self.assertFalse(ddgs_module._is_transient_ddgs_error(DDGSException("No results found.")))

    def test_remote_protocol_error_message_is_transient(self):
        self.assertTrue(
            ddgs_module._is_transient_ddgs_error(
                DDGSException("RemoteProtocolError: RemoteProtocolError('Server disconnected')")
            )
        )

    def test_connect_timeout_message_is_transient(self):
        self.assertTrue(
            ddgs_module._is_transient_ddgs_error(
                TimeoutException("Request timed out: ConnectTimeout('timed out')")
            )
        )


class _FakeDDGS:
    """Scripted fake for ddgs.DDGS: `responses` is a list of
    (exception_or_None, hits) consumed in call order, one per `.text()`
    call, regardless of which backend/attempt it's for."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def text(self, query, *, backend, max_results):
        self.calls.append(backend)
        if not self._responses:
            return []
        exc, hits = self._responses.pop(0)
        if exc is not None:
            raise exc
        return hits


def _hit(slug="jane-doe"):
    return {
        "href": f"https://www.linkedin.com/in/{slug}",
        "title": "Jane Doe - Health Coach",
        "body": "Health coach in San Francisco, California, United States.",
    }


class DdgsSearchRetryTests(unittest.TestCase):
    def test_transient_error_is_retried_on_same_backend_then_succeeds(self):
        fake = _FakeDDGS(
            [
                (TimeoutException("Request timed out"), []),
                (None, [_hit()]),
            ]
        )
        with patch.object(ddgs_module, "DISCOVERY_BACKENDS", ("yahoo",)), \
             patch("ddgs.DDGS", return_value=fake), \
             patch.object(time, "sleep", return_value=None):
            results = ddgs_module.ddgs_search("health coach LinkedIn", 10)
        self.assertEqual(len(results), 1)
        # Both calls were against the same backend -- a retry, not a
        # fall-through to a different backend.
        self.assertEqual(fake.calls, ["yahoo", "yahoo"])

    def test_no_results_error_is_not_retried_falls_through_to_next_backend(self):
        fake = _FakeDDGS(
            [
                (DDGSException("No results found."), []),
                (None, [_hit()]),
            ]
        )
        with patch.object(ddgs_module, "DISCOVERY_BACKENDS", ("yahoo", "duckduckgo")), \
             patch("ddgs.DDGS", return_value=fake), \
             patch.object(time, "sleep", return_value=None):
            results = ddgs_module.ddgs_search("health coach LinkedIn", 10)
        self.assertEqual(len(results), 1)
        # No retry on yahoo (no_results is not transient) -- straight to
        # the next backend.
        self.assertEqual(fake.calls, ["yahoo", "duckduckgo"])

    def test_third_backend_only_reached_after_first_two_fail(self):
        fake = _FakeDDGS(
            [
                (DDGSException("No results found."), []),
                (DDGSException("No results found."), []),
                (None, [_hit()]),
            ]
        )
        with patch("ddgs.DDGS", return_value=fake), patch.object(time, "sleep", return_value=None):
            results = ddgs_module.ddgs_search("health coach LinkedIn", 10)
        self.assertEqual(len(results), 1)
        self.assertEqual(fake.calls, ["yahoo", "duckduckgo", "brave"])

    def test_genuinely_zero_results_across_all_backends_returns_empty(self):
        fake = _FakeDDGS(
            [
                (DDGSException("No results found."), []),
                (DDGSException("No results found."), []),
                (DDGSException("No results found."), []),
            ]
        )
        with patch("ddgs.DDGS", return_value=fake), patch.object(time, "sleep", return_value=None):
            results = ddgs_module.ddgs_search("health coach LinkedIn", 10)
        self.assertEqual(results, [])


class NonUsLocationBroadeningTests(unittest.TestCase):
    def test_non_us_target_gets_location_less_queries(self):
        target = TargetConfig(titles=["Health Coach"], locations=["India"])
        queries = build_queries(target)
        self.assertIn("Health Coach LinkedIn", queries)

    def test_us_target_still_gets_state_city_expansion_unchanged(self):
        target = TargetConfig(titles=["Founder"], locations=["United States"])
        queries = build_queries(target)
        # State-level tier still present (unchanged behaviour).
        self.assertTrue(any("California United States LinkedIn" in q for q in queries))

    def test_non_us_target_query_count_meaningfully_larger_than_before(self):
        # Before this fix, a non-US target with a title+industry+several
        # keywords produced well under 40 total queries -- nowhere near
        # enough volume to reliably fill a double-digit target_count. The
        # location-less broadening tier (1b) roughly doubles that; this
        # asserts it landed meaningfully above the pre-fix baseline (~19
        # queries for this exact config) without hard-coding an exact
        # count that would break on unrelated query-generator tweaks.
        target = TargetConfig(
            titles=["Nutritionist", "Health Coach"],
            industries=["Health and Fitness"],
            keywords=["wellness", "coach", "nutrition"],
            locations=["India"],
        )
        queries = build_queries(target)
        self.assertGreater(len(queries), 30)


if __name__ == "__main__":
    unittest.main()
