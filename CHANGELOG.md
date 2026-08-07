# Changelog

All notable changes to Memory Hub will be documented in this file.

## [Unreleased]

### Added
- **WAL mode + concurrent writes**: SQLite WAL journal mode enabled for concurrent read/write access across multiple clients.
- **Auto-cleanup**: Stale insights and expired entries are automatically pruned to keep the database lean.
- **Conflict resolution**: Deterministic conflict resolution strategy for concurrent modifications, preventing silent data corruption.
- **28 tests**: Comprehensive test suite covering core CRUD, sync, conflict handling, and edge cases.
- **Docker**: Containerized deployment with `Dockerfile` and `docker-compose.yml` for easy setup.
- **Graceful shutdown**: Signal handling for SIGTERM/SIGINT ensures clean database closure and in-flight request completion.
- **API key auth**: Optional API key authentication for securing the hub endpoint.
- **MIT license**: Released under the MIT license.
