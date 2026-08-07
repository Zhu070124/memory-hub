"""
Unit tests for Memory Hub core operations.

Covers: add, query, search, confirm, stale, conflict resolution, stats.

Run with:
    python -m pytest tests/test_insights.py -v
Or (no deps):
    python tests/test_insights.py
"""

import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta

# Make hub.py importable from the tests/ directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Override DB_PATH and HUB_DIR before importing hub
# Use a temporary database so we never touch the real one.
TEST_DIR = Path(tempfile.mkdtemp(prefix="memory-hub-test-"))
TEST_DB = TEST_DIR / "test_insights.db"

import hub

# Monkey-patch globals to isolate tests
hub.DB_PATH = TEST_DB
hub.HUB_DIR = TEST_DIR
hub.LOG_PATH = TEST_DIR / "test_memory_hub.log"

# Re-init logging to the test path
import logging
for handler in hub.logger.handlers[:]:
    hub.logger.removeHandler(handler)
hub.logger.addHandler(logging.FileHandler(str(hub.LOG_PATH), encoding="utf-8"))
hub.logger.setLevel(logging.WARNING)  # quiet during tests


class TestInsights(unittest.TestCase):
    """Core CRUD + search + lifecycle tests."""

    @classmethod
    def setUpClass(cls):
        hub.init_db()

    @classmethod
    def tearDownClass(cls):
        # Clean up temp files
        if TEST_DB.exists():
            TEST_DB.unlink()
        for f in TEST_DIR.glob("test_memory_hub*"):
            try:
                f.unlink()
            except OSError:
                pass
        archive = TEST_DIR / "archive.jsonl"
        if archive.exists():
            archive.unlink()

    def setUp(self):
        """Clear insights table before each test."""
        db = hub.get_db()
        db.execute("DELETE FROM insights")
        db.commit()
        db.close()

    # ------------------------------------------------------------------
    # Add
    # ------------------------------------------------------------------
    def test_add_insight(self):
        fid, conflict = hub.add_insight(
            "Test insight content", "test-agent", lens="tech",
            priority="P1", confidence="observed",
        )
        self.assertIsNotNone(fid)
        self.assertIsNone(conflict)
        self.assertGreater(fid, 0)

    def test_add_insight_with_tags(self):
        fid, conflict = hub.add_insight(
            "Tagged content", "puff", lens="writing",
            priority="P0", confidence="confirmed", tags="novel,style",
        )
        self.assertIsNotNone(fid)
        self.assertIsNone(conflict)

    def test_add_insight_defaults(self):
        fid, conflict = hub.add_insight("Minimal insight", "hermes")
        self.assertIsNotNone(fid)
        self.assertIsNone(conflict)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def test_query_all(self):
        hub.add_insight("A", "src1", lens="general", confidence="observed",
                        priority="P1")
        hub.add_insight("B", "src2", lens="tech", confidence="speculative",
                        priority="P2")
        results = hub.query_profile()
        self.assertEqual(len(results), 2)

    def test_query_by_lens(self):
        hub.add_insight("Writing insight", "a", lens="writing")
        hub.add_insight("Tech insight", "a", lens="tech")
        results = hub.query_profile(lens="writing")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["lens"], "writing")

    def test_query_by_source(self):
        hub.add_insight("C", "agent-a", lens="general")
        hub.add_insight("D", "agent-b", lens="general")
        results = hub.query_profile(source="agent-a")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "agent-a")

    def test_query_excludes_stale_by_default(self):
        fid, _ = hub.add_insight("Active", "a", lens="general")
        hub.mark_stale(fid)
        results = hub.query_profile()
        self.assertEqual(len(results), 0)

    def test_query_include_stale(self):
        fid, _ = hub.add_insight("Stale item", "a", lens="general")
        hub.mark_stale(fid)
        results = hub.query_profile(include_stale=True)
        self.assertEqual(len(results), 1)

    def test_query_limit(self):
        for i in range(10):
            hub.add_insight(f"Insight {i}", "a", lens="general")
        results = hub.query_profile(limit=5)
        self.assertEqual(len(results), 5)

    # ------------------------------------------------------------------
    # Search (FTS5)
    # ------------------------------------------------------------------
    def test_search_finds_content(self):
        hub.add_insight("User prefers minimal prose style", "a", lens="writing")
        hub.add_insight("User tech stack: Python, SQLite", "a", lens="tech")
        results = hub.search_insights("minimal prose")
        self.assertEqual(len(results), 1)
        self.assertIn("minimal prose", results[0]["content"])

    def test_search_chinese(self):
        hub.add_insight("用户偏好极简风格", "a", lens="writing")
        results = hub.search_insights("极简")
        self.assertGreaterEqual(len(results), 1)

    def test_search_with_lens_filter(self):
        hub.add_insight("Minimal style", "a", lens="writing")
        hub.add_insight("Minimal tech", "a", lens="tech")
        results = hub.search_insights("Minimal", lens="writing")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["lens"], "writing")

    def test_search_no_results(self):
        results = hub.search_insights("nonexistent_term_xyz")
        self.assertEqual(len(results), 0)

    # ------------------------------------------------------------------
    # Confirm
    # ------------------------------------------------------------------
    def test_confirm_insight(self):
        fid, _ = hub.add_insight("To confirm", "a", lens="general",
                                  confidence="observed")
        ok = hub.confirm_insight(fid)
        self.assertTrue(ok)
        # Verify confidence upgraded
        db = hub.get_db()
        row = db.execute("SELECT confidence FROM insights WHERE id = ?",
                         (fid,)).fetchone()
        db.close()
        self.assertEqual(row["confidence"], "confirmed")

    def test_confirm_nonexistent(self):
        ok = hub.confirm_insight(99999)
        self.assertFalse(ok)

    # ------------------------------------------------------------------
    # Stale
    # ------------------------------------------------------------------
    def test_mark_stale(self):
        fid, _ = hub.add_insight("Stale me", "a", lens="general")
        ok = hub.mark_stale(fid)
        self.assertTrue(ok)
        db = hub.get_db()
        row = db.execute("SELECT stale FROM insights WHERE id = ?",
                         (fid,)).fetchone()
        db.close()
        self.assertEqual(row["stale"], 1)

    def test_mark_stale_nonexistent(self):
        ok = hub.mark_stale(99999)
        self.assertFalse(ok)

    # ------------------------------------------------------------------
    # Archive
    # ------------------------------------------------------------------
    def test_archive_stale(self):
        fid, _ = hub.add_insight("To archive", "a", lens="general")
        hub.mark_stale(fid)
        count = hub.archive_stale()
        self.assertEqual(count, 1)
        # Verify removed from DB
        results = hub.query_profile(include_stale=True)
        self.assertEqual(len(results), 0)

    def test_archive_empty(self):
        count = hub.archive_stale()
        self.assertEqual(count, 0)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    def test_get_stats(self):
        hub.add_insight("Stats 1", "agent-a", lens="writing",
                        confidence="observed", priority="P1")
        hub.add_insight("Stats 2", "agent-b", lens="tech",
                        confidence="confirmed", priority="P0")
        stats = hub.get_stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["active"], 2)
        self.assertEqual(stats["stale"], 0)
        self.assertEqual(len(stats["sources"]), 2)
        source_names = [s["name"] for s in stats["sources"]]
        self.assertIn("agent-a", source_names)
        self.assertIn("agent-b", source_names)

    # ------------------------------------------------------------------
    # Conflict resolution
    # ------------------------------------------------------------------
    def test_conflict_higher_confidence_wins(self):
        """When a new insight matches an existing one with lower confidence,
        the existing gets marked stale and the new one is inserted."""
        # Existing: speculative
        fid1, c1 = hub.add_insight(
            "User prefers minimal prose and clean architecture patterns",
            "agent-a", lens="writing", confidence="speculative",
        )
        self.assertIsNone(c1)

        # New: confirmed, similar content
        fid2, c2 = hub.add_insight(
            "User prefers minimal prose and clean architecture",
            "agent-b", lens="writing", confidence="confirmed",
        )
        # New should win because its confidence is higher
        self.assertIsNotNone(fid2)
        # The existing one should be marked stale
        db = hub.get_db()
        row = db.execute("SELECT stale FROM insights WHERE id = ?",
                         (fid1,)).fetchone()
        db.close()
        self.assertEqual(row["stale"], 1)

    def test_conflict_lower_confidence_suppressed(self):
        """When a new insight matches an existing one with higher confidence,
        the new one is suppressed (not inserted)."""
        # Existing: confirmed
        fid1, _ = hub.add_insight(
            "User strongly prefers short feedback loops and rapid iteration cycles",
            "agent-a", lens="tech", confidence="confirmed",
        )

        # New: speculative, nearly identical content (>= 70% token overlap)
        fid2, c2 = hub.add_insight(
            "User prefers short feedback loops and rapid iteration cycles",
            "agent-b", lens="tech", confidence="speculative",
        )
        self.assertIsNone(fid2)  # suppressed
        self.assertIsNotNone(c2)
        self.assertEqual(c2["conflict"], "suppressed")
        self.assertEqual(c2["winner"], "existing")

    def test_no_conflict_different_lens(self):
        """Insights in different lenses should not conflict."""
        hub.add_insight(
            "User prefers minimal prose styles", "a",
            lens="writing", confidence="observed",
        )
        fid2, c2 = hub.add_insight(
            "User prefers minimal prose styles", "b",
            lens="tech",  # different lens
            confidence="speculative",
        )
        self.assertIsNotNone(fid2)
        self.assertIsNone(c2)

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------
    def test_sync_since(self):
        import time
        base_time = datetime.now().isoformat()
        time.sleep(0.01)  # ensure timestamps are strictly after base_time
        hub.add_insight("Sync item 1", "a", lens="general")
        hub.add_insight("Sync item 2", "a", lens="general")
        results = hub.sync_since(base_time)
        self.assertEqual(len(results), 2)

    def test_sync_since_no_results(self):
        future = (datetime.now() + timedelta(days=1)).isoformat()
        results = hub.sync_since(future)
        self.assertEqual(len(results), 0)

    def test_sync_since_with_source_filter(self):
        import time
        base_time = datetime.now().isoformat()
        time.sleep(0.01)  # ensure timestamps are strictly after base_time
        hub.add_insight("Sync from hermes", "hermes", lens="general")
        hub.add_insight("Sync from claude", "claude-code", lens="general")
        results = hub.sync_since(base_time, source="hermes")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "hermes")


class TestAutoCleanup(unittest.TestCase):
    """Test the auto-cleanup background task logic."""

    @classmethod
    def setUpClass(cls):
        hub.init_db()

    def setUp(self):
        db = hub.get_db()
        db.execute("DELETE FROM insights")
        db.commit()
        db.close()

    def test_cleanup_stale_p2_insights(self):
        """P2 insights older than 30 days should be marked stale."""
        # Directly insert an old P2 insight via SQL to control the timestamp
        db = hub.get_db()
        old_date = (datetime.now() - timedelta(days=60)).isoformat()
        db.execute(
            "INSERT INTO insights (content, source, lens, priority, confidence, "
            "created_at, updated_at, stale) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            ("Old P2 insight", "test", "general", "P2", "observed",
             old_date, old_date),
        )
        db.commit()
        db.close()

        # Run cleanup
        hub._run_cleanup()

        # Verify the old P2 insight is now stale
        db = hub.get_db()
        row = db.execute(
            "SELECT stale FROM insights WHERE content = 'Old P2 insight'"
        ).fetchone()
        db.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["stale"], 1)

    def test_cleanup_does_not_touch_p1(self):
        """P1 insights older than 30 days should NOT be marked stale."""
        db = hub.get_db()
        old_date = (datetime.now() - timedelta(days=60)).isoformat()
        db.execute(
            "INSERT INTO insights (content, source, lens, priority, confidence, "
            "created_at, updated_at, stale) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            ("Old P1 insight", "test", "general", "P1", "observed",
             old_date, old_date),
        )
        db.commit()
        db.close()

        hub._run_cleanup()

        db = hub.get_db()
        row = db.execute(
            "SELECT stale FROM insights WHERE content = 'Old P1 insight'"
        ).fetchone()
        db.close()
        self.assertEqual(row["stale"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
