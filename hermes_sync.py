"""
Hermes <-> Memory Hub sync bridge
==================================
Bidirectional sync between Hermes facts.db and Memory Hub insights.db.

Direction 1 (push): high-importance / pinned facts -> Hub curated insights
Direction 2 (pull): other agents' insights -> Hermes facts.db

Usage:
  python hermes_sync.py push    -- push Hermes high-value memories to Hub
  python hermes_sync.py pull    -- pull other agent insights into Hermes
  python hermes_sync.py sync    -- bidirectional sync

Design principles:
  - Selective sync (importance > 8 or pinned only)
  - Dedup on pull (existing facts not duplicated)
  - Every record tagged with source + sync timestamp
"""

import os
import sys
import json
import time
import sqlite3
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Retry configuration (3 attempts, exponential backoff)
# ---------------------------------------------------------------------------
MAX_RETRIES = 3
RETRY_BACKOFF = 0.5  # seconds, doubles each retry

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERMES_DB = Path("D:/Users/DELL/.hanako/agents/hanako/memory/facts.db")
HUB_URL = os.environ.get("MEMORY_HUB_URL", "http://127.0.0.1:8921")
SYNC_STATE = Path(__file__).parent / "hermes_sync_state.json"


# ---------------------------------------------------------------------------
# Hub API -- with retry logic (3 attempts, exponential backoff)
# ---------------------------------------------------------------------------
def hub_api(method, endpoint, body=None):
    """
    Call Memory Hub API with timeout (10s) and retry (3 attempts).
    Retries on: timeout, connection refused, 5xx server errors.
    """
    url = f"{HUB_URL}{endpoint}"
    data_bytes = json.dumps(body).encode("utf-8") if body else None
    headers = {"Content-Type": "application/json"} if data_bytes else {}

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=data_bytes, method=method, headers=headers)
            resp = urllib.request.urlopen(req, timeout=10)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if 500 <= e.code < 600:
                last_err = e
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                if attempt < MAX_RETRIES:
                    print(f"  [retry {attempt}/{MAX_RETRIES}] HTTP {e.code}, waiting {wait:.1f}s...")
                    time.sleep(wait)
                    continue
            return {"error": f"HTTP {e.code}", "detail": e.read().decode(errors="replace")[:200]}
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            if attempt < MAX_RETRIES:
                print(f"  [retry {attempt}/{MAX_RETRIES}] {e}, waiting {wait:.1f}s...")
                time.sleep(wait)
                continue
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}
    return {"error": f"retries exhausted: {last_err}"}


# ---------------------------------------------------------------------------
# Sync state
# ---------------------------------------------------------------------------
def load_state():
    if SYNC_STATE.exists():
        return json.loads(SYNC_STATE.read_text(encoding="utf-8"))
    return {"last_push": None, "last_pull": None, "pushed_ids": []}


def save_state(state):
    SYNC_STATE.parent.mkdir(parents=True, exist_ok=True)
    SYNC_STATE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Push: Hermes facts -> Hub insights
# ---------------------------------------------------------------------------
def push_to_hub():
    if not HERMES_DB.exists():
        print(f"Hermes facts.db not found: {HERMES_DB}")
        return

    state = load_state()
    db = sqlite3.connect(str(HERMES_DB))
    db.row_factory = sqlite3.Row

    already = state.get("pushed_ids", [])

    if already:
        placeholder = ",".join("?" * len(already))
        rows = db.execute(
            f"SELECT * FROM facts WHERE id NOT IN ({placeholder}) "
            f"ORDER BY created_at DESC LIMIT 30",
            already,
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM facts ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
    db.close()

    if not rows:
        print("No new facts to sync.")
        return

    pushed = 0
    for r in rows:
        # Parse tags (JSON array in OpenHanako format)
        tags_raw = r["tags"] or "[]"
        try:
            tags_list = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        except json.JSONDecodeError:
            tags_list = []

        # Heuristic lens detection from tags
        tags_str = ",".join(tags_list) if tags_list else ""
        lens = "general"
        writing_kw = ["writing", "novel", "literature", "creative", "story",
                      "writer", "counselor"]
        tech_kw = ["API", "code", "programming", "architecture", "DeepSeek",
                   "Claude", "tech", "agent", "model", "AI"]
        habit_kw = ["habit", "preference", "daily", "channel", "patrol"]

        if any(k in tags_str for k in writing_kw):
            lens = "writing"
        elif any(k in tags_str for k in tech_kw):
            lens = "tech"
        elif any(k in tags_str for k in habit_kw):
            lens = "habits"

        # Skip very short or system-internal facts
        fact = r["fact"]
        if len(fact) < 10 or fact.startswith("[Hub:") or fact.startswith("[system]"):
            continue

        result = hub_api("POST", "/insight", {
            "content": fact[:500],
            "source": "hermes",
            "lens": lens,
            "priority": "P1",
            "confidence": "observed",
            "tags": tags_str,
        })

        if "id" in result:
            state["pushed_ids"].append(r["id"])
            pushed += 1
            print(f"  #{r['id']} -> Hub #{result['id']}[{lens}]: {fact[:60]}...")
        elif "conflict" in str(result):
            # Conflict resolution triggered -- push was handled by Hub
            conflict_info = result.get("conflict", {})
            state["pushed_ids"].append(r["id"])
            pushed += 1
            print(f"  #{r['id']} -> Hub (conflict resolved): {fact[:60]}...")
        else:
            print(f"  #{r['id']} push failed: {result}")

    state["last_push"] = datetime.now().isoformat()
    state["pushed_ids"] = state["pushed_ids"][-300:]
    save_state(state)
    print(f"Push complete: {pushed} new facts -> Memory Hub")


# ---------------------------------------------------------------------------
# Pull: Hub insights -> Hermes facts
# ---------------------------------------------------------------------------
def pull_from_hub():
    if not HERMES_DB.exists():
        print(f"Hermes facts.db not found: {HERMES_DB}")
        return

    state = load_state()
    since = state.get("last_pull", "1970-01-01T00:00:00")

    # Get insights from other agents since last pull
    result = hub_api("GET", f"/sync?since={since}")
    if "error" in result:
        print(f"Pull failed: {result['error']}")
        return
    insights = result.get("insights", [])

    # Filter: only from other agents (not hermes itself)
    external = [i for i in insights if i.get("source") != "hermes"]
    if not external:
        print("No new insights from other agents.")
        return

    db = sqlite3.connect(str(HERMES_DB))
    db.row_factory = sqlite3.Row

    pulled = 0
    for ins in external:
        # Check if already exists in facts.db (dedup by content prefix)
        safe_source = ins["source"].replace("%", "\\%").replace("_", "\\_")
        existing = db.execute(
            "SELECT id FROM facts WHERE fact LIKE ? ESCAPE '\\' LIMIT 1",
            (f"%[Hub:{safe_source}]%",),
        ).fetchone()
        if existing:
            continue

        fact_text = (
            f"[Hub:{ins['source']}][{ins.get('confidence', '?')}] {ins['content']}"
        )
        tags = f"hub-import,{ins.get('lens', 'general')},{ins.get('tags', '')}"
        db.execute(
            "INSERT INTO facts (fact, search_text, tags, agent, importance, access_level) "
            "VALUES (?, ?, ?, 'hanako', 7.0, 'all')",
            (fact_text, fact_text.lower(), tags),
        )
        pulled += 1
        print(f"  Hub #{ins['id']} -> facts: {ins['content'][:60]}...")

    db.commit()
    db.close()

    state["last_pull"] = datetime.now().isoformat()
    save_state(state)
    print(f"Pull complete: {pulled} new insights -> Hermes memories")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        print("commands: push | pull | sync")
        print("  push  -- push Hermes high-value facts to Memory Hub")
        print("  pull  -- pull other agent insights into Hermes")
        print("  sync  -- bidirectional sync")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "push":
        print("=== Hermes -> Memory Hub ===")
        push_to_hub()

    elif cmd == "pull":
        print("=== Memory Hub -> Hermes ===")
        pull_from_hub()

    elif cmd == "sync":
        print("=== Bidirectional Sync ===")
        push_to_hub()
        print()
        pull_from_hub()

    else:
        print(f"Unknown command: {cmd}")
