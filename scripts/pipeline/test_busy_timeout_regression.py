"""Regression test for a live crash: a second LeadStore connection (e.g. a
concurrent API request, opened while a background job's LeadStore holds a
write lock) used to raise `sqlite3.OperationalError: database is locked`
immediately instead of waiting -- because LeadStore.__init__ never set a
busy_timeout.

Rather than a timing-based simulation (inherently flaky, and Python's
sqlite3 default connect `timeout` of 5.0s can mask a missing explicit
PRAGMA busy_timeout for any contention shorter than that), this asserts
directly on the actual fix: every LeadStore connection must have a
busy_timeout configured well above SQLite's zero default, so lock
contention from a concurrent request/job retries instead of failing
immediately. A second test still exercises the real concurrent-access
shape end-to-end (write transaction held open while a second connection
reads) as a behavioral sanity check.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from .lead_store import LeadStore
from .models import Lead


class BusyTimeoutRegressionTests(unittest.TestCase):
    def test_busy_timeout_pragma_is_set_on_every_connection(self):
        """Direct, deterministic check of the fix itself: LeadStore must
        configure a real busy_timeout (not SQLite's default of 0, which
        means "fail immediately on any lock contention"). This is what
        actually prevents the "database is locked" crash -- see
        lead_store.py's LeadStore.__init__."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "busy_timeout.db"
            store = LeadStore(db_path)
            try:
                busy_timeout_ms = store._conn.execute("PRAGMA busy_timeout;").fetchone()[0]
                self.assertGreaterEqual(
                    busy_timeout_ms,
                    10000,
                    "LeadStore connections must set a busy_timeout of at least 10s so "
                    "concurrent API requests retry through brief write contention "
                    "instead of raising 'database is locked' immediately.",
                )
            finally:
                store.close()

    def test_second_connection_survives_a_concurrent_write_transaction(self):
        """End-to-end sanity check of the same fix: a second connection
        opened while a write transaction is held open on another
        connection must not raise, and must see the last committed state."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "concurrent.db"

            writer = LeadStore(db_path)
            writer.upsert_lead(Lead(full_name="Jane Doe", linkedin_url="https://www.linkedin.com/in/jane"))

            started_write = threading.Event()
            release_write = threading.Event()

            def hold_write_transaction():
                writer._conn.execute("BEGIN IMMEDIATE;")
                writer._conn.execute(
                    "UPDATE leads SET full_name = ? WHERE linkedin_url = ?",
                    ("Jane Doe Updated", "https://www.linkedin.com/in/jane"),
                )
                started_write.set()
                release_write.wait(timeout=5)
                writer._conn.commit()

            t = threading.Thread(target=hold_write_transaction)
            t.start()
            self.assertTrue(started_write.wait(timeout=2), "writer never started its transaction")

            try:
                # This is the exact call that crashed live: opening a
                # brand-new LeadStore (as
                # backend/app/db/database.get_store() does on every API
                # request) while another connection holds a write lock.
                reader = LeadStore(db_path)
                try:
                    leads = reader.all()
                    self.assertEqual(len(leads), 1)
                finally:
                    reader.close()
            finally:
                release_write.set()
                t.join(timeout=5)
                writer.close()


if __name__ == "__main__":
    unittest.main()
