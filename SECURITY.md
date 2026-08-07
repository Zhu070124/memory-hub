# Security Policy

## Concurrency Protection

Memory Hub uses SQLite WAL (Write-Ahead Logging) journal mode. WAL allows concurrent readers alongside a single writer without blocking. The hub serializes writes internally and uses optimistic row-level conflict detection. This prevents concurrent writers from silently corrupting or overwriting each other's data — conflicting operations receive a clear error response rather than a corrupted result.

## API Key Authentication (Optional)

The hub supports optional API key authentication. When configured, all requests must include a valid `Authorization: Bearer <key>` header. Requests without a valid key are rejected with `401 Unauthorized`. It is recommended to enable API key auth in any production or network-exposed deployment.

## Input Validation

All client-supplied data (insight text, metadata fields, query parameters) is validated and sanitized before storage or query execution. FTS5 queries use parameterized bindings to prevent SQL injection. Insight payloads are length-checked and stripped of non-UTF-8 sequences.

## Conflict Resolution Preventing Corruption

When two clients modify the same insight concurrently, Memory Hub detects the conflict via a version-vector or last-write-timestamp comparison and rejects the later write with a conflict error. This design prevents silent overwrites — the losing client must re-fetch and re-apply its changes on the latest state.

## Reporting a Vulnerability

If you discover a security vulnerability, please do not open a public issue. Instead, report it privately:

1. Email the maintainer with a description and reproduction steps.
2. Allow up to 7 days for an initial response.
3. Once a fix is released, coordinated disclosure is welcome.

We treat all security reports seriously and aim to patch confirmed vulnerabilities promptly.
