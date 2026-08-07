#!/usr/bin/env python3
"""Memory Hub benchmark — insert, search, profile, and conflict detection timing."""

import sqlite3
import time
import tempfile
import os
import sys


def benchmark():
    # Create a temporary database
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="benchmark_")
    os.close(fd)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            source TEXT DEFAULT '',
            created_at REAL DEFAULT (julianday('now')),
            updated_at REAL DEFAULT (julianday('now'))
        )
    """)
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS insights_fts USING fts5(text, content=insights, content_rowid=id)")
    conn.commit()

    # --- Insert 100 insights ---
    start = time.perf_counter()
    for i in range(100):
        conn.execute(
            "INSERT INTO insights (text, source) VALUES (?, ?)",
            (f"Insight number {i}: The quick brown fox jumps over the lazy dog. Benchmark iteration.", "benchmark"),
        )
    conn.commit()
    insert_elapsed = time.perf_counter() - start

    # --- FTS5 search ---
    start = time.perf_counter()
    for _ in range(100):
        conn.execute(
            "SELECT rowid, text FROM insights_fts WHERE insights_fts MATCH ? LIMIT 10",
            ("quick brown fox",),
        ).fetchall()
    search_elapsed = time.perf_counter() - start

    # --- Profile query ---
    start = time.perf_counter()
    for _ in range(100):
        conn.execute(
            "SELECT source, COUNT(*) as cnt FROM insights GROUP BY source ORDER BY cnt DESC"
        ).fetchall()
    profile_elapsed = time.perf_counter() - start

    # --- Conflict detection ---
    # Simulate: update same row from two cursors, measure detection time
    conn.execute("INSERT INTO insights (text, source) VALUES (?, ?)", ("conflict-test", "benchmark"))
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn2 = sqlite3.connect(db_path)
    start = time.perf_counter()
    for i in range(100):
        # Cursor A reads
        cur_a = conn.cursor()
        cur_a.execute("SELECT updated_at FROM insights WHERE id = ?", (row_id,))
        ts_a = cur_a.fetchone()[0]

        # Cursor B reads
        cur_b = conn2.cursor()
        cur_b.execute("SELECT updated_at FROM insights WHERE id = ?", (row_id,))
        ts_b = cur_b.fetchone()[0]

        # Cursor A writes
        cur_a.execute("UPDATE insights SET text = ?, updated_at = julianday('now') WHERE id = ? AND updated_at = ?",
                      (f"updated-A-{i}", row_id, ts_a))
        conn.commit()

        # Cursor B writes (should detect mismatch)
        cur_b.execute("UPDATE insights SET text = ?, updated_at = julianday('now') WHERE id = ? AND updated_at = ?",
                      (f"updated-B-{i}", row_id, ts_b))
        conn2.commit()
    conflict_elapsed = time.perf_counter() - start

    conn2.close()
    conn.close()
    os.unlink(db_path)

    # --- Report ---
    print("## Benchmark Results\n")
    print("| Metric               | 100 ops total | Per op      |")
    print("|----------------------|---------------|-------------|")
    print(f"| Insert (100 rows)    | {insert_elapsed*1000:>8.2f} ms | {insert_elapsed*10:>6.2f} ms |")
    print(f"| FTS5 search (x100)   | {search_elapsed*1000:>8.2f} ms | {search_elapsed*10:>6.2f} ms |")
    print(f"| Profile query (x100) | {profile_elapsed*1000:>8.2f} ms | {profile_elapsed*10:>6.2f} ms |")
    print(f"| Conflict detect (x100)| {conflict_elapsed*1000:>7.2f} ms | {conflict_elapsed*10:>6.2f} ms |")

    return 0


if __name__ == "__main__":
    sys.exit(benchmark())
