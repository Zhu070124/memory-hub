# Contributing to Memory Hub

Thanks for contributing. Memory Hub is a lightweight shared memory layer for the
Paofu AI ecosystem — built on SQLite + FTS5, zero dependencies, stdlib-only.

## Quick Links

- **Issue tracker:** [GitHub Issues](https://github.com/Zhu070124/memory-hub/issues)
- **Ecosystem:** [Puff](https://github.com/Zhu070124/puff) (creative agent) · [Workshop](https://github.com/Zhu070124/paofu-creative-workshop) (group chat)

## Getting Started

```bash
git clone https://github.com/Zhu070124/memory-hub.git
cd memory-hub
python hub.py serve          # starts on http://127.0.0.1:8921
python -m pytest tests/ -v   # run the test suite
```

No `pip install` needed — the project uses only Python stdlib (`sqlite3`, `http.server`,
`json`, `unittest`).

## Development Philosophy

1. **Stdlib-first.** Do not add dependencies unless absolutely necessary. The current
   zero-dependency design is a deliberate constraint.
2. **Curation over dumping.** Every insight added to the shared pool must be a meaningful
   observation, not raw conversation. The API enforces this with the 500-char limit.
3. **Deterministic conflict resolution.** When two insights overlap, the system picks a
   winner — it never stores both as active. The losing insight is preserved (marked stale)
   for auditability.
4. **Backward compatibility.** The SQLite schema and API contracts are stable. New features
   must not break existing agent integrations.

## How to Contribute

### Reporting Bugs

Open an issue with:
- Steps to reproduce
- Expected vs actual behavior
- `memory_hub.log` snippet (relevant lines only)
- Python version (`python --version`)
- OS (Windows/macOS/Linux)

### Suggesting Features

Open an issue describing:
- The problem this feature solves
- Which agent(s) would use it
- How it fits the "curation over dumping" philosophy

### Pull Requests

1. Fork the repo and create a branch from `master`
2. Make your changes
3. Add or update tests in `tests/test_insights.py`
4. Run the full test suite: `python -m pytest tests/ -v`
5. Verify the server starts: `python hub.py serve` and hit `http://127.0.0.1:8921/sources`
6. Open a PR against `master` with a clear description

### Commit Style

- Prefix: `feat:` / `fix:` / `docs:` / `test:` / `refactor:`
- Imperative mood, lowercase
- Examples: `fix: retry on SQLITE_BUSY for concurrent writes`, `docs: add troubleshooting section`

## Code Structure

```
hub.py              # HTTP server — all API endpoints live here
client.py           # CLI client for agent integration
hermes_sync.py      # Bidirectional sync with Hermes facts.db
tests/
  test_insights.py  # Unit tests — add new test cases here
insights.db         # SQLite database (gitignored, auto-created at runtime)
memory_hub.log      # Application log (gitignored)
```

All meaningful logic (API handlers, database operations, conflict resolution,
cleanup daemon) lives in `hub.py`. Start there.

## Testing

Tests use Python's `unittest` module (stdlib, no pytest dependency required, but
pytest works as a runner for nicer output).

```bash
# Run all tests
python -m pytest tests/ -v

# Run a single test class
python -m pytest tests/test_insights.py::TestInsights -v

# Run with unittest (no pytest)
python -m unittest tests.test_insights -v
```

Tests use a temporary in-memory SQLite database — no file I/O, fully isolated.

## Database Migrations

Schema changes are handled in `hub.py` via `init_db()`. Add new columns/tables
using `ALTER TABLE ... ADD COLUMN` with `IF NOT EXISTS` guards. Never drop columns
or change types — this breaks existing agent integrations.

## Release Process

1. All tests pass on `master`
2. Tag with semantic version: `git tag v1.x.x`
3. Push tag: `git push origin v1.x.x`
4. GitHub release notes are auto-generated from merged PRs

## Questions?

Open an issue or reach out through the Paofu AI ecosystem channels.
