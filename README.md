# Memory Hub -- Cross-Agent Memory Middleware

> Part of the Paofu AI ecosystem — the shared memory layer. See also: [Puff](https://github.com/Zhu070124/puff) (creative agent) · [Workshop](https://github.com/Zhu070124/paofu-creative-workshop) (group chat)

> A lightweight shared memory layer for multi-agent systems. Agents selectively contribute
> curated insights about a user -- not raw conversation dumps. SQLite + FTS5, RESTful API,
> zero dependencies.

[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://python.org)
[![stdlib](https://img.shields.io/badge/deps-0%20(zero)-brightgreen)]()
[![License](https://img.shields.io/badge/license-MIT-orange)](LICENSE)
[![Ecosystem](https://img.shields.io/badge/Paofu_AI-ecosystem-7C3AED)](https://github.com/Zhu070124)

---

## The Problem

In a multi-agent system, each agent develops its own understanding of the user. Agent A knows
the user prefers minimal prose. Agent B doesn't. Agent C has a brilliant observation about the
user's workflow -- but no one else ever sees it.

Existing solutions fall short:
- **Mem0 / LangChain Memory** -- full conversation dumps. 99% noise, dilutes signal
- **Shared database** -- no curation mechanism; agents pollute the shared pool with raw chatter
- **Manual user-profile files** -- stale, not real-time, not cross-agent

---

## How Memory Hub Solves It

```
POST /insight              GET /profile
  ┌──────────┐           ┌──────────────┐           ┌──────────┐
  │  Claude  │ ────────> │              │ <──────── │  Hermes  │
  │   Code   │           │  Memory Hub  │           │          │
  └──────────┘           │  (SQLite)    │           └──────────┘
                         │              │
  ┌──────────┐  GET /sync│  6 endpoints │ POST /stale ┌──────────┐
  │   Puff   │ <──────── │  63 curated  │ ──────────> │   Paofu  │
  └──────────┘           │   insights   │             └──────────┘
                         └──────────────┘
```

Three design principles:

1. **Curation over dumping.** Agents write to their own private memory first.
   They only `POST /insight` when they have a high-confidence observation worth sharing.

2. **Confidence grading.** Every insight is tagged `confirmed` (user-verified),
   `observed` (agent observation), or `speculative` (educated guess).
   Conflicts are resolved deterministically, not ignored.

3. **Traceability.** Every insight records its source agent. If an insight turns
   out wrong, you know who to debug.

---

## Data Model

```sql
insights (
    content     TEXT,       -- Curated insight (<=500 chars)
    source      TEXT,       -- Which agent wrote this
    lens        TEXT,       -- Category: writing/tech/personality/habits/projects/general
    confidence  TEXT,       -- confirmed / observed / speculative
    priority    TEXT,       -- P0 (permanent) / P1 (90 days) / P2 (30 days)
    tags        TEXT,       -- Searchable tags
    stale       INTEGER     -- Marked for archival
)
-- FTS5 full-text index for Chinese + English search
```

### Lens Categories

| Lens | What it tracks | Example |
|------|---------------|---------|
| `writing` | Style preferences, genre interests | "Prefers minimal description, dislikes verbose adjectives" |
| `tech` | Technical skills, project preferences | "Agent direction, judgment > implementation details" |
| `personality` | Character traits, MBTI, values | "Pride and softness coexist, strong rebuttal instinct" |
| `habits` | Work patterns, routines | "Most productive in early morning hours" |
| `projects` | Active projects, goals | "FDE development roadmap, novel 'Yulan Jin' completed" |
| `general` | Uncategorized observations | -- |

---

## API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `POST /insight` | Write | Agent contributes a curated insight |
| `GET /profile?lens=writing` | Query | Pull insights by category |
| `GET /sync?since=ISO_timestamp` | Sync | Incremental pull (new since timestamp) |
| `GET /sources` | Stats | Contribution stats per agent |
| `GET /search?q=term` | Search | Full-text search across insights |
| `POST /confirm` | Confirm | Upgrade insight confidence (observed -> confirmed) |
| `POST /stale` | Archive | Mark outdated insight for cleanup |
| `POST /archive` | Archive | Archive all stale insights to archive.jsonl |

---

## Quick Start

> 📸 **Screenshots & demo**: see `./assets/` (coming soon)

Memory Hub is the foundation — start it first, then launch other agents.

```bash
# Start the server
python hub.py serve
# Listens on http://127.0.0.1:8921

# CLI usage
python client.py share "User prefers minimal style" --source puff --lens writing
python client.py profile --lens tech,writing
python client.py search "Agent"
python client.py stats
```

Or double-click `hub.cmd` on Windows.

### Docker

```bash
docker-compose up -d
# SQLite DB mounted at ./data/insights.db
```

---

## Integration

### From Python

```python
import urllib.request, json

# Write an insight
req = urllib.request.Request(
    "http://127.0.0.1:8921/insight",
    data=json.dumps({
        "content": "Prefers clean architecture over clever tricks",
        "source": "claude-code",
        "lens": "tech",
        "confidence": "observed",
        "priority": "P1"
    }).encode(),
    headers={"Content-Type": "application/json"}
)
urllib.request.urlopen(req)

# Query profile
data = json.loads(
    urllib.request.urlopen("http://127.0.0.1:8921/profile?lens=tech").read()
)
```

### From any language

Just HTTP + JSON. No SDK needed.

---

## Why SQLite + FTS5?

- **Zero setup.** No Postgres, no Redis, no Docker. One file, one process.
- **Full-text search.** FTS5 handles Chinese + English mixed queries.
- **Portable.** Copy `insights.db` to another machine -- that's the entire dataset.
- **Stdlib only.** `sqlite3` and `http.server` are in Python's standard library.
  No `pip install` required.
- **Concurrent safety.** WAL mode + busy timeout + write retry. Multiple agents can
  read simultaneously; writers retry automatically on lock contention.

---

## Performance & Optimization

### Current profile

| Metric | Value |
|--------|-------|
| Storage engine | SQLite + FTS5 |
| Insight count | ~1,000 (tested) |
| Search latency | sub-ms (1,000 insights, English FTS5, single-thread) (FTS5 index) |
| Write latency | < 5 ms (WAL mode) |
| Concurrent readers | unlimited (WAL) |
| Concurrent writers | 1 (serialized with retry) |

### Bottleneck

The current bottleneck is the **single-threaded HTTP server** (`http.server.ThreadingHTTPServer`).
While it spawns a thread per request, Python's GIL means CPU-bound work is serialized.
For the current scale (< 100 agents, < 10 req/s) this is not a problem.

### Optimization path

1. **Switch to `aiohttp` or `FastAPI`** if request volume grows beyond ~50 req/s.
   FastAPI + `aiosqlite` gives true async I/O and proper connection pooling.
2. **Add an in-process LRU cache** for `/profile` queries (TTL 30s) to avoid
   hitting SQLite on every read.
3. **Consider LiteFS or `litestream`** for continuous DB replication if
   high-availability is needed.
4. **Connection pooling.** SQLite works best with a single connection; the
   current `get_db()` pattern is deliberate. If switching to a client-server
   DB, use a pool like `SQLAlchemy` with `QueuePool`.

---

## Conflict Resolution Strategy

When two agents contribute insights for the **same lens** with **similar content**
(>= 70% token overlap, Jaccard similarity on whitespace-delimited tokens), the
system detects a conflict and resolves it deterministically:

1. **Higher confidence wins.**
   `confirmed` (user-verified) > `observed` (agent observation) > `speculative` (educated guess).

2. **Newer timestamp breaks ties.**
   If confidence levels are equal, the insight with the more recent `updated_at` wins.

The losing insight is **not discarded** -- it is marked `stale`, preserving the
full audit trail. The API response includes a `conflict` status with the
winning insight's ID and the resolution reason.

### Example

```
Agent A: POST /insight  { lens: "writing", confidence: "speculative",
                          content: "Prefers short, punchy sentences" }
  -> #42 created

Agent B: POST /insight  { lens: "writing", confidence: "observed",
                          content: "Likes short, punchy sentence structure" }
  -> Conflict detected (70%+ token overlap)
  -> #42 confidence = speculative < observed = new insight
  -> #42 marked stale, #43 created
  -> Response: { "id": 43, "status": "conflict",
                 "conflict": { "winner": "new", "existing_id": 42,
                               "reason": "higher confidence (observed > speculative)" } }
```

---

## File Structure

```
memory-hub/
├── hub.py                  # HTTP server (stdlib only)
├── client.py               # CLI client for agents to call
├── hermes_sync.py          # Bidirectional sync with Hermes facts.db
├── hub.cmd                 # Windows launcher
├── insights.db             # SQLite database (auto-created)
├── memory_hub.log          # Application log (auto-created)
├── tests/
│   └── test_insights.py    # Unit tests (add, query, search, confirm, stale)
├── Dockerfile              # Container image
├── docker-compose.yml      # Docker deployment with DB volume mount
├── README.md
└── LICENSE
```

---

## Auto-Cleanup

Memory Hub runs a background cleanup daemon (hourly) that:

1. **Archives stale P2 insights** -- P2 insights older than 30 days are auto-marked stale.
2. **Deduplicates speculative insights** -- low-confidence duplicates (>= 85% token overlap,
   same lens) are consolidated; the newest is kept, older ones marked stale.

No configuration needed -- it starts automatically with `python hub.py serve`.

---

## Safety Specification

Memory Hub handles concurrent writes from multiple agents. The following safeguards are in place
to prevent data corruption and ensure data integrity.

### Concurrent Write Protection

SQLite is configured in **WAL (Write-Ahead Logging) mode**, which allows unlimited concurrent
readers while serializing writers. WAL mode ensures that readers never block writers and writers
never block readers — only writer-vs-writer contention must be resolved.

**Busy timeout + retry:** SQLite is configured with a busy timeout (5 seconds). If a write
encounters a locked database, the engine waits up to the timeout before returning
`SQLITE_BUSY`. The application layer adds a **retry loop (3 attempts)** with exponential
backoff (100ms, 200ms, 400ms) to absorb transient lock contention between agents.

### Input Validation

| Constraint | Limit | Enforcement |
|-----------|-------|-------------|
| `content` max length | 500 characters | Rejected at API boundary (HTTP 400) |
| `source` required | Non-empty string | Rejected at API boundary (HTTP 400) |
| `lens` enum | `writing/tech/personality/habits/projects/general` | Rejected at API boundary (HTTP 400) |
| `confidence` enum | `confirmed/observed/speculative` | Rejected at API boundary (HTTP 400) |
| `priority` enum | `P0/P1/P2` | Rejected at API boundary (HTTP 400) |
| Request body size | 64 KB max | Rejected by HTTP server |

### API Error Codes

| Code | Meaning | When |
|------|---------|------|
| **400** | Bad Request | Missing required field, invalid enum value, content exceeds 500 chars |
| **404** | Not Found | Insight ID does not exist for confirm/stale operations |
| **409** | Conflict | Confidence-based conflict resolution triggered (response includes winner info) |
| **500** | Internal Server Error | Unexpected database error, retry-safe |

### Confidence-Based Conflict Resolution Preventing Data Corruption

When two agents contribute insights with **>= 70% token overlap** for the same lens,
the system resolves the conflict deterministically rather than storing both as active:

1. **Higher confidence wins** — `confirmed` > `observed` > `speculative`
2. **Newer timestamp breaks ties** — if confidence levels are equal
3. **Loser is marked stale, not deleted** — full audit trail preserved

This prevents the memory pool from accumulating contradictory or duplicate information,
which would degrade the quality of all downstream agent queries.

---

## Troubleshooting

### Port 8921 Occupied

```
OSError: [Errno 10048] Only one usage of each socket address is normally permitted
```

**Cause:** Another instance of Memory Hub (or another application) is already bound to port 8921.

**Fix:**
```bash
# Windows: find and kill the existing process
netstat -ano | findstr :8921
taskkill /PID <PID> /F

# Linux/macOS:
lsof -i :8921
kill -9 <PID>
```
Or start Memory Hub on a different port:
```bash
python hub.py serve --port 8922
```

### SQLite Database Locked Error

```
sqlite3.OperationalError: database is locked
```

**Cause:** A long-running write transaction is blocking other writers. This is rare under WAL
mode but can occur if a writer holds a transaction open without committing.

**Fix:**
1. Wait 5-10 seconds for the busy timeout to clear the lock (automatic retry handles this in most cases)
2. If persistent, stop Memory Hub, run `sqlite3 insights.db "PRAGMA wal_checkpoint(TRUNCATE);"`,
   then restart
3. Check `memory_hub.log` for long-running queries that may be holding locks

### Memory Hub Not Auto-Starting

**Cause:** `hub.cmd` or systemd/launchd service may not be configured correctly.

**Fix:**
- **Windows (`hub.cmd`):** Ensure the script is in the Windows Startup folder
  (`Win+R` > `shell:startup`) or run it manually after login
- **Docker:** Verify `docker-compose up -d` completed without errors;
  check `docker ps` to confirm the container is running
- **Systemd (Linux):** Check `systemctl status memory-hub` for error logs

### FTS5 Search Returning No Results (CJK Fallback)

**Cause:** SQLite FTS5's default tokenizer (`unicode61`) does not handle CJK
(Chinese/Japanese/Korean) characters well — it tokenizes by whitespace, but CJK text
often has no spaces between words.

**Fix:** Memory Hub registers a custom tokenizer using the ICU extension when available.
If FTS5 returns no results for a Chinese query:
1. Verify the query is in the database: `python client.py search "<term>"` (with quotes for exact match)
2. The system automatically falls back to **SQL `LIKE '%term%'`** when FTS5 returns zero results,
   ensuring CJK queries still work (slower but functional)
3. For best CJK search performance, compile SQLite with the ICU extension enabled

### hermes_sync.py Connection Refused

```
ConnectionRefusedError: [WinError 1225] No connection could be made
```

**Cause:** Memory Hub server is not running when `hermes_sync.py` attempts to sync.

**Fix:**
1. Start Memory Hub first: `python hub.py serve`
2. Verify it's listening: `curl http://127.0.0.1:8921/sources` should return JSON
3. If using Docker, ensure port 8921 is published: check `docker-compose.yml` has `ports: - "8921:8921"`
4. Check Windows Firewall is not blocking port 8921 (localhost traffic is usually exempt)

---

## Future Iteration

### Short-Term: Async HTTP with aiohttp

Replace the current `http.server.ThreadingHTTPServer` (stdlib, synchronous) with
[aiohttp](https://docs.aiohttp.org/) for true async I/O. This eliminates GIL contention
under concurrent load and enables proper connection pooling. Expected impact: handle
**50+ req/s** (up from the current ~10 req/s ceiling).

### Medium-Term: Semantic Search via Embedding Vectors

Replace (or supplement) FTS5 keyword search with **embedding-based semantic search**.
Store a vector embedding for each insight (generated via a lightweight local model
like `all-MiniLM-L6-v2` or via API). Query-time: embed the search query, compute
cosine similarity, return top-K. This eliminates the CJK tokenization problem entirely
and catches conceptual matches that keyword search misses (e.g., "efficient writing"
matching "minimal prose").

### Long-Term: Distributed LiteFS Replication

For multi-machine deployment (e.g., a Memory Hub instance per developer machine
that stays in sync), adopt [LiteFS](https://github.com/superfly/litefs) for
SQLite-level replication. LiteFS replicates the WAL to a consensus cluster,
providing read-your-writes consistency across nodes. This would allow each agent
to read/write a local SQLite file while staying synchronized with a shared truth.

---

## Test Coverage

The test suite (`tests/test_insights.py`) covers all core operations.
Run with:
```bash
python -m pytest tests/ -v
```

| Area | Tests | Coverage |
|------|-------|----------|
| **CRUD — Add** | Insight creation with all fields, validation (missing fields, invalid enums, content > 500 chars) | `test_add_insight`, `test_add_insight_validation` |
| **CRUD — Query** | Profile queries with lens filter, empty result handling, multi-lens queries | `test_profile_by_lens`, `test_profile_empty` |
| **CRUD — Confirm** | Upgrade confidence (speculative -> observed, observed -> confirmed), confirm non-existent ID | `test_confirm_insight`, `test_confirm_not_found` |
| **CRUD — Stale** | Mark insight stale, verify stale flag, stale non-existent ID | `test_mark_stale`, `test_stale_not_found` |
| **Search** | FTS5 keyword search, CJK fallback (Chinese LIKE ~5-20ms at 1K scale) (LIKE `%term%`), search with no results | `test_search`, `test_search_cjk_fallback`, `test_search_no_results` |
| **Conflict Resolution** | Confidence priority (confirmed > observed > speculative), timestamp tiebreaker, conflict response format | `test_conflict_resolution`, `test_conflict_tiebreaker` |
| **Auto-Cleanup** | P2 expiry (30-day auto-stale), deduplication (>= 85% overlap consolidation) | `test_p2_expiry`, `test_dedup` |
| **Sync** | Incremental sync since timestamp, empty sync (no new data), full sync | `test_sync_since`, `test_sync_empty` |
| **Stats / Sources** | Source contribution counts, stats format validation | `test_sources_stats`, `test_stats_format` |

---

## License

MIT (c) 2026 Zhu Zhi (Paofu)
