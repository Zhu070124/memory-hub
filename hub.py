"""
Memory Hub -- Cross-agent curated insight library
==================================================
SQLite + FTS5, RESTful API (stdlib only), zero pip deps.

Usage:
  python hub.py serve [port]   -- start HTTP server (default 8921)
  python hub.py stats          -- show database stats
  python hub.py archive        -- archive stale insights
"""

import os
import sys
import signal
import json
import sqlite3
import time
import hmac
import http.server
import threading
import logging
from pathlib import Path
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HUB_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = HUB_DIR / "insights.db"
LOG_PATH = HUB_DIR / "memory_hub.log"

# ---------------------------------------------------------------------------
# Unified logging -- file output with levels
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.FileHandler(str(LOG_PATH), encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("memory-hub")

# ---------------------------------------------------------------------------
# Priority / TTL (from AI Agent Memory)
# ---------------------------------------------------------------------------
PRIORITIES = {
    "P0": {"ttl_days": None, "label": "permanent -- core portrait"},
    "P1": {"ttl_days": 90,  "label": "90 days -- active projects / recent preferences"},
    "P2": {"ttl_days": 30,  "label": "30 days -- temporary observations"},
}

# ---------------------------------------------------------------------------
# Confidence levels (from Agent Memory Hub)
# ---------------------------------------------------------------------------
CONFIDENCE = ["confirmed", "observed", "speculative"]
CONFIDENCE_RANK = {"confirmed": 3, "observed": 2, "speculative": 1}

# ---------------------------------------------------------------------------
# Database -- WAL mode + connection timeout for concurrent write safety
# ---------------------------------------------------------------------------
MAX_DB_RETRIES = 3
RETRY_BACKOFF = 0.1  # seconds, doubles each retry

def get_db():
    """
    Open a SQLite connection with WAL mode and busy timeout.
    WAL mode allows concurrent readers + one writer without table locks.
    The 10-second busy timeout gives writers time to complete before raising
    SQLITE_BUSY, and the caller's retry loop catches the remainder.
    """
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def db_retry(func, *args, **kwargs):
    """
    Execute a database operation with retry on SQLITE_BUSY / locked errors.
    Three attempts with exponential backoff: 0.1s, 0.2s, 0.4s.
    """
    last_err = None
    for attempt in range(1, MAX_DB_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError as e:
            last_err = e
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                logger.warning(
                    "DB locked (attempt %d/%d), retrying in %.2fs: %s",
                    attempt, MAX_DB_RETRIES, wait, e,
                )
                time.sleep(wait)
            else:
                raise
    raise last_err  # exhausted retries


def init_db():
    """Create tables and FTS5 index if they do not exist."""
    db = get_db()
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS insights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                lens TEXT DEFAULT 'general',
                priority TEXT DEFAULT 'P1',
                confidence TEXT DEFAULT 'observed',
                tags TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                stale INTEGER DEFAULT 0
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS insights_fts USING fts5(
                content, tags, source, lens,
                content='insights', content_rowid='id'
            );
            CREATE TRIGGER IF NOT EXISTS insights_ai AFTER INSERT ON insights BEGIN
                INSERT INTO insights_fts(rowid, content, tags, source, lens)
                VALUES (new.id, new.content, new.tags, new.source, new.lens);
            END;
            CREATE TRIGGER IF NOT EXISTS insights_ad AFTER DELETE ON insights BEGIN
                INSERT INTO insights_fts(insights_fts, rowid, content, tags, source, lens)
                VALUES ('delete', old.id, old.content, old.tags, old.source, old.lens);
            END;
            CREATE TRIGGER IF NOT EXISTS insights_au AFTER UPDATE ON insights BEGIN
                INSERT INTO insights_fts(insights_fts, rowid, content, tags, source, lens)
                VALUES ('delete', old.id, old.content, old.tags, old.source, old.lens);
                INSERT INTO insights_fts(rowid, content, tags, source, lens)
                VALUES (new.id, new.content, new.tags, new.source, new.lens);
            END;
        """)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ===========================================================================
# Conflict Resolution Strategy
# ===========================================================================
# When two agents submit insights for the **same lens** with **similar
# content** (>=70% token overlap after stripping punctuation), we detect a
# potential conflict and resolve it deterministically:
#
#   1. Higher confidence wins.  confirmed > observed > speculative.
#   2. If confidence is equal, the **newer timestamp** breaks the tie.
#
# The losing insight is NOT discarded -- it is logged, and the caller receives
# a `conflict` status with a reference to the winning insight id.  This
# preserves the full audit trail while preventing duplicate clutter.
#
# Similarity check is a fast token-overlap heuristic (no heavy embedding):
# tokenise both strings, compute Jaccard, threshold at 0.7.
# ===========================================================================

def _tokenize(text):
    """Return a set of lower-cased tokens for similarity comparison."""
    # Very fast heuristic -- just split on whitespace + punctuation boundaries
    import re
    return set(re.findall(r"\w+", text.lower()))


def _find_similar(lens, content, threshold=0.70):
    """
    Return the existing insight (id, confidence, updated_at) that is most
    similar to `content` within the same `lens`, or None.
    """
    db = get_db()
    try:
        candidates = db.execute(
            "SELECT id, content, confidence, updated_at FROM insights "
            "WHERE lens = ? AND stale = 0",
            (lens,),
        ).fetchall()
    finally:
        db.close()

    if not candidates:
        return None

    new_tokens = _tokenize(content)
    best = None
    best_sim = 0.0

    for row in candidates:
        existing_tokens = _tokenize(row["content"])
        if not new_tokens or not existing_tokens:
            continue
        intersection = new_tokens & existing_tokens
        union = new_tokens | existing_tokens
        sim = len(intersection) / len(union) if union else 0.0
        if sim > best_sim:
            best_sim = sim
            best = row

    if best and best_sim >= threshold:
        return best
    return None


def _resolve_conflict(new_conf, new_ts, existing):
    """
    Determine whether the new insight or the existing one wins.
    Returns a dict describing the conflict resolution.
    """
    exist_conf = existing["confidence"]
    exist_rank = CONFIDENCE_RANK.get(exist_conf, 0)
    new_rank = CONFIDENCE_RANK.get(new_conf, 0)

    if new_rank > exist_rank:
        return {
            "winner": "new",
            "reason": (
                f"higher confidence ({new_conf} > {exist_conf})"
            ),
            "existing_id": existing["id"],
        }
    elif exist_rank > new_rank:
        return {
            "winner": "existing",
            "reason": (
                f"lower confidence ({new_conf} < {exist_conf})"
            ),
            "existing_id": existing["id"],
        }
    else:
        # Equal confidence -- newer timestamp wins
        new_wins = new_ts > existing["updated_at"]
        return {
            "winner": "new" if new_wins else "existing",
            "reason": (
                "equal confidence, " +
                ("newer timestamp" if new_wins else "older timestamp, keeping existing")
            ),
            "existing_id": existing["id"],
        }


# ===========================================================================
# Core operations (each with rollback-on-failure)
# ===========================================================================

def _do_add(db, content, source, lens, priority, confidence, tags, now):
    """Raw INSERT helper -- caller manages db + commit/rollback."""
    db.execute(
        "INSERT INTO insights (content, source, lens, priority, confidence, "
        "tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (content, source, lens, priority, confidence, tags, now, now),
    )
    fid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return fid


def add_insight(content, source, lens="general", priority="P1",
                confidence="observed", tags=None):
    """
    Write a curated insight with conflict detection.
    Returns (id, status_dict) where status includes optional conflict info.
    """
    now = datetime.now().isoformat()
    db = get_db()

    # --- Conflict detection ---
    existing = _find_similar(lens, content)
    if existing:
        resolution = _resolve_conflict(confidence, now, existing)
        if resolution["winner"] == "existing":
            logger.info(
                "Conflict resolved: new insight suppressed (existing #%d conf=%s). "
                "Reason: %s",
                existing["id"], existing["confidence"], resolution["reason"],
            )
            db.close()
            return None, {"conflict": "suppressed", **resolution}
        else:
            # New wins -- mark old as stale, then insert new
            logger.info(
                "Conflict resolved: existing #%d marked stale (new conf=%s). "
                "Reason: %s",
                existing["id"], confidence, resolution["reason"],
            )
            db.execute(
                "UPDATE insights SET stale = 1, updated_at = ? WHERE id = ?",
                (now, existing["id"]),
            )

    try:
        fid = _do_add(db, content, source, lens, priority, confidence, tags, now)
        db.commit()
        logger.info("Insight #%d added [%s][%s][%s]: %s", fid, source, lens, confidence, content[:80])
        return fid, None
    except Exception:
        db.rollback()
        logger.exception("Failed to add insight")
        raise
    finally:
        db.close()


def query_profile(lens=None, source=None, priority=None, confidence=None,
                  include_stale=False, limit=20):
    """Pull portrait by lens/source filters. Returns list of dicts."""
    db = get_db()
    try:
        conditions = []
        params = []

        if not include_stale:
            conditions.append("stale = 0")

        if lens:
            lenses = [l.strip() for l in lens.split(",")]
            conditions.append(f"lens IN ({','.join('?' * len(lenses))})")
            params.extend(lenses)

        if source and source != "all":
            sources = [s.strip() for s in source.split(",")]
            conditions.append(f"source IN ({','.join('?' * len(sources))})")
            params.extend(sources)

        if priority:
            conditions.append("priority = ?")
            params.append(priority)

        if confidence:
            conditions.append("confidence = ?")
            params.append(confidence)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"SELECT * FROM insights {where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = db.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("query_profile failed")
        raise
    finally:
        db.close()


def search_insights(query_text, lens=None, limit=20):
    """
    Full-text search across insights.
    Uses FTS5 first; falls back to LIKE for CJK content (FTS5 default
    tokenizer only splits on whitespace, so Chinese characters are not
    indexed as individual tokens).
    """
    db = get_db()
    try:
        # Try FTS5 first
        if lens:
            rows = db.execute(
                "SELECT i.* FROM insights i JOIN insights_fts ft ON i.id = ft.rowid "
                "WHERE insights_fts MATCH ? AND i.lens = ? AND i.stale = 0 "
                "ORDER BY i.created_at DESC LIMIT ?",
                (query_text, lens, limit)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT i.* FROM insights i JOIN insights_fts ft ON i.id = ft.rowid "
                "WHERE insights_fts MATCH ? AND i.stale = 0 "
                "ORDER BY i.created_at DESC LIMIT ?",
                (query_text, limit)
            ).fetchall()

        # If FTS5 returned nothing and the query contains CJK characters,
        # fall back to LIKE search (FTS5 default tokenizer does not
        # handle CJK well since it splits on whitespace only).
        if not rows and _has_cjk(query_text):
            like_pattern = f"%{query_text}%"
            if lens:
                rows = db.execute(
                    "SELECT * FROM insights WHERE content LIKE ? AND lens = ? "
                    "AND stale = 0 ORDER BY created_at DESC LIMIT ?",
                    (like_pattern, lens, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM insights WHERE content LIKE ? "
                    "AND stale = 0 ORDER BY created_at DESC LIMIT ?",
                    (like_pattern, limit),
                ).fetchall()

        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        logger.warning("FTS5 query failed, falling back to LIKE: %s", query_text)
        try:
            like_pattern = f"%{query_text}%"
            if lens:
                rows = db.execute(
                    "SELECT * FROM insights WHERE content LIKE ? AND lens = ? "
                    "AND stale = 0 ORDER BY created_at DESC LIMIT ?",
                    (like_pattern, lens, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM insights WHERE content LIKE ? "
                    "AND stale = 0 ORDER BY created_at DESC LIMIT ?",
                    (like_pattern, limit),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            logger.exception("LIKE fallback also failed")
            return []
    except Exception:
        logger.exception("search_insights failed")
        raise
    finally:
        db.close()


def _has_cjk(text):
    """Return True if text contains any CJK (Chinese/Japanese/Korean) characters."""
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or   # CJK Unified
            0x3400 <= cp <= 0x4DBF or   # CJK Ext-A
            0x20000 <= cp <= 0x2A6DF or # CJK Ext-B
            0xF900 <= cp <= 0xFAFF or   # CJK Compat
            0x2F800 <= cp <= 0x2FA1F):  # CJK Compat Suppl
            return True
    return False


def sync_since(since_timestamp, source=None):
    """Return insights updated after a given timestamp."""
    db = get_db()
    try:
        if source:
            rows = db.execute(
                "SELECT * FROM insights WHERE updated_at > ? AND source = ? ORDER BY updated_at",
                (since_timestamp, source)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM insights WHERE updated_at > ? ORDER BY updated_at",
                (since_timestamp,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("sync_since failed")
        raise
    finally:
        db.close()


def mark_stale(insight_id):
    """Mark an insight as stale."""
    db = get_db()
    try:
        db.execute(
            "UPDATE insights SET stale = 1, updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), insight_id)
        )
        db.commit()
        affected = db.total_changes
        logger.info("Insight #%d marked stale", insight_id)
        return affected > 0
    except Exception:
        db.rollback()
        logger.exception("mark_stale failed for id=%d", insight_id)
        raise
    finally:
        db.close()


def confirm_insight(insight_id):
    """Upgrade confidence to 'confirmed'."""
    db = get_db()
    try:
        db.execute(
            "UPDATE insights SET confidence = 'confirmed', updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), insight_id)
        )
        db.commit()
        affected = db.total_changes
        logger.info("Insight #%d confirmed", insight_id)
        return affected > 0
    except Exception:
        db.rollback()
        logger.exception("confirm_insight failed for id=%d", insight_id)
        raise
    finally:
        db.close()


def archive_stale():
    """Move stale insights to archive.jsonl and remove from DB."""
    db = get_db()
    try:
        stale = db.execute("SELECT * FROM insights WHERE stale = 1").fetchall()

        if not stale:
            return 0

        archive_path = HUB_DIR / "archive.jsonl"
        with open(archive_path, "a", encoding="utf-8") as f:
            for r in stale:
                f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")

        ids = [r["id"] for r in stale]
        db.execute(
            f"DELETE FROM insights WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        db.commit()
        count = len(stale)
        logger.info("Archived %d stale insights", count)
        return count
    except Exception:
        db.rollback()
        logger.exception("archive_stale failed")
        raise
    finally:
        db.close()


def get_stats():
    """Return database statistics."""
    db = get_db()
    try:
        total = db.execute("SELECT COUNT(*) FROM insights").fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM insights WHERE stale = 0").fetchone()[0]
        stale_count = db.execute("SELECT COUNT(*) FROM insights WHERE stale = 1").fetchone()[0]
        sources = db.execute(
            "SELECT source, COUNT(*) as cnt FROM insights WHERE stale = 0 GROUP BY source ORDER BY cnt DESC"
        ).fetchall()
        lenses = db.execute(
            "SELECT lens, COUNT(*) as cnt FROM insights WHERE stale = 0 GROUP BY lens ORDER BY cnt DESC"
        ).fetchall()
        return {
            "total": total,
            "active": active,
            "stale": stale_count,
            "sources": [{"name": s["source"], "count": s["cnt"]} for s in sources],
            "lenses": [{"name": l["lens"], "count": l["cnt"]} for l in lenses],
        }
    except Exception:
        logger.exception("get_stats failed")
        raise
    finally:
        db.close()


# ===========================================================================
# Auto-cleanup: hourly background task
# ===========================================================================
# Runs in a daemon thread inside the server process.
# Two housekeeping tasks:
#   1. Archive P2 insights older than 30 days (even if not manually marked stale).
#   2. Delete low-confidence (speculative) duplicates identified by the
#      conflict-detection heuristic -- when two speculative insights share
#      the same lens and have >=85% token overlap, keep the newer one.
# ===========================================================================
CLEANUP_INTERVAL_SEC = 3600  # 1 hour

def _run_cleanup():
    """Execute one cleanup cycle. Safe -- exceptions are logged, not raised."""
    db = None
    try:
        db = get_db()

        # 1) Auto-stale P2 insights past TTL
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        result = db.execute(
            "UPDATE insights SET stale = 1, updated_at = datetime('now') "
            "WHERE priority = 'P2' AND created_at < ? AND stale = 0",
            (cutoff,),
        )
        db.commit()
        p2_count = result.rowcount
        if p2_count:
            logger.info("Auto-cleanup: marked %d P2 insights as stale (older than 30 days)", p2_count)

        # 2) Deduplicate low-confidence duplicates (speculative, same lens, >=0.85 overlap)
        speculative = db.execute(
            "SELECT id, lens, content, confidence, updated_at FROM insights "
            "WHERE confidence = 'speculative' AND stale = 0"
        ).fetchall()

        deleted = 0
        # Group by lens
        by_lens = {}
        for row in speculative:
            by_lens.setdefault(row["lens"], []).append(row)

        for _lens, group in by_lens.items():
            keep = set()
            # Sort by updated_at descending, keep the newest, mark rest as stale
            sorted_group = sorted(group, key=lambda r: r["updated_at"], reverse=True)
            keep_id = sorted_group[0]["id"]
            for row in sorted_group:
                if row["id"] == keep_id:
                    continue
                # Check token overlap
                tokens_a = _tokenize(sorted_group[0]["content"])
                tokens_b = _tokenize(row["content"])
                if not tokens_a or not tokens_b:
                    continue
                intersection = tokens_a & tokens_b
                union = tokens_a | tokens_b
                sim = len(intersection) / len(union) if union else 0.0
                if sim >= 0.85:
                    db.execute(
                        "UPDATE insights SET stale = 1, updated_at = datetime('now') WHERE id = ?",
                        (row["id"],),
                    )
                    deleted += 1

        if deleted:
            db.commit()
            logger.info("Auto-cleanup: marked %d speculative duplicates as stale", deleted)

    except Exception:
        logger.exception("Auto-cleanup cycle failed")
        if db:
            try:
                db.rollback()
            except Exception:
                pass
    finally:
        if db:
            db.close()


def _cleanup_loop():
    """Background daemon: run cleanup every CLEANUP_INTERVAL_SEC."""
    while True:
        time.sleep(CLEANUP_INTERVAL_SEC)
        logger.debug("Auto-cleanup cycle starting...")
        _run_cleanup()


# ===========================================================================
# HTTP Server
# ===========================================================================
def serve_http(port=8921):
    """Start HTTP server for Memory Hub API."""
    MEMORY_HUB_TOKEN = os.environ.get("MEMORY_HUB_TOKEN", "")

    class HubHandler(http.server.BaseHTTPRequestHandler):

        def _check_auth(self):
            """If MEMORY_HUB_TOKEN is set, validate Authorization header."""
            if not MEMORY_HUB_TOKEN:
                return True
            auth = self.headers.get("Authorization", "")
            expected = f"Bearer {MEMORY_HUB_TOKEN}"
            return hmac.compare_digest(auth, expected)

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length == 0:
                    return None
                raw = self.rfile.read(length)
                return json.loads(raw.decode("utf-8", errors="replace"))
            except (ValueError, json.JSONDecodeError, AttributeError):
                return None

        def _send_json(self, data, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

        def _send_error(self, message, status=400):
            self._send_json({"error": message}, status)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        # ------------------------------------------------------------------
        # GET endpoints
        # ------------------------------------------------------------------
        def do_GET(self):
            if not self._check_auth():
                self._send_error("unauthorized", 401)
                return
            try:
                self._do_GET_impl()
            except Exception:
                logger.exception("Unhandled error in GET %s", self.path)
                try:
                    self._send_error("internal server error", 500)
                except Exception:
                    pass

        def _do_GET_impl(self):
            path = self.path.split("?")[0]
            params = {}
            if "?" in self.path:
                for kv in self.path.split("?")[1].split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        params[k] = v

            if path == "/profile":
                try:
                    lens = params.get("lens")
                    source = params.get("source", "all")
                    priority = params.get("priority")
                    confidence = params.get("confidence")
                    limit = int(params.get("limit", 20))
                    results = query_profile(lens=lens, source=source,
                                            priority=priority, confidence=confidence,
                                            limit=limit)
                    self._send_json({"insights": results, "count": len(results)})
                except Exception:
                    logger.exception("GET /profile failed")
                    self._send_error("database error", 500)

            elif path == "/sync":
                try:
                    since = params.get("since", "1970-01-01T00:00:00")
                    source = params.get("source")
                    results = sync_since(since, source=source)
                    self._send_json({
                        "insights": results,
                        "count": len(results),
                        "synced_at": datetime.now().isoformat(),
                    })
                except Exception:
                    logger.exception("GET /sync failed")
                    self._send_error("database error", 500)

            elif path == "/sources":
                try:
                    stats = get_stats()
                    self._send_json(stats)
                except Exception:
                    logger.exception("GET /sources failed")
                    self._send_error("database error", 500)

            elif path == "/search":
                try:
                    q = params.get("q", "")
                    lens = params.get("lens")
                    if not q:
                        self._send_error("missing 'q' parameter", 400)
                        return
                    results = search_insights(q, lens=lens)
                    self._send_json({"results": results, "count": len(results)})
                except Exception:
                    logger.exception("GET /search failed")
                    self._send_error("database error", 500)

            else:
                self._send_json({"error": "not found", "endpoints": [
                    "GET /profile?lens=&source=&priority=&confidence=&limit=",
                    "GET /sync?since=ISO_TIMESTAMP&source=",
                    "GET /sources",
                    "GET /search?q=&lens=",
                    "POST /insight",
                    "POST /stale",
                    "POST /confirm",
                ]}, 404)

        # ------------------------------------------------------------------
        # POST endpoints
        # ------------------------------------------------------------------
        def do_POST(self):
            if not self._check_auth():
                self._send_error("unauthorized", 401)
                return
            try:
                self._do_POST_impl()
            except Exception:
                logger.exception("Unhandled error in POST %s", self.path)
                try:
                    self._send_error("internal server error", 500)
                except Exception:
                    pass

        def _do_POST_impl(self):
            if self.path == "/insight":
                try:
                    body = self._read_body()
                    if not isinstance(body, dict):
                        return self._send_error("invalid JSON body", 400)

                    content = body.get("content", "").strip()
                    if not content:
                        return self._send_error("content is required", 400)
                    if len(content) > 500:
                        return self._send_error("content exceeds 500 character limit", 400)

                    source = body.get("source", "unknown")
                    lens = body.get("lens", "general")
                    priority = body.get("priority", "P1")
                    confidence = body.get("confidence", "observed")
                    tags = body.get("tags")

                    if priority not in PRIORITIES:
                        return self._send_error(
                            f"priority must be one of {list(PRIORITIES.keys())}", 400,
                        )
                    if confidence not in CONFIDENCE:
                        return self._send_error(
                            f"confidence must be one of {CONFIDENCE}", 400,
                        )

                    try:
                        fid, conflict = add_insight(
                            content, source, lens=lens, priority=priority,
                            confidence=confidence, tags=tags,
                        )
                    except Exception:
                        logger.exception("add_insight failed")
                        return self._send_error("database write failed", 500)

                    if conflict:
                        # Conflict: either suppressed or replaced existing
                        return self._send_json({
                            "id": fid,
                            "status": "conflict",
                            "conflict": conflict,
                        }, 201)
                    else:
                        self._send_json({"id": fid, "status": "recorded"}, 201)
                except Exception:
                    logger.exception("POST /insight failed")
                    self._send_error("internal server error", 500)

            elif self.path == "/stale":
                try:
                    body = self._read_body()
                    if not isinstance(body, dict):
                        return self._send_error("invalid JSON body", 400)
                    insight_id = body.get("id")
                    if not insight_id:
                        return self._send_error("missing 'id'", 400)
                    try:
                        ok = mark_stale(insight_id)
                    except Exception:
                        logger.exception("mark_stale failed")
                        return self._send_error("database error", 500)
                    if ok:
                        self._send_json({"status": "marked_stale", "id": insight_id})
                    else:
                        self._send_error("insight not found", 404)
                except Exception:
                    logger.exception("POST /stale failed")
                    self._send_error("internal server error", 500)

            elif self.path == "/confirm":
                try:
                    body = self._read_body()
                    if not isinstance(body, dict):
                        return self._send_error("invalid JSON body", 400)
                    insight_id = body.get("id")
                    if not insight_id:
                        return self._send_error("missing 'id'", 400)
                    try:
                        ok = confirm_insight(insight_id)
                    except Exception:
                        logger.exception("confirm_insight failed")
                        return self._send_error("database error", 500)
                    if ok:
                        self._send_json({"status": "confirmed", "id": insight_id})
                    else:
                        self._send_error("insight not found", 404)
                except Exception:
                    logger.exception("POST /confirm failed")
                    self._send_error("internal server error", 500)

            elif self.path == "/archive":
                try:
                    count = archive_stale()
                    self._send_json({"status": "archived", "count": count})
                except Exception:
                    logger.exception("POST /archive failed")
                    self._send_error("database error", 500)

            else:
                self._send_json({"error": "not found"}, 404)

        def log_message(self, format, *args):
            """Suppress default stderr access-log noise; we use our own logger."""
            pass

    # --- Start background cleanup thread ---
    cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True, name="cleanup")
    cleanup_thread.start()
    logger.info("Auto-cleanup daemon started (interval: %d s)", CLEANUP_INTERVAL_SEC)

    # --- Start HTTP server ---
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), HubHandler)
    logger.info("Memory Hub listening on http://127.0.0.1:%d (DB: %s)", port, DB_PATH)
    server.serve_forever()


# ===========================================================================
# CLI
# ===========================================================================
if __name__ == "__main__":
    init_db()

    if len(sys.argv) < 2:
        stats = get_stats()
        print("=== Memory Hub ===")
        print(f"活跃画像: {stats['active']}  |  已过时: {stats['stale']}  |  总计: {stats['total']}")
        if stats["sources"]:
            print("\n来源分布:")
            for s in stats["sources"]:
                print(f"  {s['name']}: {s['count']} 条")
        if stats["lenses"]:
            print("\n侧面分布:")
            for l in stats["lenses"]:
                print(f"  {l['name']}: {l['count']} 条")
        print("\n命令: serve [port] | stats | archive")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "serve":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8921
        serve_http(port)

    elif cmd == "stats":
        stats = get_stats()
        print(json.dumps(stats, ensure_ascii=False, indent=2))

    elif cmd == "archive":
        count = archive_stale()
        print(f"已归档 {count} 条过时画像")

    else:
        print(f"未知命令: {cmd}")
        print("命令: serve [port] | stats | archive")
