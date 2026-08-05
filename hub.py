"""
Memory Hub — 泡芙画像总汇
=========================
Cross-agent curated insight library. Not a raw fact dump — each agent
selectively contributes high-value observations about 泡芙.

Design borrowed from:
  - AI Agent Memory: P0/P1/P2 priority + TTL, L0/L1/L2 hierarchy
  - Agent Memory Hub: governed pipeline, confidence scoring
  - cognitive_engine.py: SQLite + FTS5 pattern

Architecture:
  insights.db (SQLite + FTS5) — curated portrait insights
  HTTP API (stdlib only)       — six endpoints

Insight lifecycle:
  agent observes → writes to private memory (unfiltered)
  agent curates  → POST /insight (selected, high confidence)
  agent queries  → GET /profile?lens=writing (pull what they need)
  paofu confirms → POST /confirm (upgrade confidence)
  becomes stale  → POST /stale (mark for archival)

Usage:
  python hub.py serve [port]   — start HTTP server (default 8921)
  python hub.py stats          — show database stats
"""

import os
import sys
import json
import sqlite3
import http.server
import threading
from pathlib import Path
from datetime import datetime

# ── Paths ──────────────────────────────────────────────────────────────────
HUB_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = HUB_DIR / "insights.db"

# ── Priority / TTL (from AI Agent Memory) ──────────────────────────────────
PRIORITIES = {
    "P0": {"ttl_days": None, "label": "永不过期 — 核心画像"},
    "P1": {"ttl_days": 90,  "label": "90天 — 活跃项目/近期偏好"},
    "P2": {"ttl_days": 30,  "label": "30天 — 临时观察"},
}

# ── Confidence levels (from Agent Memory Hub) ──────────────────────────────
CONFIDENCE = ["confirmed", "observed", "speculative"]

# ── Database ───────────────────────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
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
    db.close()

# ── Core operations ────────────────────────────────────────────────────────
def add_insight(content, source, lens="general", priority="P1",
                confidence="observed", tags=None):
    """Write a curated insight. Returns the new ID."""
    db = get_db()
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO insights (content, source, lens, priority, confidence, tags, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (content, source, lens, priority, confidence, tags, now, now)
    )
    db.commit()
    fid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.close()
    return fid

def query_profile(lens=None, source=None, priority=None, confidence=None,
                  include_stale=False, limit=20):
    """Pull portrait by lens/source filters. Returns list of dicts."""
    db = get_db()
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
    db.close()
    return [dict(r) for r in rows]

def search_insights(query_text, lens=None, limit=20):
    """Full-text search across insights."""
    db = get_db()
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
    db.close()
    return [dict(r) for r in rows]

def sync_since(since_timestamp, source=None):
    """Return insights updated after a given timestamp."""
    db = get_db()
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
    db.close()
    return [dict(r) for r in rows]

def mark_stale(insight_id):
    """Mark an insight as stale."""
    db = get_db()
    db.execute(
        "UPDATE insights SET stale = 1, updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(), insight_id)
    )
    db.commit()
    affected = db.total_changes
    db.close()
    return affected > 0

def confirm_insight(insight_id):
    """Upgrade confidence to 'confirmed'."""
    db = get_db()
    db.execute(
        "UPDATE insights SET confidence = 'confirmed', updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(), insight_id)
    )
    db.commit()
    affected = db.total_changes
    db.close()
    return affected > 0

def archive_stale():
    """Move stale insights to archive."""
    db = get_db()
    stale = db.execute("SELECT * FROM insights WHERE stale = 1").fetchall()

    if not stale:
        db.close()
        return 0

    archive_path = HUB_DIR / "archive.jsonl"
    with open(archive_path, "a", encoding="utf-8") as f:
        for r in stale:
            f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")

    ids = [r["id"] for r in stale]
    db.execute(f"DELETE FROM insights WHERE id IN ({','.join('?'*len(ids))})", ids)
    db.commit()
    count = len(stale)
    db.close()
    return count

def get_stats():
    """Return database statistics."""
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM insights").fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM insights WHERE stale = 0").fetchone()[0]
    stale_count = db.execute("SELECT COUNT(*) FROM insights WHERE stale = 1").fetchone()[0]
    sources = db.execute(
        "SELECT source, COUNT(*) as cnt FROM insights WHERE stale = 0 GROUP BY source ORDER BY cnt DESC"
    ).fetchall()
    lenses = db.execute(
        "SELECT lens, COUNT(*) as cnt FROM insights WHERE stale = 0 GROUP BY lens ORDER BY cnt DESC"
    ).fetchall()
    db.close()
    return {
        "total": total,
        "active": active,
        "stale": stale_count,
        "sources": [{"name": s["source"], "count": s["cnt"]} for s in sources],
        "lenses": [{"name": l["lens"], "count": l["cnt"]} for l in lenses],
    }

# ── HTTP Server ────────────────────────────────────────────────────────────
def serve_http(port=8921):
    """Start HTTP server for Memory Hub API."""

    class HubHandler(http.server.BaseHTTPRequestHandler):

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
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

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?")[0]
            params = {}
            if "?" in self.path:
                for kv in self.path.split("?")[1].split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        params[k] = v

            if path == "/profile":
                lens = params.get("lens")
                source = params.get("source", "all")
                priority = params.get("priority")
                confidence = params.get("confidence")
                limit = int(params.get("limit", 20))
                results = query_profile(lens=lens, source=source,
                                        priority=priority, confidence=confidence,
                                        limit=limit)
                self._send_json({"insights": results, "count": len(results)})

            elif path == "/sync":
                since = params.get("since", "1970-01-01T00:00:00")
                source = params.get("source")
                results = sync_since(since, source=source)
                self._send_json({"insights": results, "count": len(results), "synced_at": datetime.now().isoformat()})

            elif path == "/sources":
                stats = get_stats()
                self._send_json(stats)

            elif path == "/search":
                q = params.get("q", "")
                lens = params.get("lens")
                if not q:
                    self._send_json({"error": "需要 q 参数"}, 400)
                    return
                results = search_insights(q, lens=lens)
                self._send_json({"results": results, "count": len(results)})

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

        def do_POST(self):
            if self.path == "/insight":
                body = self._read_body()
                if not isinstance(body, dict):
                    return self._send_json({"error": "请求格式错误"}, 400)

                content = body.get("content", "").strip()
                if not content:
                    return self._send_json({"error": "content 不能为空"}, 400)
                if len(content) > 500:
                    return self._send_json({"error": "content 超过 500 字符上限"}, 400)

                source = body.get("source", "unknown")
                lens = body.get("lens", "general")
                priority = body.get("priority", "P1")
                confidence = body.get("confidence", "observed")
                tags = body.get("tags")

                if priority not in PRIORITIES:
                    return self._send_json({"error": f"priority 必须是 {list(PRIORITIES.keys())}"}, 400)
                if confidence not in CONFIDENCE:
                    return self._send_json({"error": f"confidence 必须是 {CONFIDENCE}"}, 400)

                fid = add_insight(content, source, lens=lens, priority=priority,
                                  confidence=confidence, tags=tags)
                self._send_json({"id": fid, "status": "recorded"}, 201)

            elif self.path == "/stale":
                body = self._read_body()
                if not isinstance(body, dict):
                    return self._send_json({"error": "请求格式错误"}, 400)
                insight_id = body.get("id")
                if not insight_id:
                    return self._send_json({"error": "需要 id"}, 400)
                if mark_stale(insight_id):
                    self._send_json({"status": "marked_stale", "id": insight_id})
                else:
                    self._send_json({"error": "未找到该记录"}, 404)

            elif self.path == "/confirm":
                body = self._read_body()
                if not isinstance(body, dict):
                    return self._send_json({"error": "请求格式错误"}, 400)
                insight_id = body.get("id")
                if not insight_id:
                    return self._send_json({"error": "需要 id"}, 400)
                if confirm_insight(insight_id):
                    self._send_json({"status": "confirmed", "id": insight_id})
                else:
                    self._send_json({"error": "未找到该记录"}, 404)

            elif self.path == "/archive":
                count = archive_stale()
                self._send_json({"status": "archived", "count": count})

            else:
                self._send_json({"error": "not found"}, 404)

        def log_message(self, format, *args):
            pass  # Silent

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), HubHandler)
    print(f"Memory Hub 已启动: http://127.0.0.1:{port}")
    print(f"  画像库: {DB_PATH}")
    server.serve_forever()

# ── CLI ────────────────────────────────────────────────────────────────────
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
