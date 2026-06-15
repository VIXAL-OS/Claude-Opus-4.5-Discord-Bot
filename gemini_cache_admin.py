#!/usr/bin/env python3
"""List (and optionally delete) Gemini cachedContents for this project's API key.

Gemini context caches bill by *storage* (per token-hour) for their whole TTL,
whether or not they're ever read. Orphaned caches = silent cost. This lists
what's live so you can clear leaks.

Usage:
    python gemini_cache_admin.py            # list only (safe dry run)
    python gemini_cache_admin.py --delete   # delete ALL listed caches
"""
import json
import os
import sys
import urllib.request
import urllib.error
from dotenv import load_dotenv

load_dotenv()
KEY = os.getenv("GEMINI_API_KEY")
BASE = "https://generativelanguage.googleapis.com/v1beta"


def _req(method: str, url: str) -> dict:
    r = urllib.request.Request(url, method=method)
    r.add_header("x-goog-api-key", KEY or "")
    with urllib.request.urlopen(r, timeout=60) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}


def main() -> None:
    if not KEY:
        print("GEMINI_API_KEY not found in environment/.env")
        return
    do_delete = "--delete" in sys.argv
    try:
        data = _req("GET", f"{BASE}/cachedContents")
    except urllib.error.HTTPError as e:
        print(f"List failed: HTTP {e.code}: {e.read().decode('utf-8')[:300]}")
        return

    caches = data.get("cachedContents", [])
    if not caches:
        print("No live cachedContents for this key. Nothing to clean up.")
        return

    print(f"Found {len(caches)} live cache(s):")
    total_tokens = 0
    for c in caches:
        toks = int(c.get("usageMetadata", {}).get("totalTokenCount", 0))
        total_tokens += toks
        print(
            f"  - {c.get('name','?')} | model={c.get('model','?')} | {toks:,} tokens "
            f"| created={c.get('createTime','?')} | expires={c.get('expireTime','?')} "
            f"| {c.get('displayName','')}"
        )
    print(f"Total cached tokens across all caches: {total_tokens:,}")

    if not do_delete:
        print("\n(dry run — re-run with --delete to remove these)")
        return

    print("\nDeleting...")
    for c in caches:
        name = c.get("name")
        try:
            _req("DELETE", f"{BASE}/{name}")
            print(f"  deleted {name}")
        except urllib.error.HTTPError as e:
            print(f"  FAILED {name}: HTTP {e.code}: {e.read().decode('utf-8')[:200]}")
    print("Done. Re-run without --delete to confirm the list is empty.")


if __name__ == "__main__":
    main()
