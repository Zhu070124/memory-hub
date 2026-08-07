"""
Memory Hub CLI Client
=====================
Lightweight Python client for Memory Hub. Called by agents via subprocess.

Usage:
  python client.py share "content" --source hermes --lens writing
  python client.py profile --lens writing
  python client.py sync 2026-08-06T00:00:00 --source hermes
  python client.py search "writing"
  python client.py stats
  python client.py stale 42
  python client.py confirm 42
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
import urllib.parse

HUB_URL = os.environ.get("MEMORY_HUB_URL", "http://127.0.0.1:8921")

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------
MAX_RETRIES = 3
RETRY_BACKOFF = 0.5  # seconds, doubles each retry

# ---------------------------------------------------------------------------
# API helper with retry logic (3 attempts, exponential backoff)
# ---------------------------------------------------------------------------
def api(method, endpoint, body=None):
    """
    Call Memory Hub API with timeout (10s) and retry (3 attempts).
    Retries on: timeout, connection refused, 502/503/504.
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
            # 5xx server errors are retryable; 4xx are not
            if 500 <= e.code < 600:
                last_err = e
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                if attempt < MAX_RETRIES:
                    time.sleep(wait)
                    continue
            return {"error": f"HTTP {e.code}", "detail": e.read().decode(errors="replace")[:200]}
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
            wait = RETRY_BACKOFF * (2 ** (attempt - 1))
            if attempt < MAX_RETRIES:
                time.sleep(wait)
                continue
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}
    # Exhausted retries on 5xx
    return {"error": f"HTTP {last_err.code} (retries exhausted)" if last_err and hasattr(last_err, "code") else str(last_err)}


def format_insights(insights):
    """Format insights for agent-friendly display."""
    if not insights:
        print("(no insights)")
        return
    for ins in insights:
        badge = {"confirmed": "V", "observed": "~", "speculative": "?"}.get(
            ins.get("confidence", ""), ""
        )
        p = ins.get("priority", "?")
        src = ins.get("source", "?")
        lens = ins.get("lens", "?")
        print(f"  #{ins['id']} [{src}][{badge}][{p}][{lens}] {ins['content']}")


if __name__ == "__main__":
    # Force UTF-8 on Windows
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        print("commands: share | profile | sync | search | stats | stale | confirm")
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "share":
        content = args[0] if args else ""
        source = "hermes"
        lens = "general"
        priority = "P1"
        confidence = "observed"
        tags = None

        i = 1
        while i < len(args):
            if args[i] == "--source" and i + 1 < len(args):
                source = args[i + 1]; i += 2
            elif args[i] == "--lens" and i + 1 < len(args):
                lens = args[i + 1]; i += 2
            elif args[i] == "--priority" and i + 1 < len(args):
                priority = args[i + 1]; i += 2
            elif args[i] == "--confidence" and i + 1 < len(args):
                confidence = args[i + 1]; i += 2
            else:
                i += 1

        if not content:
            print("usage: client.py share <content> [--source hermes] [--lens writing]")
            sys.exit(1)

        result = api("POST", "/insight", {
            "content": content, "source": source, "lens": lens,
            "priority": priority, "confidence": confidence, "tags": tags,
        })
        if "id" in result:
            status = result.get("status", "recorded")
            if status == "conflict":
                conflict = result.get("conflict", {})
                print(f"! Conflict: existing #{conflict.get('existing_id')} "
                      f"({conflict.get('reason', '')}). "
                      f"New insight #{result['id']}.")
            else:
                print(f"V Written to hub #{result['id']}")
        else:
            print(f"X Write failed: {result}")

    elif cmd == "profile":
        lens = None
        source = None
        limit = 20

        i = 0
        while i < len(args):
            if args[i] == "--lens" and i + 1 < len(args):
                lens = args[i + 1]; i += 2
            elif args[i] == "--source" and i + 1 < len(args):
                source = args[i + 1]; i += 2
            elif args[i] == "--limit" and i + 1 < len(args):
                limit = int(args[i + 1]); i += 2
            else:
                i += 1

        endpoint = f"/profile?limit={limit}"
        if lens:
            endpoint += f"&lens={lens}"
        if source:
            endpoint += f"&source={source}"

        result = api("GET", endpoint)
        print("Memory Hub profile:")
        format_insights(result.get("insights", []))

    elif cmd == "sync":
        since = args[0] if args else "1970-01-01T00:00:00"
        source = None
        if "--source" in args:
            idx = args.index("--source")
            source = args[idx + 1] if idx + 1 < len(args) else None

        endpoint = f"/sync?since={since}"
        if source:
            endpoint += f"&source={source}"

        result = api("GET", endpoint)
        print(f"Since {since} ({result.get('count', 0)} insights):")
        format_insights(result.get("insights", []))

    elif cmd == "search":
        query = args[0] if args else ""
        if not query:
            print("usage: client.py search <keyword>")
            sys.exit(1)
        result = api("GET", f"/search?q={urllib.parse.quote(query)}")
        print(f"Search '{query}':")
        format_insights(result.get("results", []))

    elif cmd == "stats":
        result = api("GET", "/sources")
        print("=== Memory Hub Stats ===")
        print(f"Active: {result.get('active', 0)} | Stale: {result.get('stale', 0)} | Total: {result.get('total', 0)}")
        for s in result.get("sources", []):
            print(f"  {s['name']}: {s['count']} insights")
        print("\nLens distribution:")
        for l in result.get("lenses", []):
            print(f"  {l['name']}: {l['count']} insights")

    elif cmd == "stale":
        if not args:
            print("usage: client.py stale <id>")
            sys.exit(1)
        try:
            stale_id = int(args[0])
        except ValueError:
            print(f"Invalid ID: {args[0]}")
            sys.exit(1)
        result = api("POST", "/stale", {"id": stale_id})
        print("V Marked stale" if "marked_stale" in result.get("status", "") else f"X {result}")

    elif cmd == "confirm":
        if not args:
            print("usage: client.py confirm <id>")
            sys.exit(1)
        try:
            confirm_id = int(args[0])
        except ValueError:
            print(f"Invalid ID: {args[0]}")
            sys.exit(1)
        result = api("POST", "/confirm", {"id": confirm_id})
        print("V Confirmed" if "confirmed" in result.get("status", "") else f"X {result}")

    else:
        print(f"Unknown command: {cmd}")
        print("commands: share | profile | sync | search | stats | stale | confirm")
